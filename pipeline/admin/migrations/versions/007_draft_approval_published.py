"""draft_approvals: add published_at + sanity_published_id columns.

Revision ID: 007_draft_approval_published
Revises: 006_multilingual
Create Date: 2026-05-25

IT_PROJ_NTS_051 — Task 3 ("Approve in admin publishes to Sanity").

Before: ``approve_draft`` only wrote a row to ``draft_approvals`` —
Andriy then had to open Sanity Studio and click Publish manually. The
two-step flow made batch approvals (S6.9) feel half-finished. The
admin route now publishes via Sanity's mutate API immediately on
approve; the new columns record the resulting publish so an operator
can tell "approved but publish-pending" from "approved and live".

Two additive columns, both nullable so existing approval rows survive
unchanged:

* ``published_at``           TIMESTAMP, NULL.
                              Set when the route's mutate call returns
                              200. NULL means either pending or the
                              row predates this migration.
* ``sanity_published_id``    VARCHAR(128), NULL.
                              The post-publish doc id (i.e.
                              ``post-XXXX``, no ``drafts.`` prefix).

SQLite-safe via batch_alter_table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "007_draft_approval_published"
down_revision: str | None = "006_multilingual"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("draft_approvals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("published_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "sanity_published_id",
                sa.String(length=128),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("draft_approvals", schema=None) as batch_op:
        batch_op.drop_column("sanity_published_id")
        batch_op.drop_column("published_at")
