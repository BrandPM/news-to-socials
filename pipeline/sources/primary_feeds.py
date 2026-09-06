"""The two primary feeds that are not feeds: FATF listings and EDGAR FTS (S5).

Migration 022 inserted both rows **inactive** and the intake raised
``NotImplementedError`` on their ``fetch_method`` — deliberately, so a missing
fetcher was recorded in ``source_health_records`` as a failure rather than
looking like an empty feed. NTS_101 §1 lists both as primary sources; this is
where they arrive, and migration 027 activates the rows.

Neither is a ``Source`` subclass because neither is polled by ``run_pipeline``
(v2, and off): the intake calls them directly through
:func:`fetch_by_method`. Both return ``RawItem`` like every other fetcher, so
everything downstream — dedup, prefilter, guard, candidate — is unchanged.

**FATF** publishes an HTML listing with no feed at all. The parser is
deliberately shallow: anchors under the publications listing whose href looks
like a publication, deduplicated, newest-first as the page orders them. A
listing page that changes shape yields zero items and a health failure, which
is the correct visible outcome — guessing harder would produce navigation links
as candidates.

**SEC EDGAR full-text search** has a JSON API (``efts.sec.gov``), so it is
parsed as JSON rather than scraped. Its rate limit is real and its terms
require a declared User-Agent, which the shared client already sends.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import RawItem

log = get_logger(__name__)

# EDGAR asks for a descriptive UA and rejects unfamiliar ones; the shared
# ``outbound_user_agent`` carries the site URL, which is what they want.
_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index?q=&forms={forms}"
_EDGAR_JSON = "https://efts.sec.gov/LATEST/search-index?"

# What a publication link looks like on a regulator's listing page. Written as
# a shared vocabulary rather than per-site selectors (NTS_129 P2 Ф1): a CSS
# selector per source is a maintenance contract with every site's redesign,
# and there is no column to keep one in. These path segments are what the sites
# that publish law actually use, and they are checked against the listing
# page's own host, so a hit is a document on the regulator's own site.
_DOCUMENT_HINTS: tuple[str, ...] = (
    "/publication",
    "/document",
    "/recommendation",
    "/announcement",
    "/circular",
    "/press",
    "/news",
    "/guidance",
    "/consultation",
    "/regulation",
    "/legislation",
    "/decision",
    "/notice",
    "/statement",
    ".pdf",
)


async def _get(url: str, *, timeout: float = 30.0) -> httpx.Response:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": get_settings().outbound_user_agent},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp


async def fetch_html_list(
    *, url: str, source_name: str, source_id: str, limit: int = 50
) -> list[RawItem]:
    """Publication links off an HTML listing page.

    The channel of last resort, for a regulator that publishes law and no feed:
    the NTS_129 P2 audit found Cyprus, Poland, Ukraine, the UAE and the OECD all
    in that position, and every one of them is a jurisdiction on Icon's tier
    list. Written against the *shape* of a regulator listing page — same-host
    links whose path names a document — rather than a CSS selector per site,
    because a selector is a standing maintenance contract with somebody else's
    redesign and there is nowhere to store one per source.

    The trade is precision: this returns some navigation alongside the
    publications. That is the right way round, because the guard rejects a
    non-story for a fraction of a cent while a missed directive is invisible.

    Raises on a transport error so the intake records a health failure; returns
    an empty list when the page loaded but held nothing recognisable, which the
    intake already treats as an unsuccessful fetch (NTS_106 §1, the hotfix of
    2026-08-28).
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse

    resp = await _get(url)
    soup = BeautifulSoup(resp.content, "lxml")
    listing_host = urlparse(url).netloc.lower()
    items: list[RawItem] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, str(anchor["href"]).strip())
        parsed = urlparse(href)
        # Same host, because an off-site link on a regulator's listing page is
        # a reference, not that regulator's publication — and a candidate whose
        # primary_doc_url points somewhere else fails the document match later
        # anyway, after paying for it.
        if parsed.netloc.lower() != listing_host:
            continue
        lowered = href.lower()
        if href in seen or not any(hint in lowered for hint in _DOCUMENT_HINTS):
            continue
        title = anchor.get_text(" ", strip=True)
        # A bare "Read more" or an icon link is navigation, not a publication.
        if len(title) < 15:
            continue
        seen.add(href)
        items.append(
            RawItem(
                source_id=source_id,
                source_name=source_name,
                # pydantic coerces to HttpUrl on construction.
                url=href,  # type: ignore[arg-type]
                title=title[:300],
                # No summary on a listing page. The prefilter does not apply
                # its summary rules to primary feeds (hotfix 35b1188), so an
                # empty one is not a reason to drop a FATF publication.
                summary="",
                published_at=_nearby_date(anchor),
            )
        )
        if len(items) >= limit:
            break
    log.info("html_list.fetched", source=source_name, items=len(items))
    return items


def _nearby_date(anchor: Any) -> datetime | None:
    """A publication date from a ``<time>`` next to the link, if there is one.

    Listings put the date in a sibling element more often than in the href.
    ``None`` is fine: the prefilter's age cap for primary feeds is 240 hours
    and it treats a missing date as "unknown", not as "old".
    """
    for element in (anchor.parent, anchor.find_next("time"), anchor.find_previous("time")):
        if element is None:
            continue
        stamp = getattr(element, "get", lambda *_: None)("datetime")
        if not stamp and getattr(element, "name", "") == "time":
            stamp = element.get_text(strip=True)
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


async def fetch_edgar_fts(
    *, url: str, source_name: str, source_id: str, limit: int = 50
) -> list[RawItem]:
    """Filings from EDGAR full-text search, as JSON.

    The registry row stores the search URL with its ``forms`` filter; the query
    string is reused verbatim so the filter lives in the Sources screen rather
    than in this file.
    """
    query = parse_qs(urlparse(url).query)
    forms = (query.get("forms") or ["8-K"])[0]
    target = _EDGAR_JSON + urlencode(
        {"q": (query.get("q") or ['"merger"'])[0], "forms": forms, "dateRange": "custom"}
    )
    resp = await _get(target)
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"EDGAR returned non-JSON: {exc}") from exc

    hits = (((payload or {}).get("hits") or {}).get("hits")) or []
    items: list[RawItem] = []
    for hit in hits[:limit]:
        source = hit.get("_source") or {}
        adsh = (source.get("adsh") or "").replace("-", "")
        cik_list = source.get("ciks") or [""]
        cik = str(cik_list[0]).lstrip("0") or "0"
        doc_id = str(hit.get("_id") or "")
        filename = doc_id.split(":")[-1] if ":" in doc_id else ""
        if not adsh or not filename:
            continue
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{filename}"
        )
        items.append(
            RawItem(
                source_id=source_id,
                source_name=source_name,
                # pydantic coerces to HttpUrl on construction.
                url=filing_url,  # type: ignore[arg-type]
                title=(
                    f"{(source.get('display_names') or ['(unnamed filer)'])[0]} — "
                    f"{source.get('file_type') or forms}"
                )[:300],
                summary="",
                published_at=_edgar_date(source.get("file_date")),
            )
        )
    log.info("edgar_fts.fetched", source=source_name, items=len(items))
    return items


def _edgar_date(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).replace(tzinfo=UTC)
    except ValueError:
        return None


async def fetch_by_method(
    *, fetch_method: str, url: str, source_name: str, source_id: str, limit: int
) -> Iterable[RawItem]:
    """Dispatch on ``sources.fetch_method``. Raises for an unknown method.

    Raising rather than returning empty is the same choice S2 made for the two
    methods that had no fetcher: a missing implementation must land in
    ``source_health_records`` as a failure, not resemble a quiet feed.
    """
    if fetch_method == "html_list":
        return await fetch_html_list(
            url=url, source_name=source_name, source_id=source_id, limit=limit
        )
    if fetch_method == "edgar_fts":
        return await fetch_edgar_fts(
            url=url, source_name=source_name, source_id=source_id, limit=limit
        )
    raise NotImplementedError(f"no fetcher for fetch_method {fetch_method!r}")
