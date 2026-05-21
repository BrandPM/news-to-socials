"""Schema + cascade tests for pipeline/admin/models.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    PipelineConfig,
    Prompt,
    Run,
    Source,
    Topic,
)


@pytest.fixture
def session(tmp_path):
    """Fresh DB per test, so cascade / unique tests are isolated."""
    db_path = tmp_path / "admin.db"
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=db_path)
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
        admin_db.reset_for_tests()


def _make_source(session, **overrides) -> Source:
    defaults: dict = dict(
        brand_id="icon",
        name="Private Banker International",
        source_type="rss",
        url="https://example.com/feed",
        primary_category="wealth",
        active=True,
        paywall=False,
        polling_minutes=720,
    )
    defaults.update(overrides)
    src = Source(**defaults)
    session.add(src)
    session.flush()
    return src


def _make_run(session, **overrides) -> Run:
    defaults: dict = dict(
        brand_id="icon",
        triggered_by="manual",
        source_ids="[1]",
        started_at=datetime.now(tz=timezone.utc),
        status="running",
    )
    defaults.update(overrides)
    run = Run(**defaults)
    session.add(run)
    session.flush()
    return run


# --- Schema basics --------------------------------------------------------


def test_all_five_tables_exist(session) -> None:
    names = set(inspect(session.bind).get_table_names())
    assert {"sources", "prompts", "pipeline_config", "runs", "topics"} <= names


def test_source_type_check_constraint_rejects_unknown(session) -> None:
    src = Source(
        brand_id="icon",
        name="bad",
        source_type="podcast",  # not in {rss, web, telegram}
        url="https://example.com",
        primary_category="wealth",
    )
    session.add(src)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_prompt_type_check_constraint_rejects_unknown(session) -> None:
    p = Prompt(
        brand_id="icon",
        prompt_type="something_else",
        version_name="v1",
        content="...",
    )
    session.add(p)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_partial_unique_index_allows_many_inactive_prompts(session) -> None:
    for i in range(3):
        session.add(
            Prompt(
                brand_id="icon",
                prompt_type="writer_polish",
                version_name=f"v{i}",
                content="x",
                is_active=False,
            )
        )
    session.flush()


def test_partial_unique_index_blocks_two_active_prompts_same_type(session) -> None:
    session.add(
        Prompt(
            brand_id="icon",
            prompt_type="writer_polish",
            version_name="v1",
            content="x",
            is_active=True,
        )
    )
    session.flush()
    session.add(
        Prompt(
            brand_id="icon",
            prompt_type="writer_polish",
            version_name="v2",
            content="y",
            is_active=True,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_partial_unique_index_allows_active_per_type(session) -> None:
    session.add_all(
        [
            Prompt(
                brand_id="icon",
                prompt_type="writer_polish",
                version_name="vp",
                content="x",
                is_active=True,
            ),
            Prompt(
                brand_id="icon",
                prompt_type="writer_draft",
                version_name="vd",
                content="y",
                is_active=True,
            ),
        ]
    )
    session.flush()  # both active, different types — must succeed


# --- Cascade rules --------------------------------------------------------


def test_deleting_run_cascades_to_topics(session) -> None:
    src = _make_source(session)
    run = _make_run(session)
    session.add(
        Topic(
            run_id=run.id,
            topic_id="t1",
            source_id=src.id,
            title="X",
            status="passed",
        )
    )
    session.flush()
    assert session.scalars(select(Topic)).all()

    session.delete(run)
    session.flush()
    assert session.scalars(select(Topic)).all() == []


def test_deleting_source_with_topics_is_restricted(session) -> None:
    src = _make_source(session)
    run = _make_run(session)
    session.add(
        Topic(
            run_id=run.id,
            topic_id="t1",
            source_id=src.id,
            title="X",
            status="passed",
        )
    )
    session.flush()

    session.delete(src)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_unique_topic_per_run(session) -> None:
    src = _make_source(session)
    run = _make_run(session)
    session.add(
        Topic(
            run_id=run.id,
            topic_id="dup",
            source_id=src.id,
            title="A",
            status="passed",
        )
    )
    session.flush()
    session.add(
        Topic(
            run_id=run.id,
            topic_id="dup",  # same topic_id, same run → violation
            source_id=src.id,
            title="B",
            status="passed",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_same_topic_id_across_different_runs_allowed(session) -> None:
    src = _make_source(session)
    run1 = _make_run(session)
    run2 = _make_run(session)
    session.add_all(
        [
            Topic(
                run_id=run1.id,
                topic_id="t",
                source_id=src.id,
                title="A",
                status="passed",
            ),
            Topic(
                run_id=run2.id,
                topic_id="t",
                source_id=src.id,
                title="B",
                status="passed",
            ),
        ]
    )
    session.flush()
    assert len(session.scalars(select(Topic)).all()) == 2


def test_pipeline_config_singleton_per_brand(session) -> None:
    session.add(
        PipelineConfig(
            brand_id="icon",
            scoring_threshold=7,
            topics_per_run=3,
            voice_profile="mission: x",
        )
    )
    session.flush()
    session.add(
        PipelineConfig(
            brand_id="icon",  # same PK
            scoring_threshold=8,
            topics_per_run=4,
            voice_profile="y",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
