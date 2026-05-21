"""Bridge between the pipeline and admin.db.

The pipeline (``pipeline/run.py``) was previously parameterised by a single
hardcoded ``icon_brand_config()`` call. With the admin UI in flight we
want the same orchestrator to:

* read its source list from ``admin.db`` (sources table where active=1)
* read the active ``writer_polish`` prompt from ``admin.db``
* read scoring threshold / topics-per-run / voice profile from
  ``admin.db`` (pipeline_config row)
* write per-run history to ``admin.db`` (runs + topics tables)

…BUT keep the existing systemd timer working through the rollout
window. So when admin.db is absent or empty, we transparently fall
back to the hardcoded ``icon_brand_config()``. The fallback is an
invariant (Admin-UI-Specific Invariant B in the NTS_014 spec).

The client is sync. The pipeline calls into it from async code via
``run_in_threadpool`` where needed — SQLite is serial anyway and the
admin tables are tiny.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin import seed_data
from pipeline.admin.models import (
    PipelineConfig,
    Prompt,
    Run,
    Source,
    Topic,
)
from pipeline.common.config import get_settings


@dataclass(frozen=True)
class SourceRecord:
    id: int | None  # None when sourced from the hardcoded fallback
    name: str
    source_type: str
    url: str
    primary_category: str
    polling_minutes: int


@dataclass(frozen=True)
class ConfigRecord:
    scoring_threshold: int
    topics_per_run: int
    banned_phrases: list[str]
    voice_profile: str


class AdminConfigClient:
    """Read/write the admin DB. Falls back to ``icon_brand_config()``
    transparently when admin.db is absent or empty.

    The check is performed lazily on each call — if the operator runs
    ``alembic upgrade head`` mid-session, the pipeline will pick up the
    new schema on the next run without a process restart.
    """

    def __init__(self, brand_id: str = "icon") -> None:
        self.brand_id = brand_id

    # --- existence checks ----------------------------------------------

    def admin_db_available(self) -> bool:
        """``True`` iff the SQLite file exists AND has the sources table."""
        path = Path(get_settings().admin_db_path).expanduser()
        if not path.exists():
            return False
        try:
            factory = admin_db.get_session_factory()
            with factory() as session:
                session.execute(select(Source).limit(1))
            return True
        except Exception:  # noqa: BLE001
            return False

    # --- sources --------------------------------------------------------

    def get_active_sources(self) -> list[SourceRecord]:
        """Active sources for ``brand_id``. Falls back to seed data when
        admin.db is missing OR contains zero active sources for this brand.

        The "zero active sources" check is important: an admin user might
        deactivate every source temporarily, and we don't want the timer
        to drift back to hardcoded URLs at that point. So we only fall
        back when admin.db itself is unusable (missing/no table).
        """
        if not self.admin_db_available():
            return self._fallback_sources()
        factory = admin_db.get_session_factory()
        with factory() as session:
            rows = session.scalars(
                select(Source).where(
                    Source.brand_id == self.brand_id, Source.active.is_(True)
                )
            ).all()
        if not rows:
            # Schema present but no active rows — still fall back so the
            # systemd timer keeps producing drafts. The admin UI is the
            # right place to fix this; the pipeline shouldn't fail silently.
            return self._fallback_sources()
        return [
            SourceRecord(
                id=r.id,
                name=r.name,
                source_type=r.source_type,
                url=r.url,
                primary_category=r.primary_category,
                polling_minutes=r.polling_minutes,
            )
            for r in rows
        ]

    def _fallback_sources(self) -> list[SourceRecord]:
        return [
            SourceRecord(
                id=None,
                name=s.name,
                source_type=s.source_type,
                url=s.url,
                primary_category=s.primary_category,
                polling_minutes=s.polling_minutes,
            )
            for s in seed_data.ICON_SEED_SOURCES
            if s.active
        ]

    # --- prompts --------------------------------------------------------

    def get_active_prompt(self, prompt_type: str) -> tuple[str, str] | None:
        """Return ``(version_name, content)`` for the active prompt or
        ``None`` if no active prompt exists. The pipeline falls back to
        the in-repo prompt module when this returns None.
        """
        if not self.admin_db_available():
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.scalars(
                select(Prompt).where(
                    Prompt.brand_id == self.brand_id,
                    Prompt.prompt_type == prompt_type,
                    Prompt.is_active.is_(True),
                )
            ).first()
        if row is None:
            return None
        return row.version_name, row.content

    # --- config ---------------------------------------------------------

    def get_config(self) -> ConfigRecord:
        """Return the live config row. Falls back to hardcoded defaults
        when admin.db is missing or has no row for this brand.
        """
        if self.admin_db_available():
            factory = admin_db.get_session_factory()
            with factory() as session:
                row = session.get(PipelineConfig, self.brand_id)
            if row is not None:
                banned = (
                    json.loads(row.banned_phrases) if row.banned_phrases else []
                )
                return ConfigRecord(
                    scoring_threshold=row.scoring_threshold,
                    topics_per_run=row.topics_per_run,
                    banned_phrases=banned,
                    voice_profile=row.voice_profile,
                )
        # Fallback: pull the voice_profile YAML straight from the hardcoded
        # brand config, parse banned phrases out of it.
        from pipeline.generator.comment_writer import parse_voice_guardrails  # noqa: PLC0415
        from pipeline.run import icon_brand_config  # noqa: PLC0415

        brand = icon_brand_config()
        banned, _ = parse_voice_guardrails(brand.voice_profile_yaml)
        return ConfigRecord(
            scoring_threshold=seed_data.ICON_SEED_THRESHOLD,
            topics_per_run=seed_data.ICON_SEED_TOPICS_PER_RUN,
            banned_phrases=banned,
            voice_profile=brand.voice_profile_yaml,
        )

    # --- run history ----------------------------------------------------

    def record_run_start(
        self,
        source_ids: list[int],
        triggered_by: str = "cron",
    ) -> int | None:
        """Create a new ``runs`` row and return its id. Returns ``None``
        if admin.db is unavailable (fallback path).
        """
        if not self.admin_db_available():
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            run = Run(
                brand_id=self.brand_id,
                triggered_by=triggered_by,
                source_ids=json.dumps(source_ids),
                started_at=datetime.now(tz=timezone.utc),
                status="running",
            )
            session.add(run)
            session.commit()
            return run.id

    def record_run_finish(
        self,
        run_id: int,
        *,
        status: str,
        stats: dict[str, Any] | None = None,
        log_excerpt: str | None = None,
    ) -> None:
        if run_id is None:
            return
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                return
            row.status = status
            row.finished_at = datetime.now(tz=timezone.utc)
            if stats is not None:
                row.stats = json.dumps(stats)
            if log_excerpt is not None:
                row.log_excerpt = log_excerpt
            session.commit()

    def record_topic_result(
        self,
        *,
        run_id: int | None,
        topic_id: str,
        source_id: int | None,
        title: str,
        url: str | None,
        score: int | None,
        status: str,
        filter_reason: str | None = None,
        draft_id: str | None = None,
    ) -> None:
        """Record a per-topic row. No-ops when run_id or source_id is None
        (fallback path doesn't have DB rows to FK against).
        """
        if run_id is None or source_id is None:
            return
        factory = admin_db.get_session_factory()
        with factory() as session:
            session.add(
                Topic(
                    run_id=run_id,
                    topic_id=topic_id,
                    source_id=source_id,
                    title=title,
                    url=url,
                    score=score,
                    status=status,
                    filter_reason=filter_reason,
                    draft_id=draft_id,
                )
            )
            session.commit()

    def get_run_source_ids(self, run_id: int) -> list[int]:
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                raise LookupError(f"run {run_id} not found")
            return json.loads(row.source_ids)
