"""IT_PROJ_NTS_070 — manager-feedback quality merge into draft/polish + voice.

Asserts the SELECTED rules landed in both EN-canon prompts, the REJECTED ones
did not, polish now renders topics_relevant (AUDIENCE LINK), and the new EN
banned phrases are caught.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock

import pytest


def _flat(s: str) -> str:
    """Collapse the prompt's manual line-wrapping so multi-line clauses match."""
    return re.sub(r"\s+", " ", s)

from pipeline.common.models import Language, RawItem, Topic
from pipeline.generator.anti_ai_check import find_banned_phrase_hits
from pipeline.generator.comment_writer import (
    _DRAFT_PROMPT,
    _POLISH_PROMPT,
    CommentWriter,
    parse_topics_relevant,
    parse_voice_guardrails,
)
from pipeline.run import icon_brand_config

NEW_EN_BANNED = [
    "growing uncertainty",
    "rising uncertainty",
    "significant impact",
    "immediate action required",
    "potential conflict",
    "each case is different",
    "it is important to",
    "plays a crucial role",
    "when it comes to",
]


# --- selected rules present in BOTH prompts -------------------------------


@pytest.mark.parametrize("prompt", [_DRAFT_PROMPT, _POLISH_PROMPT])
def test_selected_blocks_present(prompt):
    assert "NO REPETITION & DENSITY" in prompt
    assert "AUDIENCE LINK" in prompt
    assert "topics_relevant" in prompt
    # NTS_092 renamed the NO INVENTION block to GROUNDING and widened it
    # (extrapolation, "sensible" rounding, unit conversion, misattribution)
    # and promoted it above SPECIFICITY. The NTS_070 rule it superseded is
    # still enforced, so assert the invariant rather than the old label.
    assert "GROUNDING" in prompt
    assert "Never invent" in prompt
    assert "Your own knowledge is NOT a source." in prompt
    assert "So what does this mean specifically?" in _flat(prompt)
    # tighten-not-gut (softened "remove removable") — not the absolute form.
    assert "tighten, do not gut" in prompt


@pytest.mark.parametrize("prompt", [_DRAFT_PROMPT, _POLISH_PROMPT])
def test_rejected_rules_absent(prompt):
    low = prompt.lower()
    # language-specific punctuation rules (would break RU/UK/PL) — rejected.
    assert "en-dash" not in low
    assert "25 words" not in low
    assert "comma before" not in low
    # scoring / editorial-policy leakage — rejected from the polish layer.
    assert "should not be published" not in low
    assert "do not publish" not in low


def test_close_merge_is_single_close_not_duplicated():
    # The "so what specifically" clause is merged into the existing anchored
    # close, not added as a second close section.
    for prompt in (_DRAFT_PROMPT, _POLISH_PROMPT):
        assert _flat(prompt).count("So what does this mean specifically?") == 1
        # still exactly one anchored-close bullet (no duplicate close block).
        assert prompt.lower().count("forward-looking close") == 1


# --- parse_topics_relevant ------------------------------------------------


def test_parse_topics_relevant_top_level():
    y = "topics_relevant:\n  - cross-border tax structuring\n  - family office operations\n"
    assert parse_topics_relevant(y) == [
        "cross-border tax structuring",
        "family office operations",
    ]


def test_parse_topics_relevant_missing_empty():
    assert parse_topics_relevant("mission: x\n") == []


# --- polish renders topics_relevant (AUDIENCE LINK wired) -----------------


def _resp(payload: dict[str, Any]) -> Any:
    msg = type("M", (), {"content": json.dumps(payload)})()
    return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": None})()


async def test_polish_prompt_renders_topics_relevant():
    voice = (
        "voice_principles:\n  - One concrete number per paragraph.\n"
        "topics_relevant:\n  - cross-border tax structuring\n  - family office operations\n"
        "banned_phrases: []\n"
    )
    clean = {"title": "T", "body": "## H\n\nClean body paragraph here.", "key_takeaway": "K"}
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_resp(clean), _resp(clean)])
    writer = CommentWriter(client=client)
    topic = Topic(
        id="t-1",
        brand_id="icon",
        raw=RawItem(source_id="s", source_name="s", url="https://e.com/x", title="X"),
        relevance_score=8.0,
    )
    await writer.write(topic, voice, Language.en)

    polish_prompt = client.chat.completions.create.await_args_list[1].kwargs[
        "messages"
    ][0]["content"]
    assert "AUDIENCE LINK" in polish_prompt
    assert "cross-border tax structuring" in polish_prompt
    assert "family office operations" in polish_prompt


# --- new EN banned phrases ------------------------------------------------


def test_new_banned_phrases_caught_by_matcher():
    text = (
        "Growing uncertainty creates significant impact. It is important to "
        "act when it comes to potential conflict."
    )
    hits = find_banned_phrase_hits(text, NEW_EN_BANNED)
    for expected in ["growing uncertainty", "significant impact", "it is important to",
                     "when it comes to", "potential conflict"]:
        assert expected in hits


def test_new_banned_phrases_in_icon_seed_voice():
    """The in-code seed/fallback voice carries the new EN banned phrases and
    the new bad example (keeps the code seed current with prod)."""
    voice = icon_brand_config().voice_profile_yaml
    banned, _ = parse_voice_guardrails(voice, Language.en)
    for p in NEW_EN_BANNED:
        assert p in banned, p
    assert "require immediate action" in voice.lower()
