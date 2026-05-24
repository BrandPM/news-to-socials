"""Pydantic request/response schemas for the admin API.

We keep these separate from the SQLAlchemy models in ``models.py`` for
two reasons:

1. The wire format diverges from the storage format (JSON columns get
   parsed into lists/dicts; timestamps go to ISO strings).
2. Pydantic's validators run on every request, so a bad payload fails
   fast at the route boundary instead of bubbling up as an ORM error
   halfway through the handler.

After the multi-brand refactor (NTS_025), the wire-format ``brand_id``
field is an integer that maps to ``brands.id``. The model attribute is
``brand_id_fk`` to make the FK relationship obvious in code; the wire
format keeps the friendlier name.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

SourceType = Literal["rss", "web", "telegram"]
PromptType = Literal["writer_polish", "writer_draft", "topic_picker", "image_prompt"]
RunStatus = Literal["running", "success", "failed", "dry_run"]
TopicStatus = Literal[
    "passed", "filtered_banned", "filtered_dup", "filtered_score", "failed"
]


# --- Source --------------------------------------------------------------


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: int
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
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand_id: int = Field(validation_alias="brand_id_fk")
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

    brand_id: int
    prompt_type: PromptType
    version_name: str
    content: str
    notes: str | None = None


class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand_id: int = Field(validation_alias="brand_id_fk")
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


class PromptDiffOut(BaseModel):
    """Side-by-side diff of two prompts (S5 Step 8).

    ``unified_diff`` is the standard --- / +++ / @@ form so the UI can
    render it line-by-line with familiar +/- gutter colors. Both source
    prompts are returned in full so the UI doesn't need a second roundtrip.
    """

    a: PromptOut
    b: PromptOut
    unified_diff: str
    same_brand: bool
    same_prompt_type: bool


class PromptTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_topic_id: str | None = None


class PromptTestOut(BaseModel):
    generated_text: str
    cost_usd: float
    ai_tells_count: int


# --- Pipeline config ----------------------------------------------------


class PipelineConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    brand_id: int = Field(validation_alias="brand_id_fk")
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
    model_config = ConfigDict(from_attributes=True)

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
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand_id: int = Field(validation_alias="brand_id_fk")
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


# --- Brands -------------------------------------------------------------


BrandStatus = Literal["draft", "active", "paused", "archived"]


class BrandSummary(BaseModel):
    """Wire format for brand list — NO sensitive credential fields."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    language: str
    timezone: str
    status: BrandStatus
    active: bool


class BrandIn(BaseModel):
    """Payload for POST /brands. Sensitive fields are encrypted at insert."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    name: str
    language: str = "en"
    timezone: str = "Europe/Madrid"
    sanity_project_id: str | None = None
    sanity_dataset: str | None = None
    sanity_api_version: str | None = "2024-01-01"
    sanity_api_token: str | None = None
    sanity_studio_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_channel_id: str | None = None
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_access_token: str | None = None
    meta_page_id: str | None = None
    meta_ig_business_id: str | None = None
    voice_profile_yaml: str | None = None


class BrandUpdate(BaseModel):
    """Payload for PUT /brands/{id}.

    Credential fields follow preserve/clear/replace semantics: missing key
    → preserve existing value; empty string ``""`` → clear (NULL); any
    other string → encrypt and replace. Implemented in the route handler.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    language: str | None = None
    timezone: str | None = None
    status: BrandStatus | None = None
    sanity_project_id: str | None = None
    sanity_dataset: str | None = None
    sanity_api_version: str | None = None
    sanity_api_token: str | None = None
    sanity_studio_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_channel_id: str | None = None
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_access_token: str | None = None
    meta_page_id: str | None = None
    meta_ig_business_id: str | None = None
    voice_profile_yaml: str | None = None


class BrandDetail(BaseModel):
    """Wire format for GET /brands/{id} — sensitive token presence as bools."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    language: str
    timezone: str
    status: BrandStatus
    active: bool
    sanity_project_id: str | None
    sanity_dataset: str | None
    sanity_api_version: str | None
    sanity_studio_url: str | None
    telegram_channel_id: str | None
    meta_app_id: str | None
    meta_page_id: str | None
    meta_ig_business_id: str | None
    voice_profile_yaml: str | None
    # "<configured>" booleans for encrypted secrets — never expose plaintext.
    has_sanity_api_token: bool
    has_telegram_bot_token: bool
    has_meta_app_secret: bool
    has_meta_access_token: bool
    created_at: datetime
    updated_at: datetime


class BrandTestSanityOut(BaseModel):
    ok: bool
    error: str | None = None
    project_id: str | None = None
    document_count: int | None = None


class BrandCloneForTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    name: str


class BrandCloneForTestOut(BaseModel):
    id: int
    slug: str


# --- Sources run-all ----------------------------------------------------


class RunAllIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: int


# --- Drafts (full GET) ---------------------------------------------------


class CostBreakdownItem(BaseModel):
    operation: str
    cost_usd: float
    count: int


class CostByTopicItem(BaseModel):
    """Per-topic cost rollup attached to GET /runs/{id} (added in S4).

    ``topic_id`` may be ``None`` to represent costs incurred at the run
    level (e.g., pre-topic scoring batch). The frontend renders this as
    a "Run overhead" row in the per-topic bar chart.
    """

    topic_id: int | None
    by_operation: dict[str, float]
    total_usd: float


class DraftApprovalOut(BaseModel):
    """Approval row exposed on GET /drafts/{id} (S5 Step 7)."""

    status: Literal["draft", "approved", "rejected"]
    decided_at: datetime
    decided_by: str
    note: str | None = None


class DraftApprovalIn(BaseModel):
    """Optional payload for approve/reject (S5 Step 7)."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class DraftDetailOut(BaseModel):
    sanity_id: str
    title: str | None
    body_markdown: str | None
    key_takeaway: str | None
    cover_image_url: str | None
    generated_by: str | None
    brand_slug: str | None
    created_at: str | None
    cost_total_usd: float
    cost_breakdown: list[CostBreakdownItem]
    approval: DraftApprovalOut | None = None
    ai_tells_score: int | None = None
    ai_tells: list[str] = []


# --- Cost summary -------------------------------------------------------


class CostSummaryByDay(BaseModel):
    date: str
    cost_usd: float


class CostSummaryOut(BaseModel):
    total_usd: float
    by_operation: dict[str, float]
    by_provider: dict[str, float]
    by_day: list[CostSummaryByDay]


class CostTrendDayOut(BaseModel):
    """One day in the cost-trend series, broken down by operation.

    The frontend stacked-area chart (S4) renders one stacked layer per
    operation key. Days with zero cost are returned with an empty
    ``by_operation`` dict so the X-axis has no gaps.
    """

    date: str
    by_operation: dict[str, float]
    total: float


class DashboardSummaryOut(BaseModel):
    """Bundled KPI payload for the /dashboard hero row (S4).

    Bundled so the page mounts with one round-trip instead of four. Each
    field is independently testable.
    """

    cost_today_usd: float
    cost_yesterday_usd: float
    cost_today_trend_pct: float | None
    cost_month_usd: float
    cost_month_forecast_usd: float
    cost_month_days_progress_pct: float
    drafts_today: int
    drafts_this_week: int
    drafts_prev_week: int
    last_run_finished_at: datetime | None
    last_run_status: str | None
    active_runs_count: int
    avg_daily_cost_7d_usd: float


class CostRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand_id: int = Field(validation_alias="brand_id_fk")
    run_id: int | None
    topic_id: int | None
    draft_id: str | None
    provider: str
    operation: str
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    duration_seconds: float | None
    cost_usd: float
    created_at: datetime


# --- Runs cost extension (for GET /runs/{id}) ---------------------------


class RunDetailWithCostOut(BaseModel):
    run: RunOut
    topics: list[TopicOut]
    cost_total_usd: float
    cost_breakdown: list[CostBreakdownItem]
    cost_by_topic: list[CostByTopicItem] = []


# --- Source health (S5 Step 6) ------------------------------------------


class SourceHealthDayOut(BaseModel):
    """One day's bucket of health stats."""

    date: str  # ISO date YYYY-MM-DD
    fetches: int
    success_count: int
    failure_count: int
    articles_total: int


class SourceHealthOut(BaseModel):
    """``GET /api/v1/sources/{id}/health`` response."""

    source_id: int
    days: int
    success_rate_pct: float
    last_fetch_at: datetime | None
    last_error: str | None
    series: list[SourceHealthDayOut]
