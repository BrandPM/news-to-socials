"""Re-seed ``writer_draft``: the article is built from five moves (NTS_129 P2 Ф2).

Revision ID: 035_resync_writer_draft_moves
Revises: 034_brief_depth_band
Create Date: 2026-09-06

The live rows still hold v2.0, which asks for "an original expert commentary
on a news peg". That is a genre, not a structure: it gives the model nothing to
check completeness against, and the observed articles showed it — a lede, two
loosely-themed sections and a close, with the thresholds and the deadlines from
the fact pack left sitting in the pack.

v3.0 names the structure the material has to fill:

1. what happened — issuer, instrument, date
2. what it changes — the delta against the position before it
3. who is affected — with the threshold that decides it, not "large holders"
4. what to do — with the date that binds it
5. what we don't know — the gaps, named specifically

Each move must rest on a fact from the pack; a move that cannot be grounded
goes to move 5 rather than becoming prose. Move 5 is the part that matters
most for an expert reader and the part a generic commentary never has, and
routing ungrounded moves into it is also what keeps the anti-invention rule
from simply producing a shorter article.

The other half of the same defect: v2.0 carried FIVE separate instructions to
write shorter against ONE length target. Stopping was the only rule the model
was told twice, so it stopped — at ~300 words against a 600-900 band.
Grounding still outranks length here and that must not change; what v3.0 adds
is what running short actually *means*, which is that a move above is still
unanswered.

**No placeholder change.** v3.0 renders exactly the set v2.0 did —
``voice_profile_yaml`` ``title`` ``url`` ``summary`` ``language``
``language_name`` ``banned_phrases`` ``fact_pack`` ``plan`` ``depth_guidance``
``primary_document`` — so no row can be knocked onto the in-code fallback by
this migration (NTS_071 §2). The sentinel test asserts that; this migration is
a content re-sync and nothing else, the same shape as 019, 024 and 031.

A row an operator has edited away from v2.0 is left alone: overwriting it would
discard their work with no trace, and the version_name is what tells them a
newer canonical prompt exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "035_resync_writer_draft_moves"
down_revision: str | None = "034_brief_depth_band"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_NAME = "v3.0 — five moves: happened, changes, affected, todo, unknown"
_NOTES = (
    "NTS_129 P2 Ф2. Structure comes from the material rather than from the "
    "genre: each of the five moves — what happened, what it changes, who is "
    "affected (with thresholds), what to do (with deadlines), what we don't "
    "know — must rest on a fact from the pack, and a move that cannot be "
    "grounded goes to 'what we don't know' instead of being written as prose. "
    "Also removes two of the five redundant write-shorter instructions that "
    "produced ~300-word articles against a 600-900 band: grounding still "
    "outranks length, but running short now means an unanswered move rather "
    "than a finished piece.\n\n"
    # The placeholder manual, carried forward from 019's notes. This field is
    # what an operator reads on the Editorial screen before editing, and a
    # prompt saved without one of these silently falls back to the in-code
    # constant (NTS_071 §2) — so dropping the list would remove the only
    # warning they get.
    "PLACEHOLDERS — all of these must survive an edit: {voice_profile_yaml} "
    "{title} {url} {summary} {language} {language_name} {banned_phrases} "
    "{fact_pack} {plan} {depth_guidance} {primary_document}. {fact_pack} is "
    "the researched facts with their source URLs, {plan} is the section plan "
    "with facts already assigned, {depth_guidance} is the length target "
    "computed from how much material there is, and {primary_document} is the "
    "act or filing itself. A prompt missing any of them is rejected in favour "
    "of the shipped constant, and only a log line says so."
)

# The v2.0 text this migration replaces. Matched exactly: a row that differs
# has been edited by an operator and is left alone.
_PREVIOUS_MARKER = (
    "You write an original expert commentary from the brand below on the news "
    "peg."
)


def upgrade() -> None:
    from pipeline.generator.comment_writer import _DRAFT_PROMPT

    bind = op.get_bind()
    if "prompts" not in set(sa.inspect(bind).get_table_names()):
        return
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    rows = bind.execute(
        sa.text(
            "SELECT id, content, version_name FROM prompts "
            "WHERE prompt_type = 'writer_draft' AND is_active = 1"
        )
    ).fetchall()
    for row_id, current, version_name in rows:
        already_v3 = current == _DRAFT_PROMPT
        if already_v3 and version_name == _VERSION_NAME:
            continue
        if not already_v3 and _PREVIOUS_MARKER not in (current or ""):
            # Not the shipped v2.0 — an operator's own text. Leave it.
            continue
        # ``already_v3`` with the wrong label happens on a database that was
        # behind: migration 028 re-seeds ``writer_draft`` from the same code
        # constant, so by the time this migration runs on a fresh chain the
        # content is already right and only the version_name is stale. Skipping
        # on content alone would leave "v2.0" on the Editorial screen next to
        # v3.0 text — the operator's only signal about which prompt is live.
        bind.execute(
            sa.text(
                "UPDATE prompts SET content = :c, version_name = :v, "
                "notes = :n, created_at = :ts WHERE id = :i"
            ),
            {
                "c": _DRAFT_PROMPT,
                "v": _VERSION_NAME,
                "n": _NOTES,
                "ts": now,
                "i": row_id,
            },
        )


def downgrade() -> None:
    # A content re-sync is not meaningfully reversible; no schema change.
    pass
