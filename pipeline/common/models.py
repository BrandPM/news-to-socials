"""Canonical data models used across the pipeline.

Keep these small and **stable**. Anything brand- or channel-specific belongs
in the relevant submodule, not here.

Naming aligns with our Master Documentation §3 and §12 glossary:
* Source  — RSS / Telegram / web origin (config in admin.db ``sources``)
* RawItem — what a Source returns
* Topic   — a RawItem that passed the relevance + dedup gate
* Draft   — LLM output (title + body + key takeaway), pre-format
* Post    — formatted under a specific channel
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
    blog = "blog"
    telegram = "telegram"
    facebook = "facebook"
    instagram = "instagram"


class PostStatus(StrEnum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    scheduled = "scheduled"
    published = "published"
    failed = "failed"
    rejected = "rejected"


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


class Post(BaseModel):
    """Channel-ready post. One Draft → many Posts (one per channel)."""

    draft_id: str
    brand_id: str
    language: Language
    channel: Channel
    content: str
    image_url: HttpUrl | None = None
    status: PostStatus = PostStatus.draft
    external_post_id: str | None = None
    scheduled_at: datetime | None = None
