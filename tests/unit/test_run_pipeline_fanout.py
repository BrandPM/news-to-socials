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

    IT_PROJ_NTS_051 (S6.4-bis): the pipeline now splits text vs image
    generation into two helpers — ``generate_image_for_topic`` (called
    ONCE per topic) and ``generate_draft_for_language`` (called per
    (topic, language)). ``fail_for_language`` failure-injection points
    at the per-language draft step now."""
    from pipeline import run as pipe

    fake_items = [
        RawItem(
            source_id="s",
            source_name="s",
            url="https://test.example.com/a",
            title="Cross-border tax structuring for expats",
            summary="Summary A about cross-border tax structuring.",
        ),
        RawItem(
            source_id="s",
            source_name="s",
            url="https://test.example.com/b",
            title="Family office operations under new EU rules",
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

    image_call_log: list[str] = []

    async def fake_generate_image(topic, brand, sanity_publisher):
        # Track topic ids to assert "once per topic, not per language".
        image_call_log.append(topic.id)
        return f"asset-{topic.id}"

    monkeypatch.setattr(pipe, "generate_image_for_topic", fake_generate_image)

    # NTS_092: research is a paid per-topic call. Stub the seam so no test in
    # this module can reach the network, and record the calls so "once per
    # topic, not per language" is assertable the same way images are.
    research_call_log: list[str] = []

    async def fake_build_fact_pack(topic, *, research_enabled=True, budget=None):
        research_call_log.append(topic.id)
        if not research_enabled:
            return None
        from pipeline.generator.research import Fact, FactPack

        return FactPack(
            source_facts=[
                Fact(text="Acme raised $5m.", url="https://reuters.com/acme")
            ],
            citations=["https://reuters.com/acme"],
        )

    monkeypatch.setattr(pipe, "build_fact_pack_for_topic", fake_build_fact_pack)

    async def fake_generate_draft(topic, brand, language, fact_pack=None):
        if fail_for_language is not None and language.value == fail_for_language:
            raise RuntimeError(f"forced failure for {language.value}")
        return Draft(
            topic_id=topic.id,
            brand_id="icon",
            language=language,
            title=topic.raw.title,
            body="Body for " + topic.raw.title,
            key_takeaway="kt",
        )

    monkeypatch.setattr(pipe, "generate_draft_for_language", fake_generate_draft)

    # NTS_065: non-EN drafts now flow through translate_draft_for_language,
    # which takes the canonical EN draft and returns its translation. The
    # fake echoes the EN draft under the target language so the fanout
    # count/shape assertions still hold; failure injection points here too.
    async def fake_translate_draft(topic, brand, language, en_draft):
        if fail_for_language is not None and language.value == fail_for_language:
            raise RuntimeError(f"forced failure for {language.value}")
        return Draft(
            topic_id=en_draft.topic_id,
            brand_id=en_draft.brand_id,
            language=language,
            title=en_draft.title,
            body=en_draft.body,
            key_takeaway=en_draft.key_takeaway,
        )

    monkeypatch.setattr(pipe, "translate_draft_for_language", fake_translate_draft)

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
    # Attach the image call log on the fake_sanity object so tests can
    # assert "image generated once per topic, not 4× per language".
    fake_sanity.image_call_log = image_call_log  # type: ignore[attr-defined]
    fake_sanity.research_call_log = research_call_log  # type: ignore[attr-defined]
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


def test_non_en_drafts_are_translations_of_the_canonical_en_draft(
    fresh_admin_db_with_source, monkeypatch
):
    """NTS_065 core guarantee: every non-EN draft is produced by
    translate_draft_for_language fed the EN draft for the same topic — NOT a
    native generation from the topic. We capture the en_draft handed to the
    translate seam and assert it is the EN-branch output."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    _mock_externals(monkeypatch)

    from pipeline import run as pipe

    translate_calls: list[tuple[str, str]] = []
    real_translate = pipe.translate_draft_for_language

    async def spy_translate(topic, brand, language, en_draft):
        # The source must be the canonical EN draft for THIS topic.
        translate_calls.append((language.value, en_draft.language.value))
        assert en_draft.language == Language.en
        assert en_draft.topic_id == topic.id
        return await real_translate(topic, brand, language, en_draft)

    monkeypatch.setattr(pipe, "translate_draft_for_language", spy_translate)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=1, dry_run=False))

    # 1 topic × 3 non-EN languages = 3 translate calls, each from EN.
    assert len(translate_calls) == 3
    assert {c[0] for c in translate_calls} == {"ru", "uk", "pl"}
    assert all(src == "en" for _, src in translate_calls)


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


def test_run_pipeline_for_run_writes_live_progress_across_sources(
    fresh_admin_db_with_source, monkeypatch
):
    """NTS_068: run_pipeline_for_run keeps the run RUNNING with live X/N
    progress across a multi-source run-all, accumulates per-source stats, and
    writes one authoritative finish. We stub run_pipeline so each 'source'
    just records its per-source stats (mimicking record_run_finish)."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    factory = admin_db.get_session_factory()

    # Add a 2nd source so the loop exercises the between-sources re-assert.
    with factory() as session:
        session.add(
            Source(
                brand_id_fk=icon_id,
                name="Second Feed",
                source_type="rss",
                url="https://test.example.com/feed2",
                primary_category="wealth",
                active=True,
            )
        )
        session.commit()
        src_ids = [
            s.id
            for s in session.scalars(
                select(Source).where(Source.brand_id_fk == icon_id)
            )
        ]
        run = Run(
            brand_id_fk=icon_id,
            triggered_by="manual",
            source_ids=json.dumps(src_ids),
            started_at=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
            status="running",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    from pipeline import run as pipe
    from pipeline.admin.config_client import AdminConfigClient

    async def fake_run_pipeline(*args, **kwargs):
        rid = kwargs["existing_run_id"]
        # Mimic a per-source run finishing: write that source's stats + a
        # terminal status on the shared run row.
        AdminConfigClient(brand_slug="icon").record_run_finish(
            rid, status="success",
            stats={"fetched": 10, "scored": 2, "drafted": 2, "errors": 0},
        )
        return []

    monkeypatch.setattr(pipe, "run_pipeline", fake_run_pipeline)

    from pipeline.run import run_pipeline_for_run

    asyncio.run(run_pipeline_for_run(run_id))

    with factory() as session:
        run = session.get(Run, run_id)
        progress = json.loads(run.progress)
        assert progress["sources_total"] == 2
        assert progress["sources_done"] == 2
        assert progress["stage"] == "done"
        # 2 sources × 2 drafted each = 4 accumulated (not last-source-only).
        assert progress["drafts"] == 4
        assert progress["errors"] == 0
        # Authoritative finish: terminal status + finished_at + aggregate stats.
        assert run.status == "success"
        assert run.finished_at is not None
        assert json.loads(run.stats)["drafted"] == 4


def test_run_pipeline_for_run_writes_topics_with_real_source_id(
    fresh_admin_db_with_source, monkeypatch
):
    """Regression for IT_PROJ_NTS_050: ``run_pipeline_for_run`` used to
    pass ``source_id=str(source.id)`` + ``source_url`` into
    ``run_pipeline``, which routed through the override branch and
    hard-coded ``SourceRecord.id=None``. That made every
    ``record_topic_result`` call silently no-op, so the topics table
    stayed empty across every production run (see run 10).

    With the fix, the override path looks up the real DB row and
    preserves ``source.id`` so per-topic rows land with a valid FK.
    """
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "uk"])
    _mock_externals(monkeypatch)

    factory = admin_db.get_session_factory()
    with factory() as session:
        src = session.scalars(
            select(Source).where(Source.brand_id_fk == icon_id)
        ).first()
        assert src is not None
        run = Run(
            brand_id_fk=icon_id,
            triggered_by="manual",
            source_ids=json.dumps([src.id]),
            started_at=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
            status="running",
        )
        session.add(run)
        session.commit()
        run_id = run.id
        src_id = src.id

    from pipeline.run import run_pipeline_for_run

    asyncio.run(run_pipeline_for_run(run_id))

    with factory() as session:
        topics = list(
            session.scalars(select(Topic).where(Topic.run_id == run_id))
        )
        # 2 fake items × 2 languages = 4 topic rows. All must point at
        # the real source.id (FK preserved), not None.
        assert len(topics) == 4, f"got {len(topics)} topics, expected 4"
        for t in topics:
            assert t.source_id == src_id, (
                f"topic.source_id={t.source_id} but expected {src_id}"
            )
            assert t.status == "passed"


def test_run_pipeline_generates_image_once_per_topic_not_per_language(
    fresh_admin_db_with_source, monkeypatch
):
    """IT_PROJ_NTS_051: with 4 languages × 2 topics, image generation must
    fire exactly 2 times total (once per topic) — not 8 (per (topic, lang)).
    All 8 drafts must reference the same asset id within each topic."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    image_calls = fake_sanity.image_call_log  # type: ignore[attr-defined]
    # 2 topics, image gen called for each ONCE — not 8 (was 4x bug).
    assert len(image_calls) == 2, (
        f"expected 2 image calls (one per topic), got {len(image_calls)}: "
        f"{image_calls}"
    )
    assert len(set(image_calls)) == 2  # distinct topic ids

    # Drafts: 2 topics × 4 langs = 8. Each topic's 4 drafts share asset id.
    assert len(fake_sanity.created) == 8
    by_topic: dict[str, set[str]] = {}
    for post in fake_sanity.created:
        by_topic.setdefault(post.topic_id, set()).add(
            str(post.cover_image_asset_id)
        )
    for topic_id, asset_ids in by_topic.items():
        assert len(asset_ids) == 1, (
            f"topic {topic_id} has multiple asset ids: {asset_ids}"
        )


def test_run_pipeline_image_failure_does_not_block_drafts(
    fresh_admin_db_with_source, monkeypatch
):
    """If image generation fails for a topic, the 4 language drafts must
    still publish (with cover_image_asset_id=None). The cost win we want
    to preserve cuts both ways — we can't let an image-API hiccup nuke
    the entire fanout."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline import run as pipe

    async def failing_image(topic, brand, sanity_publisher):
        return None  # mimic generate_image_for_topic's swallow-and-log

    monkeypatch.setattr(pipe, "generate_image_for_topic", failing_image)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    assert len(fake_sanity.created) == 8
    for post in fake_sanity.created:
        assert post.cover_image_asset_id is None


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
