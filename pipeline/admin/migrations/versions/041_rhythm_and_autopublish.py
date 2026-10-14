"""A steady two-a-run rhythm, a scope term, autopublish, staleness by kind.

Revision ID: 041_rhythm_and_autopublish
Revises: 040_attribution_on_gpt4o
Create Date: 2026-10-14

NTS_133 P5. Every value below changes only where the row still holds the
shipped default — NTS_071's rule, applied to config, the same way 039 did it.
An operator who already chose a number keeps it.

**1. Rhythm (Ф2.1).** Production moves to Tuesday and Thursday (the timers in
``infra/systemd``), two topics a run:

    weekly_draft_budget     6 → 4
    production_batch_size   new, 2     how many one run may take

With the weekly budget alone, Tuesday could take all six and Thursday none —
and the next week none at all if the portfolio was thin on Tuesday. Two caps
make the week 2 + 2.

**2. Scope term (Ф2.2).** ``rank_weights`` gains ``w_scope`` = 0.40 where the
JSON does not mention it. See ``pipeline.selector.scope``: a candidate that is
not about private wealth scores 0 on the term and is never auto-picked.

**3. Autopublish (Ф2.3, from NTS_132 Ф4).** ``autopublish_enabled``, OFF.

**4. Staleness by kind (Ф4).** ``stale_draft_days_document``, 60. The 3-day
``stale_draft_days`` keeps governing news.

Downgrade drops the three columns and moves the budget and the weight back
only where they still hold this migration's values.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "041_rhythm_and_autopublish"
down_revision: str | None = "040_attribution_on_gpt4o"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, sa.TextClause | str], ...] = (
    ("production_batch_size", sa.Integer(), "2"),
    ("autopublish_enabled", sa.Boolean(), sa.text("0")),
    ("stale_draft_days_document", sa.Integer(), "60"),
)

_DEFAULT_MOVES: tuple[tuple[str, int, int], ...] = (
    ("weekly_draft_budget", 6, 4),
)

_SCOPE_KEY = "w_scope"
_SCOPE_WEIGHT = 0.40


def _config_columns() -> set[str]:
    return {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("pipeline_config")
    }


def _set_scope_weight(*, add: bool) -> None:
    bind = op.get_bind()
    for brand_id, raw in bind.execute(
        sa.text("SELECT brand_id_fk, rank_weights FROM pipeline_config")
    ).fetchall():
        try:
            weights = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(weights, dict):
            continue
        if add:
            if _SCOPE_KEY in weights:
                continue
            weights[_SCOPE_KEY] = _SCOPE_WEIGHT
        else:
            if weights.get(_SCOPE_KEY) != _SCOPE_WEIGHT:
                continue
            weights.pop(_SCOPE_KEY)
        bind.execute(
            sa.text(
                "UPDATE pipeline_config SET rank_weights = :w WHERE brand_id_fk = :b"
            ),
            {"w": json.dumps(weights), "b": brand_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    have = _config_columns()
    for name, type_, default in _NEW_COLUMNS:
        if name not in have:
            op.add_column(
                "pipeline_config",
                sa.Column(name, type_, nullable=False, server_default=default),
            )
    for column, old, new in _DEFAULT_MOVES:
        bind.execute(
            sa.text(f"UPDATE pipeline_config SET {column} = :new WHERE {column} = :old"),
            {"new": new, "old": old},
        )
    _set_scope_weight(add=True)


def downgrade() -> None:
    bind = op.get_bind()
    _set_scope_weight(add=False)
    for column, old, new in _DEFAULT_MOVES:
        bind.execute(
            sa.text(f"UPDATE pipeline_config SET {column} = :old WHERE {column} = :new"),
            {"new": new, "old": old},
        )
    have = _config_columns()
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        for name, _type, _default in _NEW_COLUMNS:
            if name in have:
                batch_op.drop_column(name)
