"""Generic web/HTML source — for sources without RSS.

Stub for now. Use BeautifulSoup + a per-source CSS-selector config to extract
article cards. When pages are paywalled, ``_render_get`` from the base class
handles the browser-rendering fallback.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..common.logging import get_logger
from ..common.models import RawItem, SourceType
from .base import Source, register

log = get_logger(__name__)


@register
class WebSource(Source):
    type = SourceType.web

    async def fetch(self, since: datetime | None = None) -> Iterable[RawItem]:
        # TODO(stage-3+): per-source selectors stored in admin.db sources.opts.
        log.warning("web.fetch.not_implemented", source=self.name)
        return []
