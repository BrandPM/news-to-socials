"""SQLAlchemy 2.0 models for the admin database.

Column names + CHECK constraints follow IT_PROJ_NTS_014 § "Модель данных"
plus the multi-brand revision in IT_PROJ_NTS_025 § "Schema (Step 2 в S3)".

Six tables:

* ``brands``           — first-class brand entity, owns credentials (encrypted)
* ``sources``          — RSS/web/telegram feeds per brand
* ``prompts``          — versioned LLM prompts per brand
* ``pipeline_config``  — singleton row per brand (threshold, voice, etc.)
* ``runs``             — historical pipeline executions per brand
* ``topics``           — per-run topic outcomes (inherits brand via run_id)

Cascade rules:

* ``brands.id`` → all FK ON DELETE RESTRICT. NO cascade — operator
  cleans related rows manually per M5 (NTS_025).
* ``topics.run_id`` → runs.id ON DELETE CASCADE (deleting a run wipes
  its per-topic detail rows, no orphans).
* ``topics.source_id`` → sources.id ON DELETE RESTRICT (a source with
  historical topics cannot be silently deleted).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# Allowed brand lifecycle statuses. The boolean ``active`` column is a
# derived convenience field, kept in sync at write time.
_BRAND_STATUSES = ("draft", "active", "paused", "archived")


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(
        String, nullable=False, default="Europe/Madrid"
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Sanity credentials (project_id/dataset/api_version plaintext, token encrypted)
    sanity_project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sanity_dataset: Mapped[str | None] = mapped_column(String, nullable=True)
    sanity_api_version: Mapped[str | None] = mapped_column(
        String, nullable=True, default="2024-01-01"
    )
    sanity_api_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sanity_studio_url: Mapped[str | None] = mapped_column(String, nullable=True)

    # Telegram credentials (encrypted)
    telegram_bot_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_channel_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Meta credentials (encrypted)
    meta_app_id: Mapped[str | None] = mapped_column(String, nullable=True)
    meta_app_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_page_id: Mapped[str | None] = mapped_column(String, nullable=True)
    meta_ig_business_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Brand voice + style guidance (plaintext)
    voice_profile_yaml: Mapped[str | None] = mapped_column(Text, nullable=True)

    # S6 — languages the pipeline fans out drafts into. JSON-as-TEXT
    # because SQLite has no native JSON column type. Application reads
    # via ``json.loads(brand.languages)``.
    languages: Mapped[str] = mapped_column(
        Text, nullable=False, default='["en"]', server_default='["en"]'
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name="ck_brands_status",
        ),
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    primary_category: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    paywall: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    polling_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=720)
    credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_parser: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_stats: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="source", cascade="save-update"
    )

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('rss', 'web', 'telegram')",
            name="ck_sources_source_type",
        ),
        Index("ix_sources_brand_active", "brand_id_fk", "active"),
    )


class Prompt(Base):
    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prompt_type: Mapped[str] = mapped_column(String, nullable=False)
    version_name: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="human")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    test_results: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "prompt_type IN ('writer_polish', 'writer_draft', "
            "'topic_picker', 'image_prompt', 'writer_translate')",
            name="ck_prompts_prompt_type",
        ),
        # Spec § "prompts" partial UNIQUE: at most one active prompt per
        # (brand, type). SQLite supports partial indexes since 3.8.
        Index(
            "idx_active_prompt",
            "brand_id_fk",
            "prompt_type",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )


class PipelineConfig(Base):
    __tablename__ = "pipeline_config"

    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    scoring_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    topics_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # NTS_089 — a pending draft whose display_date is older than this many
    # days gets a ⚠️ staleness flag in the Content Hub ("news going stale").
    # Editable from Settings without a deploy. Default 3.
    stale_draft_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    # NTS_090 — embedding dedup (editable from Settings, no deploy).
    #   dedup_enabled     master switch (dedup fails OPEN regardless).
    #   dedup_threshold   cosine >= this → duplicate; [0.75, this) → yellow.
    #   dedup_window_days how far back persisted embeddings/titles compare.
    dedup_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    dedup_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.85, server_default="0.85"
    )
    dedup_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )
    # NTS_091 — LLM-judge eval (editable from Settings, no deploy).
    #   eval_enabled    master switch (eval fails OPEN regardless).
    #   eval_threshold  weighted total below this → draft flagged needs_attention.
    eval_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    eval_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=7.0, server_default="7.0"
    )
    # NTS_094 — cover images on manager demand (editable from Settings).
    # False (default) = the run generates a cover per topic, as it always
    # has. True = the run skips image generation entirely and the draft is
    # written with ``coverImage: null`` ON PURPOSE; the manager generates the
    # cover for the draft they actually picked, from the publish-guard button.
    # Default False so applying migration 017 changes nothing until the
    # operator flips it — that flip is where the cost change starts.
    images_on_demand: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # NTS_092 — web-research fact pack before the draft (editable from
    # Settings, no deploy).
    #   research_enabled          master switch; research fails OPEN either way
    #                             (a failed pack = a thin article, never a
    #                             dropped topic).
    #   research_max_sources      distinct outlets allowed in ``context``.
    #   research_max_tokens       output ceiling on the research call.
    #   research_timeout_seconds  hard ceiling on the whole call.
    research_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    research_max_sources: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    research_max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2000, server_default="2000"
    )
    research_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    # JSON array stored as TEXT — admin code is the only writer, so a
    # dedicated JSON column type would only add migration friction.
    banned_phrases: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    voice_profile: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    triggered_by: Mapped[str] = mapped_column(String, nullable=False)
    source_ids: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # NTS_074 — os pid of the detached run-worker subprocess. Written by the
    # spawning endpoint; read by POST /runs/{id}/cancel (kill) and the restart
    # orphan-sweep (dead pid + status='running' → force-fail). NULL for legacy
    # in-process runs and the row-insert→spawn window.
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # S6 — JSON array of language codes the run finished fanout for.
    # Pipeline appends to this as each language branch completes so the
    # admin can see progress without joining against topics.
    languages_completed: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    # NTS_068 — live run progress as JSON-as-TEXT:
    # {sources_total, sources_done, current_source, drafts, errors, stage}.
    # Written best-effort by run_pipeline_for_run for the global status badge.
    progress: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'dry_run', 'cancelled')",
            name="ck_runs_status",
        ),
        Index("ix_runs_started_at", "started_at"),
    )


class CostRecord(Base):
    """One row per paid call (LLM completion, embedding, image gen).

    Granular cost log per NTS_025 C1. Aggregated by ``brand_id`` /
    ``operation`` / ``created_at`` for the cost dashboards in S4.

    ``run_id`` / ``topic_id`` / ``draft_id`` are ON DELETE SET NULL so
    deleting a Run doesn't lose the historical cost — it just detaches
    from the run context.
    """

    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    topic_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("topics.id", ondelete="SET NULL"),
        nullable=True,
    )
    draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("ix_cost_records_brand_created", "brand_id_fk", "created_at"),
        Index("ix_cost_records_run_id", "run_id"),
        Index("ix_cost_records_topic_id", "topic_id"),
        Index("ix_cost_records_draft_id", "draft_id"),
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    topic_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    filter_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # S6 — language code for this topic's draft branch ("en", "ru", "uk",
    # "pl"). Same topic_id can appear N times in a run (one per language),
    # each row owns its own Sanity draft via ``draft_id``.
    language: Mapped[str] = mapped_column(
        String(8), nullable=False, default="en", server_default="en"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )

    run: Mapped[Run] = relationship(back_populates="topics")
    source: Mapped[Source] = relationship(back_populates="topics")

    __table_args__ = (
        CheckConstraint(
            "status IN ('passed', 'filtered_banned', "
            "'filtered_dup', 'filtered_score', 'failed')",
            name="ck_topics_status",
        ),
        UniqueConstraint(
            "run_id", "topic_id", "language", name="uq_topics_run_topic_lang"
        ),
        Index("ix_topics_topic_lang", "topic_id", "language"),
    )


class DraftApproval(Base):
    """One approval row per (Sanity draft, brand). Approve/Reject decisions.

    The default ``status`` ``'draft'`` exists for completeness — the route
    handler upserts only on approve/reject, so in practice every row has
    ``approved`` or ``rejected``. ``UNIQUE (sanity_draft_id, brand_id_fk)``
    keeps the latest decision authoritative; re-approving simply updates
    ``decided_at``.
    """

    __tablename__ = "draft_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sanity_draft_id: Mapped[str] = mapped_column(String, nullable=False)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )
    decided_by: Mapped[str] = mapped_column(
        String, nullable=False, default="admin"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IT_PROJ_NTS_051 Task 3 — approve now publishes to Sanity, so we
    # record the publish completion alongside the approval decision.
    # Both nullable: row may be ``rejected`` (never published) OR an
    # older row from before this column existed.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    sanity_published_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'rejected')",
            name="ck_draft_approvals_status",
        ),
        UniqueConstraint(
            "sanity_draft_id",
            "brand_id_fk",
            name="uq_draft_approvals_draft_brand",
        ),
        Index("ix_draft_approvals_sanity_id", "sanity_draft_id"),
        Index("ix_draft_approvals_brand_status", "brand_id_fk", "status"),
    )


class SourceHealthRecord(Base):
    """One row per source fetch. Powers /sources sparkline + health view.

    Brand-scoped via ``brand_id_fk`` so the health endpoint can enforce
    brand isolation. ``source_id`` is CASCADE so deleting a source cleans
    its history; ``brand_id_fk`` is RESTRICT so a brand cannot be deleted
    while history exists (consistent with the rest of the brand graph).
    """

    __tablename__ = "source_health_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    articles_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_msg: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_health_source_fetched", "source_id", "fetched_at"),
        Index("ix_health_brand_fetched", "brand_id_fk", "fetched_at"),
    )


class AlertSent(Base):
    """Dedup ledger for the Telegram push-alerter (NTS_073).

    One row per notification id that has already been pushed to the
    monitoring chat. The alerter (:mod:`pipeline.monitoring.alerts`) only
    sends ids absent from this table, then records them here. When a
    notification clears (the underlying failed run is closed/deleted) the
    row is removed so a recurrence re-alerts.

    Not brand-scoped: notification ids (``run-47``, ``source-3``) are
    already globally unique, and the alerter sweeps all brands at once.
    """

    __tablename__ = "alert_sent"

    notification_id: Mapped[str] = mapped_column(String, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class TopicEmbedding(Base):
    """Persisted source-text embedding for cross-run/-source dedup (NTS_090).

    The pipeline runs each source in its OWN ``run_pipeline`` invocation
    (NTS_074 isolation), so an in-memory deduper can't see candidates from a
    sibling source. This table is the shared memory: every kept topic's
    embedding is stored here, and each new candidate is compared (numpy
    brute-force cosine — fine at <10k rows) against the window of rows from
    the last ``dedup_window_days``. Cleaned up on pipeline start.

    ``embedding`` is a raw float32 buffer (``np.ndarray.tobytes()``); the
    dimensionality is fixed by ``model`` (text-embedding-3-small → 1536).
    """

    __tablename__ = "topic_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    # Normalised source title stored alongside so the L1 (title Jaccard) check
    # can compare against the window without a second table.
    title_norm: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )

    __table_args__ = (
        Index("ix_topic_embeddings_brand_created", "brand_id_fk", "created_at"),
    )


class DedupLog(Base):
    """Calibration dataset for dedup (NTS_090) — Telegram alone isn't enough.

    One row per dedup decision that mattered: a ``skipped`` duplicate or a
    ``yellow``-zone near-miss (0.75–threshold, NOT skipped). After a week of
    real runs this table tells us whether ``dedup_threshold`` is too tight
    (legit follow-ups skipped) or too loose (dupes leaking through).
    """

    __tablename__ = "dedup_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic_id: Mapped[str] = mapped_column(String, nullable=False)
    matched_topic_id: Mapped[str | None] = mapped_column(String, nullable=True)
    similarity: Mapped[float] = mapped_column(Float, nullable=False)
    # 1 = deterministic title match, 2 = embedding cosine.
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('skipped', 'yellow')", name="ck_dedup_log_action"
        ),
        CheckConstraint("level IN (1, 2)", name="ck_dedup_log_level"),
    )


class DraftScore(Base):
    """LLM-as-judge score for one draft in one language (NTS_091).

    Written automatically after EN generation + each translation. EN gets the
    FULL rubric (factuality, specificity, voice_match, structure,
    banned_leakage); RU/UK/PL get the REDUCED rubric (translation_fidelity,
    banned_leakage) — factuality/structure are inherited from the EN canon
    (NTS_065), which cuts eval cost ~60% vs full×4.

    ``rubric_json`` holds the per-axis 0–10 scores + the judge's feedback +
    deterministic banned hits. ``total`` is the weighted sum. Scores are only
    comparable within one ``judge_prompt_version``. ``model`` records which
    judge produced this row (gpt-4o for the stream; gpt-5.5 for escalated
    yellow-band cases). ``flagged`` = total < the eval_threshold in force when
    scored (denormalised so the UI + sort don't depend on the live threshold).
    """

    __tablename__ = "draft_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    brand_id_fk: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("brands.id", ondelete="CASCADE"), nullable=True
    )
    lang: Mapped[str] = mapped_column(String(8), nullable=False)
    rubric_json: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[float] = mapped_column(Float, nullable=False)
    flagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    judge_prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )

    __table_args__ = (
        Index("ix_draft_scores_brand_created", "brand_id_fk", "created_at"),
        Index("ix_draft_scores_version", "judge_prompt_version"),
    )
