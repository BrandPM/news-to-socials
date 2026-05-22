"""Tests for AdminConfigClient — both DB-backed and fallback paths.

The fallback (Admin-UI-Specific Invariant B from NTS_014) keeps the
pipeline working when admin.db is missing OR the brand row hasn't been
seeded yet. Step 4 removes the fallback; Step 2 still relies on it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.config_client import AdminConfigClient
from pipeline.admin.models import PipelineConfig, Prompt, Run, Source, Topic
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand


@pytest.fixture
def fresh_admin_db(tmp_path, monkeypatch):
    """Schema-migrated admin.db + seeded Icon brand."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        session.commit()
    yield {"path": tmp_path / "admin.db", "icon_id": icon_id}
    admin_db.reset_for_tests()


@pytest.fixture
def no_admin_db(tmp_path, monkeypatch):
    """Settings point at a non-existent admin.db path."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "nope.db"))
    admin_db.reset_for_tests()
    yield tmp_path / "nope.db"
    admin_db.reset_for_tests()


# --- fallback path ------------------------------------------------------


def test_fallback_when_db_missing(no_admin_db) -> None:
    client = AdminConfigClient()
    assert client.admin_db_available() is False
    sources = client.get_active_sources()
    assert len(sources) == 1
    assert "Private Banker" in sources[0].name


def test_fallback_when_db_present_but_no_active_sources(fresh_admin_db) -> None:
    client = AdminConfigClient()
    assert client.admin_db_available() is True
    sources = client.get_active_sources()
    # No sources in DB → fallback to seed list.
    assert len(sources) == 1
    assert "Private Banker" in sources[0].name


def test_db_drives_sources_when_present(fresh_admin_db) -> None:
    icon_id = fresh_admin_db["icon_id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add_all(
            [
                Source(
                    brand_id_fk=icon_id,
                    name="Custom Feed A",
                    source_type="rss",
                    url="https://feed-a.example.com/rss",
                    primary_category="wealth",
                    active=True,
                ),
                Source(
                    brand_id_fk=icon_id,
                    name="Custom Feed B",
                    source_type="rss",
                    url="https://feed-b.example.com/rss",
                    primary_category="ma",
                    active=False,
                ),
            ]
        )
        session.commit()

    sources = AdminConfigClient().get_active_sources()
    assert len(sources) == 1
    assert sources[0].name == "Custom Feed A"
    assert sources[0].id is not None


def test_get_active_prompt_returns_db_row(fresh_admin_db) -> None:
    icon_id = fresh_admin_db["icon_id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            Prompt(
                brand_id_fk=icon_id,
                prompt_type="writer_polish",
                version_name="v9",
                content="hello",
                is_active=True,
            )
        )
        session.commit()
    res = AdminConfigClient().get_active_prompt("writer_polish")
    assert res == ("v9", "hello")


def test_get_active_prompt_none_when_db_missing(no_admin_db) -> None:
    assert AdminConfigClient().get_active_prompt("writer_polish") is None


def test_get_config_fallback_uses_seed_threshold(no_admin_db) -> None:
    cfg = AdminConfigClient().get_config()
    assert cfg.scoring_threshold == 7
    assert cfg.topics_per_run == 3
    assert isinstance(cfg.banned_phrases, list)
    assert "mission" in cfg.voice_profile.lower()


def test_get_config_reads_db_row(fresh_admin_db) -> None:
    icon_id = fresh_admin_db["icon_id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            PipelineConfig(
                brand_id_fk=icon_id,
                scoring_threshold=9,
                topics_per_run=5,
                banned_phrases=json.dumps(["zzz"]),
                voice_profile="mission: edited\n",
            )
        )
        session.commit()
    cfg = AdminConfigClient().get_config()
    assert cfg.scoring_threshold == 9
    assert cfg.topics_per_run == 5
    assert cfg.banned_phrases == ["zzz"]
    assert cfg.voice_profile == "mission: edited\n"


# --- run + topic recording ---------------------------------------------


def test_record_run_lifecycle(fresh_admin_db) -> None:
    icon_id = fresh_admin_db["icon_id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        src = Source(
            brand_id_fk=icon_id,
            name="x",
            source_type="rss",
            url="https://example.com/feed",
            primary_category="wealth",
        )
        session.add(src)
        session.commit()
        src_id = src.id

    client = AdminConfigClient()
    run_id = client.record_run_start(source_ids=[src_id], triggered_by="manual")
    assert isinstance(run_id, int)

    client.record_topic_result(
        run_id=run_id,
        topic_id="t1",
        source_id=src_id,
        title="A",
        url="https://example.com/x",
        score=8,
        status="passed",
        draft_id="drafts.post-zz",
    )
    client.record_run_finish(
        run_id,
        status="success",
        stats={"fetched": 4, "drafted": 1, "errors": 0},
        log_excerpt="source X: ok",
    )

    with factory() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.status == "success"
        assert row.finished_at is not None
        assert json.loads(row.stats) == {"fetched": 4, "drafted": 1, "errors": 0}
        topics = list(session.scalars(select(Topic).where(Topic.run_id == run_id)))
        assert len(topics) == 1
        assert topics[0].draft_id == "drafts.post-zz"


def test_record_run_noop_when_db_unavailable(no_admin_db) -> None:
    client = AdminConfigClient()
    assert client.record_run_start(source_ids=[1], triggered_by="cron") is None
    client.record_run_finish(None, status="success")  # type: ignore[arg-type]


def test_record_topic_result_noop_without_run_id(no_admin_db) -> None:
    AdminConfigClient().record_topic_result(
        run_id=None,
        topic_id="t",
        source_id=None,
        title="x",
        url=None,
        score=None,
        status="passed",
    )
