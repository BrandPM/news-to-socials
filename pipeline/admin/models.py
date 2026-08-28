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

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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

    # --- NTS_101 §1 — the primary-feed registry lives in ``sources``, not in
    # a second table. ``source_role`` splits the 28 legacy news feeds from the
    # regulator/legislation feeds v3 reads documents out of.
    source_role: Mapped[str] = mapped_column(
        String, nullable=False, default="news", server_default="news"
    )
    source_class: Mapped[str] = mapped_column(
        String, nullable=False, default="news", server_default="news"
    )
    # NTS_108 §1 — what composition may legally do with the text. Starts at the
    # most restrictive class for every pre-v3 feed (headline as a lead, nothing
    # more) and is reclassified upward from the Sources screen.
    license_class: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="news_paywalled",
        server_default="news_paywalled",
    )
    doc_language: Mapped[str | None] = mapped_column(String, nullable=True)
    fetch_method: Mapped[str | None] = mapped_column(String, nullable=True)
    # Rolling share of successful extractions, maintained by the fetcher.
    reliability: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('rss', 'web', 'telegram')",
            name="ck_sources_source_type",
        ),
        CheckConstraint(
            "source_role IN ('news', 'primary_feed', 'primary_site')",
            name="ck_sources_source_role",
        ),
        CheckConstraint(
            "source_class IN ('regulator', 'tax_authority', 'legislation', "
            "'jurisdiction_list', 'filings', 'court', 'professional_alert', "
            "'corporate_pr', 'news')",
            name="ck_sources_source_class",
        ),
        CheckConstraint(
            "license_class IN ('public_official', 'public_domain', "
            "'corporate_pr', 'professional_commentary', 'news_paywalled')",
            name="ck_sources_license_class",
        ),
        CheckConstraint(
            "fetch_method IN ('rss', 'atom', 'html_list', 'edgar_fts')",
            name="ck_sources_fetch_method",
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
            "'topic_picker', 'image_prompt', 'writer_translate', "
            # NTS_099 §6 — the guard rubric is a prompt like any other, so it
            # is edited from Editorial Policy instead of deployed. Migration
            # 021 rebuilt the table to admit it.
            "'editorial_guard')",
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
    # --- v2 legacy, read-only (NTS_099: scoring was demoted to the free
    # prefilter, so neither key steers anything in the v3 contour).
    #
    # ``scoring_threshold`` is still read by ``pipeline.run`` (v2 ``min_score``)
    # and by ``POST /topics/simulate``, so it is legacy, not dead.
    #
    # ``topics_per_run`` has **no consumer at all**: the v2 per-source limit
    # comes from the CLI ``--limit``, and nothing in the pipeline reads this
    # column. It survives only because the deployed Settings form posts it, and
    # dropping the column mid-shadow-week would break the one screen Andriy
    # needs in order to flip ``intake_enabled``. Orphan column, drop after v2
    # off (NTS_121 §2).
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
    # === v3 contour-1 keys (NTS_098 §4, NTS_099 §1) — migration 020 ========
    # All 25 are editable from Settings without a deploy, and all 25 are
    # sentinel-tested end to end. NOT NULL with the spec's Icon starting
    # values, so a config row that predates them still answers every read.

    # --- Rhythm (NTS_098 §4/§5). Slots are a JSON array of
    # {"day": "<mon..sun>", "capacity": <int>}; publication stays manual.
    publication_slots: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='[{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]',
        server_default='[{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]',
    )
    # Ceiling on candidates entering in_production per ISO week.
    weekly_draft_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6, server_default="6"
    )
    # NTS_099 §5 — the daily accept cap is counted per input_kind.
    portfolio_daily_cap_document: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    portfolio_daily_cap_news: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # TTL by event_stage as JSON; "default" covers stages not listed.
    candidate_ttl_days: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='{"deal_announced": 7, "deal_closed": 7, '
        '"consultation": 21, "default": 14}',
        server_default='{"deal_announced": 7, "deal_closed": 7, '
        '"consultation": 21, "default": 14}',
    )
    production_timeout_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    # Overlaps ``brands.timezone`` by design — NTS_098 §4 puts the slot
    # timezone on the config surface. Migration 020 copies brands.timezone in
    # so the two start identical; from S4 on THIS key is the authority for
    # slot arithmetic and displayDate.
    brand_timezone: Mapped[str] = mapped_column(
        Text, nullable=False, default="Europe/Madrid", server_default="Europe/Madrid"
    )
    retention_days_rejected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    # --- Dedup windows (NTS_098 §3). Distinct from the NTS_090 keys above:
    # those govern v2 draft dedup and keep working until v2 is switched off.
    dedup_threshold_live: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.90, server_default="0.90"
    )
    dedup_threshold_rejected: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.92, server_default="0.92"
    )
    dedup_window_rejected_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14, server_default="14"
    )
    dedup_threshold_published: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.88, server_default="0.88"
    )
    dedup_window_published_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )

    # --- Guard axes (NTS_099 §2). JSON {"tier1": [...], "tier2": [...]};
    # anything unlisted is tier3. Values from NTS_115 artefact 4.
    jurisdiction_tiers: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='{"tier1": ["CH", "CY", "MT", "AE", "UK", "PL", "UA", "LI", "EU"], '
        '"tier2": ["US", "SG", "HK", "LU", "MC", "PT", "ES", "IT", "DE", "AT", '
        '"IL", "KZ", "TR"]}',
        server_default='{"tier1": ["CH", "CY", "MT", "AE", "UK", "PL", "UA", "LI", "EU"], '
        '"tier2": ["US", "SG", "HK", "LU", "MC", "PT", "ES", "IT", "DE", "AT", '
        '"IL", "KZ", "TR"]}',
    )

    # --- Depth thresholds (NTS_102 v2) — fact count decides depth_final.
    depth_article_min_facts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )
    depth_deep_min_facts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )

    # --- Spend kill-switch (NTS_106 §3). At 80% of the monthly cap an alert
    # fires; at 100% production stops and intake keeps running.
    monthly_spend_cap_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=150.0, server_default="150"
    )
    max_cost_per_candidate_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0, server_default="5"
    )

    # --- Prefilter (NTS_099 §1) — free, in code, configured here. Deny
    # patterns are NOT applied to primary_feed items: a regulator may
    # "appoint" a board, which is not a personnel story.
    prefilter_deny_title_patterns: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["appoints", "hires", "joins", "named as", "wins award", '
        '"ranked", "opens office", "rebrand", "outlook", "forecast", '
        '"analysts expect"]',
        server_default='["appoints", "hires", "joins", "named as", "wins award", '
        '"ranked", "opens office", "rebrand", "outlook", "forecast", '
        '"analysts expect"]',
    )
    prefilter_require_summary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    prefilter_max_age_hours_news: Mapped[int] = mapped_column(
        Integer, nullable=False, default=72, server_default="72"
    )
    prefilter_max_age_hours_primary: Mapped[int] = mapped_column(
        Integer, nullable=False, default=240, server_default="240"
    )
    prefilter_languages: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["en", "de", "fr", "it", "pl", "uk", "ru", "el"]',
        server_default='["en", "de", "fr", "it", "pl", "uk", "ru", "el"]',
    )
    prefilter_min_summary_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, default=80, server_default="80"
    )

    # === v3 contour-1 mode flags (NTS_103 шаг 1/3) — migration 022 =========
    # Both modes are explicit and independently switchable, because the whole
    # point of NTS_103 is that the cutover is a sequence of flags rather than a
    # deploy. Both default OFF:
    #
    # * ``intake_enabled`` is a NEW mode, and a new mode ships off — the run
    #   that fills ``candidates`` starts when the operator says so, not when
    #   the deploy lands.
    # * ``v2_generation_enabled`` is the OLD mode, and it ships off by
    #   Andriy's gate-journal directive of 2026-08-28 (NTS_105 §9, NTS_114 S2):
    #   the daily generation was paying for translations and covers on articles
    #   the rubric itself calls rejects. Turning it back on is one PUT away if
    #   publications are needed before the v3 production path exists.
    #
    # Consequence worth stating out loud: on the deploy that applies 022 the
    # cron does nothing until a flag is flipped. That is deliberate — an idle
    # pipeline is visible in the runs list; a pipeline generating waste is not.
    intake_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    v2_generation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # NTS_099 §2 — "дешёвая модель (gpt-4o-mini или аналог)". A config key so
    # swapping the guard model is a Settings edit, not a deploy.
    guard_model: Mapped[str] = mapped_column(
        Text, nullable=False, default="gpt-4o-mini", server_default="gpt-4o-mini"
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

    # NTS_106 §5 — which v3 contour this run belongs to. NULL means a pre-v3
    # run: the ~72 historical rows are none of these four, and NULL passes a
    # SQLite CHECK, so no value had to be invented for them.
    run_type: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'dry_run', 'cancelled')",
            name="ck_runs_status",
        ),
        CheckConstraint(
            "run_type IN ('intake', 'production', 'publish', 'ttl')",
            name="ck_runs_run_type",
        ),
        Index("ix_runs_started_at", "started_at"),
        Index("ix_runs_run_type", "run_type"),
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
    # NTS_106 §3 — the candidate this money was spent on. Without it
    # ``max_cost_per_candidate_usd`` is not a limit but a number in a form:
    # ``topic_id`` only ever exists on the v2 path, so on the v3 path every
    # paid row was brand-and-run-scoped and nothing more. Migration 025.
    # ON DELETE SET NULL for the same reason as ``run_id``: deleting the
    # candidate must not lose the historical spend, only detach it.
    candidate_id_fk: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("candidates.id", ondelete="SET NULL"),
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
        Index("ix_cost_records_candidate", "candidate_id_fk"),
    )


class Topic(Base):
    """**v2 legacy, read-only.** Per-topic outcomes of a v2 generation run.

    The only writer is ``AdminConfigClient.record_topic_result``, and the only
    caller of that is ``pipeline.run`` — the v2 generation path, gated behind
    ``v2_generation_enabled`` (OFF since 2026-08-28). While the flag is off no
    new row appears here.

    NTS_098 §6 settles its fate: the table stays until v2 is switched off, then
    becomes a read-only archive. The v3 equivalent is ``candidates``, which is
    not a duplicate of this — a Topic is one item of one run, a Candidate is a
    self-contained portfolio entry that outlives its feed item.

    Still read, so do NOT drop it: ``GET /runs/{id}`` (history),
    ``POST /topics/simulate``, the delete guards in ``/sources`` and
    ``/brands``, and ``purge_draft_local_refs``.
    """

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

    # NTS_098 §1 — the candidate this draft came from. Nullable: every v2 row
    # predates the portfolio and will never have one.
    candidate_id_fk: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
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
        Index("ix_draft_approvals_candidate", "candidate_id_fk"),
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
    """**v2 legacy, read-only.** Calibration dataset for v2 dedup (NTS_090).

    Written only by ``dedup_service.DedupEngine`` on the v2 path. The v3
    deduper (``selector.candidate_dedup``, three windows per NTS_098 §3) does
    NOT write here: its calibration data is ``candidates.reason_code`` plus the
    dedup history the Portfolio card renders. Read by nothing but
    ``scripts/tune_dedup.py``.

    Original rationale below.

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
    """**v2 legacy, read-only.** LLM-as-judge score per draft+language (NTS_091).

    Written by ``judge.score_draft``, whose only caller is ``pipeline.run`` —
    so no new row appears while ``v2_generation_enabled`` is off. Still read by
    ``GET /eval/summary`` and the drafts list. NTS_114 S10 puts the judge into
    the real v3 cycle (NTS_080); until then this is history.

    Original rationale below.

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


# =========================================================================
# v3 contour 1 — portfolio (NTS_098 / NTS_099 / NTS_101 / NTS_107 / NTS_109)
# Created by migration 020. Nothing reads these yet: S2 fills ``candidates``
# from the intake run, S3 renders them, S4 selects out of them.
# =========================================================================


# Guard verdict vocabulary (NTS_099 §3) — kept as module constants so the
# guard, the API schemas and the UI filters all spell them the same way.
CANDIDATE_INPUT_KINDS = ("document", "news")
CANDIDATE_VERDICTS = ("accept", "reject")
CANDIDATE_REASON_CODES = (
    "ok",
    "personnel",
    "forecast",
    "award_pr",
    "no_document",
    "no_consequence",
    "out_of_jurisdiction",
    "out_of_scope",
    "duplicate_stage",
    "retail_crypto",
    "daily_cap",
    "guard_error",
)
CANDIDATE_EVENT_STAGES = (
    "consultation",
    "adopted",
    "in_force",
    "ruling",
    "deal_announced",
    "deal_closed",
    "list_update",
    "other",
)
CANDIDATE_DEPTHS = ("note", "article", "deep")
# NTS_098 §2. Terminal: published, expired, failed, superseded, rejected.
CANDIDATE_STATUSES = (
    "pending",
    "selected",
    "in_production",
    "drafted",
    "returned",
    "ready",
    "published",
    "doc_missing",
    "expired",
    "failed",
    "superseded",
    "rejected",
)
# The subset a candidate is "alive" in — the live dedup window (NTS_098 §3).
CANDIDATE_LIVE_STATUSES = (
    "pending",
    "doc_missing",
    "selected",
    "in_production",
    "drafted",
    "returned",
    "ready",
)
CANDIDATE_MANUAL_ACTIONS = ("promoted", "held", "rejected")
REVIEW_ACTIONS = ("approve", "return", "reject", "hold", "promote", "disagree_guard")
SOURCE_ROLES = ("news", "primary_feed", "primary_site")
SOURCE_CLASSES = (
    "regulator",
    "tax_authority",
    "legislation",
    "jurisdiction_list",
    "filings",
    "court",
    "professional_alert",
    "corporate_pr",
    "news",
)
LICENSE_CLASSES = (
    "public_official",
    "public_domain",
    "corporate_pr",
    "professional_commentary",
    "news_paywalled",
)
FETCH_METHODS = ("rss", "atom", "html_list", "edgar_fts")
RUN_TYPES = ("intake", "production", "publish", "ttl")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Candidate(Base):
    """Where a topic lives between "spotted" and "published" (NTS_098 §1).

    **Self-contained by design.** The feed item is *copied* into the
    ``source_*`` columns rather than joined, because an RSS item routinely
    falls off the end of a feed within hours while a candidate can sit in the
    portfolio for two weeks. ``source_id_fk`` is a provenance pointer, not a
    read path.

    The status machine (NTS_098 §2) is enforced in code, not here: SQLite
    cannot express "pending → selected only", and a CHECK per transition
    would be unmaintainable. The CHECK on ``status`` bounds the vocabulary;
    the transition rules and their tests live with the selector (S4).
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_kind: Mapped[str] = mapped_column(String, nullable=False)

    # --- source snapshot (copied, see docstring)
    source_id_fk: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=True
    )
    source_title: Mapped[str] = mapped_column(Text, nullable=False)
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    source_language: Mapped[str | None] = mapped_column(String, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String, nullable=True)
    source_class: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- dedup (NTS_079)
    topic_embedding_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- guard verdict (NTS_099 §3). ``reason`` is required even on accept:
    # it is the sentence Andriy reads when proofreading the rubric.
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    service_category: Mapped[str | None] = mapped_column(String, nullable=True)
    jurisdictions: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    # Ranking only — length is decided by depth_final after research.
    depth_prior: Mapped[str | None] = mapped_column(String, nullable=True)
    depth_final: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- primary document (NTS_101 v2)
    primary_doc_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_doc_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_match: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_language_expected: Mapped[str | None] = mapped_column(String, nullable=True)
    # NTS_101 §2-7 / NTS_114 S5 — JSON array of the section labels the
    # extraction actually read ("art. 4", "annex II"). This is the last link of
    # the traceability chain: from a published article, through the candidate,
    # to the parts of the document the numbers came from. **Writer arrives in
    # S5**; migration 025 puts the column in so the chain is complete in the
    # schema before the fetcher exists (NTS_121 §3).
    doc_sections_used: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- lifecycle (NTS_098 §2)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_action: Mapped[str | None] = mapped_column(String, nullable=True)
    manual_by: Mapped[str | None] = mapped_column(String, nullable=True)
    manual_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Rejected by the daily cap rather than by the rubric — kept visible so a
    # manager can promote it the same day (NTS_099 §5).
    cap_overflow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Required from ``drafted`` onward — the link to the Sanity draft.
    sanity_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    publication_slot: Mapped[date | None] = mapped_column(Date, nullable=True)
    # EN canon was edited after translation (NTS_107 §4).
    canon_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set on the NEW candidate when a later stage of the same event arrives.
    supersedes_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            _check_in("input_kind", CANDIDATE_INPUT_KINDS),
            name="ck_candidates_input_kind",
        ),
        CheckConstraint(
            _check_in("verdict", CANDIDATE_VERDICTS), name="ck_candidates_verdict"
        ),
        CheckConstraint(
            _check_in("reason_code", CANDIDATE_REASON_CODES),
            name="ck_candidates_reason_code",
        ),
        CheckConstraint(
            _check_in("event_stage", CANDIDATE_EVENT_STAGES),
            name="ck_candidates_event_stage",
        ),
        CheckConstraint(
            _check_in("depth_prior", CANDIDATE_DEPTHS),
            name="ck_candidates_depth_prior",
        ),
        CheckConstraint(
            _check_in("depth_final", CANDIDATE_DEPTHS),
            name="ck_candidates_depth_final",
        ),
        CheckConstraint(
            _check_in("status", CANDIDATE_STATUSES), name="ck_candidates_status"
        ),
        CheckConstraint(
            _check_in("manual_action", CANDIDATE_MANUAL_ACTIONS),
            name="ck_candidates_manual_action",
        ),
        CheckConstraint(
            _check_in("source_class", SOURCE_CLASSES),
            name="ck_candidates_source_class",
        ),
        Index("ix_candidates_brand_status", "brand_id_fk", "status"),
        Index("ix_candidates_brand_created", "brand_id_fk", "created_at"),
        Index("ix_candidates_sanity_draft", "sanity_draft_id"),
        Index("ix_candidates_status_expires", "status", "expires_at"),
        Index("ix_candidates_primary_doc", "primary_doc_url"),
    )


class ReviewDecision(Base):
    """One row per editor action, with the review timer (NTS_107 §5).

    The only free signal for tuning the guard rubric and the rank weights
    (NTS_113), which is why the candidate FK is RESTRICT: a candidate cannot
    be deleted out from under its own decision history.
    """

    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer, ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_id_fk: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    # Which part the editor sent back — free text, e.g. "attribution", "lead".
    scope: Mapped[str | None] = mapped_column(String, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str] = mapped_column(
        String, nullable=False, default="admin", server_default="admin"
    )
    # Card timer, seconds. Feeds the "review time per article" acceptance
    # metric in NTS_114 (target ≤ 25 min).
    time_spent_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            _check_in("action", REVIEW_ACTIONS), name="ck_review_decisions_action"
        ),
        Index("ix_review_decisions_brand_at", "brand_id_fk", "at"),
        Index("ix_review_decisions_candidate", "candidate_id_fk"),
    )


class BrandTaxonomy(Base):
    """A brand's services, per-brand instead of a hardcoded enum (NTS_109).

    ``description_for_guard`` is what the rubric's ``{services}`` placeholder
    renders; ``service_url_path`` is what NTS_093 internal linking resolves
    against. Onboarding a second brand becomes rows here, not a code change.
    """

    __tablename__ = "brand_taxonomy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer, ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description_for_guard: Mapped[str] = mapped_column(Text, nullable=False)
    service_url_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("brand_id_fk", "key", name="uq_brand_taxonomy_brand_key"),
    )


class FactPack(Base):
    """The material an article was built on, kept (NTS_096 part A).

    Before this table the research pack was parsed, used and thrown away. The
    consequence, recorded in NTS_096: reconstructing the line-by-line
    provenance of one published article required a **new paid research call**,
    and it still came back with a different set of sources — so the question
    "where did this number come from" had no answer at all.

    One row per research call, **including calls for topics that never
    publish**: a pack that only survives successful articles cannot explain why
    the others came out thin.

    ``candidate_id_fk`` is nullable on purpose and will stay NULL until the S4
    production path hands a candidate to the generator — on the v2 path there
    is no candidate to point at. ``sanity_draft_id`` is the article side of the
    same chain, ``primary_doc_url`` + ``doc_sections_used`` the document side.
    Together with ``candidates`` they are the through-line NTS_121 §6 asks for:
    published article → draft → candidate → document → sections.

    ``doc_text`` holds the **extracted** text, never the source PDF
    (NTS_096 §Риски — storage volume).
    """

    __tablename__ = "fact_packs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id_fk: Mapped[int] = mapped_column(
        Integer, ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    # RESTRICT would block the rejected-candidate prune (NTS_098 §2) on any
    # candidate that ever got a pack; SET NULL keeps the pack and the audit.
    candidate_id_fk: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    # The v2 identifier for the same thing, so packs written before S4 are not
    # orphans: ``topics.topic_id``, not ``topics.id``.
    topic_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sanity_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Parsed pack as JSON-as-TEXT — never the raw provider dump (NTS_096).
    pack: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    primary_doc_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_sections_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("ix_fact_packs_candidate", "candidate_id_fk"),
        Index("ix_fact_packs_draft", "sanity_draft_id"),
        Index("ix_fact_packs_topic", "topic_id"),
        Index("ix_fact_packs_brand_created", "brand_id_fk", "created_at"),
    )
