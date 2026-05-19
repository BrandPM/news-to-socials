"""Rate-limit parsing and check.

Format: ``"<count>/<unit>"`` where unit ∈ {``minute``, ``hour``, ``day``}.

The actual check is delegated to whoever feeds ``recent_count`` in (the
queue ``dequeue_ready`` step counts published rows from ``audit_log`` for
the channel inside the time window). We just declare the limit here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

_RE = re.compile(r"^(?P<count>\d+)\s*/\s*(?P<unit>minute|hour|day)s?$", re.IGNORECASE)

_UNIT_TO_DELTA = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}


@dataclass(frozen=True)
class RateLimit:
    count: int
    window: timedelta

    def has_room(self, recent_count: int) -> bool:
        return recent_count < self.count


def parse_rate(spec: str | None) -> RateLimit | None:
    if not spec:
        return None
    m = _RE.match(spec.strip())
    if not m:
        raise ValueError(f"Invalid rate spec: {spec!r}; expected e.g. '3/day' or '1/hour'")
    return RateLimit(
        count=int(m.group("count")),
        window=_UNIT_TO_DELTA[m.group("unit").lower()],
    )
