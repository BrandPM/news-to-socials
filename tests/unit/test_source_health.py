"""Tests for source_health_records + GET /sources/{id}/health (S5 Step 6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.config_client import AdminConfigClient
from pipeline.admin.models import Source, SourceHealthRecord
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "test-token-123"
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
        icon_id = seed_icon_brand(session)
        other_id = seed_brand(session, slug="other", name="Other").id
        icon_source = Source(
            brand_id_fk=icon_id,
            name="Test RSS",
            source_type="rss",
            url="https://example.com/feed.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        other_source = Source(
            brand_id_fk=other_id,
            name="Other RSS",
            source_type="rss",
            url="https://other.example.com/feed.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        session.add_all([icon_source, other_source])
        session.flush()
        icon_source_id = icon_source.id
        other_source_id = other_source.id
        session.commit()

    from pipeline.admin.server import create_app

    client = TestClient(create_app())
    yield {
        "client": client,
        "icon_id": icon_id,
        "other_id": other_id,
        "icon_source_id": icon_source_id,
        "other_source_id": other_source_id,
        "tmp_path": tmp_path,
    }
    admin_db.reset_for_tests()


def _insert_health(
    source_id: int,
    brand_id_fk: int,
    *,
    days_ago: int,
    success: bool = True,
    articles: int = 5,
    error: str | None = None,
) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            SourceHealthRecord(
                source_id=source_id,
                brand_id_fk=brand_id_fk,
                fetched_at=datetime.now(tz=timezone.utc) - timedelta(days=days_ago),
                success=success,
                articles_count=articles,
                error_msg=error,
            )
        )
        session.commit()


def test_health_empty_returns_zero_series(env):
    """No history → all-zero series of length `days` and 0% success."""
    r = env["client"].get(
        f"/api/v1/sources/{env['icon_source_id']}/health"
        f"?brand_id={env['icon_id']}&days=7",
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_id"] == env["icon_source_id"]
    assert body["days"] == 7
    assert body["success_rate_pct"] == 0.0
    assert body["last_fetch_at"] is None
    assert body["last_error"] is None
    assert len(body["series"]) == 7
    for day in body["series"]:
        assert day["fetches"] == 0
        assert day["success_count"] == 0
        assert day["failure_count"] == 0
        assert day["articles_total"] == 0


def test_health_records_success_and_failure(env):
    """Mixed history → success_rate_pct reflects ratio, last_error from last fail."""
    sid = env["icon_source_id"]
    bid = env["icon_id"]
    _insert_health(sid, bid, days_ago=0, success=True, articles=12)
    _insert_health(sid, bid, days_ago=1, success=True, articles=8)
    _insert_health(sid, bid, days_ago=2, success=False, articles=0, error="HTTPError: 503")
    _insert_health(sid, bid, days_ago=3, success=True, articles=3)

    r = env["client"].get(
        f"/api/v1/sources/{sid}/health?brand_id={bid}&days=7",
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success_rate_pct"] == 75.0  # 3 of 4
    assert body["last_error"] == "HTTPError: 503"
    assert body["last_fetch_at"] is not None
    total_articles = sum(day["articles_total"] for day in body["series"])
    assert total_articles == 23  # 12 + 8 + 3 (failures contribute 0)


def test_health_brand_isolation_returns_404(env):
    """Asking for icon source under other brand → 404, not silent empty."""
    r = env["client"].get(
        f"/api/v1/sources/{env['icon_source_id']}/health"
        f"?brand_id={env['other_id']}&days=7",
        headers=AUTH,
    )
    assert r.status_code == 404


def test_health_404_for_unknown_source(env):
    r = env["client"].get(
        f"/api/v1/sources/9999/health?brand_id={env['icon_id']}&days=7",
        headers=AUTH,
    )
    assert r.status_code == 404


def test_health_days_clamped_to_range(env):
    """days < 1 → 1, days > 90 → 90."""
    sid = env["icon_source_id"]
    bid = env["icon_id"]
    r = env["client"].get(
        f"/api/v1/sources/{sid}/health?brand_id={bid}&days=500",
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["days"] == 90
    assert len(r.json()["series"]) == 90

    r = env["client"].get(
        f"/api/v1/sources/{sid}/health?brand_id={bid}&days=0",
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["days"] == 1


def test_record_source_health_writes_row(env, monkeypatch):
    """The recorder helper writes a row and truncates long error msgs."""
    monkeypatch.setenv("ADMIN_DB_PATH", str(env["tmp_path"] / "admin.db"))
    config_module._settings = None
    AdminConfigClient.record_source_health(
        source_id=env["icon_source_id"],
        brand_id_fk=env["icon_id"],
        success=False,
        articles_count=0,
        error_msg="x" * 1000,
    )
    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(SourceHealthRecord).all()
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].articles_count == 0
        assert rows[0].error_msg is not None
        assert len(rows[0].error_msg) == 500  # truncated
