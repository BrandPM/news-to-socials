"""LLM-as-judge draft eval (IT_PROJ_NTS_091 / spec NTS_080).

Scores every draft on a rubric BEFORE it reaches the content manager, turning
the manual "Analyze" tool (NTS_064) into an automatic pipeline stage. Two
rubrics (cost-optimised, deliberately different from the spec):

* **EN (canonical)** → FULL rubric: ``factuality`` (claims absent from the
  source — the "67% of clients" killer), ``specificity``, ``voice_match``,
  ``structure``, ``banned_leakage`` (deterministic banned scan + judge
  paraphrase check).
* **RU/UK/PL** → REDUCED rubric: ``translation_fidelity`` (EN↔translation) +
  ``banned_leakage`` only. Factuality/structure are inherited from the EN canon
  (NTS_065) — never re-judged. ~60% cheaper than full×4.

Model policy: **gpt-4o** for the stream; escalate to **gpt-5.5** (Responses
API, reasoning=high — same infra as NTS_064 Analyze) only for drafts whose
gpt-4o total lands in the *yellow band* around the threshold, where the
flag/no-flag call is most consequential. The model that produced the
authoritative row is stored per score.

HARD STOP — FAIL OPEN: every judge error raises :class:`JudgeError`, which the
pipeline catches so a dead judge never blocks draft creation or publishing. NO
auto-reject, NO auto-retry — scoring + flagging only; the human decides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pipeline.common.logging import get_logger

log = get_logger(__name__)

# Bump when the rubric wording / axes / weights change — scores are only
# comparable within one version (stored on every draft_scores row).
JUDGE_PROMPT_VERSION = "v1"

# Stream vs escalation models (reuse NTS_064 infra + the shared pricing table).
STREAM_MODEL = "gpt-4o"
ESCALATION_MODEL = "gpt-5.5-2026-04-23"

# gpt-4o total within ±this of the threshold → escalate to gpt-5.5 for a more
# reliable read (the ambiguous middle, where flagging actually matters).
YELLOW_MARGIN = 1.0

# Rubric weights (sum to 1.0 each). EN = full; non-EN = reduced.
EN_WEIGHTS: dict[str, float] = {
    "factuality": 0.30,
    "specificity": 0.20,
    "voice_match": 0.20,
    "structure": 0.15,
    "banned_leakage": 0.15,
}
NONEN_WEIGHTS: dict[str, float] = {
    "translation_fidelity": 0.70,
    "banned_leakage": 0.30,
}

# How hard a deterministic banned-phrase hit pulls the banned_leakage axis down
# (per hit), regardless of what the judge thought about paraphrases.
_BANNED_PENALTY_PER_HIT = 3.0

_MAX_TEXT_CHARS = 6000  # bound judge input cost


class JudgeError(RuntimeError):
    """Judge call failed or returned an uncoercible payload. The pipeline
    catches this and lets the draft proceed UNSCORED (fail-open)."""


@dataclass
class JudgeResult:
    lang: str
    model: str
    axes: dict[str, float]
    total: float
    feedback: str
    worst_axis: str
    banned_hits: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    def rubric_json(self) -> str:
        return json.dumps(
            {
                "version": JUDGE_PROMPT_VERSION,
                "axes": self.axes,
                "weights": weights_for(self.lang),
                "feedback": self.feedback,
                "worst_axis": self.worst_axis,
                "banned_hits": self.banned_hits,
                "model": self.model,
            },
            ensure_ascii=False,
        )


def weights_for(lang: str) -> dict[str, float]:
    return EN_WEIGHTS if lang == "en" else NONEN_WEIGHTS


# --- pure helpers (unit-tested) --------------------------------------------


def deterministic_banned_hits(text: str, banned: list[str]) -> list[str]:
    """Exact/substring banned-phrase hits (reuses the anti-AI-check scanner)."""
    from pipeline.generator.anti_ai_check import find_banned_phrase_hits  # noqa: PLC0415

    return find_banned_phrase_hits(text or "", banned or [])


def parse_scores(raw: object, axes: list[str]) -> dict[str, float]:
    """Validate a judge payload's ``scores`` into ``{axis: 0..10}``.

    Raises :class:`JudgeError` on missing axes or non-numeric / out-of-range
    values — a malformed judge reply must never silently score 0.
    """
    if not isinstance(raw, dict):
        raise JudgeError("judge payload is not an object")
    scores_obj = raw.get("scores")
    if not isinstance(scores_obj, dict):
        raise JudgeError("judge payload missing 'scores' object")
    out: dict[str, float] = {}
    for axis in axes:
        val = scores_obj.get(axis)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise JudgeError(f"axis {axis!r} missing or non-numeric")
        out[axis] = max(0.0, min(10.0, float(val)))
    return out


def apply_banned_penalty(axes: dict[str, float], hit_count: int) -> dict[str, float]:
    """Deterministic banned hits dominate the ``banned_leakage`` axis — an
    exact banned phrase is unambiguous leakage regardless of the judge's take."""
    if hit_count > 0 and "banned_leakage" in axes:
        capped = max(0.0, 10.0 - _BANNED_PENALTY_PER_HIT * hit_count)
        axes = {**axes, "banned_leakage": min(axes["banned_leakage"], capped)}
    return axes


def weighted_total(axes: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum of axis scores. Weights are renormalised over the axes
    actually present so a partial rubric still yields a 0–10 total."""
    present = {k: w for k, w in weights.items() if k in axes}
    wsum = sum(present.values()) or 1.0
    return round(sum(axes[k] * w for k, w in present.items()) / wsum, 3)


def worst_axis(axes: dict[str, float]) -> str:
    return min(axes, key=lambda k: axes[k]) if axes else ""


def is_yellow(total: float, threshold: float) -> bool:
    """gpt-4o total ambiguous enough (near the threshold) to warrant gpt-5.5."""
    return abs(total - threshold) <= YELLOW_MARGIN


# --- prompt construction ---------------------------------------------------


def _schema_for(axes: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scores": {
                "type": "object",
                "additionalProperties": False,
                "properties": {a: {"type": "integer"} for a in axes},
                "required": axes,
            },
            "feedback": {"type": "string"},
            "banned_paraphrases": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["scores", "feedback", "banned_paraphrases"],
    }


_EN_SYSTEM = (
    "You are a ruthless finance-news editor scoring an ENGLISH article draft on "
    "a 0-10 rubric (10 = excellent, 0 = unacceptable). Score every axis:\n"
    "- factuality: 10 only if EVERY factual claim and number in the draft is "
    "present in or directly supported by the SOURCE. Heavily penalise invented "
    "statistics (e.g. a fabricated '67% of clients'), dates, or quotes.\n"
    "- specificity: concrete, load-bearing detail vs generic filler.\n"
    "- voice_match: adherence to the brand VOICE PROFILE.\n"
    "- structure: clear lead, logical H2 sections, strong close.\n"
    "- banned_leakage: 10 = none of the BANNED phrases or close paraphrases "
    "appear; penalise each. List paraphrases you catch in banned_paraphrases.\n"
    "Give one-line feedback naming the single biggest problem. Reward concrete, "
    "sourced, on-voice writing; punish generic 'smooth' filler."
)

_NONEN_SYSTEM = (
    "You are scoring a TRANSLATION (target language: {lang}) of a canonical "
    "ENGLISH finance article on a 0-10 rubric. Score:\n"
    "- translation_fidelity: 10 = faithful — the same facts, numbers, and H2 "
    "structure as the EN canon, nothing invented or dropped, natural in the "
    "target language. Penalise invented numbers, dropped sections, or drift "
    "from the EN meaning.\n"
    "- banned_leakage: 10 = none of the target-language BANNED phrases or close "
    "paraphrases appear; penalise each. List paraphrases in banned_paraphrases.\n"
    "Do NOT re-judge factual correctness of the underlying story — that is "
    "inherited from the EN canon. One-line feedback naming the biggest issue."
)


def _en_payload(draft_text: str, source_text: str, voice_profile: str, banned: list[str]) -> str:
    return (
        f"--- SOURCE (news item) ---\n{source_text[:_MAX_TEXT_CHARS]}\n\n"
        f"--- BRAND VOICE PROFILE ---\n{(voice_profile or '(none)')[:2000]}\n\n"
        f"--- BANNED PHRASES ---\n{', '.join(banned) or '(none)'}\n\n"
        f"--- DRAFT UNDER REVIEW ---\n{draft_text[:_MAX_TEXT_CHARS]}"
    )


def _nonen_payload(draft_text: str, en_text: str, banned: list[str]) -> str:
    return (
        f"--- EN CANONICAL DRAFT ---\n{en_text[:_MAX_TEXT_CHARS]}\n\n"
        f"--- BANNED PHRASES (target language) ---\n{', '.join(banned) or '(none)'}\n\n"
        f"--- TRANSLATION UNDER REVIEW ---\n{draft_text[:_MAX_TEXT_CHARS]}"
    )


# --- LLM calls (thin + monkeypatchable in tests) ---------------------------


async def _call_gpt4o(system: str, payload: str, schema: dict) -> tuple[dict, int | None, int | None]:
    import openai  # noqa: PLC0415

    from pipeline.common.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    if not settings.openai_api_key:
        raise JudgeError("OPENAI_API_KEY not set")
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=STREAM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "draft_eval", "schema": schema, "strict": True},
        },
        max_tokens=900,
        timeout=90.0,
    )
    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    return _loads(text), getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)


async def _call_gpt55(system: str, payload: str, schema: dict) -> tuple[dict, int | None, int | None]:
    import openai  # noqa: PLC0415
    from openai.types.responses.response_text_config_param import (  # noqa: PLC0415
        ResponseTextConfigParam,
    )
    from openai.types.shared_params import Reasoning  # noqa: PLC0415

    from pipeline.common.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    if not settings.openai_api_key:
        raise JudgeError("OPENAI_API_KEY not set")
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    reasoning_param: Reasoning = {"effort": "high"}
    text_param: ResponseTextConfigParam = {
        "format": {"type": "json_schema", "name": "draft_eval", "schema": schema, "strict": True}
    }
    resp = await client.responses.create(
        model=ESCALATION_MODEL,
        instructions=system,
        input=payload,
        reasoning=reasoning_param,
        text=text_param,
        max_output_tokens=6000,
        timeout=180.0,
    )
    text = (resp.output_text or "").strip()
    usage = getattr(resp, "usage", None)
    return _loads(text), getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def _loads(text: str) -> dict:
    if not text:
        raise JudgeError("judge returned empty output")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise JudgeError("judge did not return valid JSON") from exc
    if not isinstance(parsed, dict):
        raise JudgeError("judge JSON is not an object")
    return parsed


# --- orchestration ---------------------------------------------------------


async def run_judge(
    *,
    draft_text: str,
    lang: str,
    source_text: str = "",
    en_text: str = "",
    banned: list[str] | None = None,
    voice_profile: str = "",
    model: str,
    brand_id_fk: int | None = None,
    run_id: int | None = None,
    draft_id: str | None = None,
) -> JudgeResult:
    """Run ONE judge call (``model``) and return a scored result.

    Records a ``cost_records`` row (operation ``draft_eval``) per call (C1).
    Raises :class:`JudgeError` on any failure — caller decides fail-open.
    """
    banned = banned or []
    is_en = lang == "en"
    weights = weights_for(lang)
    axes = list(weights.keys())
    schema = _schema_for(axes)
    if is_en:
        system = _EN_SYSTEM
        payload = _en_payload(draft_text, source_text, voice_profile, banned)
    else:
        system = _NONEN_SYSTEM.format(lang=lang)
        payload = _nonen_payload(draft_text, en_text, banned)

    caller = _call_gpt55 if model == ESCALATION_MODEL else _call_gpt4o
    raw, tin, tout = await caller(system, payload, schema)

    scores = parse_scores(raw, axes)
    hits = deterministic_banned_hits(draft_text, banned)
    scores = apply_banned_penalty(scores, len(hits))
    total = weighted_total(scores, weights)
    feedback = str(raw.get("feedback") or "").strip()[:500]

    # Cost (C1) — shared pricing table so the Costs dashboard agrees.
    from pipeline.common.pricing import openai_cost  # noqa: PLC0415

    cost = openai_cost(model, tin, tout)
    if brand_id_fk is not None:
        from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415

        from pipeline.admin.cost_recorder import get_context  # noqa: PLC0415

        try:
            AdminConfigClient.record_cost(
                brand_id_fk=brand_id_fk,
                run_id=run_id,
                draft_id=draft_id,
                # The judge writes its row directly rather than through
                # ``record_cost``, because it is given brand and run
                # explicitly. That skipped the ambient candidate id, and the
                # first real production run showed it: two of thirteen cost
                # rows landed unattributed, which makes
                # ``max_cost_per_candidate_usd`` wrong by the price of the
                # judge. Reading the context here keeps the v2 path unchanged
                # (no context → None) and completes the v3 one.
                candidate_id_fk=get_context().candidate_id,
                provider="openai",
                operation="draft_eval",
                model=model,
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=cost,
            )
        except Exception:  # noqa: BLE001 — cost recording never breaks eval
            log.warning("judge.cost_record_failed", draft_id=draft_id)

    return JudgeResult(
        lang=lang,
        model=model,
        axes=scores,
        total=total,
        feedback=feedback,
        worst_axis=worst_axis(scores),
        banned_hits=hits,
        cost_usd=round(cost, 6),
    )


async def evaluate_draft(
    *,
    draft_text: str,
    lang: str,
    eval_threshold: float,
    source_text: str = "",
    en_text: str = "",
    banned: list[str] | None = None,
    voice_profile: str = "",
    brand_id_fk: int | None = None,
    run_id: int | None = None,
    draft_id: str | None = None,
) -> JudgeResult:
    """Score a draft: gpt-4o first, escalate to gpt-5.5 only in the yellow band.

    Returns the authoritative :class:`JudgeResult`. Raises :class:`JudgeError`
    on failure (fail-open handled by the caller).
    """
    base = await run_judge(
        draft_text=draft_text, lang=lang, source_text=source_text, en_text=en_text,
        banned=banned, voice_profile=voice_profile, model=STREAM_MODEL,
        brand_id_fk=brand_id_fk, run_id=run_id, draft_id=draft_id,
    )
    if not is_yellow(base.total, eval_threshold):
        return base
    # Yellow band → re-judge on the reasoning model for a firmer call.
    log.info("judge.escalate", draft_id=draft_id, lang=lang, gpt4o_total=base.total)
    try:
        return await run_judge(
            draft_text=draft_text, lang=lang, source_text=source_text, en_text=en_text,
            banned=banned, voice_profile=voice_profile, model=ESCALATION_MODEL,
            brand_id_fk=brand_id_fk, run_id=run_id, draft_id=draft_id,
        )
    except JudgeError:
        # Escalation failed — keep the gpt-4o read rather than losing the score.
        log.warning("judge.escalation_failed_keeping_gpt4o", draft_id=draft_id)
        return base


def banned_for(voice_profile_yaml: str, lang: str) -> list[str]:
    """Per-language banned phrases from the brand voice profile (NTS_072)."""
    try:
        from pipeline.admin.voice_banned import read_banned_by_language  # noqa: PLC0415

        return read_banned_by_language(voice_profile_yaml or "", [lang]).get(lang, [])
    except Exception:  # noqa: BLE001
        return []


def _format_flag_alert(res: JudgeResult, threshold: float) -> str:
    import html  # noqa: PLC0415

    worst = res.worst_axis or "—"
    worst_score = res.axes.get(res.worst_axis, 0.0) if res.worst_axis else 0.0
    fb = html.escape(res.feedback or "")
    return (
        "🟡 <b>Draft flagged — needs attention</b>\n"
        f"Lang: {res.lang.upper()} · score <b>{res.total:.1f}</b>/10 "
        f"(threshold {threshold:.1f})\n"
        f"Worst axis: {html.escape(worst)} {worst_score:.0f}/10\n"
        f"{fb}"
    )


async def _maybe_alert(res: JudgeResult, *, threshold: float, draft_id: str) -> None:
    """Send ONE Telegram alert per flagged draft (dedup via alert_sent).
    No-op if Telegram is unconfigured. Never raises."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from pipeline.common.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    chat = settings.telegram_monitoring_chat_id
    if not settings.telegram_bot_token or not chat:
        return
    key = f"eval_flag:{draft_id}:{res.lang}"
    try:
        from sqlalchemy import select  # noqa: PLC0415

        from pipeline.admin.db import session_scope  # noqa: PLC0415
        from pipeline.admin.models import AlertSent  # noqa: PLC0415

        with session_scope() as session:
            already = session.execute(
                select(AlertSent.notification_id).where(
                    AlertSent.notification_id == key
                )
            ).scalar_one_or_none()
        if already:
            return
        from pipeline.publisher.telegram_bot import TelegramPublisher  # noqa: PLC0415

        await TelegramPublisher()._send_message(chat, _format_flag_alert(res, threshold))
        with session_scope() as session:
            session.merge(AlertSent(notification_id=key, sent_at=datetime.now(tz=timezone.utc)))
    except Exception:  # noqa: BLE001
        log.warning("judge.alert_failed", draft_id=draft_id, lang=res.lang)


async def score_draft(
    *,
    draft_id: str,
    lang: str,
    draft_text: str,
    eval_enabled: bool,
    eval_threshold: float,
    source_text: str = "",
    en_text: str = "",
    voice_profile_yaml: str = "",
    brand_id_fk: int | None = None,
    run_id: int | None = None,
) -> JudgeResult | None:
    """Score a freshly-created draft, persist it, flag + alert if below
    threshold. FULLY FAIL-OPEN — returns ``None`` on any problem (or when
    eval is disabled) and never raises, so the pipeline is never blocked.
    """
    if not eval_enabled or not draft_text:
        return None
    try:
        banned = banned_for(voice_profile_yaml, lang)
        res = await evaluate_draft(
            draft_text=draft_text,
            lang=lang,
            eval_threshold=eval_threshold,
            source_text=source_text,
            en_text=en_text,
            banned=banned,
            voice_profile=voice_profile_yaml,
            brand_id_fk=brand_id_fk,
            run_id=run_id,
            draft_id=draft_id,
        )
    except Exception as exc:  # noqa: BLE001 — HARD STOP: eval fails open
        log.warning("judge.eval_failed_fail_open", draft_id=draft_id, lang=lang, err=str(exc))
        return None

    flagged = res.total < eval_threshold
    try:
        from datetime import datetime, timezone  # noqa: PLC0415

        from pipeline.admin.db import session_scope  # noqa: PLC0415
        from pipeline.admin.models import DraftScore  # noqa: PLC0415

        with session_scope() as session:
            session.add(
                DraftScore(
                    draft_id=draft_id,
                    brand_id_fk=brand_id_fk,
                    lang=lang,
                    rubric_json=res.rubric_json(),
                    total=res.total,
                    flagged=flagged,
                    model=res.model,
                    judge_prompt_version=JUDGE_PROMPT_VERSION,
                    created_at=datetime.now(tz=timezone.utc),
                )
            )
    except Exception:  # noqa: BLE001 — persistence failure never blocks
        log.warning("judge.persist_failed", draft_id=draft_id, lang=lang)

    log.info(
        "judge.scored", draft_id=draft_id, lang=lang, total=res.total,
        model=res.model, flagged=flagged,
    )
    if flagged:
        await _maybe_alert(res, threshold=eval_threshold, draft_id=draft_id)
    return res
