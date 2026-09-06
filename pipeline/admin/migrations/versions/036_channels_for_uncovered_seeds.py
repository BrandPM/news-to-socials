"""Channels for the seed topics that had none (NTS_129 P2 Ф1, second pass).

Revision ID: 036_channels_for_uncovered_seeds
Revises: 035_resync_writer_draft_moves
Create Date: 2026-09-06

The first Ф3 measurement after migration 032 moved recall from 8% to 15% and,
more usefully, made all 20 seed topics *measurable* — the European Commission
feed closed the ``legislation`` channel gap that had excluded eight EU topics
from the denominator entirely.

What it also did was name the remaining holes precisely. Reading the seed list
against the source list, these topics had no channel at all:

    Malta residence programmes    MT   — MFSA regulates finance, not residency
    UAE corporate tax / free zone AE   — no UAE source of any kind
    Cyprus IP box / tax rulings   CY   — no Cypriot source of any kind
    Golden visa closures          EU   — decided in Parliament, not by the EC

So four channels, each fetched live before it was written here:

* **Identità (Malta)** — the agency that actually administers the residence
  and citizenship programmes. RSS, 10 items.
* **European Parliament press** — RSS, 20 items. Where a directive is debated
  and amended, which is months before the Commission announces it.
* **UAE Federal Tax Authority** — no feed exists; ``html_list``, 22 links.
* **CySEC announcements** — no feed exists (their RSS paths answer 410 Gone);
  ``html_list``, 3 links.

``html_list`` was generalised for the last two. It was written against FATF's
URL shape and matched three hardcoded path segments; it now matches the *shape*
of a regulator listing page — same-host links whose path names a document —
which is what makes it usable as the channel of last resort for a regulator
that publishes law and no feed. The trade is precision: it returns some
navigation alongside the publications, and that is the right way round, because
the guard rejects a non-story for a fraction of a cent while a missed directive
is invisible.

**Still closed, and they need a human.** Poland (gov.pl and KNF render their
listings in JavaScript, so there is nothing in the HTML to read), Ukraine
(tax.gov.ua and mof.gov.ua answer 403 to any non-browser agent), and the OECD
(403 likewise, on every path tried) — which is the only channel for the three
OECD seed topics, Pillar Two, CRS and CARF. These are recorded in the session
log as work for Andriy rather than worked around: a 403 is the site declining,
and NTS_108 §1 does not allow scraping past one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "036_channels_for_uncovered_seeds"
down_revision: str | None = "035_resync_writer_draft_moves"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (name, url, primary_category, source_role, source_class, license_class,
#  doc_language, fetch_method, polling_minutes)
NEW_FEEDS: tuple[tuple[str, str, str, str, str, str, str, str, int], ...] = (
    (
        "Identità (Malta residence & citizenship)",
        "https://www.identita.gov.mt/feed/",
        "special",
        "primary_feed",
        "jurisdiction_list",
        "public_official",
        "en",
        "rss",
        1440,
    ),
    (
        "European Parliament press",
        "https://www.europarl.europa.eu/rss/doc/press-releases/en.xml",
        "structuring",
        "primary_feed",
        "legislation",
        "public_official",
        "en",
        "rss",
        720,
    ),
    (
        "UAE Federal Tax Authority",
        "https://tax.gov.ae/en/media.centre/news.aspx",
        "structuring",
        "primary_site",
        "tax_authority",
        "public_official",
        "en",
        "html_list",
        1440,
    ),
    (
        "CySEC announcements",
        "https://www.cysec.gov.cy/en-GB/public-info/announcements/",
        "wealth",
        "primary_site",
        "regulator",
        "public_official",
        "en",
        "html_list",
        1440,
    ),
)

_TTL_BY_CLASS = {
    "legislation": 30,
    "jurisdiction_list": 7,
    "regulator": 14,
    "tax_authority": 14,
}


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
            sa.text(
                "INSERT INTO sources (brand_id_fk, name, source_type, url, "
                "primary_category, active, paywall, polling_minutes, source_role, "
                "source_class, license_class, doc_language, fetch_method, "
                "cache_ttl_days, created_at, updated_at) "
                "VALUES (:b, :name, 'rss', :url, :cat, 1, 0, :poll, :role, "
                ":sclass, :lclass, :dlang, :fmethod, :ttl, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
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
                "ttl": _TTL_BY_CLASS.get(source_class, 14),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    icon_id = _icon_id(bind)
    if icon_id is None:
        return
    for _name, url, *_rest in NEW_FEEDS:
        # Only rows with no work attached — 022's rule, which 026 had to widen.
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
