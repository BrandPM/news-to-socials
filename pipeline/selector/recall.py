"""The recall test, computed rather than marked by hand (NTS_099 §7, NTS_115).

The acceptance criterion of NTS_114 is two ratios over twenty seed topics:

    in_feed ≥ 0.7            — did the subject reach the funnel at all
    accepted / in_feed ≥ 0.8 — and did the rubric keep it once it did

They are different failures with different fixes, which is why they are two
numbers and not one. A low ``in_feed`` is a **sourcing** problem: the feeds do
not carry that subject, and no amount of rubric editing will change it. A low
``accepted/in_feed`` is an **editorial** problem: the material arrives and the
guard throws it away.

This was meant to be done by hand during the shadow week and never was — nine
days of intake, no verdicts (NTS_117, gate journal). Andriy's directive of
2026-09-06 replaced the manual exercise with this: the same two ratios,
recomputed over the accumulated ``candidates`` whenever anyone opens the
screen, so the number moves as the portfolio grows instead of being pasted into
a document once.

**Keyword matching, deliberately, not embeddings.** The question is whether a
subject reached the funnel — a fact about the feed, not a similarity. An
embedding threshold would answer a softer question and return a number that
looks identical, and the two would be impossible to tell apart in a report.

**A topic whose channel does not exist is reported separately.** NTS_119 left
the two EUR-Lex saved searches unbuilt (only Andriy can create them), so seed
topics 2, 3, 13 and 15 are missing a channel rather than being missed by the
rubric. Counting those as recall failures would blame the guard for a feed that
was never connected — the exact mistake that makes a metric useless.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# NTS_114 §Приёмка.
TARGET_IN_FEED = 0.7
TARGET_ACCEPTED = 0.8

# Jurisdictions whose only intended channel is a feed that does not exist yet
# (NTS_119: the two EUR-Lex saved searches need a ``myRssId`` only a human can
# create). Reported as "no channel", never as a recall failure.
MISSING_CHANNEL_JURISDICTIONS: tuple[str, ...] = ("EU",)
MISSING_CHANNEL_SOURCE_CLASS = "legislation"


@dataclass
class TopicResult:
    """One seed topic's outcome over the accumulated candidates."""

    topic: str
    jurisdiction: str | None
    keywords: list[str]
    seen: int = 0
    accepted: int = 0
    rejected: int = 0
    reason_codes: dict[str, int] = field(default_factory=dict)
    channel_missing: bool = False
    examples: list[str] = field(default_factory=list)

    @property
    def in_feed(self) -> bool:
        return self.seen > 0

    @property
    def accept_rate(self) -> float | None:
        return (self.accepted / self.seen) if self.seen else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "jurisdiction": self.jurisdiction,
            "keywords": self.keywords,
            "seen": self.seen,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "in_feed": self.in_feed,
            "accept_rate": self.accept_rate,
            "channel_missing": self.channel_missing,
            "reason_codes": self.reason_codes,
            "examples": self.examples[:3],
        }


@dataclass
class RecallReport:
    """Both ratios, the per-topic detail, and what the numbers are measured on."""

    topics: list[TopicResult] = field(default_factory=list)
    candidates_considered: int = 0
    window_days: int = 30
    since: datetime | None = None

    @property
    def measurable(self) -> list[TopicResult]:
        """Topics whose channel exists — the only ones recall can be read off."""
        return [t for t in self.topics if not t.channel_missing]

    @property
    def in_feed_rate(self) -> float | None:
        pool = self.measurable
        if not pool:
            return None
        return sum(1 for t in pool if t.in_feed) / len(pool)

    @property
    def accepted_rate(self) -> float | None:
        """Accepted over *in feed*, not over all topics.

        The denominator is the point: a subject the feeds never carried says
        nothing about the rubric, and folding it in would let a sourcing gap
        read as an editorial one.
        """
        pool = [t for t in self.measurable if t.in_feed]
        if not pool:
            return None
        return sum(1 for t in pool if t.accepted > 0) / len(pool)

    def as_dict(self) -> dict[str, Any]:
        return {
            "in_feed_rate": self.in_feed_rate,
            "accepted_rate": self.accepted_rate,
            "target_in_feed": TARGET_IN_FEED,
            "target_accepted": TARGET_ACCEPTED,
            "meets_in_feed": (
                self.in_feed_rate is not None and self.in_feed_rate >= TARGET_IN_FEED
            ),
            "meets_accepted": (
                self.accepted_rate is not None
                and self.accepted_rate >= TARGET_ACCEPTED
            ),
            "topics_total": len(self.topics),
            "topics_measurable": len(self.measurable),
            "channel_missing": len(self.topics) - len(self.measurable),
            "candidates_considered": self.candidates_considered,
            "window_days": self.window_days,
            "since": self.since.isoformat() if self.since else None,
            "topics": [t.as_dict() for t in self.topics],
        }


def _keywords(raw: Any) -> list[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        return []
    return [str(k).strip().lower() for k in parsed if str(k).strip()]


def matches(text: str, keywords: Sequence[str]) -> bool:
    """Case-insensitive keyword hit on a word boundary.

    The boundary matters: without it "crs" matches "concerns" and every topic
    reports itself in the feed.
    """
    lowered = (text or "").lower()
    for keyword in keywords:
        if not keyword:
            continue
        if re.search(rf"(?<![\w-]){re.escape(keyword)}", lowered):
            return True
    return False


def compute_recall(
    *,
    brand_id_fk: int,
    window_days: int = 30,
    now: datetime | None = None,
) -> RecallReport:
    """Both ratios over the candidates of the last ``window_days``.

    Counts every candidate the intake wrote, accepted and rejected alike:
    ``in_feed`` asks whether the subject arrived, and a rejected candidate
    arrived. ``accepted`` then reads the rubric's decision on what arrived.
    """
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, SeedTopic

    now = now or datetime.now(tz=UTC)
    since = now - timedelta(days=max(1, int(window_days)))

    with get_session_factory()() as session:
        seeds = (
            session.execute(
                select(SeedTopic).where(SeedTopic.brand_id_fk == brand_id_fk)
            )
            .scalars()
            .all()
        )
        rows = session.execute(
            select(
                Candidate.source_title,
                Candidate.source_summary,
                Candidate.verdict,
                Candidate.reason_code,
                Candidate.source_class,
            ).where(
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.created_at >= since.replace(tzinfo=None),
            )
        ).all()

    classes_present = {row[4] for row in rows if row[4]}
    report = RecallReport(
        candidates_considered=len(rows), window_days=window_days, since=since
    )
    for seed in seeds:
        keywords = _keywords(seed.keywords)
        result = TopicResult(
            topic=seed.topic,
            jurisdiction=seed.jurisdiction,
            keywords=keywords,
            # A subject whose only intended channel was never connected is not
            # a recall failure (NTS_119). Detected by the absence of the class
            # rather than by a hard-coded topic list, so connecting the feed
            # makes the flag disappear on its own.
            channel_missing=(
                seed.jurisdiction in MISSING_CHANNEL_JURISDICTIONS
                and MISSING_CHANNEL_SOURCE_CLASS not in classes_present
            ),
        )
        for title, summary, verdict, reason_code, _source_class in rows:
            if not matches(f"{title or ''} {summary or ''}", keywords):
                continue
            result.seen += 1
            if verdict == "accept":
                result.accepted += 1
                if len(result.examples) < 3:
                    result.examples.append((title or "")[:160])
            else:
                result.rejected += 1
                code = reason_code or "unknown"
                result.reason_codes[code] = result.reason_codes.get(code, 0) + 1
        report.topics.append(result)

    log.info(
        "recall.computed",
        brand_id=brand_id_fk,
        topics=len(report.topics),
        measurable=len(report.measurable),
        in_feed_rate=report.in_feed_rate,
        accepted_rate=report.accepted_rate,
        candidates=report.candidates_considered,
    )
    return report
