"""Seed the editorial-guard rubric as an active ``prompts`` row (NTS_099 §6).

Revision ID: 023_seed_editorial_guard_rubric
Revises: 022_intake_flags_and_primary_feeds
Create Date: 2026-08-28

Migration 021 admitted ``editorial_guard`` into the ``prompt_type`` CHECK and
deliberately seeded nothing. This is the seed, and per NTS_071 §2 it is a
migration rather than a fixture because the rubric arrives with a **placeholder
set**: ``{services}`` ``{jurisdiction_tiers}`` ``{input_kind}`` ``{title}``
``{summary}`` ``{source_name}`` ``{source_class}`` ``{source_language}``
``{published_at}`` ``{recent_accepted_titles}``.

Why that makes a migration load-bearing rather than convenient: the guard
resolves its template the way NTS_067 taught the writers to
(``resolve_guard_template``) — the DB row is used only when its placeholder set
is *exactly* the expected one, otherwise the code constant runs and the
operator's edits stop reaching production with nothing but a log line to show
it. A rubric row that this repo's code cannot render is therefore worse than no
row at all, and the only way to guarantee the shipped row matches the shipped
code is to write it from the constant at apply time. That is what 009/011/019
do for the writer prompts; this does it for the rubric.

The row lands **active**, and for every brand rather than only for Icon: an
inactive rubric is indistinguishable at runtime from no rubric (both resolve to
the constant, and the Editorial Policy screen would show an unused draft), and a
brand activated later must not need a second migration to acquire one.

Insert-when-absent: a brand that already has any ``editorial_guard`` row is
skipped entirely, edits included. ``downgrade`` deletes only the rows this
migration created (``created_by = 'migration_023'``) whose content is still
byte-identical to the constant — an edited rubric is the operator's text and is
left in place, which then makes 021's downgrade refuse, exactly as 021 intends.

Sentinel (``tests/unit/test_editorial_guard_nts099.py``): the seeded row's
placeholder set equals ``GUARD_REQUIRED_PLACEHOLDERS``, and the resolver
returns it with source ``"db"`` rather than falling back — which is the whole
point of writing the row from the same constant the resolver validates against.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "023_seed_editorial_guard_rubric"
down_revision: str | None = "022_intake_flags_and_primary_feeds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v1.0 — editorial guard rubric (NTS_099 §4 + NTS_115 art. 3)"
_CREATED_BY = "migration_023"
_NOTES = (
    "The editorial rubric. This active row is what the intake run reads; edits "
    "here affect the next run with no deploy. KEEP ALL TEN {placeholders} — "
    "{services} {jurisdiction_tiers} {input_kind} {title} {summary} "
    "{source_name} {source_class} {source_language} {published_at} "
    "{recent_accepted_titles}. Add or remove one and the guard silently falls "
    "back to the code constant (log: editorial_guard.db_prompt_rejected) and "
    "your edits stop reaching production. Services and jurisdiction tiers are "
    "NOT edited here — they come from brand_taxonomy and "
    "pipeline_config.jurisdiction_tiers."
)


def upgrade() -> None:
    # Import at apply time so the seeded text is always the text this revision's
    # code validates against (same pattern as 009 / 011 / 019).
    from pipeline.selector.editorial_guard import _GUARD_PROMPT

    conn = op.get_bind()
    now = datetime.now(tz=UTC).replace(tzinfo=None)

    # Every brand, not only the ones with a config row: the rubric is what the
    # guard resolves per brand, and a brand that gets activated later must not
    # need a second migration to acquire one. Onboarding a brand (NTS_109) is
    # rows, not deploys — including this row.
    brand_ids = [
        r[0] for r in conn.execute(sa.text("SELECT id FROM brands ORDER BY id"))
    ]
    for brand_id in brand_ids:
        existing = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM prompts "
                "WHERE brand_id_fk = :b AND prompt_type = 'editorial_guard'"
            ),
            {"b": brand_id},
        ).scalar()
        if existing:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO prompts (brand_id_fk, prompt_type, version_name, "
                "content, notes, is_active, created_by, created_at) "
                "VALUES (:b, 'editorial_guard', :v, :c, :n, 1, :by, :ts)"
            ),
            {
                "b": brand_id,
                "v": _VERSION_NAME,
                "c": _GUARD_PROMPT,
                "n": _NOTES,
                "by": _CREATED_BY,
                "ts": now,
            },
        )


def downgrade() -> None:
    from pipeline.selector.editorial_guard import _GUARD_PROMPT

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM prompts WHERE prompt_type = 'editorial_guard' "
            "AND created_by = :by AND content = :c"
        ),
        {"by": _CREATED_BY, "c": _GUARD_PROMPT},
    )
