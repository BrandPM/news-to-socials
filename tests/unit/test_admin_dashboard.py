"""Tests for S4 dashboard aggregation endpoints.

Covers:
- ``GET /api/v1/cost/trend`` — daily series grouped by operation.
- ``GET /api/v1/dashboard/summary`` — bundled KPI payload.
- ``GET /api/v1/runs?status=...`` — status filter added in S4 so the
  active-runs panel can poll ``status=running`` cheaply.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Brand, CostRecord, Run
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-s4"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client_and_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
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
        icon_id = seed_icon_brand(session)
        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def _add_cost(
    brand_id: int,
    *,
    operation: str,
    provider: str = "openai",
    cost: float = 0.01,
    days_ago: float = 0,
    run_id: int | None = None,
) -> None:
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        session.add(
            CostRecord(
                brand_id_fk=brand_id,
                run_id=run_id,
                provider=provider,
                operation=operation,
                cost_usd=cost,
                created_at=now - timedelta(days=days_ago),
            )
        )
        session.commit()


def _add_run(
    brand_id: int,
    *,
    status: str = "success",
    drafted: int = 0,
    days_ago: float = 0,
    finished: bool = True,
) -> int:
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    started = now - timedelta(days=days_ago)
    with factory() as session:
        run = Run(
            brand_id_fk=brand_id,
            triggered_by="test",
            source_ids="[]",
            started_at=started,
            finished_at=started + timedelta(seconds=5) if finished else None,
            status=status,
            stats=json.dumps({"drafted": drafted}),
        )
        session.add(run)
        session.flush()
        rid = run.id
        session.commit()
    return rid


# --- /cost/trend ---------------------------------------------------------


def test_trend_returns_full_day_window_with_zero_fill(client_and_brand) -> None:
    """Days with no cost must still appear with empty by_operation + total=0."""
    client, bid = client_and_brand
    _add_cost(bid, operation="draft", cost=0.05, days_ago=0)
    resp = client.get(
        f"/api/v1/cost/trend?brand_id={bid}&days=7", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 7
    # Days sorted ascending.
    dates = [row["date"] for row in body]
    assert dates == sorted(dates)
    # Last day has the cost; earlier days are empty.
    assert body[-1]["total"] == pytest.approx(0.05)
    assert body[-1]["by_operation"]["draft"] == pytest.approx(0.05)
    for row in body[:-1]:
        assert row["total"] == 0.0
        assert row["by_operation"] == {}


def test_trend_groups_by_operation_per_day(client_and_brand) -> None:
    client, bid = client_and_brand
    _add_cost(bid, operation="draft", cost=0.10, days_ago=0)
    _add_cost(bid, operation="polish", cost=0.05, days_ago=0)
    _add_cost(bid, operation="draft", cost=0.20, days_ago=0)
    _add_cost(bid, operation="image_master", provider="replicate", cost=0.04, days_ago=1)
    resp = client.get(
        f"/api/v1/cost/trend?brand_id={bid}&days=3", headers=AUTH
    )
    body = resp.json()
    today = body[-1]
    yesterday = body[-2]
    assert today["by_operation"]["draft"] == pytest.approx(0.30)
    assert today["by_operation"]["polish"] == pytest.approx(0.05)
    assert today["total"] == pytest.approx(0.35)
    assert yesterday["by_operation"]["image_master"] == pytest.approx(0.04)


def test_trend_brand_scoped(client_and_brand) -> None:
    """Costs from another brand must NOT leak into the trend (M1)."""
    client, bid = client_and_brand
    factory = admin_db.get_session_factory()
    with factory() as session:
        other = Brand(
            slug="other",
            name="Other",
            language="en",
            timezone="Europe/Madrid",
            status="active",
            active=True,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        session.add(other)
        session.flush()
        other_id = other.id
        session.commit()
    _add_cost(other_id, operation="draft", cost=99.0, days_ago=0)
    _add_cost(bid, operation="draft", cost=0.01, days_ago=0)
    body = client.get(
        f"/api/v1/cost/trend?brand_id={bid}&days=3", headers=AUTH
    ).json()
    totals = [row["total"] for row in body]
    assert sum(totals) == pytest.approx(0.01)


def test_trend_days_param_bounds(client_and_brand) -> None:
    client, bid = client_and_brand
    too_large = client.get(
        f"/api/v1/cost/trend?brand_id={bid}&days=999", headers=AUTH
    )
    assert too_large.status_code == 422
    too_small = client.get(
        f"/api/v1/cost/trend?brand_id={bid}&days=0", headers=AUTH
    )
    assert too_small.status_code == 422


# --- /dashboard/summary --------------------------------------------------


def test_summary_empty_brand_returns_zeros(client_and_brand) -> None:
    client, bid = client_and_brand
    resp = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cost_today_usd"] == 0.0
    assert body["cost_yesterday_usd"] == 0.0
    assert body["cost_month_usd"] == 0.0
    assert body["drafts_today"] == 0
    assert body["drafts_this_week"] == 0
    assert body["active_runs_count"] == 0
    assert body["last_run_finished_at"] is None


def test_summary_today_vs_yesterday_trend(client_and_brand) -> None:
    client, bid = client_and_brand
    _add_cost(bid, operation="draft", cost=0.50, days_ago=1)  # yesterday
    _add_cost(bid, operation="draft", cost=1.00, days_ago=0)  # today
    body = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    ).json()
    assert body["cost_today_usd"] == pytest.approx(1.00)
    assert body["cost_yesterday_usd"] == pytest.approx(0.50)
    # +100% increase today vs yesterday.
    assert body["cost_today_trend_pct"] == pytest.approx(100.0)


def test_summary_trend_null_when_zero_yesterday_but_today_has_cost(
    client_and_brand,
) -> None:
    client, bid = client_and_brand
    _add_cost(bid, operation="draft", cost=0.05, days_ago=0)
    body = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    ).json()
    assert body["cost_yesterday_usd"] == 0.0
    assert body["cost_today_trend_pct"] is None


def test_summary_drafts_today_and_week(client_and_brand) -> None:
    client, bid = client_and_brand
    _add_run(bid, status="success", drafted=2, days_ago=0)
    _add_run(bid, status="success", drafted=1, days_ago=3)
    _add_run(bid, status="success", drafted=5, days_ago=10)  # outside week
    body = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    ).json()
    assert body["drafts_today"] == 2
    assert body["drafts_this_week"] == 3


def test_summary_active_runs_count_only_running(client_and_brand) -> None:
    client, bid = client_and_brand
    _add_run(bid, status="running", finished=False, days_ago=0)
    _add_run(bid, status="running", finished=False, days_ago=0)
    _add_run(bid, status="success", days_ago=0)
    body = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    ).json()
    assert body["active_runs_count"] == 2


def test_summary_last_run_picks_most_recent_finished(client_and_brand) -> None:
    client, bid = client_and_brand
    _add_run(bid, status="failed", days_ago=0.5)
    _add_run(bid, status="success", days_ago=0.1)
    body = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    ).json()
    assert body["last_run_status"] == "success"
    assert body["last_run_finished_at"] is not None


def test_summary_brand_scoped(client_and_brand) -> None:
    """drafts_today / cost_today must not leak across brands (M1)."""
    client, bid = client_and_brand
    factory = admin_db.get_session_factory()
    with factory() as session:
        other = Brand(
            slug="other",
            name="Other",
            language="en",
            timezone="Europe/Madrid",
            status="active",
            active=True,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        session.add(other)
        session.flush()
        other_id = other.id
        session.commit()
    _add_cost(other_id, operation="draft", cost=99.0, days_ago=0)
    _add_run(other_id, status="success", drafted=10, days_ago=0)
    body = client.get(
        f"/api/v1/dashboard/summary?brand_id={bid}", headers=AUTH
    ).json()
    assert body["cost_today_usd"] == 0.0
    assert body["drafts_today"] == 0


# --- /runs?status= filter ------------------------------------------------


def test_runs_status_filter_returns_only_matching(client_and_brand) -> None:
    client, bid = client_and_brand
    _add_run(bid, status="success", days_ago=0)
    _add_run(bid, status="running", finished=False, days_ago=0)
    _add_run(bid, status="failed", days_ago=0)
    resp = client.get(
        f"/api/v1/runs?brand_id={bid}&status=running", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "running"


def test_runs_status_filter_rejects_unknown_value(client_and_brand) -> None:
    client, bid = client_and_brand
    resp = client.get(
        f"/api/v1/runs?brand_id={bid}&status=garbage", headers=AUTH
    )
    assert resp.status_code == 422
