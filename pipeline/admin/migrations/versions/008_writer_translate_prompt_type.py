"""prompts: allow ``writer_translate`` prompt_type + seed its active version.

Revision ID: 008_writer_translate
Revises: 007_draft_approval_published
Create Date: 2026-06-22

IT_PROJ_NTS_065 — translation rework. Non-EN languages stop being native
generations from the topic and become faithful TRANSLATIONS of the
canonical English draft (same H2 set, same facts/numbers, comparable
length). The live pipeline uses the hardcoded ``_TRANSLATE_PROMPT`` in
``comment_writer`` exactly as it uses ``_DRAFT_PROMPT`` / ``_POLISH_PROMPT``;
this migration brings the prompt-versioning system (admin /prompts, /test,
/analyze) to parity by:

1. Widening the ``ck_prompts_prompt_type`` CHECK to admit
   ``'writer_translate'`` — SQLite-safe via ``batch_alter_table``, which
   rebuilds the table (the other CHECKs / indexes are preserved by the
   recreate).
2. Seeding ONE active ``writer_translate`` row for every brand that already
   runs the pipeline (detected by an active ``writer_polish`` row), mirroring
   the live hardcoded template so the admin can test/analyze/version it.
   Idempotent: skips a brand that already has a ``writer_translate`` row.

No data is destroyed and no EN behaviour changes. ``downgrade`` removes the
seeded rows first (so they don't violate the reverted CHECK) then narrows
the constraint back.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "008_writer_translate"
down_revision: str | None = "007_draft_approval_published"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_TYPES = "'writer_polish', 'writer_draft', 'topic_picker', 'image_prompt'"
_NEW_TYPES = _OLD_TYPES + ", 'writer_translate'"

_VERSION_NAME = "v1.0 — faithful EN→target translation (NTS_065)"
_NOTES = (
    "Translates the canonical EN draft into the target language. Preserves "
    "structure (H2 set), all facts/numbers, and length; voice profile applies "
    "only as phrasing localisation. Mirrors comment_writer._TRANSLATE_PROMPT."
)


def _set_check(constraint_sql: str) -> None:
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.drop_constraint("ck_prompts_prompt_type", type_="check")
        batch_op.create_check_constraint(
            "ck_prompts_prompt_type",
            f"prompt_type IN ({constraint_sql})",
        )


def upgrade() -> None:
    _set_check(_NEW_TYPES)

    # Seed an active writer_translate row for brands already running the
    # pipeline. Import the live template at apply-time so the seeded content
    # matches what the pipeline actually sends (same pattern seed_data.py uses
    # for writer_polish / writer_draft).
    from pipeline.generator.comment_writer import _TRANSLATE_PROMPT  # noqa: PLC0415

    conn = op.get_bind()
    brand_ids = conn.execute(
        sa.text(
            "SELECT DISTINCT brand_id_fk FROM prompts "
            "WHERE prompt_type = 'writer_polish' AND is_active = 1"
        )
    ).fetchall()

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    insert = sa.text(
        "INSERT INTO prompts "
        "(brand_id_fk, prompt_type, version_name, content, notes, "
        " is_active, created_by, created_at) "
        "VALUES (:b, 'writer_translate', :v, :c, :n, 1, 'system', :ts)"
    )
    for (brand_id_fk,) in brand_ids:
        already = conn.execute(
            sa.text(
                "SELECT 1 FROM prompts "
                "WHERE brand_id_fk = :b AND prompt_type = 'writer_translate' "
                "LIMIT 1"
            ),
            {"b": brand_id_fk},
        ).first()
        if already:
            continue
        conn.execute(
            insert,
            {
                "b": brand_id_fk,
                "v": _VERSION_NAME,
                "c": _TRANSLATE_PROMPT,
                "n": _NOTES,
                "ts": now,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Remove rows that would violate the narrowed CHECK before reapplying it.
    conn.execute(sa.text("DELETE FROM prompts WHERE prompt_type = 'writer_translate'"))
    _set_check(_OLD_TYPES)
