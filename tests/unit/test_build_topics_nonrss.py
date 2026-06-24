"""NTS_076 audit — non-rss source must not crash the run.

``_build_topics_for_source`` is declared ``-> tuple[list[Topic], int]`` and
the caller unpacks ``topics, fetched_count = await _build_topics_for_source``.
The non-rss early-return used to be a bare ``[]``, which would raise
``ValueError: not enough values to unpack`` for any non-rss source. Lock in
the correct ``([], 0)`` shape.
"""

from __future__ import annotations

import asyncio
import types

from pipeline import run as pipe


def test_build_topics_for_non_rss_source_returns_empty_tuple():
    src = types.SimpleNamespace(source_type="telegram", name="tg-src", id=1)
    topics, fetched = asyncio.run(
        pipe._build_topics_for_source(
            source_record=src,
            brand=None,  # early-returns before brand is touched
            brand_id_fk=1,
            client=None,
            limit=3,
            min_score=7,
        )
    )
    assert topics == []
    assert fetched == 0
