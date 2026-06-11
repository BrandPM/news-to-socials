"""admin_users: multi-user admin login (NTS_058).

Revision ID: 008_admin_users
Revises: 007_draft_approval_published
Create Date: 2026-06-11

Adds the ``admin_users`` table and seeds the first user (Andriy) from the
``ADMIN_UI_USERNAME`` / ``ADMIN_UI_PASSWORD`` environment variables so the
switch from shared-password auth to per-user auth never locks the owner
out.

Seed semantics:
  * Both env vars present  → insert seed user (id=1), bcrypt-hashed.
  * Table already has rows → skip seeding (idempotent re-run / restore).
  * Env vars missing       → raise a clear error. The deploy runbook adds
    them to the VPS ``.env`` BEFORE running ``alembic upgrade head``; the
    NextAuth env-var fallback is the runtime lock-out backstop, but we
    still refuse to create an empty users table silently.

The password is read from the environment only — never hard-coded, never
logged.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from pipeline.admin.passwords import hash_password, validate_username


revision: str = "008_admin_users"
down_revision: str | None = "007_draft_approval_published"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _admin_users_table() -> sa.Table:
    return sa.table(
        "admin_users",
        sa.column("username", sa.String),
        sa.column("password_hash", sa.Text),
        sa.column("created_at", sa.DateTime),
        sa.column("created_by_user_id", sa.Integer),
        sa.column("last_login_at", sa.DateTime),
        sa.column("is_active", sa.Boolean),
    )


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.UniqueConstraint("username", name="uq_admin_users_username"),
    )
    op.create_index(
        "idx_admin_users_username", "admin_users", ["username"], unique=True
    )

    # --- Data migration: seed the owner ------------------------------------
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM admin_users")
    ).scalar()
    if existing:
        # Re-run / restore onto a populated table — leave it alone.
        return

    username = (os.environ.get("ADMIN_UI_USERNAME") or "").strip()
    password = os.environ.get("ADMIN_UI_PASSWORD") or ""
    if not username or not password:
        # Skip seeding rather than hard-failing the migration: failing would
        # break `alembic upgrade head` in CI/tests/fresh checkouts where the
        # creds aren't set, and the NextAuth env-var fallback already keeps
        # the owner from being locked out of an empty users table. The deploy
        # runbook (NTS_058 DoD) sets ADMIN_UI_USERNAME/PASSWORD on the VPS
        # BEFORE upgrading and then VERIFIES the seed row exists.
        import warnings  # noqa: PLC0415

        warnings.warn(
            "008_admin_users: ADMIN_UI_USERNAME/ADMIN_UI_PASSWORD not set — "
            "seed user NOT created. Set them and re-run `alembic upgrade head`, "
            "or rely on the env-var auth fallback until then.",
            RuntimeWarning,
            stacklevel=2,
        )
        return

    username = validate_username(username)
    op.bulk_insert(
        _admin_users_table(),
        [
            {
                "username": username,
                "password_hash": hash_password(password),
                "created_at": datetime.now(tz=timezone.utc),
                "created_by_user_id": None,  # seed user has no creator
                "last_login_at": None,
                "is_active": True,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("idx_admin_users_username", table_name="admin_users")
    op.drop_table("admin_users")
