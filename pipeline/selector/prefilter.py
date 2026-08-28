"""Free, configurable, measurable prefilter (IT_PROJ_NTS_099 §1).

Runs between dedup and the guard: **before any paid call**. Nothing here asks
a model anything — it is title patterns, a summary length, an age and a
language list, all of them values on the brand's ``pipeline_config`` row and
therefore editable from the Editorial Policy screen without a deploy.

Two rules that are easy to get backwards, so they are stated here and asserted
by test:

* **Deny title patterns do not apply to ``primary_feed``.** A regulator may
  "appoint" a board and a tax authority may publish an "outlook"; on a
  regulator's own feed those words describe the composition of an organ or a
  fiscal projection, not a personnel story. Applying the news deny-list there
  silently drops exactly the class of item v3 exists to read (NTS_099 §1).
* **Age is per source role**, 72 h for news and 240 h for primary feeds
  (NTS_099 §1): a consultation paper is still worth writing about a week
  later, a news item about it is not.

The measured output is ``prefilter_drop_rate`` — NTS_099 §1 wants it in every
run summary, with an alert below 0.3 (the prefilter is not doing anything) and
above 0.95 (it is eating the feed). Both are equally bad and neither is
visible without the number.

Nothing here raises. An unparseable date or a missing language is a *keep* with
a reason logged: the prefilter is an optimisation on the guard's bill, and the
one thing it must never do is drop an item because of an internal defect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# Why an item was dropped. Deliberately NOT the guard's ``reason_code`` enum:
# a prefilter drop never reaches the guard and never becomes a candidate, so
# borrowing that vocabulary would put rows in the funnel that no verdict
# produced.
PREFILTER_DROP_REASONS = (
    "deny_title",
    "no_summary",
    "summary_too_short",
    "too_old",
    "language",
)


@dataclass(frozen=True)
class PrefilterRules:
    """The five NTS_099 §1 keys, lifted off a :class:`ConfigRecord`."""

    deny_title_patterns: tuple[str, ...]
    require_summary: bool
    max_age_hours_news: int
    max_age_hours_primary: int
    languages: tuple[str, ...]
    min_summary_chars: int

    @classmethod
    def from_config(cls, config: Any) -> PrefilterRules:
        """Build from a ``ConfigRecord``. ``getattr`` with the spec defaults so
        a config row predating migration 020 still yields usable rules."""
        return cls(
            deny_title_patterns=tuple(
                str(p).strip().lower()
                for p in getattr(config, "prefilter_deny_title_patterns", ())
                if str(p).strip()
            ),
            require_summary=bool(getattr(config, "prefilter_require_summary", True)),
            max_age_hours_news=int(
                getattr(config, "prefilter_max_age_hours_news", 72) or 72
            ),
            max_age_hours_primary=int(
                getattr(config, "prefilter_max_age_hours_primary", 240) or 240
            ),
            languages=tuple(
                str(x).strip().lower()
                for x in getattr(config, "prefilter_languages", ())
                if str(x).strip()
            ),
            min_summary_chars=int(
                getattr(config, "prefilter_min_summary_chars", 80) or 0
            ),
        )

    def max_age_hours(self, source_role: str) -> int:
        return (
            self.max_age_hours_primary
            if source_role in ("primary_feed", "primary_site")
            else self.max_age_hours_news
        )


@dataclass(frozen=True)
class PrefilterDecision:
    keep: bool
    reason: str | None = None
    detail: str | None = None


_KEEP = PrefilterDecision(True)


def prefilter_item(
    *,
    title: str,
    summary: str | None,
    published_at: datetime | None,
    source_role: str,
    source_language: str | None,
    rules: PrefilterRules,
    now: datetime | None = None,
) -> PrefilterDecision:
    """Decide one feed item. Never raises; an internal defect is a keep."""
    now = now or datetime.now(tz=UTC)
    text = (title or "").strip()
    body = (summary or "").strip()

    # 1. Deny patterns — news feeds only (see module docstring).
    if source_role not in ("primary_feed", "primary_site"):
        lowered = text.lower()
        for pattern in rules.deny_title_patterns:
            if pattern in lowered:
                return PrefilterDecision(False, "deny_title", pattern)

    # 2. Summary present / long enough.
    if rules.require_summary and not body:
        return PrefilterDecision(False, "no_summary")
    if body and len(body) < rules.min_summary_chars:
        return PrefilterDecision(
            False, "summary_too_short", f"{len(body)}<{rules.min_summary_chars}"
        )

    # 3. Age. An item with no date is KEPT: plenty of regulator feeds omit
    #    pubDate, and dropping those would remove a whole source class on a
    #    formatting detail.
    if published_at is not None:
        cutoff_hours = rules.max_age_hours(source_role)
        published = published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        if published < now - timedelta(hours=cutoff_hours):
            age_h = int((now - published).total_seconds() // 3600)
            return PrefilterDecision(False, "too_old", f"{age_h}h>{cutoff_hours}h")

    # 4. Language. Only checked when both sides are known — an unclassified
    #    source must not be filtered on a field nobody filled in.
    lang = (source_language or "").strip().lower()
    if lang and rules.languages and lang not in rules.languages:
        return PrefilterDecision(False, "language", lang)

    return _KEEP


def drop_rate(*, considered: int, dropped: int) -> float:
    """``dropped / considered``, 0.0 on an empty run.

    NTS_099 §1's metric. Zero for an empty run rather than ``None`` so the
    summary always prints a number — but note that an empty intake makes this
    0.0 for the same reason a broken prefilter does, which is why the heartbeat
    prints the absolute counts next to it (NTS_106 §2).
    """
    if considered <= 0:
        return 0.0
    return dropped / considered


def is_drop_rate_alarming(rate: float, *, considered: int) -> bool:
    """NTS_099 §1: alert below 0.3 (not filtering) or above 0.95 (eating the
    feed). Suppressed under 10 items, where the ratio is noise."""
    if considered < 10:
        return False
    return rate < 0.3 or rate > 0.95
