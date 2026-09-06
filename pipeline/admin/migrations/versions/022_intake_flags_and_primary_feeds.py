"""v3 mode flags + the primary-feed registry rows (NTS_103 шаг 1, NTS_115 §1).

Revision ID: 022_intake_flags_and_primary_feeds
Revises: 021_editorial_guard_prompt_type
Create Date: 2026-08-28

Session S2 of IT_PROJ_NTS_114. Two concerns, both data:

**1. Three ``pipeline_config`` keys, the first two of which are the cutover
switches from NTS_103.** Both ship OFF:

* ``intake_enabled`` — the new contour-1 run. A new mode ships off; the run
  that starts filling ``candidates`` begins when the operator says so.
* ``v2_generation_enabled`` — the OLD daily generation, off by Andriy's
  gate-journal directive of 2026-08-28 (NTS_105 §9, NTS_114 §S2). It was
  paying for four translations and a cover per article that the new rubric
  classifies as a reject.
* ``guard_model`` — NTS_099 §2 wants "a cheap model or equivalent"; a config
  key means swapping it is a Settings edit rather than a deploy.

The consequence is deliberate and worth stating: **on the deploy that applies
this migration, the daily cron does nothing** until a flag is flipped. An idle
pipeline is visible in the runs list. A pipeline generating articles nobody
will publish is not.

**2. The 12 primary feeds from IT_PROJ_NTS_115 artefact 1.** Migration 020
added the registry columns; the rows are here, because a feed list is data the
operator then owns from the Sources screen, not a constant in the fetcher.

``active`` is set per feed by whether the code can actually read it today:

* the 9 rss/atom feeds are active — every URL was fetched and parsed during
  this session before being written down here;
* ``html_list`` (FATF) and ``edgar_fts`` (SEC EDGAR full-text search) are
  inserted **inactive**: no fetcher exists for either until S5 (NTS_101 §2-7),
  and an active source with no fetcher is a daily health-record failure that
  teaches the operator to ignore health records;
* the two EUR-Lex rows are inserted **inactive with a placeholder URL**. A
  EUR-Lex Atom feed is an account-bound saved search (``myRssId``), which
  cannot be created from here — the URL has to be pasted in. Flagged in the
  session log rather than guessed at, because a guessed feed URL that returns
  HTTP 200 with an error document (Deloitte's does exactly that) looks healthy
  and delivers nothing.

Insert-when-absent keyed on ``(brand_id_fk, url)``, so an operator who has
already edited a feed's class or paused it keeps that edit across the next
deploy. ``downgrade`` drops the three columns and deletes only the feed rows
this migration inserted that still have no candidates or topics attached — a
feed that has produced work is left alone rather than taken out from under its
foreign keys.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022_intake_flags_and_primary_feeds"
down_revision: str | None = "021_editorial_guard_prompt_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (name, type, server_default)
_CONFIG_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    ("intake_enabled", sa.Boolean(), "0"),
    ("v2_generation_enabled", sa.Boolean(), "0"),
    ("guard_model", sa.Text(), "gpt-4o-mini"),
)

# NTS_115 artefact 1. Columns:
#   name, source_type, url, primary_category, source_class, license_class,
#   doc_language, fetch_method, active, polling_minutes
#
# ``source_type`` is the pre-v3 fetcher discriminator whose CHECK admits only
# rss/web/telegram, so atom rides on 'rss' (feedparser reads both) and the two
# non-feed methods ride on 'web'. ``fetch_method`` is the v3 registry field and
# carries the real answer.
_PRIMARY_FEEDS: tuple[tuple[str, str, str, str, str, str, str, str, int, int], ...] = (
    (
        "FINMA News EN",
        "rss",
        "https://www.finma.ch/en/rss/news/",
        "structuring",
        "regulator",
        "public_official",
        "en",
        "rss",
        1,
        720,
    ),
    (
        "FINMA News DE",
        "rss",
        "https://www.finma.ch/de/rss/news/",
        "structuring",
        "regulator",
        "public_official",
        "de",
        "rss",
        1,
        720,
    ),
    (
        "FINMA Sanctions",
        "rss",
        "https://www.finma.ch/en/rss/sanktionen/",
        "special",
        "regulator",
        "public_official",
        "en",
        "rss",
        1,
        720,
    ),
    (
        "FCA News",
        "rss",
        "https://www.fca.org.uk/news/rss.xml?category=all",
        "wealth",
        "regulator",
        "public_official",
        "en",
        "rss",
        1,
        720,
    ),
    # NTS_115 names the BaFin feed «Meldungen». The RSS hub
    # (bafin.de/DE/service/rss/rss_node.html) publishes five feeds and none is
    # called that; "Alle Veröffentlichungen" (rssnewsfeed.xml) is the widest,
    # which is the right side to err on while recall is what the shadow week
    # measures. Narrow it to RSS_Aufsicht from the Sources screen if the
    # funnel comes back noisy.
    (
        "BaFin Alle Veröffentlichungen",
        "rss",
        "https://www.bafin.de/DE/service/rss/_function/rssnewsfeed.xml?nn=150166",
        "wealth",
        "regulator",
        "public_official",
        "de",
        "rss",
        1,
        720,
    ),
    (
        "ESMA News",
        "rss",
        "https://www.esma.europa.eu/rss.xml",
        "structuring",
        "regulator",
        "public_official",
        "en",
        "rss",
        1,
        720,
    ),
    (
        "HMRC policy papers",
        "rss",
        "https://www.gov.uk/search/policy-papers-and-consultations.atom"
        "?organisations%5B%5D=hm-revenue-customs",
        "structuring",
        "tax_authority",
        "public_official",
        "en",
        "atom",
        1,
        720,
    ),
    (
        "GlobeNewswire M&A",
        "rss",
        "https://www.globenewswire.com/RssFeed/subjectcode/"
        "09-Mergers%20And%20Acquisitions/feedTitle/"
        "GlobeNewswire%20-%20Mergers%20And%20Acquisitions",
        "ma",
        "corporate_pr",
        "corporate_pr",
        "en",
        "rss",
        1,
        720,
    ),
    # Returns HTTP 200 with an "Access Denied" HTML page to a plain client, so
    # it parses to zero entries. Left ACTIVE deliberately: NTS_115's rule is
    # that a feed which does not return a valid list is marked unhealthy and
    # does not block the run, and source_health_records is where that belongs —
    # invisible in a migration comment, visible on the Sources screen.
    (
        "Deloitte tax@hand",
        "rss",
        "https://www.taxathand.com/rss",
        "structuring",
        "professional_alert",
        "professional_commentary",
        "en",
        "rss",
        1,
        720,
    ),
    # --- inactive: no fetcher until S5 (NTS_101 §2-7)
    (
        "FATF Publications",
        "web",
        "https://www.fatf-gafi.org/en/publications.html",
        "structuring",
        "jurisdiction_list",
        "public_official",
        "en",
        "html_list",
        0,
        1440,
    ),
    (
        "SEC EDGAR FTS (8-K, S-4, SC 13D)",
        "web",
        "https://efts.sec.gov/LATEST/search-index?forms=8-K,S-4,SC%2013D",
        "ma",
        "filings",
        "public_domain",
        "en",
        "edgar_fts",
        0,
        1440,
    ),
    # --- inactive: URL is an account-bound saved search, operator must paste it
    (
        "EUR-Lex — taxation (saved search)",
        "rss",
        "https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=REPLACE_ME_TAXATION",
        "structuring",
        "legislation",
        "public_official",
        "en",
        "atom",
        0,
        1440,
    ),
    (
        "EUR-Lex — money laundering (saved search)",
        "rss",
        "https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=REPLACE_ME_AML",
        "structuring",
        "legislation",
        "public_official",
        "en",
        "atom",
        0,
        1440,
    ),
)


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {
        r[1] for r in bind.execute(sa.text(f"PRAGMA table_info('{table}')")).fetchall()
    }


def _seed_primary_feeds() -> None:
    """Insert-when-absent on ``(brand_id_fk, url)`` — an operator edit made
    before the next deploy survives it."""
    bind = op.get_bind()
    icon_id = bind.execute(
        sa.text("SELECT id FROM brands WHERE slug = 'icon'")
    ).scalar()
    if icon_id is None:
        return
    existing = {
        r[0]
        for r in bind.execute(
            sa.text("SELECT url FROM sources WHERE brand_id_fk = :b"),
            {"b": icon_id},
        )
    }
    insert = sa.text(
        "INSERT INTO sources (brand_id_fk, name, source_type, url, "
        "primary_category, active, paywall, polling_minutes, source_role, "
        "source_class, license_class, doc_language, fetch_method, "
        "created_at, updated_at) "
        "VALUES (:b, :name, :stype, :url, :cat, :active, 0, :poll, "
        "'primary_feed', :sclass, :lclass, :dlang, :fmethod, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    for (
        name,
        source_type,
        url,
        category,
        source_class,
        license_class,
        doc_language,
        fetch_method,
        active,
        polling,
    ) in _PRIMARY_FEEDS:
        if url in existing:
            continue
        bind.execute(
            insert,
            {
                "b": icon_id,
                "name": name,
                "stype": source_type,
                "url": url,
                "cat": category,
                "active": active,
                "poll": polling,
                "sclass": source_class,
                "lclass": license_class,
                "dlang": doc_language,
                "fmethod": fetch_method,
            },
        )


def upgrade() -> None:
    present = _columns("pipeline_config")
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        for name, coltype, default in _CONFIG_COLUMNS:
            if name in present:
                continue
            batch_op.add_column(
                sa.Column(name, coltype, nullable=False, server_default=default)
            )

    # Requires the registry columns from 020; if they are somehow absent the
    # rows cannot be described, so skip rather than insert half a feed.
    if {"source_role", "source_class", "license_class", "fetch_method"} <= _columns(
        "sources"
    ):
        _seed_primary_feeds()


def downgrade() -> None:
    bind = op.get_bind()
    icon_id = bind.execute(
        sa.text("SELECT id FROM brands WHERE slug = 'icon'")
    ).scalar()
    if icon_id is not None:
        for row in _PRIMARY_FEEDS:
            url = row[2]
            source_id = bind.execute(
                sa.text(
                    "SELECT id FROM sources WHERE brand_id_fk = :b AND url = :u "
                    "AND source_role = 'primary_feed'"
                ),
                {"b": icon_id, "u": url},
            ).scalar()
            if source_id is None:
                continue
            # A feed that has produced work stays: deleting it would either
            # break a RESTRICT FK or orphan provenance. Downgrade is for
            # undoing a deploy, not for erasing history.
            #
            # ``source_health_records`` joined this list in S4. It was missed
            # first time round because on 2026-08-28 the new feeds had no
            # health history yet; a week of intake later, a primary feed that
            # was fetched but never produced a candidate had 23 health rows and
            # no other trace, so the downgrade deleted the source and left them
            # pointing at nothing. ``PRAGMA foreign_key_check`` on a rehearsed
            # rollback of the prod database is what surfaced it.
            used = bind.execute(
                sa.text(
                    "SELECT (SELECT COUNT(*) FROM topics WHERE source_id = :s) + "
                    "(SELECT COUNT(*) FROM candidates WHERE source_id_fk = :s) + "
                    "(SELECT COUNT(*) FROM source_health_records "
                    "WHERE source_id = :s)"
                ),
                {"s": source_id},
            ).scalar()
            if used:
                continue
            bind.execute(
                sa.text("DELETE FROM sources WHERE id = :s"), {"s": source_id}
            )

    present = _columns("pipeline_config")
    with op.batch_alter_table("pipeline_config", schema=None) as batch_op:
        for name, _coltype, _default in _CONFIG_COLUMNS:
            if name in present:
                batch_op.drop_column(name)
