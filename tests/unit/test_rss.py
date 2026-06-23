"""Unit tests for sources/rss.py against a static feed fixture.

No network. We mock the shared async HTTP client (``_http_get``) so feedparser
only ever sees already-downloaded bytes — mirroring the NTS_058 fix where the
blocking ``feedparser.parse(url)`` was replaced with an awaited fetch.
"""

from __future__ import annotations

import asyncio

import httpx

from pipeline.sources.rss import RssSource


_FEED_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example Finance</title>
  <link>https://example.com/</link>
  <description>desc</description>

  <item>
    <title>Visa launches instant SEPA</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 13 May 2026 09:00:00 GMT</pubDate>
    <description>Short snippet</description>
  </item>

  <item>
    <title>Empty link skipped</title>
    <link></link>
    <pubDate>Mon, 13 May 2026 10:00:00 GMT</pubDate>
    <description>x</description>
  </item>

  <item>
    <title>Mastercard pilots cross-border</title>
    <link>https://example.com/b</link>
    <pubDate>Mon, 13 May 2026 11:00:00 GMT</pubDate>
    <description>Another snippet</description>
  </item>
</channel></rss>
"""


def _make_response() -> httpx.Response:
    return httpx.Response(
        200,
        content=_FEED_XML.encode("utf-8"),
        request=httpx.Request("GET", "https://example.com/feed"),
    )


def test_rss_parses_valid_items(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Items with empty link must be skipped; the rest become RawItems."""

    async def fake_http_get(self, url, timeout=20.0):  # type: ignore[no-untyped-def]
        assert url == "https://example.com/feed"
        return _make_response()

    monkeypatch.setattr(RssSource, "_http_get", fake_http_get)

    src = RssSource(source_id="s1", name="example", url="https://example.com/feed")
    items = list(asyncio.run(_collect(src)))

    assert [str(i.url) for i in items] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert items[0].title == "Visa launches instant SEPA"
    assert items[0].published_at is not None


def test_rss_timeout_returns_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A timed-out / failing feed must yield [] and never raise — one broken
    source must not abort the whole run-all (NTS_058)."""

    async def fake_http_get(self, url, timeout=20.0):  # type: ignore[no-untyped-def]
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(RssSource, "_http_get", fake_http_get)

    src = RssSource(source_id="s1", name="rbc", url="https://rssexport.rbc.ru/feed")
    items = list(asyncio.run(_collect(src)))

    assert items == []


async def _collect(src: RssSource) -> list:
    return list(await src.fetch())
