"""Topic picker: score raw items 0-10 for a brand via OpenAI gpt-4o-mini.

Why gpt-4o-mini and not gpt-4o here: this is a high-frequency filter,
called dozens of times per polling cycle. gpt-4o-mini is ~17x cheaper
($0.15/1M input vs $2.50/1M for gpt-4o) and the task (classification,
no nuanced writing) is well within its capability. The expensive gpt-4o
pass happens later in ``generator.comment_writer``.

Pattern reference: fin-thread's ``Filter`` step does the same conceptual job
(/research/fin-thread/composer/composer.go), but on a different scale --
they filter "is this even financial news", we score "how well does this fit
brand X". So we drive the prompt with brand context, not just a topic list.

See ADR-013 (two-stage LLM) and ADR-017 (OpenAI-only stack).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openai import AsyncOpenAI

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import RawItem
from ..common.retry import with_retry

log = get_logger(__name__)


@dataclass(frozen=True)
class BrandContext:
    """Just enough about a brand for the relevance step."""

    brand_id: str
    name: str
    topics_relevant: list[str]
    topics_banned: list[str]


_PROMPT = """\
You score how relevant a news item is to a brand for content marketing.

Brand: {brand_name}
Relevant topics: {topics_relevant}
Banned topics: {topics_banned}

News:
  Title: {title}
  Summary: {summary}

Return a JSON object: {{"score": <integer 0-10>, "reason": "<10 words max>"}}.
* 10 = a perfect peg for an original expert commentary from this brand
* 7  = clearly relevant
* 4  = tangentially related
* 0  = unrelated or in banned-topics

Respond with ONLY the JSON object, no markdown, no preamble.
"""


class TopicPicker:
    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self.client = client or AsyncOpenAI(api_key=get_settings().openai_api_key)
        self.model = model

    async def score(self, item: RawItem, brand: BrandContext) -> tuple[int, str]:
        prompt = _PROMPT.format(
            brand_name=brand.name,
            topics_relevant=", ".join(brand.topics_relevant) or "(none specified)",
            topics_banned=", ".join(brand.topics_banned) or "(none specified)",
            title=item.title[:200],
            summary=(item.summary or "")[:600],
        )
        score, reason = await self._call(prompt)
        log.info(
            "topic_picker.score",
            brand=brand.brand_id,
            url=str(item.url),
            score=score,
            reason=reason,
        )
        return score, reason

    @with_retry()
    async def _call(self, prompt: str) -> tuple[int, str]:
        resp = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=80,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        # Per NTS_025 C1: record cost for every paid call (no-op if no
        # cost context is active — see pipeline.admin.cost_recorder).
        from pipeline.admin.cost_recorder import record_cost  # noqa: PLC0415
        from pipeline.common.pricing import openai_cost  # noqa: PLC0415

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)
        record_cost(
            provider="openai",
            operation="topic_scoring",
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=openai_cost(self.model, tokens_in, tokens_out),
        )

        text = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(text)
            return int(data.get("score", 0)), str(data.get("reason", ""))[:80]
        except (json.JSONDecodeError, ValueError, TypeError):
            log.warning("topic_picker.parse_failed", raw=text[:200])
            return 0, "parse_failed"
