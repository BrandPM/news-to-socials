"""Application settings loaded from environment.

Uses pydantic-settings so the same model can be populated from .env or from
environment variables. Validation happens at startup, not at first use.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM providers (ADR-017: OpenAI-only)
    openai_api_key: str = ""

    # Image generation
    replicate_api_token: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_approver_chat_id: str = ""
    telegram_monitoring_chat_id: str = ""

    # Meta
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""

    # Sanity CMS (ADR-018)
    sanity_project_id: str = ""
    sanity_dataset: str = "production"
    sanity_api_version: str = "2024-01-01"
    sanity_api_token: str = ""

    # Directus (DEPRECATED — kept for backwards-compat, see ADR-018)
    directus_url: str = ""
    directus_token: str = ""

    # Pipeline runtime
    pipeline_db_path: Path = Path("./pipeline.db")
    admin_db_path: Path = Path("./admin.db")
    log_level: str = "INFO"
    tz: str = "Europe/Madrid"

    # Admin API (IT_PROJ_NTS_014). Token validated by middleware on every
    # request except /health. Empty string means admin API is disabled.
    admin_trigger_secret: str = ""
    admin_cors_origin: str = "http://localhost:3000"
    # Hourly background job that force-fails runs stuck in 'running' >6h
    # (NTS_056 Task 3; threshold lowered from 24h in NTS_058). On in
    # production; tests disable it so no scheduler thread leaks across the suite.
    admin_run_scheduler: bool = True
    stale_run_max_age_hours: int = 6
    # Where /runs/{id}/log + /runs/{id}/events read structured pipeline
    # events from. After NTS_025 the pipeline runs inside the admin-api
    # systemd unit, which redirects stdout to admin-api.log; the legacy
    # run.log only catches cron-triggered runs predating that move.
    admin_log_path: Path = Path("/var/log/news-to-socials/admin-api.log")

    # Master Fernet key for per-brand credential encryption in admin.db.
    # Same value must be set on Mac + VPS .env. repr=False keeps the key
    # out of any accidental `print(settings)` or structlog `event=settings`.
    # NTS_025 § "Credential encryption".
    brands_encryption_key: str = Field(default="", repr=False)

    # Optional services
    ml_service_url: str = ""
    browser_render_url: str = ""
    browser_render_token: str = ""

    # Feature flags
    dry_run: bool = Field(default=False, description="Skip external publish APIs")
    # IT_PROJ_NTS_052: rejecting a draft now patches the Sanity doc with
    # ``status: "rejected"`` (kept for audit + restore). Flipping this to
    # ``true`` restores the legacy NTS_051 behaviour of hard-deleting the
    # ``drafts.*`` document — kept as a back-compat switch but off by
    # default so the Content hub's Rejected tab has documents to show.
    delete_rejected_from_sanity: bool = Field(default=False)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy-load and cache settings. Tests can monkeypatch this."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
