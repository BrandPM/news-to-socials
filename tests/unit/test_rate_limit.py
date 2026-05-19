"""Unit tests for queue/rate_limit.py."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pipeline.queue.rate_limit import RateLimit, parse_rate


def test_parse_basic_forms() -> None:
    assert parse_rate("3/day") == RateLimit(count=3, window=timedelta(days=1))
    assert parse_rate("1/hour") == RateLimit(count=1, window=timedelta(hours=1))
    assert parse_rate("30/minute") == RateLimit(count=30, window=timedelta(minutes=1))


def test_parse_tolerates_plural_and_whitespace() -> None:
    assert parse_rate("3 / hours") == RateLimit(count=3, window=timedelta(hours=1))


def test_parse_none() -> None:
    assert parse_rate(None) is None
    assert parse_rate("") is None


def test_parse_invalid() -> None:
    with pytest.raises(ValueError):
        parse_rate("3 per day")


def test_has_room() -> None:
    r = RateLimit(count=3, window=timedelta(hours=1))
    assert r.has_room(0)
    assert r.has_room(2)
    assert not r.has_room(3)
    assert not r.has_room(10)
