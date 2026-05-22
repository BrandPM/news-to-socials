"""Two-stage commentary writer (ADR-013, OpenAI-only — ADR-017).

Stage 1 -- gpt-4o-mini produces a fast first draft against the brand
voice-profile.
Stage 2 -- gpt-4o polishes the draft, targeting natural prose and
removing AI tells. The polish prompt is informed by what the anti-AI check
found in the draft.

Why two stages: gpt-4o-mini gives us speed and cost (~$0.001/post in our
volumes), but tends to fall into patterns. gpt-4o costs ~17x more but only
runs once per accepted topic, so the bill stays bounded.

Pattern reference: fin-thread composes news in a single OpenAI call
(/research/fin-thread/composer/composer.go). We deliberately split into two
because their output (1-2 sentence headline) and ours (200-400 word original
commentary) have very different quality requirements.

When to revisit: if gpt-4o output starts feeling generic, swap polish_model
to "gpt-4o-2024-11-20" or the latest snapshot. The constants below are the
only place that knows which model is in use.
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from openai import AsyncOpenAI
from pydantic import BaseModel

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Draft, Language, Topic
from ..common.retry import with_retry
from .anti_ai_check import find_banned_phrase_hits, score_ai_tells

log = get_logger(__name__)


class _DraftJSON(BaseModel):
    """Wire format we ask the model to return."""

    title: str
    body: str
    key_takeaway: str = ""


_DRAFT_PROMPT = """\
You write an original expert commentary from the brand below on the news peg.
This is NOT a rewrite. The news peg is just an excuse to share the brand's
informed perspective.

Brand voice profile (YAML):
{voice_profile_yaml}

News peg:
  Title: {title}
  Source URL: {url}
  Summary: {summary}

Language: {language}
Audience: people in the brand's target segment, NOT general public.
Length: 250-400 words.

STRUCTURE REQUIREMENTS (mandatory — markdown headings, not bold):
* Open with a 1-2 sentence lede paragraph that names the specific
  consequence, NOT a general framing. No heading on the lede.
* Then 2-3 H2 sections (`## Heading`). Heading names must be
  substantive and describe the actual content
  (e.g. "## The repricing of mezzanine credit", NOT
  "## What this means" or "## Key takeaways").
* End with a forward-looking close that names what changes for the
  reader's next decision. No "in conclusion"-style restatement.
* Source quotes ≤ 15 words. The piece is commentary, not a rewrite.

Rules:
* Strictly follow the voice profile.
* Original perspective, not a summary of the article.
* No filler phrases ("moreover", "furthermore", "it's important to note").
* Em-dashes used sparingly.

Return ONLY a JSON object: {{"title": "...", "body": "...", "key_takeaway": "..."}}
where ``body`` is markdown including the H2 headings.
"""


_POLISH_PROMPT = """\
Rewrite this draft to sound more natural and less AI-generated, preserving
its meaning. Pay special attention to these tells found in the draft:
{ai_tells}

STRUCTURE REQUIREMENTS (preserve / enforce — markdown, not bold):
* The piece must open with a 1-2 sentence lede paragraph (no heading).
* Then 2-3 H2 sections (`## Heading`). Substantive heading names that
  describe the section content, e.g. "## The repricing of mezzanine
  credit" — NEVER "## What this means", "## Conclusion", "## Key
  takeaways", "## Overview", or other content-free labels.
* End with a forward-looking close: what changes for the reader's
  next decision. No "in conclusion" restatement.
* If the draft is one flat block, restructure it into lede + H2
  sections + close while keeping the meaning.

VOICE GUARDRAILS:
* Banned phrases — do NOT use any of these (case-insensitive):
{banned_phrases}
* Examples of the voice we want (mirror this register and cadence):
{good_examples}

Draft:
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
where ``body`` is markdown with H2 headings.
"""


_BANNED_PHRASE_RETRY_PROMPT = """\
The previous polish still uses the following banned phrases:
{hit_phrases}

Rewrite the draft to remove ALL of these phrases. Preserve meaning,
structure (lede + 2-3 H2 sections + forward-looking close), and the
voice. Use specific concrete language instead of these clichés.

VOICE GUARDRAILS (still apply):
* Banned phrases — do NOT use any of these (case-insensitive):
{banned_phrases}
* Examples of the voice we want:
{good_examples}

Draft:
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
"""


_BANNED_PHRASE_RETRY_THRESHOLD = 2


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"  - {x}" for x in items)


def _record_openai_cost(resp: Any, *, model: str, operation: str) -> None:
    """Record a cost_records row for an OpenAI ChatCompletion response.

    Safe to call with mocks: missing ``usage`` → cost is 0, row still
    written iff a brand context is active (otherwise no-op). NTS_025 C1.
    """
    from pipeline.admin.cost_recorder import record_cost  # noqa: PLC0415
    from pipeline.common.pricing import openai_cost  # noqa: PLC0415

    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None)
    tokens_out = getattr(usage, "completion_tokens", None)
    record_cost(
        provider="openai",
        operation=operation,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=openai_cost(model, tokens_in, tokens_out),
    )


def parse_voice_guardrails(voice_profile_yaml: str) -> tuple[list[str], list[str]]:
    """Extract ``banned_phrases`` and ``style_examples.good`` from the YAML.

    Returns ``(banned_phrases, good_examples)``. Missing keys → empty lists.
    Malformed YAML is logged and treated as no guardrails so a bad voice
    profile never crashes the pipeline.
    """
    try:
        data = yaml.safe_load(voice_profile_yaml) or {}
    except yaml.YAMLError as exc:
        log.warning("comment_writer.voice_profile_parse_failed", err=str(exc))
        return [], []

    banned = data.get("banned_phrases") or []
    examples = data.get("style_examples") or []
    good: list[str] = []
    # ``style_examples`` was historically a flat list; current form is
    # ``{good: [...], bad: [...]}``. Support both.
    if isinstance(examples, dict):
        good = list(examples.get("good") or [])
    elif isinstance(examples, list):
        good = list(examples)

    banned = [str(p) for p in banned if p]
    good = [str(g) for g in good if g]
    return banned, good


class CommentWriter:
    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        draft_model: str = "gpt-4o-mini",
        polish_model: str = "gpt-4o",
    ) -> None:
        self.client = client or AsyncOpenAI(api_key=get_settings().openai_api_key)
        self.draft_model = draft_model
        self.polish_model = polish_model

    async def write(
        self,
        topic: Topic,
        voice_profile_yaml: str,
        language: Language,
    ) -> Draft:
        banned_phrases, good_examples = parse_voice_guardrails(voice_profile_yaml)

        # --- Stage 1: draft ---
        draft = await self._draft(topic, voice_profile_yaml, language)

        # --- Anti-AI check ---
        score, tells = score_ai_tells(draft.body)
        log.info("comment_writer.draft_ai_score", topic=topic.id, score=score, tells=tells)

        # --- Stage 2: polish (always; injects voice guardrails) ---
        polished = await self._polish(draft, tells, banned_phrases, good_examples)

        # --- Banned-phrase retry (one extra pass, cap to avoid runaways) ---
        hits = find_banned_phrase_hits(polished.body, banned_phrases)
        if len(hits) > _BANNED_PHRASE_RETRY_THRESHOLD:
            log.info(
                "comment_writer.banned_phrase_retry",
                topic=topic.id,
                hits=hits,
            )
            polished = await self._retry_for_banned_phrases(
                polished, hits, banned_phrases, good_examples
            )
            remaining = find_banned_phrase_hits(polished.body, banned_phrases)
            log.info(
                "comment_writer.banned_phrase_retry_done",
                topic=topic.id,
                remaining=remaining,
            )

        return Draft(
            topic_id=topic.id,
            brand_id=topic.brand_id,
            language=language,
            title=polished.title,
            body=polished.body,
            key_takeaway=polished.key_takeaway,
        )

    @with_retry()
    async def _draft(
        self, topic: Topic, voice_profile_yaml: str, language: Language
    ) -> _DraftJSON:
        prompt = _DRAFT_PROMPT.format(
            voice_profile_yaml=voice_profile_yaml,
            title=topic.raw.title,
            url=topic.raw.url,
            summary=topic.raw.summary[:1000],
            language=language.value,
        )
        resp = await self.client.chat.completions.create(
            model=self.draft_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(resp, model=self.draft_model, operation="draft")
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _polish(
        self,
        draft: _DraftJSON,
        ai_tells: list[str],
        banned_phrases: list[str],
        good_examples: list[str],
    ) -> _DraftJSON:
        prompt = _POLISH_PROMPT.format(
            ai_tells=", ".join(ai_tells) if ai_tells else "no specific tells noted",
            banned_phrases=_bullet_list(banned_phrases) or "  (none specified)",
            good_examples=_bullet_list(good_examples) or "  (none specified)",
            draft_json=draft.model_dump_json(),
        )
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(resp, model=self.polish_model, operation="polish")
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _retry_for_banned_phrases(
        self,
        draft: _DraftJSON,
        hit_phrases: list[str],
        banned_phrases: list[str],
        good_examples: list[str],
    ) -> _DraftJSON:
        prompt = _BANNED_PHRASE_RETRY_PROMPT.format(
            hit_phrases=_bullet_list(hit_phrases),
            banned_phrases=_bullet_list(banned_phrases),
            good_examples=_bullet_list(good_examples) or "  (none specified)",
            draft_json=draft.model_dump_json(),
        )
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(
            resp, model=self.polish_model, operation="anti_check_retry"
        )
        return self._parse(resp.choices[0].message.content or "{}")

    @staticmethod
    def _parse(text: str) -> _DraftJSON:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
            return _DraftJSON.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            log.error("comment_writer.parse_failed", raw=text[:200], err=str(exc))
            return _DraftJSON(title="(parse failed)", body=text[:800], key_takeaway="")
