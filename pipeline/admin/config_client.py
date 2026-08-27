"""Bridge between the pipeline and admin.db (multi-brand aware).

The pipeline reads its source list / config / active prompts from
``admin.db`` through this client. Step 2 of S3 (NTS_025) refactors all
internal queries to use the ``brand_id_fk`` integer FK; the public
interface still takes a brand slug for back-compat with the systemd
ExecStart and the existing tests. Step 4 will switch the constructor
to take ``brand_id: int`` directly and add a ``BrandNotReadyError``
path.

When admin.db is missing OR the brand row doesn't exist yet, ``get_*``
methods fall back to the hardcoded seed list — see Admin-UI-Specific
Invariant B (NTS_014). This back-compat path is removed in Step 4.
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
    Brand,
    CostRecord,
    PipelineConfig,
    Prompt,
    Run,
    Source,
    SourceHealthRecord,
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
    # NTS_090 — dedup tunables (defaults match the migration server_defaults;
    # used when a config row predates the columns / the fallback path).
    dedup_enabled: bool = True
    dedup_threshold: float = 0.85
    dedup_window_days: int = 7
    # NTS_091 — LLM-judge eval tunables.
    eval_enabled: bool = True
    eval_threshold: float = 7.0
    # NTS_094 — cover generation on manager demand. Defaults to False so a
    # config row that predates the column (or the hardcoded fallback path)
    # keeps generating covers during the run exactly as before.
    images_on_demand: bool = False
    # NTS_092 — research-stage switch + budgets. Defaults match the migration
    # server_defaults so a config row predating the columns (or the hardcoded
    # fallback path) still researches under sane limits.
    research_enabled: bool = True
    research_max_sources: int = 5
    research_max_tokens: int = 2000
    research_timeout_seconds: int = 60


class BrandNotReadyError(RuntimeError):
    """Raised by pipeline entry points when a brand's row is missing,
    paused/archived/draft, or has empty Sanity credentials. The pipeline
    refuses to run in that state — no silent fallback (NTS_025 M4)."""


@dataclass(frozen=True)
class BrandRecord:
    """In-memory snapshot of one brands row.

    ``sanity_api_token`` etc are populated lazily through
    ``decrypted_sanity_token()`` so plaintext credentials never sit on
    a long-lived attribute (M3 carve-out).
    """

    id: int
    slug: str
    name: str
    language: str
    timezone: str
    status: str
    active: bool
    sanity_project_id: str | None
    sanity_dataset: str | None
    sanity_api_version: str | None
    sanity_api_token_enc: str | None
    sanity_studio_url: str | None
    telegram_bot_token_enc: str | None
    telegram_channel_id: str | None
    meta_app_id: str | None
    meta_app_secret_enc: str | None
    meta_access_token_enc: str | None
    meta_page_id: str | None
    meta_ig_business_id: str | None
    voice_profile_yaml: str | None
    # S6 — JSON-encoded list of language codes this brand publishes in.
    # Default ``["en"]`` so legacy brands keep their single-language run.
    languages: str = '["en"]'

    def decrypted_sanity_token(self) -> str | None:
        from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

        return get_encryption().decrypt_or_none(self.sanity_api_token_enc)

    def decrypted_telegram_token(self) -> str | None:
        from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

        return get_encryption().decrypt_or_none(self.telegram_bot_token_enc)

    def decrypted_meta_app_secret(self) -> str | None:
        from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

        return get_encryption().decrypt_or_none(self.meta_app_secret_enc)

    def decrypted_meta_access_token(self) -> str | None:
        from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

        return get_encryption().decrypt_or_none(self.meta_access_token_enc)

    @property
    def has_sanity_token(self) -> bool:
        return bool(self.sanity_api_token_enc)


def _brand_row_to_record(row: Brand) -> BrandRecord:
    return BrandRecord(
        id=row.id,
        slug=row.slug,
        name=row.name,
        language=row.language,
        timezone=row.timezone,
        status=row.status,
        active=row.active,
        sanity_project_id=row.sanity_project_id,
        sanity_dataset=row.sanity_dataset,
        sanity_api_version=row.sanity_api_version,
        sanity_api_token_enc=row.sanity_api_token_enc,
        sanity_studio_url=row.sanity_studio_url,
        telegram_bot_token_enc=row.telegram_bot_token_enc,
        telegram_channel_id=row.telegram_channel_id,
        meta_app_id=row.meta_app_id,
        meta_app_secret_enc=row.meta_app_secret_enc,
        meta_access_token_enc=row.meta_access_token_enc,
        meta_page_id=row.meta_page_id,
        meta_ig_business_id=row.meta_ig_business_id,
        voice_profile_yaml=row.voice_profile_yaml,
        languages=row.languages or '["en"]',
    )


def get_brand(brand_id_or_slug: int | str) -> BrandRecord:
    """Fetch a brand by id (int) or slug (str). Raises ``LookupError`` when
    no brand matches.
    """
    factory = admin_db.get_session_factory()
    with factory() as session:
        if isinstance(brand_id_or_slug, int):
            row = session.get(Brand, brand_id_or_slug)
        else:
            row = session.execute(
                select(Brand).where(Brand.slug == brand_id_or_slug)
            ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"brand {brand_id_or_slug!r} not found")
    return _brand_row_to_record(row)


def list_brands() -> list[BrandRecord]:
    """All brands as BrandRecord (no decrypted tokens; call decrypted_*
    methods on the returned record when you need plaintext)."""
    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.scalars(select(Brand).order_by(Brand.slug)).all()
    return [_brand_row_to_record(r) for r in rows]


def get_active_brand_ids() -> list[int]:
    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.scalars(
            select(Brand.id).where(Brand.active.is_(True))
        ).all()
    return list(rows)


class AdminConfigClient:
    """Read/write admin.db scoped to a single brand.

    ``brand_slug`` selects the brand; the constructor does NOT load the
    row — that happens lazily on the first method call so a missing
    admin.db doesn't blow up at import time.
    """

    def __init__(self, brand_slug: str = "icon") -> None:
        self.brand_slug = brand_slug

    # --- existence checks ----------------------------------------------

    def admin_db_available(self) -> bool:
        """``True`` iff the SQLite file exists AND has the brands table."""
        path = Path(get_settings().admin_db_path).expanduser()
        if not path.exists():
            return False
        try:
            factory = admin_db.get_session_factory()
            with factory() as session:
                session.execute(select(Brand).limit(1))
            return True
        except Exception:  # noqa: BLE001
            return False

    def _resolve_brand_id_fk(self) -> int | None:
        """Look up brands.id for ``self.brand_slug``. Returns ``None``
        when admin.db is missing or the brand row doesn't exist."""
        if not self.admin_db_available():
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.execute(
                select(Brand).where(Brand.slug == self.brand_slug)
            ).scalar_one_or_none()
        return row.id if row is not None else None

    # --- sources --------------------------------------------------------

    def get_active_sources(self) -> list[SourceRecord]:
        """Active sources for this brand. Falls back to seed data when
        admin.db is missing, brand row absent, or zero active sources."""
        brand_id_fk = self._resolve_brand_id_fk()
        if brand_id_fk is None:
            return self._fallback_sources()
        factory = admin_db.get_session_factory()
        with factory() as session:
            rows = session.scalars(
                select(Source).where(
                    Source.brand_id_fk == brand_id_fk, Source.active.is_(True)
                )
            ).all()
        if not rows:
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
        ``None`` if no active prompt exists."""
        brand_id_fk = self._resolve_brand_id_fk()
        if brand_id_fk is None:
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.scalars(
                select(Prompt).where(
                    Prompt.brand_id_fk == brand_id_fk,
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
        when admin.db is missing, brand absent, or no config row."""
        brand_id_fk = self._resolve_brand_id_fk()
        if brand_id_fk is not None:
            factory = admin_db.get_session_factory()
            with factory() as session:
                row = session.get(PipelineConfig, brand_id_fk)
            if row is not None:
                banned = (
                    json.loads(row.banned_phrases) if row.banned_phrases else []
                )
                return ConfigRecord(
                    scoring_threshold=row.scoring_threshold,
                    topics_per_run=row.topics_per_run,
                    banned_phrases=banned,
                    voice_profile=row.voice_profile,
                    dedup_enabled=bool(getattr(row, "dedup_enabled", True)),
                    dedup_threshold=float(getattr(row, "dedup_threshold", 0.85)),
                    dedup_window_days=int(getattr(row, "dedup_window_days", 7)),
                    eval_enabled=bool(getattr(row, "eval_enabled", True)),
                    eval_threshold=float(getattr(row, "eval_threshold", 7.0)),
                    images_on_demand=bool(
                        getattr(row, "images_on_demand", False)
                    ),
                    research_enabled=bool(getattr(row, "research_enabled", True)),
                    research_max_sources=int(
                        getattr(row, "research_max_sources", 5) or 5
                    ),
                    research_max_tokens=int(
                        getattr(row, "research_max_tokens", 2000) or 2000
                    ),
                    research_timeout_seconds=int(
                        getattr(row, "research_timeout_seconds", 60) or 60
                    ),
                )
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
        if admin.db / brand row are unavailable."""
        brand_id_fk = self._resolve_brand_id_fk()
        if brand_id_fk is None:
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            run = Run(
                brand_id_fk=brand_id_fk,
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
        run_id: int | None,
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
        language: str = "en",
    ) -> None:
        """Record a per-topic row. No-ops when run_id or source_id is None
        (fallback path doesn't have DB rows to FK against)."""
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
                    language=language,
                )
            )
            session.commit()

    def mark_language_completed(
        self, run_id: int | None, language: str
    ) -> None:
        """Append ``language`` to ``runs.languages_completed`` if not present.

        S6 fanout calls this once per language as that language's branch
        finishes (success OR failure isolated to one language). Use a
        SELECT-for-UPDATE-like pattern within a single session so concurrent
        gather() branches don't lose appends. SQLite serialises writes, so
        the JSON read-modify-write is safe enough at our scale.
        """
        if run_id is None:
            return
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                return
            try:
                current = json.loads(row.languages_completed or "[]")
                if not isinstance(current, list):
                    current = []
            except (ValueError, TypeError):
                current = []
            if language not in current:
                current.append(language)
                row.languages_completed = json.dumps(current)
                session.commit()

    # --- live run progress (NTS_068) ------------------------------------

    def update_run_progress(self, run_id: int | None, **fields: Any) -> None:
        """Merge ``fields`` into ``runs.progress`` (JSON). Best-effort: any
        failure is swallowed so progress reporting never breaks a run.

        ``None`` values are skipped so callers can pass only what changed."""
        if run_id is None:
            return
        try:
            factory = admin_db.get_session_factory()
            with factory() as session:
                row = session.get(Run, run_id)
                if row is None:
                    return
                try:
                    current = json.loads(row.progress or "{}")
                    if not isinstance(current, dict):
                        current = {}
                except (ValueError, TypeError):
                    current = {}
                current.update({k: v for k, v in fields.items() if v is not None})
                row.progress = json.dumps(current)
                session.commit()
        except Exception:  # noqa: BLE001 — progress is never load-bearing
            pass

    def set_run_running(
        self, run_id: int | None, stats: dict[str, Any] | None = None
    ) -> None:
        """Re-assert a run as ``running`` (clearing ``finished_at``).

        ``run_pipeline_for_run`` calls this between sources: each per-source
        ``run_pipeline`` finalises the shared run row, which would otherwise
        flip the status to terminal mid-fanout and make the global indicator
        read "completed" seconds into a multi-source pass."""
        if run_id is None:
            return
        try:
            factory = admin_db.get_session_factory()
            with factory() as session:
                row = session.get(Run, run_id)
                if row is None:
                    return
                row.status = "running"
                row.finished_at = None
                if stats is not None:
                    row.stats = json.dumps(stats)
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    def get_run_stats(self, run_id: int | None) -> dict[str, Any]:
        """Return the run's current ``stats`` dict (``{}`` if absent)."""
        if run_id is None:
            return {}
        try:
            factory = admin_db.get_session_factory()
            with factory() as session:
                row = session.get(Run, run_id)
                if row is None or not row.stats:
                    return {}
                parsed = json.loads(row.stats)
                return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    # --- cost recording -------------------------------------------------

    @staticmethod
    def record_cost(
        *,
        brand_id_fk: int,
        provider: str,
        operation: str,
        cost_usd: float,
        run_id: int | None = None,
        topic_id: int | None = None,
        draft_id: str | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        duration_seconds: float | None = None,
    ) -> int | None:
        """Write one ``cost_records`` row. Returns the inserted row id,
        or ``None`` if admin.db isn't reachable.

        Static because the cost record lives outside the per-instance
        ``brand_slug`` scope — the brand is supplied explicitly. See
        ``pipeline.admin.cost_recorder.record_cost`` for the high-level
        wrapper that pulls brand/run/topic from the context var.
        """
        path = Path(get_settings().admin_db_path).expanduser()
        if not path.exists():
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = CostRecord(
                brand_id_fk=brand_id_fk,
                run_id=run_id,
                topic_id=topic_id,
                draft_id=draft_id,
                provider=provider,
                operation=operation,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_seconds=duration_seconds,
                cost_usd=cost_usd,
            )
            session.add(row)
            session.commit()
            return row.id

    # --- source health (S5 Step 6) --------------------------------------

    @staticmethod
    def record_source_health(
        *,
        source_id: int,
        brand_id_fk: int,
        success: bool,
        articles_count: int,
        error_msg: str | None = None,
    ) -> int | None:
        """Write one ``source_health_records`` row. Returns the inserted
        row id or ``None`` if admin.db isn't reachable.

        Static because we call it from the pipeline runtime where the
        per-instance ``brand_slug`` isn't necessarily set (cron path).
        """
        path = Path(get_settings().admin_db_path).expanduser()
        if not path.exists():
            return None
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = SourceHealthRecord(
                source_id=source_id,
                brand_id_fk=brand_id_fk,
                fetched_at=datetime.now(tz=timezone.utc),
                success=success,
                articles_count=articles_count,
                error_msg=(error_msg[:500] if error_msg else None),
            )
            session.add(row)
            session.commit()
            return row.id

    def get_run_source_ids(self, run_id: int) -> list[int]:
        factory = admin_db.get_session_factory()
        with factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                raise LookupError(f"run {run_id} not found")
            return json.loads(row.source_ids)
