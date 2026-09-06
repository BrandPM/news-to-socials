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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
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
from pipeline.common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SourceRecord:
    id: int | None  # None when sourced from the hardcoded fallback
    name: str
    source_type: str
    url: str
    primary_category: str
    polling_minutes: int
    # NTS_101 §1 registry fields, added by migration 020 and read by the v3
    # intake run. Defaults describe a pre-v3 news feed, which is what every
    # row that predates the columns actually is — and what the hardcoded
    # fallback sources are. ``source_role`` decides the item's ``input_kind``
    # and which prefilter age limit applies, so a wrong default here would
    # quietly route regulator documents through the news rules.
    source_role: str = "news"
    source_class: str = "news"
    license_class: str = "news_paywalled"
    doc_language: str | None = None
    fetch_method: str | None = None
    # NTS_101 §1/§5 — how long a document off this source may be served from
    # the cache. ``None`` means "always refetch": an unclassified source is one
    # nobody has decided about, and the safe reading is that its content moves.
    cache_ttl_days: int | None = None


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
    # === v3 contour-1 keys (NTS_098 §4, NTS_099 §1) — migration 020 =======
    # Defaults mirror the migration server_defaults so the hardcoded-fallback
    # path (admin.db missing / brand absent) still answers every read with the
    # spec's Icon starting values instead of None.
    #
    # The five JSON-as-TEXT columns are parsed HERE, once, into real Python
    # objects. The pipeline must never see the raw string: a caller that has
    # to json.loads a config value is a caller that will eventually forget.
    publication_slots: tuple[dict[str, Any], ...] = (
        {"day": "mon", "capacity": 2},
        {"day": "thu", "capacity": 2},
    )
    weekly_draft_budget: int = 6
    portfolio_daily_cap_document: int = 2
    portfolio_daily_cap_news: int = 1
    candidate_ttl_days: Mapping[str, int] = MappingProxyType(
        {
            "deal_announced": 7,
            "deal_closed": 7,
            "consultation": 21,
            "default": 14,
        }
    )
    production_timeout_min: int = 60
    max_attempts: int = 2
    brand_timezone: str = "Europe/Madrid"
    retention_days_rejected: int = 30
    dedup_threshold_live: float = 0.90
    dedup_threshold_rejected: float = 0.92
    dedup_window_rejected_days: int = 14
    dedup_threshold_published: float = 0.88
    dedup_window_published_days: int = 60
    jurisdiction_tiers: Mapping[str, tuple[str, ...]] = MappingProxyType(
        {
            "tier1": ("CH", "CY", "MT", "AE", "UK", "PL", "UA", "LI", "EU"),
            "tier2": (
                "US", "SG", "HK", "LU", "MC", "PT", "ES", "IT",
                "DE", "AT", "IL", "KZ", "TR",
            ),
        }
    )
    depth_article_min_facts: int = 4
    depth_deep_min_facts: int = 10
    monthly_spend_cap_usd: float = 150.0
    max_cost_per_candidate_usd: float = 5.0
    prefilter_deny_title_patterns: tuple[str, ...] = (
        "appoints", "hires", "joins", "named as", "wins award", "ranked",
        "opens office", "rebrand", "outlook", "forecast", "analysts expect",
    )
    prefilter_require_summary: bool = True
    prefilter_max_age_hours_news: int = 72
    prefilter_max_age_hours_primary: int = 240
    prefilter_languages: tuple[str, ...] = (
        "en", "de", "fr", "it", "pl", "uk", "ru", "el",
    )
    prefilter_min_summary_chars: int = 80
    # --- v3 mode flags (NTS_103) — migration 022. Both default False, which
    # is also the safe answer on the hardcoded-fallback path: a runtime that
    # cannot read admin.db must not decide on its own to spend money.
    intake_enabled: bool = False
    v2_generation_enabled: bool = False
    # NTS_114 S4 — the v3 generation path, migration 026. Same rule as the two
    # above on the fallback path: a runtime that cannot read admin.db must not
    # decide on its own to start producing.
    production_enabled: bool = False
    # --- Primary document fetch budgets (NTS_101 §4) — migration 027.
    doc_timeout_s: int = 60
    doc_max_mb: int = 25
    doc_max_tokens_for_composition: int = 12000
    doc_retries: int = 2
    doc_match_model: str = "gpt-4o-mini"
    # --- Composition (NTS_102 v2, NTS_095, NTS_108 §1) — migration 028.
    data_blocks_enabled: bool = False
    depth_length_targets: Mapping[str, tuple[int, int | None]] = MappingProxyType(
        {"note": (300, 450), "article": (600, 900), "deep": (1200, None)}
    )
    max_quote_words: Mapping[str, int] = MappingProxyType(
        {"professional_commentary": 15, "corporate_pr": 25, "news_paywalled": 0}
    )
    attribution_model: str = "gpt-4o-mini"
    # NTS_112 — data | flux.
    cover_mode: str = "flux"
    # NTS_100 §2 — the rank formula's seven weights, parsed here like the other
    # JSON-as-TEXT keys so the selector never sees a string.
    rank_weights: Mapping[str, float] = MappingProxyType(
        {
            "w_conf": 0.30,
            "w_depth": 0.25,
            "w_fresh": 0.15,
            "w_juris": 0.15,
            "w_kind": 0.05,
            "w_div": 0.20,
            "w_juris_div": 0.10,
        }
    )
    guard_model: str = "gpt-4o-mini"


# --- v3 contour-1 config (NTS_098 §4, NTS_099 §1) -------------------------

# A throwaway instance used only to read the dataclass field defaults; the
# four required fields are irrelevant to the v3 keys and get dummy values.
_V3_DEFAULTS = ConfigRecord(
    scoring_threshold=0, topics_per_run=0, banned_phrases=[], voice_profile=""
)


def _json_or_default(raw: Any, default: Any) -> Any:
    """Parse a JSON-as-TEXT config column, falling back to the documented
    default. A malformed value must never take the pipeline down: the config
    surface is hand-editable, and a stray comma is an operator typo, not an
    outage. The default is the same one the migration wrote.
    """
    if raw is None or raw == "":
        return default
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("config_client.bad_json_config_value", raw=str(raw)[:120])
        return default
    return parsed


def _int_or(raw: Any, default: int) -> int:
    """Coerce a config column to int, falling back on NULL/blank.

    ``0`` and ``False`` are legitimate values here (a zero daily cap means
    "accept nothing today"), so the test is against None, not truthiness.
    """
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_or(raw: Any, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _str_or(raw: Any, default: str) -> str:
    if raw is None or raw == "":
        return default
    return str(raw)


def _v3_keys(row: Any) -> dict[str, Any]:
    """Map the 25 v3 columns off a ``pipeline_config`` row.

    ``getattr`` with a default throughout, so a row written before migration
    020 (or the hardcoded fallback path) still yields a complete record
    instead of raising — same contract the NTS_090/091/092 keys use above.
    """
    d = _V3_DEFAULTS
    slots = _json_or_default(
        getattr(row, "publication_slots", None), list(d.publication_slots)
    )
    ttl = _json_or_default(
        getattr(row, "candidate_ttl_days", None), dict(d.candidate_ttl_days)
    )
    tiers = _json_or_default(
        getattr(row, "jurisdiction_tiers", None),
        {k: list(v) for k, v in d.jurisdiction_tiers.items()},
    )
    deny = _json_or_default(
        getattr(row, "prefilter_deny_title_patterns", None),
        list(d.prefilter_deny_title_patterns),
    )
    langs = _json_or_default(
        getattr(row, "prefilter_languages", None), list(d.prefilter_languages)
    )
    weights = _json_or_default(
        getattr(row, "rank_weights", None), dict(d.rank_weights)
    )
    targets = _json_or_default(
        getattr(row, "depth_length_targets", None),
        {k: list(v) for k, v in d.depth_length_targets.items()},
    )
    quotes = _json_or_default(
        getattr(row, "max_quote_words", None), dict(d.max_quote_words)
    )
    return {
        "publication_slots": tuple(slots),
        "weekly_draft_budget": _int_or(
            getattr(row, "weekly_draft_budget", None), d.weekly_draft_budget
        ),
        "portfolio_daily_cap_document": _int_or(
            getattr(row, "portfolio_daily_cap_document", None),
            d.portfolio_daily_cap_document,
        ),
        "portfolio_daily_cap_news": _int_or(
            getattr(row, "portfolio_daily_cap_news", None),
            d.portfolio_daily_cap_news,
        ),
        "candidate_ttl_days": MappingProxyType(
            {str(k): int(v) for k, v in dict(ttl).items()}
        ),
        "production_timeout_min": _int_or(
            getattr(row, "production_timeout_min", None), d.production_timeout_min
        ),
        "max_attempts": _int_or(
            getattr(row, "max_attempts", None), d.max_attempts
        ),
        "brand_timezone": _str_or(
            getattr(row, "brand_timezone", None), d.brand_timezone
        ),
        "retention_days_rejected": _int_or(
            getattr(row, "retention_days_rejected", None), d.retention_days_rejected
        ),
        "dedup_threshold_live": _float_or(
            getattr(row, "dedup_threshold_live", None), d.dedup_threshold_live
        ),
        "dedup_threshold_rejected": _float_or(
            getattr(row, "dedup_threshold_rejected", None), d.dedup_threshold_rejected
        ),
        "dedup_window_rejected_days": _int_or(
            getattr(row, "dedup_window_rejected_days", None),
            d.dedup_window_rejected_days,
        ),
        "dedup_threshold_published": _float_or(
            getattr(row, "dedup_threshold_published", None),
            d.dedup_threshold_published,
        ),
        "dedup_window_published_days": _int_or(
            getattr(row, "dedup_window_published_days", None),
            d.dedup_window_published_days,
        ),
        "jurisdiction_tiers": MappingProxyType(
            {str(k): tuple(v) for k, v in dict(tiers).items()}
        ),
        "depth_article_min_facts": _int_or(
            getattr(row, "depth_article_min_facts", None), d.depth_article_min_facts
        ),
        "depth_deep_min_facts": _int_or(
            getattr(row, "depth_deep_min_facts", None), d.depth_deep_min_facts
        ),
        "monthly_spend_cap_usd": _float_or(
            getattr(row, "monthly_spend_cap_usd", None), d.monthly_spend_cap_usd
        ),
        "max_cost_per_candidate_usd": _float_or(
            getattr(row, "max_cost_per_candidate_usd", None),
            d.max_cost_per_candidate_usd,
        ),
        "prefilter_deny_title_patterns": tuple(deny),
        "prefilter_require_summary": bool(
            getattr(row, "prefilter_require_summary", d.prefilter_require_summary)
        ),
        "prefilter_max_age_hours_news": _int_or(
            getattr(row, "prefilter_max_age_hours_news", None),
            d.prefilter_max_age_hours_news,
        ),
        "prefilter_max_age_hours_primary": _int_or(
            getattr(row, "prefilter_max_age_hours_primary", None),
            d.prefilter_max_age_hours_primary,
        ),
        "prefilter_languages": tuple(langs),
        "prefilter_min_summary_chars": _int_or(
            getattr(row, "prefilter_min_summary_chars", None),
            d.prefilter_min_summary_chars,
        ),
        # NTS_103 — mode flags. ``bool(getattr(..., default))`` and not
        # ``or default``: ``False or True`` is True, which would turn a mode
        # the operator switched OFF back on.
        "intake_enabled": bool(
            getattr(row, "intake_enabled", d.intake_enabled)
        ),
        "v2_generation_enabled": bool(
            getattr(row, "v2_generation_enabled", d.v2_generation_enabled)
        ),
        "production_enabled": bool(
            getattr(row, "production_enabled", d.production_enabled)
        ),
        "doc_timeout_s": _int_or(
            getattr(row, "doc_timeout_s", None), d.doc_timeout_s
        ),
        "doc_max_mb": _int_or(getattr(row, "doc_max_mb", None), d.doc_max_mb),
        "doc_max_tokens_for_composition": _int_or(
            getattr(row, "doc_max_tokens_for_composition", None),
            d.doc_max_tokens_for_composition,
        ),
        "doc_retries": _int_or(getattr(row, "doc_retries", None), d.doc_retries),
        "doc_match_model": _str_or(
            getattr(row, "doc_match_model", None), d.doc_match_model
        ),
        "data_blocks_enabled": bool(
            getattr(row, "data_blocks_enabled", d.data_blocks_enabled)
        ),
        # ``[min, max]`` with a null max: a tuple of two, second possibly None.
        "depth_length_targets": MappingProxyType(
            {
                str(key): (
                    int(value[0]),
                    int(value[1]) if len(value) > 1 and value[1] else None,
                )
                for key, value in dict(targets).items()
                if isinstance(value, (list, tuple)) and value
            }
        ),
        "max_quote_words": MappingProxyType(
            {str(key): int(value) for key, value in dict(quotes).items()}
        ),
        "attribution_model": _str_or(
            getattr(row, "attribution_model", None), d.attribution_model
        ),
        "cover_mode": _str_or(getattr(row, "cover_mode", None), d.cover_mode),
        # A weight the operator zeroed is a weight the operator zeroed, so the
        # per-key fallback is only for keys the JSON does not mention at all.
        "rank_weights": MappingProxyType(
            {
                str(key): _float_or(dict(weights).get(key), default)
                for key, default in d.rank_weights.items()
            }
        ),
        "guard_model": _str_or(getattr(row, "guard_model", None), d.guard_model),
    }


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
                source_role=getattr(r, "source_role", None) or "news",
                source_class=getattr(r, "source_class", None) or "news",
                license_class=(
                    getattr(r, "license_class", None) or "news_paywalled"
                ),
                doc_language=getattr(r, "doc_language", None),
                fetch_method=getattr(r, "fetch_method", None),
                cache_ttl_days=getattr(r, "cache_ttl_days", None),
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
                    **_v3_keys(row),
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
        run_type: str | None = None,
    ) -> int | None:
        """Create a new ``runs`` row and return its id. Returns ``None``
        if admin.db / brand row are unavailable.

        ``run_type`` (NTS_106 §5) is which v3 contour this run belongs to —
        ``intake`` / ``production`` / ``publish`` / ``ttl``. It stays optional
        because the ~72 pre-v3 rows carry NULL and callers that predate the
        column must keep working; the intake and production entry points pass
        it explicitly.
        """
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
                run_type=run_type,
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
        candidate_id_fk: int | None = None,
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
                candidate_id_fk=candidate_id_fk,
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
