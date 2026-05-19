"""Shared pytest fixtures.

Tests should never read real .env from disk. We patch ``get_settings`` to
return a fresh Settings instance with empty credentials, so any code that
*does* try to call an external API will fail with a clear "missing token"
error rather than charging Andriy's account.
"""

from __future__ import annotations

import pytest

from pipeline.common import config as config_module


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a clean Settings instance for every test."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("META_ACCESS_TOKEN", "")
    monkeypatch.setenv("DIRECTUS_TOKEN", "")
