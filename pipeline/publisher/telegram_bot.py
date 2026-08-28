"""Telegram Bot API transport for the monitoring alerter (NTS_073/075).

Raw HTTP because one endpoint (``sendMessage``) is the whole need. This module
is **live, but as monitoring, not as a channel**: ``_send_message`` is called by
:mod:`pipeline.monitoring.alerts` and by the judge's flag alert, and nothing
else. The Wave-3 channel publisher (``publish``, ``_send_photo``) and the
per-channel post formatters it depended on were removed in NTS_121 §7 — they
had no caller after the ADR-018 pivot to Sanity.

Mitigations baked in:
* 429 ``retry_after`` honoured via tenacity (see common/retry.py).
* HTML parse_mode by default — what the alert formatters produce.
* Bot token is redacted from HTTP errors before they reach logs — the
  token sits in the request URL (``.../bot<token>/...``), and an
  unredacted ``httpx.HTTPStatusError`` would otherwise write it into the
  monitoring logs on any API error (e.g. a 429). NTS_076.
"""

from __future__ import annotations

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.retry import with_retry

log = get_logger(__name__)


def _redact(text: str, token: str) -> str:
    """Strip the bot token from a string before it can reach logs."""
    return text.replace(token, "<bot-token-redacted>") if token else text


def _raise_for_status_redacted(resp: httpx.Response, token: str) -> None:
    """``resp.raise_for_status()`` but with the token scrubbed from the error.

    Re-raises the SAME ``httpx.HTTPStatusError`` type (so tenacity's retry
    predicate, which keys on the type + ``response.status_code``, is
    unchanged) — only the message string is redacted, and ``from None``
    drops the original (URL-bearing) exception from the chain.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            _redact(str(exc), token),
            request=exc.request,
            response=exc.response,
        ) from None


class TelegramPublisher:
    """Send messages to a Telegram chat as the bot identified by token."""

    def __init__(self, bot_token: str | None = None) -> None:
        self.bot_token = bot_token or get_settings().telegram_bot_token
        self.api = f"https://api.telegram.org/bot{self.bot_token}"

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
            _raise_for_status_redacted(resp, self.bot_token)
            data = resp.json()["result"]
            log.info("telegram.sent", chat=chat_id, message_id=data["message_id"])
            return str(data["message_id"])
