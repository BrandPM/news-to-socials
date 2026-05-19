"""Publish-window parsing.

Format: ``"09:00-22:00 Europe/Madrid"``.

The window is computed in the configured timezone, so a window like
"09:00-22:00 Europe/Madrid" behaves correctly during DST shifts — Python's
``zoneinfo`` handles the offset for us.

Edge cases:
* Reverse windows (e.g. "22:00-06:00") cross midnight — supported.
* Empty/None window → ``is_within_window`` returns True (no restriction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_WINDOW_RE = re.compile(
    r"^(?P<from>\d{2}:\d{2})-(?P<to>\d{2}:\d{2})\s+(?P<tz>[\w/+-]+)$"
)


@dataclass(frozen=True)
class Window:
    start: time
    end: time
    tz: ZoneInfo


def parse_window(spec: str | None) -> Window | None:
    if not spec:
        return None
    m = _WINDOW_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Invalid window spec: {spec!r}; expected 'HH:MM-HH:MM TZ'")
    start = _parse_hhmm(m.group("from"))
    end = _parse_hhmm(m.group("to"))
    tz = ZoneInfo(m.group("tz"))
    return Window(start=start, end=end, tz=tz)


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))


def is_within_window(window: Window | None, now: datetime | None = None) -> bool:
    if window is None:
        return True
    n = (now or datetime.now(tz=window.tz)).astimezone(window.tz).time()
    if window.start <= window.end:
        return window.start <= n <= window.end
    # Crosses midnight
    return n >= window.start or n <= window.end


def next_window_start(window: Window | None, now: datetime | None = None) -> datetime | None:
    """Return the next moment we'd be allowed to publish.

    If ``window`` is None or we're currently inside it, returns ``now``.
    """
    if window is None:
        return now or datetime.now(tz=ZoneInfo("UTC"))
    n = (now or datetime.now(tz=window.tz)).astimezone(window.tz)
    if is_within_window(window, n):
        return n
    today_start = n.replace(
        hour=window.start.hour, minute=window.start.minute, second=0, microsecond=0
    )
    if n.time() > window.end and (window.start <= window.end):
        # Past today's end → tomorrow's start
        return today_start + timedelta(days=1)
    return today_start
