"""``/api/v1/topics`` route group — threshold simulator (S5 Step 9).

Replays the score-based filter against historical topics so the operator
can see how a threshold change would have changed run yield without
needing to run the pipeline. Only topics with a non-null score are
considered — ``filtered_banned`` / ``filtered_dup`` / ``failed`` are
upstream of scoring and unaffected by the threshold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from pipeline.admin.db import session_scope
from pipeline.admin.models import Brand, Run, Topic
from pipeline.admin.schemas import (
    ScoreBucket,
    TopicsSimulateIn,
    TopicsSimulateOut,
)

router = APIRouter()


@router.post("/simulate", response_model=TopicsSimulateOut)
def simulate_threshold(payload: TopicsSimulateIn) -> TopicsSimulateOut:
    """Replay topic-score filter under a candidate threshold."""
    with session_scope() as session:
        brand = session.get(Brand, payload.brand_id)
        if brand is None:
            raise HTTPException(status_code=404, detail="brand not found")

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=payload.days)
        stmt = (
            select(Topic.score, Topic.status)
            .join(Run, Topic.run_id == Run.id)
            .where(
                Run.brand_id_fk == payload.brand_id,
                Topic.score.is_not(None),
                Topic.created_at >= cutoff,
            )
        )
        rows = list(session.execute(stmt).all())

    total_scored = len(rows)
    currently_passed = 0
    would_pass = 0
    swing_in = 0
    swing_out = 0
    dist: dict[int, int] = {}

    for score, status in rows:
        s = int(score)
        dist[s] = dist.get(s, 0) + 1
        currently = status == "passed"
        future = s >= payload.threshold and status in ("passed", "filtered_score")
        if currently:
            currently_passed += 1
        if future:
            would_pass += 1
        if future and not currently:
            swing_in += 1
        if currently and not future:
            swing_out += 1

    score_distribution = [
        ScoreBucket(score=k, count=v) for k, v in sorted(dist.items())
    ]
    return TopicsSimulateOut(
        threshold=payload.threshold,
        days=payload.days,
        total_scored=total_scored,
        currently_passed=currently_passed,
        would_pass=would_pass,
        delta=would_pass - currently_passed,
        swing_in=swing_in,
        swing_out=swing_out,
        score_distribution=score_distribution,
    )
