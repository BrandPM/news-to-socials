"""The candidate's second half: draft link, slot, publication (NTS_098 §2).

``candidate_store`` owns everything up to ``pending``. This module owns
everything after it, and exists because on 2026-08-28 none of it existed at
all: ``candidates.sanity_draft_id`` was filled on 0 of 337 production rows and
``draft_approvals.candidate_id_fk`` on 0 of 137. NTS_098 §1 declares that link;
nothing wrote it. Without it the status ``published`` is unreachable by
definition, and the whole contour ends at ``drafted``.

Three rules are enforced here rather than at the call sites, because each one
is the kind that gets re-implemented slightly differently the second time:

**The link is one transaction.** ``candidates.sanity_draft_id`` and
``draft_approvals.candidate_id_fk`` are written in the same session and
committed once. Half a link is worse than none: a candidate reading ``drafted``
with no approval row is missing from the review queue and from the publish gate
simultaneously, and nothing in the UI would say so.

**The slot is assigned on the move to ``ready``**, in ``brand_timezone``, and
capacity is counted against candidates already holding that date. The only
timezone-dependent arithmetic in the system is this date (NTS_098 §5), which is
why :func:`next_publication_slot` is a pure function with midnight and DST
tests rather than a query.

**``published`` is set only against a non-empty
``draft_approvals.published_at``** (NTS_098 §2). Not on an approve click, not
on a 200 from Sanity's mutate — on the recorded stamp. An approve whose Sanity
promote failed leaves an ``approved`` row with ``published_at`` NULL, and a
candidate that called itself published there would be a lie the calendar then
repeats.

Every transition is a compare-and-set on the statuses it is legal from, for the
reason spelled out in ``candidate_store.claim_pending``: two production runs
overlapping is ordinary, and the read-then-write version passes every
single-threaded test there is.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..common.logging import get_logger
from .candidate_store import resolve_timezone

log = get_logger(__name__)

# NTS_098 §2 — ``in_production --ok--> drafted``. ``selected`` is admitted
# because a single-pass production run may never write an intermediate
# ``in_production``, and ``returned --regen--> in_production`` comes back
# through the same door. ``drafted`` is admitted so a re-link with the same
# draft id is idempotent rather than a 409 in the middle of a retry.
LINKABLE_FROM: tuple[str, ...] = (
    "selected",
    "in_production",
    "returned",
    "drafted",
)

# ``drafted --editor approve--> ready``; ``returned`` gets a slot when the
# editor accepts the regenerated draft.
SLOTTABLE_FROM: tuple[str, ...] = ("drafted", "returned")

# ``ready --slot--> published``. ``drafted`` is admitted because the current
# review screen approves and publishes in one click (S7 splits them), and the
# candidate must not be left behind its own article.
PUBLISHABLE_FROM: tuple[str, ...] = ("ready", "drafted")

_WEEKDAYS: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

# How far ahead :func:`next_publication_slot` will look before giving up. Eight
# weeks of fully booked slots is not a scheduling problem, it is a backlog
# alert — returning None makes it visible instead of silently landing the
# candidate in the next quarter.
_SLOT_SEARCH_DAYS = 56


# --------------------------------------------------------------------------
# 1. the draft link — both sides, one transaction
# --------------------------------------------------------------------------


def _attach_approval(
    session: Any, *, sanity_draft_id: str, brand_id_fk: int, candidate_id: int
) -> None:
    """Point the approval row for this (draft, brand) at the candidate.

    Adopts an existing row rather than inserting a second one: a regenerated or
    previously rejected draft already has one, and
    ``uq_draft_approvals_draft_brand`` would reject the duplicate. Adopting
    deliberately does **not** touch ``status`` — linking is bookkeeping, not a
    re-decision.
    """
    from sqlalchemy import select

    from pipeline.admin.models import DraftApproval

    row = session.execute(
        select(DraftApproval).where(
            DraftApproval.sanity_draft_id == sanity_draft_id,
            DraftApproval.brand_id_fk == brand_id_fk,
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            DraftApproval(
                sanity_draft_id=sanity_draft_id,
                brand_id_fk=brand_id_fk,
                # 'draft' is the "no decision yet" state the model documents:
                # the draft exists and is waiting for the editor.
                status="draft",
                candidate_id_fk=candidate_id,
            )
        )
        return
    row.candidate_id_fk = candidate_id


def link_candidate_to_draft(
    *,
    candidate_id: int,
    sanity_draft_id: str,
    brand_id_fk: int,
    now: datetime | None = None,
) -> bool:
    """Bind candidate and Sanity draft to each other. ``True`` if it stuck.

    Called at the moment the draft is created, which is the only moment both
    ids are known and nothing has been paid for twice yet. Returns ``False``
    (and writes nothing) when the candidate is not in a status a draft can
    exist for — a silent accept there would hide a bug in the selector behind a
    plausible-looking row.
    """
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    with get_session_factory()() as session:
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.status.in_(LINKABLE_FROM),
            )
            .values(
                sanity_draft_id=sanity_draft_id,
                status="drafted",
                drafted_at=now,
                last_error=None,
            )
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            session.rollback()
            log.warning(
                "candidate_lifecycle.link_refused",
                candidate_id=candidate_id,
                draft_id=sanity_draft_id,
                allowed_from=LINKABLE_FROM,
            )
            return False
        # Same session, same commit — see the module docstring. If this raises,
        # the candidate side goes back with it.
        _attach_approval(
            session,
            sanity_draft_id=sanity_draft_id,
            brand_id_fk=brand_id_fk,
            candidate_id=candidate_id,
        )
        session.commit()
    log.info(
        "candidate_lifecycle.linked",
        candidate_id=candidate_id,
        draft_id=sanity_draft_id,
    )
    return True


def candidate_for_draft(
    sanity_draft_id: str, brand_id_fk: int
) -> int | None:
    """The candidate behind a Sanity draft, or ``None`` for a v2 draft.

    Reads the approval row first (that is the side the drafts API already has
    in hand) and falls back to ``candidates.sanity_draft_id`` so a link written
    by only one of the two paths is still followable.
    """
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, DraftApproval

    with get_session_factory()() as session:
        found = session.execute(
            select(DraftApproval.candidate_id_fk).where(
                DraftApproval.sanity_draft_id == sanity_draft_id,
                DraftApproval.brand_id_fk == brand_id_fk,
            )
        ).scalar_one_or_none()
        if found:
            return int(found)
        found = session.execute(
            select(Candidate.id).where(
                Candidate.sanity_draft_id == sanity_draft_id,
                Candidate.brand_id_fk == brand_id_fk,
            )
        ).scalar_one_or_none()
        return int(found) if found else None


# --------------------------------------------------------------------------
# 2. the publication slot
# --------------------------------------------------------------------------


def parse_slots(raw: Any) -> list[dict[str, Any]]:
    """``publication_slots`` as a list of ``{"day": ..., "capacity": ...}``.

    Fails soft to an empty list: an unparseable slot config must produce "no
    slot available" (visible, and the candidate stays ``ready``), never a
    traceback in the middle of an approve.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("candidate_lifecycle.bad_slots_json")
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        day = str(entry.get("day", "")).strip().lower()[:3]
        if day not in _WEEKDAYS:
            continue
        try:
            capacity = int(entry.get("capacity", 0))
        except (TypeError, ValueError):
            continue
        if capacity > 0:
            out.append({"day": day, "capacity": capacity})
    return out


def next_publication_slot(
    *,
    slots: Any,
    timezone_name: str | None,
    now: datetime,
    taken: dict[date, int],
) -> date | None:
    """The nearest slot date with room, in the brand's timezone.

    Pure by design — the single timezone-dependent computation in the system
    (NTS_098 §5) should be testable at a midnight boundary and across a DST
    transition without a database. ``now`` is any aware datetime; the search
    starts on **the brand's** current date, which is why 23:30 UTC on a Sunday
    already counts as Monday in Madrid.

    Today is eligible: publication is a manual Approve (NTS_098 §5), so a
    Monday morning draft belongs in Monday's slot, not next Monday's.
    """
    parsed = parse_slots(slots)
    if not parsed:
        return None
    capacity_by_weekday: dict[int, int] = {}
    for entry in parsed:
        weekday = _WEEKDAYS[entry["day"]]
        # Two entries for the same day are additive rather than last-wins:
        # "mon 2" plus "mon 1" reads as three on Monday to anyone typing it.
        capacity_by_weekday[weekday] = (
            capacity_by_weekday.get(weekday, 0) + entry["capacity"]
        )

    tz = resolve_timezone(timezone_name)
    start = now.astimezone(tz).date()
    for offset in range(_SLOT_SEARCH_DAYS):
        day = start + timedelta(days=offset)
        capacity = capacity_by_weekday.get(day.weekday())
        if capacity and taken.get(day, 0) < capacity:
            return day
    log.warning(
        "candidate_lifecycle.no_slot_available",
        searched_days=_SLOT_SEARCH_DAYS,
        from_date=start.isoformat(),
    )
    return None


def _slots_taken(session: Any, brand_id_fk: int) -> dict[date, int]:
    """How many candidates already hold each future slot date.

    Counts every non-terminal-failure status: a ``ready`` candidate is
    occupying the slot it is waiting in, and a ``published`` one occupied it.
    ``expired``/``failed``/``superseded``/``rejected`` release it.
    """
    from sqlalchemy import func, select

    from pipeline.admin.models import Candidate

    rows = session.execute(
        select(Candidate.publication_slot, func.count(Candidate.id))
        .where(
            Candidate.brand_id_fk == brand_id_fk,
            Candidate.publication_slot.is_not(None),
            Candidate.status.in_(("ready", "published", "drafted", "returned")),
        )
        .group_by(Candidate.publication_slot)
    ).all()
    return {row[0]: int(row[1]) for row in rows if row[0] is not None}


def assign_publication_slot(
    *,
    candidate_id: int,
    brand_id_fk: int,
    now: datetime | None = None,
) -> date | None:
    """``drafted``/``returned`` → ``ready`` with a slot. Returns the date.

    ``None`` means nothing moved: either the candidate is not in a status that
    can be made ready, or every slot for the next eight weeks is full. Both are
    conditions the operator should see, not conditions to work around.
    """
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, PipelineConfig

    now = now or datetime.now(tz=UTC)
    with get_session_factory()() as session:
        config = session.get(PipelineConfig, brand_id_fk)
        slot = next_publication_slot(
            slots=getattr(config, "publication_slots", None),
            timezone_name=getattr(config, "brand_timezone", None),
            now=now,
            taken=_slots_taken(session, brand_id_fk),
        )
        if slot is None:
            return None
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.status.in_(SLOTTABLE_FROM),
            )
            .values(status="ready", publication_slot=slot)
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            session.rollback()
            log.warning(
                "candidate_lifecycle.slot_refused",
                candidate_id=candidate_id,
                allowed_from=SLOTTABLE_FROM,
            )
            return None
        session.commit()
    log.info(
        "candidate_lifecycle.slot_assigned",
        candidate_id=candidate_id,
        slot=slot.isoformat(),
    )
    return slot


# --------------------------------------------------------------------------
# 3. published, and only against the recorded stamp
# --------------------------------------------------------------------------


def mark_published_if_approved(
    *, candidate_id: int, brand_id_fk: int | None = None
) -> bool:
    """``ready`` → ``published``, **iff** the approval carries a publish stamp.

    NTS_098 §2: "``published`` ставится только при непустом
    ``draft_approvals.published_at``". The stamp is copied rather than
    re-taken, so the candidate and the article agree on when the article went
    out even if this call happens a minute later or on a retry.
    """
    from sqlalchemy import select, update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, DraftApproval

    with get_session_factory()() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            return False
        if brand_id_fk is not None and candidate.brand_id_fk != brand_id_fk:
            return False
        if not candidate.sanity_draft_id:
            log.warning(
                "candidate_lifecycle.publish_without_draft_link",
                candidate_id=candidate_id,
            )
            return False
        published_at = session.execute(
            select(DraftApproval.published_at).where(
                DraftApproval.sanity_draft_id == candidate.sanity_draft_id,
                DraftApproval.brand_id_fk == candidate.brand_id_fk,
            )
        ).scalar_one_or_none()
        if published_at is None:
            log.info(
                "candidate_lifecycle.publish_deferred_no_stamp",
                candidate_id=candidate_id,
                draft_id=candidate.sanity_draft_id,
            )
            return False
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.status.in_(PUBLISHABLE_FROM),
            )
            .values(status="published", published_at=published_at)
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            session.rollback()
            return False
        session.commit()
    log.info("candidate_lifecycle.published", candidate_id=candidate_id)
    return True


def return_candidate_to_editor(
    *,
    candidate_id: int,
    brand_id_fk: int,
    reviewer: str = "admin",
    now: datetime | None = None,
) -> bool:
    """``drafted``/``ready``/``rejected`` → ``returned`` (NTS_098 §2 "editor return").

    Used by the Restore action on a rejected draft: the article is back in the
    editor's hands, which is exactly what ``returned`` means. The slot is
    released — holding a Monday slot for something nobody has approved is how a
    calendar starts lying.
    """
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    with get_session_factory()() as session:
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.status.in_(("drafted", "ready", "rejected")),
            )
            .values(
                status="returned",
                publication_slot=None,
                manual_action=None,
                manual_by=reviewer,
                manual_at=now,
            )
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            session.rollback()
            return False
        session.commit()
    return True


def mark_candidate_rejected(
    *,
    candidate_id: int,
    brand_id_fk: int,
    reason: str | None = None,
    reviewer: str = "admin",
    now: datetime | None = None,
) -> bool:
    """Manual reject from the review screen (NTS_098 §2 "manual reject")."""
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    with get_session_factory()() as session:
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.status.not_in(("published", "rejected")),
            )
            .values(
                status="rejected",
                verdict="reject",
                reason_code="out_of_scope",
                reason=(reason or "manual reject from review")[:200],
                manual_action="rejected",
                manual_by=reviewer,
                manual_at=now,
            )
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            session.rollback()
            return False
        session.commit()
    return True


# --------------------------------------------------------------------------
# 4. cost per candidate (NTS_106 §3)
# --------------------------------------------------------------------------


def candidate_spend_usd(candidate_id: int) -> float:
    """Everything ``cost_records`` has charged to this candidate."""
    from sqlalchemy import func, select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import CostRecord

    with get_session_factory()() as session:
        return float(
            session.execute(
                select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)).where(
                    CostRecord.candidate_id_fk == candidate_id
                )
            ).scalar()
            or 0.0
        )


def exceeds_cost_cap(candidate_id: int, cap_usd: float) -> bool:
    """``max_cost_per_candidate_usd`` (NTS_106 §3), now that it is computable.

    A cap of 0 or below means "no cap" — the same convention the monthly cap
    uses — rather than "everything is over budget", which is how a fresh config
    row with an unset key would otherwise stop all production.

    **No caller yet.** The production loop this belongs in is S4; it is here
    because the ceiling was not merely unenforced but arithmetically impossible
    before ``cost_records.candidate_id_fk`` existed (NTS_121 §2).
    """
    if cap_usd is None or cap_usd <= 0:
        return False
    return candidate_spend_usd(candidate_id) > float(cap_usd)
