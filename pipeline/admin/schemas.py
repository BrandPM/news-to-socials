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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

SourceType = Literal["rss", "web", "telegram"]
PromptType = Literal[
    "writer_polish", "writer_draft", "topic_picker", "image_prompt", "writer_translate"
]
RunStatus = Literal["running", "success", "failed", "dry_run", "cancelled"]
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


class PromptContradiction(BaseModel):
    """One internal contradiction the reviewer found in a prompt.

    ``issue`` names the conflict, ``why`` explains the failure mode it
    causes downstream, and ``suggestion`` is a concrete edit. NTS_060
    is the canonical example: a prompt that asks for ``## H2`` headings
    in the body while another clause forbids markdown in the title can
    leak ``##`` into titles — the reviewer should surface that pairing.
    """

    issue: str
    why: str
    suggestion: str


class PromptAnalysisOut(BaseModel):
    """Strict result of POST /prompts/{id}/analyze (NTS task 3).

    The LLM is forced to emit exactly this shape; a response we can't
    coerce into it becomes a 422 at the route boundary rather than a
    500. Analyze is read-only — it never mutates the prompt.
    """

    strengths: list[str]
    contradictions: list[PromptContradiction]
    risks: list[str]
    summary: str


# --- Pipeline config ----------------------------------------------------


class PublicationSlot(BaseModel):
    """One weekly publication slot (NTS_098 §4).

    ``capacity`` is how many articles that weekday can absorb. Typed rather
    than left as a free dict so a malformed slot is rejected at save time,
    not when the publish run tries to fill it.
    """

    model_config = ConfigDict(extra="forbid")

    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    capacity: int = Field(ge=0, le=20)


class PipelineConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    brand_id: int = Field(validation_alias="brand_id_fk")
    scoring_threshold: int
    topics_per_run: int
    banned_phrases: list[str]
    voice_profile: str
    # NTS_089 — staleness threshold (days) for the Content Hub ⚠️ flag.
    stale_draft_days: int
    # NTS_090 — embedding-dedup tunables.
    dedup_enabled: bool
    dedup_threshold: float
    dedup_window_days: int
    # NTS_091 — LLM-judge eval tunables.
    eval_enabled: bool
    eval_threshold: float
    # NTS_094 — skip cover generation during the run; manager generates on demand.
    images_on_demand: bool
    # NTS_092 — research-stage switch + budgets.
    research_enabled: bool
    research_max_sources: int
    research_max_tokens: int
    research_timeout_seconds: int
    # === v3 contour-1 keys (NTS_098 §4, NTS_099 §1) — migration 020 =======
    # Rhythm
    publication_slots: list[dict[str, Any]]
    weekly_draft_budget: int
    portfolio_daily_cap_document: int
    portfolio_daily_cap_news: int
    candidate_ttl_days: dict[str, int]
    production_timeout_min: int
    max_attempts: int
    brand_timezone: str
    retention_days_rejected: int
    # Dedup windows
    dedup_threshold_live: float
    dedup_threshold_rejected: float
    dedup_window_rejected_days: int
    dedup_threshold_published: float
    dedup_window_published_days: int
    # Guard axes
    jurisdiction_tiers: dict[str, list[str]]
    # Depth
    depth_article_min_facts: int
    depth_deep_min_facts: int
    # Spend kill-switch
    monthly_spend_cap_usd: float
    max_cost_per_candidate_usd: float
    # Prefilter
    prefilter_deny_title_patterns: list[str]
    prefilter_require_summary: bool
    prefilter_max_age_hours_news: int
    prefilter_max_age_hours_primary: int
    prefilter_languages: list[str]
    prefilter_min_summary_chars: int
    updated_at: datetime

    @field_validator("banned_phrases", mode="before")
    @classmethod
    def _parse_banned(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else []
        return v

    @field_validator(
        "publication_slots",
        "prefilter_deny_title_patterns",
        "prefilter_languages",
        mode="before",
    )
    @classmethod
    def _parse_json_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else []
        return list(v) if isinstance(v, tuple) else v

    @field_validator("candidate_ttl_days", "jurisdiction_tiers", mode="before")
    @classmethod
    def _parse_json_obj(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v else {}
        return dict(v) if v is not None else v


class PipelineConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scoring_threshold: int | None = Field(default=None, ge=1, le=10)
    topics_per_run: int | None = Field(default=None, ge=1, le=10)
    banned_phrases: list[str] | None = None
    voice_profile: str | None = None
    stale_draft_days: int | None = Field(default=None, ge=1, le=60)
    # NTS_090 — dedup tunables (editable from Settings, no deploy).
    dedup_enabled: bool | None = None
    dedup_threshold: float | None = Field(default=None, ge=0.5, le=0.99)
    dedup_window_days: int | None = Field(default=None, ge=1, le=90)
    # NTS_091 — LLM-judge eval tunables.
    eval_enabled: bool | None = None
    eval_threshold: float | None = Field(default=None, ge=0.0, le=10.0)
    # NTS_094 — cover generation on manager demand instead of every topic.
    images_on_demand: bool | None = None
    # NTS_092 — research-stage switch + budgets (editable from Settings).
    research_enabled: bool | None = None
    research_max_sources: int | None = Field(default=None, ge=1, le=20)
    research_max_tokens: int | None = Field(default=None, ge=500, le=8000)
    research_timeout_seconds: int | None = Field(default=None, ge=10, le=300)
    # === v3 contour-1 keys (NTS_098 §4, NTS_099 §1) — migration 020 =======
    # Bounds are guard rails against a typo, not policy: the spec's starting
    # values sit comfortably inside each range. The five JSON keys are typed
    # structurally so a malformed slot list is a 422, not a broken run.
    publication_slots: list[PublicationSlot] | None = None
    weekly_draft_budget: int | None = Field(default=None, ge=0, le=50)
    portfolio_daily_cap_document: int | None = Field(default=None, ge=0, le=50)
    portfolio_daily_cap_news: int | None = Field(default=None, ge=0, le=50)
    candidate_ttl_days: dict[str, int] | None = None
    production_timeout_min: int | None = Field(default=None, ge=5, le=1440)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    brand_timezone: str | None = None
    retention_days_rejected: int | None = Field(default=None, ge=1, le=3650)
    dedup_threshold_live: float | None = Field(default=None, ge=0.5, le=0.99)
    dedup_threshold_rejected: float | None = Field(default=None, ge=0.5, le=0.99)
    dedup_window_rejected_days: int | None = Field(default=None, ge=1, le=365)
    dedup_threshold_published: float | None = Field(default=None, ge=0.5, le=0.99)
    dedup_window_published_days: int | None = Field(default=None, ge=1, le=365)
    jurisdiction_tiers: dict[str, list[str]] | None = None
    depth_article_min_facts: int | None = Field(default=None, ge=1, le=100)
    depth_deep_min_facts: int | None = Field(default=None, ge=1, le=200)
    monthly_spend_cap_usd: float | None = Field(default=None, ge=0.0, le=100000.0)
    max_cost_per_candidate_usd: float | None = Field(default=None, ge=0.0, le=1000.0)
    prefilter_deny_title_patterns: list[str] | None = None
    prefilter_require_summary: bool | None = None
    prefilter_max_age_hours_news: int | None = Field(default=None, ge=1, le=8760)
    prefilter_max_age_hours_primary: int | None = Field(default=None, ge=1, le=8760)
    prefilter_languages: list[str] | None = None
    prefilter_min_summary_chars: int | None = Field(default=None, ge=0, le=5000)

    @field_validator("brand_timezone")
    @classmethod
    def _known_timezone(cls, v: str | None) -> str | None:
        """Reject an unknown zone at the API edge.

        Slot dates and displayDate are the only timezone-dependent arithmetic
        in the system (NTS_098 §5). A typo saved here would not fail until the
        next publish run tried to resolve it.
        """
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {v!r}") from exc
        return v

    @field_validator("jurisdiction_tiers")
    @classmethod
    def _known_tiers(
        cls, v: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        """Only tier1/tier2 are stored; everything unlisted is tier3 by
        definition (NTS_115 artefact 4), so a "tier3" key would be a silently
        ignored edit."""
        if v is None:
            return v
        unknown = set(v) - {"tier1", "tier2"}
        if unknown:
            raise ValueError(
                f"unknown tier key(s): {sorted(unknown)} — only tier1/tier2 "
                "are stored, anything unlisted is tier3"
            )
        return v


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
    language: str = "en"


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand_id: int = Field(validation_alias="brand_id_fk")
    triggered_by: str
    source_ids: list[int]
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    # NTS_074 — os pid of the detached run-worker (None for legacy in-process
    # runs). Surfaced so the UI can tell a cancellable live run from a stale row.
    pid: int | None = None
    stats: dict[str, Any] | None
    log_excerpt: str | None
    languages_completed: list[str] = []
    # NTS_068 — live run progress {sources_total, sources_done, current_source,
    # drafts, errors, stage}. Empty dict when the run predates the column.
    progress: dict[str, Any] = {}

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

    @field_validator("progress", mode="before")
    @classmethod
    def _parse_progress(cls, v: Any) -> Any:
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except (ValueError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return v if isinstance(v, dict) else {}

    @field_validator("languages_completed", mode="before")
    @classmethod
    def _parse_languages_completed(cls, v: Any) -> Any:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except (ValueError, TypeError):
                return []
            return parsed if isinstance(parsed, list) else []
        if isinstance(v, list):
            return v
        return []


class RunDetailOut(BaseModel):
    run: RunOut
    topics: list[TopicOut]


class RunLogOut(BaseModel):
    log: str
    source: Literal["file", "stub"]


class RunEventOut(BaseModel):
    """One normalized line from the pipeline log, scoped to a single run.

    ``kind`` is the original ``event`` field (e.g. ``pipeline.start``,
    ``source.fetched``, ``score.done``, ``dedup.sanity_hit``,
    ``topic.published_as_draft``, ``image.failed``). ``data`` carries the
    remaining JSON keys verbatim so the timeline UI can pull
    event-specific fields without a new endpoint per event type.
    """

    timestamp: datetime
    level: str
    kind: str
    data: dict[str, Any] = {}


class RunEventsOut(BaseModel):
    events: list[RunEventOut]
    total: int
    truncated: bool = False
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

# Languages the Icon pipeline can fan out into. ``en`` is canonical and
# every brand must include it (NTS_044 S6 / NTS_056 Task 2).
SUPPORTED_LANGUAGES = ("en", "ru", "uk", "pl")
SUPPORTED_LANGUAGE_SET = frozenset(SUPPORTED_LANGUAGES)


def _coerce_languages_out(v: Any) -> list[str]:
    """Normalise ``Brand.languages`` (JSON-as-TEXT or list) for the wire.

    ``Brand.languages`` is stored as a JSON string in SQLite; when loaded
    via ``from_attributes`` the raw value is a string. Surface as a list so
    the frontend can iterate without knowing about the storage shape.
    Falls back to ``["en"]`` for null/empty/garbage so the UI never breaks.
    """
    if v is None or v == "":
        return ["en"]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return ["en"]
        return parsed if isinstance(parsed, list) and parsed else ["en"]
    if isinstance(v, list):
        return v or ["en"]
    return ["en"]


def validate_languages_payload(v: list[str]) -> list[str]:
    """Validate a ``languages`` payload: non-empty, supported, includes en.

    Normalises case and de-duplicates while preserving order. Raises
    ``ValueError`` (→ 422/400 at the route boundary) on any violation.
    """
    if not isinstance(v, list) or not v:
        raise ValueError("languages must be a non-empty list")
    seen: list[str] = []
    for raw in v:
        lang = str(raw).strip().lower()
        if lang not in SUPPORTED_LANGUAGE_SET:
            raise ValueError(
                f"unsupported language {raw!r} — must be one of "
                f"{list(SUPPORTED_LANGUAGES)}"
            )
        if lang not in seen:
            seen.append(lang)
    if "en" not in seen:
        raise ValueError("languages must include 'en' (canonical language)")
    return seen


class BrandSummary(BaseModel):
    """Wire format for brand list — NO sensitive credential fields."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    language: str
    languages: list[str] = ["en"]
    timezone: str
    status: BrandStatus
    active: bool

    @field_validator("languages", mode="before")
    @classmethod
    def _parse_languages(cls, v: Any) -> Any:
        return _coerce_languages_out(v)


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
    languages: list[str] | None = None
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
    # NOTE: ``languages`` content rules (non-empty / supported / includes en)
    # are enforced in the route handler so the failure is a 400 with a clear
    # message (NTS_056 Task 2 contract) rather than Pydantic's generic 422.


class BrandDetail(BaseModel):
    """Wire format for GET /brands/{id} — sensitive token presence as bools."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    language: str
    languages: list[str] = ["en"]
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

    @field_validator("languages", mode="before")
    @classmethod
    def _parse_languages(cls, v: Any) -> Any:
        return _coerce_languages_out(v)


class BannedByLanguageOut(BaseModel):
    """Per-language banned phrases as stored in voice.<lang>.banned_phrases
    (NTS_072). ``languages`` is the brand's roster (tab order for the editor);
    ``banned`` maps each to its raw list (empty when none set yet)."""

    languages: list[str]
    banned: dict[str, list[str]]


class BannedPhraseUpdateIn(BaseModel):
    """Set one language's banned list. Only that language is touched."""

    language: str
    phrases: list[str]


class ImageStylesOut(BaseModel):
    """Cover-image style prompts as stored in ``image.style_prompts`` (NTS_075).
    Brand-wide (one cover per topic shared across languages), so no per-language
    split. ``styles`` is the raw list (empty → generation uses the default set)."""

    styles: list[str]


class ImageStylesUpdateIn(BaseModel):
    """Replace the brand's cover-image style prompts."""

    model_config = ConfigDict(extra="forbid")

    styles: list[str]


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
    """Approval row exposed on GET /drafts/{id} (S5 Step 7).

    IT_PROJ_NTS_051 Task 3: ``published_at`` + ``sanity_published_id``
    surface whether an "approved" row actually made it to Sanity. The
    publish step can fail independently (network blip, 4xx) without
    rolling back the local approval — so the admin UI needs to render
    "approved, publish pending" distinctly from "approved + live".
    """

    status: Literal["draft", "approved", "rejected"]
    decided_at: datetime
    decided_by: str
    note: str | None = None
    published_at: datetime | None = None
    sanity_published_id: str | None = None


class BatchApprovalResult(BaseModel):
    """Per-language result of /drafts/{topic_id}/approve-all-siblings (S6.9-bis).

    ``status`` is one of:
      - ``published``                — local approval + Sanity publish succeeded
      - ``approved_publish_pending`` — approval recorded, Sanity publish failed
      - ``skipped``                  — already published, no-op
      - ``error``                    — unexpected failure (see ``detail``)
    """

    sanity_draft_id: str
    language: str
    status: Literal[
        "published", "approved_publish_pending", "skipped", "error"
    ]
    sanity_published_id: str | None = None
    detail: str | None = None


class BatchApprovalOut(BaseModel):
    """Response for batch-approve. ``results`` is per-sibling per the
    above; ``ok_count`` / ``fail_count`` are convenience aggregates the
    toast helper uses."""

    topic_id: str
    ok_count: int
    fail_count: int
    results: list[BatchApprovalResult]


class DraftApprovalIn(BaseModel):
    """Optional payload for approve/reject (S5 Step 7)."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class DisplayDatePatchIn(BaseModel):
    """Body for ``PATCH /drafts/{id}/display-date`` (NTS_089).

    ``display_date`` is a bare ``YYYY-MM-DD`` (UTC, date-only). Future dates
    are rejected by the route (scheduled publishing is out of scope, NTS_085).
    """

    model_config = ConfigDict(extra="forbid")

    display_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class DisplayDatePatchOut(BaseModel):
    """Result of a display-date patch: the value written + which sibling
    drafts it was applied to (one date per topic, all languages)."""

    display_date: str
    updated_draft_ids: list[str]


class EvalSummaryRow(BaseModel):
    """One aggregation bucket for the measurability view (NTS_091)."""

    key: str  # judge_prompt_version, or ISO "YYYY-Www"
    avg_total: float
    n: int
    flagged: int


class EvalSummaryOut(BaseModel):
    """Average judge score by prompt version and by week — the measurable
    signal for prompt iteration (no charting library; a table is enough)."""

    by_version: list[EvalSummaryRow] = []
    by_week: list[EvalSummaryRow] = []


class DraftScoreOut(BaseModel):
    """LLM-judge score for a draft (NTS_091). ``axes`` holds per-axis 0–10;
    ``total`` is the weighted sum, comparable only within ``judge_prompt_version``."""

    total: float
    flagged: bool
    model: str
    judge_prompt_version: str
    axes: dict[str, float] = {}
    feedback: str | None = None
    worst_axis: str | None = None
    banned_hits: list[str] = []
    created_at: datetime | None = None


class DraftDetailOut(BaseModel):
    sanity_id: str
    title: str | None
    body_markdown: str | None
    key_takeaway: str | None
    cover_image_url: str | None
    generated_by: str | None
    brand_slug: str | None
    created_at: str | None
    language: str | None = None
    topic_id: str | None = None
    cost_total_usd: float
    cost_breakdown: list[CostBreakdownItem]
    approval: DraftApprovalOut | None = None
    ai_tells_score: int | None = None
    ai_tells: list[str] = []
    # NTS_089 — displayed publication date (date-only "YYYY-MM-DD", UTC).
    # Editable in the Content Hub before publish; becomes publishedAt on
    # approve. None on legacy drafts created before this feature.
    display_date: str | None = None
    # NTS_090 — structural components this draft is missing; empty means it
    # is publishable. Derived from the Sanity doc on every read (nothing is
    # stored), by the same validator the publish endpoint enforces, so the
    # UI's disabled Approve button and the server's 422 can never disagree.
    # Codes: coverImage | title | slug | body | body_h2 | displayDate.
    missing: list[str] = []


class PublishedDocOut(BaseModel):
    """Slim view of a published Sanity post (no ``drafts.`` prefix).

    Returned by ``GET /drafts/{id}`` when the draft has already been
    promoted to published (state ``published_only`` / ``both``). Only the
    fields the admin's "Published" view needs — slug, title, language,
    cover — so we don't pay the AI-tells / cost rollup cost on what is
    just a success page.
    """

    sanity_id: str
    title: str | None = None
    slug: str | None = None
    language: str | None = None
    cover_image_url: str | None = None
    brand_slug: str | None = None
    updated_at: str | None = None


class PublicationInfoOut(BaseModel):
    """When/how a draft became published. Combines the Sanity-side id with
    the local ``draft_approvals`` timeline. ``live_url`` is constructed
    server-side from the published slug + the brand's public URL pattern.
    """

    sanity_published_id: str
    published_at: datetime | None = None
    approver: str | None = None
    note: str | None = None
    live_url: str | None = None


class RejectionInfoOut(BaseModel):
    """When/why a draft was rejected. IT_PROJ_NTS_052 Content hub: the
    Rejected tab + /drafts/[id] rejected view both consume this — the UI
    shows the timestamp + optional reason, and offers Restore (un-reject)
    or Delete permanently. The Sanity doc itself stays with
    ``status: "rejected"`` until the operator explicitly deletes it.
    """

    rejected_at: datetime | None = None
    reason: str | None = None
    rejected_by: str | None = None


# IT_PROJ_NTS_052 Content hub: state names align with the three tabs the
# admin UI shows (``pending`` / ``published`` / ``rejected``) plus
# ``neither`` for the genuine-404 case. A doc that has both a published
# mirror AND an open ``drafts.*`` (operator re-edit) reports
# ``state='pending'`` with ``publication_info`` populated — the UI uses
# the publication_info signal to show the "Editing published post"
# warning above the draft preview.
DraftLifecycleState = Literal[
    "pending", "published", "rejected", "neither"
]


class DraftStateOut(BaseModel):
    """Lifecycle-aware wrapper around the legacy ``DraftDetailOut`` shape.

    IT_PROJ_NTS_052 Content hub: ``state`` drives the per-status view
    inside /drafts/[id]:

    * ``pending``   — ``drafts.{id}`` exists; not flagged rejected. May
                       also have a published mirror (re-edit case →
                       ``publication_info`` populated for warning UI).
    * ``published`` — only the published doc exists; ``drafts.{id}`` was
                       removed by the approve chain (NTS_051).
    * ``rejected``  — ``drafts.{id}`` exists with ``status='rejected'``;
                       Restore returns it to pending.
    * ``neither``   — genuine 404 (typo / hard-deleted in Studio). The
                       endpoint surfaces this as HTTP 404.

    Returned for every non-error case (200). Only ``state='neither'``
    becomes a 404.
    """

    sanity_id: str
    state: DraftLifecycleState
    draft: DraftDetailOut | None = None
    published: PublishedDocOut | None = None
    publication_info: PublicationInfoOut | None = None
    rejection_info: RejectionInfoOut | None = None
    # NTS_091 — latest LLM-judge score for this draft (its language), if any.
    score: DraftScoreOut | None = None


class DraftListSibling(BaseModel):
    """Lightweight sibling-draft pointer attached to each list item
    (IT_PROJ_NTS_052 Content hub). The Pending/Rejected tabs show one
    card per draft with language chips for its siblings so an operator
    can see at a glance which languages are still in-flight without
    opening the detail page.
    """

    sanity_id: str
    language: str
    status: Literal["pending", "published", "rejected"]


# IT_PROJ_NTS_052 — explicit status used by the Content hub. Distinct
# from ``approval_status`` (legacy DB-side approval row) because a
# Sanity draft can be ``pending`` (no approval row) OR ``rejected``
# (flagged in Sanity) OR ``published`` (lives at non-drafts.* id) —
# whereas the old ``approval_status`` only ever read the DB row.
DraftStatus = Literal["pending", "published", "rejected"]


class DraftListItem(BaseModel):
    """Row in the multilingual /drafts list view (S6.7).

    IT_PROJ_NTS_052 Content hub adds: ``status`` (the three-tab kind),
    ``published_at`` / ``rejected_at`` timestamps, ``live_url`` for
    published rows, and a ``siblings`` array for per-language fanout.
    """

    sanity_id: str
    title: str | None
    language: str
    topic_id: str | None
    created_at: str | None
    cover_image_url: str | None = None
    approval_status: Literal["draft", "approved", "rejected"] = "draft"
    # S6 slug-fix: surface slug.current so the admin can assert it's
    # populated. Null is acceptable for legacy drafts before the
    # backfill runs.
    slug: str | None = None

    # IT_PROJ_NTS_052 Content hub additions.
    status: DraftStatus = "pending"
    published_at: datetime | None = None
    # NTS_089 — displayed publication date (date-only "YYYY-MM-DD", UTC).
    # Powers the date badge + staleness ⚠️ on pending cards.
    display_date: str | None = None
    # NTS_091 — latest judge score + flag, for the badge + score sort.
    score_total: float | None = None
    score_flagged: bool = False
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    live_url: str | None = None
    siblings: list[DraftListSibling] = []
    # NTS_090 — missing structural components (same codes as
    # ``DraftDetailOut.missing``); powers the ⚠️ badge on incomplete cards.
    missing: list[str] = []


class DraftListOut(BaseModel):
    """Paginated drafts list with language counts (S6.7) + status counts
    (IT_PROJ_NTS_052).

    ``by_language`` powers the language tab strip (brand-wide; stays
    stable as the user filters). ``by_status`` powers the Content hub
    Pending / Published / Rejected tabs and DOES reflect the active
    language filter — switching to RU updates the per-status counts to
    "how many RU pending / published / rejected".
    """

    items: list[DraftListItem]
    total: int
    by_language: dict[str, int]
    by_status: dict[str, int] = {}
    has_more: bool = False


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
    drafts_this_week_by_language: dict[str, int] = {}
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


class RunKpis(BaseModel):
    """Authoritative per-run KPI summary used by /runs/[id] hero metrics.

    Computed in the runs route from cost_records + topics so each number
    has a single source of truth. The legacy ``run.stats`` dict is kept
    for back-compat but the frontend should prefer this block.

    - ``fetched``: RSS items pulled from sources (from ``stats.fetched``)
    - ``scored``: LLM scoring calls (``cost_records`` ``operation='topic_scoring'``)
    - ``passed``: topics row count with ``status='passed'``
    - ``drafts``: distinct ``draft_id`` values across passed topics
      (falls back to ``stats.drafted`` while topics table is empty —
      handled by the route, not the schema)
    - ``errors``: ``stats.errors`` (failed source/lang branches)
    """

    fetched: int
    scored: int
    passed: int
    drafts: int
    errors: int


class RunDetailWithCostOut(BaseModel):
    run: RunOut
    topics: list[TopicOut]
    cost_total_usd: float
    cost_breakdown: list[CostBreakdownItem]
    cost_by_topic: list[CostByTopicItem] = []
    kpis: RunKpis


# --- Notifications (S5 Step 10) -----------------------------------------


class NotificationItemOut(BaseModel):
    id: str
    kind: Literal["run_failed", "source_unhealthy", "draft_rejected"]
    severity: Literal["warning", "danger"]
    title: str
    description: str
    href: str | None = None
    created_at: datetime


class NotificationsListOut(BaseModel):
    items: list[NotificationItemOut]
    count: int


# --- Threshold simulator (S5 Step 9) ------------------------------------


class TopicsSimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: int
    threshold: int = Field(ge=1, le=10)
    days: int = Field(default=30, ge=1, le=180)


class ScoreBucket(BaseModel):
    score: int
    count: int


class TopicsSimulateOut(BaseModel):
    threshold: int
    days: int
    total_scored: int
    currently_passed: int
    would_pass: int
    delta: int
    swing_in: int
    swing_out: int
    score_distribution: list[ScoreBucket]


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
