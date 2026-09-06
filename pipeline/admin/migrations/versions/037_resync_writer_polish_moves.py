"""Re-seed ``writer_polish``: move 5 must survive the pass (NTS_129 P2 Ф2).

Revision ID: 037_resync_writer_polish_moves
Revises: 036_channels_for_uncovered_seeds
Create Date: 2026-09-06

Found by the Ф3 proof run, not by reading. The planner emitted move 5 —

    "What specific measures will be implemented to ensure compliance from
     third-country financial institutions?"
    "How will the doubling of fines impact the behavior of global banks and
     payment providers?"

— the plan rendered it to the writer under "WHAT WE DON'T KNOW", and it was
absent from the published draft. The stage that removed it is this one, and its
own SPECIFICITY rule says why:

    Cut or rewrite any sentence that could be pasted into an article on a
    different topic (vague intensifiers, generic risk/urgency statements).

A named gap reads exactly like that to a model applying the rule literally, and
is its precise opposite: "the guidance on valuation dates has not been issued"
is a statement about *this* document and nothing else. So v2.2 does three
things:

* names the five moves the draft was built from and requires all five back;
* exempts move 5 from the SPECIFICITY cut, in the same block that states it,
  because a rule and its exception are read together or not at all;
* forbids deleting a threshold, a deadline or a date to tighten prose — moves
  3 and 4, and the reason the reader opened the piece.

No placeholder change: v2.2 renders exactly the set v2.1 did, so no row can be
knocked onto the in-code fallback (NTS_071 §2). Same shape as 031, which
re-seeded this prompt for the length target.

An operator's own edited prompt is left alone, and a row whose content is
already correct but whose label is stale is relabelled — the two cases
migration 035 documents.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "037_resync_writer_polish_moves"
down_revision: str | None = "036_channels_for_uncovered_seeds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v2.2 — the five moves survive the polish, move 5 above all"
_NOTES = (
    "NTS_129 P2 Ф2, found by the Ф3 proof run: the planner produced 'what we "
    "don't know', the writer was handed it, and this pass removed it — its "
    "SPECIFICITY rule cuts sentences that 'could be pasted into an article on "
    "a different topic', and a named gap looks like one while being its exact "
    "opposite. v2.2 lists the five moves, exempts move 5 from that cut in the "
    "same block that states it, and forbids deleting a threshold or a deadline "
    "to tighten prose.\n\n"
    "PLACEHOLDERS — all of these must survive an edit: {ai_tells} "
    "{banned_phrases} {good_examples} {voice_principles} {topics_relevant} "
    "{draft_json} {language_name} {depth_guidance}. A prompt missing any of "
    "them is rejected in favour of the shipped constant, and only a log line "
    "says so."
)

# The v2.1 text this replaces. A row that does not carry this has been edited
# by an operator and is left alone.
_PREVIOUS_MARKER = "Rewrite this draft to sound more natural and less AI-generated"


def upgrade() -> None:
    from pipeline.generator.comment_writer import _POLISH_PROMPT

    bind = op.get_bind()
    if "prompts" not in set(sa.inspect(bind).get_table_names()):
        return
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    rows = bind.execute(
        sa.text(
            "SELECT id, content, version_name FROM prompts "
            "WHERE prompt_type = 'writer_polish' AND is_active = 1"
        )
    ).fetchall()
    for row_id, current, version_name in rows:
        already_current = current == _POLISH_PROMPT
        if already_current and version_name == _VERSION_NAME:
            continue
        if not already_current and _PREVIOUS_MARKER not in (current or ""):
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
