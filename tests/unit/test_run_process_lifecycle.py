"""NTS_074 — run executes off the event loop; cancel; orphan sweep.

Covers the three guarantees of taking ``run-all`` out of the admin-API event
loop:

* :func:`jobs.spawn_pipeline_run` launches a DETACHED subprocess (mocked) with
  the right argv + ``start_new_session=True`` and records its pid — never
  blocking the caller (no ``wait``/``communicate``).
* :func:`jobs.cancel_run` kills the worker by pid, flips the row to
  ``cancelled``, and is idempotent.
* :func:`jobs.sweep_orphaned_runs` force-fails ``running`` rows whose worker
  pid is dead (or absent past the grace window) while leaving live runs alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.admin import db as admin_db
from pipeline.admin import jobs
from pipeline.admin.models import Run
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory_and_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    # Parent dir exists → _run_worker_log_target opens a real file we can
    # assert gets closed (no fd leak), instead of falling back to DEVNULL.
    monkeypatch.setenv("ADMIN_LOG_PATH", str(tmp_path / "admin.log"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand_id = seed_icon_brand(session)
        session.commit()
    yield factory, brand_id
    admin_db.reset_for_tests()


def _make_run(factory, brand_id, *, status="running", pid=None, age_seconds=0) -> int:
    with factory() as session:
        run = Run(
            brand_id_fk=brand_id,
            triggered_by="manual",
            source_ids="[]",
            started_at=NOW - timedelta(seconds=age_seconds),
            status=status,
            pid=pid,
            finished_at=None if status == "running" else NOW,
        )
        session.add(run)
        session.commit()
        return run.id


# --- spawn (Task 1) -------------------------------------------------------


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.waited = False

    def wait(self, *a, **k):  # pragma: no cover - must never be called
        self.waited = True

    def communicate(self, *a, **k):  # pragma: no cover - must never be called
        self.waited = True


def test_spawn_launches_detached_subprocess_and_records_pid(
    factory_and_brand, monkeypatch
) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=None)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(pid=314159)

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)

    pid = jobs.spawn_pipeline_run(run_id)

    # Returns the worker pid, recorded on the row.
    assert pid == 314159
    with factory() as session:
        assert session.get(Run, run_id).pid == 314159

    # argv runs the detached worker entrypoint for THIS run id.
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "pipeline.run",
        "for-run",
        "--run-id",
        str(run_id),
    ]
    # Detached: own session/process-group so it's killable as a group on
    # cancel and free of the API's stdio. The caller never waits → non-blocking.
    assert captured["kwargs"]["start_new_session"] is True
    # The worker log fd handed to the child was closed in the parent (no leak).
    stdout = captured["kwargs"]["stdout"]
    if stdout not in (subprocess.DEVNULL, None):
        assert stdout.closed is True


def test_spawn_failure_force_fails_the_run(factory_and_brand, monkeypatch) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=None)

    def boom(cmd, **kwargs):
        raise OSError("fork failed")

    monkeypatch.setattr(jobs.subprocess, "Popen", boom)

    pid = jobs.spawn_pipeline_run(run_id)
    assert pid is None
    with factory() as session:
        row = session.get(Run, run_id)
        assert row.status == "failed"
        assert row.finished_at is not None
        assert "spawn failed" in row.log_excerpt


# --- cancel (Task 2) ------------------------------------------------------


def test_cancel_running_kills_and_marks_cancelled(
    factory_and_brand, monkeypatch
) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=4242)

    killed: list[int] = []
    monkeypatch.setattr(jobs, "_terminate_process_group", lambda pid: killed.append(pid))

    assert jobs.cancel_run(run_id) == "cancelled"
    assert killed == [4242]  # the worker pid was signalled
    with factory() as session:
        row = session.get(Run, run_id)
        assert row.status == "cancelled"
        assert row.finished_at is not None
        assert jobs.CANCEL_NOTE in row.log_excerpt


def test_cancel_is_idempotent(factory_and_brand, monkeypatch) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=4242)

    killed: list[int] = []
    monkeypatch.setattr(jobs, "_terminate_process_group", lambda pid: killed.append(pid))

    assert jobs.cancel_run(run_id) == "cancelled"
    # Second cancel: already terminal → no-op, no second kill.
    assert jobs.cancel_run(run_id) == "already:cancelled"
    assert killed == [4242]


def test_cancel_unknown_run_is_not_found(factory_and_brand) -> None:
    factory, _ = factory_and_brand
    assert jobs.cancel_run(999999) == "not_found"


def test_cancel_finished_run_is_noop(factory_and_brand, monkeypatch) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="success", pid=4242)
    killed: list[int] = []
    monkeypatch.setattr(jobs, "_terminate_process_group", lambda pid: killed.append(pid))

    assert jobs.cancel_run(run_id) == "already:success"
    assert killed == []  # never signal a finished run's (possibly reused) pid
    with factory() as session:
        assert session.get(Run, run_id).status == "success"


def test_cancel_running_without_pid_still_marks_cancelled(factory_and_brand) -> None:
    # A run cancelled in the insert→spawn window has no pid yet — nothing to
    # kill, but the row must still flip so the UI/cron stop reflecting it.
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=None)
    assert jobs.cancel_run(run_id) == "cancelled"
    with factory() as session:
        assert session.get(Run, run_id).status == "cancelled"


# --- orphan sweep (Task 3) ------------------------------------------------


def test_process_alive_self_is_true_and_dead_is_false() -> None:
    assert jobs._process_alive(os.getpid()) is True
    assert jobs._process_alive(None) is False
    assert jobs._process_alive(0) is False


def test_sweep_dead_pid_is_failed(factory_and_brand, monkeypatch) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=4242)
    monkeypatch.setattr(jobs, "_process_alive", lambda pid: False)

    assert jobs.sweep_orphaned_runs(now=NOW) == 1
    with factory() as session:
        row = session.get(Run, run_id)
        assert row.status == "failed"
        assert row.finished_at is not None
        assert "orphaned by restart" in row.log_excerpt


def test_sweep_alive_pid_is_untouched(factory_and_brand, monkeypatch) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="running", pid=4242)
    monkeypatch.setattr(jobs, "_process_alive", lambda pid: True)

    assert jobs.sweep_orphaned_runs(now=NOW) == 0
    with factory() as session:
        row = session.get(Run, run_id)
        assert row.status == "running"
        assert row.finished_at is None


def test_sweep_null_pid_young_is_untouched(factory_and_brand) -> None:
    # No pid yet but only seconds old → likely mid-spawn, not an orphan.
    factory, brand_id = factory_and_brand
    _make_run(factory, brand_id, status="running", pid=None, age_seconds=5)
    assert jobs.sweep_orphaned_runs(now=NOW) == 0


def test_sweep_null_pid_old_is_failed(factory_and_brand) -> None:
    # No pid and well past the grace window → legacy in-process run orphaned by
    # a restart (the Run #42 class).
    factory, brand_id = factory_and_brand
    run_id = _make_run(
        factory, brand_id, status="running", pid=None, age_seconds=600
    )
    assert jobs.sweep_orphaned_runs(now=NOW) == 1
    with factory() as session:
        assert session.get(Run, run_id).status == "failed"


def test_sweep_leaves_finished_runs_alone(factory_and_brand, monkeypatch) -> None:
    factory, brand_id = factory_and_brand
    run_id = _make_run(factory, brand_id, status="success", pid=4242)
    monkeypatch.setattr(jobs, "_process_alive", lambda pid: False)
    assert jobs.sweep_orphaned_runs(now=NOW) == 0
    with factory() as session:
        assert session.get(Run, run_id).status == "success"
