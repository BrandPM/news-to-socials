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
    # NTS_074 — the pipeline run executes as a DETACHED subprocess
    # (``python -m pipeline.run for-run --run-id N``) off the admin-API event
    # loop. When True the spawn is wrapped in ``systemd-run --user --scope`` so
    # the run gets its OWN cgroup + MemoryMax instead of sharing the admin-API
    # unit's 512M (requires ``loginctl enable-linger`` for the service user).
    # Off by default — plain subprocess works everywhere incl. local/CI.
    admin_run_via_systemd_run: bool = False
    admin_run_memory_max: str = "1G"
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

    # Public-facing site (NTS_075). Base URL of the live marketing site the
    # "View live" link points at. Canonical live URL is
    # ``{base}/{language}/insights/{slug}``. Override per-env via
    # PUBLIC_SITE_BASE_URL.
    public_site_base_url: str = "https://www.iconfinance.io"

    # Backup heartbeat (NTS_088). The daily admin.db backup (nts-backup.sh)
    # writes an ISO-8601 UTC timestamp to this file on success. The
    # nts-monitor alerter (pipeline.monitoring.alerts) fires a Telegram alert
    # if it is missing or older than backup_max_age_hours. Cannot detect a
    # full-VPS outage — external monitoring is a separate future item.
    backup_heartbeat_path: Path = Path("/opt/news-to-socials/backups/.last_ok")
    backup_max_age_hours: int = 26

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

    @property
    def public_site_host(self) -> str:
        """Bare public host (no scheme / path) derived from
        ``public_site_base_url`` — for the rare caller that needs just the
        domain. Single source of truth: never hardcode the domain elsewhere."""
        from urllib.parse import urlparse  # noqa: PLC0415

        return urlparse(self.public_site_base_url).netloc or self.public_site_base_url

    @property
    def outbound_user_agent(self) -> str:
        """User-Agent string for outbound HTTP (RSS / web fetches). Carries the
        public site URL so source operators can identify us, built from the
        single domain source of truth rather than a hardcoded literal."""
        return f"{APP_NAME}/{APP_VERSION} (+{self.public_site_base_url.rstrip('/')})"


# App identity for outbound User-Agent etc. (kept here so the domain lives in
# exactly one place — see Settings.public_site_base_url).
APP_NAME = "news-to-socials"
APP_VERSION = "0.0.1"

_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy-load and cache settings. Tests can monkeypatch this."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
