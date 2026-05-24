"""S6.1 — multilingual schema tests.

Covers the three S6 columns added by migration ``006_multilingual``:

* ``brands.languages``         JSON array of language codes (default ``["en"]``)
* ``topics.language``          language tag per topic row (default ``"en"``)
* ``runs.languages_completed`` JSON array, appended as fanout completes

Also verifies the relaxed UNIQUE constraint on ``topics`` so a single
``topic_id`` can have one row per language inside a run.

Migration semantics (alembic upgrade + Icon backfill) are exercised via
``test_migration_006_alembic_round_trip`` further down — it runs the
migration head-to-base-to-head on a temp SQLite DB.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pipeline.admin import db as admin_db
from pipeline.admin.models import Brand, Run, Source, Topic
from tests.unit.conftest import seed_icon_brand


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "admin.db"
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=db_path)
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
        admin_db.reset_for_tests()


@pytest.fixture
def icon_brand_id(session) -> int:
    return seed_icon_brand(session)


def _make_source(session, brand_id_fk: int) -> Source:
    src = Source(
        brand_id_fk=brand_id_fk,
        name="Test feed",
        source_type="rss",
        url="https://example.com/feed.rss",
        primary_category="wealth",
        active=True,
    )
    session.add(src)
    session.flush()
    return src


def _make_run(session, brand_id_fk: int) -> Run:
    run = Run(
        brand_id_fk=brand_id_fk,
        triggered_by="test",
        source_ids="[]",
        started_at=datetime.now(tz=timezone.utc),
        status="running",
    )
    session.add(run)
    session.flush()
    return run


# --- brands.languages -----------------------------------------------------


def test_brand_languages_defaults_to_en_only(session):
    """A new brand created without specifying languages gets ``["en"]``."""
    brand = Brand(
        slug="acme",
        name="ACME",
        language="en",
        timezone="UTC",
        status="active",
        active=True,
    )
    session.add(brand)
    session.commit()
    session.refresh(brand)
    assert json.loads(brand.languages) == ["en"]


def test_brand_languages_accepts_multi_value_json(session):
    """An explicit JSON value round-trips through the TEXT column."""
    brand = Brand(
        slug="multi",
        name="Multi-lang",
        language="en",
        timezone="UTC",
        status="active",
        active=True,
        languages='["en","ru","uk","pl"]',
    )
    session.add(brand)
    session.commit()
    assert json.loads(brand.languages) == ["en", "ru", "uk", "pl"]


# --- topics.language ------------------------------------------------------


def test_topic_language_defaults_to_en(session, icon_brand_id):
    """A topic inserted without ``language`` falls back to ``"en"`` via the
    server default (mirrors how legacy callers in ``config_client`` write rows)."""
    src = _make_source(session, icon_brand_id)
    run = _make_run(session, icon_brand_id)
    topic = Topic(
        run_id=run.id,
        topic_id="abc123",
        source_id=src.id,
        title="Hello",
        url="https://example.com/x",
        score=8,
        status="passed",
    )
    session.add(topic)
    session.commit()
    session.refresh(topic)
    assert topic.language == "en"


def test_topic_language_can_be_set_per_row(session, icon_brand_id):
    """Same run + topic_id may exist multiple times — one row per language."""
    src = _make_source(session, icon_brand_id)
    run = _make_run(session, icon_brand_id)
    for lang in ("en", "ru", "uk", "pl"):
        session.add(
            Topic(
                run_id=run.id,
                topic_id="dup",
                source_id=src.id,
                title="Hello",
                url="https://example.com/x",
                score=8,
                status="passed",
                language=lang,
            )
        )
    session.commit()
    rows = session.execute(
        select(Topic).where(Topic.run_id == run.id, Topic.topic_id == "dup")
    ).scalars().all()
    assert {r.language for r in rows} == {"en", "ru", "uk", "pl"}


def test_topic_unique_run_topic_language_constraint(session, icon_brand_id):
    """Inserting two rows with the same (run_id, topic_id, language) fails."""
    src = _make_source(session, icon_brand_id)
    run = _make_run(session, icon_brand_id)
    session.add(
        Topic(
            run_id=run.id,
            topic_id="dup",
            source_id=src.id,
            title="Hello",
            url="https://example.com/x",
            score=8,
            status="passed",
            language="en",
        )
    )
    session.commit()
    session.add(
        Topic(
            run_id=run.id,
            topic_id="dup",
            source_id=src.id,
            title="Hello again",
            url="https://example.com/x",
            score=8,
            status="passed",
            language="en",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --- runs.languages_completed --------------------------------------------


def test_run_languages_completed_defaults_to_empty_array(session, icon_brand_id):
    """A new run starts with ``"[]"``; fanout appends as each branch finishes."""
    run = _make_run(session, icon_brand_id)
    session.commit()
    session.refresh(run)
    assert json.loads(run.languages_completed) == []


def test_run_languages_completed_round_trips_a_list(session, icon_brand_id):
    """The application stores a JSON-serialised list; readers can decode it."""
    run = _make_run(session, icon_brand_id)
    run.languages_completed = json.dumps(["en", "ru"])
    session.commit()
    session.refresh(run)
    assert json.loads(run.languages_completed) == ["en", "ru"]


# --- migration head + Icon backfill --------------------------------------


def test_migration_006_alembic_round_trip(tmp_path: Path):
    """Run alembic upgrade head → downgrade base → upgrade head, then assert
    the Icon brand's languages backfill applied. We shell out so the
    test exercises the real migration runner rather than a hand-rolled
    schema snapshot."""
    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-test.db"
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(test_db),
    }
    # Newer alembic.ini in this repo uses ADMIN_DB_PATH to compose the URL.
    # Forward the rest of the env so HOME / TMPDIR / SHELL still resolve.
    import os as _os

    env = {**_os.environ, **env}

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
    # Migration 002 already seeds Icon (and the rest of the brand roster);
    # migration 006 then runs ``UPDATE brands SET languages = …`` against
    # it. We assert the resulting state.
    import sqlite3

    with sqlite3.connect(test_db) as conn:
        row = conn.execute(
            "SELECT languages FROM brands WHERE slug = 'icon'"
        ).fetchone()
        assert row is not None, "Icon brand should be seeded by migration 002"
        assert json.loads(row[0]) == ["en", "ru", "uk", "pl"]

        # Non-Icon brands keep the EN-only default.
        for slug in ("neovox", "creolix", "vilatrix", "nexora"):
            other = conn.execute(
                "SELECT languages FROM brands WHERE slug = ?", (slug,)
            ).fetchone()
            if other is not None:
                assert json.loads(other[0]) == ["en"]

        # New tables/columns exist
        topic_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        run_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        brand_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(brands)").fetchall()
        }
        assert "language" in topic_cols
        assert "languages_completed" in run_cols
        assert "languages" in brand_cols

    # Downgrade unwinds everything
    alembic("downgrade", "004_draft_approvals")
    with sqlite3.connect(test_db) as conn:
        topic_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        assert "language" not in topic_cols

    # And upgrade reapplies clean
    alembic("upgrade", "head")
    with sqlite3.connect(test_db) as conn:
        topic_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        assert "language" in topic_cols
