"""NTS_058 Task 2 — systemd sd_notify watchdog helper."""

from __future__ import annotations

import asyncio
import socket

from pipeline.admin import watchdog


def test_sd_notify_noop_without_socket(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert watchdog.sd_notify("WATCHDOG=1") is False


def test_start_watchdog_returns_none_without_socket(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    async def _run():
        return watchdog.start_watchdog()

    assert asyncio.run(_run()) is None


def test_sd_notify_sends_to_socket(tmp_path, monkeypatch) -> None:
    """A real datagram lands on the unix socket named by $NOTIFY_SOCKET."""
    # AF_UNIX paths are capped (~104 bytes on macOS); pytest's tmp_path blows
    # that, so chdir there and bind a short relative name instead.
    monkeypatch.chdir(tmp_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("n.sock")
    srv.settimeout(2.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", "n.sock")
        assert watchdog.sd_notify("WATCHDOG=1") is True
        assert srv.recv(64) == b"WATCHDOG=1"
    finally:
        srv.close()


def test_ping_interval_from_watchdog_usec(monkeypatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")  # 30s
    assert watchdog._ping_interval() == 15.0
