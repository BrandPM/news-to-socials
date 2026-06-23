"""Tests for the extracted compute_notifications (IT_PROJ_NTS_073).

The HTTP route's behaviour is covered by ``test_notifications.py``; these
assert the core function it now delegates to returns the same data when
called directly (the alerter consumes this same path).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.admin import db as admin_db
from pipeline.admin.models import Run
from pipeline.admin.notifications_core import compute_notifications
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand_id = seed_icon_brand(session)
        session.commit()
    yield factory, brand_id
    admin_db.reset_for_tests()


def test_empty(session_env) -> None:
    factory, brand_id = session_env
    with factory() as session:
        assert compute_notifications(session, brand_id) == []


def test_failed_run_surfaces_as_danger_sorted_newest_first(session_env) -> None:
    factory, brand_id = session_env
    now = datetime.now(tz=timezone.utc)
    with factory() as session:
        session.add_all(
            [
                Run(
                    brand_id_fk=brand_id,
                    triggered_by="manual",
                    source_ids="[]",
                    started_at=now - timedelta(hours=2),
                    status="failed",
                    log_excerpt="first\nold failure",
                ),
                Run(
                    brand_id_fk=brand_id,
                    triggered_by="manual",
                    source_ids="[]",
                    started_at=now - timedelta(minutes=5),
                    status="failed",
                    log_excerpt="recent failure line",
                ),
            ]
        )
        session.commit()

    with factory() as session:
        items = compute_notifications(session, brand_id)

    assert len(items) == 2
    assert all(i.kind == "run_failed" and i.severity == "danger" for i in items)
    # Newest first.
    assert items[0].description == "recent failure line"
    assert items[0].created_at >= items[1].created_at


def test_old_failed_run_excluded(session_env) -> None:
    factory, brand_id = session_env
    with factory() as session:
        session.add(
            Run(
                brand_id_fk=brand_id,
                triggered_by="manual",
                source_ids="[]",
                started_at=datetime.now(tz=timezone.utc) - timedelta(hours=30),
                status="failed",
                log_excerpt="stale",
            )
        )
        session.commit()
    with factory() as session:
        assert compute_notifications(session, brand_id) == []
