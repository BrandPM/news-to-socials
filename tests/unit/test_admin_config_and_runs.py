"""Integration tests for /api/v1/config and /api/v1/runs routes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import PipelineConfig, Run, Source, Topic
from pipeline.common import config as config_module

ADMIN_TOKEN = "tok-conf"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("ADMIN_LOG_PATH", str(tmp_path / "missing.log"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    admin_db.reset_for_tests()


def _seed_config(banned=("delve into",)) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            PipelineConfig(
                brand_id="icon",
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps(list(banned)),
                voice_profile="mission: x\n",
            )
        )
        session.commit()


# --- Config -------------------------------------------------------------


def test_get_config_returns_seeded_row(client) -> None:
    _seed_config()
    resp = client.get("/api/v1/config?brand_id=icon", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["scoring_threshold"] == 7
    assert body["topics_per_run"] == 3
    assert "delve into" in body["banned_phrases"]


def test_get_config_404_when_no_row(client) -> None:
    resp = client.get("/api/v1/config?brand_id=missing", headers=AUTH)
    assert resp.status_code == 404


def test_put_config_partial_update(client) -> None:
    _seed_config()
    resp = client.put(
        "/api/v1/config?brand_id=icon",
        headers=AUTH,
        json={"scoring_threshold": 8, "banned_phrases": ["foo", "bar"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scoring_threshold"] == 8
    assert body["topics_per_run"] == 3
    assert body["banned_phrases"] == ["foo", "bar"]


def test_put_config_rejects_out_of_range_threshold(client) -> None:
    _seed_config()
    resp = client.put(
        "/api/v1/config?brand_id=icon",
        headers=AUTH,
        json={"scoring_threshold": 99},
    )
    assert resp.status_code == 422


# --- Runs ---------------------------------------------------------------


def _make_run_with_topic(brand_id="icon") -> tuple[int, int]:
    factory = admin_db.get_session_factory()
    with factory() as session:
        src = Source(
            brand_id=brand_id,
            name="x",
            source_type="rss",
            url="https://example.com/feed",
            primary_category="wealth",
        )
        session.add(src)
        session.flush()
        run = Run(
            brand_id=brand_id,
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


def test_list_runs_sorted_desc_with_paging(client) -> None:
    # Create several runs and verify ordering.
    factory = admin_db.get_session_factory()
    with factory() as session:
        for i in range(3):
            session.add(
                Run(
                    brand_id="icon",
                    triggered_by="manual",
                    source_ids="[]",
                    started_at=datetime.now(tz=timezone.utc) + timedelta(seconds=i),
                    status="success",
                )
            )
        session.commit()
    resp = client.get("/api/v1/runs?brand_id=icon&limit=2", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    # Most recent first.
    assert resp.json()[0]["started_at"] > resp.json()[1]["started_at"]


def test_run_detail_includes_topics(client) -> None:
    run_id, _ = _make_run_with_topic()
    resp = client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["id"] == run_id
    assert body["run"]["stats"] == {"fetched": 4, "drafted": 2}
    assert len(body["topics"]) == 1
    assert body["topics"][0]["draft_id"] == "drafts.post-aaa"


def test_run_log_returns_stub_when_file_missing(client) -> None:
    run_id, _ = _make_run_with_topic()
    resp = client.get(f"/api/v1/runs/{run_id}/log", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "stub"


def test_run_log_reads_real_file_when_present(client, tmp_path, monkeypatch) -> None:
    log = tmp_path / "real.log"
    log.write_text(
        "\n".join(f"line-{i}" for i in range(500)) + "\n", encoding="utf-8"
    )
    # Re-stub get_settings to point at our log file.
    monkeypatch.setenv("ADMIN_LOG_PATH", str(log))
    config_module._settings = None  # clear cache

    run_id, _ = _make_run_with_topic()
    resp = client.get(f"/api/v1/runs/{run_id}/log?tail=10", headers=AUTH)
    body = resp.json()
    assert body["source"] == "file"
    lines = body["log"].strip().split("\n")
    assert len(lines) == 10
    assert lines[-1] == "line-499"
