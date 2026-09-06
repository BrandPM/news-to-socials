"""Re-sync ``writer_polish``: the polish pass gets the computed length target.

Revision ID: 031_resync_polish_depth
Revises: 030_cover_mode
Create Date: 2026-09-06

Found on the first real production run (the S10 e2e proof). S6 gave the drafter
a length target computed from the material (``{depth_guidance}``) but left the
polish prompt enforcing the literal "600-800 words" NTS_092 wrote into it. The
two stages could therefore disagree: a ``note`` drafted to 300-450 was polished
against 600-800, and a ``deep`` piece with no ceiling was polished against one.

That is exactly the failure NTS_102 opens with — "число в промпте не знает, о
чём статья" — surviving in the one stage S6 did not touch.

``writer_polish`` gains ``{depth_guidance}``, which changes its placeholder set,
which means a live row silently fails ``_resolve_template`` and generation
reverts to the code constant. So the active rows are re-synced, exactly as 009,
011, 019 and 028 did before it. Idempotent; a row already equal is skipped.
``downgrade`` is a no-op: the superseded text has no value to restore.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "031_resync_polish_depth"
down_revision: str | None = "030_cover_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v2.1 — the polish pass reads the computed length target"
_NOTES = (
    "Re-synced to the comment_writer constant. The length target is no longer "
    "written here: it arrives as {depth_guidance}, computed from the fact "
    "pack (NTS_102 v2 §1). Keep the placeholder — without it this row is "
    "rejected and your edits stop reaching production."
)


def upgrade() -> None:
    from pipeline.generator.comment_writer import _POLISH_PROMPT

    bind = op.get_bind()
    if "prompts" not in set(sa.inspect(bind).get_table_names()):
        return
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    rows = bind.execute(
        sa.text(
            "SELECT id, content FROM prompts "
            "WHERE prompt_type = 'writer_polish' AND is_active = 1"
        )
    ).fetchall()
    for row_id, current in rows:
        if current == _POLISH_PROMPT:
            continue
        bind.execute(
            sa.text(
                "UPDATE prompts SET content = :c, version_name = :v, "
                "notes = :n, created_at = :ts WHERE id = :i"
            ),
            {
                "c": _POLISH_PROMPT,
                "v": _VERSION_NAME,
                "n": _NOTES,
                "ts": now,
                "i": row_id,
            },
        )


def downgrade() -> None:
    # A content re-sync is not meaningfully reversible; no schema change.
    pass
