"""runs: add ``pid`` + admit a ``cancelled`` status.

Revision ID: 013_run_pid_cancel
Revises: 012_alert_sent
Create Date: 2026-06-23

IT_PROJ_NTS_074 — take the pipeline run out of the admin-API event loop. The
run now executes as a detached OS subprocess (``python -m pipeline.run
for-run --run-id N``) instead of a FastAPI ``BackgroundTask`` sharing the API
process. Two additive schema changes support that:

* ``pid``  INTEGER, nullable. The os-level process id of the detached run
  worker, written by the spawning endpoint. Used by ``POST /runs/{id}/cancel``
  (kill by pid) and the restart orphan-sweep (a ``running`` row whose pid is
  no longer alive is force-failed). NULL for legacy in-process runs and for
  the brief window between row-insert and spawn.
* a new ``cancelled`` run status — operator-initiated stop, kept DISTINCT
  from ``failed`` so it never trips the failed-run notifications / Telegram
  push-alerter (NTS_073). The ``ck_runs_status`` CHECK is widened to admit it,
  SQLite-safe via ``batch_alter_table`` (rebuilds the table; the FK + index
  are preserved by the recreate — same pattern as migration 008).

Additive + nullable-safe: existing rows read ``pid = NULL`` and keep their
current status. ``downgrade`` narrows the CHECK back (rewriting any
``cancelled`` rows to ``failed`` first so they don't violate it) and drops the
column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013_run_pid_cancel"
down_revision: str | None = "012_alert_sent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = "'running', 'success', 'failed', 'dry_run'"
_NEW_STATUSES = _OLD_STATUSES + ", 'cancelled'"


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pid", sa.Integer(), nullable=True))
        batch_op.drop_constraint("ck_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_status",
            f"status IN ({_NEW_STATUSES})",
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Collapse cancelled → failed before narrowing the CHECK so no row
    # violates the reverted constraint.
    conn.execute(
        sa.text("UPDATE runs SET status = 'failed' WHERE status = 'cancelled'")
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_status",
            f"status IN ({_OLD_STATUSES})",
        )
        batch_op.drop_column("pid")
