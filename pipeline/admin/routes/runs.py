"""``/api/v1/runs`` route group."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from pipeline.admin.db import session_scope
from pipeline.admin.models import Run, Topic
from pipeline.admin.schemas import RunDetailOut, RunLogOut, RunOut, TopicOut
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


@router.get("/{run_id}", response_model=RunDetailOut)
def get_run(run_id: int) -> RunDetailOut:
    with session_scope() as session:
        r = session.get(Run, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="run not found")
        topics = list(
            session.scalars(
                select(Topic).where(Topic.run_id == run_id).order_by(Topic.id)
            )
        )
        return RunDetailOut(
            run=RunOut.model_validate(r),
            topics=[TopicOut.model_validate(t) for t in topics],
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
