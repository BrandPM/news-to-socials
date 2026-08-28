"""Canonical data models used across the pipeline.

Keep these small and **stable**. Anything brand- or channel-specific belongs
in the relevant submodule, not here.

Naming aligns with our Master Documentation §3 and §12 glossary:
* Source  — RSS / Telegram / web origin (config in admin.db ``sources``)
* RawItem — what a Source returns
* Topic   — a RawItem that passed the relevance + dedup gate
* Draft   — LLM output (title + body + key takeaway), pre-format

``Post``/``PostStatus`` used to sit here as "formatted for one channel". Both
were removed in NTS_121 §7 together with the per-channel adapters and the
publish queue: after the ADR-018 pivot, a draft goes to Sanity and the manager
publishes it — nothing formats a Post.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceType(StrEnum):
    rss = "rss"
    telegram = "telegram"
    web = "web"


class Language(StrEnum):
    ru = "ru"
    uk = "uk"
    en = "en"
    pl = "pl"


class Channel(StrEnum):
    """Output surface. Only ``blog`` is reachable today.

    The other three are kept because ``image_resizer.TARGETS`` still carries
    their aspect ratios and a cover is generated once at master size — the
    ratios are cheap, correct and referenced. The publishers behind them are
    not: ``meta_graph``, ``pipeline.adapter`` and ``pipeline.queue`` were
    removed in NTS_121 §7 as dead since the ADR-018 pivot to Sanity.
    """

    blog = "blog"
    telegram = "telegram"
    facebook = "facebook"
    instagram = "instagram"


class RawItem(BaseModel):
    """Raw item fetched from a source. Untrusted, may be filtered out later."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    source_name: str
    url: HttpUrl
    title: str
    summary: str = ""
    raw_html: str = ""
    published_at: datetime | None = None


class Topic(BaseModel):
    """A RawItem that survived relevance scoring and dedup."""

    id: str
    brand_id: str
    raw: RawItem
    relevance_score: float = Field(ge=0.0, le=10.0)
    embedding: list[float] | None = None
    entities: list[str] = Field(default_factory=list)
    # NTS_098 §1 — the portfolio row this topic is being produced for. The seam
    # between contour 1 and generation: when the S4 production path selects a
    # candidate it puts the id here, and the generator links the Sanity draft
    # back to it the moment the draft exists (``candidate_lifecycle``). ``None``
    # on the v2 path, where topics come straight off a feed and no candidate
    # exists — which is why 337 candidates had zero draft links.
    candidate_id: int | None = None


class Draft(BaseModel):
    """LLM output before channel-specific formatting."""

    topic_id: str
    brand_id: str
    language: Language
    title: str
    body: str
    key_takeaway: str = ""

    # populated after image generation
    image_url: HttpUrl | None = None
    image_alt: str = ""
