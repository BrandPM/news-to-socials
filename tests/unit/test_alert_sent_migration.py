"""Alembic round-trip for migration 012_alert_sent (IT_PROJ_NTS_073).

Runs the real migration runner (upgrade head → downgrade 011 → upgrade head)
against a temp SQLite DB and asserts the ``alert_sent`` table appears and
unwinds correctly.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _alembic_env(test_db: Path) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    return {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(test_db),
    }


def test_migration_012_alert_sent_round_trip(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-test.db"
    env = _alembic_env(test_db)

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    alembic("upgrade", "head")
    with sqlite3.connect(test_db) as conn:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(alert_sent)").fetchall()
        }
        # Migration 029 turned this from a dedup ledger into a delivery
        # ledger (NTS_106 §1): a row now means "we intended to send this", and
        # ``delivered`` says whether it landed. 012's own two columns are
        # still here and still carry the same meaning.
        assert {"notification_id", "sent_at"} <= cols
        assert {"delivered", "attempts", "last_attempt_at", "message"} <= cols
        # notification_id is the primary key.
        pk = [
            r[1]
            for r in conn.execute("PRAGMA table_info(alert_sent)").fetchall()
            if r[5]  # pk flag
        ]
        assert pk == ["notification_id"]

    # Downgrade one step drops the table.
    alembic("downgrade", "011_resync_nts070")
    with sqlite3.connect(test_db) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "alert_sent" not in tables

    # Re-upgrade restores it.
    alembic("upgrade", "head")
    with sqlite3.connect(test_db) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "alert_sent" in tables
