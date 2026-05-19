"""Unit tests for sources/rss.py against a static feed fixture.

No network. We pass feedparser a local string.
"""

from __future__ import annotations

import asyncio

import feedparser

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


def test_rss_parses_valid_items(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Items with empty link must be skipped; the rest become RawItems."""
    real_parse = feedparser.parse  # capture BEFORE patching

    def fake_parse(_url: str):  # type: ignore[no-untyped-def]
        return real_parse(_FEED_XML)

    monkeypatch.setattr("pipeline.sources.rss.feedparser.parse", fake_parse)

    src = RssSource(source_id="s1", name="example", url="https://example.com/feed")
    items = list(asyncio.run(_collect(src)))

    assert [str(i.url) for i in items] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert items[0].title == "Visa launches instant SEPA"
    assert items[0].published_at is not None


async def _collect(src: RssSource) -> list:
    return list(await src.fetch())
