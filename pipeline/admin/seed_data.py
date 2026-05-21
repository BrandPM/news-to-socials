"""Canonical seed data for the admin database.

Single source of truth for two consumers:

* ``scripts/seed_admin_db.py`` — populates admin.db on first install.
* ``pipeline/admin/config_client.py`` — used by ``run_pipeline`` as the
  fallback when admin.db is missing or empty, so the existing systemd
  timer keeps producing drafts during the S2/S3 rollout window.

If you need to *change* a seed value at runtime, update admin.db through
the API — don't edit this file. This file is only authoritative when
admin.db doesn't yet exist or has been wiped.
"""

from __future__ import annotations

from dataclasses import dataclass


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
# IT_PROJ_NTS_021, so they are seeded as **inactive** until Andriy
# verifies the feed URLs in the admin UI (S2). The S1 contract is "3
# seeded rows in /sources", not "3 active sources".
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
