"""Integration tests for /api/v1/sources routes (uses FastAPI TestClient)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import Run, Source, Topic
from pipeline.common import config as config_module

ADMIN_TOKEN = "test-token-123"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    # Recreate the app *after* env is set so create_app() sees the right
    # CORS origin and the routes pick up the new session factory.
    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    admin_db.reset_for_tests()


def _make_payload(**overrides):
    base = {
        "brand_id": "icon",
        "name": "Private Banker International",
        "source_type": "rss",
        "url": "https://www.privatebankerinternational.com/feed/",
        "primary_category": "wealth",
        "active": True,
        "paywall": False,
        "polling_minutes": 720,
    }
    base.update(overrides)
    return base


# --- CRUD ---------------------------------------------------------------


def test_list_empty(client) -> None:
    resp = client.get("/api/v1/sources", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_then_get_then_list(client) -> None:
    resp = client.post("/api/v1/sources", headers=AUTH, json=_make_payload())
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["id"] >= 1
    assert created["name"] == "Private Banker International"
    assert created["active"] is True

    src_id = created["id"]
    resp = client.get(f"/api/v1/sources/{src_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://www.privatebankerinternational.com/feed/"

    resp = client.get("/api/v1/sources", headers=AUTH)
    assert len(resp.json()) == 1


def test_list_filters_by_brand_id(client) -> None:
    client.post("/api/v1/sources", headers=AUTH, json=_make_payload(brand_id="icon"))
    client.post("/api/v1/sources", headers=AUTH, json=_make_payload(brand_id="other"))
    resp = client.get("/api/v1/sources?brand_id=icon", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["brand_id"] == "icon"


def test_update_partial(client) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload()
    ).json()
    resp = client.put(
        f"/api/v1/sources/{created['id']}",
        headers=AUTH,
        json={"active": False, "polling_minutes": 60},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False
    assert body["polling_minutes"] == 60
    # untouched fields preserved
    assert body["name"] == "Private Banker International"


def test_delete_clean(client) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload()
    ).json()
    resp = client.delete(f"/api/v1/sources/{created['id']}", headers=AUTH)
    assert resp.status_code == 204
    # Subsequent GET → 404
    resp = client.get(f"/api/v1/sources/{created['id']}", headers=AUTH)
    assert resp.status_code == 404


def test_delete_blocked_when_topics_reference_source(client, tmp_path) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload()
    ).json()
    # Attach a Run + Topic referencing this source.
    from datetime import datetime, timezone

    factory = admin_db.get_session_factory()
    with factory() as session:
        run = Run(
            brand_id="icon",
            triggered_by="manual",
            source_ids=f"[{created['id']}]",
            started_at=datetime.now(tz=timezone.utc),
            status="running",
        )
        session.add(run)
        session.flush()
        session.add(
            Topic(
                run_id=run.id,
                topic_id="abc",
                source_id=created["id"],
                title="x",
                status="passed",
            )
        )
        session.commit()
    resp = client.delete(f"/api/v1/sources/{created['id']}", headers=AUTH)
    assert resp.status_code == 409
    assert "topic" in resp.json()["detail"].lower()


def test_create_rejects_invalid_source_type(client) -> None:
    resp = client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(source_type="podcast"),
    )
    assert resp.status_code == 422  # pydantic-level rejection


def test_unauth_get_returns_401(client) -> None:
    resp = client.get("/api/v1/sources")
    assert resp.status_code == 401


# --- Test + Run ---------------------------------------------------------


def test_test_parse_returns_headlines(monkeypatch, client) -> None:
    """``POST /{id}/test`` must NOT write to DB and must call RssSource.fetch."""
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload()
    ).json()

    from pipeline.common.models import RawItem
    from pipeline.sources import rss as rss_mod

    fake_items = [
        RawItem(
            source_id="x",
            source_name="x",
            url="https://example.com/a",
            title="Headline A",
        ),
        RawItem(
            source_id="x",
            source_name="x",
            url="https://example.com/b",
            title="Headline B",
        ),
    ]

    async def fake_fetch(self):  # noqa: ANN001
        return fake_items

    monkeypatch.setattr(rss_mod.RssSource, "fetch", fake_fetch)
    resp = client.post(
        f"/api/v1/sources/{created['id']}/test?limit=5", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parser_status"] == "ok"
    assert len(body["headlines"]) == 2
    assert body["headlines"][0]["title"] == "Headline A"


def test_test_parse_reports_error_without_raising(monkeypatch, client) -> None:
    created = client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(url="https://invalid.example.com/feed"),
    ).json()

    from pipeline.sources import rss as rss_mod

    async def boom(self):  # noqa: ANN001
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(rss_mod.RssSource, "fetch", boom)
    resp = client.post(
        f"/api/v1/sources/{created['id']}/test", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parser_status"] == "error"
    assert "connect timeout" in body["error"]


def test_run_creates_run_row_returns_202(monkeypatch, client) -> None:
    """``POST /{id}/run`` schedules the background task and returns 202."""
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload()
    ).json()

    called: list[int] = []

    async def fake_execute(run_id: int) -> None:
        called.append(run_id)

    monkeypatch.setattr(admin_jobs, "execute_pipeline_run", fake_execute)

    resp = client.post(f"/api/v1/sources/{created['id']}/run", headers=AUTH)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert isinstance(run_id, int) and run_id >= 1
    # TestClient drains BackgroundTasks before returning — execute should
    # have been invoked.
    assert called == [run_id]

    # And there is a runs row.
    factory = admin_db.get_session_factory()
    with factory() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.status == "running"
        assert row.triggered_by == "manual"
        assert created["id"] in __import__("json").loads(row.source_ids)
