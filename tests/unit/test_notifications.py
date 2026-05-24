"""Tests for /api/v1/notifications (S5 Step 10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    DraftApproval,
    Run,
    Source,
    SourceHealthRecord,
)
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "tok-notif"
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
        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id, other_id
    admin_db.reset_for_tests()


def test_empty_state(env) -> None:
    client, icon_id, _ = env
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []


def test_failed_run_within_24h_appears(env) -> None:
    client, icon_id, _ = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        session.add(
            Run(
                brand_id_fk=icon_id,
                triggered_by="manual",
                source_ids="[]",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=1),
                status="failed",
                log_excerpt="ERROR: openai timed out\nfinal line of log",
            )
        )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    body = resp.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["kind"] == "run_failed"
    assert item["severity"] == "danger"
    assert item["href"].startswith("/runs/")
    assert "final line" in item["description"]


def test_failed_run_outside_24h_filtered(env) -> None:
    client, icon_id, _ = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        session.add(
            Run(
                brand_id_fk=icon_id,
                triggered_by="manual",
                source_ids="[]",
                started_at=now - timedelta(days=3),
                finished_at=now - timedelta(days=3),
                status="failed",
            )
        )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    assert resp.json()["count"] == 0


def test_unhealthy_source_below_50pct(env) -> None:
    client, icon_id, _ = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        source = Source(
            brand_id_fk=icon_id,
            name="Bad RSS",
            source_type="rss",
            url="https://example.com/bad.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        session.add(source)
        session.flush()
        # 5 attempts; 1 success → 20% success rate → unhealthy.
        for i in range(5):
            session.add(
                SourceHealthRecord(
                    source_id=source.id,
                    brand_id_fk=icon_id,
                    fetched_at=now - timedelta(hours=i),
                    success=(i == 0),
                    articles_count=0,
                    error_msg=None if i == 0 else "404",
                )
            )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    body = resp.json()
    kinds = [i["kind"] for i in body["items"]]
    assert "source_unhealthy" in kinds


def test_healthy_source_does_not_alert(env) -> None:
    client, icon_id, _ = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        source = Source(
            brand_id_fk=icon_id,
            name="Good RSS",
            source_type="rss",
            url="https://example.com/good.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        session.add(source)
        session.flush()
        # 10 attempts all green.
        for i in range(10):
            session.add(
                SourceHealthRecord(
                    source_id=source.id,
                    brand_id_fk=icon_id,
                    fetched_at=now - timedelta(hours=i),
                    success=True,
                    articles_count=5,
                )
            )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    assert resp.json()["count"] == 0


def test_under_three_attempts_does_not_alert(env) -> None:
    """A single failure with 2 attempts isn't enough signal — skip it."""
    client, icon_id, _ = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        source = Source(
            brand_id_fk=icon_id,
            name="Sparse RSS",
            source_type="rss",
            url="https://example.com/sparse.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        session.add(source)
        session.flush()
        for s in (False, False):
            session.add(
                SourceHealthRecord(
                    source_id=source.id,
                    brand_id_fk=icon_id,
                    fetched_at=now,
                    success=s,
                    articles_count=0,
                    error_msg="x",
                )
            )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    assert resp.json()["count"] == 0


def test_rejected_draft_appears(env) -> None:
    client, icon_id, _ = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        session.add(
            DraftApproval(
                sanity_draft_id="drafts.post-bad",
                brand_id_fk=icon_id,
                status="rejected",
                decided_at=now - timedelta(hours=4),
                decided_by="admin",
                note="off-brand voice",
            )
        )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={icon_id}", headers=AUTH)
    body = resp.json()
    item = next(i for i in body["items"] if i["kind"] == "draft_rejected")
    assert item["description"] == "off-brand voice"
    assert item["href"] == "/drafts/post-bad"


def test_brand_isolation(env) -> None:
    """Notifications must NOT leak between brands."""
    client, icon_id, other_id = env
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        session.add(
            Run(
                brand_id_fk=icon_id,
                triggered_by="manual",
                source_ids="[]",
                started_at=now - timedelta(hours=1),
                finished_at=now,
                status="failed",
            )
        )
        session.commit()
    resp = client.get(f"/api/v1/notifications?brand_id={other_id}", headers=AUTH)
    assert resp.json()["count"] == 0


def test_brand_not_found(env) -> None:
    client, _, _ = env
    resp = client.get("/api/v1/notifications?brand_id=9999", headers=AUTH)
    assert resp.status_code == 404
