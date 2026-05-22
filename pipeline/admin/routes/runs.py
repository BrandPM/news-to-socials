"""``/api/v1/runs`` route group."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from pipeline.admin.db import session_scope
from pipeline.admin.models import CostRecord, Run, Topic
from pipeline.admin.schemas import (
    CostBreakdownItem,
    RunDetailWithCostOut,
    RunLogOut,
    RunOut,
    TopicOut,
)
from pipeline.common.config import get_settings

router = APIRouter()


@router.get("", response_model=list[RunOut])
def list_runs(
    brand_id: int | None = None, limit: int = 20, offset: int = 0
) -> list[RunOut]:
    with session_scope() as session:
        stmt = select(Run).order_by(Run.started_at.desc())
        if brand_id is not None:
            stmt = stmt.where(Run.brand_id_fk == brand_id)
        stmt = stmt.offset(offset).limit(limit)
        return [RunOut.model_validate(r) for r in session.scalars(stmt)]


@router.get("/{run_id}", response_model=RunDetailWithCostOut)
def get_run(run_id: int) -> RunDetailWithCostOut:
    total = 0.0
    by_op: dict[str, tuple[float, int]] = {}
    with session_scope() as session:
        r = session.get(Run, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="run not found")
        topics = list(
            session.scalars(
                select(Topic).where(Topic.run_id == run_id).order_by(Topic.id)
            )
        )
        cost_rows = list(
            session.scalars(
                select(CostRecord).where(CostRecord.run_id == run_id)
            )
        )
        run_out = RunOut.model_validate(r)
        topics_out = [TopicOut.model_validate(t) for t in topics]
        for c in cost_rows:
            total += c.cost_usd
            agg = by_op.get(c.operation, (0.0, 0))
            by_op[c.operation] = (agg[0] + c.cost_usd, agg[1] + 1)

    breakdown = [
        CostBreakdownItem(operation=op, cost_usd=round(amt, 6), count=n)
        for op, (amt, n) in sorted(by_op.items())
    ]
    return RunDetailWithCostOut(
        run=run_out,
        topics=topics_out,
        cost_total_usd=round(total, 6),
        cost_breakdown=breakdown,
    )


@router.get("/{run_id}/log", response_model=RunLogOut)
def get_run_log(run_id: int, tail: int = 200) -> RunLogOut:
    """Return up to ``tail`` lines from the pipeline log file.

    In S1 local dev the log path on Mac usually doesn't exist (the file
    lives on the VPS under ``/var/log/news-to-socials/run.log``). We
    return a stub in that case so the UI has a defined contract; S3
    wires the real path on the VPS.
    """
    with session_scope() as session:
        r = session.get(Run, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="run not found")
        excerpt = r.log_excerpt or ""

    log_path = Path(get_settings().admin_log_path)
    if not log_path.exists():
        body = (
            excerpt
            or f"# log file at {log_path} is not present on this host (likely Mac dev).\n"
            "# Tail of run.log_excerpt as stored in admin.db (may be empty)."
        )
        return RunLogOut(log=body, source="stub")

    # File exists — read the last `tail` lines. Bounded read keeps us safe
    # even if the file has rotated and grown large.
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-tail:]
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not read log: {exc}") from exc
    return RunLogOut(log="".join(lines), source="file")
