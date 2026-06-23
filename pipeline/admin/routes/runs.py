"""``/api/v1/runs`` route group."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from pipeline.admin import jobs
from pipeline.admin.db import session_scope
from pipeline.admin.models import CostRecord, Run, Topic
from pipeline.admin.schemas import (
    CostBreakdownItem,
    CostByTopicItem,
    RunDetailWithCostOut,
    RunEventOut,
    RunEventsOut,
    RunKpis,
    RunLogOut,
    RunOut,
    RunStatus,
    TopicOut,
)
from pipeline.common.config import get_settings

router = APIRouter()


@router.get("", response_model=list[RunOut])
def list_runs(
    brand_id: int | None = None,
    status: RunStatus | None = Query(default=None),
    limit: int = 20,
    offset: int = 0,
) -> list[RunOut]:
    """List runs, newest first.

    ``status`` filter added in S4 so the dashboard active-runs panel can
    poll ``?status=running`` cheaply, and the activity feed can scope to
    ``success`` / ``failed`` when needed.
    """
    with session_scope() as session:
        stmt = select(Run).order_by(Run.started_at.desc())
        if brand_id is not None:
            stmt = stmt.where(Run.brand_id_fk == brand_id)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        stmt = stmt.offset(offset).limit(limit)
        return [RunOut.model_validate(r) for r in session.scalars(stmt)]


@router.get("/latest", response_model=RunOut)
def get_latest_run(brand_id: int | None = None) -> RunOut:
    """Return the most-recently-started run, optionally scoped to a brand.

    Used by the sidebar "Last run" link to resolve dynamically instead
    of hard-coding a run id. 404 when no runs exist yet.
    """
    with session_scope() as session:
        stmt = select(Run).order_by(Run.started_at.desc()).limit(1)
        if brand_id is not None:
            stmt = stmt.where(Run.brand_id_fk == brand_id)
        row = session.scalars(stmt).first()
        if row is None:
            raise HTTPException(status_code=404, detail="no runs yet")
        return RunOut.model_validate(row)


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: int) -> RunOut:
    """Cancel a running pipeline run (NTS_074 Task 2).

    Kills the detached worker process (by stored pid) and marks the run
    ``cancelled`` + ``finished_at``. Idempotent: cancelling an already-finished
    or already-cancelled run is a 200 no-op; only a missing run is a 404.

    Sync handler (FastAPI threadpool) — the kill + DB write never touch the
    event loop. ``cancelled`` is a distinct status from ``failed`` so an
    operator stop never trips the failed-run notifications / Telegram alerter.
    """
    outcome = jobs.cancel_run(run_id)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="run not found")
    with session_scope() as session:
        row = session.get(Run, run_id)
        if row is None:  # vanished between cancel + read — treat as 404
            raise HTTPException(status_code=404, detail="run not found")
        return RunOut.model_validate(row)


@router.get("/{run_id}", response_model=RunDetailWithCostOut)
def get_run(run_id: int) -> RunDetailWithCostOut:
    total = 0.0
    by_op: dict[str, tuple[float, int]] = {}
    # S4: per-topic rollup. topic_id is nullable on cost_records (e.g.,
    # pre-topic scoring batch costs land with topic_id=NULL), so the
    # "unattributed" bucket is keyed by None and folded into the chart
    # later as run-level overhead.
    by_topic: dict[int | None, dict[str, float]] = {}
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
            topic_bucket = by_topic.setdefault(c.topic_id, {})
            topic_bucket[c.operation] = (
                topic_bucket.get(c.operation, 0.0) + c.cost_usd
            )

    breakdown = [
        CostBreakdownItem(operation=op, cost_usd=round(amt, 6), count=n)
        for op, (amt, n) in sorted(by_op.items())
    ]
    cost_by_topic = [
        CostByTopicItem(
            topic_id=tid,
            by_operation={k: round(v, 6) for k, v in ops.items()},
            total_usd=round(sum(ops.values()), 6),
        )
        for tid, ops in by_topic.items()
    ]
    cost_by_topic.sort(key=lambda c: c.total_usd, reverse=True)

    # Authoritative KPI block — single source of truth per metric.
    # ``scored`` counts topic_scoring LLM calls (every item evaluated by
    # the picker, including ones below threshold). ``passed`` and
    # ``drafts`` read off the topics table. We fall back to ``run.stats``
    # for ``drafted`` when the topics table is empty for a legacy run.
    stats_raw = run_out.stats or {}
    fetched = int(stats_raw.get("fetched", 0) or 0)
    errors = int(stats_raw.get("errors", 0) or 0)
    passed_count = sum(1 for t in topics_out if t.status == "passed")
    drafts_from_topics = len(
        {t.draft_id for t in topics_out if t.draft_id}
    )
    stats_drafted = int(stats_raw.get("drafted", 0) or 0)
    drafts_count = drafts_from_topics or stats_drafted
    scored_calls = next(
        (item.count for item in breakdown if item.operation == "topic_scoring"),
        0,
    )
    kpis = RunKpis(
        fetched=fetched,
        scored=scored_calls,
        passed=passed_count,
        drafts=drafts_count,
        errors=errors,
    )
    return RunDetailWithCostOut(
        run=run_out,
        topics=topics_out,
        cost_total_usd=round(total, 6),
        cost_breakdown=breakdown,
        cost_by_topic=cost_by_topic,
        kpis=kpis,
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


_EVENT_PAGE_CAP = 500


@router.get("/{run_id}/events", response_model=RunEventsOut)
def get_run_events(run_id: int, level: str | None = None) -> RunEventsOut:
    """Return normalized log events for one run.

    Reads the structlog JSON-line file at ``settings.admin_log_path``
    and filters entries by ``timestamp`` ∈ ``[run.started_at,
    run.finished_at]`` (open-ended when the run is still running).
    Non-JSON / uvicorn access lines are skipped silently. Capped at
    :data:`_EVENT_PAGE_CAP` events to keep the response cheap.
    """
    with session_scope() as session:
        r = session.get(Run, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="run not found")
        started_at = r.started_at
        finished_at = r.finished_at

    log_path = Path(get_settings().admin_log_path)
    if not log_path.exists():
        return RunEventsOut(events=[], total=0, truncated=False, source="stub")

    # Normalise to UTC for tz-naive timestamps (SQLite stores naive UTC).
    def _aware_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    start_utc = _aware_utc(started_at)
    end_utc = _aware_utc(finished_at)

    events: list[RunEventOut] = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = payload.get("timestamp")
                kind = payload.get("event")
                lvl = payload.get("level", "info")
                if not isinstance(ts_raw, str) or not isinstance(kind, str):
                    continue
                if level is not None and lvl != level:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if start_utc and ts < start_utc:
                    continue
                if end_utc and ts > end_utc:
                    # Past the run window — log file is append-only, so
                    # everything after this is from a later run.
                    break
                data = {
                    k: v
                    for k, v in payload.items()
                    if k not in {"timestamp", "event", "level"}
                }
                events.append(
                    RunEventOut(
                        timestamp=ts, level=lvl, kind=kind, data=data
                    )
                )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not read log: {exc}") from exc

    total = len(events)
    truncated = total > _EVENT_PAGE_CAP
    if truncated:
        events = events[-_EVENT_PAGE_CAP:]
    return RunEventsOut(
        events=events, total=total, truncated=truncated, source="file"
    )
