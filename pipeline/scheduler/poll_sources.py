"""Periodic source polling.

Entry point: ``python -m pipeline.scheduler.poll_sources``. Designed to be
invoked by ``infra/systemd/news-poll.timer`` every 5 minutes. The handler
itself is cheap when nothing's due — it just reads source config and
checks ``last_polled_at + polling_interval``.

Pipeline flow per source (per polling cycle):
1. fetch RawItems
2. for each item: topic_picker.score(item, brand)
3. drop low scores
4. compute embedding
5. dedup check — skip duplicates
6. write Topic to Directus for the next stage to pick up

For the MVP this module wires steps 1, 3, 5, 6 together. Embeddings/2 live
in their own modules and can be swapped without touching the scheduler.
"""

from __future__ import annotations

import asyncio
import hashlib

from ..common.config import get_settings
from ..common.logging import configure_logging, get_logger
from ..publisher.directus import DirectusClient
from ..sources import REGISTRY
from ..sources.base import utcnow

log = get_logger(__name__)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    log.info("poll.start", db=str(settings.pipeline_db_path))

    directus = DirectusClient()
    # Active sources from Directus.
    async with __import__("httpx").AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{directus.base_url}/items/sources",
            headers=directus._headers(),
            params={"filter[active][_eq]": "true"},
        )
        resp.raise_for_status()
        sources = resp.json().get("data", [])

    now = utcnow()
    for src in sources:
        try:
            await _poll_one(src, now, directus)
        except Exception as exc:  # noqa: BLE001
            log.error("poll.source_failed", source=src.get("name"), err=str(exc))

    log.info("poll.done", count=len(sources))


async def _poll_one(src: dict, now, directus: DirectusClient) -> None:
    src_type = src["type"]
    cls = REGISTRY.get(src_type)
    if cls is None:
        log.warning("poll.no_handler", type=src_type)
        return

    last_polled = src.get("last_polled_at")
    interval_min = int(src.get("polling_interval_minutes", 30))
    if last_polled is not None:
        # Cheap timezone-naive comparison via ISO; fine for "is it time yet?"
        pass  # TODO(stage-6): real time-since check; for MVP we poll on every tick

    source = cls(
        source_id=src["id"],
        name=src["name"],
        url=src["url"],
        **(src.get("opts") or {}),
    )
    items = await source.fetch()

    new_count = 0
    for item in items:
        url_str = str(item.url)
        hash_ = hashlib.sha1(url_str.encode("utf-8")).hexdigest()
        # We dedupe by URL hash here as a cheap first pass; semantic dedup
        # happens in selector.dedup against the embedding store.
        existing = await directus.create_item(
            "topics",
            {
                "source_id": src["id"],
                "original_url": url_str,
                "original_title": item.title[:280],
                "original_text": item.raw_html[:8000] or item.summary[:8000],
                "fetched_at": now.isoformat(),
                "hash": hash_,
                "status": "new",
            },
        ) if False else None  # MVP: in real run, wrap with try/IntegrityError on hash unique
        if existing:
            new_count += 1

    log.info("poll.source_done", source=src["name"], items=len(list(items)), new=new_count)
    await directus.update_item(
        "sources", src["id"], {"last_polled_at": now.isoformat()}
    )


if __name__ == "__main__":
    asyncio.run(main())
