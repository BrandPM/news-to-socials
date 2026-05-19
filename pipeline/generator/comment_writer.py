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

from openai import AsyncOpenAI
from pydantic import BaseModel

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Draft, Language, Topic
from ..common.retry import with_retry
from .anti_ai_check import score_ai_tells

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
Structure: hook headline (1 line), 2-4 paragraphs, one-sentence takeaway.

Rules:
* Strictly follow the voice profile.
* Original perspective, not a summary of the article.
* No filler phrases ("moreover", "furthermore", "it's important to note").
* Em-dashes used sparingly.

Return ONLY a JSON object: {{"title": "...", "body": "...", "key_takeaway": "..."}}
"""


_POLISH_PROMPT = """\
Rewrite this draft to sound more natural and less AI-generated, preserving
its meaning and structure. Pay special attention to: {ai_tells}

Draft:
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
"""


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
        # --- Stage 1: draft ---
        draft = await self._draft(topic, voice_profile_yaml, language)

        # --- Anti-AI check ---
        score, tells = score_ai_tells(draft.body)
        log.info("comment_writer.draft_ai_score", topic=topic.id, score=score, tells=tells)

        # --- Stage 2: polish (always -- gpt-4o improves gpt-4o-mini's prose
        # even when AI-tell score is borderline) ---
        polished = await self._polish(draft, tells)

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
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _polish(self, draft: _DraftJSON, ai_tells: list[str]) -> _DraftJSON:
        prompt = _POLISH_PROMPT.format(
            ai_tells=", ".join(ai_tells) if ai_tells else "no specific tells noted",
            draft_json=draft.model_dump_json(),
        )
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
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
