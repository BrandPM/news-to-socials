"""Regression tests for the NTS_059 hang fix.

Two guarantees the production incident hinged on:

1. Every SQLite connection comes up with a non-zero ``busy_timeout`` and the
   DB in WAL journal mode — without these, the stale-run sweep thread and
   request threads deadlocked/failed on the rollback-journal writer lock.
2. App startup never blocks the event loop — ``/health`` must answer fast
   immediately after boot, even with the BackgroundScheduler enabled. A
   blocking sync DB sweep in the async startup path would freeze this.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.common import config as config_module


@pytest.fixture
def file_engine(tmp_path):
    """Engine bound to a real on-disk DB (WAL is a no-op for :memory:)."""
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    try:
        yield engine
    finally:
        admin_db.reset_for_tests()


def test_connection_has_busy_timeout(file_engine):
    with file_engine.connect() as conn:
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert busy is not None and busy >= 5000


def test_connection_is_wal(file_engine):
    with file_engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert str(mode).lower() == "wal"


def test_connection_synchronous_and_fk(file_engine):
    """WAL's safe pairing is synchronous=NORMAL (1); FK enforcement stays on."""
    with file_engine.connect() as conn:
        synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
        fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert synchronous == 1  # NORMAL
    assert fk == 1


def test_startup_does_not_block_event_loop(tmp_path, monkeypatch):
    """Boot the app with the scheduler ON; startup + first /health < 500ms.

    Regression guard for the NTS_058 hotfix: the stale-run sweep must run on
    the BackgroundScheduler thread, never as a blocking sync call in the async
    startup path. The TestClient context manager runs lifespan startup
    synchronously, so this wall-clock window covers boot + the first request.
    """
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_RUN_SCHEDULER", "1")  # override conftest's "0"
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    from pipeline.admin.server import create_app

    app = create_app()
    start = time.perf_counter()
    # Entering the context manager fires lifespan startup (scheduler.start()).
    with TestClient(app) as client:
        resp = client.get("/health")
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert elapsed_ms < 500, f"startup+health took {elapsed_ms:.0f}ms"
    admin_db.reset_for_tests()
