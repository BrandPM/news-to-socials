"""prompts: admit ``editorial_guard`` into the prompt_type CHECK (NTS_099 §6).

Revision ID: 021_editorial_guard_prompt_type
Revises: 020_v3_portfolio_core
Create Date: 2026-08-28

The guard rubric is a prompt, so it lives in ``prompts`` and is edited from the
Editorial Policy screen like every other prompt — not in a constant somebody
has to deploy to change. ``prompts.prompt_type`` carries a CHECK listing five
values and **SQLite cannot ALTER a CHECK**, so admitting a sixth means
rebuilding the table. That is what this migration is: a rebuild, and nothing
else.

Two things the rebuild must not lose, both asserted by test:

* ``idx_active_prompt`` — the *partial* UNIQUE index
  ``(brand_id_fk, prompt_type) WHERE is_active = 1`` that enforces "one active
  prompt per type per brand". Partial indexes are the classic casualty of a
  SQLite table rebuild; it is dropped and recreated by hand here rather than
  left to reflection.
* Every existing row. The rebuild copies data; ``writer_draft`` /
  ``writer_polish`` rows re-seeded by 009/011/019 must come out the other side
  byte-identical, because they are what the live generator reads (NTS_067).

**No row is seeded here.** This migration widens the constraint and stops.
Seeding the rubric itself is S2's job (NTS_114 §S2) and it arrives with its own
placeholder set — ``{services}`` ``{jurisdiction_tiers}`` ``{input_kind}``
``{title}`` ``{summary}`` ``{source_name}`` ``{source_class}``
``{source_language}`` ``{published_at}`` ``{recent_accepted_titles}`` — which
per NTS_071 §2 needs its own re-seed migration with a sentinel check. Widening
the CHECK first keeps that S2 migration to one concern.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021_editorial_guard_prompt_type"
down_revision: str | None = "020_v3_portfolio_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_prompts_prompt_type"
_PARTIAL_INDEX = "idx_active_prompt"

_TYPES_BEFORE = (
    "writer_polish",
    "writer_draft",
    "topic_picker",
    "image_prompt",
    "writer_translate",
)
_TYPES_AFTER = (*_TYPES_BEFORE, "editorial_guard")


def _check(values: Sequence[str]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"prompt_type IN ({joined})"


def _accepts(value: str) -> bool:
    """True when the live CHECK already admits ``value``.

    Read from sqlite_master rather than tracked in a flag column: this is what
    makes the migration re-runnable after a half-applied deploy.
    """
    bind = op.get_bind()
    ddl = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='prompts'")
    ).scalar()
    return bool(ddl) and f"'{value}'" in str(ddl)


def _rebuild(values: Sequence[str]) -> None:
    # Drop the partial index first: batch mode reflects plain indexes but the
    # ``WHERE is_active = 1`` clause does not survive the round trip, and a
    # UNIQUE index silently downgraded to non-partial would let a second
    # active prompt per type exist.
    op.drop_index(_PARTIAL_INDEX, table_name="prompts")
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check(values))
    op.create_index(
        _PARTIAL_INDEX,
        "prompts",
        ["brand_id_fk", "prompt_type"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )


def upgrade() -> None:
    if _accepts("editorial_guard"):
        return
    _rebuild(_TYPES_AFTER)


def downgrade() -> None:
    if not _accepts("editorial_guard"):
        return
    # Rows of the type being removed would violate the narrowed CHECK on the
    # copy-back. Refuse loudly rather than rebuild into a table the data
    # cannot satisfy — the operator decides what happens to a live rubric.
    remaining = op.get_bind().execute(
        sa.text("SELECT count(*) FROM prompts WHERE prompt_type = 'editorial_guard'")
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"cannot downgrade 021: {remaining} editorial_guard prompt row(s) "
            "exist. Delete or retype them first — narrowing the CHECK would "
            "either drop them silently or fail mid-rebuild."
        )
    _rebuild(_TYPES_BEFORE)
