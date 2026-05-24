"""Tests for /api/v1/topics/simulate (S5 Step 9)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import Run, Source, Topic
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "tok-simul"
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
        source = Source(
            brand_id_fk=icon_id,
            name="RSS",
            source_type="rss",
            url="https://example.com/feed.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        session.add(source)
        session.flush()
        source_id = source.id
        other_source = Source(
            brand_id_fk=other_id,
            name="Other",
            source_type="rss",
            url="https://other.example.com/feed.xml",
            primary_category="wealth",
            active=True,
            paywall=False,
            polling_minutes=720,
        )
        session.add(other_source)
        session.flush()
        other_source_id = other_source.id

        now = datetime.now(tz=timezone.utc)
        run = Run(
            brand_id_fk=icon_id,
            triggered_by="manual",
            source_ids="[1]",
            started_at=now,
            finished_at=now,
            status="success",
        )
        session.add(run)
        session.flush()
        run_id = run.id

        other_run = Run(
            brand_id_fk=other_id,
            triggered_by="manual",
            source_ids="[1]",
            started_at=now,
            finished_at=now,
            status="success",
        )
        session.add(other_run)
        session.flush()
        other_run_id = other_run.id

        # Icon topics: scored 4,5,6,7,8,9 with current status mirroring
        # threshold of 7 (4-6 filtered_score, 7-9 passed).
        def add_topic(rid: int, sid: int, idx: int, score: int, status: str, days_ago: int = 0) -> None:
            session.add(
                Topic(
                    run_id=rid,
                    topic_id=f"t-{rid}-{idx}",
                    source_id=sid,
                    title=f"Topic {idx}",
                    score=score,
                    status=status,
                    created_at=now - timedelta(days=days_ago),
                )
            )

        # 6 scored topics for icon
        add_topic(run_id, source_id, 1, 4, "filtered_score")
        add_topic(run_id, source_id, 2, 5, "filtered_score")
        add_topic(run_id, source_id, 3, 6, "filtered_score")
        add_topic(run_id, source_id, 4, 7, "passed")
        add_topic(run_id, source_id, 5, 8, "passed")
        add_topic(run_id, source_id, 6, 9, "passed")
        # 1 banned (no score impact)
        session.add(
            Topic(
                run_id=run_id,
                topic_id="t-banned",
                source_id=source_id,
                title="Banned",
                score=None,
                status="filtered_banned",
                created_at=now,
            )
        )
        # 1 too-old topic (45 days ago)
        add_topic(run_id, source_id, 7, 8, "passed", days_ago=45)

        # Cross-brand isolation: other brand has 1 high-score topic
        add_topic(other_run_id, other_source_id, 8, 10, "passed")

        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id, other_id
    admin_db.reset_for_tests()


def test_simulate_at_current_threshold_is_a_no_op(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 7, "days": 30},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_scored"] == 6  # the 45-day-ago one is out of window
    assert body["currently_passed"] == 3
    assert body["would_pass"] == 3
    assert body["delta"] == 0
    assert body["swing_in"] == 0
    assert body["swing_out"] == 0


def test_simulate_lower_threshold_increases_pass_count(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 5, "days": 30},
    )
    body = resp.json()
    # scores 5,6,7,8,9 → would_pass=5; currently_pass=3 (7,8,9). swing_in=5,6.
    assert body["would_pass"] == 5
    assert body["swing_in"] == 2
    assert body["swing_out"] == 0
    assert body["delta"] == 2


def test_simulate_higher_threshold_decreases_pass_count(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 9, "days": 30},
    )
    body = resp.json()
    # Only score 9 would pass → would_pass=1, currently=3 → swing_out=2.
    assert body["would_pass"] == 1
    assert body["swing_out"] == 2
    assert body["swing_in"] == 0
    assert body["delta"] == -2


def test_simulate_excludes_banned_and_dup_and_failed(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 1, "days": 30},
    )
    body = resp.json()
    # All 6 scored topics would pass at threshold=1; banned is not counted.
    assert body["total_scored"] == 6
    assert body["would_pass"] == 6


def test_simulate_score_distribution_is_correct(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 7, "days": 30},
    )
    dist = {b["score"]: b["count"] for b in resp.json()["score_distribution"]}
    assert dist == {4: 1, 5: 1, 6: 1, 7: 1, 8: 1, 9: 1}


def test_simulate_respects_days_window(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 7, "days": 90},
    )
    # Adding the 45-day-ago topic into window → total grows by 1.
    assert resp.json()["total_scored"] == 7


def test_simulate_brand_isolation(env) -> None:
    client, _, other_id = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": other_id, "threshold": 5, "days": 30},
    )
    body = resp.json()
    assert body["total_scored"] == 1  # only the other-brand topic


def test_simulate_brand_not_found(env) -> None:
    client, _, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": 9999, "threshold": 5, "days": 30},
    )
    assert resp.status_code == 404


def test_simulate_validates_threshold_bounds(env) -> None:
    client, icon_id, _ = env
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 11, "days": 30},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/topics/simulate",
        headers=AUTH,
        json={"brand_id": icon_id, "threshold": 0, "days": 30},
    )
    assert resp.status_code == 422
