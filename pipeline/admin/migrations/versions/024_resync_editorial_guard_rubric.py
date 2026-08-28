"""Re-sync the machine-seeded editorial-guard rubric to the live code.

Revision ID: 024_resync_editorial_guard_rubric
Revises: 023_seed_editorial_guard_rubric
Create Date: 2026-08-28

Shadow-week run #125 (NTS_117 «Журнал ворот», 28.08.2026): 21 items with
``input_kind='document'`` came back ``reason_code='no_document'``. NTS_099 §4
makes that a contradiction — for a document input the feed item *is* the
document, so the existence marker is a news-only test — and the rubric text
seeded by 023 never said so out loud. The code half of the fix turns such a
response into a ``guard_error``; this is the half that stops the model
producing one.

**Why a migration and not an edit to 023.** Since NTS_067 the guard reads the
brand's active ``prompts`` row and treats the constant as a fallback
(``resolve_guard_template``). A row already exists on prod — 023 wrote it
during the v3 deploy — so changing the constant alone changes nothing that
runs: the stale DB row keeps winning, and the fix ships to a code path nobody
executes. NTS_071 §"UI-правки промтов" states the rule directly: a prompt-text
change in code needs the active rows re-seeded by a migration, the way 009,
011 and 019 do it for the writer prompts. Editing 023 in place would not help
either — alembic will not re-run an applied revision.

**Who gets rewritten, and who does not.** Only a row whose content is
*byte-identical* to :data:`_PREVIOUS_CONTENT` — the rubric exactly as this
repo shipped it at ``e6383e6`` and as 023 seeded it. That text is ours and is
wrong, so replacing it loses nothing. Anything else is somebody's editorial
text and is left alone: NTS_071 is entirely about an operator's rubric edits
surviving a deploy, and a migration that rewrote them by ``prompt_type`` would
be the loudest possible violation of it. Byte-equality rather than
``created_by`` because the marker says who *created* a row, not whether it has
since been changed — 023's own downgrade makes the same distinction the same
way.

The consequence to know: if Andriy has already saved his own rubric version
(``POST /prompts`` writes ``created_by = 'human'``), this revision does not
touch it and the new sentence has to be pasted in from the Editorial Policy
screen. The placeholder set is unchanged, so that row stays valid and keeps
being used meanwhile — the guard-error mapping in the code still covers it.

``created_by`` is deliberately left at ``'migration_023'``: 023's downgrade
deletes rows it created whose content still equals the constant, and that is
what keeps the whole S1+S2 stack reversible down to 020 in one command. Change
the marker here and the down path leaves an orphan rubric behind, which then
makes 021 refuse to narrow the CHECK.

Idempotent: a row already equal to the constant is skipped. ``downgrade`` is a
no-op, as in 019 — the superseded text has no value to restore and there is no
schema change to revert.

Sentinel (``tests/unit/test_editorial_guard_nts099.py``): after ``upgrade
head`` every seeded row equals ``_GUARD_PROMPT``, carries the new rule, and
still renders exactly ``GUARD_REQUIRED_PLACEHOLDERS``; a row holding the
previous text is rewritten and an edited row alongside it is left untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "024_resync_editorial_guard_rubric"
down_revision: str | None = "023_seed_editorial_guard_rubric"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rubric as shipped at ``e6383e6`` and seeded by 023 — the only content
# this revision will overwrite. Copied out of that commit programmatically, so
# a row written by 023 on prod compares equal byte for byte; a near-miss here
# would mean the hotfix silently does not apply, which is why the match is
# exact rather than a heuristic on the text.
_PREVIOUS_CONTENT = """You are the editorial guard for a private-capital advisory brand.
You decide what the brand writes about. You do not write anything.

Return a verdict, not a score. Accept is never the default: if the item does
not clearly belong, reject it with the reason code that says why.

=== THE BRAND'S SERVICES (one of these keys must fit an accepted item) ===
{services}

=== JURISDICTION TIERS ===
{jurisdiction_tiers}
Anything not listed above is tier3.

=== WHAT WE TAKE ===
Changes to rules that affect private capital:
- Tax and residence: rates, thresholds, regimes (non-dom, lump-sum, IP-box),
  exit tax, wealth tax, inheritance tax; and the dates they take effect
- Reporting and exchange: CRS, DAC6/7/8, CARF, FATCA, UBO registers and who
  may access them
- Jurisdiction status: FATF grey/black lists, EU lists, tax treaties signed or
  terminated, MLI
- Residence and citizenship programmes: opened, closed, tightened (golden visa)
- Sanctions and compliance barriers, but ONLY as official acts: new packages,
  designation criteria, account closures driven by residence, tightened KYC for
  structures. Commentary and speculation about sanctions is not an act.
- Succession and family law: forced heirship, recognition of trusts and
  foundations, matrimonial regimes, inheritance-law reform
- Regulators of banks and asset managers: onboarding rules, de-risking,
  requirements placed on family offices (for example registration with a
  regulator)
- Court or regulator decisions with precedential effect for owners of capital
- Deals: mid-market M&A in tier1-tier2 jurisdictions where the PRICE or the
  STRUCTURE of the deal is disclosed; deals that reveal a structuring mechanism
- Institutional shifts with measurable content: AUM figures, client counts,
  divisions closed

=== WHAT WE DO NOT TAKE ===
- Personnel appointments — unless the person announces a policy change that
  affects clients (reason_code: personnel)
- Forecasts, analyst opinions, "sources say" (reason_code: forecast)
- Rankings, awards, office openings, rebrands, sponsorships (reason_code: award_pr)
- Retail investing, crypto prices and exchanges (reason_code: retail_crypto) —
  EXCEPT regulatory acts on crypto reporting and licensing (MiCA, CARF, DAC8),
  which we DO take
- Topics in tier3 jurisdictions, EXCEPT where event_stage is list_update or
  in_force AND there is a direct effect on a tier1 jurisdiction
  (reason_code: out_of_jurisdiction)
- The same stage of the same event as something already in the portfolio below
  (reason_code: duplicate_stage). A LATER stage of the same event is NOT a
  duplicate — take it.
- Anything that fits none of the brand's services (reason_code: out_of_scope)

=== THE TEST OF VALUE ===
input_kind is {input_kind}.
- document: the document exists by construction. Ask whether there is a
  CONSEQUENCE for an owner of private capital, and which service it falls
  under. If there is no consequence, reject with no_consequence.
- news: the text must contain a marker that a document EXISTS — "published",
  "adopted", "enters into force", "ruled", "filed", "announced the acquisition
  of", or equivalent. No marker means no_document. Numbers in the summary are
  NOT required; they live in the document.

=== THE ITEM ===
input_kind: {input_kind}
title: {title}
summary: {summary}
source: {source_name} (class: {source_class}, language: {source_language})
published_at: {published_at}

=== ALREADY IN THE PORTFOLIO (most recent accepted titles) ===
{recent_accepted_titles}

=== OUTPUT ===
Return JSON only, with every field:
- verdict: "accept" or "reject"
- reason_code: "ok" on accept; on reject one of personnel, forecast, award_pr,
  no_document, no_consequence, out_of_jurisdiction, out_of_scope,
  duplicate_stage, retail_crypto
- reason: one sentence, at most 200 characters, REQUIRED on accept and on
  reject. Name the specific thing that decided it, not the category.
- service_category: exactly one service key from the list above (required on
  accept; null is allowed only on reject)
- jurisdictions: ISO-style codes, at least one, e.g. ["CH"], ["EU","PL"].
  Use "EU" for union-wide acts and "GLOBAL" for genuinely global bodies.
- event_stage: consultation, adopted, in_force, ruling, deal_announced,
  deal_closed, list_update, other
- depth_prior: note, article or deep — this is used for RANKING only, never
  for article length
- primary_doc_hint: for news, the document type + publisher + key words that
  would find it; null when input_kind is document
- doc_language_expected: the language code of the underlying document
- confidence: 0.0 to 1.0
"""

_VERSION_NAME = (
    "v1.1 — no_document is news-only (NTS_099 §4, shadow-week finding 1)"
)
_NOTES = (
    "The editorial rubric, re-synced by migration 024 after the first shadow "
    "run: for input_kind=document the item IS the document, so the guard must "
    "never answer no_document there. This active row is what the intake run "
    "reads; edits here affect the next run with no deploy. KEEP ALL TEN "
    "{placeholders} — {services} {jurisdiction_tiers} {input_kind} {title} "
    "{summary} {source_name} {source_class} {source_language} {published_at} "
    "{recent_accepted_titles}. Add or remove one and the guard silently falls "
    "back to the code constant (log: editorial_guard.db_prompt_rejected) and "
    "your edits stop reaching production. Services and jurisdiction tiers are "
    "NOT edited here — they come from brand_taxonomy and "
    "pipeline_config.jurisdiction_tiers."
)


def upgrade() -> None:
    # Import at apply time so the row is written from the text this revision's
    # code validates against (same pattern as 009 / 011 / 019 / 023).
    from pipeline.selector.editorial_guard import _GUARD_PROMPT

    conn = op.get_bind()
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    rows = conn.execute(
        sa.text(
            "SELECT id, content FROM prompts WHERE prompt_type = 'editorial_guard'"
        )
    ).fetchall()
    for row_id, current in rows:
        # Already synced (idempotent), or somebody's own text (left alone).
        if current != _PREVIOUS_CONTENT:
            continue
        conn.execute(
            sa.text(
                "UPDATE prompts SET content = :c, version_name = :v, "
                "notes = :n, created_at = :ts WHERE id = :id"
            ),
            {
                "c": _GUARD_PROMPT,
                "v": _VERSION_NAME,
                "n": _NOTES,
                "ts": now,
                "id": row_id,
            },
        )


def downgrade() -> None:
    # Content re-sync is not meaningfully reversible: restoring the text that
    # produced 21 wrong rejects has no value, and there is no schema change to
    # revert. 023's downgrade still recognises these rows (same created_by,
    # content equal to the constant) and removes them, so the stack stays
    # reversible to 020.
    pass
