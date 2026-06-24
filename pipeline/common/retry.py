"""Retry decorator used by all external API calls.

Wraps tenacity with sane defaults for our case:
* exponential backoff 1s → 2s → 4s → 8s → 16s
* up to 5 attempts
* respects ``Retry-After`` on 429 if the caller raises an HTTPError carrying it
* retries on connection errors, timeouts, and 5xx / 429

Apply to **every** outbound call (OpenAI, Replicate, Meta, Sanity,
Telegram, httpx fetches). Non-retryable 4xx must raise and abort.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .logging import get_logger

log = get_logger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether ``exc`` should trigger another attempt."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    return False


def with_retry(
    *,
    max_attempts: int = 5,
    multiplier: float = 1.0,
    max_wait: float = 30.0,
) -> Any:
    """Decorator: retry with exponential backoff, up to ``max_attempts`` total."""

    def _before_sleep(retry_state: Any) -> None:
        log.warning(
            "retrying",
            fn=retry_state.fn.__name__ if retry_state.fn else "?",
            attempt=retry_state.attempt_number,
            next_sleep_s=getattr(retry_state.next_action, "sleep", None),
            err=str(retry_state.outcome.exception()) if retry_state.outcome else None,
        )

    return retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=multiplier, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        before_sleep=_before_sleep,
        reraise=True,
    )
