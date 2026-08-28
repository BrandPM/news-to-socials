"""Writes and claims against ``candidates`` (NTS_098 §1, §2, §4; NTS_099 §5).

Everything that touches a candidate row from the intake side lives here, so the
intake orchestrator stays readable and the two rules that are easy to get wrong
have one implementation each:

**The daily cap is per ``input_kind`` and counted in the brand's day**
(NTS_099 §5: document 2, news 1). Counting in UTC would move the boundary by an
hour or two twice a year and make the cap silently wrong in exactly the weeks
DST changes. Over-cap items are **not discarded**: they are stored as rejects
with ``reason_code='daily_cap'`` and ``cap_overflow=1`` so a manager can promote
one the same day from the Portfolio screen.

**``pending → selected`` is a compare-and-set** (NTS_098 §2, DoD 3):
``UPDATE … WHERE status='pending'`` and the caller believes the rowcount, not a
prior read. Two production runs overlapping — a cron firing while the operator
presses "Run now" — is not exotic, and the failure mode is two runs generating
the same article and paying twice for it. The read-then-write version passes
every single-threaded test there is.

``expires_at`` is set at creation from ``candidate_ttl_days`` keyed by
``event_stage``, because a candidate with no expiry is a candidate that lives
forever if the TTL pass (S4) is ever off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CandidateInput:
    """The snapshot + verdict needed to write one candidate row."""

    brand_id_fk: int
    input_kind: str
    source_id_fk: int | None
    source_title: str
    source_summary: str | None
    source_url: str | None
    source_published_at: datetime | None
    source_language: str | None
    source_name: str | None
    source_class: str | None
    topic_embedding_ref: str | None
    verdict: str
    reason_code: str
    reason: str
    confidence: float | None = None
    service_category: str | None = None
    jurisdictions: tuple[str, ...] = ()
    event_stage: str | None = None
    depth_prior: str | None = None
    primary_doc_hint: str | None = None
    primary_doc_url: str | None = None
    doc_language_expected: str | None = None
    cap_overflow: bool = False
    supersedes_id: int | None = None


def resolve_timezone(name: str | None) -> ZoneInfo:
    """``ZoneInfo`` for the brand, UTC when the name is unusable.

    A bad zone must not stop an intake: the cap boundary being wrong by an hour
    is a smaller problem than not judging the feed at all. The API rejects
    unknown zones on save (``PipelineConfigUpdate._known_timezone``), so this
    path only fires for a value written before that validator existed.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("candidate_store.unknown_timezone", tz=name)
        return ZoneInfo("UTC")


def brand_day_bounds(
    *, now: datetime, timezone_name: str | None
) -> tuple[datetime, datetime]:
    """UTC bounds of "today" in the brand's timezone (NTS_098 §5)."""
    tz = resolve_timezone(timezone_name)
    local = now.astimezone(tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(UTC),
        end_local.astimezone(UTC),
    )


def ttl_for_stage(ttl_config: Any, event_stage: str | None) -> int:
    """Days from ``candidate_ttl_days``; the ``default`` entry, else 14."""
    try:
        mapping = dict(ttl_config or {})
    except (TypeError, ValueError):
        mapping = {}
    if event_stage and event_stage in mapping:
        return int(mapping[event_stage])
    return int(mapping.get("default", 14))


def count_accepted_today(
    *,
    brand_id_fk: int,
    input_kind: str,
    now: datetime,
    timezone_name: str | None,
) -> int:
    """Accepted candidates created in the brand's current day, this input kind.

    Counts rows with ``verdict='accept'`` regardless of what happened to them
    since: the cap limits how much the guard admits per day, and a candidate
    that has already moved to ``drafted`` still consumed that day's allowance.
    """
    from sqlalchemy import func, select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    start, end = brand_day_bounds(now=now, timezone_name=timezone_name)
    with get_session_factory()() as session:
        return int(
            session.execute(
                select(func.count(Candidate.id)).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.input_kind == input_kind,
                    Candidate.verdict == "accept",
                    Candidate.created_at >= start,
                    Candidate.created_at < end,
                )
            ).scalar()
            or 0
        )


def recent_accepted_titles(
    *, brand_id_fk: int, limit: int = 20
) -> tuple[str, ...]:
    """The most recent accepted titles, for the rubric's ``{recent_accepted_titles}``.

    This is what lets the guard return ``duplicate_stage`` on its own — it can
    see what is already in the portfolio (NTS_099 §2).
    """
    try:
        from sqlalchemy import select

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import Candidate

        with get_session_factory()() as session:
            rows = session.execute(
                select(Candidate.source_title)
                .where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.verdict == "accept",
                )
                .order_by(Candidate.created_at.desc())
                .limit(limit)
            ).all()
        return tuple(r[0] for r in rows if r[0])
    # The rubric renders without the recent-titles block; it must not be the
    # reason an intake does not run.
    except Exception as exc:
        log.warning("candidate_store.recent_titles_failed", err=str(exc))
        return ()


def create_candidate(
    payload: CandidateInput,
    *,
    ttl_config: Any = None,
    now: datetime | None = None,
) -> int:
    """Insert one candidate row and return its id.

    ``status`` follows the verdict: an accept lands ``pending`` (the selector's
    input, NTS_098 §2), a reject lands ``rejected`` and is kept for
    ``retention_days_rejected`` — the reject distribution is the only evidence
    the rubric is right, so rejects are stored, not counted and dropped.
    """
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    accepted = payload.verdict == "accept"
    status = "pending" if accepted else "rejected"
    expires_at = now + timedelta(
        days=ttl_for_stage(ttl_config, payload.event_stage)
    )

    with get_session_factory()() as session:
        row = Candidate(
            brand_id_fk=payload.brand_id_fk,
            input_kind=payload.input_kind,
            source_id_fk=payload.source_id_fk,
            source_title=payload.source_title,
            source_summary=payload.source_summary,
            source_url=payload.source_url,
            source_published_at=payload.source_published_at,
            source_language=payload.source_language,
            source_name=payload.source_name,
            source_class=payload.source_class,
            topic_embedding_ref=payload.topic_embedding_ref,
            verdict=payload.verdict,
            reason_code=payload.reason_code,
            reason=payload.reason[:200],
            confidence=payload.confidence,
            service_category=payload.service_category,
            jurisdictions=(
                json.dumps(list(payload.jurisdictions))
                if payload.jurisdictions
                else None
            ),
            event_stage=payload.event_stage,
            depth_prior=payload.depth_prior,
            primary_doc_hint=payload.primary_doc_hint,
            primary_doc_url=payload.primary_doc_url,
            doc_language_expected=payload.doc_language_expected,
            status=status,
            cap_overflow=payload.cap_overflow,
            supersedes_id=payload.supersedes_id,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def claim_pending(candidate_id: int, *, now: datetime | None = None) -> bool:
    """``pending → selected``, atomically. ``True`` if this caller won.

    NTS_098 §2/DoD 3. The whole guarantee is in the ``WHERE status='pending'``
    plus trusting the rowcount: SQLite serialises the write, so of two
    concurrent claimants exactly one sees ``rowcount == 1`` and the other sees
    0. A read-then-write implementation would hand the same candidate to both.
    """
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    with get_session_factory()() as session:
        result = session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id, Candidate.status == "pending")
            .values(status="selected", selected_at=now)
        )
        session.commit()
        # ``rowcount`` IS the guarantee — an UPDATE yields a CursorResult, but
        # session.execute() is annotated as the base Result, which does not
        # declare it.
        return bool(result.rowcount)  # type: ignore[attr-defined]


def mark_superseded(candidate_id: int) -> bool:
    """Retire a predecessor when a later stage of its event arrives.

    Only from ``pending``/``doc_missing`` (NTS_098 §2): once a candidate is in
    production or with the editor there is work attached to it, and silently
    retiring that is how a draft becomes an orphan.
    """
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    with get_session_factory()() as session:
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.status.in_(("pending", "doc_missing")),
            )
            .values(status="superseded")
        )
        session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]
