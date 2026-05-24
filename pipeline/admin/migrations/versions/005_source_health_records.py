"""source_health_records — per-source fetch outcome log (S5 Step 6).

Revision ID: 005_source_health
Revises: 003_cost_records
Create Date: 2026-05-24

Records one row each time a source is fetched. Powers the sparkline on
the /sources table and the source-detail health view. Brand-scoped so
multi-brand admins see only their own sources' history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "005_source_health"
down_revision: str | None = "003_cost_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_health_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("brand_id_fk", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("articles_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_msg", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["brand_id_fk"], ["brands.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("source_health_records", schema=None) as batch_op:
        batch_op.create_index(
            "ix_health_source_fetched",
            ["source_id", "fetched_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_health_brand_fetched",
            ["brand_id_fk", "fetched_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("source_health_records", schema=None) as batch_op:
        batch_op.drop_index("ix_health_brand_fetched")
        batch_op.drop_index("ix_health_source_fetched")
    op.drop_table("source_health_records")
