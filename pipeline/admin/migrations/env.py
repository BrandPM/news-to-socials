"""Alembic environment for the admin database.

DB URL is derived from ``settings.admin_db_path`` so the same alembic.ini
works on every host. To override (e.g. point at a tmp DB during tests),
set the ``ADMIN_DB_PATH`` environment variable or pass
``-x sqlalchemy.url=sqlite:///…`` on the CLI.
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from pipeline.admin.db import Base
from pipeline.admin import models  # noqa: F401  (import side-effect: register tables)
from pipeline.common.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    # Precedence: CLI -x sqlalchemy.url=… > alembic.ini sqlalchemy.url > settings.
    cli_args = context.get_x_argument(as_dictionary=True)
    if "sqlalchemy.url" in cli_args:
        return cli_args["sqlalchemy.url"]
    ini_url = config.get_main_option("sqlalchemy.url") or ""
    if ini_url:
        return ini_url
    path = Path(get_settings().admin_db_path).expanduser()
    return f"sqlite:///{path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
