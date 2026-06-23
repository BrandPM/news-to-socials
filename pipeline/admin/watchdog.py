"""systemd watchdog integration (NTS_058 Task 2).

The admin API runs under systemd with ``WatchdogSec=30``. systemd expects the
service to send ``WATCHDOG=1`` via the ``sd_notify`` protocol at least once per
watchdog window or it force-restarts the unit. We send those pings **from the
asyncio event loop** on purpose: if the loop ever wedges again (the NTS_058
incident — a blocking ``feedparser.parse`` froze everything), the pings stop and
systemd restarts us automatically instead of leaving a dead-but-listening box.

No external dependency: we talk the ``sd_notify`` wire protocol directly over the
``$NOTIFY_SOCKET`` unix datagram socket. When that env var is absent (local dev,
test suite) everything no-ops, so importing/starting the watchdog is always safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

logger = logging.getLogger(__name__)

# Fallback ping interval (seconds) if systemd did not export WATCHDOG_USEC but a
# NOTIFY_SOCKET is present. Comfortably under any sane WatchdogSec.
_DEFAULT_PING_INTERVAL_S = 10.0


def sd_notify(state: str) -> bool:
    """Send a single ``sd_notify`` message. Returns True if it was sent.

    No-ops (returns False) when ``$NOTIFY_SOCKET`` is unset — i.e. when we are
    not running under systemd (local dev, tests).
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Abstract namespace sockets start with '@' which maps to a leading NUL.
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        logger.warning("sd_notify failed for state %r", state, exc_info=True)
        return False


def _ping_interval() -> float:
    """Half the systemd watchdog window, in seconds (WATCHDOG_USEC / 2)."""
    usec = os.environ.get("WATCHDOG_USEC")
    if usec:
        try:
            return max(1.0, (int(usec) / 1_000_000) / 2)
        except ValueError:
            pass
    return _DEFAULT_PING_INTERVAL_S


async def _watchdog_loop(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        # Runs on the event loop: a wedged loop can't reach this, so the pings
        # stop and systemd restarts the unit. That's the whole point.
        sd_notify("WATCHDOG=1")


def start_watchdog() -> asyncio.Task | None:
    """Start the periodic WATCHDOG=1 pinger if running under systemd.

    Returns the created asyncio Task (so the lifespan can cancel it on
    shutdown), or None when there is no watchdog to feed.
    """
    if not os.environ.get("NOTIFY_SOCKET"):
        return None
    interval = _ping_interval()
    # Tell systemd we are up (harmless under Type=simple, required if the unit
    # is ever switched to Type=notify) and send the first ping immediately.
    sd_notify("READY=1")
    sd_notify("WATCHDOG=1")
    logger.info("systemd watchdog pinger started (every %.1fs)", interval)
    return asyncio.create_task(_watchdog_loop(interval), name="sd-watchdog")
