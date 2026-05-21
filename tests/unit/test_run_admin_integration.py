"""End-to-end test of pipeline/run.py against admin.db.

Mocks every external service (OpenAI scoring + embeddings, Sanity, image
generation) so we can run the whole orchestrator in <1 second and verify
that admin.db is read for sources/config AND written for runs/topics.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import numpy as np
import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.models import PipelineConfig, Run, Source, Topic
from pipeline.common import config as config_module
from pipeline.common.models import Language, RawItem


@pytest.fixture
def fresh_admin_db_with_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            Source(
                brand_id="icon",
                name="Test Feed",
                source_type="rss",
                url="https://test.example.com/feed",
                primary_category="wealth",
                active=True,
            )
        )
        session.add(
            PipelineConfig(
                brand_id="icon",
                scoring_threshold=6,
                topics_per_run=3,
                banned_phrases=json.dumps(["delve into"]),
                voice_profile="mission: edited via admin\n",
            )
        )
        session.commit()
    yield tmp_path / "admin.db"
    admin_db.reset_for_tests()


def _mock_externals(monkeypatch):
    """Stub OpenAI scoring/categorisation, embeddings, image gen, Sanity."""
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

    async def fake_fetch(self):  # noqa: ANN001
        return fake_items

    monkeypatch.setattr(
        "pipeline.sources.rss.RssSource.fetch", fake_fetch
    )

    async def fake_score(items, brand, *, min_score, limit_pool):  # noqa: ANN001
        # Score everything at 8 so it passes any threshold ≤8.
        return [(it, 8) for it in items[:limit_pool]]

    monkeypatch.setattr(pipe, "score_relevant_topics", fake_score)

    async def fake_embed(text, *, model="text-embedding-3-small"):  # noqa: ANN001
        return np.zeros(8, dtype=np.float32)

    monkeypatch.setattr(pipe, "_embed", fake_embed)

    async def fake_assign(item, brand):  # noqa: ANN001
        return "wealth"

    monkeypatch.setattr(pipe, "assign_category", fake_assign)

    from pipeline.common.models import Draft

    async def fake_generate(topic, brand, language, sanity_publisher):  # noqa: ANN001
        draft = Draft(
            topic_id=topic.id,
            brand_id="icon",
            language=language,
            title=topic.raw.title,
            body="Body for " + topic.raw.title,
            key_takeaway="kt",
        )
        return draft, None  # no image

    monkeypatch.setattr(pipe, "generate_with_image", fake_generate)

    # Sanity publisher: never call the real one — replace with a stub that
    # records draft creations.
    class FakeSanity:
        def __init__(self) -> None:
            self.created: list[dict] = []

        async def is_topic_already_posted(self, topic_id, language):  # noqa: ANN001
            return False

        async def upload_cover_image(self, image_bytes, filename):  # noqa: ANN001
            return "asset-id"

        async def publish_draft(self, post):  # noqa: ANN001
            self.created.append(post)
            return f"drafts.post-{post.topic_id}"

    fake_sanity = FakeSanity()
    monkeypatch.setattr(pipe, "SanityPublisher", lambda *a, **kw: fake_sanity)
    return fake_sanity


def test_run_pipeline_reads_admin_db_writes_runs_topics(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    results = asyncio.run(
        run_pipeline(
            brand_slug="icon",
            language=Language.en,
            limit=2,
            dry_run=False,
            triggered_by="cron",
        )
    )
    # Two topics → two drafts.
    assert len(results) == 2
    assert len(fake_sanity.created) == 2

    # Run + topics rows written.
    factory = admin_db.get_session_factory()
    with factory() as session:
        runs = list(session.scalars(select(Run)))
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "success"
        assert run.finished_at is not None
        stats = json.loads(run.stats)
        assert stats["drafted"] == 2
        assert stats["fetched"] == 2

        topics = list(session.scalars(select(Topic).where(Topic.run_id == run.id)))
        assert len(topics) == 2
        assert {t.status for t in topics} == {"passed"}
        assert all(t.draft_id and t.draft_id.startswith("drafts.post-") for t in topics)


def test_run_pipeline_falls_back_when_admin_db_missing(tmp_path, monkeypatch) -> None:
    """The systemd timer must keep working until S3 ships, even if
    admin.db doesn't exist on the VPS. The pipeline must:

    1. NOT raise because admin.db is absent.
    2. Use the hardcoded seed source list (Private Banker active).
    3. NOT write to runs/topics tables (there are none).
    """
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "missing.db"))
    admin_db.reset_for_tests()

    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    results = asyncio.run(
        run_pipeline(
            brand_slug="icon",
            language=Language.en,
            limit=2,
            dry_run=False,
        )
    )
    assert len(results) == 2  # two drafts created from the fallback source
    # admin.db never came into existence — verify.
    assert not (tmp_path / "missing.db").exists()


def test_run_pipeline_with_source_id_url_overrides_admin_db(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The systemd timer currently passes --source-id/--source-url. Those
    overrides must take precedence over admin.db's source list so we keep
    backwards compatibility during the rollout.
    """
    _mock_externals(monkeypatch)
    from pipeline.run import run_pipeline

    results = asyncio.run(
        run_pipeline(
            brand_slug="icon",
            source_id="privatebanker",
            source_url="https://www.privatebankerinternational.com/feed/",
            language=Language.en,
            limit=2,
            dry_run=False,
        )
    )
    # The override source has id=None → no topics rows written (FK would
    # fail), but the run row should still record finish status.
    factory = admin_db.get_session_factory()
    with factory() as session:
        runs = list(session.scalars(select(Run)))
        # source_ids was [] (override has no DB id), but the run was recorded.
        assert len(runs) == 1
        assert json.loads(runs[0].source_ids) == []
        topics = list(session.scalars(select(Topic)))
        # Override path doesn't write topics (no source_id) — that's fine
        # for the back-compat window.
        assert topics == []
    assert len(results) == 2


def test_run_pipeline_for_run_executes_existing_row(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """``run_pipeline_for_run`` is the entry point used by the BackgroundTasks
    queue (``POST /sources/{id}/run``). It must update the pre-existing
    run row instead of creating a new one.
    """
    _mock_externals(monkeypatch)

    factory = admin_db.get_session_factory()
    with factory() as session:
        src = session.scalars(select(Source)).first()
        assert src is not None
        run = Run(
            brand_id="icon",
            triggered_by="manual",
            source_ids=json.dumps([src.id]),
            started_at=datetime.now(tz=timezone.utc),
            status="running",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    from pipeline.run import run_pipeline_for_run

    asyncio.run(run_pipeline_for_run(run_id))

    with factory() as session:
        rows = list(session.scalars(select(Run)))
        # Still exactly one run — same row updated, not a sibling.
        assert len(rows) == 1
        assert rows[0].id == run_id
        assert rows[0].status == "success"
        assert rows[0].finished_at is not None
