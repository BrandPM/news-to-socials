"""``/api/v1/dashboard`` route group — bundled KPI aggregates for S4.

The /dashboard page needs ~8 KPI numbers that span cost_records, runs,
and runs.stats. Computing them with five separate frontend round-trips
would have made the first paint visibly slow on cold start. This module
bundles them into one query.

All aggregations are in SQL; no raw row materialisation. M1 is enforced
by requiring brand_id on every endpoint.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from pipeline.admin.db import session_scope
from pipeline.admin.models import CostRecord, Run
from pipeline.admin.schemas import DashboardSummaryOut

router = APIRouter()


def _start_of_today_utc(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _drafts_count_in_window(
    session, brand_id: int, start: datetime, end: datetime
) -> int:
    """Sum the 'drafted' stat across successful runs in [start, end).

    Counted from runs.stats JSON rather than from Sanity to keep this
    page tied to pipeline output (Sanity could have manually-created
    documents that aren't pipeline drafts).
    """
    rows = session.scalars(
        select(Run).where(
            Run.brand_id_fk == brand_id,
            Run.status.in_(("success", "running")),
            Run.started_at >= start,
            Run.started_at < end,
        )
    ).all()
    total = 0
    for r in rows:
        if not r.stats:
            continue
        try:
            stats = json.loads(r.stats)
        except (TypeError, ValueError):
            continue
        drafted = stats.get("drafted") if isinstance(stats, dict) else None
        if isinstance(drafted, int):
            total += drafted
    return total


@router.get("/summary", response_model=DashboardSummaryOut)
def dashboard_summary(brand_id: int) -> DashboardSummaryOut:
    now = datetime.now(tz=timezone.utc)
    today_start = _start_of_today_utc(now)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=6)
    prev_week_start = today_start - timedelta(days=13)
    prev_week_end = week_start
    month_start = today_start.replace(day=1)
    seven_days_ago = today_start - timedelta(days=7)

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    day_of_month = now.day  # 1-based; treat as "partial day" via fraction below
    fraction_through_day = (
        (now - today_start).total_seconds() / (24 * 3600)
        if now >= today_start
        else 0.0
    )
    days_elapsed_real = (day_of_month - 1) + fraction_through_day
    if days_elapsed_real <= 0:
        days_elapsed_real = 1 / 24  # avoid div-by-zero at midnight on day 1
    days_progress_pct = round(100.0 * days_elapsed_real / days_in_month, 2)

    with session_scope() as session:
        def _sum_cost(start: datetime, end: datetime | None) -> float:
            stmt = select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)).where(
                CostRecord.brand_id_fk == brand_id,
                CostRecord.created_at >= start,
            )
            if end is not None:
                stmt = stmt.where(CostRecord.created_at < end)
            value = session.execute(stmt).scalar_one()
            return float(value or 0.0)

        cost_today = _sum_cost(today_start, None)
        cost_yesterday = _sum_cost(yesterday_start, today_start)
        cost_month = _sum_cost(month_start, None)
        cost_7d = _sum_cost(seven_days_ago, today_start)

        last_finished_row = session.execute(
            select(Run.finished_at, Run.status)
            .where(
                Run.brand_id_fk == brand_id,
                Run.finished_at.is_not(None),
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        ).first()
        last_finished_at = last_finished_row.finished_at if last_finished_row else None
        last_status = last_finished_row.status if last_finished_row else None

        active_runs_count = session.execute(
            select(func.count())
            .select_from(Run)
            .where(
                Run.brand_id_fk == brand_id,
                Run.status == "running",
            )
        ).scalar_one()

        drafts_today = _drafts_count_in_window(
            session, brand_id, today_start, today_start + timedelta(days=1)
        )
        drafts_week = _drafts_count_in_window(
            session, brand_id, week_start, today_start + timedelta(days=1)
        )
        drafts_prev_week = _drafts_count_in_window(
            session, brand_id, prev_week_start, prev_week_end
        )

    # Forecast: extrapolate current month-to-date to end-of-month, linearly.
    cost_forecast = (
        round((cost_month / days_elapsed_real) * days_in_month, 6)
        if days_elapsed_real > 0
        else cost_month
    )

    if cost_yesterday > 0:
        trend_pct: float | None = round(
            100.0 * (cost_today - cost_yesterday) / cost_yesterday, 2
        )
    elif cost_today > 0:
        trend_pct = None  # +∞ — frontend will render "new" instead of a number
    else:
        trend_pct = 0.0

    avg_daily_7d = round(cost_7d / 7.0, 6)

    return DashboardSummaryOut(
        cost_today_usd=round(cost_today, 6),
        cost_yesterday_usd=round(cost_yesterday, 6),
        cost_today_trend_pct=trend_pct,
        cost_month_usd=round(cost_month, 6),
        cost_month_forecast_usd=cost_forecast,
        cost_month_days_progress_pct=days_progress_pct,
        drafts_today=drafts_today,
        drafts_this_week=drafts_week,
        drafts_prev_week=drafts_prev_week,
        last_run_finished_at=last_finished_at,
        last_run_status=last_status,
        active_runs_count=int(active_runs_count or 0),
        avg_daily_cost_7d_usd=avg_daily_7d,
    )
