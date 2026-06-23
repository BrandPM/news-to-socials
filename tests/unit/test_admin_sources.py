"""Integration tests for /api/v1/sources routes (uses FastAPI TestClient)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import Run, Source, Topic
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "test-token-123"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client_and_brands(tmp_path, monkeypatch):
    """TestClient + (icon_brand_id, other_brand_id) tuple."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        other_id = seed_brand(session, slug="other", name="Other").id
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id, other_id
    admin_db.reset_for_tests()


@pytest.fixture
def client(client_and_brands):
    """Backwards-compatible single-brand client fixture."""
    return client_and_brands[0]


@pytest.fixture
def icon_brand_id(client_and_brands) -> int:
    return client_and_brands[1]


def _make_payload(icon_brand_id: int, **overrides):
    base = {
        "brand_id": icon_brand_id,
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


def test_create_then_get_then_list(client, icon_brand_id) -> None:
    resp = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_brand_id)
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["id"] >= 1
    assert created["name"] == "Private Banker International"
    assert created["active"] is True
    assert created["brand_id"] == icon_brand_id

    src_id = created["id"]
    resp = client.get(f"/api/v1/sources/{src_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://www.privatebankerinternational.com/feed/"

    resp = client.get("/api/v1/sources", headers=AUTH)
    assert len(resp.json()) == 1


def test_list_filters_by_brand_id(client_and_brands) -> None:
    client, icon_id, other_id = client_and_brands
    client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_id)
    )
    client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(other_id, url="https://other.example.com/feed"),
    )
    resp = client.get(f"/api/v1/sources?brand_id={icon_id}", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["brand_id"] == icon_id


def test_update_partial(client, icon_brand_id) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_brand_id)
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
    assert body["name"] == "Private Banker International"


def test_delete_clean(client, icon_brand_id) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_brand_id)
    ).json()
    resp = client.delete(f"/api/v1/sources/{created['id']}", headers=AUTH)
    assert resp.status_code == 204
    resp = client.get(f"/api/v1/sources/{created['id']}", headers=AUTH)
    assert resp.status_code == 404


def test_delete_blocked_when_topics_reference_source(client, icon_brand_id) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_brand_id)
    ).json()
    from datetime import datetime, timezone

    factory = admin_db.get_session_factory()
    with factory() as session:
        run = Run(
            brand_id_fk=icon_brand_id,
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


def test_create_rejects_invalid_source_type(client, icon_brand_id) -> None:
    resp = client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(icon_brand_id, source_type="podcast"),
    )
    assert resp.status_code == 422


def test_unauth_get_returns_401(client) -> None:
    resp = client.get("/api/v1/sources")
    assert resp.status_code == 401


def test_create_rejects_unknown_brand_id(client) -> None:
    resp = client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(999999),  # no such brand
    )
    assert resp.status_code == 422
    assert "brand" in resp.json()["detail"].lower()


# --- Test + Run ---------------------------------------------------------


def test_test_parse_returns_headlines(monkeypatch, client, icon_brand_id) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_brand_id)
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


def test_test_parse_reports_error_without_raising(monkeypatch, client, icon_brand_id) -> None:
    created = client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(icon_brand_id, url="https://invalid.example.com/feed"),
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


def test_run_all_sources_returns_202_with_run_id(monkeypatch, client, icon_brand_id) -> None:
    """POST /sources/run-all kicks off a pipeline run for every active
    source of the given brand."""
    # Seed 2 active + 1 inactive source.
    client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(icon_brand_id, active=True),
    )
    client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(
            icon_brand_id,
            url="https://other.example.com/feed",
            name="Other",
            active=True,
        ),
    )
    client.post(
        "/api/v1/sources",
        headers=AUTH,
        json=_make_payload(
            icon_brand_id,
            url="https://inactive.example.com/feed",
            name="Inactive",
            active=False,
        ),
    )

    called: list[int] = []

    def fake_spawn(run_id: int) -> int | None:
        called.append(run_id)
        return 4242

    monkeypatch.setattr(admin_jobs, "spawn_pipeline_run", fake_spawn)

    # Brand must be active for run-all (M4). Use the brand-update endpoint.
    # Activate by directly setting the active=true via PUT /brands/{id}
    # would need sanity creds — for this test we'll go straight to DB.
    factory = admin_db.get_session_factory()
    from pipeline.admin.models import Brand

    with factory() as session:
        b = session.get(Brand, icon_brand_id)
        b.active = True
        b.status = "active"
        session.commit()

    resp = client.post(
        "/api/v1/sources/run-all",
        headers=AUTH,
        json={"brand_id": icon_brand_id},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]

    factory = admin_db.get_session_factory()
    with factory() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.brand_id_fk == icon_brand_id
        # Only 2 active sources should be in the run, not the inactive one.
        import json as _json

        assert len(_json.loads(row.source_ids)) == 2


def test_run_all_409_when_brand_not_active(client, icon_brand_id) -> None:
    """Brand left at status='active'=True but creds absent → still
    reachable here because the active flag was set by the conftest
    fixture. We test the M4 'must be active' guard by setting status=paused."""
    factory = admin_db.get_session_factory()
    from pipeline.admin.models import Brand

    with factory() as session:
        b = session.get(Brand, icon_brand_id)
        b.status = "paused"
        b.active = False
        session.commit()
    resp = client.post(
        "/api/v1/sources/run-all",
        headers=AUTH,
        json={"brand_id": icon_brand_id},
    )
    assert resp.status_code == 409


def test_run_all_409_when_brand_has_no_active_sources(client, icon_brand_id) -> None:
    factory = admin_db.get_session_factory()
    from pipeline.admin.models import Brand

    with factory() as session:
        b = session.get(Brand, icon_brand_id)
        b.active = True
        b.status = "active"
        session.commit()
    resp = client.post(
        "/api/v1/sources/run-all",
        headers=AUTH,
        json={"brand_id": icon_brand_id},
    )
    assert resp.status_code == 409


def test_run_creates_run_row_returns_202(monkeypatch, client, icon_brand_id) -> None:
    created = client.post(
        "/api/v1/sources", headers=AUTH, json=_make_payload(icon_brand_id)
    ).json()

    called: list[int] = []

    def fake_spawn(run_id: int) -> int | None:
        called.append(run_id)
        return 4242

    monkeypatch.setattr(admin_jobs, "spawn_pipeline_run", fake_spawn)

    resp = client.post(f"/api/v1/sources/{created['id']}/run", headers=AUTH)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert isinstance(run_id, int) and run_id >= 1
    assert called == [run_id]

    factory = admin_db.get_session_factory()
    with factory() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.status == "running"
        assert row.triggered_by == "manual"
        assert created["id"] in __import__("json").loads(row.source_ids)
