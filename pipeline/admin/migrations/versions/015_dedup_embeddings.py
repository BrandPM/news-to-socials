"""dedup: topic_embeddings + dedup_log tables + pipeline_config dedup keys.

Revision ID: 015_dedup_embeddings
Revises: 014_stale_draft_days
Create Date: 2026-07-13

IT_PROJ_NTS_090 (spec NTS_079) — embedding-based news deduplication at topic
selection. Three additive changes:

* ``topic_embeddings`` — persisted source-text embeddings (float32 BLOB) +
  normalised title, brand-scoped, so dedup works across sources processed in
  separate run_pipeline invocations (NTS_074 per-source isolation) and across
  runs within the window.
* ``dedup_log`` — every skipped/yellow decision (the calibration dataset).
* ``pipeline_config`` — ``dedup_enabled`` / ``dedup_threshold`` /
  ``dedup_window_days`` so the operator tunes dedup from Settings, no deploy.

All additive + defaulted → safe on existing rows. ``downgrade`` drops them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015_dedup_embeddings"
down_revision: str | None = "014_stale_draft_days"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("topic_id", sa.String(), nullable=False),
        sa.Column(
            "brand_id_fk",
            sa.Integer(),
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column(
            "title_norm", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_topic_embeddings_topic_id", "topic_embeddings", ["topic_id"]
    )
    op.create_index(
        "ix_topic_embeddings_created_at", "topic_embeddings", ["created_at"]
    )
    op.create_index(
        "ix_topic_embeddings_brand_created",
        "topic_embeddings",
        ["brand_id_fk", "created_at"],
    )

    op.create_table(
        "dedup_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("topic_id", sa.String(), nullable=False),
        sa.Column("matched_topic_id", sa.String(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "action IN ('skipped', 'yellow')", name="ck_dedup_log_action"
        ),
        sa.CheckConstraint("level IN (1, 2)", name="ck_dedup_log_level"),
    )
    op.create_index("ix_dedup_log_run_id", "dedup_log", ["run_id"])
    op.create_index("ix_dedup_log_created_at", "dedup_log", ["created_at"])

    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "dedup_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "dedup_threshold",
                sa.Float(),
                nullable=False,
                server_default="0.85",
            )
        )
        batch_op.add_column(
            sa.Column(
                "dedup_window_days",
                sa.Integer(),
                nullable=False,
                server_default="7",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.drop_column("dedup_window_days")
        batch_op.drop_column("dedup_threshold")
        batch_op.drop_column("dedup_enabled")
    op.drop_index("ix_dedup_log_created_at", table_name="dedup_log")
    op.drop_index("ix_dedup_log_run_id", table_name="dedup_log")
    op.drop_table("dedup_log")
    op.drop_index("ix_topic_embeddings_brand_created", table_name="topic_embeddings")
    op.drop_index("ix_topic_embeddings_created_at", table_name="topic_embeddings")
    op.drop_index("ix_topic_embeddings_topic_id", table_name="topic_embeddings")
    op.drop_table("topic_embeddings")
