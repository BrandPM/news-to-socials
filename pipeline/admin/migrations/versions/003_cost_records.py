"""cost_records table — granular paid-call audit log

Revision ID: 003_cost_records
Revises: 002_brands_fk
Create Date: 2026-05-22

Per NTS_025 C1: every LLM call / image generation / paid API call
records one row in ``cost_records`` with brand_id_fk, provider,
operation, tokens, cost_usd. The indexes target the cost-dashboard
queries planned for S4 (per-brand-per-day, per-run, per-topic, per-draft).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "003_cost_records"
down_revision: str | None = "002_brands_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("brand_id_fk", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("draft_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["brand_id_fk"], ["brands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cost_records", schema=None) as batch_op:
        batch_op.create_index(
            "ix_cost_records_brand_created",
            ["brand_id_fk", "created_at"],
            unique=False,
        )
        batch_op.create_index("ix_cost_records_run_id", ["run_id"], unique=False)
        batch_op.create_index("ix_cost_records_topic_id", ["topic_id"], unique=False)
        batch_op.create_index("ix_cost_records_draft_id", ["draft_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("cost_records", schema=None) as batch_op:
        batch_op.drop_index("ix_cost_records_draft_id")
        batch_op.drop_index("ix_cost_records_topic_id")
        batch_op.drop_index("ix_cost_records_run_id")
        batch_op.drop_index("ix_cost_records_brand_created")
    op.drop_table("cost_records")
