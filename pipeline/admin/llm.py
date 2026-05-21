"""LLM dispatchers used by admin routes (prompt /test endpoint).

Kept small + injectable so tests can monkeypatch a single function instead
of mocking the OpenAI client. Real implementation calls
``pipeline.generator.comment_writer.CommentWriter`` for writer-* prompt
types and a thin gpt-4o-mini call for the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PromptTestResult:
    text: str
    cost_usd: float
    ai_tells_count: int


async def run_prompt_test(
    *,
    prompt_type: str,
    prompt_content: str,
    sample_topic: dict[str, Any],
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

    model = "gpt-4o" if prompt_type == "writer_polish" else "gpt-4o-mini"
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": rendered}],
        max_tokens=600,
    )
    text = (resp.choices[0].message.content or "").strip()

    # Pull banned-phrase list from the active config (best-effort). If we
    # can't reach it, AI-tells is just 0 — informational, not blocking.
    try:
        from pipeline.admin.db import session_scope  # noqa: PLC0415
        from pipeline.admin.models import PipelineConfig  # noqa: PLC0415
        import json as _json  # noqa: PLC0415

        with session_scope() as session:
            cfg = session.get(PipelineConfig, "icon")
            banned: list[str] = (
                _json.loads(cfg.banned_phrases) if cfg and cfg.banned_phrases else []
            )
        ai_tells = len(find_banned_phrase_hits(text, banned))
    except Exception:  # noqa: BLE001
        ai_tells = 0

    # Rough cost: gpt-4o is $5/1M in, $15/1M out; gpt-4o-mini is
    # $0.15/1M in, $0.6/1M out. Use the usage object if present.
    usage = getattr(resp, "usage", None)
    if usage is None:
        cost = 0.0
    else:
        if prompt_type == "writer_polish":
            cost = (
                usage.prompt_tokens * 5e-6 + usage.completion_tokens * 15e-6
            )
        else:
            cost = (
                usage.prompt_tokens * 0.15e-6 + usage.completion_tokens * 0.6e-6
            )
    return PromptTestResult(text=text, cost_usd=round(cost, 4), ai_tells_count=ai_tells)
