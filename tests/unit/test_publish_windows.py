"""Unit tests for queue/publish_windows.py."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pipeline.queue.publish_windows import (
    is_within_window,
    parse_window,
)


def test_parse_basic() -> None:
    w = parse_window("09:00-22:00 Europe/Madrid")
    assert w is not None
    assert w.start.hour == 9
    assert w.end.hour == 22


def test_parse_none_is_unrestricted() -> None:
    assert parse_window(None) is None
    assert parse_window("") is None
    assert is_within_window(None) is True


def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_window("9-22 UTC")


def test_within_window_simple() -> None:
    w = parse_window("09:00-22:00 UTC")
    inside = datetime(2026, 5, 17, 12, 0, tzinfo=ZoneInfo("UTC"))
    outside_early = datetime(2026, 5, 17, 6, 0, tzinfo=ZoneInfo("UTC"))
    outside_late = datetime(2026, 5, 17, 23, 0, tzinfo=ZoneInfo("UTC"))
    assert is_within_window(w, inside)
    assert not is_within_window(w, outside_early)
    assert not is_within_window(w, outside_late)


def test_window_crossing_midnight() -> None:
    w = parse_window("22:00-06:00 UTC")
    assert is_within_window(w, datetime(2026, 5, 17, 23, 0, tzinfo=ZoneInfo("UTC")))
    assert is_within_window(w, datetime(2026, 5, 17, 3, 0, tzinfo=ZoneInfo("UTC")))
    assert not is_within_window(w, datetime(2026, 5, 17, 12, 0, tzinfo=ZoneInfo("UTC")))
