"""Telegram Bot API publisher.

Uses raw HTTP because we don't need the full ``python-telegram-bot``
framework here — just two endpoints (``sendPhoto`` and ``sendMessage``).
The bot itself (approval flow) uses the framework; that lives in ``bot/``.

Mitigations baked in:
* 429 ``retry_after`` honoured via tenacity (see common/retry.py).
* HTML parse_mode by default — matches what the adapter produces.
"""

from __future__ import annotations

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Post
from ..common.retry import with_retry
from ..adapter.telegram import will_fit_caption

log = get_logger(__name__)


class TelegramPublisher:
    """Send messages to a Telegram chat as the bot identified by token."""

    def __init__(self, bot_token: str | None = None) -> None:
        self.bot_token = bot_token or get_settings().telegram_bot_token
        self.api = f"https://api.telegram.org/bot{self.bot_token}"

    async def publish(self, post: Post, chat_id: str) -> str:
        """Send the post; return the resulting Telegram message_id as string."""
        if post.image_url and will_fit_caption(post.content):
            return await self._send_photo(chat_id, post.content, str(post.image_url))

        if post.image_url:
            # Photo with short stub + full text as follow-up.
            await self._send_photo(chat_id, "", str(post.image_url))
            return await self._send_message(chat_id, post.content)

        return await self._send_message(chat_id, post.content)

    @with_retry()
    async def _send_message(self, chat_id: str, html: str) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.api}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": html,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()["result"]
            log.info("telegram.sent", chat=chat_id, message_id=data["message_id"])
            return str(data["message_id"])

    @with_retry()
    async def _send_photo(self, chat_id: str, caption_html: str, photo_url: str) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.api}/sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": photo_url,
                    "caption": caption_html,
                    "parse_mode": "HTML",
                },
            )
            resp.raise_for_status()
            data = resp.json()["result"]
            log.info("telegram.photo_sent", chat=chat_id, message_id=data["message_id"])
            return str(data["message_id"])
