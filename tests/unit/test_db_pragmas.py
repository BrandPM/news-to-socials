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
from sqlalchemy import create_engine

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


# --- NTS_061: PRAGMA listener is scoped to the admin engine only ----------


def test_other_sqlite_engine_unaffected_by_admin_listener(tmp_path):
    """A foreign SQLite engine created in the SAME process must NOT inherit the
    admin engine's PRAGMAs.

    Before NTS_061 the listener was attached to the ``Engine`` *class*
    (``@event.listens_for(Engine, "connect")``), so every SQLite engine in the
    process — e.g. the pipeline's own DB — got flipped to WAL + busy_timeout
    uninvited. With the listener bound to the admin-engine instance, an
    unrelated engine keeps SQLite's defaults (rollback-journal "delete" mode,
    busy_timeout 0).
    """
    # Build the admin engine first so its instance-scoped listener is live.
    admin_db.reset_for_tests()
    admin_engine = admin_db.get_engine(path=tmp_path / "admin.db")
    try:
        with admin_engine.connect() as conn:
            assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"

        # A separate, unrelated on-disk SQLite engine in the same process.
        other = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}", future=True)
        try:
            with other.connect() as conn:
                mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
                fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
                synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
            # Our handler would force these to wal / 1 / 1. A foreign engine the
            # admin listener never touched keeps SQLite's defaults: rollback
            # "delete" journal, FK enforcement OFF, synchronous FULL (2).
            # (busy_timeout is NOT asserted here — it's environment-dependent
            #  and some libsqlite builds default it non-zero, so it can't tell
            #  "we set it" from "the platform did".)
            assert str(mode).lower() != "wal"
            assert fk == 0
            assert synchronous == 2  # FULL — our handler would have set NORMAL (1)
        finally:
            other.dispose()
    finally:
        admin_db.reset_for_tests()


def test_admin_engine_recreated_keeps_pragmas(tmp_path):
    """Recreating the admin engine (e.g. reset_for_tests + new path) re-attaches
    the listener — boot/idempotency guard. The fresh engine must still come up
    WAL + busy_timeout, and a foreign engine made afterwards stays clean."""
    admin_db.reset_for_tests()
    admin_db.get_engine(path=tmp_path / "a.db")
    admin_db.reset_for_tests()
    engine2 = admin_db.get_engine(path=tmp_path / "b.db")
    try:
        with engine2.connect() as conn:
            assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 5000
    finally:
        admin_db.reset_for_tests()
