"""Pydantic request/response schemas for the admin API.

We keep these separate from the SQLAlchemy models in ``models.py`` for
two reasons:

1. The wire format diverges from the storage format (JSON columns get
   parsed into lists/dicts; timestamps go to ISO strings).
2. Pydantic's validators run on every request, so a bad payload fails
   fast at the route boundary instead of bubbling up as an ORM error
   halfway through the handler.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

SourceType = Literal["rss", "web", "telegram"]
PromptType = Literal["writer_polish", "writer_draft", "topic_picker", "image_prompt"]
RunStatus = Literal["running", "success", "failed"]
TopicStatus = Literal[
    "passed", "filtered_banned", "filtered_dup", "filtered_score", "failed"
]


# --- Source --------------------------------------------------------------


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: str
    name: str
    source_type: SourceType
    url: HttpUrl
    primary_category: str
    active: bool = True
    paywall: bool = False
    polling_minutes: int = Field(default=720, ge=1, le=10080)
    credentials: str | None = None
    custom_parser: str | None = None


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    source_type: SourceType | None = None
    url: HttpUrl | None = None
    primary_category: str | None = None
    active: bool | None = None
    paywall: bool | None = None
    polling_minutes: int | None = Field(default=None, ge=1, le=10080)
    credentials: str | None = None
    custom_parser: str | None = None


class SourceOut(BaseModel):
    id: int
    brand_id: str
    name: str
    source_type: SourceType
    url: str
    primary_category: str
    active: bool
    paywall: bool
    polling_minutes: int
    credentials: str | None
    custom_parser: str | None
    last_run_at: datetime | None
    last_run_stats: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @field_validator("last_run_stats", mode="before")
    @classmethod
    def _parse_stats(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else None
        return v


class SourceTestOut(BaseModel):
    parser_status: Literal["ok", "error"]
    headlines: list[dict[str, str]]
    error: str | None = None


class RunTriggerOut(BaseModel):
    run_id: int


# --- Prompt --------------------------------------------------------------


class PromptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: str
    prompt_type: PromptType
    version_name: str
    content: str
    notes: str | None = None


class PromptOut(BaseModel):
    id: int
    brand_id: str
    prompt_type: PromptType
    version_name: str
    content: str
    notes: str | None
    is_active: bool
    created_by: str
    created_at: datetime
    test_results: dict[str, Any] | None

    @field_validator("test_results", mode="before")
    @classmethod
    def _parse_results(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else None
        return v


class PromptTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_topic_id: str | None = None


class PromptTestOut(BaseModel):
    generated_text: str
    cost_usd: float
    ai_tells_count: int


# --- Pipeline config ----------------------------------------------------


class PipelineConfigOut(BaseModel):
    brand_id: str
    scoring_threshold: int
    topics_per_run: int
    banned_phrases: list[str]
    voice_profile: str
    updated_at: datetime

    @field_validator("banned_phrases", mode="before")
    @classmethod
    def _parse_banned(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else []
        return v


class PipelineConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scoring_threshold: int | None = Field(default=None, ge=1, le=10)
    topics_per_run: int | None = Field(default=None, ge=1, le=10)
    banned_phrases: list[str] | None = None
    voice_profile: str | None = None


# --- Runs ---------------------------------------------------------------


class TopicOut(BaseModel):
    id: int
    topic_id: str
    source_id: int
    title: str
    url: str | None
    score: int | None
    status: TopicStatus
    filter_reason: str | None
    draft_id: str | None
    created_at: datetime


class RunOut(BaseModel):
    id: int
    brand_id: str
    triggered_by: str
    source_ids: list[int]
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    stats: dict[str, Any] | None
    log_excerpt: str | None

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_source_ids(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("stats", mode="before")
    @classmethod
    def _parse_stats(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else None
        return v


class RunDetailOut(BaseModel):
    run: RunOut
    topics: list[TopicOut]


class RunLogOut(BaseModel):
    log: str
    source: Literal["file", "stub"]


# --- Drafts / image regenerate ------------------------------------------


class ImageRegenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_prompt: str | None = None


class JobAcceptedOut(BaseModel):
    job_id: str


class JobStatusOut(BaseModel):
    state: Literal["pending", "done", "error"]
    asset_id: str | None = None
    error: str | None = None
