"""IT_PROJ_NTS_098 DoD 9 — the v3 tables are inside the backup set (NTS_078).

admin.db is the only copy of the portfolio, the editor's decision history and
the brand's service taxonomy. Sanity holds the published articles; nothing
else holds these. So "candidates is in the backup set" is not a documentation
claim — it is exercised here by actually running ``nts-backup.sh`` and
``nts-restore.sh`` against a database at head with v3 rows in it.

The backup takes a whole-file snapshot through the SQLite online-backup API,
so new tables are included by construction. That is a property worth pinning:
the day someone "optimises" it into a per-table dump, this test is what says
the portfolio silently stopped being backed up.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SH = PROJECT_ROOT / "infra/backup/nts-backup.sh"
RESTORE_SH = PROJECT_ROOT / "infra/backup/nts-restore.sh"

_V3_TABLES = ("candidates", "review_decisions", "brand_taxonomy")
_SOURCE_V3_COLUMNS = (
    "source_role",
    "source_class",
    "license_class",
    "doc_language",
    "fetch_method",
    "reliability",
    "cache_ttl_days",
)

needs_sqlite3_cli = pytest.mark.skipif(
    shutil.which("sqlite3") is None,
    reason="the backup scripts shell out to the sqlite3 CLI",
)


# --- the scripts say what they should ------------------------------------


def test_backup_snapshots_the_whole_file_not_a_table_list() -> None:
    """A per-table dump is how a new table quietly stops being backed up."""
    script = BACKUP_SH.read_text()
    assert ".backup" in script, "backup must use the SQLite online-backup API"
    # No table allowlist anywhere: the moment one appears, every future table
    # has to be remembered by hand.
    assert "SELECT * FROM" not in script
    assert ".dump" not in script


def test_restore_verification_names_the_v3_tables() -> None:
    """A restore that only counts ``runs`` proves one table out of sixteen."""
    script = RESTORE_SH.read_text()
    for table in _V3_TABLES:
        assert table in script, f"restore verification never mentions {table}"
    for column in _SOURCE_V3_COLUMNS:
        assert column in script, f"restore verification never checks sources.{column}"


# --- end to end: dump and restore a database with v3 rows ----------------


@pytest.fixture
def db_at_head(tmp_path: Path) -> Path:
    db_path = tmp_path / "admin.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "PYTHONPATH": str(PROJECT_ROOT),
        "ADMIN_DB_PATH": str(db_path),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    with sqlite3.connect(db_path) as conn:
        # WAL is how prod runs (NTS_059/061) — and the reason the script must
        # use .backup rather than cp.
        conn.execute("PRAGMA journal_mode=WAL")
        brand_id = conn.execute("SELECT id FROM brands WHERE slug='icon'").fetchone()[0]
        conn.execute(
            "INSERT INTO sources (brand_id_fk, name, source_type, url, "
            "primary_category, active, paywall, polling_minutes, created_at, "
            "updated_at, source_role, source_class, license_class, doc_language, "
            "fetch_method, reliability, cache_ttl_days) VALUES (?, 'FINMA News EN', "
            "'rss', 'https://www.finma.ch/en/rss/news/', 'regulation', 1, 0, 720, "
            "datetime('now'), datetime('now'), 'primary_feed', 'regulator', "
            "'public_official', 'en', 'rss', 0.97, 14)",
            (brand_id,),
        )
        source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO candidates (brand_id_fk, source_id_fk, input_kind, "
            "source_title, source_summary, verdict, reason_code, reason, status, "
            "service_category, event_stage, depth_prior) VALUES (?, ?, 'document', "
            "'FINMA circular 2026/1', 'Trustee onboarding tightened', 'accept', "
            "'ok', 'changes onboarding rules for trustees', 'pending', 'wealth', "
            "'adopted', 'deep')",
            (brand_id, source_id),
        )
        candidate_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO review_decisions (brand_id_fk, candidate_id_fk, action, "
            "scope, reviewer, time_spent_s) VALUES (?, ?, 'disagree_guard', "
            "'rubric', 'andriy', 312)",
            (brand_id, candidate_id),
        )
    return db_path


def _run(script: Path, args: list[str], db: Path, backups: Path):
    return subprocess.run(
        ["bash", str(script), *args],
        env={
            **os.environ,
            "NTS_DB_PATH": str(db),
            "NTS_BACKUP_DIR": str(backups),
        },
        capture_output=True,
        text=True,
    )


@needs_sqlite3_cli
def test_v3_rows_survive_a_real_backup_and_restore(
    db_at_head: Path, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"

    result = _run(BACKUP_SH, [], db_at_head, backups)
    assert result.returncode == 0, result.stderr

    dumps = list(backups.glob("admin-*.db.gz"))
    assert len(dumps) == 1, f"expected one dump, got {dumps}"
    assert (backups / ".last_ok").read_text().strip(), "heartbeat not written"

    stamp = dumps[0].name.removeprefix("admin-").removesuffix(".db.gz")
    result = _run(RESTORE_SH, [stamp], db_at_head, backups)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout, result.stdout

    restored = Path("/tmp/restore-test") / f"admin-{stamp}.db"
    assert restored.exists()

    with sqlite3.connect(restored) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(_V3_TABLES) <= tables

        # The rows themselves, not just the tables.
        assert conn.execute(
            "SELECT source_title FROM candidates"
        ).fetchone()[0] == "FINMA circular 2026/1"
        assert conn.execute(
            "SELECT time_spent_s FROM review_decisions"
        ).fetchone()[0] == 312
        assert conn.execute(
            "SELECT count(*) FROM brand_taxonomy"
        ).fetchone()[0] == 5

        # The registry fields are columns, not tables — a row count would not
        # have noticed them going missing.
        source_columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        assert set(_SOURCE_V3_COLUMNS) <= source_columns
        assert conn.execute(
            "SELECT source_role, license_class, reliability FROM sources "
            "WHERE name = 'FINMA News EN'"
        ).fetchone() == ("primary_feed", "public_official", 0.97)


@needs_sqlite3_cli
def test_restore_output_reports_the_v3_tables(
    db_at_head: Path, tmp_path: Path
) -> None:
    """Andriy reads this output during an incident; it has to show him that
    the portfolio came back, not just that a file opened."""
    backups = tmp_path / "backups"
    _run(BACKUP_SH, [], db_at_head, backups)
    stamp = next(backups.glob("admin-*.db.gz")).name.removeprefix(
        "admin-"
    ).removesuffix(".db.gz")

    result = _run(RESTORE_SH, [stamp], db_at_head, backups)

    assert "candidates" in result.stdout
    assert "review_decisions" in result.stdout
    assert "brand_taxonomy" in result.stdout
    assert "MISSING" not in result.stdout, result.stdout


@needs_sqlite3_cli
def test_restore_of_a_pre_v3_dump_reports_rather_than_aborts(
    tmp_path: Path,
) -> None:
    """A dump taken before migration 020 has no ``candidates``. Verifying it
    must still work — during a restore, an abort on a missing table is the
    difference between a usable old backup and none."""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO runs (id) VALUES (1)")

    backups = tmp_path / "backups"
    assert _run(BACKUP_SH, [], db, backups).returncode == 0
    stamp = next(backups.glob("admin-*.db.gz")).name.removeprefix(
        "admin-"
    ).removesuffix(".db.gz")

    result = _run(RESTORE_SH, [stamp], db, backups)
    assert result.returncode == 0, result.stderr
    assert "candidates" in result.stdout
    # Absent tables print a dash; the run still completes.
    assert "-" in result.stdout
