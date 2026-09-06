"""Secrets never reach a log (NTS_129 P2 Ф0.2).

The leak this closes: ``httpx`` emits one INFO line per request containing the
full URL, and the Telegram Bot API puts the bot token in the path. Every alert
the monitor sent wrote the token into ``journalctl``.

Two properties, and both need their own test because either alone leaves a
hole:

* **The noisy loggers are silent at INFO**, so the line is not emitted.
* **What does get emitted is masked**, so lowering the level to debug a request
  cannot re-open the leak — which is exactly when someone would.

And one negative test, which matters as much as the rest: the masker must not
eat ordinary identifiers. A redactor that swallowed a Sanity asset ref would
make the log useless in the situation logs exist for, and nobody would notice
until they needed it.
"""

from __future__ import annotations

import io
import logging

import pytest

from pipeline.common.logging import configure_logging
from pipeline.common.redaction import (
    NOISY_HTTP_LOGGERS,
    RedactingFilter,
    redact,
    redact_processor,
)

# A token-shaped string that is not a real credential.
FAKE_TOKEN = "8123456789:AAHxyz_abcDEF-1234567890abcdefghij"
TELEGRAM_LINE = (
    f'HTTP Request: POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage '
    '"HTTP/1.1 200 OK"'
)


def test_the_telegram_token_is_masked_and_the_bot_id_survives():
    """The numeric id says WHICH bot the line is about and is not the secret
    half; keeping it is the difference between a redacted log and a useless
    one."""
    masked = redact(TELEGRAM_LINE)
    assert "AAHxyz_abcDEF" not in masked
    assert FAKE_TOKEN not in masked
    assert "bot8123456789:***" in masked
    # The rest of the line is intact — it is what an operator reads.
    assert "api.telegram.org" in masked and "sendMessage" in masked


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("key=sk-proj-abcdefghijklmnopqrstuvwxyz0123", "vwxyz0123"),
        ("Authorization: Bearer skABCDEFGHIJKLMNOPQRSTUVWX", "MNOPQRSTUVWX"),
        (f"token {FAKE_TOKEN} in a bare field", "AAHxyz_abcDEF"),
    ],
)
def test_other_secret_shapes_are_masked(raw, must_not_contain):
    assert must_not_contain not in redact(raw)


@pytest.mark.parametrize(
    "identifier",
    [
        "drafts.post-867c9c7506da",
        "image-f8c54c0c680c7516a7e7382da9e29e2e78a8a7ce-1200x630-png",
        "candidate 962 run 146 topicId f748cfd1788e188a",
        "https://www.finma.ch/en/news/2026/08/20260820-sr/",
        "2026-09-06T18:21:17.217125",
    ],
)
def test_ordinary_identifiers_are_left_alone(identifier):
    """The masker is narrow on purpose. Eating a draft id or an asset ref would
    break the log in the one situation it is read."""
    assert redact(identifier) == identifier


def test_the_structlog_processor_masks_every_string_value():
    """Values, not the rendered line: it must hold for the JSON renderer and
    the console one, and must not depend on field order."""
    event = redact_processor(
        None,
        "info",
        {
            "event": "alerts.send_failed",
            "url": f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
            "chat_id": 12345,
            "note": f"retrying with {FAKE_TOKEN}",
        },
    )
    assert "AAHxyz_abcDEF" not in str(event)
    assert event["chat_id"] == 12345  # non-strings pass through untouched


def test_the_stdlib_filter_masks_the_message_and_its_args():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s",
        args=(f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert "AAHxyz_abcDEF" not in record.getMessage()


def test_httpx_is_silent_at_info_after_configure_logging(monkeypatch):
    """The first line of defence: the per-request line is not emitted at all.

    Masking it would have been enough for safety and useless for noise — those
    lines are one per HTTP call and say nothing an operator reads.
    """
    from pipeline.common import config as config_module

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()

    for name in NOISY_HTTP_LOGGERS:
        assert logging.getLogger(name).level >= logging.WARNING, name

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addHandler(handler)
    try:
        httpx_logger.info(TELEGRAM_LINE)
        assert stream.getvalue() == "", "httpx INFO reached a handler"
        # And if someone raises the level to debug a request, the filter still
        # holds the line.
        httpx_logger.warning(TELEGRAM_LINE)
        assert "AAHxyz_abcDEF" not in stream.getvalue()
    finally:
        httpx_logger.removeHandler(handler)
