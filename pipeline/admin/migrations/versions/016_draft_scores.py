"""eval: draft_scores table + pipeline_config eval keys.

Revision ID: 016_draft_scores
Revises: 015_dedup_embeddings
Create Date: 2026-07-13

IT_PROJ_NTS_091 (spec NTS_080) — LLM-as-judge auto-eval of every draft before
the manager sees it.

* ``draft_scores`` — one row per (draft, language) scoring: per-axis rubric
  JSON, weighted total, the judge model, and the judge-prompt version (scores
  compare only within a version). ``flagged`` = below the eval_threshold in
  force when scored.
* ``pipeline_config`` — ``eval_enabled`` / ``eval_threshold`` so the operator
  tunes eval from Settings, no deploy.

Additive + defaulted → safe on existing rows. ``downgrade`` drops them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_draft_scores"
down_revision: str | None = "015_dedup_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("draft_id", sa.String(), nullable=False),
        sa.Column(
            "brand_id_fk",
            sa.Integer(),
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("lang", sa.String(length=8), nullable=False),
        sa.Column("rubric_json", sa.Text(), nullable=False),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column(
            "flagged", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("judge_prompt_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_draft_scores_draft_id", "draft_scores", ["draft_id"])
    op.create_index("ix_draft_scores_created_at", "draft_scores", ["created_at"])
    op.create_index(
        "ix_draft_scores_brand_created", "draft_scores", ["brand_id_fk", "created_at"]
    )
    op.create_index(
        "ix_draft_scores_version", "draft_scores", ["judge_prompt_version"]
    )

    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "eval_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "eval_threshold",
                sa.Float(),
                nullable=False,
                server_default="7.0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        batch_op.drop_column("eval_threshold")
        batch_op.drop_column("eval_enabled")
    op.drop_index("ix_draft_scores_version", table_name="draft_scores")
    op.drop_index("ix_draft_scores_brand_created", table_name="draft_scores")
    op.drop_index("ix_draft_scores_created_at", table_name="draft_scores")
    op.drop_index("ix_draft_scores_draft_id", table_name="draft_scores")
    op.drop_table("draft_scores")
