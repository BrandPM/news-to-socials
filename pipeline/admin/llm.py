"""LLM dispatchers used by admin routes (prompt /test endpoint).

Kept small + injectable so tests can monkeypatch a single function instead
of mocking the OpenAI client. Real implementation calls
``pipeline.generator.comment_writer.CommentWriter`` for writer-* prompt
types and a thin gpt-4o-mini call for the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class PromptTestResult:
    text: str
    cost_usd: float
    ai_tells_count: int


@dataclass
class PromptAnalysisResult:
    strengths: list[str]
    contradictions: list[dict[str, str]]
    risks: list[str]
    summary: str
    cost_usd: float


class PromptAnalysisError(ValueError):
    """The LLM returned something we can't coerce into the strict
    analysis schema. The /analyze route maps this to HTTP 422 so a flaky
    model response never surfaces as a 500."""


# NTS_064 — Analyze runs on a top reasoning model (prompt review is
# LLM-as-judge, quality over latency). Pinned snapshot so behaviour stays
# stable; the structured-output schema enforces the wire contract at the
# API layer, so the system prompt can stay high-level. Reasoning models do
# their own thinking — no chain-of-thought / "think step by step" framing.
_ANALYSIS_MODEL = "gpt-5.5-2026-04-23"
_ANALYSIS_EFFORT: Literal["high"] = "high"
# Generous output cap: at effort=high the reasoning tokens count against
# this budget, so leave plenty of room above the (small) JSON answer.
_ANALYSIS_MAX_OUTPUT_TOKENS = 8000
# Backend-side request timeout (seconds). Below the proxy's maxDuration so
# a slow call fails cleanly here instead of being killed mid-flight.
_ANALYSIS_TIMEOUT_S = 180.0

_ANALYZER_SYSTEM = (
    "You are a senior prompt engineer doing a rigorous review of a "
    "production LLM prompt used in a news-to-social content pipeline. "
    "Judge the prompt the user provides: name its genuine strengths, find "
    "internal contradictions, and list concrete risks. Internal "
    "contradictions are instructions that fight each other and cause real "
    "downstream failures — for example, requiring '## H2' markdown "
    "headings in the article body while forbidding markdown in the title, "
    "which leaks '##' into the title field. Be specific to this prompt's "
    "actual wording; avoid generic prompt-writing advice. For each "
    "contradiction give the issue, why it bites downstream, and a concrete "
    "fix."
)

# Strict structured-output schema (Responses API text.format). Forces the
# exact wire shape so a well-formed model reply always matches the
# contract; _coerce_analysis stays as defence-in-depth for the 422 path.
_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "strengths": {"type": "array", "items": {"type": "string"}},
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "issue": {"type": "string"},
                    "why": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["issue", "why", "suggestion"],
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["strengths", "contradictions", "risks", "summary"],
}


def _coerce_analysis(raw: Any) -> tuple[list[str], list[dict[str, str]], list[str], str]:
    """Validate the parsed LLM JSON against the strict analysis shape.

    Raises ``PromptAnalysisError`` on any structural deviation so the
    route can return 422. We coerce scalars to ``str`` defensively but
    reject missing/incorrectly-typed top-level keys.
    """
    if not isinstance(raw, dict):
        raise PromptAnalysisError("expected a JSON object")

    def _str_list(value: Any, field: str) -> list[str]:
        if not isinstance(value, list):
            raise PromptAnalysisError(f"{field!r} must be an array")
        return [str(item) for item in value]

    strengths = _str_list(raw.get("strengths"), "strengths")
    risks = _str_list(raw.get("risks"), "risks")

    raw_contras = raw.get("contradictions")
    if not isinstance(raw_contras, list):
        raise PromptAnalysisError("'contradictions' must be an array")
    contradictions: list[dict[str, str]] = []
    for item in raw_contras:
        if not isinstance(item, dict):
            raise PromptAnalysisError(
                "each contradiction must be an object with issue/why/suggestion"
            )
        contradictions.append(
            {
                "issue": str(item.get("issue", "")),
                "why": str(item.get("why", "")),
                "suggestion": str(item.get("suggestion", "")),
            }
        )

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise PromptAnalysisError("'summary' must be a non-empty string")

    return strengths, contradictions, risks, summary.strip()


async def run_prompt_analysis(
    *,
    prompt_type: str,
    prompt_content: str,
    brand_id_fk: int | None = None,
) -> PromptAnalysisResult:
    """Send a prompt to the reviewer LLM and return a structured critique.

    Reuses the same OpenAI client + settings + pricing table + cost
    recorder as ``run_prompt_test`` (NTS task 3: do not spin up a new
    client). Records a ``prompt_analysis`` cost row. Read-only: the
    prompt itself is never modified. Raises ``PromptAnalysisError`` when
    the model's reply can't be coerced into the strict schema.
    """
    import json as _json  # noqa: PLC0415

    import openai  # noqa: PLC0415
    from openai.types.responses.response_text_config_param import (  # noqa: PLC0415
        ResponseTextConfigParam,
    )
    from openai.types.shared_params import Reasoning  # noqa: PLC0415

    from pipeline.common.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot run /prompts/analyze")

    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    model = _ANALYSIS_MODEL
    user_msg = (
        f"Prompt type: {prompt_type}\n\n"
        "--- PROMPT UNDER REVIEW ---\n"
        f"{prompt_content}\n"
        "--- END PROMPT ---"
    )
    # Responses API (gpt-5.5 is Responses-first): system goes in
    # ``instructions``, the prompt under review in ``input``, reasoning
    # effort high, strict JSON via ``text.format``. Params are annotated
    # with the SDK TypedDicts so the literals type-check.
    reasoning_param: Reasoning = {"effort": _ANALYSIS_EFFORT}
    text_param: ResponseTextConfigParam = {
        "format": {
            "type": "json_schema",
            "name": "prompt_analysis",
            "schema": _ANALYSIS_SCHEMA,
            "strict": True,
        }
    }
    resp = await client.responses.create(
        model=model,
        instructions=_ANALYZER_SYSTEM,
        input=user_msg,
        reasoning=reasoning_param,
        text=text_param,
        max_output_tokens=_ANALYSIS_MAX_OUTPUT_TOKENS,
        timeout=_ANALYSIS_TIMEOUT_S,
    )
    text = (resp.output_text or "").strip()
    if not text:
        # Empty output usually means the reasoning budget was exhausted
        # before any answer tokens — surface it as a 422, not a 500.
        raise PromptAnalysisError("model returned an empty response")

    try:
        parsed = _json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PromptAnalysisError("model did not return valid JSON") from exc
    strengths, contradictions, risks, summary = _coerce_analysis(parsed)

    # Cost recording mirrors run_prompt_test exactly (NTS_025 — shared
    # pricing table so the Costs dashboard agrees with this figure). On the
    # Responses API reasoning tokens are part of ``output_tokens``, so the
    # output rate already accounts for them.
    from pipeline.common.pricing import openai_cost  # noqa: PLC0415

    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "input_tokens", None)
    tokens_out = getattr(usage, "output_tokens", None)
    cost = openai_cost(model, tokens_in, tokens_out)

    if brand_id_fk is not None:
        from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415

        try:
            AdminConfigClient.record_cost(
                brand_id_fk=brand_id_fk,
                provider="openai",
                operation="prompt_analysis",
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
        except Exception:  # noqa: BLE001
            # Cost recording must never break the endpoint.
            pass

    return PromptAnalysisResult(
        strengths=strengths,
        contradictions=contradictions,
        risks=risks,
        summary=summary,
        cost_usd=round(cost, 4),
    )


async def run_prompt_test(
    *,
    prompt_type: str,
    prompt_content: str,
    sample_topic: dict[str, Any],
    brand_id_fk: int | None = None,
) -> PromptTestResult:
    """Render the prompt against the sample topic and return the result.

    For writer_polish / writer_draft we approximate a real call by asking
    gpt-4o to follow the prompt verbatim with the topic injected. For
    topic_picker / image_prompt we send a scoring/image-prompt request to
    gpt-4o-mini. AI-tells are counted via the existing
    ``anti_ai_check.find_banned_phrase_hits``.
    """
    # Lazy imports so the admin server can boot without OpenAI configured.
    import openai  # noqa: PLC0415

    from pipeline.common.config import get_settings  # noqa: PLC0415
    from pipeline.generator.anti_ai_check import find_banned_phrase_hits  # noqa: PLC0415

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot run /prompts/test")

    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    # We feed the prompt as-is plus the topic context. The prompt itself
    # is responsible for declaring its own placeholders — admin UI users
    # write prompts that already reference {title}, {summary}, etc. We
    # do a best-effort .format(); if a key is missing we send raw.
    try:
        rendered = prompt_content.format(
            title=sample_topic["title"],
            summary=sample_topic["summary"],
            url=sample_topic["url"],
            voice_profile_yaml="(omitted in test)",
            language="en",
            draft="(draft would go here in a real run)",
            body="(body would go here in a real run)",
        )
    except KeyError:
        # If the prompt uses placeholders we don't recognise, send the raw
        # content with the topic appended so the LLM has *something* to
        # work with.
        rendered = (
            prompt_content
            + "\n\n---\nSample topic:\n"
            + f"Title: {sample_topic['title']}\n"
            + f"Summary: {sample_topic['summary']}\n"
            + f"URL: {sample_topic['url']}\n"
        )

    # writer_polish + writer_translate exercise the gpt-4o-class prompt the
    # pipeline really uses; the cheaper types stay on gpt-4o-mini.
    model = (
        "gpt-4o"
        if prompt_type in ("writer_polish", "writer_translate")
        else "gpt-4o-mini"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": rendered}],
        max_tokens=600,
    )
    text = (resp.choices[0].message.content or "").strip()

    # Pull banned-phrase list from the active config (best-effort). If we
    # can't reach it, AI-tells is just 0 — informational, not blocking.
    ai_tells = 0
    if brand_id_fk is not None:
        try:
            from pipeline.admin.db import session_scope  # noqa: PLC0415
            from pipeline.admin.models import PipelineConfig  # noqa: PLC0415
            import json as _json  # noqa: PLC0415

            with session_scope() as session:
                cfg = session.get(PipelineConfig, brand_id_fk)
                banned: list[str] = (
                    _json.loads(cfg.banned_phrases) if cfg and cfg.banned_phrases else []
                )
            ai_tells = len(find_banned_phrase_hits(text, banned))
        except Exception:  # noqa: BLE001
            ai_tells = 0

    # Use the shared pricing table (NTS_025 — single source of truth so
    # cost dashboards in S4 agree with the per-test returned value).
    from pipeline.common.pricing import openai_cost  # noqa: PLC0415

    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None)
    tokens_out = getattr(usage, "completion_tokens", None)
    cost = openai_cost(model, tokens_in, tokens_out)

    # Record a cost_records row when a brand context is supplied (the
    # /prompts/{id}/test endpoint passes brand_id_fk explicitly so the
    # row is attributed to the right brand even though no run is
    # active). NTS_025 C1.
    if brand_id_fk is not None:
        from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415

        try:
            AdminConfigClient.record_cost(
                brand_id_fk=brand_id_fk,
                provider="openai",
                operation="prompt_test",
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
        except Exception:  # noqa: BLE001
            # Cost recording must never break the endpoint.
            pass

    return PromptTestResult(text=text, cost_usd=round(cost, 4), ai_tells_count=ai_tells)
