"""draft_approvals — approve/reject decisions per Sanity draft (S5 Step 7).

Revision ID: 004_draft_approvals
Revises: 005_source_health
Create Date: 2026-05-24

Chains after ``005_source_health`` because revision 004 was reserved
during S5 Step 5 but minted afterwards. Alembic uses ``down_revision``
pointers, not numeric ordering — the chain remains valid.

One row per draft per brand. ``UNIQUE (sanity_draft_id, brand_id_fk)``
keeps a draft to a single live decision row that the route handler
upserts. ``status`` is constrained via CHECK so a typo in the route
fails at the DB layer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "004_draft_approvals"
down_revision: str | None = "005_source_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sanity_draft_id", sa.String(), nullable=False),
        sa.Column("brand_id_fk", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column(
            "decided_by", sa.String(), nullable=False, server_default="admin"
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'rejected')",
            name="ck_draft_approvals_status",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id_fk"], ["brands.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sanity_draft_id",
            "brand_id_fk",
            name="uq_draft_approvals_draft_brand",
        ),
    )
    with op.batch_alter_table("draft_approvals", schema=None) as batch_op:
        batch_op.create_index(
            "ix_draft_approvals_sanity_id",
            ["sanity_draft_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_draft_approvals_brand_status",
            ["brand_id_fk", "status"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("draft_approvals", schema=None) as batch_op:
        batch_op.drop_index("ix_draft_approvals_brand_status")
        batch_op.drop_index("ix_draft_approvals_sanity_id")
    op.drop_table("draft_approvals")
