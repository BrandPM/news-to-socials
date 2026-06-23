"""Notification computation — shared by the HTTP route and the alerter.

Extracted from ``routes/notifications.py`` (NTS_073) so the Telegram
push-alerter (:mod:`pipeline.monitoring.alerts`) and the
``/api/v1/notifications`` route compute from the *same* logic. The route's
behaviour is unchanged: it calls :func:`compute_notifications` and returns
the result.

No persistence — the list is derived from existing tables on each call, so
deleting the underlying row makes the notification disappear (the right
behaviour for "action items" rather than "alerts").

Three kinds, all brand-scoped:

* ``run_failed`` — runs with ``status='failed'`` in the last 24h. danger.
* ``source_unhealthy`` — sources with success rate < 50% over the last
  7 days *and* at least 3 attempts in the window. warning.
* ``draft_rejected`` — draft_approvals rows with ``status='rejected'``
  in the last 7 days. warning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from pipeline.admin.models import (
    DraftApproval,
    Run,
    Source,
    SourceHealthRecord,
)
from pipeline.admin.schemas import NotificationItemOut


def compute_notifications(
    session: Session, brand_id: int
) -> list[NotificationItemOut]:
    """Return the current notification items for ``brand_id``.

    Sorted newest-first, matching what the route returned before the
    extraction. The caller is responsible for verifying the brand exists.
    """
    now = datetime.now(tz=timezone.utc)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    items: list[NotificationItemOut] = []

    # --- 1. Failed runs in last 24h ---
    failed_runs = (
        session.execute(
            select(Run)
            .where(
                Run.brand_id_fk == brand_id,
                Run.status == "failed",
                Run.started_at >= day_ago,
            )
            .order_by(Run.started_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    for r in failed_runs:
        items.append(
            NotificationItemOut(
                id=f"run-{r.id}",
                kind="run_failed",
                severity="danger",
                title=f"Run #{r.id} failed",
                description=(
                    (r.log_excerpt or "Pipeline run did not finish.")
                    .strip()
                    .splitlines()[-1][:200]
                    if r.log_excerpt
                    else "Pipeline run did not finish."
                ),
                href=f"/runs/{r.id}",
                created_at=r.started_at,
            )
        )

    # --- 2. Unhealthy sources (7d) ---
    source_rows = (
        session.execute(
            select(Source).where(
                Source.brand_id_fk == brand_id,
                Source.active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    for s in source_rows:
        # SQLAlchemy coerces SUM(Boolean) back to bool, so use CASE to
        # force an integer SUM that survives the round-trip.
        agg = session.execute(
            select(
                func.count(SourceHealthRecord.id),
                func.sum(
                    case((SourceHealthRecord.success.is_(True), 1), else_=0)
                ),
            ).where(
                SourceHealthRecord.source_id == s.id,
                SourceHealthRecord.fetched_at >= week_ago,
            )
        ).one()
        total = agg[0] or 0
        success_count = int(agg[1] or 0)
        if total < 3:
            continue
        failure_count = total - success_count
        rate = success_count / total if total else 1.0
        if rate < 0.5:
            items.append(
                NotificationItemOut(
                    id=f"source-{s.id}",
                    kind="source_unhealthy",
                    severity="warning",
                    title=f"Source “{s.name}” unhealthy",
                    description=(
                        f"{failure_count}/{total} fetches failed in the "
                        f"last 7 days ({int(rate * 100)}% success)."
                    ),
                    href="/sources",
                    created_at=now,
                )
            )

    # --- 3. Rejected drafts (7d) ---
    rejected = (
        session.execute(
            select(DraftApproval)
            .where(
                DraftApproval.brand_id_fk == brand_id,
                DraftApproval.status == "rejected",
                DraftApproval.decided_at >= week_ago,
            )
            .order_by(DraftApproval.decided_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    for a in rejected:
        # Drop the leading "drafts." prefix for the UI link.
        slug = a.sanity_draft_id
        slug = slug[len("drafts."):] if slug.startswith("drafts.") else slug
        items.append(
            NotificationItemOut(
                id=f"reject-{a.id}",
                kind="draft_rejected",
                severity="warning",
                title="Draft rejected",
                description=(
                    a.note
                    if a.note
                    else f"Draft {slug} marked rejected. Review or regenerate."
                ),
                href=f"/drafts/{slug}",
                created_at=a.decided_at,
            )
        )

    items.sort(key=lambda n: n.created_at, reverse=True)
    return items
