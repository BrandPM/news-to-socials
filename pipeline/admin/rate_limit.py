"""Tiny in-memory sliding-window rate limiter (NTS_058).

Used to throttle the login-verify endpoint (5 attempts / IP / minute). The
admin API runs as a single uvicorn process for a tiny operator team, so an
in-process limiter is sufficient — no Redis, no slowapi dependency.

Not safe across multiple worker processes; if the API is ever scaled out,
swap this for a shared store. ``now`` is injectable so tests don't sleep.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt for ``key``; return True if within the limit.

        Each call counts as one attempt. Returns False once the number of
        attempts in the trailing window exceeds ``max_attempts``.
        """
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= cutoff:
                q.popleft()
            q.append(now)
            return len(q) <= self.max_attempts

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Login-verify limiter: 5 attempts / minute / IP (NTS_058 Task 3).
login_verify_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0)
