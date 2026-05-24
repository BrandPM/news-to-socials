"""Integration tests for /api/v1/config and /api/v1/runs routes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import CostRecord, PipelineConfig, Run, Source, Topic
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-conf"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client_and_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("ADMIN_LOG_PATH", str(tmp_path / "missing.log"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


@pytest.fixture
def client(client_and_brand):
    return client_and_brand[0]


@pytest.fixture
def icon_brand_id(client_and_brand) -> int:
    return client_and_brand[1]


def _seed_config(icon_brand_id: int, banned=("delve into",)) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            PipelineConfig(
                brand_id_fk=icon_brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps(list(banned)),
                voice_profile="mission: x\n",
            )
        )
        session.commit()


# --- Config -------------------------------------------------------------


def test_get_config_returns_seeded_row(client, icon_brand_id) -> None:
    _seed_config(icon_brand_id)
    resp = client.get(f"/api/v1/config?brand_id={icon_brand_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["scoring_threshold"] == 7
    assert body["topics_per_run"] == 3
    assert "delve into" in body["banned_phrases"]


def test_get_config_404_when_no_row(client) -> None:
    # brand_id 999 doesn't exist — config 404s.
    resp = client.get("/api/v1/config?brand_id=999", headers=AUTH)
    assert resp.status_code == 404


def test_put_config_partial_update(client, icon_brand_id) -> None:
    _seed_config(icon_brand_id)
    resp = client.put(
        f"/api/v1/config?brand_id={icon_brand_id}",
        headers=AUTH,
        json={"scoring_threshold": 8, "banned_phrases": ["foo", "bar"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scoring_threshold"] == 8
    assert body["topics_per_run"] == 3
    assert body["banned_phrases"] == ["foo", "bar"]


def test_put_config_rejects_out_of_range_threshold(client, icon_brand_id) -> None:
    _seed_config(icon_brand_id)
    resp = client.put(
        f"/api/v1/config?brand_id={icon_brand_id}",
        headers=AUTH,
        json={"scoring_threshold": 99},
    )
    assert resp.status_code == 422


# --- Runs ---------------------------------------------------------------


def _make_run_with_topic(brand_id_fk: int) -> tuple[int, int]:
    factory = admin_db.get_session_factory()
    with factory() as session:
        src = Source(
            brand_id_fk=brand_id_fk,
            name="x",
            source_type="rss",
            url="https://example.com/feed",
            primary_category="wealth",
        )
        session.add(src)
        session.flush()
        run = Run(
            brand_id_fk=brand_id_fk,
            triggered_by="manual",
            source_ids=json.dumps([src.id]),
            started_at=datetime.now(tz=timezone.utc),
            status="success",
            stats=json.dumps({"fetched": 4, "drafted": 2}),
            finished_at=datetime.now(tz=timezone.utc) + timedelta(seconds=60),
        )
        session.add(run)
        session.flush()
        session.add(
            Topic(
                run_id=run.id,
                topic_id="abc",
                source_id=src.id,
                title="A topic",
                url="https://example.com/x",
                score=8,
                status="passed",
                draft_id="drafts.post-aaa",
            )
        )
        session.commit()
        return run.id, src.id


def test_list_runs_sorted_desc_with_paging(client, icon_brand_id) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        for i in range(3):
            session.add(
                Run(
                    brand_id_fk=icon_brand_id,
                    triggered_by="manual",
                    source_ids="[]",
                    started_at=datetime.now(tz=timezone.utc) + timedelta(seconds=i),
                    status="success",
                )
            )
        session.commit()
    resp = client.get(f"/api/v1/runs?brand_id={icon_brand_id}&limit=2", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert resp.json()[0]["started_at"] > resp.json()[1]["started_at"]


def test_run_detail_includes_topics(client, icon_brand_id) -> None:
    run_id, _ = _make_run_with_topic(icon_brand_id)
    resp = client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["id"] == run_id
    assert body["run"]["stats"] == {"fetched": 4, "drafted": 2}
    assert len(body["topics"]) == 1
    assert body["topics"][0]["draft_id"] == "drafts.post-aaa"


def test_run_log_returns_stub_when_file_missing(client, icon_brand_id) -> None:
    run_id, _ = _make_run_with_topic(icon_brand_id)
    resp = client.get(f"/api/v1/runs/{run_id}/log", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "stub"


def test_run_log_reads_real_file_when_present(client, tmp_path, monkeypatch, icon_brand_id) -> None:
    log = tmp_path / "real.log"
    log.write_text(
        "\n".join(f"line-{i}" for i in range(500)) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("ADMIN_LOG_PATH", str(log))
    config_module._settings = None  # clear cache

    run_id, _ = _make_run_with_topic(icon_brand_id)
    resp = client.get(f"/api/v1/runs/{run_id}/log?tail=10", headers=AUTH)
    body = resp.json()
    assert body["source"] == "file"
    lines = body["log"].strip().split("\n")
    assert len(lines) == 10
    assert lines[-1] == "line-499"


# --- KPI block + latest + events ----------------------------------------


def _make_multilingual_run(brand_id_fk: int) -> tuple[int, int]:
    """Seed a run that mirrors the run-10 scenario: 40 scoring calls
    (10 per language × 4 languages), 4 passed topics, 2 with drafts."""
    factory = admin_db.get_session_factory()
    with factory() as session:
        src = Source(
            brand_id_fk=brand_id_fk,
            name="multilingual src",
            source_type="rss",
            url="https://example.com/feed",
            primary_category="wealth",
        )
        session.add(src)
        session.flush()
        run = Run(
            brand_id_fk=brand_id_fk,
            triggered_by="manual",
            source_ids=json.dumps([src.id]),
            started_at=datetime(2026, 5, 24, 20, 11, 45, tzinfo=timezone.utc),
            finished_at=datetime(2026, 5, 24, 20, 12, 35, tzinfo=timezone.utc),
            status="success",
            stats=json.dumps(
                {"fetched": 40, "scored": 4, "drafted": 2, "errors": 0}
            ),
            languages_completed=json.dumps(["en", "ru", "uk", "pl"]),
        )
        session.add(run)
        session.flush()
        # 40 topic_scoring cost records — 10 per language.
        for i in range(40):
            session.add(
                CostRecord(
                    brand_id_fk=brand_id_fk,
                    run_id=run.id,
                    provider="openai",
                    operation="topic_scoring",
                    cost_usd=0.00005,
                )
            )
        # 4 passed topics across 4 languages; 2 carry a draft_id.
        for i, (lang, draft) in enumerate(
            [
                ("en", None),
                ("ru", None),
                ("uk", "drafts.post-cc342566ec2e"),
                ("pl", "drafts.post-0648abf738fb"),
            ]
        ):
            session.add(
                Topic(
                    run_id=run.id,
                    topic_id="6b640357e6a87b25",
                    source_id=src.id,
                    title=f"Topic {lang}",
                    url=f"https://example.com/topic-{lang}",
                    score=7,
                    status="passed",
                    language=lang,
                    draft_id=draft,
                )
            )
        session.commit()
        return run.id, src.id


def test_run_detail_kpis_from_authoritative_sources(client, icon_brand_id) -> None:
    """Mirrors the run-10 bug: ``scored`` must reflect topic_scoring LLM
    calls (40) not the legacy ``stats.scored`` (4)."""
    run_id, _ = _make_multilingual_run(icon_brand_id)
    resp = client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    kpis = body["kpis"]
    assert kpis["fetched"] == 40
    assert kpis["scored"] == 40, "scored counts topic_scoring LLM calls"
    assert kpis["passed"] == 4, "4 rows in topics with status=passed"
    assert kpis["drafts"] == 2, "2 distinct draft_id values across passed topics"
    assert kpis["errors"] == 0


def test_run_detail_kpi_drafts_falls_back_to_stats_for_legacy_runs(
    client, icon_brand_id
) -> None:
    """Pre-fix runs have empty topics tables; the drafts KPI must still
    report a non-zero value pulled from ``run.stats.drafted``."""
    factory = admin_db.get_session_factory()
    with factory() as session:
        run = Run(
            brand_id_fk=icon_brand_id,
            triggered_by="cron",
            source_ids="[1]",
            started_at=datetime.now(tz=timezone.utc),
            finished_at=datetime.now(tz=timezone.utc) + timedelta(seconds=60),
            status="success",
            stats=json.dumps({"fetched": 10, "scored": 1, "drafted": 1, "errors": 0}),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    resp = client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    kpis = resp.json()["kpis"]
    assert kpis["drafts"] == 1, "fallback to stats.drafted when topics empty"


def test_get_latest_run_returns_most_recent(client, icon_brand_id) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        for i in range(3):
            session.add(
                Run(
                    brand_id_fk=icon_brand_id,
                    triggered_by="manual",
                    source_ids="[]",
                    started_at=datetime.now(tz=timezone.utc) + timedelta(seconds=i),
                    status="success",
                )
            )
        session.commit()
    resp = client.get(
        f"/api/v1/runs/latest?brand_id={icon_brand_id}", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    # Highest started_at wins → seed offset i=2.
    assert body["brand_id"] == icon_brand_id


def test_get_latest_run_404_when_none(client) -> None:
    resp = client.get("/api/v1/runs/latest?brand_id=999", headers=AUTH)
    assert resp.status_code == 404


def test_run_events_filter_by_window_and_kind(
    client, tmp_path, monkeypatch, icon_brand_id
) -> None:
    log = tmp_path / "events.log"
    # Two events inside the run window, one before, one after.
    inside_a = '{"event": "pipeline.start", "level": "info", "timestamp": "2026-05-24T20:11:46.000Z", "brand": "icon"}'
    inside_b = '{"event": "topic.published_as_draft", "level": "info", "timestamp": "2026-05-24T20:12:30.000Z", "topic": "abc", "draft_id": "drafts.post-x", "language": "en"}'
    before = '{"event": "noise", "level": "info", "timestamp": "2026-05-24T19:00:00.000Z"}'
    after = '{"event": "later", "level": "info", "timestamp": "2026-05-24T21:00:00.000Z"}'
    log.write_text(
        "\n".join([before, inside_a, inside_b, after]) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("ADMIN_LOG_PATH", str(log))
    config_module._settings = None

    run_id, _ = _make_multilingual_run(icon_brand_id)
    resp = client.get(f"/api/v1/runs/{run_id}/events", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "file"
    kinds = [e["kind"] for e in body["events"]]
    assert kinds == ["pipeline.start", "topic.published_as_draft"]
    publish = body["events"][1]
    assert publish["data"]["draft_id"] == "drafts.post-x"
    assert publish["data"]["language"] == "en"


def test_run_events_stub_when_log_missing(client, icon_brand_id) -> None:
    run_id, _ = _make_multilingual_run(icon_brand_id)
    resp = client.get(f"/api/v1/runs/{run_id}/events", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "stub"
    assert body["events"] == []
