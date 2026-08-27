"""Re-sync active writer_draft/writer_polish rows to the live code (NTS_092).

Revision ID: 019_resync_nts092
Revises: 018_research_budgets
Create Date: 2026-08-27

IT_PROJ_NTS_092 — research fact pack before the draft, 600-800 words.

**Why this migration is the load-bearing part of the task.** Since NTS_067,
generation reads the brand's ACTIVE ``prompts`` row and treats the in-code
constant as a fallback. ``CommentWriter._resolve_template`` accepts the DB row
only if every REQUIRED placeholder is present and no unknown one is. NTS_092
adds ``{fact_pack}`` to writer_draft's required set, so on the deploy that
ships it, every live writer_draft row instantly fails that check and
generation silently reverts to the code constant. The visible symptom is not
an error — it is that the manager's admin-UI edits stop reaching production
and nobody notices, with only ``comment_writer.db_prompt_rejected`` in the
log. That is exactly the drift migration 009 was written to close.

So this migration overwrites each active writer_draft / writer_polish row's
content with the current code constant, exactly as 009 and 011 did before it.
writer_polish is re-synced too even though its placeholder set is unchanged:
its 600-800 length and 3-5 H2 rules are what stop the polish pass compressing
the longer piece straight back to 400 words, and a stale active row would
still be *valid* — so the silent-revert safety net does not catch it. A valid
stale row is the more dangerous of the two.

``writer_translate`` is deliberately NOT touched (NTS_065 faithfulness holds
the non-EN length and H2 count).

Idempotent: a row already equal to the constant is skipped, so re-running, or
a fresh DB seeded from the same constants, is a no-op. Non-destructive: no
schema change, only ``content`` / ``version_name`` / ``notes`` updated in
place, no ``is_active`` flip, so the partial-unique "one active per
(brand, type)" index is untouched. ``downgrade`` is a no-op — the superseded
text has no value to restore, and there is nothing schema-level to revert.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "019_resync_nts092"
down_revision: str | None = "018_research_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v1.4 — research fact pack + 600-800 words (NTS_092)"
_NOTES = (
    "Re-synced to comment_writer constant (NTS_092). Generation reads this "
    "active row; edits here affect the next run. Keep the {placeholders} — "
    "writer_draft now also requires {fact_pack}, which carries the web-research "
    "facts and their URLs. Removing it drops the row back to the code "
    "constant and your edits stop reaching production."
)


def upgrade() -> None:
    # Import the live templates at apply-time (same pattern as 009 / 011).
    from pipeline.generator.comment_writer import (  # noqa: PLC0415
        _DRAFT_PROMPT,
        _POLISH_PROMPT,
    )

    conn = op.get_bind()
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    update = sa.text(
        "UPDATE prompts SET content = :c, version_name = :v, notes = :n, "
        "created_at = :ts WHERE id = :id"
    )
    for prompt_type, content in (
        ("writer_draft", _DRAFT_PROMPT),
        ("writer_polish", _POLISH_PROMPT),
    ):
        rows = conn.execute(
            sa.text(
                "SELECT id, content FROM prompts "
                "WHERE prompt_type = :pt AND is_active = 1"
            ),
            {"pt": prompt_type},
        ).fetchall()
        for row_id, current in rows:
            if current == content:
                continue  # already synced — idempotent
            conn.execute(
                update,
                {
                    "c": content,
                    "v": _VERSION_NAME,
                    "n": _NOTES,
                    "ts": now,
                    "id": row_id,
                },
            )


def downgrade() -> None:
    # Content re-sync is not meaningfully reversible (we do not restore the
    # superseded pre-fact-pack prompt text). No schema change → no-op.
    pass
