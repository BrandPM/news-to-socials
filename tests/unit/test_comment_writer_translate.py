"""CommentWriter.translate — the NTS_065 EN→target translation pass.

The LLM round-trip is mocked; we assert on the prompt the writer would send
and on how it post-processes the response. End-to-end fidelity (real model
output) is covered offline by the backfill verifier, not here.
"""
# ruff: noqa: RUF001 — fixtures mix Cyrillic with ASCII figures/markdown on
# purpose (real translated bodies); the "ambiguous character" lint is noise here.

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pipeline.common.models import Draft, Language
from pipeline.generator.comment_writer import CommentWriter
from pipeline.generator.translation_check import (
    h2_count,
    has_markdown_in_title,
    invented_numbers,
)


def _chat_resp(payload: dict[str, Any]) -> Any:
    class _Msg:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Choice:
        def __init__(self, content: str) -> None:
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content: str) -> None:
            self.choices = [_Choice(content)]

    return _Resp(json.dumps(payload))


def _en_draft() -> Draft:
    return Draft(
        topic_id="t-1",
        brand_id="icon",
        language=Language.en,
        title="The repricing of mezzanine credit",
        body=(
            "Icon sees a shift.\n\n"
            "## The repricing\n\n"
            "A $2.4m allocation moved into 3 funds, up 67%.\n\n"
            "## What changes next\n\nBase rates held at 50bp.\n"
        ),
        key_takeaway="Allocators should revisit yield assumptions.",
    )


def test_translate_rejects_english_target():
    writer = CommentWriter(client=AsyncMock())
    with pytest.raises(ValueError, match="canonical"):
        import asyncio

        asyncio.run(writer.translate(_en_draft(), Language.en, "banned_phrases: []\n"))


@pytest.mark.parametrize(
    "language,expected_name",
    [
        (Language.ru, "Russian"),
        (Language.uk, "Ukrainian"),
        (Language.pl, "Polish"),
    ],
)
async def test_translate_prompt_carries_language_and_source(language, expected_name):
    ru_payload = {
        "title": "Переоценка мезонинного кредита",
        "body": (
            "## Переоценка\n\n$2,4 млн ушли в 3 фонда, рост 67%.\n\n"
            "## Что дальше\n\nСтавки на уровне 50bp.\n"
        ),
        "key_takeaway": "Пересмотрите допущения.",
    }
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_chat_resp(ru_payload)]
    )
    writer = CommentWriter(client=client)
    en = _en_draft()
    await writer.translate(en, language, "banned_phrases: []\n")

    assert client.chat.completions.create.await_count == 1
    call = client.chat.completions.create.await_args_list[0]
    # Faithful translation runs on the gpt-4o-class polish model, not mini.
    assert call.kwargs["model"] == writer.polish_model == "gpt-4o"
    prompt = call.kwargs["messages"][0]["content"]
    assert f"OUTPUT LANGUAGE: {expected_name}" in prompt
    # The canonical EN body + title must be embedded as the source.
    assert "The repricing of mezzanine credit" in prompt
    assert "$2.4m" in prompt
    # Prompt must forbid inventing/dropping facts.
    assert "Do NOT invent" in prompt


async def test_translate_returns_target_language_draft_and_sanitizes_title():
    payload = {
        # Model leaks a stray "## " into the title — must be stripped.
        "title": "## Переоценка мезонинного кредита",
        "body": (
            "## Переоценка\n\n$2,4 млн ушли в 3 фонда, рост 67%.\n\n"
            "## Что дальше\n\nСтавки на уровне 50bp.\n"
        ),
        "key_takeaway": "Пересмотрите допущения.",
    }
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_chat_resp(payload)])
    writer = CommentWriter(client=client)
    en = _en_draft()

    out = await writer.translate(en, Language.ru, "banned_phrases: []\n")

    assert out.language == Language.ru
    assert out.topic_id == en.topic_id
    assert out.brand_id == en.brand_id
    # sanitize_title (NTS_060) ran: no leading markdown survives.
    assert not has_markdown_in_title(out.title)
    assert out.title == "Переоценка мезонинного кредита"
    # Structure + fact parity preserved relative to EN.
    assert h2_count(out.body) == h2_count(en.body) == 2
    assert invented_numbers(en.body, out.body) == []
