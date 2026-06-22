"""Re-sync active writer_draft/writer_polish rows to the live code (NTS_070).

Revision ID: 011_resync_nts070
Revises: 010_run_progress
Create Date: 2026-06-22

IT_PROJ_NTS_070 — manager-feedback quality merge. The draft/polish code
templates gained NO-REPETITION / AUDIENCE-LINK / NO-INVENTION blocks and a
"so what specifically" close clause, and writer_polish gained a new
``{topics_relevant}`` placeholder. Generation reads the brand's ACTIVE
``prompts`` row (NTS_067), so the active rows must carry the new templates —
otherwise the stale polish row (missing ``{topics_relevant}``) fails
placeholder validation and silently falls back to the in-code constant.

Same mechanism as migration 009: overwrite each active writer_draft /
writer_polish row's content with the current code constant. Idempotent (skip
if already equal). Non-destructive: no schema change; only ``content`` /
``version_name`` updated in place. ``downgrade`` is a no-op (the superseded
text has no value to restore). Verified to run cleanly both directions.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "011_resync_nts070"
down_revision: str | None = "010_run_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v1.3 — manager quality merge (NTS_070)"
_NOTES = (
    "Re-synced to comment_writer constant (NTS_070). Generation reads this "
    "active row; edits here affect the next run. Keep the {placeholders} — "
    "writer_polish now also requires {topics_relevant}."
)


def upgrade() -> None:
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
    # Content re-sync is not meaningfully reversible. No schema change → no-op.
    pass
