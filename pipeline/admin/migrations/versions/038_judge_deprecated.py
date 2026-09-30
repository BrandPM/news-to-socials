"""The judge is out of the pipeline; its columns stay, marked (NTS_131 P3 Ф1).

Revision ID: 038_judge_deprecated
Revises: 037_resync_writer_polish_moves
Create Date: 2026-09-30

NTS_091 put an LLM judge after every draft and NTS_114 S10 moved it into the
v3 production loop. Over four weeks it gated nothing — the score was advisory
by design — and the one thing it did do was make acceptance noisier: a draft
could be argued about on a 6.5 versus a 7.0 while the actual defects (short
text, uncovered claims) went unnamed. NTS_131 v2.0 removes it. The attribution
check stays and gets stricter; the judge goes.

What this migration does, and what it deliberately does not:

* **Creates ``deprecated_columns``** and writes one row per retired column and
  one ``'*'`` row for the ``draft_scores`` table. SQLite has no column
  comments, so "deprecated" has to be data somewhere, or the next person to
  open the Settings table sees two live-looking judge knobs.
* **Sets ``eval_enabled = 0``** on every config row, so a UI that still shows
  the switch shows the truth.
* **Drops nothing.** ``draft_scores`` is the record of what the judge said
  about ~90 real drafts, and ``eval_threshold`` is what those rows were
  flagged against. History is not a cost worth saving a few kilobytes on.

Downgrade removes the registry. It does not switch ``eval_enabled`` back on:
there is no judge in the code to switch on, and a flag that says "on" over
nothing is the exact state this migration exists to end.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "038_judge_deprecated"
down_revision = "037_resync_writer_polish_moves"
branch_labels = None
depends_on = None

_REASON = (
    "NTS_131 P3 Ф1: the LLM judge was removed from the pipeline. It gated "
    "nothing and made acceptance noisier; attribution checking replaces it."
)

_ROWS: tuple[tuple[str, str], ...] = (
    ("pipeline_config", "eval_enabled"),
    ("pipeline_config", "eval_threshold"),
    ("draft_scores", "*"),
)


def upgrade() -> None:
    # A database built by ``metadata.create_all`` (tests, a fresh install)
    # already has the table; only the rows and the switch are this
    # migration's to write then.
    if "deprecated_columns" not in sa.inspect(op.get_bind()).get_table_names():
        _create_registry()
    _mark()


def _create_registry() -> None:
    op.create_table(
        "deprecated_columns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.String(), nullable=False),
        sa.Column("column_name", sa.String(), nullable=False),
        sa.Column("deprecated_in", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "table_name", "column_name", name="uq_deprecated_columns"
        ),
    )


def _mark() -> None:
    bind = op.get_bind()
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    table = sa.table(
        "deprecated_columns",
        sa.column("table_name", sa.String()),
        sa.column("column_name", sa.String()),
        sa.column("deprecated_in", sa.String()),
        sa.column("reason", sa.Text()),
        sa.column("created_at", sa.DateTime()),
    )
    present = {
        (r[0], r[1])
        for r in bind.execute(
            sa.text("SELECT table_name, column_name FROM deprecated_columns")
        )
    }
    rows = [
        {
            "table_name": t,
            "column_name": c,
            "deprecated_in": revision,
            "reason": _REASON,
            "created_at": now,
        }
        for t, c in _ROWS
        if (t, c) not in present
    ]
    if rows:
        op.bulk_insert(table, rows)
    op.execute("UPDATE pipeline_config SET eval_enabled = 0")


def downgrade() -> None:
    op.drop_table("deprecated_columns")
