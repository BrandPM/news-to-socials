"""Secret masking for anything that reaches a log (NTS_129 P2 Ф0.2).

``httpx`` logs one INFO line per request containing the full URL, and the
Telegram Bot API puts the bot token *in the path*:

    HTTP Request: POST https://api.telegram.org/bot8123456:AAH…/sendMessage

So every alert the monitor sent wrote the token into ``journalctl`` and into
the output of any run that touched Telegram. Nothing was breached — the logs
are on the VPS and in this session's scrollback — but a token that has been
written to a log is a token to rotate, and one that keeps being written is a
leak that renews itself daily.

Two layers, because either alone leaves a hole:

**The processor** masks anything that reaches structlog, whatever wrote it and
whichever field it landed in. That covers our own log lines, including the ones
that pass a URL as an event value.

**The logger levels** stop ``httpx``/``httpcore`` at WARNING, so the per-request
INFO line is not emitted at all. Masking it would have been enough for safety
and useless for noise: those lines are one per HTTP call and say nothing an
operator reads.

The patterns are deliberately narrow. A masker that ate anything resembling a
key would eventually eat a candidate id or a Sanity asset ref and make a log
unreadable in the one situation logs exist for.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

# Telegram bot tokens: ``<digits>:<35-ish base64url chars>``, in a URL path or
# on its own. The colon is what makes this shape unambiguous.
_TELEGRAM_TOKEN = re.compile(r"(bot)?(\d{6,12}):([A-Za-z0-9_-]{30,})")
# OpenAI / Anthropic style keys.
_SK_KEY = re.compile(r"\b(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]{8,}")
# Sanity and Replicate tokens travel in headers, not URLs, but a stray
# ``Authorization: Bearer …`` in an exception message would carry one.
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{16,}", re.IGNORECASE)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_TELEGRAM_TOKEN, r"\1\2:***"),
    (_SK_KEY, r"\1***"),
    (_BEARER, r"\1***"),
)


def redact(value: str) -> str:
    """Mask every known secret shape in ``value``.

    The token's *numeric id* is deliberately kept: it identifies which bot the
    line is about, which is the only thing an operator needs from it, and it is
    not the secret half.
    """
    if not value:
        return value
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: mask secrets in every string value.

    Applied to values rather than to the rendered line, so it works with both
    the JSON and the console renderer and cannot be defeated by a field that
    happens to be rendered first.
    """
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact(value)
    return event_dict


class RedactingFilter:
    """stdlib ``logging`` filter, for libraries that never see structlog.

    Rewrites the formatted message in place. Used as a belt to the levels'
    braces: if someone lowers ``httpx`` back to INFO to debug a request, the
    token still does not reach the file.
    """

    def filter(self, record: Any) -> bool:
        if isinstance(getattr(record, "msg", None), str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


# Libraries whose INFO output is one line per HTTP call and carries URLs.
NOISY_HTTP_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "openai._base_client",
)
