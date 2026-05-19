"""Source base class and registry.

Pattern note: this is our take on the "Provider" interface from fin-thread —
the abstraction is the same (a thing that takes a config and yields RawItem),
but we keep it async and add a paywall-fallback hook inspired by meridian's
Browser-Rendering API integration.

See also:
    /research/fin-thread/journalist/provider.go (GPL-3.0, pattern only)
    /research/meridian/apps/backend/src/   (MIT)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import ClassVar

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import RawItem, SourceType
from ..common.retry import with_retry

log = get_logger(__name__)


class Source(ABC):
    """Abstract source. Subclass and set ``type`` to register."""

    type: ClassVar[SourceType]

    def __init__(self, source_id: str, name: str, url: str, **opts: object) -> None:
        self.source_id = source_id
        self.name = name
        self.url = url
        self.opts = opts

    @abstractmethod
    async def fetch(self, since: datetime | None = None) -> Iterable[RawItem]:
        """Return items newer than ``since`` (or all if ``since`` is None)."""

    # ----- shared helpers below -----

    @with_retry()
    async def _http_get(self, url: str, timeout: float = 20.0) -> httpx.Response:
        """Plain HTTP fetch with project-wide retry policy."""
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "news-to-socials/0.0.1 (+https://icon.finance)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp

    @with_retry()
    async def _render_get(self, url: str) -> str:
        """Paywall-aware fetch: route through BROWSER_RENDER_URL if configured.

        Mitigates §5 W9 from the Master Documentation. If no render service is
        configured we just fall back to a plain GET — the caller decides what
        to do with the result (RSS-only headlines are still usable).
        """
        settings = get_settings()
        if not settings.browser_render_url:
            resp = await self._http_get(url)
            return resp.text

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                settings.browser_render_url,
                json={"url": url},
                headers={"Authorization": f"Bearer {settings.browser_render_token}"},
            )
            resp.raise_for_status()
            return resp.json().get("html", "")


# Registry populated by import-time side effects in submodules.
REGISTRY: dict[SourceType, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """Decorator: register a concrete Source class by its ``type`` attribute."""
    REGISTRY[cls.type] = cls
    return cls


def utcnow() -> datetime:
    """Helper: aware UTC ``now()``."""
    return datetime.now(tz=timezone.utc)
