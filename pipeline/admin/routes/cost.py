"""``/api/v1/cost`` route group — read-only aggregates for S3 / S4.

S3 ships a simple summary endpoint + paginated records list. S4 will
add per-component charts, top-expensive-drafts, runrate forecast.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from pipeline.admin.db import session_scope
from pipeline.admin.models import CostRecord
from pipeline.admin.schemas import (
    CostRecordOut,
    CostSummaryByDay,
    CostSummaryOut,
)

router = APIRouter()


Period = Literal["today", "week", "month"]


def _period_start(period: Period) -> datetime:
    now = datetime.now(tz=timezone.utc)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    raise ValueError(f"unknown period {period!r}")


@router.get("/summary", response_model=CostSummaryOut)
def cost_summary(brand_id: int, period: Period = "month") -> CostSummaryOut:
    start = _period_start(period)
    total = 0.0
    by_op: dict[str, float] = {}
    by_provider: dict[str, float] = {}
    by_day_map: dict[str, float] = {}
    with session_scope() as session:
        rows = session.scalars(
            select(CostRecord).where(
                CostRecord.brand_id_fk == brand_id,
                CostRecord.created_at >= start,
            )
        ).all()
        for r in rows:
            total += r.cost_usd
            by_op[r.operation] = by_op.get(r.operation, 0.0) + r.cost_usd
            by_provider[r.provider] = by_provider.get(r.provider, 0.0) + r.cost_usd
            day = r.created_at.strftime("%Y-%m-%d")
            by_day_map[day] = by_day_map.get(day, 0.0) + r.cost_usd

    by_day = [
        CostSummaryByDay(date=d, cost_usd=round(c, 6))
        for d, c in sorted(by_day_map.items())
    ]
    return CostSummaryOut(
        total_usd=round(total, 6),
        by_operation={k: round(v, 6) for k, v in by_op.items()},
        by_provider={k: round(v, 6) for k, v in by_provider.items()},
        by_day=by_day,
    )


@router.get("/records", response_model=list[CostRecordOut])
def cost_records(
    brand_id: int,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CostRecordOut]:
    with session_scope() as session:
        rows = session.scalars(
            select(CostRecord)
            .where(CostRecord.brand_id_fk == brand_id)
            .order_by(CostRecord.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        out = [CostRecordOut.model_validate(r) for r in rows]
    return out
