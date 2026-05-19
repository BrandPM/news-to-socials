"""Dispatcher — routes a Post to the right publisher.

Channels are 1:1 mapped to publishers:

* ``blog``                  → :class:`DirectusPublisher`
* ``telegram``              → :class:`TelegramPublisher`
* ``facebook``, ``instagram`` → :class:`MetaGraphPublisher`

The dispatcher is also the only place that calls into the audit log on
success/failure, so callers don't need to remember to log.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Channel, Post
from .directus import DirectusClient, DirectusPublisher
from .meta_graph import MetaGraphPublisher
from .telegram_bot import TelegramPublisher

log = get_logger(__name__)


@dataclass(frozen=True)
class ChannelRoute:
    """Per-channel destination info loaded from Directus.channels."""

    channel: Channel
    target_id: str  # page_id / ig_user_id / chat_id / directus posts row id
    link: str | None = None  # optional FB link preview source
    hashtags: list[str] | None = None


class Dispatcher:
    def __init__(
        self,
        directus_client: DirectusClient | None = None,
        telegram_pub: TelegramPublisher | None = None,
        meta_pub: MetaGraphPublisher | None = None,
    ) -> None:
        self.directus = directus_client or DirectusClient()
        self.directus_pub = DirectusPublisher(self.directus)
        self.telegram_pub = telegram_pub or TelegramPublisher()
        self.meta_pub = meta_pub or MetaGraphPublisher()

    async def dispatch(self, post: Post, route: ChannelRoute) -> str:
        """Publish ``post`` via the right backend; return external id."""
        settings = get_settings()
        if settings.dry_run:
            log.info("dispatch.dry_run", channel=post.channel.value, route=route.target_id)
            return f"dryrun-{post.channel.value}-{route.target_id}"

        if post.channel is Channel.blog:
            external = await self.directus_pub.publish(post, route.target_id)
        elif post.channel is Channel.telegram:
            external = await self.telegram_pub.publish(post, route.target_id)
        elif post.channel in (Channel.facebook, Channel.instagram):
            external = await self.meta_pub.publish(
                post, route.target_id, link=route.link, hashtags=route.hashtags
            )
        else:
            raise ValueError(f"Unsupported channel: {post.channel}")

        # Audit log → Directus
        try:
            await self.directus.create_item(
                "audit_log",
                {
                    "action": "publish",
                    "entity": "post",
                    "entity_id": post.draft_id,
                    "actor": "pipeline",
                    "payload_json": {
                        "channel": post.channel.value,
                        "external_id": external,
                        "brand_id": post.brand_id,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Don't fail the publish over a logging hiccup.
            log.warning("audit_log.write_failed", err=str(exc))
        return external
