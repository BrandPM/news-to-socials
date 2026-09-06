"""``/api/v1/candidates`` — the Portfolio board's backend (NTS_098 §7, NTS_111).

What the screen above this needs, and why each piece is here rather than
computed in the browser:

* **The guard's `reason` on every row.** It is the instrument for proofreading
  the rubric (NTS_099 §4 is a draft until 50 verdicts have been read against
  it), so it travels with the list, not with the detail.
* **Counters split by status and by today's `reason_code`.** "143 отсеяно" is a
  number; "personnel 61 · forecast 40 · out_of_jurisdiction 22" is a finding.
  Computing that in the client would mean shipping every rejected row to it.
* **The four manual actions** from NTS_098 §7 — `promote / hold / reject /
  reset` — as *transitions*, not as a generic PATCH of `status`. A board that
  can set any status from the browser is a board that will eventually put a
  candidate into `published` without a Sanity document behind it.
* **Every action writes `review_decisions`.** NTS_113 calls that table the only
  free signal for tuning the rubric and the rank weights, and it is only free
  if writing it is not optional.

Concurrency: every transition is a compare-and-set on the status it expects,
the same rule as `candidate_store.claim_pending` (NTS_098 DoD 3). Two operators
on one board, or an operator and the TTL pass, race for real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from pipeline.admin.db import session_scope
from pipeline.admin.models import (
    Brand,
    BrandTaxonomy,
    Candidate,
    CostRecord,
    PipelineConfig,
    ReviewDecision,
    Source,
    SourceHealthRecord,
)
from pipeline.admin.schemas import (
    CandidateActionIn,
    CandidateCountsOut,
    CandidateDedupMatchOut,
    CandidateDetailOut,
    CandidateDocumentIn,
    CandidateOut,
    CandidateReturnIn,
    PortfolioSlotOut,
    PortfolioSummaryOut,
    ReviewDecisionIn,
    ReviewDecisionOut,
)

router = APIRouter()

# NTS_111 §Портфель: the board's columns, left to right. "Отсеяно сегодня" is
# folded (a healthy day produces a few accepts and hundreds of rejects), which
# is why the rejected column is counted per reason_code rather than listed.
BOARD_COLUMNS = (
    "rejected",
    "pending",
    "doc_missing",
    "in_production",
    "drafted",
    "ready",
    "published",
)

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# How far ahead the slot strip looks. Four weeks matches the Publications
# calendar (NTS_111 §Публикации) so the two screens cannot disagree.
_SLOT_HORIZON_DAYS = 28

# The manual actions from NTS_098 §7, as (allowed source statuses,
# target status, manual_action, review action). ``reset`` is the operator's
# escape hatch out of ``failed``: NTS_098 §2 asks for a manual reset to
# pending from the Portfolio, and without it a candidate that hit
# ``attempts >= 2`` is dead.
_TRANSITIONS: dict[str, dict[str, Any]] = {
    "promote": {
        "from": ("rejected", "pending", "doc_missing"),
        "to": "pending",
        "manual_action": "promoted",
        "review_action": "promote",
        "requires_comment": False,
    },
    "hold": {
        "from": ("pending", "doc_missing"),
        "to": "pending",
        "manual_action": "held",
        "review_action": "hold",
        "requires_comment": False,
    },
    "reject": {
        "from": ("pending", "doc_missing", "drafted", "returned", "ready"),
        "to": "rejected",
        "manual_action": "rejected",
        "review_action": "reject",
        # A rejection with no sentence is indistinguishable from a mis-click
        # when the row is read back a week later.
        "requires_comment": True,
    },
    "reset": {
        "from": ("failed", "expired", "superseded", "rejected"),
        "to": "pending",
        "manual_action": None,
        "review_action": "promote",
        "requires_comment": False,
    },
}


def _brand_or_404(session: Session, brand_id: int) -> Brand:
    brand: Brand | None = session.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="brand not found")
    return brand


def _candidate_or_404(
    session: Session, candidate_id: int, brand_id: int
) -> Candidate:
    row: Candidate | None = session.get(Candidate, candidate_id)
    if row is None or row.brand_id_fk != brand_id:
        # Same 404 for "absent" and "another brand's": a brand-scoped API must
        # not confirm the existence of another tenant's row.
        raise HTTPException(status_code=404, detail="candidate not found")
    return row


def _brand_today(
    session: Session, brand_id: int
) -> tuple[date_type, datetime, datetime, str]:
    """``(today, day_start_utc, day_end_utc, tz_name)`` in the brand's day.

    ``pipeline_config.brand_timezone`` is the authority, per the S1 decision —
    never ``brands.timezone``, which migration 020 only seeded it from.
    """
    from pipeline.selector.candidate_store import (
        brand_day_bounds,
        resolve_timezone,
    )

    cfg = session.get(PipelineConfig, brand_id)
    tz_name = getattr(cfg, "brand_timezone", None) or "UTC"
    now = datetime.now(tz=UTC)
    start, end = brand_day_bounds(now=now, timezone_name=tz_name)
    today = now.astimezone(resolve_timezone(tz_name)).date()
    return today, start, end, tz_name


# --- list + counters -------------------------------------------------------


@router.get("", response_model=list[CandidateOut])
def list_candidates(
    brand_id: int,
    status: str | None = None,
    reason_code: str | None = None,
    input_kind: str | None = None,
    service_category: str | None = None,
    cap_overflow: bool | None = None,
    q: str | None = None,
    today_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CandidateOut]:
    """Board column / filtered list. Newest first.

    ``status`` accepts a comma-separated list so one request fills one column
    (and `?status=pending,doc_missing` fills the navigation counter).
    ``today_only`` bounds to the brand's current day — the rejected column,
    which is otherwise unbounded.
    """
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        stmt = select(Candidate).where(Candidate.brand_id_fk == brand_id)
        if status:
            wanted = [s.strip() for s in status.split(",") if s.strip()]
            stmt = stmt.where(Candidate.status.in_(wanted))
        if reason_code:
            wanted = [s.strip() for s in reason_code.split(",") if s.strip()]
            stmt = stmt.where(Candidate.reason_code.in_(wanted))
        if input_kind:
            stmt = stmt.where(Candidate.input_kind == input_kind)
        if service_category:
            stmt = stmt.where(Candidate.service_category == service_category)
        if cap_overflow is not None:
            stmt = stmt.where(Candidate.cap_overflow.is_(cap_overflow))
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(Candidate.source_title.ilike(like))
        if today_only:
            _today, start, end, _tz = _brand_today(session, brand_id)
            stmt = stmt.where(
                Candidate.created_at >= start, Candidate.created_at < end
            )
        stmt = stmt.order_by(Candidate.created_at.desc(), Candidate.id.desc())
        stmt = stmt.limit(limit).offset(offset)
        return [CandidateOut.model_validate(r) for r in session.scalars(stmt)]


@router.get("/counts", response_model=CandidateCountsOut)
def candidate_counts(brand_id: int) -> CandidateCountsOut:
    """Board counters, today's reject distribution, and the six-section
    navigation counters (NTS_111 §Навигация)."""
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        _today, start, end, _tz = _brand_today(session, brand_id)

        by_status = {
            row[0]: int(row[1])
            for row in session.execute(
                select(Candidate.status, func.count(Candidate.id))
                .where(Candidate.brand_id_fk == brand_id)
                .group_by(Candidate.status)
            )
        }
        by_reason_today = {
            row[0]: int(row[1])
            for row in session.execute(
                select(Candidate.reason_code, func.count(Candidate.id))
                .where(
                    Candidate.brand_id_fk == brand_id,
                    Candidate.verdict == "reject",
                    Candidate.created_at >= start,
                    Candidate.created_at < end,
                )
                .group_by(Candidate.reason_code)
            )
        }
        accepted_today = int(
            session.execute(
                select(func.count(Candidate.id)).where(
                    Candidate.brand_id_fk == brand_id,
                    Candidate.verdict == "accept",
                    Candidate.created_at >= start,
                    Candidate.created_at < end,
                )
            ).scalar()
            or 0
        )
        cap_overflow_today = int(
            session.execute(
                select(func.count(Candidate.id)).where(
                    Candidate.brand_id_fk == brand_id,
                    Candidate.cap_overflow.is_(True),
                    Candidate.created_at >= start,
                    Candidate.created_at < end,
                )
            ).scalar()
            or 0
        )
        # NTS_111 §Источники counter: unhealthy = a source whose last three
        # fetches all failed (NTS_106 §1, "источник unhealthy после 3 подряд").
        unhealthy = 0
        for source in session.scalars(
            select(Source).where(
                Source.brand_id_fk == brand_id, Source.active.is_(True)
            )
        ):
            recent = list(
                session.scalars(
                    select(SourceHealthRecord.success)
                    .where(SourceHealthRecord.source_id == source.id)
                    .order_by(SourceHealthRecord.fetched_at.desc())
                    .limit(3)
                )
            )
            if len(recent) == 3 and not any(recent):
                unhealthy += 1

        return CandidateCountsOut(
            by_status=by_status,
            by_reason_code_today=by_reason_today,
            rejected_today=sum(by_reason_today.values()),
            accepted_today=accepted_today,
            cap_overflow_today=cap_overflow_today,
            nav_portfolio=by_status.get("pending", 0)
            + by_status.get("doc_missing", 0),
            nav_review=by_status.get("drafted", 0) + by_status.get("returned", 0),
            nav_ready=by_status.get("ready", 0),
            nav_sources_unhealthy=unhealthy,
        )


def next_slots(
    *,
    slots_config: Any,
    today: date_type,
    horizon_days: int = _SLOT_HORIZON_DAYS,
) -> list[tuple[date_type, str, int]]:
    """The next publication slot dates from `publication_slots`.

    Pure and separately tested (midnight and DST) because it is the only
    date arithmetic on this screen, and NTS_098 §5 makes the slot date the one
    timezone-dependent calculation in the system. ``today`` is already the
    brand's day — resolving the zone is the caller's job, so this function
    cannot get it wrong in a way a test would not see.
    """
    parsed: list[tuple[str, int]] = []
    try:
        for entry in list(slots_config or []):
            day = str(entry.get("day", "")).strip().lower()
            capacity = int(entry.get("capacity", 0))
            if day in _WEEKDAYS and capacity > 0:
                parsed.append((day, capacity))
    except (AttributeError, TypeError, ValueError):
        return []
    if not parsed:
        return []
    # dict() also means a repeated day in the config keeps the LAST capacity
    # rather than raising — the config surface is hand-editable.
    wanted = dict(parsed)
    out: list[tuple[date_type, str, int]] = []
    for offset in range(horizon_days):
        day_date = today + timedelta(days=offset)
        name = _WEEKDAYS[day_date.weekday()]
        if name in wanted:
            out.append((day_date, name, wanted[name]))
    return out


@router.get("/summary", response_model=PortfolioSummaryOut)
def portfolio_summary(brand_id: int) -> PortfolioSummaryOut:
    """The strip above the board: slot capacity and the month's spend.

    Spend is the sum of `cost_records` for the calendar month against
    `monthly_spend_cap_usd` (NTS_106 §3). The kill-switch itself is S7 — this
    is the number, which is what NTS_111 §Портфель asks for.
    """
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        cfg = session.get(PipelineConfig, brand_id)
        today, _start, _end, tz_name = _brand_today(session, brand_id)

        slots_config: Any = []
        if cfg is not None and getattr(cfg, "publication_slots", None):
            try:
                slots_config = json.loads(cfg.publication_slots)
            except (ValueError, TypeError):
                slots_config = []
        raw_slots = next_slots(slots_config=slots_config, today=today)

        filled: dict[date_type, int] = {}
        if raw_slots:
            horizon_end = raw_slots[-1][0]
            for row in session.execute(
                select(Candidate.publication_slot, func.count(Candidate.id))
                .where(
                    Candidate.brand_id_fk == brand_id,
                    Candidate.publication_slot.is_not(None),
                    Candidate.publication_slot >= today,
                    Candidate.publication_slot <= horizon_end,
                    Candidate.status.in_(("ready", "published")),
                )
                .group_by(Candidate.publication_slot)
            ):
                filled[row[0]] = int(row[1])

        month_start = today.replace(day=1)
        spend = float(
            session.execute(
                select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)).where(
                    CostRecord.brand_id_fk == brand_id,
                    CostRecord.created_at
                    >= datetime(
                        month_start.year, month_start.month, 1, tzinfo=UTC
                    ),
                )
            ).scalar()
            or 0.0
        )
        cap = float(getattr(cfg, "monthly_spend_cap_usd", 0.0) or 0.0)

        return PortfolioSummaryOut(
            slots=[
                PortfolioSlotOut(
                    date=slot_date,
                    day=day,
                    capacity=capacity,
                    filled=filled.get(slot_date, 0),
                )
                for slot_date, day, capacity in raw_slots
            ],
            month_spend_usd=round(spend, 4),
            month_cap_usd=cap,
            month_spend_pct=round(spend / cap * 100.0, 1) if cap > 0 else 0.0,
            brand_timezone=tz_name,
            today=today,
        )


# --- detail ---------------------------------------------------------------


@router.get("/{candidate_id}", response_model=CandidateDetailOut)
def candidate_detail(candidate_id: int, brand_id: int) -> CandidateDetailOut:
    """The side panel: the card, the service label, the dedup history, the
    decision log, and anything this candidate superseded."""
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        row = _candidate_or_404(session, candidate_id, brand_id)

        service_label: str | None = None
        if row.service_category:
            taxonomy = session.scalars(
                select(BrandTaxonomy).where(
                    BrandTaxonomy.brand_id_fk == brand_id,
                    BrandTaxonomy.key == row.service_category,
                )
            ).first()
            service_label = taxonomy.label if taxonomy is not None else None

        decisions = [
            ReviewDecisionOut.model_validate(d)
            for d in session.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.candidate_id_fk == candidate_id)
                .order_by(ReviewDecision.at.desc())
            )
        ]
        superseded_by = [
            int(r)
            for r in session.scalars(
                select(Candidate.id).where(
                    Candidate.supersedes_id == candidate_id
                )
            )
        ]

        return CandidateDetailOut(
            candidate=CandidateOut.model_validate(row),
            service_label=service_label,
            dedup_matches=_dedup_matches(session, row),
            review_decisions=decisions,
            superseded_by=superseded_by,
        )


def _dedup_matches(session: Session, row: Candidate) -> list[CandidateDedupMatchOut]:
    """«похоже на кандидата #412, 0.91» (NTS_111 §Портфель).

    Recomputed on demand rather than stored: the intake's dedup decision is a
    *drop* (no row survives to hang the similarity off), so the only way to
    show the operator what this candidate resembles is to compare it now,
    against the windows as they stand. Empty when the embedding is missing —
    honest, and cheap: this is one panel, not a list.
    """
    import numpy as np

    from pipeline.admin.models import CANDIDATE_LIVE_STATUSES, TopicEmbedding
    from pipeline.selector.candidate_dedup import (
        CandidateDedupConfig,
        _best_match,
        _load_window,
    )

    if not row.topic_embedding_ref:
        return []
    blob = session.scalars(
        select(TopicEmbedding.embedding).where(
            TopicEmbedding.topic_id == row.topic_embedding_ref
        )
    ).first()
    if not blob:
        return []
    embedding = np.frombuffer(blob, dtype=np.float32)

    cfg = session.get(PipelineConfig, row.brand_id_fk)
    config = CandidateDedupConfig.from_config(_config_view(cfg))
    out: list[CandidateDedupMatchOut] = []
    windows = (
        ("live", tuple(CANDIDATE_LIVE_STATUSES), config.threshold_live),
        ("published", ("published",), config.threshold_published),
        ("rejected", ("rejected",), config.threshold_rejected),
    )
    for name, statuses, threshold in windows:
        items = [
            item
            for item in _load_window(
                brand_id_fk=row.brand_id_fk,
                statuses=statuses,
                since=None,
                since_column="created_at",
            )
            if item.candidate_id != row.id
        ]
        match, similarity = _best_match(embedding, items)
        # Report from the *yellow* floor down, not only above the threshold:
        # the panel exists so the operator can see a near-miss the pipeline
        # decided to keep, which is exactly the calibration signal NTS_079
        # logs but nothing displays.
        if match is None or similarity < min(0.75, threshold):
            continue
        title = session.scalars(
            select(Candidate.source_title).where(
                Candidate.id == match.candidate_id
            )
        ).first()
        out.append(
            CandidateDedupMatchOut(
                candidate_id=match.candidate_id,
                similarity=round(float(similarity), 4),
                window=name,
                status=match.status,
                title=title or "—",
            )
        )
    return out


def _config_view(cfg: PipelineConfig | None) -> Any:
    """A stand-in with the four dedup attributes `CandidateDedupConfig` reads.

    The ORM row carries them already; this exists so a brand with no config
    row still gets the documented defaults instead of an AttributeError in a
    read-only panel.
    """
    if cfg is not None:
        return cfg

    class _Defaults:
        pass

    return _Defaults()


# --- manual actions -------------------------------------------------------


@router.post("/{candidate_id}/action", response_model=CandidateOut)
def candidate_action(
    candidate_id: int, brand_id: int, payload: CandidateActionIn
) -> CandidateOut:
    """`promote / hold / reject / reset` (NTS_098 §7).

    Each is a compare-and-set on the statuses it is legal from, so a stale
    board cannot move a candidate that has since gone into production. A
    refused transition is a 409 naming the current status — the browser then
    knows to refetch rather than to retry.
    """
    spec = _TRANSITIONS[payload.action]
    comment = (payload.comment or "").strip() or None
    if spec["requires_comment"] and not comment:
        raise HTTPException(
            status_code=422,
            detail=f"action {payload.action!r} requires a comment",
        )

    now = datetime.now(tz=UTC)
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        row = _candidate_or_404(session, candidate_id, brand_id)
        current = row.status

        values: dict[str, Any] = {
            "status": spec["to"],
            "manual_action": spec["manual_action"],
            "manual_by": payload.reviewer,
            "manual_at": now,
        }
        if payload.action == "promote":
            # NTS_099 §5: a cap_overflow reject promoted by hand becomes a real
            # accept. Leaving verdict='reject' would keep it out of every
            # accepted-candidate query, including the guard's own
            # recent_accepted_titles — the promotion would not stick.
            values["verdict"] = "accept"
            values["cap_overflow"] = False
            if row.reason_code == "daily_cap":
                values["reason_code"] = "ok"
        if payload.action == "reset":
            # A reset is a second chance, so the attempt counter goes with it;
            # otherwise the candidate re-fails on its first try (max_attempts).
            values["attempts"] = 0
            values["last_error"] = None
            values["failed_at"] = None
            values["verdict"] = "accept"
            if row.reason_code != "ok":
                values["reason_code"] = "ok"
        if payload.action == "reject":
            values["verdict"] = "reject"
            values["reason_code"] = "out_of_scope"
            values["reason"] = (comment or "manual reject")[:200]

        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.status.in_(tuple(spec["from"])),
            )
            .values(**values)
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot {payload.action} a candidate in status "
                    f"{current!r} — allowed from {list(spec['from'])}"
                ),
            )
        session.add(
            ReviewDecision(
                brand_id_fk=brand_id,
                candidate_id_fk=candidate_id,
                action=spec["review_action"],
                scope=payload.action,
                comment=comment,
                reviewer=payload.reviewer,
                time_spent_s=payload.time_spent_s,
                at=now,
            )
        )
        session.flush()
        session.refresh(row)
        return CandidateOut.model_validate(row)


@router.put("/{candidate_id}/document", response_model=CandidateOut)
def set_candidate_document(
    candidate_id: int, brand_id: int, payload: CandidateDocumentIn
) -> CandidateOut:
    """Manual document link (NTS_111 §Портфель).

    The operator can always beat the fetcher to a document, and until S5 there
    is no fetcher for several source classes at all. A candidate sitting in
    `doc_missing` returns to `pending` when a link arrives — that is the whole
    point of the field.
    """
    now = datetime.now(tz=UTC)
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        row = _candidate_or_404(session, candidate_id, brand_id)
        row.primary_doc_url = str(payload.primary_doc_url)
        # ``manual`` distinguishes an operator's link from the two automatic
        # paths NTS_101 will write here, so the document-find share stays an
        # honest measure of the fetcher rather than of the operator.
        row.doc_match = "manual"
        row.manual_by = payload.reviewer
        row.manual_at = now
        if row.status == "doc_missing":
            row.status = "pending"
            row.last_error = None
        # Deliberately NOT a ``review_decisions`` row. That table's vocabulary
        # (NTS_107: approve/return/reject/hold/promote/disagree_guard) is
        # editorial decisions, and pasting a URL is data entry — filing it
        # under "hold" would put a non-decision into the one dataset NTS_113
        # reads to tune the rubric. Who and when are on the candidate itself.
        session.flush()
        session.refresh(row)
        return CandidateOut.model_validate(row)


# --- review decisions -----------------------------------------------------


@router.post(
    "/review-decisions", response_model=ReviewDecisionOut, status_code=201
)
def create_review_decision(
    brand_id: int, payload: ReviewDecisionIn
) -> ReviewDecisionOut:
    """Write one `review_decisions` row — the disagree-with-the-verdict
    action included.

    NTS_111 §Портфель routes the disagree button here with
    `action=disagree_guard`. A comment is required for that action
    specifically: a disagreement with no reason cannot be read back into a
    rubric edit, which is the only reason the button exists.
    """
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        _candidate_or_404(session, payload.candidate_id, brand_id)
        comment = (payload.comment or "").strip() or None
        if payload.action == "disagree_guard" and not comment:
            raise HTTPException(
                status_code=422,
                detail=(
                    "disagree_guard requires a comment — a disagreement with "
                    "no reason cannot be read back into a rubric edit"
                ),
            )
        row = ReviewDecision(
            brand_id_fk=brand_id,
            candidate_id_fk=payload.candidate_id,
            action=payload.action,
            scope=payload.scope,
            comment=comment,
            reviewer=payload.reviewer,
            time_spent_s=payload.time_spent_s,
            at=datetime.now(tz=UTC),
        )
        session.add(row)
        session.flush()
        return ReviewDecisionOut.model_validate(row)


@router.get("/review-decisions/list", response_model=list[ReviewDecisionOut])
def list_review_decisions(
    brand_id: int,
    action: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ReviewDecisionOut]:
    """The decision log, newest first. `?action=disagree_guard` is the rubric
    review's reading list."""
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        stmt = select(ReviewDecision).where(ReviewDecision.brand_id_fk == brand_id)
        if action:
            stmt = stmt.where(ReviewDecision.action == action)
        stmt = stmt.order_by(ReviewDecision.at.desc()).limit(limit)
        return [ReviewDecisionOut.model_validate(r) for r in session.scalars(stmt)]


@router.get("/recall/report")
def recall_report(
    brand_id: int,
    window_days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """The two acceptance ratios of NTS_114, over the accumulated candidates.

    ``in_feed`` and ``accepted/in_feed`` (NTS_099 §7). This is what replaced
    the shadow week's hand-marking after Andriy lifted that gate on
    2026-09-06: the same measurement, recomputed on every open, so it tracks
    the portfolio instead of freezing into a document.

    Computed live rather than cached. It is a scan over one brand's candidates
    with a keyword match — cheap next to being wrong about how recall stands.
    """
    from pipeline.selector.recall import compute_recall

    with session_scope() as session:
        _brand_or_404(session, brand_id)
    return compute_recall(brand_id_fk=brand_id, window_days=window_days).as_dict()


@router.get("/{candidate_id}/traceability")
def candidate_traceability(candidate_id: int, brand_id: int) -> dict[str, Any]:
    """Everything the article was made of, without one new paid call.

    NTS_096 part B — the block the editor opens on the review card: the primary
    document with its ``as_of``, the fact pack behind it, the plan the article
    was written from and the attribution verdicts it was checked against. The
    DoD line is "полная трассировка любой статьи собирается **без единого
    нового платного вызова**", which is why every field here is a read of
    something the run already stored (migrations 025/027/028).
    """
    from pipeline.admin.models import DocumentVersion, FactPack

    with session_scope() as session:
        _brand_or_404(session, brand_id)
        candidate = session.get(Candidate, candidate_id)
        if candidate is None or candidate.brand_id_fk != brand_id:
            raise HTTPException(status_code=404, detail="candidate not found")

        pack_row = (
            session.execute(
                select(FactPack)
                .where(FactPack.candidate_id_fk == candidate_id)
                .order_by(FactPack.created_at.desc(), FactPack.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        document = None
        if candidate.doc_version_id:
            try:
                document = session.get(DocumentVersion, int(candidate.doc_version_id))
            except (TypeError, ValueError):
                document = None

        decisions = [
            {
                "action": d.action,
                "scope": d.scope,
                "comment": d.comment,
                "reviewer": d.reviewer,
                "time_spent_s": d.time_spent_s,
                "at": d.at.isoformat(),
            }
            for d in session.execute(
                select(ReviewDecision)
                .where(ReviewDecision.candidate_id_fk == candidate_id)
                .order_by(ReviewDecision.at.desc())
            )
            .scalars()
            .all()
        ]
        # Everything is read INSIDE the session. The first version of this
        # endpoint built its dict after the block closed, and every attribute
        # access then raised DetachedInstanceError — an error the endpoint's
        # own shape made invisible until a test opened a candidate with no
        # fact pack.
        snapshot = {
            "status": candidate.status,
            "needs_attention": bool(candidate.needs_attention),
            "canon_dirty": bool(candidate.canon_dirty),
            "depth_prior": candidate.depth_prior,
            "depth_final": candidate.depth_final,
            "return_scope": candidate.return_scope,
            "primary_doc_url": candidate.primary_doc_url,
            "doc_match": candidate.doc_match,
            "doc_sections_used": candidate.doc_sections_used,
        }
        document_row = (
            {
                "url": document.url,
                "title": document.title,
                "as_of": document.fetched_at.isoformat(),
                "language": document.doc_language,
                "section_count": document.section_count,
            }
            if document is not None
            else None
        )
        pack = (
            {
                "pack": pack_row.pack,
                "plan": pack_row.plan,
                "attribution": pack_row.attribution,
                "sources": pack_row.sources,
            }
            if pack_row is not None
            else None
        )

    def _json(raw: Any, default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    return {
        "candidate_id": candidate_id,
        "status": snapshot["status"],
        "needs_attention": snapshot["needs_attention"],
        "canon_dirty": snapshot["canon_dirty"],
        "depth_prior": snapshot["depth_prior"],
        "depth_final": snapshot["depth_final"],
        "return_scope": snapshot["return_scope"],
        "document": (
            {
                **document_row,
                "sections_used": _json(snapshot["doc_sections_used"], []),
                "sections_total": document_row["section_count"],
                "doc_match": snapshot["doc_match"],
            }
            if document_row is not None
            else (
                {
                    "url": snapshot["primary_doc_url"],
                    "doc_match": snapshot["doc_match"],
                    "sections_used": _json(snapshot["doc_sections_used"], []),
                }
                if snapshot["primary_doc_url"]
                else None
            )
        ),
        "fact_pack": _json(pack["pack"], None) if pack else None,
        "plan": _json(pack["plan"], None) if pack else None,
        "attribution": _json(pack["attribution"], None) if pack else None,
        "sources": _json(pack["sources"], []) if pack else [],
        "history": decisions,
    }


@router.post("/{candidate_id}/return", response_model=CandidateOut)
def candidate_return(
    candidate_id: int, brand_id: int, payload: CandidateReturnIn
) -> CandidateOut:
    """Send an article back to one stage (NTS_100 §5, NTS_107).

    The scope is stored on the candidate, which is what makes the next
    production run cheap: ``translation:uk`` re-runs the UK translation and
    nothing before it, so one complaint costs one stage rather than a whole
    article. The decision row carries the same scope, because the review log is
    the only dataset for tuning the rubric (NTS_113).

    **The slot is released.** A Monday slot held by something nobody approved
    makes the calendar lie — the same rule ``unreject`` follows.
    """
    now = datetime.now(tz=UTC)
    with session_scope() as session:
        _brand_or_404(session, brand_id)
        row = _candidate_or_404(session, candidate_id, brand_id)
        if row.status not in ("drafted", "ready", "returned"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"candidate is {row.status!r}; a return is only meaningful "
                    "from drafted / ready / returned"
                ),
            )
        row.status = "returned"
        row.return_scope = payload.scope
        row.publication_slot = None
        row.manual_by = payload.reviewer
        row.manual_at = now
        session.add(
            ReviewDecision(
                brand_id_fk=brand_id,
                candidate_id_fk=candidate_id,
                action="return",
                scope=payload.scope,
                comment=payload.comment,
                reviewer=payload.reviewer,
                time_spent_s=payload.time_spent_s,
                at=now,
            )
        )
        session.flush()
        return CandidateOut.model_validate(row)
