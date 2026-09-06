"""Deny patterns taken from what the funnel actually carried (NTS_129 P2 Ф1.5).

Revision ID: 033_prefilter_from_audit
Revises: 032_source_overhaul
Create Date: 2026-09-06

Migration 032 switched off the general wires. What is left on the ``news`` side
is private-wealth and private-equity trade press, and reading its rejects from
28.08–06.09 shows the same four shapes over and over:

    Edmond de Rothschild names new global CIO and equities co-head
    Citizens Snags Northern Trust Duo in Florida
    Shook CEO Tells Advisors Rankings Will Resume in 2027
    Market Brief: AI Spending Is Becoming a Pillar of the Global Economy
    PE-backed Authentic Brands acquires IP of Drake's lifestyle brand OVO
    Women in PE 2020: where are they now?

Personnel moves, rankings and milestones, macro digests, and small-cap
deal-flow with no consequence for anyone Icon writes for. Each of these
currently costs a guard call to reject — cheap individually, and the reason the
funnel reads 288 → 3.

These patterns apply to ``news`` sources only: the prefilter has not touched
``primary_feed`` / ``primary_site`` since the hotfix of 2026-08-28, because a
regulator may perfectly well "appoint" a board. So an over-eager pattern here
costs trade-press items, never a directive.

**Operator edits are preserved.** The list is *extended*, not replaced: any
pattern already present is left alone and the new ones are appended. A
migration that overwrote this key would silently discard whatever Andriy had
tuned from the Editorial screen, which is the failure NTS_071 exists to
prevent — and unlike a prompt, this value has no placeholder contract to catch
it.

Four shapes from the same sample were considered and left out, because on a
``news`` feed they cost more than they save: ``-backed`` (eats "EU-backed",
"state-backed"), ``to lead`` (eats "Malta to lead review of citizenship
rules"), ``succeeds``, and ``conference`` (a ministers' communique is reported
as one). The deny list is free to run and expensive to get wrong in the
direction of silence — a pattern that drops a real item leaves no trace in the
funnel at all, which is the one failure this pipeline cannot measure.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "033_prefilter_from_audit"
down_revision: str | None = "032_source_overhaul"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Each entry is a case-insensitive substring match on the TITLE. They are
# phrases rather than words on purpose: "ranked" already existed and is safe,
# but a bare "fund" or "deal" would eat the material this pipeline is for.
NEW_DENY_PATTERNS: tuple[str, ...] = (
    # --- personnel (the single largest reject class in the audit) ----------
    "names new",
    "appointed as",
    "senior hire",
    "co-head",
    "steps down",
    "snags",
    # --- rankings, awards, milestones -------------------------------------
    "rankings",
    "milestone",
    "where are they now",
    "best places to work",
    # --- macro digests and market commentary ------------------------------
    "market brief",
    "morning briefing",
    "weekly wrap",
    "week ahead",
    "what to watch",
    # --- events and marketing ---------------------------------------------
    "webinar",
    "podcast",
    "sponsored",
    # --- small-cap deal-flow with no consequence for a private client ------
    # ``ma`` is one of Icon's five services, so a genuine cross-border deal
    # must survive: these match the *shape* of the trade-press round-up
    # ("PE-backed X acquires Y"), not the subject.
    "pe-backed",
    "pe backs",
    "debut fund",
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT brand_id_fk, prefilter_deny_title_patterns FROM pipeline_config"
        )
    ).fetchall()
    for brand_id, raw in rows:
        try:
            current = list(json.loads(raw) if raw else [])
        except (TypeError, ValueError):
            # An unreadable value is an operator typo, and replacing it here
            # would hide it. Skip; the reader already falls back to the
            # documented default (``_json_or_default``).
            continue
        lowered = {str(p).strip().lower() for p in current}
        added = [p for p in NEW_DENY_PATTERNS if p not in lowered]
        if not added:
            continue
        bind.execute(
            sa.text(
                "UPDATE pipeline_config SET prefilter_deny_title_patterns = :v "
                "WHERE brand_id_fk = :b"
            ),
            {"v": json.dumps(current + added, ensure_ascii=False), "b": brand_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT brand_id_fk, prefilter_deny_title_patterns FROM pipeline_config"
        )
    ).fetchall()
    for brand_id, raw in rows:
        try:
            current = list(json.loads(raw) if raw else [])
        except (TypeError, ValueError):
            continue
        kept = [p for p in current if str(p).strip().lower() not in NEW_DENY_PATTERNS]
        bind.execute(
            sa.text(
                "UPDATE pipeline_config SET prefilter_deny_title_patterns = :v "
                "WHERE brand_id_fk = :b"
            ),
            {"v": json.dumps(kept, ensure_ascii=False), "b": brand_id},
        )
