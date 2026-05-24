"""S6.3 — language-aware draft + polish + retry prompts.

Closes IT_PROJ_NTS_022 (multilingual polish bug). All three prompts in
CommentWriter must inject ``OUTPUT LANGUAGE: <Language Name>`` so the
LLM doesn't silently translate back to English during polish.

We assert against the rendered prompt text the writer would have sent
to the model — the LLM round-trip itself is mocked.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pipeline.common.models import Language, RawItem, Topic
from pipeline.generator.comment_writer import CommentWriter, _language_name


def _make_topic() -> Topic:
    return Topic(
        id="t-1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="s",
            url="https://example.com/x",
            title="Russian bond yields tighten on CB hold",
            summary="Headline summary about Russian fixed income.",
        ),
        relevance_score=8.0,
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


def _clean_payload() -> dict[str, Any]:
    return {
        "title": "T",
        "body": "## H\n\nClean body paragraph here.",
        "key_takeaway": "K",
    }


# --- helper -------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected_name",
    [
        ("en", "English"),
        ("ru", "Russian"),
        ("uk", "Ukrainian"),
        ("pl", "Polish"),
    ],
)
def test_language_name_maps_codes_to_names(code, expected_name):
    assert _language_name(code) == expected_name
    assert _language_name(Language(code)) == expected_name


def test_language_name_falls_back_to_code_for_unknown():
    assert _language_name("xx") == "xx"


# --- draft prompt -------------------------------------------------------


@pytest.mark.parametrize(
    "language,expected_name",
    [
        (Language.en, "English"),
        (Language.ru, "Russian"),
        (Language.uk, "Ukrainian"),
        (Language.pl, "Polish"),
    ],
)
async def test_draft_prompt_includes_output_language(language, expected_name):
    """First message sent to the draft model must include the
    ``OUTPUT LANGUAGE:`` directive with the language *name*, not just the
    2-letter code. The code "ru" routinely produced English output during
    earlier S5 testing — see NTS_022."""
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_chat_resp(_clean_payload()), _chat_resp(_clean_payload())]
    )
    writer = CommentWriter(client=client)
    await writer.write(_make_topic(), "banned_phrases: []\n", language)
    draft_call = client.chat.completions.create.await_args_list[0]
    prompt = draft_call.kwargs["messages"][0]["content"]
    assert f"OUTPUT LANGUAGE: {expected_name}" in prompt
    assert f"Write the title, body, and key" in prompt


# --- polish prompt ------------------------------------------------------


@pytest.mark.parametrize(
    "language,expected_name",
    [
        (Language.en, "English"),
        (Language.ru, "Russian"),
        (Language.uk, "Ukrainian"),
        (Language.pl, "Polish"),
    ],
)
async def test_polish_prompt_includes_output_language(language, expected_name):
    """Second message (polish stage) must also include the directive so
    the polish doesn't translate to English. This is the actual fix for
    NTS_022 — the draft prompt already had a Language hint pre-S6, but
    polish never did, which is where the regression happened."""
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_chat_resp(_clean_payload()), _chat_resp(_clean_payload())]
    )
    writer = CommentWriter(client=client)
    await writer.write(_make_topic(), "banned_phrases: []\n", language)
    polish_call = client.chat.completions.create.await_args_list[1]
    prompt = polish_call.kwargs["messages"][0]["content"]
    assert f"OUTPUT LANGUAGE: {expected_name}" in prompt
    assert "Do NOT translate or shift to English" in prompt


# --- banned-phrase retry prompt -----------------------------------------


async def test_retry_prompt_includes_output_language():
    """When polish triggers a banned-phrase retry, the retry prompt must
    also carry the language directive — otherwise the third hop is the
    one that quietly drifts back to English."""
    dirty_polish = {
        "title": "T",
        "body": (
            "## H\n\n"
            "Moreover, this matters. Furthermore, it lands. "
            "In conclusion, allocators care."
        ),
        "key_takeaway": "K",
    }
    voice = (
        "banned_phrases:\n"
        "  - moreover\n"
        "  - furthermore\n"
        "  - in conclusion\n"
    )
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[
            _chat_resp(_clean_payload()),   # draft
            _chat_resp(dirty_polish),       # polish
            _chat_resp(_clean_payload()),   # retry
        ]
    )
    writer = CommentWriter(client=client)
    await writer.write(_make_topic(), voice, Language.ru)
    assert client.chat.completions.create.await_count == 3
    retry_call = client.chat.completions.create.await_args_list[2]
    prompt = retry_call.kwargs["messages"][0]["content"]
    assert "OUTPUT LANGUAGE: Russian" in prompt
    assert "The rewrite must remain in Russian" in prompt
