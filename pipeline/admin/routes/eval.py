"""``/api/v1/eval`` — LLM-judge measurability (IT_PROJ_NTS_091).

A single read endpoint powering the "measurability" table: average judge total
by prompt version and by ISO week. This is the measurable signal for prompt
iteration ("did version v2 actually score better than v1?"). No charting
library — a table is enough per the spec.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import Integer, cast, func, select

from pipeline.admin.db import session_scope
from pipeline.admin.models import DraftScore
from pipeline.admin.schemas import EvalSummaryOut, EvalSummaryRow

router = APIRouter()


@router.get("/summary", response_model=EvalSummaryOut)
def eval_summary(brand_id: int) -> EvalSummaryOut:
    """Average total (+ counts + flagged) grouped by judge prompt version and
    by week, for one brand. Empty lists when there are no scores yet."""
    # SQLite ISO-ish week key. strftime('%Y-W%W') → "2026-W28".
    week_expr = func.strftime("%Y-W%W", DraftScore.created_at)
    with session_scope() as session:
        by_version = [
            EvalSummaryRow(
                key=str(version),
                avg_total=round(float(avg or 0.0), 3),
                n=int(n or 0),
                flagged=int(flagged or 0),
            )
            for version, avg, n, flagged in session.execute(
                select(
                    DraftScore.judge_prompt_version,
                    func.avg(DraftScore.total),
                    func.count(),
                    func.sum(cast(DraftScore.flagged, Integer)),
                )
                .where(DraftScore.brand_id_fk == brand_id)
                .group_by(DraftScore.judge_prompt_version)
                .order_by(DraftScore.judge_prompt_version)
            ).all()
        ]
        by_week = [
            EvalSummaryRow(
                key=str(week),
                avg_total=round(float(avg or 0.0), 3),
                n=int(n or 0),
                flagged=int(flagged or 0),
            )
            for week, avg, n, flagged in session.execute(
                select(
                    week_expr,
                    func.avg(DraftScore.total),
                    func.count(),
                    func.sum(cast(DraftScore.flagged, Integer)),
                )
                .where(DraftScore.brand_id_fk == brand_id)
                .group_by(week_expr)
                .order_by(week_expr)
            ).all()
        ]
    return EvalSummaryOut(by_version=by_version, by_week=by_week)
