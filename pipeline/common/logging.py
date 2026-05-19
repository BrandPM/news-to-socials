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


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Call once at startup."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    is_tty = sys.stdout.isatty()
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer() if is_tty else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
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
