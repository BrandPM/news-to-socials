"""Telegram channel source.

Implemented in Stage 4. Intentionally a stub for now so the registry
import-graph stays valid and we can write integration tests against the
abstract :class:`Source`.

Implementation notes for later:
* Use `telethon` (MTProto) — Bot API can't read channels you don't own.
* Keep an offset per ``source_id`` in our SQLite to avoid double-processing.
* Strip Telegram-specific markup; the LLM doesn't need it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..common.logging import get_logger
from ..common.models import RawItem, SourceType
from .base import Source, register

log = get_logger(__name__)


@register
class TelegramSource(Source):
    type = SourceType.telegram

    async def fetch(self, since: datetime | None = None) -> Iterable[RawItem]:
        # TODO(stage-4): implement via telethon.
        log.warning("telegram.fetch.not_implemented", source=self.name)
        return []
