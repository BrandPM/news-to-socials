"""S6.4 — pipeline fanout per language tests.

These verify the outer-loop-over-languages behaviour added to
``run_pipeline``:

* Brand's ``languages`` JSON drives how many times each topic flows
  through generation/publication.
* An explicit ``language=`` argument still pins the run to a single
  language (backwards-compat with the CLI and legacy tests).
* ``runs.languages_completed`` gains an entry as each language's
  branch finishes (success or partial failure).
* A failure inside one language doesn't abort the other languages.
* Per-topic rows carry the right ``language`` value.

Externals (RSS, OpenAI, Sanity) are mocked exactly as
``test_run_admin_integration`` does.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Brand, PipelineConfig, Run, Source, Topic
from pipeline.common import config as config_module
from pipeline.common.models import Language, RawItem
from pipeline.run import _languages_for_brand
from tests.unit.conftest import seed_icon_brand


# --- helpers --------------------------------------------------------------


@pytest.fixture
def fresh_admin_db_with_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv(
        "BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session, with_sanity_creds=True)
        session.add(
            Source(
                brand_id_fk=icon_id,
                name="Test Feed",
                source_type="rss",
                url="https://test.example.com/feed",
                primary_category="wealth",
                active=True,
            )
        )
        session.add(
            PipelineConfig(
                brand_id_fk=icon_id,
                scoring_threshold=6,
                topics_per_run=3,
                banned_phrases=json.dumps(["delve into"]),
                voice_profile="mission: edited via admin\n",
            )
        )
        session.commit()
    yield {"path": tmp_path / "admin.db", "icon_id": icon_id}
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def _set_brand_languages(brand_id: int, codes: list[str]) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand = session.get(Brand, brand_id)
        assert brand is not None
        brand.languages = json.dumps(codes)
        session.commit()


def _mock_externals(monkeypatch, fail_for_language: str | None = None):
    """Stub OpenAI scoring/categorisation, embeddings, image gen, Sanity.

    If ``fail_for_language`` is set, ``generate_with_image`` raises a
    RuntimeError whenever that language is requested — used to drive the
    failure-isolation test."""
    from pipeline import run as pipe

    fake_items = [
        RawItem(
            source_id="s",
            source_name="s",
            url="https://test.example.com/a",
            title="A wealth story",
            summary="Summary A about cross-border tax structuring.",
        ),
        RawItem(
            source_id="s",
            source_name="s",
            url="https://test.example.com/b",
            title="B wealth story",
            summary="Summary B about family office operations.",
        ),
    ]

    async def fake_fetch(self):
        return fake_items

    monkeypatch.setattr("pipeline.sources.rss.RssSource.fetch", fake_fetch)

    async def fake_score(items, brand, *, min_score, limit_pool):
        return [(it, 8) for it in items[:limit_pool]]

    monkeypatch.setattr(pipe, "score_relevant_topics", fake_score)

    async def fake_embed(text, *, model="text-embedding-3-small"):
        return np.zeros(8, dtype=np.float32)

    monkeypatch.setattr(pipe, "_embed", fake_embed)

    async def fake_assign(item, brand):
        return "wealth"

    monkeypatch.setattr(pipe, "assign_category", fake_assign)

    from pipeline.common.models import Draft

    async def fake_generate(topic, brand, language, sanity_publisher):
        if fail_for_language is not None and language.value == fail_for_language:
            raise RuntimeError(f"forced failure for {language.value}")
        draft = Draft(
            topic_id=topic.id,
            brand_id="icon",
            language=language,
            title=topic.raw.title,
            body="Body for " + topic.raw.title,
            key_takeaway="kt",
        )
        return draft, None

    monkeypatch.setattr(pipe, "generate_with_image", fake_generate)

    class FakeSanity:
        def __init__(self) -> None:
            self.created: list[dict] = []

        async def is_topic_already_posted(self, topic_id, language):
            return False

        async def upload_cover_image(self, image_bytes, filename):
            return "asset-id"

        async def publish_draft(self, post):
            self.created.append(post)
            return f"drafts.{post.language.value}-{post.topic_id}"

    fake_sanity = FakeSanity()
    monkeypatch.setattr(pipe, "SanityPublisher", lambda *a, **kw: fake_sanity)
    return fake_sanity


# --- _languages_for_brand pure unit tests ---------------------------------


def test_languages_for_brand_reads_json_column():
    class FakeBrand:
        languages = '["en","ru","uk","pl"]'

    result = _languages_for_brand(FakeBrand())
    assert result == [Language.en, Language.ru, Language.uk, Language.pl]


def test_languages_for_brand_falls_back_to_en_for_missing_column():
    class FakeBrand:
        languages = None

    result = _languages_for_brand(FakeBrand())
    assert result == [Language.en]


def test_languages_for_brand_falls_back_to_en_for_malformed_json():
    class FakeBrand:
        languages = "{not-json"

    result = _languages_for_brand(FakeBrand())
    assert result == [Language.en]


def test_languages_for_brand_skips_unknown_codes():
    """An unknown code is logged and dropped; valid codes still pass."""
    class FakeBrand:
        languages = '["en","xx","ru"]'

    result = _languages_for_brand(FakeBrand())
    assert result == [Language.en, Language.ru]


def test_languages_for_brand_override_pins_to_single_language():
    """The CLI / legacy callers still pass language=Language.en explicitly;
    the override beats the brand list."""
    class FakeBrand:
        languages = '["en","ru","uk","pl"]'

    result = _languages_for_brand(FakeBrand(), override=Language.ru)
    assert result == [Language.ru]


def test_languages_for_brand_dedup_codes():
    class FakeBrand:
        languages = '["en","en","ru"]'

    result = _languages_for_brand(FakeBrand())
    assert result == [Language.en, Language.ru]


# --- end-to-end fanout via run_pipeline ----------------------------------


def test_run_pipeline_fans_out_to_every_brand_language(
    fresh_admin_db_with_source, monkeypatch
):
    """With brand.languages = [en, ru, uk, pl] the pipeline produces 4
    drafts per topic and 4 Topic rows per topic_id."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    results = asyncio.run(
        run_pipeline(brand_slug="icon", limit=2, dry_run=False)
    )

    # 2 topics × 4 languages = 8 drafts total.
    assert len(fake_sanity.created) == 8
    languages_seen = {post.language.value for post in fake_sanity.created}
    assert languages_seen == {"en", "ru", "uk", "pl"}

    factory = admin_db.get_session_factory()
    with factory() as session:
        runs = list(session.scalars(select(Run)))
        assert len(runs) == 1
        assert runs[0].status == "success"
        completed = json.loads(runs[0].languages_completed)
        assert set(completed) == {"en", "ru", "uk", "pl"}

        topics = list(session.scalars(select(Topic)))
        # 2 topics × 4 languages = 8 rows.
        assert len(topics) == 8
        for t in topics:
            assert t.language in {"en", "ru", "uk", "pl"}
            assert t.status == "passed"
        # Every topic_id has exactly 4 language rows (the shared topic_id
        # is the multilingual sibling key that the admin UI groups on).
        by_topic_id: dict[str, set[str]] = {}
        for t in topics:
            by_topic_id.setdefault(t.topic_id, set()).add(t.language)
        for tid, langs in by_topic_id.items():
            assert langs == {"en", "ru", "uk", "pl"}, (tid, langs)


def test_run_pipeline_single_language_override_pins_to_one_branch(
    fresh_admin_db_with_source, monkeypatch
):
    """Existing CLI callers pass language=Language.en; we must keep
    producing exactly one draft per topic even when the brand publishes
    more languages."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(
        run_pipeline(
            brand_slug="icon",
            language=Language.en,
            limit=2,
            dry_run=False,
        )
    )

    assert len(fake_sanity.created) == 2
    for post in fake_sanity.created:
        assert post.language == Language.en

    factory = admin_db.get_session_factory()
    with factory() as session:
        runs = list(session.scalars(select(Run)))
        completed = json.loads(runs[0].languages_completed)
        assert completed == ["en"]


def test_run_pipeline_failure_in_one_language_is_isolated(
    fresh_admin_db_with_source, monkeypatch
):
    """A RuntimeError raised inside the RU branch must NOT prevent the
    EN/UK/PL branches from completing."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch, fail_for_language="ru")

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    # 2 topics × 3 surviving languages = 6 drafts; RU produced 0.
    assert len(fake_sanity.created) == 6
    assert all(p.language != Language.ru for p in fake_sanity.created)

    factory = admin_db.get_session_factory()
    with factory() as session:
        run = list(session.scalars(select(Run)))[0]
        # Run finishes with status='failed' because the failure count is
        # non-zero, but every language still appears in
        # languages_completed — completion means "fanout reached this
        # language", success/failure is captured separately in stats.
        assert run.status == "failed"
        stats = json.loads(run.stats)
        assert stats["errors"] >= 2  # at least 2 RU topic failures
        completed = json.loads(run.languages_completed)
        assert set(completed) == {"en", "ru", "uk", "pl"}

        # Confirm RU topic rows exist with status='failed' so the admin
        # UI shows the failure surface (you can re-run just RU later).
        ru_topics = list(
            session.scalars(select(Topic).where(Topic.language == "ru"))
        )
        assert len(ru_topics) == 2
        assert all(t.status == "failed" for t in ru_topics)
