"""Re-sync active writer_draft/writer_polish prompt rows to the live code.

Revision ID: 009_resync_writer_prompts
Revises: 008_writer_translate
Create Date: 2026-06-22

IT_PROJ_NTS_067 — make the prompts table the source of truth for generation.

Before: generation read the hardcoded constants in ``comment_writer``; the
``prompts`` rows the admin-UI edits were a drifted mirror (writer_draft DB
1348 B vs code 1763 B; writer_polish 1133 vs 1473), so UI edits never reached
the pipeline. NTS_067 flips the generation path to read the brand's ACTIVE
``prompts`` row (with the constant as a safe fallback). For that to use the
NEW anti-generic prompt text — and so the active row carries the new
``{banned_phrases}`` / ``{voice_principles}`` placeholders the generator
requires — this migration overwrites each brand's active writer_draft /
writer_polish row content with the current code constant.

Idempotent: a row already equal to the constant is skipped, so re-running (or
a fresh DB seeded from the same constants) is a no-op. Non-destructive: no
schema change; only the active row's ``content`` / ``version_name`` are
updated in place (the partial-unique "one active per (brand,type)" index is
untouched — no active flip).

``downgrade`` is intentionally a no-op: the superseded text was the stale,
watery prompt; there is no value in restoring it, and nothing schema-level to
revert. Verified to run cleanly in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "009_resync_writer_prompts"
down_revision: str | None = "008_writer_translate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v1.2 — anti-generic + DB source of truth (NTS_067)"
_NOTES = (
    "Re-synced to comment_writer constant. Generation now reads this active "
    "row (NTS_067); edits here affect the next run. Keep the {placeholders}."
)


def upgrade() -> None:
    # Import the live templates at apply-time (same pattern as 008's seed).
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
    # superseded stale prompt text). No schema change → no-op.
    pass
