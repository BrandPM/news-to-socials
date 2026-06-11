"""NTS_056 Task 3 — stale-run cleanup (close runs stuck 'running' >24h).

Mirrors the zombie runs (#13, #21) from NTS_055 that hung in the Active
runs panel for days because no cleanup job ever closed them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.admin import db as admin_db
from pipeline.admin import jobs
from pipeline.admin.models import Run
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory_and_brand(tmp_path, monkeypatch):
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


def _make_run(factory, brand_id, *, status, age_hours, log=None) -> int:
    with factory() as session:
        run = Run(
            brand_id_fk=brand_id,
            triggered_by="cron",
            source_ids="[]",
            started_at=NOW - timedelta(hours=age_hours),
            status=status,
            log_excerpt=log,
            finished_at=None if status == "running" else NOW - timedelta(hours=age_hours),
        )
        session.add(run)
        session.commit()
        return run.id


def test_running_25h_is_failed(factory_and_brand) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", age_hours=25)

    closed = jobs.close_stale_runs(now=NOW)
    assert closed == 1

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "failed"
        assert run.finished_at is not None
        assert "NTS_056 cleanup" in run.log_excerpt


def test_running_23h_is_untouched(factory_and_brand) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", age_hours=23)

    closed = jobs.close_stale_runs(now=NOW)
    assert closed == 0

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == "running"
        assert run.finished_at is None


def test_old_success_is_untouched(factory_and_brand) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="success", age_hours=240)

    closed = jobs.close_stale_runs(now=NOW)
    assert closed == 0

    with factory() as session:
        assert session.get(Run, run_id).status == "success"


def test_cleanup_appends_to_existing_log(factory_and_brand) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(
        factory, brand_id, status="running", age_hours=30, log="prior line"
    )

    jobs.close_stale_runs(now=NOW)

    with factory() as session:
        log = session.get(Run, run_id).log_excerpt
        assert log.startswith("prior line")
        assert "NTS_056 cleanup" in log


def test_cleanup_is_idempotent(factory_and_brand) -> None:
    factory, brand_id = factory_and_brand
    _make_run(factory, brand_id, status="running", age_hours=50)

    assert jobs.close_stale_runs(now=NOW) == 1
    # Second pass finds nothing left to close.
    assert jobs.close_stale_runs(now=NOW) == 0
