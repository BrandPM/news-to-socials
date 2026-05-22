"""Canonical seed data for the admin database.

Single source of truth for two consumers:

* ``scripts/seed_admin_db.py`` — populates admin.db on first install.
* tests that pre-populate a brand row before exercising Source/Prompt/etc.

The seed surface covers three layers:

1. **Brands** — Icon (active, real Sanity creds from .env) + 4
   placeholder drafts. Idempotent by slug. Once a brand exists in the
   DB, the seed NEVER overwrites its credentials (operator may have
   edited them via the UI; we don't want to silently undo that).
2. **Sources** for Icon — three RSS feeds (Private Banker Int'l +
   Bloomberg + CNBC).
3. **Prompts** for Icon — active polish + draft from the live
   ``comment_writer`` module.

If you need to *change* a seed value at runtime, update admin.db through
the API — don't edit this file.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Brand seeds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaceholderBrandSeed:
    slug: str
    name: str
    language: str = "en"
    timezone: str = "Europe/Madrid"


# Five fintech brands per founding requirements (NTS_017 + NTS_025). Icon
# is the only active brand at S3 deploy time; the other four are seeded
# as status='draft' so they appear in the brand switcher with a "Setup
# required" marker but cannot run the pipeline.
PLACEHOLDER_BRAND_SEEDS: tuple[PlaceholderBrandSeed, ...] = (
    PlaceholderBrandSeed(slug="neovox", name="Neovox"),
    PlaceholderBrandSeed(slug="creolix", name="Creolix"),
    PlaceholderBrandSeed(slug="vilatrix", name="Vilatrix"),
    PlaceholderBrandSeed(slug="nexora", name="Nexora"),
)


ICON_BRAND_SLUG = "icon"
ICON_BRAND_NAME = "Icon Finance"
ICON_BRAND_LANGUAGE = "en"
ICON_BRAND_TIMEZONE = "Europe/Madrid"


# ---------------------------------------------------------------------------
# Source seeds (Icon)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedSource:
    name: str
    source_type: str
    url: str
    primary_category: str
    active: bool
    polling_minutes: int = 720


# Three Icon-brand sources tracked in production. Only privatebanker is
# actively scored in the current pipeline (matches the systemd ExecStart);
# Bloomberg Wealth and CNBC Wealth ran zero passes on 2026-05-21 per
# IT_PROJ_NTS_021, so they are seeded as inactive until Andriy verifies
# the feed URLs in the admin UI.
ICON_SEED_SOURCES: tuple[SeedSource, ...] = (
    SeedSource(
        name="Private Banker International",
        source_type="rss",
        url="https://www.privatebankerinternational.com/feed/",
        primary_category="wealth",
        active=True,
        polling_minutes=720,
    ),
    SeedSource(
        name="Bloomberg Wealth",
        source_type="rss",
        url="https://feeds.bloomberg.com/wealth/news.rss",
        primary_category="wealth",
        active=False,
        polling_minutes=720,
    ),
    SeedSource(
        name="CNBC Wealth",
        source_type="rss",
        url="https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000770",
        primary_category="wealth",
        active=False,
        polling_minutes=720,
    ),
)


# ---------------------------------------------------------------------------
# Prompt seeds (Icon)
# ---------------------------------------------------------------------------


# The active polish prompt is imported lazily at seed time to avoid a
# hard import dependency in modules that don't need it (e.g. tests).
def get_active_polish_prompt() -> tuple[str, str]:
    """Return ``(version_name, content)`` for the currently-shipping
    writer_polish prompt — i.e. the IT_PROJ_NTS_013 v1.1 version."""
    from pipeline.generator.comment_writer import _POLISH_PROMPT  # noqa: PLC0415

    return ("v1.1 — H2 + voice guardrails (IT_PROJ_NTS_013)", _POLISH_PROMPT)


def get_active_draft_prompt() -> tuple[str, str]:
    from pipeline.generator.comment_writer import _DRAFT_PROMPT  # noqa: PLC0415

    return ("v1.1 — H2 structure (IT_PROJ_NTS_013)", _DRAFT_PROMPT)


# Pipeline config defaults — match icon_brand_config() at seed time.
ICON_SEED_THRESHOLD = 7
ICON_SEED_TOPICS_PER_RUN = 3
