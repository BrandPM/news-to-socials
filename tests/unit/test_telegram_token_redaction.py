"""NTS_076 audit — the Telegram bot token must never reach logs.

The token sits in the request URL (``https://api.telegram.org/bot<token>/...``).
``httpx.HTTPStatusError`` embeds that URL in its message, so an unredacted
error propagating to ``alerts.py``'s ``log.exception`` would write the token
into the monitoring logs on any API error (a 429 is routine). These tests
lock in the redaction + that retry semantics are preserved.
"""

from __future__ import annotations

import httpx
import pytest

from pipeline.publisher.telegram_bot import (
    _raise_for_status_redacted,
    _redact,
)

TOKEN = "123456789:AAFakeTokenValueForTestingOnly_abcdefghi"


def test_redact_strips_token():
    text = f"error for url 'https://api.telegram.org/bot{TOKEN}/sendMessage'"
    out = _redact(text, TOKEN)
    assert TOKEN not in out
    assert "<bot-token-redacted>" in out


def test_redact_noop_when_no_token():
    assert _redact("some text", "") == "some text"


def test_raise_for_status_redacted_scrubs_token_but_keeps_status():
    req = httpx.Request(
        "POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    )
    resp = httpx.Response(429, request=req)

    with pytest.raises(httpx.HTTPStatusError) as ei:
        _raise_for_status_redacted(resp, TOKEN)

    err = ei.value
    # Token must not appear anywhere in the rendered error message.
    assert TOKEN not in str(err)
    # Same type + status preserved so tenacity still treats 429 as retryable.
    assert isinstance(err, httpx.HTTPStatusError)
    assert err.response.status_code == 429


def test_raise_for_status_redacted_noop_on_2xx():
    req = httpx.Request("POST", f"https://api.telegram.org/bot{TOKEN}/x")
    resp = httpx.Response(200, request=req)
    # No raise on success.
    _raise_for_status_redacted(resp, TOKEN)
