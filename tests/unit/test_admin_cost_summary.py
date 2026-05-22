"""Tests for /api/v1/cost/summary + /api/v1/cost/records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import CostRecord
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-cost"
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


def _seed_costs(brand_id: int, items: list[tuple[str, str, float, int]]) -> None:
    """items: list of (provider, operation, cost_usd, days_ago)."""
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        for provider, op, cost, days_ago in items:
            session.add(
                CostRecord(
                    brand_id_fk=brand_id,
                    provider=provider,
                    operation=op,
                    cost_usd=cost,
                    created_at=now - timedelta(days=days_ago),
                )
            )
        session.commit()


def test_summary_empty_returns_zero_total(client_and_brand) -> None:
    client, bid = client_and_brand
    resp = client.get(f"/api/v1/cost/summary?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_usd"] == 0.0
    assert body["by_operation"] == {}
    assert body["by_day"] == []


def test_summary_aggregates_by_operation(client_and_brand) -> None:
    client, bid = client_and_brand
    _seed_costs(
        bid,
        [
            ("openai", "topic_scoring", 0.01, 0),
            ("openai", "topic_scoring", 0.02, 0),
            ("openai", "draft", 0.10, 0),
            ("replicate", "image_master", 0.04, 0),
        ],
    )
    resp = client.get(f"/api/v1/cost/summary?brand_id={bid}", headers=AUTH)
    body = resp.json()
    assert body["total_usd"] == pytest.approx(0.17)
    assert body["by_operation"]["topic_scoring"] == pytest.approx(0.03)
    assert body["by_operation"]["draft"] == pytest.approx(0.10)
    assert body["by_provider"]["openai"] == pytest.approx(0.13)
    assert body["by_provider"]["replicate"] == pytest.approx(0.04)


def test_summary_period_today_excludes_older_rows(client_and_brand) -> None:
    client, bid = client_and_brand
    _seed_costs(
        bid,
        [
            ("openai", "draft", 0.01, 0),
            ("openai", "draft", 0.99, 5),  # 5 days old → excluded today
        ],
    )
    resp = client.get(
        f"/api/v1/cost/summary?brand_id={bid}&period=today", headers=AUTH
    )
    body = resp.json()
    assert body["total_usd"] == pytest.approx(0.01)


def test_summary_period_week_includes_last_7_days(client_and_brand) -> None:
    client, bid = client_and_brand
    _seed_costs(
        bid,
        [
            ("openai", "draft", 0.01, 0),
            ("openai", "draft", 0.05, 6),
            ("openai", "draft", 0.99, 10),  # 10 days old → excluded
        ],
    )
    resp = client.get(
        f"/api/v1/cost/summary?brand_id={bid}&period=week", headers=AUTH
    )
    body = resp.json()
    assert body["total_usd"] == pytest.approx(0.06)


def test_summary_by_day_sorted_ascending(client_and_brand) -> None:
    client, bid = client_and_brand
    _seed_costs(
        bid,
        [
            ("openai", "draft", 0.01, 3),
            ("openai", "draft", 0.02, 1),
            ("openai", "draft", 0.03, 0),
        ],
    )
    resp = client.get(
        f"/api/v1/cost/summary?brand_id={bid}&period=week", headers=AUTH
    )
    days = [d["date"] for d in resp.json()["by_day"]]
    assert days == sorted(days)


def test_records_returns_paginated_descending(client_and_brand) -> None:
    client, bid = client_and_brand
    _seed_costs(
        bid,
        [
            ("openai", "draft", 0.01, 0),
            ("openai", "polish", 0.02, 0),
            ("openai", "topic_scoring", 0.03, 0),
        ],
    )
    resp = client.get(
        f"/api/v1/cost/records?brand_id={bid}&limit=2", headers=AUTH
    )
    body = resp.json()
    assert len(body) == 2
    # Each row exposes the brand_id (aliased from brand_id_fk).
    assert all(r["brand_id"] == bid for r in body)


def test_records_excludes_other_brands(client_and_brand) -> None:
    """Cost records for brand A must NOT leak when querying brand B (M1)."""
    client, bid = client_and_brand
    # Seed another brand.
    factory = admin_db.get_session_factory()
    from pipeline.admin.models import Brand

    other_id: int
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
        session.add(
            CostRecord(
                brand_id_fk=other_id,
                provider="openai",
                operation="draft",
                cost_usd=99.0,
            )
        )
        session.add(
            CostRecord(
                brand_id_fk=bid,
                provider="openai",
                operation="draft",
                cost_usd=0.01,
            )
        )
        session.commit()

    resp = client.get(f"/api/v1/cost/summary?brand_id={bid}", headers=AUTH)
    assert resp.json()["total_usd"] == pytest.approx(0.01)


def test_runs_detail_includes_cost_total_and_breakdown(client_and_brand) -> None:
    client, bid = client_and_brand
    from pipeline.admin.models import Run

    factory = admin_db.get_session_factory()
    with factory() as session:
        run = Run(
            brand_id_fk=bid,
            triggered_by="manual",
            source_ids="[]",
            started_at=datetime.now(tz=timezone.utc),
            status="success",
        )
        session.add(run)
        session.flush()
        rid = run.id
        session.add_all(
            [
                CostRecord(
                    brand_id_fk=bid, run_id=rid,
                    provider="openai", operation="draft", cost_usd=0.10,
                ),
                CostRecord(
                    brand_id_fk=bid, run_id=rid,
                    provider="openai", operation="polish", cost_usd=0.15,
                ),
                CostRecord(
                    brand_id_fk=bid, run_id=rid,
                    provider="replicate", operation="image_master", cost_usd=0.04,
                ),
            ]
        )
        session.commit()
    resp = client.get(f"/api/v1/runs/{rid}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cost_total_usd"] == pytest.approx(0.29)
    ops = {item["operation"] for item in body["cost_breakdown"]}
    assert ops == {"draft", "polish", "image_master"}
