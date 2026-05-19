"""Directus publisher + minimal client.

Used both as a publisher (for the blog channel) and as a state store
(every Post goes into ``posts`` so the bot and the dispatcher can read it).

We hit Directus over plain REST with ``httpx``. The Python ``directus-sdk``
exists but is a thin wrapper and adds another moving part; for ~20 endpoints
we touch, raw HTTP is fine.

Endpoints used:
* ``POST /items/posts``  — create post record
* ``PATCH /items/posts/{id}`` — update status / external_post_id
* ``POST /files``  — multipart upload (images)
* ``GET /items/brands/{id}`` — load voice profile + visual config
"""

from __future__ import annotations

from typing import Any

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Post, PostStatus
from ..common.retry import with_retry

log = get_logger(__name__)


class DirectusClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        s = get_settings()
        self.base_url = (base_url or s.directus_url).rstrip("/")
        self.token = token or s.directus_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @with_retry()
    async def create_item(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/items/{collection}",
                headers=self._headers(),
                json=data,
            )
            resp.raise_for_status()
            return resp.json().get("data", {})

    @with_retry()
    async def update_item(
        self, collection: str, item_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{self.base_url}/items/{collection}/{item_id}",
                headers=self._headers(),
                json=patch,
            )
            resp.raise_for_status()
            return resp.json().get("data", {})

    @with_retry()
    async def get_item(self, collection: str, item_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/items/{collection}/{item_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json().get("data", {})

    @with_retry()
    async def upload_file(self, content: bytes, filename: str, mimetype: str) -> str:
        """Upload bytes to Directus Files; return the file ID (UUID)."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/files",
                headers={"Authorization": f"Bearer {self.token}"},
                files={"file": (filename, content, mimetype)},
            )
            resp.raise_for_status()
            return str(resp.json().get("data", {}).get("id", ""))


class DirectusPublisher:
    """Publishes a blog-channel post into Directus.

    Pre-condition: the matching ``posts`` row already exists (created at
    Draft time with status=draft). Here we just flip the status to
    ``published`` and write ``external_post_id`` = same id (no external
    system for the blog — the post IS the blog).
    """

    def __init__(self, client: DirectusClient | None = None) -> None:
        self.client = client or DirectusClient()

    async def publish(self, post: Post, posts_row_id: str) -> str:
        await self.client.update_item(
            "posts",
            posts_row_id,
            {
                "status": PostStatus.published.value,
                "content": post.content,
                "external_post_id": posts_row_id,
            },
        )
        log.info("directus.published", post_id=posts_row_id, brand=post.brand_id)
        return posts_row_id
