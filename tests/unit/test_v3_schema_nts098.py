"""IT_PROJ_NTS_098 DoD 1 — the v3 contour-1 schema, migrations 020 + 021.

Session S1 of NTS_114 lays the data floor the rest of v3 stands on. Nothing
reads these tables yet, which is exactly why they need testing now: a wrong
column or a CHECK that admits garbage will not surface as a failure until S4
is writing into it under load.

What is asserted here:

* 020 round-trips — upgrade, re-upgrade (idempotent), downgrade, re-upgrade.
* 020's downgrade does not take neighbouring columns with it. SQLite batch
  ALTER rebuilds tables, which is precisely how a neighbour goes missing.
* The CHECK constraints actually reject out-of-vocabulary values. A CHECK that
  was written but not enforced is worse than none — it reads as a guarantee.
* 021 rebuilds ``prompts`` without losing rows or the *partial* unique index.
* The migration schema and the ORM schema agree, column for column. The test
  suite builds its DBs with ``create_all`` while prod is built by alembic; if
  those two drift, every other test is testing a schema prod does not have.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from pipeline.admin.db import Base
from pipeline.admin.models import (  # noqa: F401  (registers the tables)
    Candidate,
)

_PREV = "019_resync_nts092"
_020 = "020_v3_portfolio_core"
_021 = "021_editorial_guard_prompt_type"

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


@pytest.fixture
def alembic_db(tmp_path: Path):
    """A scratch admin.db plus a bound alembic runner."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "admin-v3.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(db_path),
    }

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    return db_path, alembic


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _ddl(db: Path, name: str) -> str:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
    return row[0] if row and row[0] else ""


# --- migration 020: round trip -------------------------------------------


def test_020_creates_the_three_tables_and_round_trips(alembic_db) -> None:
    db, alembic = alembic_db

    alembic("upgrade", _020)
    assert set(_V3_TABLES) <= _tables(db)

    # Idempotent: a half-applied deploy can be re-run without hand-editing
    # alembic_version.
    alembic("upgrade", _020)
    assert set(_V3_TABLES) <= _tables(db)

    alembic("downgrade", _PREV)
    assert not (set(_V3_TABLES) & _tables(db))

    alembic("upgrade", _020)
    assert set(_V3_TABLES) <= _tables(db)


def test_020_widens_sources_draft_approvals_and_runs(alembic_db) -> None:
    db, alembic = alembic_db
    alembic("upgrade", _020)

    assert set(_SOURCE_V3_COLUMNS) <= _columns(db, "sources")
    assert "candidate_id_fk" in _columns(db, "draft_approvals")
    assert "run_type" in _columns(db, "runs")

    alembic("downgrade", _PREV)
    assert not (set(_SOURCE_V3_COLUMNS) & _columns(db, "sources"))
    assert "candidate_id_fk" not in _columns(db, "draft_approvals")
    assert "run_type" not in _columns(db, "runs")


def test_020_downgrade_leaves_neighbouring_columns_alone(alembic_db) -> None:
    """Batch ALTER rebuilds the table — the classic way an unrelated column
    from NTS_090/091/092/094 disappears."""
    db, alembic = alembic_db
    alembic("upgrade", _020)
    alembic("downgrade", _PREV)

    survivors = {
        "pipeline_config": (
            "images_on_demand",
            "research_enabled",
            "research_timeout_seconds",
            "eval_threshold",
            "dedup_threshold",
            "dedup_window_days",
            "stale_draft_days",
            "banned_phrases",
            "voice_profile",
        ),
        "sources": ("url", "primary_category", "polling_minutes", "brand_id_fk"),
        "runs": ("pid", "progress", "languages_completed", "log_excerpt"),
        "draft_approvals": ("published_at", "sanity_published_id", "note"),
    }
    for table, columns in survivors.items():
        present = _columns(db, table)
        for column in columns:
            assert column in present, f"020 downgrade lost {table}.{column}"


def test_020_downgrade_keeps_the_draft_approvals_check_and_unique(
    alembic_db,
) -> None:
    """The draft_approvals rebuild is the riskiest step in 020: a dropped
    CHECK or UNIQUE would let a second decision row exist per draft."""
    db, alembic = alembic_db
    alembic("upgrade", _020)
    alembic("downgrade", _PREV)

    ddl = _ddl(db, "draft_approvals")
    assert "ck_draft_approvals_status" in ddl
    assert "uq_draft_approvals_draft_brand" in ddl


# --- migration 020: the CHECKs are real ----------------------------------


def _seed_brand_and_source(db: Path) -> tuple[int, int]:
    with sqlite3.connect(db) as conn:
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO sources (brand_id_fk, name, source_type, url, "
            "primary_category, active, paywall, polling_minutes, created_at, "
            "updated_at) VALUES (?, 'FINMA', 'rss', 'https://x/rss', 'reg', "
            "1, 0, 720, datetime('now'), datetime('now'))",
            (brand_id,),
        )
        source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return brand_id, source_id


def _insert_candidate(db: Path, brand_id: int, **overrides: object) -> None:
    row = {
        "brand_id_fk": brand_id,
        "input_kind": "document",
        "source_title": "FINMA circular 2026/1",
        "verdict": "accept",
        "reason_code": "ok",
        "reason": "changes onboarding rules for trustees",
        "status": "pending",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with sqlite3.connect(db) as conn:
        conn.execute(f"INSERT INTO candidates ({cols}) VALUES ({marks})", tuple(row.values()))


@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        ("input_kind", "rumour"),
        ("verdict", "maybe"),
        ("reason_code", "vibes"),
        ("status", "in_flight"),
        ("event_stage", "gossip"),
        ("depth_prior", "epic"),
        ("depth_final", "epic"),
        ("manual_action", "yeeted"),
        ("source_class", "blog"),
    ],
)
def test_candidates_checks_reject_unknown_values(
    alembic_db, column: str, bad_value: str
) -> None:
    db, alembic = alembic_db
    alembic("upgrade", "head")
    brand_id, _ = _seed_brand_and_source(db)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_candidate(db, brand_id, **{column: bad_value})


def test_candidates_accepts_the_documented_vocabulary(alembic_db) -> None:
    db, alembic = alembic_db
    alembic("upgrade", "head")
    brand_id, source_id = _seed_brand_and_source(db)

    _insert_candidate(
        db,
        brand_id,
        source_id_fk=source_id,
        input_kind="news",
        verdict="reject",
        reason_code="no_document",
        status="rejected",
        event_stage="deal_announced",
        depth_prior="deep",
        depth_final="article",
        manual_action="held",
        source_class="regulator",
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1


def test_review_decisions_check_rejects_unknown_action(alembic_db) -> None:
    db, alembic = alembic_db
    alembic("upgrade", "head")
    brand_id, _ = _seed_brand_and_source(db)
    _insert_candidate(db, brand_id)

    with sqlite3.connect(db) as conn:
        candidate_id = conn.execute("SELECT id FROM candidates").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO review_decisions (brand_id_fk, candidate_id_fk, action) "
                "VALUES (?, ?, 'ghosted')",
                (brand_id, candidate_id),
            )
        conn.execute(
            "INSERT INTO review_decisions (brand_id_fk, candidate_id_fk, action, "
            "time_spent_s) VALUES (?, ?, 'disagree_guard', 240)",
            (brand_id, candidate_id),
        )


def test_runs_run_type_is_nullable_for_pre_v3_rows(alembic_db) -> None:
    """The ~72 historical rows are none of the four v3 types. NULL means
    "pre-v3" and must remain insertable; a bogus value must not."""
    db, alembic = alembic_db
    alembic("upgrade", "head")
    with sqlite3.connect(db) as conn:
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO runs (brand_id_fk, triggered_by, source_ids, started_at, "
            "status, languages_completed, progress) VALUES (?, 'cron', '[]', "
            "datetime('now'), 'success', '[]', '{}')",
            (brand_id,),
        )
        assert conn.execute(
            "SELECT run_type FROM runs WHERE run_type IS NULL"
        ).fetchone() is not None

        conn.execute(
            "INSERT INTO runs (brand_id_fk, triggered_by, source_ids, started_at, "
            "status, run_type, languages_completed, progress) VALUES (?, 'cron', "
            "'[]', datetime('now'), 'success', 'intake', '[]', '{}')",
            (brand_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO runs (brand_id_fk, triggered_by, source_ids, "
                "started_at, status, run_type, languages_completed, progress) "
                "VALUES (?, 'cron', '[]', datetime('now'), 'success', 'wat', "
                "'[]', '{}')",
                (brand_id,),
            )


@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        ("source_role", "gossip_column"),
        ("source_class", "substack"),
        ("license_class", "vibes"),
        ("fetch_method", "carrier_pigeon"),
    ],
)
def test_sources_registry_checks_reject_unknown_values(
    alembic_db, column: str, bad_value: str
) -> None:
    db, alembic = alembic_db
    alembic("upgrade", "head")
    with sqlite3.connect(db) as conn:
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                f"INSERT INTO sources (brand_id_fk, name, source_type, url, "
                f"primary_category, active, paywall, polling_minutes, "
                f"created_at, updated_at, {column}) VALUES (?, 'x', 'rss', "
                f"'https://x', 'c', 1, 0, 720, datetime('now'), "
                f"datetime('now'), ?)",
                (brand_id, bad_value),
            )


def test_existing_sources_default_to_the_most_restrictive_licence(
    alembic_db,
) -> None:
    """NTS_108 §1 — an unclassified feed must not be readable as
    unrestricted. ``news_paywalled`` allows the headline as a lead, nothing
    more; the Sources screen reclassifies upward from there."""
    db, alembic = alembic_db
    alembic("upgrade", _PREV)
    _seed_brand_and_source(db)
    alembic("upgrade", "head")

    with sqlite3.connect(db) as conn:
        role, klass, licence, fetch = conn.execute(
            "SELECT source_role, source_class, license_class, fetch_method "
            "FROM sources WHERE name = 'FINMA'"
        ).fetchone()
    assert (role, klass, licence) == ("news", "news", "news_paywalled")
    # Backfilled from source_type rather than left NULL.
    assert fetch == "rss"


def test_brand_timezone_is_backfilled_from_the_brand_row(alembic_db) -> None:
    """``pipeline_config.brand_timezone`` overlaps ``brands.timezone`` by
    spec (NTS_098 §4). Two sources of truth that disagree from birth would be
    a silent slot-date bug, so 020 copies the existing value across."""
    db, alembic = alembic_db
    alembic("upgrade", _PREV)
    with sqlite3.connect(db) as conn:
        brand_id = conn.execute(
            "SELECT id FROM brands WHERE slug = 'icon'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE brands SET timezone = 'Europe/Zurich' WHERE id = ?", (brand_id,)
        )
        conn.execute(
            "INSERT INTO pipeline_config (brand_id_fk, scoring_threshold, "
            "topics_per_run, banned_phrases, voice_profile, updated_at) "
            "VALUES (?, 7, 3, '[]', 'mission: x', datetime('now'))",
            (brand_id,),
        )

    alembic("upgrade", _020)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT brand_timezone FROM pipeline_config WHERE brand_id_fk = ?",
            (brand_id,),
        ).fetchone()[0] == "Europe/Zurich"


def test_migrating_a_populated_database_loses_nothing(alembic_db) -> None:
    """The rebuild steps (draft_approvals, runs, sources, prompts) copy data.
    Row counts and integrity are checked on a database that has rows in it —
    an empty-database migration proves very little.

    S2 added two seeding migrations, so "unchanged" became the wrong assertion:
    022 inserts the twelve NTS_115 primary feeds and 023 inserts one rubric per
    brand. The invariant is *no loss* — the pre-existing rows survive the
    rebuilds — plus the exact expected additions, spelled out so a migration
    that starts inserting something else fails here instead of quietly growing
    the tables.
    """
    db, alembic = alembic_db
    alembic("upgrade", _PREV)
    brand_id, _source_id = _seed_brand_and_source(db)
    with sqlite3.connect(db) as conn:
        for _ in range(72):
            conn.execute(
                "INSERT INTO runs (brand_id_fk, triggered_by, source_ids, "
                "started_at, status, languages_completed, progress) VALUES "
                "(?, 'cron', '[1]', datetime('now'), 'success', '[]', '{}')",
                (brand_id,),
            )
        conn.execute(
            "INSERT INTO draft_approvals (sanity_draft_id, brand_id_fk, status, "
            "decided_at, decided_by) VALUES ('drafts.abc', ?, 'approved', "
            "datetime('now'), 'andriy')",
            (brand_id,),
        )
        before = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("brands", "sources", "runs", "draft_approvals", "prompts")
        }

    alembic("upgrade", "head")

    with sqlite3.connect(db) as conn:
        after = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in before
        }
        brands = before["brands"]
        expected = {
            **before,
            # 022 — the primary feeds from NTS_115 artefact 1: thirteen rows
            # for its twelve listed feeds, because its EUR-Lex line is one row
            # per saved search and it asks for two.
            "sources": before["sources"] + 13,
            # 023 — one active editorial_guard rubric per brand.
            "prompts": before["prompts"] + brands,
        }
        assert after == expected
        # Nothing the earlier revisions wrote was dropped by a rebuild.
        assert all(after[t] >= before[t] for t in before)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # The v2 approval row is intact and simply has no candidate.
        assert conn.execute(
            "SELECT candidate_id_fk FROM draft_approvals WHERE "
            "sanity_draft_id = 'drafts.abc'"
        ).fetchone()[0] is None


def test_brand_taxonomy_is_seeded_for_icon_and_is_unique_per_key(
    alembic_db,
) -> None:
    """NTS_115 artefact 4 — the guard's {services} placeholder needs rows to
    resolve against from S2 on."""
    db, alembic = alembic_db
    alembic("upgrade", "head")

    with sqlite3.connect(db) as conn:
        rows = dict(
            conn.execute(
                "SELECT key, service_url_path FROM brand_taxonomy t "
                "JOIN brands b ON b.id = t.brand_id_fk WHERE b.slug = 'icon'"
            )
        )
        assert set(rows) == {"wealth", "family", "structuring", "ma", "special"}
        assert rows["ma"] == "/services/ma-consulting"
        assert all(
            r[0].strip()
            for r in conn.execute("SELECT description_for_guard FROM brand_taxonomy")
        )

        brand_id = conn.execute(
            "SELECT id FROM brands WHERE slug = 'icon'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO brand_taxonomy (brand_id_fk, key, label, "
                "description_for_guard, service_url_path) VALUES "
                "(?, 'wealth', 'dupe', 'd', '/x')",
                (brand_id,),
            )


def test_taxonomy_seed_does_not_overwrite_an_operator_edit(alembic_db) -> None:
    """Insert-when-absent: re-running the migration after an edit must not
    quietly restore the spec text over what the operator wrote."""
    db, alembic = alembic_db
    alembic("upgrade", "head")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE brand_taxonomy SET description_for_guard = 'EDITED BY ANDRIY' "
            "WHERE key = 'wealth'"
        )
    alembic("downgrade", _PREV)
    alembic("upgrade", "head")
    # A fresh table is reseeded from spec — that is correct. The guarantee is
    # about a re-run against a table that still holds the row:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE brand_taxonomy SET description_for_guard = 'EDITED BY ANDRIY' "
            "WHERE key = 'wealth'"
        )
    alembic("upgrade", "head")
    with sqlite3.connect(db) as conn:
        text = conn.execute(
            "SELECT description_for_guard FROM brand_taxonomy WHERE key = 'wealth'"
        ).fetchone()[0]
    assert text == "EDITED BY ANDRIY"


def test_candidate_survives_its_source_row_being_emptied(alembic_db) -> None:
    """NTS_098 §1 — the feed item is copied, not joined. An RSS entry falling
    off the end of the feed must not blank the candidate."""
    db, alembic = alembic_db
    alembic("upgrade", "head")
    brand_id, source_id = _seed_brand_and_source(db)
    _insert_candidate(
        db,
        brand_id,
        source_id_fk=source_id,
        source_summary="FINMA tightens trustee onboarding",
        source_url="https://finma.ch/x",
        source_name="FINMA News EN",
    )
    with sqlite3.connect(db) as conn:
        # The feed no longer carries the item; the source row is still there
        # but its last_run_stats are wiped and the item is gone upstream.
        conn.execute("UPDATE sources SET last_run_stats = NULL WHERE id = ?", (source_id,))
        title, summary, url, name = conn.execute(
            "SELECT source_title, source_summary, source_url, source_name "
            "FROM candidates"
        ).fetchone()
    assert title == "FINMA circular 2026/1"
    assert summary == "FINMA tightens trustee onboarding"
    assert url == "https://finma.ch/x"
    assert name == "FINMA News EN"


def test_review_decisions_restrict_blocks_deleting_a_reviewed_candidate(
    alembic_db,
) -> None:
    """The decision log is the only free signal for tuning the rubric
    (NTS_113) — it must not be orphanable."""
    db, alembic = alembic_db
    alembic("upgrade", "head")
    brand_id, _ = _seed_brand_and_source(db)
    _insert_candidate(db, brand_id)

    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        candidate_id = conn.execute("SELECT id FROM candidates").fetchone()[0]
        conn.execute(
            "INSERT INTO review_decisions (brand_id_fk, candidate_id_fk, action) "
            "VALUES (?, ?, 'approve')",
            (brand_id, candidate_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))


# --- migration 021: prompts CHECK rebuild --------------------------------


def _seed_prompt(db: Path, prompt_type: str, *, active: int = 1) -> None:
    with sqlite3.connect(db) as conn:
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO prompts (prompt_type, version_name, content, is_active, "
            "created_by, created_at, brand_id_fk) VALUES (?, 'v1', 'BODY {x}', ?, "
            "'test', datetime('now'), ?)",
            (prompt_type, active, brand_id),
        )


def test_021_admits_editorial_guard_and_still_rejects_nonsense(
    alembic_db,
) -> None:
    db, alembic = alembic_db
    alembic("upgrade", "head")

    # 023 already seeded one ACTIVE rubric per brand, so the type being
    # admitted is proved by the row's existence; a second row of the type has
    # to be inactive or it hits the partial unique index.
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM prompts WHERE prompt_type = 'editorial_guard'"
        ).fetchone()[0] >= 1
    _seed_prompt(db, "editorial_guard", active=0)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO prompts (prompt_type, version_name, content, is_active, "
            "created_by, created_at, brand_id_fk) VALUES ('bogus_type', 'v1', 'x', "
            "0, 'test', datetime('now'), 1)"
        )


def test_021_rebuild_keeps_every_existing_prompt_row(alembic_db) -> None:
    """The rebuild copies data. ``writer_draft`` / ``writer_polish`` rows are
    what the live generator reads (NTS_067) — losing one silently swaps the
    prompt for a code constant."""
    db, alembic = alembic_db
    alembic("upgrade", _020)
    for prompt_type in ("writer_draft", "writer_polish", "writer_translate"):
        _seed_prompt(db, prompt_type)
    with sqlite3.connect(db) as conn:
        before = sorted(
            conn.execute("SELECT id, prompt_type, content, is_active FROM prompts")
        )

    alembic("upgrade", _021)

    with sqlite3.connect(db) as conn:
        after = sorted(
            conn.execute("SELECT id, prompt_type, content, is_active FROM prompts")
        )
    assert after == before


def test_021_keeps_the_partial_unique_index(alembic_db) -> None:
    """``idx_active_prompt`` is UNIQUE *WHERE is_active = 1*. A SQLite rebuild
    that dropped the WHERE clause would still look like a unique index while
    silently forbidding a second inactive version — and a rebuild that dropped
    the index entirely would allow two active prompts of one type."""
    db, alembic = alembic_db
    alembic("upgrade", "head")

    ddl = _ddl(db, "idx_active_prompt")
    assert "UNIQUE" in ddl.upper()
    assert "is_active = 1" in ddl

    # Two ACTIVE rows of one type — forbidden.
    _seed_prompt(db, "writer_draft", active=1)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO prompts (prompt_type, version_name, content, is_active, "
            "created_by, created_at, brand_id_fk) VALUES ('writer_draft', 'v2', "
            "'x', 1, 'test', datetime('now'), ?)",
            (brand_id,),
        )

    # A second INACTIVE version of the same type — allowed (version history).
    _seed_prompt(db, "writer_draft", active=0)
    _seed_prompt(db, "writer_draft", active=0)


def test_021_downgrade_refuses_while_a_guard_prompt_exists(alembic_db) -> None:
    """Narrowing the CHECK under a live rubric row would either drop it
    silently or fail mid-rebuild. Refuse loudly instead."""
    db, alembic = alembic_db
    alembic("upgrade", "head")
    # 023's downgrade removes the rubric IT seeded, so a clean head → 020 walk
    # succeeds (asserted in test_editorial_guard_nts099). What 021 must still
    # refuse is a rubric row it did not create — an operator's edit, modelled
    # here as a row with different content that 023 leaves in place.
    _seed_prompt(db, "editorial_guard", active=0)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        alembic("downgrade", _020)
    assert "cannot downgrade 021" in excinfo.value.stderr

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM prompts WHERE prompt_type = 'editorial_guard'")
    alembic("downgrade", _020)
    assert "editorial_guard" not in _ddl(db, "prompts")
    # Down then up again restores both the CHECK and the partial index.
    alembic("upgrade", "head")
    assert "editorial_guard" in _ddl(db, "prompts")
    assert "is_active = 1" in _ddl(db, "idx_active_prompt")


# --- migration schema == ORM schema --------------------------------------


def test_alembic_head_and_create_all_agree(alembic_db, tmp_path: Path) -> None:
    """Tests build their DB with ``create_all``; prod is built by alembic. If
    those drift, the whole suite is asserting against a schema prod does not
    have."""
    db, alembic = alembic_db
    alembic("upgrade", "head")

    orm_path = tmp_path / "orm.db"
    engine = sa.create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    for table in (
        *_V3_TABLES,
        "sources",
        "runs",
        "draft_approvals",
        "pipeline_config",
        "prompts",
    ):
        migration_cols = _columns(db, table)
        orm_cols = _columns(orm_path, table)
        assert migration_cols == orm_cols, (
            f"{table}: alembic-only={sorted(migration_cols - orm_cols)} "
            f"orm-only={sorted(orm_cols - migration_cols)}"
        )
