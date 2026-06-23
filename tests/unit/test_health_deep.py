"""Tests for GET /api/v1/health/deep (IT_PROJ_NTS_073).

External calls are mocked: the Sanity probe is replaced so no network is
touched, and the DB/run reads exercise a real temp admin.db.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import Run
from pipeline.admin.routes import health as health_module
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-health"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand_id = seed_icon_brand(session)
        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app()), brand_id, monkeypatch
    admin_db.reset_for_tests()


def _add_run(brand_id: int, status: str, *, age_min: int = 1) -> None:
    factory = admin_db.get_session_factory()
    started = datetime.now(tz=timezone.utc) - timedelta(minutes=age_min)
    with factory() as session:
        session.add(
            Run(
                brand_id_fk=brand_id,
                triggered_by="manual",
                source_ids="[]",
                started_at=started,
                finished_at=started,
                status=status,
            )
        )
        session.commit()


def _patch_sanity(monkeypatch, value: str) -> None:
    async def fake_sanity() -> str:
        return value

    monkeypatch.setattr(health_module, "_check_sanity", fake_sanity)


def test_ok_when_db_and_recent_success(env) -> None:
    client, brand_id, monkeypatch = env
    _patch_sanity(monkeypatch, "ok")
    _add_run(brand_id, "success", age_min=5)

    resp = client.get("/api/v1/health/deep", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"]["db"] == "ok"
    assert body["components"]["sanity"] == "ok"
    assert body["components"]["last_successful_run_age_min"] is not None
    assert body["components"]["last_successful_run_age_min"] <= 6
    assert body["components"]["last_run_status"] == "success"


def test_degraded_when_no_successful_run(env) -> None:
    client, _brand_id, monkeypatch = env
    _patch_sanity(monkeypatch, "ok")
    # No success run at all → last_successful is None → degraded.
    resp = client.get("/api/v1/health/deep", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["last_successful_run_age_min"] is None


def test_degraded_when_sanity_errors(env) -> None:
    client, brand_id, monkeypatch = env
    _patch_sanity(monkeypatch, "error: ConnectTimeout")
    _add_run(brand_id, "success", age_min=5)

    resp = client.get("/api/v1/health/deep", headers=AUTH)
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["sanity"].startswith("error")


def test_sanity_unconfigured_is_not_degrading(env) -> None:
    client, brand_id, monkeypatch = env
    _patch_sanity(monkeypatch, "unconfigured")
    _add_run(brand_id, "success", age_min=5)

    resp = client.get("/api/v1/health/deep", headers=AUTH)
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"]["sanity"] == "unconfigured"


def test_requires_auth(env) -> None:
    client, _brand_id, _mp = env
    resp = client.get("/api/v1/health/deep")
    assert resp.status_code == 401
