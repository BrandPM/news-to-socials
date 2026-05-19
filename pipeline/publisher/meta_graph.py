"""Meta Graph API publisher: Facebook pages + Instagram business accounts.

API version is **pinned** to mitigate §5 W7 (Meta breaking changes). When
v18 deprecates, we move with intention — not because Meta upgraded us.

Facebook page post:
* photo:  POST /{page_id}/photos      caption + url
* link:   POST /{page_id}/feed        message + link

Instagram business post (always image-first):
* step 1: POST /{ig_user_id}/media         → container_id
* step 2: POST /{ig_user_id}/media_publish → published id

Both endpoints take ``access_token`` as a query arg or in body. We pass it
in the body to keep it out of access logs.
"""

from __future__ import annotations

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Channel, Post
from ..common.retry import with_retry

log = get_logger(__name__)

_API_VERSION = "v18.0"
_BASE = f"https://graph.facebook.com/{_API_VERSION}"


class MetaGraphPublisher:
    def __init__(self, access_token: str | None = None) -> None:
        s = get_settings()
        self.access_token = access_token or s.meta_access_token

    async def publish(
        self,
        post: Post,
        target_id: str,
        link: str | None = None,
        hashtags: list[str] | None = None,
    ) -> str:
        if post.channel is Channel.facebook:
            return await self._publish_facebook(
                page_id=target_id,
                message=post.content,
                image_url=str(post.image_url) if post.image_url else None,
                link=link,
            )
        if post.channel is Channel.instagram:
            if not post.image_url:
                raise ValueError("Instagram requires image_url")
            caption = post.content
            if hashtags:
                tag_block = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
                caption = f"{caption}\n\n{tag_block}"
            return await self._publish_instagram(
                ig_user_id=target_id,
                image_url=str(post.image_url),
                caption=caption,
            )
        raise ValueError(f"MetaGraphPublisher cannot handle channel {post.channel}")

    @with_retry()
    async def _publish_facebook(
        self,
        page_id: str,
        message: str,
        image_url: str | None,
        link: str | None,
    ) -> str:
        if image_url:
            url = f"{_BASE}/{page_id}/photos"
            payload = {"url": image_url, "caption": message, "access_token": self.access_token}
        else:
            url = f"{_BASE}/{page_id}/feed"
            payload = {"message": message, "access_token": self.access_token}
            if link:
                payload["link"] = link

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            external_id = str(resp.json().get("id", ""))
            log.info("meta.fb.published", page_id=page_id, id=external_id)
            return external_id

    @with_retry()
    async def _publish_instagram(
        self, ig_user_id: str, image_url: str, caption: str
    ) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # step 1 — container
            resp = await client.post(
                f"{_BASE}/{ig_user_id}/media",
                data={
                    "image_url": image_url,
                    "caption": caption,
                    "access_token": self.access_token,
                },
            )
            resp.raise_for_status()
            container_id = str(resp.json().get("id", ""))

            # step 2 — publish
            resp2 = await client.post(
                f"{_BASE}/{ig_user_id}/media_publish",
                data={
                    "creation_id": container_id,
                    "access_token": self.access_token,
                },
            )
            resp2.raise_for_status()
            external_id = str(resp2.json().get("id", ""))
            log.info(
                "meta.ig.published",
                ig_user_id=ig_user_id,
                container=container_id,
                id=external_id,
            )
            return external_id
