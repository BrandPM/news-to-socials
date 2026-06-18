"""Unit tests for generator/comment_writer.py.

Covers IT_PROJ_NTS_013 fixes:
* Defect 4B — voice guardrails parsed from YAML and injected into polish prompt.
* Defect 4C — banned-phrase detection triggers one retry pass.

The LLM round-trip itself is mocked; we verify the orchestration (which
prompts were sent, when retry fires) not the model's output quality.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from pipeline.common.models import Language, RawItem, Topic
from pipeline.generator.anti_ai_check import find_banned_phrase_hits
from pipeline.generator.comment_writer import (
    CommentWriter,
    parse_voice_guardrails,
    sanitize_title,
)


# --- find_banned_phrase_hits -----------------------------------------


def test_find_banned_phrase_hits_basic() -> None:
    text = "In today's fast-paced world, we navigate the landscape together."
    hits = find_banned_phrase_hits(
        text, ["in today's fast-paced", "navigate the landscape", "moreover"]
    )
    assert "in today's fast-paced" in hits
    assert "navigate the landscape" in hits
    assert "moreover" not in hits


def test_find_banned_phrase_hits_case_insensitive() -> None:
    hits = find_banned_phrase_hits("Moreover, things changed.", ["moreover"])
    assert hits == ["moreover"]


def test_find_banned_phrase_hits_empty_banned_list() -> None:
    assert find_banned_phrase_hits("anything", []) == []


def test_find_banned_phrase_hits_no_match() -> None:
    assert find_banned_phrase_hits("clean prose here.", ["moreover", "furthermore"]) == []


# --- parse_voice_guardrails ------------------------------------------


def test_parse_voice_guardrails_modern_yaml() -> None:
    yaml_str = (
        "banned_phrases:\n"
        "  - moreover\n"
        "  - in today's fast-paced\n"
        "style_examples:\n"
        "  good:\n"
        "    - \"A 50bp move is not the story.\"\n"
        "    - \"Trust planning fails on family.\"\n"
        "  bad:\n"
        "    - \"Moreover, the landscape evolves.\"\n"
    )
    banned, good = parse_voice_guardrails(yaml_str)
    assert "moreover" in banned
    assert "in today's fast-paced" in banned
    assert len(good) == 2
    assert good[0].startswith("A 50bp move")


def test_parse_voice_guardrails_legacy_flat_examples() -> None:
    """Old YAMLs had ``style_examples`` as a flat list. Still supported."""
    yaml_str = (
        "banned_phrases:\n"
        "  - moreover\n"
        "style_examples:\n"
        "  - \"flat example one\"\n"
        "  - \"flat example two\"\n"
    )
    banned, good = parse_voice_guardrails(yaml_str)
    assert banned == ["moreover"]
    assert good == ["flat example one", "flat example two"]


def test_parse_voice_guardrails_missing_keys() -> None:
    banned, good = parse_voice_guardrails("mission: anything\n")
    assert banned == []
    assert good == []


def test_parse_voice_guardrails_malformed_yaml() -> None:
    banned, good = parse_voice_guardrails(":\n:\n  - bad\nindent")
    assert banned == []
    assert good == []


# --- CommentWriter — banned-phrase retry --------------------------


def _make_topic() -> Topic:
    return Topic(
        id="t-1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="s",
            url="https://example.com/x",
            title="A test news item",
            summary="A short summary.",
        ),
        relevance_score=8.0,
    )


def _voice_yaml() -> str:
    return (
        "banned_phrases:\n"
        "  - moreover\n"
        "  - furthermore\n"
        "  - in conclusion\n"
        "  - it is important to note\n"
        "style_examples:\n"
        "  good:\n"
        "    - \"Short, concrete prose.\"\n"
    )


def _chat_resp(payload: dict[str, Any]) -> Any:
    """Minimal stand-in for an OpenAI ChatCompletion response."""

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


async def test_write_triggers_retry_when_polish_returns_banned_phrases() -> None:
    """If polish output contains > _BANNED_PHRASE_RETRY_THRESHOLD banned
    phrases, CommentWriter fires one retry pass."""
    draft_payload = {
        "title": "Draft title",
        "body": "## Section A\n\nDraft body, clean.",
        "key_takeaway": "Takeaway.",
    }
    dirty_polish = {
        "title": "Polished title",
        "body": (
            "## Section A\n\n"
            "Moreover, the proposal matters. Furthermore, it lands hard. "
            "In conclusion, allocators should look closely."
        ),
        "key_takeaway": "Takeaway.",
    }
    clean_retry = {
        "title": "Retry title",
        "body": "## Section A\n\nA concrete sentence with no clichés.",
        "key_takeaway": "Takeaway.",
    }

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[
            _chat_resp(draft_payload),   # stage 1
            _chat_resp(dirty_polish),    # stage 2 (polish)
            _chat_resp(clean_retry),     # stage 2b (banned-phrase retry)
        ]
    )

    writer = CommentWriter(client=client)
    out = await writer.write(_make_topic(), _voice_yaml(), Language.en)

    assert client.chat.completions.create.await_count == 3
    assert out.body == clean_retry["body"]
    # The retry's polish should have removed the banned phrases.
    assert "moreover" not in out.body.lower()
    assert "furthermore" not in out.body.lower()


async def test_write_skips_retry_when_polish_is_clean() -> None:
    draft_payload = {
        "title": "Draft",
        "body": "## H\n\nClean draft.",
        "key_takeaway": "T",
    }
    clean_polish = {
        "title": "Polished",
        "body": "## H\n\nThe proposal moves the discussion, not the timeline.",
        "key_takeaway": "T",
    }

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_chat_resp(draft_payload), _chat_resp(clean_polish)]
    )

    writer = CommentWriter(client=client)
    out = await writer.write(_make_topic(), _voice_yaml(), Language.en)

    assert client.chat.completions.create.await_count == 2
    assert out.body == clean_polish["body"]


async def test_write_does_not_retry_more_than_once() -> None:
    """Cap at one retry — protects cost / wall-clock when the model is stubborn."""
    draft_payload = {
        "title": "D",
        "body": "## H\n\nDraft body.",
        "key_takeaway": "T",
    }
    dirty = {
        "title": "P",
        "body": (
            "Moreover, A. Furthermore, B. In conclusion, C. "
            "It is important to note, D."
        ),
        "key_takeaway": "T",
    }

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_chat_resp(draft_payload), _chat_resp(dirty), _chat_resp(dirty)]
    )

    writer = CommentWriter(client=client)
    await writer.write(_make_topic(), _voice_yaml(), Language.en)

    # 3 calls: draft + polish + 1 retry. No 4th call even though body is still dirty.
    assert client.chat.completions.create.await_count == 3


# --- sanitize_title (IT_PROJ_NTS_060) --------------------------------
#
# Translation/polish passes leak markdown into the title field ("## Foo").
# sanitize_title is the last line of defence before persistence.


def test_sanitize_title_strips_h2() -> None:
    assert sanitize_title("## The Shifting Landscape") == "The Shifting Landscape"


def test_sanitize_title_strips_h1() -> None:
    assert sanitize_title("# A Clean Headline") == "A Clean Headline"


def test_sanitize_title_strips_bold() -> None:
    assert sanitize_title("**Bold Title**") == "Bold Title"


def test_sanitize_title_strips_leading_space_and_hashes() -> None:
    assert sanitize_title("  ##  Spaced Heading") == "Spaced Heading"


def test_sanitize_title_clean_unchanged() -> None:
    clean = "The Shifting Landscape of Tax Advisory Services"
    assert sanitize_title(clean) == clean


def test_sanitize_title_real_prod_examples() -> None:
    # The PL/RU/UK titles that shipped with markdown on topic 0f7c49edcb.
    assert (
        sanitize_title("## Klienci stawiają na strategię w doradztwie podatkowym")
        == "Klienci stawiają na strategię w doradztwie podatkowym"
    )
    assert (
        sanitize_title("## Совершенствование стратегической экспертизы через ИИ")
        == "Совершенствование стратегической экспертизы через ИИ"
    )


def test_sanitize_title_multiple_hashes() -> None:
    assert sanitize_title("#### Deep Heading") == "Deep Heading"


def test_sanitize_title_strips_backticks() -> None:
    assert sanitize_title("`code title`") == "code title"


def test_sanitize_title_strips_list_marker() -> None:
    assert sanitize_title("- A bullet title") == "A bullet title"


def test_sanitize_title_inner_bold_preserved() -> None:
    # Emphasis in the middle is unwrapped, surrounding text kept.
    assert sanitize_title("Why **mezzanine** repriced") == "Why mezzanine repriced"


def test_sanitize_title_does_not_eat_hash_number() -> None:
    # "#1" is not a heading (no space) — must survive.
    assert sanitize_title("#1 ranking shift") == "#1 ranking shift"


def test_sanitize_title_empty() -> None:
    assert sanitize_title("") == ""
    assert sanitize_title("   ") == ""


def test_sanitize_title_is_idempotent() -> None:
    once = sanitize_title("## ** Stacked ** markup")
    assert sanitize_title(once) == once


# --- pipeline path runs the title through the sanitizer --------------


async def test_write_sanitizes_markdown_title_from_polish() -> None:
    """End-to-end: a polish stage that returns a "## " title yields a clean
    Draft.title. Proves the pipeline path runs every title through
    sanitize_title before it can reach Sanity / the admin DB."""
    draft_payload = {
        "title": "## Draft markdown title",
        "body": "## Section A\n\nClean draft body.",
        "key_takeaway": "T",
    }
    polish_payload = {
        "title": "## Polished markdown title",
        "body": "## Section A\n\nThe proposal moves the discussion.",
        "key_takeaway": "T",
    }

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_chat_resp(draft_payload), _chat_resp(polish_payload)]
    )

    writer = CommentWriter(client=client)
    out = await writer.write(_make_topic(), _voice_yaml(), Language.uk)

    assert out.title == "Polished markdown title"
    assert not out.title.startswith("#")
