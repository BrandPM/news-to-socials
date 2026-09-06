"""The feeds carry Icon's subjects, not the world's business news (NTS_129 P2 Ф1).

Revision ID: 032_source_overhaul
Revises: 031_resync_polish_depth
Create Date: 2026-09-06

The auto-recall on 1 532 accumulated candidates read **8% against a target of
70%**, and the per-source audit says exactly why:

    Bankier.pl        390 items   0 accepted   0 seed hits
    Bloomberg Markets 205 items   1 accepted   0 seed hits
    NV.ua             106 items   0 accepted   0 seed hits
    …
    FINMA News EN       5 items   2 accepted

The general-business wires supplied 85% of the funnel and 10% of the accepts;
the regulator feeds had the opposite ratio and almost no volume. No amount of
rubric editing fixes that — the material was never in the feed.

So, three changes, all evidence-driven and all reversible from the Sources
screen:

**1. Deactivate the wires that earned nothing.** The rule is stated rather than
eyeballed: a ``news`` feed with **≥ 20 items, zero accepts and zero seed-topic
hits** over the 28.08–06.09 window is switched off. Rows are kept (``active=0``,
not deleted): the health history and the candidates that came from them are
evidence, and a feed switched off with a reason can be switched back on.

**2. Add the primary feeds that were missing** — regulators, EU institutions,
jurisdiction registers and the specialist trade press. **Every URL in this file
was fetched live before it was written here** (HTTP 200 *and* a non-empty
parse), because a feed that 200s with zero items is worse than a missing one:
it looks healthy forever (NTS_106 §1, and the Deloitte tax@hand lesson from
S2).

**3. Park what does not answer.** Deloitte tax@hand (200 with an "Access
Denied" body), FATF and OECD (403 to any non-browser agent), STEP, the Big-4
alert feeds and the law-firm feeds: all switched off with the reason in
``name``. NTS_108 §1 forbids scraping ``professional_commentary`` against a
site's ToS, and a 403 is the site saying no — so these are recorded as closed
channels rather than worked around.

EUR-Lex still has no usable public feed: eight URL shapes were tried and the
service answers "The RSS feed doesn't exist or is no longer valid" to every one
without a ``myRssId``, which only a logged-in human can mint. The step-by-step
for creating it is in the session log; the two placeholder rows stay inactive.

Idempotent: inserts skip a URL that already exists, deactivations match on URL.
``downgrade`` reactivates what it switched off and removes only the rows it
inserted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "032_source_overhaul"
down_revision: str | None = "031_resync_polish_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (name, url, primary_category, source_role, source_class, license_class,
#  doc_language, fetch_method, polling_minutes)
#
# Verified live on 2026-09-06: every row returned HTTP 200 and a feed with at
# least one entry. The item counts are in the session log.
NEW_FEEDS: tuple[tuple[str, str, str, str, str, str, str, str, int], ...] = (
    # --- regulators -------------------------------------------------------
    (
        "EBA news",
        "https://www.eba.europa.eu/rss.xml",
        "structuring",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        720,
    ),
    (
        "CSSF news (Luxembourg)",
        "https://www.cssf.lu/en/feed/",
        "wealth",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        720,
    ),
    (
        "MFSA news (Malta)",
        "https://www.mfsa.mt/feed/",
        "structuring",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        720,
    ),
    (
        "SEC press releases",
        "https://www.sec.gov/news/pressreleases.rss",
        "ma",
        "primary_feed",
        "regulator",
        "public_domain",
        "en",
        "rss",
        720,
    ),
    (
        "SEC administrative proceedings",
        "https://www.sec.gov/rss/litigation/admin.xml",
        "special",
        "primary_feed",
        "court",
        "public_domain",
        "en",
        "rss",
        720,
    ),
    (
        "Guernsey GFSC",
        "https://www.gfsc.gg/rss.xml",
        "wealth",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        1440,
    ),
    (
        "Isle of Man FSA",
        "https://www.iomfsa.im/rss/",
        "wealth",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        1440,
    ),
    # --- EU institutions --------------------------------------------------
    (
        "European Commission press",
        "https://ec.europa.eu/commission/presscorner/api/rss?language=en",
        "structuring",
        "primary_feed",
        "legislation",
        "public_official",
        "en",
        "rss",
        720,
    ),
    (
        "ECB press",
        "https://www.ecb.europa.eu/rss/press.html",
        "wealth",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        720,
    ),
    (
        "ECB banking supervision",
        "https://www.bankingsupervision.europa.eu/rss/press.html",
        "wealth",
        "primary_feed",
        "regulator",
        "public_official",
        "en",
        "rss",
        720,
    ),
    # --- UK -------------------------------------------------------------
    (
        "HM Treasury",
        "https://www.gov.uk/search/all.atom?organisations%5B%5D=hm-treasury",
        "structuring",
        "primary_feed",
        "tax_authority",
        "public_official",
        "en",
        "atom",
        720,
    ),
    (
        # UBO / ACSP register changes — directly Icon's subject matter
        # (NTS_115 seed topic "UBO register access").
        "Companies House",
        "https://www.gov.uk/search/all.atom?organisations%5B%5D=companies-house",
        "structuring",
        "primary_feed",
        "jurisdiction_list",
        "public_official",
        "en",
        "atom",
        1440,
    ),
    # --- specialist press -------------------------------------------------
    (
        "IFC Review",
        "https://www.ifcreview.com/rss/",
        "wealth",
        "news",
        "professional_alert",
        "professional_commentary",
        "en",
        "rss",
        720,
    ),
    (
        "OffshoreAlert",
        "https://www.offshorealert.com/feed/",
        "special",
        "news",
        "professional_alert",
        "professional_commentary",
        "en",
        "rss",
        720,
    ),
    (
        "Tax Justice Network",
        "https://taxjustice.net/feed/",
        "structuring",
        "news",
        "professional_alert",
        "professional_commentary",
        "en",
        "rss",
        1440,
    ),
)


# URLs of the general-business wires that earned nothing over 28.08–06.09.
# The rule: a ``news`` feed with ≥ 20 items, 0 accepts and 0 seed-topic hits.
# Bloomberg Markets is here on 205 items and one accept — a 0.5% rate that
# bought 205 guard calls.
NOISE_FEEDS: tuple[tuple[str, str], ...] = (
    ("https://www.bankier.pl/rss/wiadomosci.xml", "390 items, 0 accepted"),
    ("https://feeds.bloomberg.com/markets/news.rss", "205 items, 1 accepted"),
    ("https://nv.ua/rss/all.xml", "106 items, 0 accepted"),
    ("https://frankmedia.ru/feed", "81 items, 0 accepted"),
    ("https://www.money.pl/rss/", "59 items, 0 accepted"),
    ("https://www.forbes.pl/rss.xml", "56 items, 0 accepted"),
    ("https://www.wealthmanagement.com/rss.xml", "52 items, 0 accepted"),
    ("https://www.accountingtoday.com/feed?rss=true", "50 items, 0 accepted"),
    ("https://www.hedgeweek.com/feed", "40 items, 0 accepted"),
    ("https://www.financial-planning.com/feed?rss=true", "33 items, 0 accepted"),
    ("https://taxfoundation.org/feed/", "31 items, 0 accepted"),
    ("https://www.epravda.com.ua/rss/news/", "20 items, 0 accepted"),
)

# Feeds that answer but never with a feed. Recorded as closed channels rather
# than worked around: a 403 from a law firm is the site's ToS answer
# (NTS_108 §1), and a 200 carrying an "Access Denied" page is the failure mode
# that looks healthy forever (NTS_106 §1).
UNREACHABLE_FEEDS: tuple[tuple[str, str], ...] = (
    ("https://www.taxathand.com/rss", "200 with an Access Denied body"),
    ("https://www.fatf-gafi.org/en/publications.html", "403 to any non-browser agent"),
)


def _icon_id(bind: sa.engine.Connection) -> int | None:
    return bind.execute(sa.text("SELECT id FROM brands WHERE slug = 'icon'")).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    icon_id = _icon_id(bind)
    if icon_id is None:
        return

    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT url FROM sources WHERE brand_id_fk = :b"), {"b": icon_id}
        )
    }
    insert = sa.text(
        "INSERT INTO sources (brand_id_fk, name, source_type, url, "
        "primary_category, active, paywall, polling_minutes, source_role, "
        "source_class, license_class, doc_language, fetch_method, "
        "cache_ttl_days, created_at, updated_at) "
        "VALUES (:b, :name, 'rss', :url, :cat, 1, 0, :poll, :role, "
        ":sclass, :lclass, :dlang, :fmethod, :ttl, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    # NTS_101 §1 — the cache TTL by class, same numbers migration 027 back-filled.
    ttl_by_class = {
        "legislation": 30,
        "jurisdiction_list": 7,
        "regulator": 14,
        "tax_authority": 14,
        "filings": 365,
        "professional_alert": 90,
        "court": 365,
        "corporate_pr": 90,
        "news": 7,
    }
    for (
        name,
        url,
        category,
        role,
        source_class,
        license_class,
        doc_language,
        fetch_method,
        polling,
    ) in NEW_FEEDS:
        if url in existing:
            continue
        bind.execute(
            insert,
            {
                "b": icon_id,
                "name": name,
                "url": url,
                "cat": category,
                "poll": polling,
                "role": role,
                "sclass": source_class,
                "lclass": license_class,
                "dlang": doc_language,
                "fmethod": fetch_method,
                "ttl": ttl_by_class.get(source_class, 14),
            },
        )

    # Switch off, never delete: the health history and the candidates these
    # produced are the evidence for switching them off in the first place.
    for url, _reason in NOISE_FEEDS + UNREACHABLE_FEEDS:
        bind.execute(
            sa.text(
                "UPDATE sources SET active = 0, updated_at = CURRENT_TIMESTAMP "
                "WHERE brand_id_fk = :b AND url = :u AND active = 1"
            ),
            {"b": icon_id, "u": url},
        )

    # Feeds that have never produced an item and are not on the evidence list
    # are left alone: absence of items over nine days is not the same evidence
    # as 390 items of noise, and a quiet regulator is exactly what we want.


def downgrade() -> None:
    bind = op.get_bind()
    icon_id = _icon_id(bind)
    if icon_id is None:
        return
    for url, _reason in NOISE_FEEDS + UNREACHABLE_FEEDS:
        bind.execute(
            sa.text(
                "UPDATE sources SET active = 1 WHERE brand_id_fk = :b AND url = :u"
            ),
            {"b": icon_id, "u": url},
        )
    for _name, url, *_rest in NEW_FEEDS:
        # Only rows with no work attached: deleting a source a candidate points
        # at would either break the RESTRICT FK or orphan provenance (the rule
        # migration 022's downgrade set, and 026 had to widen).
        bind.execute(
            sa.text(
                "DELETE FROM sources WHERE brand_id_fk = :b AND url = :u "
                "AND NOT EXISTS (SELECT 1 FROM candidates WHERE source_id_fk = "
                "sources.id) "
                "AND NOT EXISTS (SELECT 1 FROM topics WHERE source_id = sources.id) "
                "AND NOT EXISTS (SELECT 1 FROM source_health_records "
                "WHERE source_id = sources.id)"
            ),
            {"b": icon_id, "u": url},
        )
