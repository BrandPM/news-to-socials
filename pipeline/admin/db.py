"""SQLAlchemy engine and session helpers for the admin database.

A separate SQLite file (``admin.db``) sits next to the pipeline's own
``pipeline.db``. Path comes from ``settings.admin_db_path`` so tests can
override it via a tmp_path fixture without touching real data.

We use a synchronous engine here — the admin surface is low-traffic
(handful of requests/minute, single operator) and SQLite's writer lock
makes async drivers (aiosqlite) more trouble than they're worth for
this use case. The pipeline itself remains async; ``AdminConfigClient``
hops out to a sync session when it needs to read.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from pipeline.common.config import get_settings


class Base(DeclarativeBase):
    """Single declarative base for all admin tables."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_url(path: Path) -> str:
    # SQLite needs a 3-slash URL for relative paths and 4 for absolute.
    p = Path(path).expanduser()
    if p.is_absolute():
        return f"sqlite:///{p}"
    return f"sqlite:///{p}"


def get_engine(path: Path | None = None, *, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first call.

    Pass ``path`` to force a fresh engine bound to a specific DB file
    (useful for tests). Note that this also resets the session factory.
    """
    global _engine, _SessionLocal
    if path is not None or _engine is None:
        db_path = path if path is not None else get_settings().admin_db_path
        _engine = create_engine(
            _make_url(db_path),
            echo=echo,
            future=True,
            # check_same_thread=False allows FastAPI's threadpool to share
            # the engine across worker threads. SQLite is otherwise serial.
            connect_args={"check_same_thread": False},
        )
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, future=True
        )
    return _engine


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    """Foreign keys are OFF by default in SQLite; turn them on per-connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def get_session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on failure."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Drop cached engine + session factory. Tests use this to switch DBs."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
