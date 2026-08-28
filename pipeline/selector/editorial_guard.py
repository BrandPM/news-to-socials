"""The editorial guard: what Icon writes about (IT_PROJ_NTS_099).

Replaces the 1-10 relevance score with an editorial verdict plus the metadata
everything downstream needs. Two inputs, because NTS_101 v2 subscribes the
pipeline directly to regulator and law-firm feeds:

* ``input_kind='document'`` — the item *is* a document announcement from a
  ``primary_feed``. The document exists by construction; the question is
  whether it has a consequence for a private-capital owner.
* ``input_kind='news'`` — a news item. The question is first whether a document
  exists at all: no existence marker in the text ("published", "adopted",
  "enters into force", "ruled", "announced the acquisition of") → ``no_document``.
  **Numbers in the summary are not required** — they live in the document.

Three properties this module is built around, each of which is a way the stage
fails silently if it is missing:

**Accept is never the default.** A response missing a required field, or
carrying an enum value outside the spec's vocabulary, is a ``guard_error``:
no candidate row is created and the item is counted in the run summary. The
alternative — coercing a malformed response into an accept, or into a reject —
either spends money on garbage or throws away a real story, and neither leaves
a trace.

**The rubric is data, not code.** ``_GUARD_PROMPT`` below is a *fallback*. The
live rubric is the brand's active ``prompts`` row of type ``editorial_guard``,
edited from the Editorial Policy screen, and it is accepted only if its
placeholder set is exactly right — the NTS_067 contract, same as the writer
prompts (see :func:`resolve_guard_template`). A rubric edit that breaks the
placeholders degrades to this constant and says so in the log, instead of
raising mid-run.

**Services and jurisdictions are per brand.** ``{services}`` renders
``brand_taxonomy`` rows and ``{jurisdiction_tiers}`` renders the config key
(NTS_099 §3, values from NTS_115 artefact 4). Neither is a constant in this
file — onboarding a second brand (NTS_109) is rows in a table, not a code
change.

Failure policy (NTS_106 §1): 429/5xx/timeout gets three attempts with backoff,
after which the item is **deferred** — not judged, no row, replayed by the next
intake. A deferred item is a counted, visible non-decision; the one thing it
must never become is an unlogged drop.
"""

from __future__ import annotations

import asyncio
import json
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# NTS_099 §2 — cheap model. Overridable per brand via ``pipeline_config.guard_model``.
DEFAULT_GUARD_MODEL = "gpt-4o-mini"

GUARD_MAX_TOKENS = 500
GUARD_TIMEOUT_S = 60.0
# NTS_106 §1 — three attempts, then defer.
GUARD_MAX_ATTEMPTS = 3
GUARD_BACKOFF_BASE_S = 2.0

REASON_MAX_CHARS = 200
# How many recently accepted titles the rubric sees (NTS_099 §2).
RECENT_ACCEPTED_LIMIT = 20

INPUT_KINDS = ("document", "news")
VERDICTS = ("accept", "reject")
REASON_CODES = (
    "ok",
    "personnel",
    "forecast",
    "award_pr",
    "no_document",
    "no_consequence",
    "out_of_jurisdiction",
    "out_of_scope",
    "duplicate_stage",
    "retail_crypto",
    "daily_cap",
    "guard_error",
)
EVENT_STAGES = (
    "consultation",
    "adopted",
    "in_force",
    "ruling",
    "deal_announced",
    "deal_closed",
    "list_update",
    "other",
)
DEPTHS = ("note", "article", "deep")

# Reason codes the model may return. ``daily_cap`` and ``guard_error`` are
# ours to assign, never the model's: the cap is arithmetic over rows the model
# cannot see, and a model that could self-report ``guard_error`` would be
# reporting the one condition that means we should not believe it.
MODEL_REASON_CODES = tuple(
    c for c in REASON_CODES if c not in ("daily_cap", "guard_error")
)


class GuardError(RuntimeError):
    """Base class for everything that can go wrong in the guard."""


class GuardSchemaError(GuardError):
    """The response violated the output contract (NTS_099 §3).

    → ``reason_code='guard_error'``, **no candidate row**, counted in the
    summary. Never coerced into a verdict.
    """


class GuardDeferred(GuardError):  # noqa: N818 — a postponement, not a failure
    """The model could not be reached after ``GUARD_MAX_ATTEMPTS`` (NTS_106 §1).

    → item not judged, no row, replayed by the next intake, counted as
    ``deferred`` in the heartbeat.
    """


# --- output contract -------------------------------------------------------


def guard_json_schema() -> dict[str, Any]:
    """Strict JSON schema for the guard's response (NTS_099 §3).

    ``strict: True`` on the OpenAI side makes the *shape* the provider's
    problem, but :func:`parse_guard_response` re-validates every field anyway.
    A schema-constrained decode is a strong prior, not a guarantee — and the
    live rubric is operator-editable text that could ask for something else
    entirely.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "reason_code",
            "reason",
            "service_category",
            "jurisdictions",
            "event_stage",
            "depth_prior",
            "primary_doc_hint",
            "doc_language_expected",
            "confidence",
        ],
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "reason_code": {"type": "string", "enum": list(MODEL_REASON_CODES)},
            "reason": {"type": "string"},
            "service_category": {"type": ["string", "null"]},
            "jurisdictions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "event_stage": {"type": "string", "enum": list(EVENT_STAGES)},
            "depth_prior": {"type": "string", "enum": list(DEPTHS)},
            "primary_doc_hint": {"type": ["string", "null"]},
            "doc_language_expected": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
        },
    }


@dataclass(frozen=True)
class GuardVerdict:
    """A validated guard response. Every field is spec-legal by construction."""

    verdict: str
    reason_code: str
    reason: str
    service_category: str | None
    jurisdictions: tuple[str, ...]
    event_stage: str
    depth_prior: str
    primary_doc_hint: str | None
    doc_language_expected: str | None
    confidence: float | None

    @property
    def accepted(self) -> bool:
        return self.verdict == "accept"


def parse_guard_response(
    payload: Any,
    *,
    input_kind: str,
    allowed_service_keys: tuple[str, ...],
) -> GuardVerdict:
    """Validate a raw response into a :class:`GuardVerdict`.

    Raises :class:`GuardSchemaError` on anything the spec does not allow: a
    missing required field, an unknown enum value, an empty jurisdiction list,
    a ``service_category`` outside the brand's taxonomy. "Default deny" here
    means *no verdict at all* — the caller creates no row.
    """
    if not isinstance(payload, dict):
        raise GuardSchemaError(f"response is {type(payload).__name__}, not an object")

    missing = [
        f
        for f in guard_json_schema()["required"]
        if f not in payload
    ]
    if missing:
        raise GuardSchemaError(f"missing required field(s): {missing}")

    verdict = payload["verdict"]
    if verdict not in VERDICTS:
        raise GuardSchemaError(f"unknown verdict: {verdict!r}")

    reason_code = payload["reason_code"]
    if reason_code not in MODEL_REASON_CODES:
        raise GuardSchemaError(f"unknown reason_code: {reason_code!r}")

    # An accept whose reason_code is a rejection reason (or vice versa) is a
    # contradiction, and the two halves would be read by different consumers:
    # the portfolio board reads the code, the editor reads the verdict.
    if verdict == "accept" and reason_code != "ok":
        raise GuardSchemaError(
            f"verdict=accept with reason_code={reason_code!r} (expected 'ok')"
        )
    if verdict == "reject" and reason_code == "ok":
        raise GuardSchemaError("verdict=reject with reason_code='ok'")

    reason = str(payload["reason"] or "").strip()
    if not reason:
        # NTS_099 §3: required "и для отказов" — the sentence Andriy reads when
        # proofreading 50 verdicts. A blank reason makes that review impossible.
        raise GuardSchemaError("reason is empty")
    reason = reason[:REASON_MAX_CHARS]

    event_stage = payload["event_stage"]
    if event_stage not in EVENT_STAGES:
        raise GuardSchemaError(f"unknown event_stage: {event_stage!r}")

    depth_prior = payload["depth_prior"]
    if depth_prior not in DEPTHS:
        raise GuardSchemaError(f"unknown depth_prior: {depth_prior!r}")

    raw_jurisdictions = payload["jurisdictions"]
    if not isinstance(raw_jurisdictions, list):
        raise GuardSchemaError("jurisdictions is not a list")
    jurisdictions = tuple(
        str(j).strip().upper() for j in raw_jurisdictions if str(j).strip()
    )
    if not jurisdictions:
        raise GuardSchemaError("jurisdictions is empty (NTS_099 §3 requires ≥1)")

    service_category = payload["service_category"]
    if service_category is not None:
        service_category = str(service_category).strip()
    if verdict == "accept":
        # An accepted candidate with no service, or a service this brand does
        # not sell, cannot be ranked (NTS_100) or internally linked (NTS_093).
        if not service_category:
            raise GuardSchemaError("accept without a service_category")
        if allowed_service_keys and service_category not in allowed_service_keys:
            raise GuardSchemaError(
                f"service_category {service_category!r} is not in this brand's "
                f"taxonomy {list(allowed_service_keys)}"
            )
    elif service_category and allowed_service_keys and (
        service_category not in allowed_service_keys
    ):
        # A reject may legitimately carry no service; a *wrong* one is dropped
        # rather than stored, so the reject-distribution report stays honest.
        service_category = None

    confidence: float | None
    try:
        confidence = float(payload["confidence"])
    except (TypeError, ValueError) as exc:
        raise GuardSchemaError("confidence is not a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise GuardSchemaError(f"confidence out of range: {confidence}")

    hint = payload["primary_doc_hint"]
    hint = str(hint).strip() or None if hint is not None else None
    if input_kind == "document":
        # NTS_099 §3: null for document input — the document is the item.
        hint = None

    doc_lang = payload["doc_language_expected"]
    doc_lang = str(doc_lang).strip().lower() or None if doc_lang is not None else None

    return GuardVerdict(
        verdict=verdict,
        reason_code=reason_code,
        reason=reason,
        service_category=service_category or None,
        jurisdictions=jurisdictions,
        event_stage=event_stage,
        depth_prior=depth_prior,
        primary_doc_hint=hint,
        doc_language_expected=doc_lang,
        confidence=confidence,
    )


# --- the rubric (fallback constant; the live one is a ``prompts`` row) -----

# NTS_099 §6 — exactly these ten. Fewer and the rubric cannot see its input;
# more and the render raises a KeyError mid-run, which is why the resolver
# rejects an unknown placeholder instead of trying to be helpful.
GUARD_REQUIRED_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "services",
        "jurisdiction_tiers",
        "input_kind",
        "title",
        "summary",
        "source_name",
        "source_class",
        "source_language",
        "published_at",
        "recent_accepted_titles",
    }
)

# Rubric text per NTS_099 §4 with the NTS_115 artefact-3 decisions applied:
# wealth tax, inheritance tax, CARF, golden visa, sanctions (official acts
# only, NTS_108 §6), succession and family law, family-office requirements and
# de-risking, mid-market M&A with price or structure disclosed — all IN; the
# "nothing outside tier1-tier2" line reformulated as item 9 of that artefact.
_GUARD_PROMPT = """You are the editorial guard for a private-capital advisory brand.
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


def guard_template_placeholders(template: str) -> frozenset[str]:
    """The ``{placeholder}`` names a template references."""
    return frozenset(
        fname for _, fname, _, _ in string.Formatter().parse(template) if fname
    )


def resolve_guard_template(brand_id_fk: int | None) -> tuple[str, str]:
    """Return ``(template, source)`` — the brand's active rubric or the constant.

    ``source`` is ``"db"`` or ``"code"``. The NTS_067 contract, applied to the
    rubric exactly as ``CommentWriter._resolve_template`` applies it to the
    writer prompts: the DB row is used only when its placeholder set is
    *exactly* :data:`GUARD_REQUIRED_PLACEHOLDERS` — nothing missing, nothing we
    cannot render. A broken edit therefore falls back with
    ``editorial_guard.db_prompt_rejected`` in the log rather than raising
    mid-run, and NTS_071 §6's "check the log for a rejected prompt" advice
    transfers unchanged.
    """
    if brand_id_fk is None:
        return _GUARD_PROMPT, "code"
    try:
        from sqlalchemy import select

        from pipeline.admin import db as admin_db
        from pipeline.admin.models import Prompt

        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.scalars(
                select(Prompt).where(
                    Prompt.brand_id_fk == brand_id_fk,
                    Prompt.prompt_type == "editorial_guard",
                    Prompt.is_active.is_(True),
                )
            ).first()
        if row is None or not row.content:
            return _GUARD_PROMPT, "code"
        fields = guard_template_placeholders(row.content)
        if fields != GUARD_REQUIRED_PLACEHOLDERS:
            log.warning(
                "editorial_guard.db_prompt_rejected",
                missing_required=sorted(GUARD_REQUIRED_PLACEHOLDERS - fields),
                unknown_placeholders=sorted(fields - GUARD_REQUIRED_PLACEHOLDERS),
            )
            return _GUARD_PROMPT, "code"
        return row.content, "db"
    # A DB hiccup must not stop intake — fall back to the constant.
    except Exception as exc:
        log.warning("editorial_guard.db_prompt_resolve_failed", err=str(exc))
        return _GUARD_PROMPT, "code"


# --- placeholder rendering (per brand, never constants) -------------------


def render_services(taxonomy: Sequence[Mapping[str, str]]) -> str:
    """Render ``{services}`` from ``brand_taxonomy`` rows (NTS_099 §2)."""
    if not taxonomy:
        return "(no services configured — reject everything as out_of_scope)"
    return "\n".join(
        f"- {row['key']}: {row['label']} — {row['description_for_guard']}"
        for row in taxonomy
    )


def render_jurisdiction_tiers(tiers: Mapping[str, Sequence[str]] | None) -> str:
    """Render ``{jurisdiction_tiers}`` from the config key (NTS_099 §2)."""
    if not tiers:
        return "(no tiers configured)"
    lines = []
    for tier in ("tier1", "tier2"):
        codes = tiers.get(tier)
        if codes:
            lines.append(f"- {tier}: {', '.join(str(c) for c in codes)}")
    return "\n".join(lines) or "(no tiers configured)"


def load_brand_taxonomy(brand_id_fk: int | None) -> tuple[dict[str, str], ...]:
    """``brand_taxonomy`` rows for a brand, ordered by key. Empty on any error.

    Empty is *not* benign and must not read as benign: with no services the
    rubric can only reject, which is the correct failure — a guard that accepts
    into a service the brand does not sell is worse than one that accepts
    nothing.
    """
    if brand_id_fk is None:
        return ()
    try:
        from sqlalchemy import select

        from pipeline.admin import db as admin_db
        from pipeline.admin.models import BrandTaxonomy

        factory = admin_db.get_session_factory()
        with factory() as session:
            rows = session.scalars(
                select(BrandTaxonomy)
                .where(BrandTaxonomy.brand_id_fk == brand_id_fk)
                .order_by(BrandTaxonomy.key)
            ).all()
        return tuple(
            {
                "key": r.key,
                "label": r.label,
                "description_for_guard": r.description_for_guard,
            }
            for r in rows
        )
    # No taxonomy means the rubric can only reject, which is the safe failure.
    except Exception as exc:
        log.warning("editorial_guard.taxonomy_load_failed", err=str(exc))
        return ()


def render_guard_prompt(
    template: str,
    *,
    services: str,
    jurisdiction_tiers: str,
    input_kind: str,
    title: str,
    summary: str | None,
    source_name: str,
    source_class: str,
    source_language: str,
    published_at: datetime | str | None,
    recent_accepted_titles: Sequence[str],
) -> str:
    """Render the rubric. Every value is a string by the time it lands."""
    published = (
        published_at.isoformat()
        if isinstance(published_at, datetime)
        else (str(published_at) if published_at else "(unknown)")
    )
    recent = (
        "\n".join(f"- {t}" for t in recent_accepted_titles)
        if recent_accepted_titles
        else "(nothing accepted yet)"
    )
    return template.format(
        services=services,
        jurisdiction_tiers=jurisdiction_tiers,
        input_kind=input_kind,
        title=title or "(no title)",
        summary=(summary or "(no summary)"),
        source_name=source_name or "(unknown)",
        source_class=source_class or "(unknown)",
        source_language=source_language or "(unknown)",
        published_at=published,
        recent_accepted_titles=recent,
    )


# --- the call -------------------------------------------------------------


async def _call_guard_model(
    prompt: str, *, model: str
) -> tuple[dict[str, Any], int | None, int | None]:
    """One schema-constrained completion. Thin, so tests monkeypatch it."""
    import openai

    from pipeline.common.config import get_settings

    settings = get_settings()
    if not settings.openai_api_key:
        raise GuardError("OPENAI_API_KEY not set")
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "editorial_verdict",
                "schema": guard_json_schema(),
                "strict": True,
            },
        },
        max_tokens=GUARD_MAX_TOKENS,
        timeout=GUARD_TIMEOUT_S,
    )
    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    if not text:
        raise GuardSchemaError("guard returned empty output")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise GuardSchemaError("guard did not return valid JSON") from exc
    return (
        parsed,
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )


def _record_guard_cost(
    *, model: str, input_kind: str, tokens_in: int | None, tokens_out: int | None
) -> None:
    """One ``cost_records`` row per guard call, ``operation='guard:<input_kind>'``.

    NTS_099 §"Мерить" (measure from day one) wants guard cost split by
    ``input_kind``.
    ``cost_records`` has no such column and the operation string is free-form
    and grouped dynamically by the cost dashboard, so the split rides there
    rather than in a migration.
    """
    from pipeline.admin.cost_recorder import record_cost
    from pipeline.common.pricing import openai_cost

    record_cost(
        provider="openai",
        operation=f"guard:{input_kind}",
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=openai_cost(model, tokens_in, tokens_out),
    )


async def judge_item(
    *,
    input_kind: str,
    title: str,
    summary: str | None,
    source_name: str,
    source_class: str,
    source_language: str,
    published_at: datetime | None,
    recent_accepted_titles: Sequence[str],
    template: str,
    services_block: str,
    tiers_block: str,
    allowed_service_keys: tuple[str, ...],
    model: str = DEFAULT_GUARD_MODEL,
    max_attempts: int = GUARD_MAX_ATTEMPTS,
    sleep=asyncio.sleep,
) -> GuardVerdict:
    """Judge one item. The only function in this module that spends money.

    Raises :class:`GuardSchemaError` when the response violates the contract
    (→ ``guard_error``, no row) and :class:`GuardDeferred` when the model could
    not be reached (→ replayed next intake). The distinction matters: the first
    is a rubric or model problem to look at, the second is weather.
    """
    if input_kind not in INPUT_KINDS:
        raise ValueError(f"input_kind must be one of {INPUT_KINDS}, got {input_kind!r}")

    prompt = render_guard_prompt(
        template,
        services=services_block,
        jurisdiction_tiers=tiers_block,
        input_kind=input_kind,
        title=title,
        summary=summary,
        source_name=source_name,
        source_class=source_class,
        source_language=source_language,
        published_at=published_at,
        recent_accepted_titles=recent_accepted_titles,
    )

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            payload, tokens_in, tokens_out = await _call_guard_model(
                prompt, model=model
            )
        except GuardSchemaError:
            # A malformed body is not a transport failure — retrying the same
            # prompt against the same model reproduces it and pays twice.
            raise
        # Transport: 429 / 5xx / timeout. Retried with backoff, then deferred.
        except Exception as exc:
            last_error = exc
            log.warning(
                "editorial_guard.call_failed",
                attempt=attempt,
                of=max_attempts,
                err=f"{type(exc).__name__}: {exc}",
            )
            if attempt < max_attempts:
                await sleep(GUARD_BACKOFF_BASE_S * (2 ** (attempt - 1)))
            continue
        _record_guard_cost(
            model=model,
            input_kind=input_kind,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        return parse_guard_response(
            payload,
            input_kind=input_kind,
            allowed_service_keys=allowed_service_keys,
        )

    raise GuardDeferred(
        f"guard unreachable after {max_attempts} attempts: {last_error!r}"
    )
