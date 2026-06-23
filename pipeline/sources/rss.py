"""RSS source.

Strategy:
1. Parse feed with feedparser.
2. For each item, prefer ``content[0].value`` over ``summary`` (many feeds
   put the full text only in ``content``).
3. If still short and ``opts['fetch_article'] is True``, do a follow-up
   GET on the item URL via :meth:`Source._render_get` so paywalled
   sources still produce a usable RawItem.

Pattern notes (no code copied):
* fin-thread's RssProvider uses gofeed and skips items missing title/link —
  we do the same. /research/fin-thread/journalist/provider.go
* RSS-to-Telegram-Bot handles a long list of feed quirks (RSS 1.0/2.0/Atom,
  encoding bugs, missing pubDate). feedparser already covers most of these
  for us.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from time import mktime

import feedparser

from ..common.logging import get_logger
from ..common.models import RawItem, SourceType
from .base import Source, register

log = get_logger(__name__)

_MIN_USEFUL_BODY = 200  # chars; below this we try to fetch the article page


@register
class RssSource(Source):
    type = SourceType.rss

    async def fetch(self, since: datetime | None = None) -> Iterable[RawItem]:
        log.info("rss.fetch.start", source=self.name, url=self.url)
        # NTS_058 incident: feedparser.parse(url) does its OWN synchronous,
        # un-timeoutable network request. Called inside this async coroutine it
        # blocks the whole event loop — a single slow feed (rssexport.rbc.ru)
        # wedged the admin API and tripped Vercel's 504. Fetch the bytes through
        # the shared async client (retry + UA + hard timeout) and only hand the
        # already-downloaded content to feedparser, which then never touches the
        # network. A single broken feed must NOT take down run-all, so any
        # network/timeout error is logged and yields an empty item list.
        try:
            resp = await self._http_get(self.url, timeout=20.0)
        except Exception as exc:  # noqa: BLE001 — one bad feed must not kill run-all
            log.warning(
                "rss.fetch.error", source=self.name, url=self.url, err=str(exc)
            )
            return []

        parsed = feedparser.parse(resp.content)
        if parsed.bozo:
            log.warning("rss.bozo", source=self.name, err=str(parsed.bozo_exception))

        items: list[RawItem] = []
        fetch_article = bool(self.opts.get("fetch_article", False))

        for entry in parsed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue

            published_at = self._parse_pubdate(entry)
            if since and published_at and published_at < since:
                continue

            summary = (entry.get("summary") or "").strip()
            content_list = entry.get("content") or []
            body = content_list[0]["value"].strip() if content_list else summary

            if len(body) < _MIN_USEFUL_BODY and fetch_article:
                try:
                    body = await self._render_get(link) or body
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "rss.article_fetch_failed", source=self.name, link=link, err=str(exc)
                    )

            items.append(
                RawItem(
                    source_id=self.source_id,
                    source_name=self.name,
                    url=link,
                    title=title,
                    summary=summary,
                    raw_html=body,
                    published_at=published_at,
                )
            )

        log.info("rss.fetch.done", source=self.name, count=len(items))
        return items

    @staticmethod
    def _parse_pubdate(entry: feedparser.FeedParserDict) -> datetime | None:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if struct is None:
            return None
        return datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
