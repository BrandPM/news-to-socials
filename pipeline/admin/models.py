"""SQLAlchemy 2.0 models for the admin database.

Column names and CHECK constraints follow IT_PROJ_NTS_014 §"Модель данных"
exactly. Five tables: sources, prompts, pipeline_config, runs, topics.

Cascade rules:
- ``topics.run_id``     → runs.id      ON DELETE CASCADE
  (deleting a run removes its per-topic detail; we never want orphan rows)
- ``topics.source_id``  → sources.id   ON DELETE RESTRICT
  (a source with historical topic rows cannot be silently deleted; the
   /sources DELETE endpoint returns 409 if any topics still reference it,
   so the operator can choose whether to keep history or wipe runs first)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
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
        Index("ix_sources_brand_active", "brand_id", "active"),
    )


class Prompt(Base):
    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
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
            "brand_id",
            "prompt_type",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )


class PipelineConfig(Base):
    __tablename__ = "pipeline_config"

    brand_id: Mapped[str] = mapped_column(String, primary_key=True)
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
    brand_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
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
            "status IN ('running', 'success', 'failed')",
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
