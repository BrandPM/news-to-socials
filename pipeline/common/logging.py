"""Structured logging setup.

Use ``get_logger(__name__)`` at the top of any module. In dev, logs render as
human-readable colored lines; in production (LOG_LEVEL=INFO or stricter,
NO_COLOR env, or non-TTY stdout), they render as JSON for easy ingestion.
"""

from __future__ import annotations

import logging
import sys

import structlog

from .config import get_settings
from .redaction import NOISY_HTTP_LOGGERS, RedactingFilter, redact_processor


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Call once at startup."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # NTS_129 P2 Ф0.2 — httpx logs one INFO line per request with the full URL,
    # and the Telegram Bot API carries the bot token in the path. Every alert
    # the monitor sent wrote that token into journalctl. Two layers: the noisy
    # loggers are raised to WARNING so the line is not emitted, and a filter
    # masks anything that still gets through (someone lowering the level to
    # debug a request should not re-open the leak).
    redacting = RedactingFilter()
    for name in NOISY_HTTP_LOGGERS:
        noisy = logging.getLogger(name)
        noisy.setLevel(max(level, logging.WARNING))
        noisy.addFilter(redacting)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redacting)

    is_tty = sys.stdout.isatty()
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer() if is_tty else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            # Before the renderer, and over values rather than the rendered
            # line, so it holds for both JSON and console output.
            redact_processor,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
