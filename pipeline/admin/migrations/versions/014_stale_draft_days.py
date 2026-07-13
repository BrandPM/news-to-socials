"""pipeline_config: add ``stale_draft_days`` (default 3).

Revision ID: 014_stale_draft_days
Revises: 013_run_pid_cancel
Create Date: 2026-07-13

IT_PROJ_NTS_089 (spec NTS_084) — publication-date control. A pending draft
whose displayed publication date is older than N days is flagged ⚠️ in the
Content Hub so the manager can decide "publish with the old date or reject".
N is per-brand and editable from Settings without a deploy, so it lives on
``pipeline_config`` alongside the other tunables.

Additive + safe: ``server_default='3'`` backfills every existing row (one per
brand) with 3 during the ALTER, so no row is left NULL and the app default
matches. ``downgrade`` drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014_stale_draft_days"
down_revision: str | None = "013_run_pid_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "stale_draft_days",
                sa.Integer(),
                nullable=False,
                server_default="3",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.drop_column("stale_draft_days")
