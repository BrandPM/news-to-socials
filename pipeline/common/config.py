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
    log_level: str = "INFO"
    tz: str = "Europe/Madrid"

    # Optional services
    ml_service_url: str = ""
    browser_render_url: str = ""
    browser_render_token: str = ""

    # Feature flags
    dry_run: bool = Field(default=False, description="Skip external publish APIs")


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy-load and cache settings. Tests can monkeypatch this."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
