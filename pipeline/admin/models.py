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
    ForeignKey,
    Index,
    Integer,
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
            "'topic_picker', 'image_prompt')",
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
    stats: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'dry_run')",
            name="ck_runs_status",
        ),
        Index("ix_runs_started_at", "started_at"),
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
        UniqueConstraint("run_id", "topic_id", name="uq_topics_run_topic"),
    )
