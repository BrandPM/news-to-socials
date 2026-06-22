"""runs: add a light ``progress`` JSON column for live run status.

Revision ID: 010_run_progress
Revises: 009_resync_writer_prompts
Create Date: 2026-06-22

IT_PROJ_NTS_068 — global run-status indicator. The admin needs live "X of N
sources · stage" while a "Run all sources" pass is in flight. The run row
already has status / started_at / finished_at / stats, but no live
granularity (stats is only written at finish, and on a multi-source run-all
it reflects the last source). This adds ONE additive column:

* ``progress``  JSON-as-TEXT, default ``'{}'``. The orchestrator
  (``run_pipeline_for_run``) writes ``{sources_total, sources_done,
  current_source, drafts, errors, stage}`` as it walks the source list.
  Best-effort — never load-bearing for the pipeline itself.

Additive + nullable-safe via a server default, so existing rows read ``'{}'``.
SQLite-safe through ``batch_alter_table``. ``downgrade`` drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_run_progress"
down_revision: str | None = "009_resync_writer_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "progress",
                sa.Text(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("progress")
