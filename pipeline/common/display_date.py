"""Display-date computation for drafts (IT_PROJ_NTS_084 / NTS_089).

The *displayed* publication date a reader sees on the site should be the
**news date** (the source RSS ``pubDate``), not the day a manager happened to
approve the draft. This module centralises the priority + clamp rules so both
the pipeline (draft creation) and tests share one implementation.

Priority:
  1. RSS ``pubDate`` (``RawItem.published_at``) — the news date.
  2. Fallback: the draft creation date (``now``) when no pubDate is available.

Clamps:
  * ``pubDate`` in the **future** → use today (a feed with a bad/scheduled
    date must never post-date a story into tomorrow).
  * ``pubDate`` missing/unparseable (``None``) → creation date (today).

Dates are **date-only, UTC** — storing a bare ``YYYY-MM-DD`` avoids the
timezone confusion that a full timestamp would invite (see the spec's
timezone guardrail).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

# Which rule produced the date — logged at draft creation for traceability.
DisplayDateSource = str  # "rss_pubdate" | "clamped_future" | "fallback_creation"


def _to_utc_date(dt: datetime) -> date:
    """Bare UTC calendar date of ``dt`` (naive datetimes are treated as UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


def parse_display_date(value: str | date | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` string (or pass a ``date`` through).

    Returns ``None`` for empty/None/garbage — callers treat that as "no
    display date set" and fall back to real publish time.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return _to_utc_date(value)
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def compute_published_at(display_date: str | date | None, now: datetime) -> datetime:
    """Timestamp to write to Sanity ``publishedAt`` on approve (NTS_089).

    * ``display_date`` missing/unparseable → ``now`` (legacy fallback — keeps
      the pre-NTS_089 behaviour of stamping the real publish moment).
    * ``display_date == today`` (UTC) → ``now`` (the real publish time, so
      several stories approved the same day keep natural intra-day order).
    * otherwise → that date at **12:00:00 UTC** (a stable, TZ-safe noon that
      reads as the right calendar day everywhere).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    parsed = parse_display_date(display_date)
    if parsed is None or parsed == now.date():
        return now
    return datetime(parsed.year, parsed.month, parsed.day, 12, 0, 0, tzinfo=timezone.utc)


def compute_display_date(
    published_at: datetime | None, now: datetime
) -> tuple[date, DisplayDateSource]:
    """Return ``(display_date, source)`` for a draft.

    ``published_at`` is the source item's RSS pubDate (may be ``None``).
    ``now`` is the draft-creation moment (UTC). See the module docstring for
    the priority + clamp rules. ``source`` is one of ``rss_pubdate`` /
    ``clamped_future`` / ``fallback_creation`` — logged so an operator can see
    why a given date was chosen.
    """
    today = _to_utc_date(now)
    if published_at is None:
        return today, "fallback_creation"
    pub_date = _to_utc_date(published_at)
    if pub_date > today:
        return today, "clamped_future"
    return pub_date, "rss_pubdate"
