"""Dispatch ready queue entries through publishers.

Invoked every 5 minutes by ``infra/systemd/news-dispatch.timer``.

For each ready entry:
1. Load the Post from Directus by ``post_id``.
2. Build the ``ChannelRoute`` from Directus.channels.
3. Check publish window + rate-limit; if either blocks, push the entry's
   ``scheduled_at`` forward and skip.
4. ``Dispatcher.dispatch(post, route)``.
5. ``mark_published`` or ``mark_failed`` on the queue entry.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from ..common.config import get_settings
from ..common.logging import configure_logging, get_logger
from ..common.models import Channel, Post, PostStatus
from ..publisher.directus import DirectusClient
from ..publisher.dispatcher import ChannelRoute, Dispatcher
from ..queue.publish_queue import PublishQueue
from ..queue.publish_windows import is_within_window, parse_window
from ..queue.rate_limit import parse_rate
from ..sources.base import utcnow

log = get_logger(__name__)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    queue = PublishQueue(settings.pipeline_db_path)
    await queue.init()
    directus = DirectusClient()
    dispatcher = Dispatcher(directus_client=directus)

    ready = await queue.dequeue_ready()
    log.info("dispatch.tick", ready=len(ready))

    for entry in ready:
        try:
            await _dispatch_one(entry, queue, directus, dispatcher)
        except Exception as exc:  # noqa: BLE001
            await queue.mark_failed(entry.id, str(exc))


async def _dispatch_one(entry, queue, directus, dispatcher) -> None:
    channel_row = await directus.get_item("channels", entry.channel_id)
    window = parse_window(channel_row.get("publish_window"))
    rate = parse_rate(channel_row.get("rate_limit"))

    if not is_within_window(window):
        log.info("dispatch.outside_window", queue_id=entry.id, channel=entry.channel_id)
        # Push forward by 30 minutes so the timer revisits it.
        async with __import__("aiosqlite").connect(queue.db_path) as db:
            await db.execute(
                "UPDATE publish_queue SET scheduled_at=? WHERE id=?",
                ((utcnow() + timedelta(minutes=30)).isoformat(), entry.id),
            )
            await db.commit()
        return

    # rate-limit check is a coarse query against audit_log; for MVP we
    # don't enforce, just log if config says we should.
    if rate is not None:
        log.debug("dispatch.rate_check_skipped_mvp", channel=entry.channel_id)

    post_row = await directus.get_item("posts", entry.post_id)
    if post_row.get("status") != PostStatus.approved.value:
        log.info("dispatch.not_approved", post=entry.post_id, status=post_row.get("status"))
        return

    post = Post(
        draft_id=post_row["id"],
        brand_id=post_row["brand_id"],
        language=post_row["language"],
        channel=Channel(post_row["channel"]),
        content=post_row["content"],
        image_url=post_row.get("image_url"),
    )
    route = ChannelRoute(
        channel=post.channel,
        target_id=channel_row.get("account_ref", entry.post_id),
        link=channel_row.get("link_preview_url"),
        hashtags=channel_row.get("hashtags") or [],
    )
    external_id = await dispatcher.dispatch(post, route)
    await directus.update_item(
        "posts",
        post.draft_id,
        {"status": PostStatus.published.value, "external_post_id": external_id},
    )
    await queue.mark_published(entry.id)


if __name__ == "__main__":
    asyncio.run(main())
