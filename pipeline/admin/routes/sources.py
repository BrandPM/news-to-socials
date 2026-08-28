"""``/api/v1/sources`` route group."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from pipeline.admin import jobs
from pipeline.admin.db import session_scope
from pipeline.admin.models import Source, SourceHealthRecord, Topic
from pipeline.admin.schemas import (
    RunAllIn,
    RunTriggerOut,
    SourceHealthDayOut,
    SourceHealthOut,
    SourceIn,
    SourceOut,
    SourceRegistryOut,
    SourceRegistryUpdate,
    SourceTestOut,
    SourceUpdate,
)

router = APIRouter()

# Audit-trail provenance for a pipeline run. The cron systemd unit passes
# ``X-Triggered-By: cron``; the admin UI omits the header (→ "manual"); CLI
# tooling may pass "cli". Anything else is rejected so a typo never silently
# pollutes the audit trail (NTS_056 Task 1).
_ALLOWED_TRIGGERS = frozenset({"cron", "manual", "cli"})


def _resolve_triggered_by(value: str | None) -> str:
    """Validate the X-Triggered-By header, defaulting to 'manual'.

    Raises 400 for any value outside the allow-list.
    """
    resolved = (value or "manual").strip().lower()
    if resolved not in _ALLOWED_TRIGGERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid X-Triggered-By {value!r} — must be one of "
                f"{sorted(_ALLOWED_TRIGGERS)}"
            ),
        )
    return resolved


@router.get("", response_model=list[SourceOut])
def list_sources(brand_id: int | None = None) -> list[SourceOut]:
    with session_scope() as session:
        stmt = select(Source).order_by(Source.id)
        if brand_id is not None:
            stmt = stmt.where(Source.brand_id_fk == brand_id)
        return [SourceOut.model_validate(s) for s in session.scalars(stmt)]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(payload: SourceIn) -> SourceOut:
    with session_scope() as session:
        src = Source(
            brand_id_fk=payload.brand_id,
            name=payload.name,
            source_type=payload.source_type,
            url=str(payload.url),
            primary_category=payload.primary_category,
            active=payload.active,
            paywall=payload.paywall,
            polling_minutes=payload.polling_minutes,
            credentials=payload.credentials,
            custom_parser=payload.custom_parser,
        )
        session.add(src)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"brand_id={payload.brand_id} does not reference an existing brand",
            ) from exc
        return SourceOut.model_validate(src)


# --- v3 registry view (NTS_111 §Источники, NTS_101 §1) --------------------


@router.get("/registry", response_model=list[SourceRegistryOut])
def sources_registry(brand_id: int, days: int = 7) -> list[SourceRegistryOut]:
    """One row per source with everything the Sources screen shows.

    Assembled server-side because the screen's columns are four different
    queries per source (health series, candidate counts, accepted counts,
    document-find share) and doing that from the browser would be N+1 over the
    wire for a table the operator opens to answer "what is broken".

    ``health`` is one entry per day, newest last: ``True`` all fetches
    succeeded, ``False`` at least one failed, ``None`` no fetch that day. The
    three-state answer matters — a gap is not a success, and rendering it as
    one is how a source that stopped being polled looks healthy.

    ``doc_find_share`` stays ``None`` until S5 writes ``primary_doc_url`` for
    ``news`` items (NTS_101 §2-7). A hard 0.0 would read as "this source never
    finds documents" rather than "not measured yet".
    """
    from datetime import timedelta

    from pipeline.admin.models import Candidate

    days = max(1, min(90, days))
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=days)
    month_cutoff = now - timedelta(days=30)

    with session_scope() as session:
        rows = list(
            session.scalars(
                select(Source)
                .where(Source.brand_id_fk == brand_id)
                .order_by(Source.source_role.desc(), Source.name)
            )
        )
        out: list[SourceRegistryOut] = []
        for src in rows:
            health_rows = list(
                session.scalars(
                    select(SourceHealthRecord)
                    .where(
                        SourceHealthRecord.source_id == src.id,
                        SourceHealthRecord.fetched_at >= cutoff,
                    )
                    .order_by(SourceHealthRecord.fetched_at.asc())
                )
            )
            per_day: dict[str, list[bool]] = {}
            for record in health_rows:
                per_day.setdefault(
                    record.fetched_at.date().isoformat(), []
                ).append(bool(record.success))
            health: list[bool | None] = []
            for offset in range(days):
                key = (now - timedelta(days=days - 1 - offset)).date().isoformat()
                results = per_day.get(key)
                health.append(all(results) if results else None)

            total = len(health_rows)
            succeeded = sum(1 for r in health_rows if r.success)
            last_error: str | None = None
            for record in reversed(health_rows):
                if not record.success and record.error_msg:
                    last_error = record.error_msg
                    break

            candidates_30d = int(
                session.execute(
                    select(func.count(Candidate.id)).where(
                        Candidate.source_id_fk == src.id,
                        Candidate.created_at >= month_cutoff,
                    )
                ).scalar()
                or 0
            )
            accepted_30d = int(
                session.execute(
                    select(func.count(Candidate.id)).where(
                        Candidate.source_id_fk == src.id,
                        Candidate.verdict == "accept",
                        Candidate.created_at >= month_cutoff,
                    )
                ).scalar()
                or 0
            )
            with_doc = int(
                session.execute(
                    select(func.count(Candidate.id)).where(
                        Candidate.source_id_fk == src.id,
                        Candidate.verdict == "accept",
                        Candidate.primary_doc_url.is_not(None),
                        Candidate.created_at >= month_cutoff,
                    )
                ).scalar()
                or 0
            )

            record_out = SourceRegistryOut.model_validate(src)
            record_out = record_out.model_copy(
                update={
                    "health": health,
                    "success_rate_pct": (
                        round(succeeded / total * 100.0, 1) if total else None
                    ),
                    "last_error": last_error,
                    "candidates_30d": candidates_30d,
                    "accepted_30d": accepted_30d,
                    # Only meaningful for news: a primary_feed item IS the
                    # document, so a share of 1.0 there would say nothing.
                    "doc_find_share": (
                        round(with_doc / accepted_30d, 3)
                        if src.source_role == "news" and accepted_30d
                        else None
                    ),
                }
            )
            out.append(record_out)
        return out


@router.get("/{source_id}", response_model=SourceOut)
def get_source(source_id: int) -> SourceOut:
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        return SourceOut.model_validate(src)


@router.put("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourceUpdate) -> SourceOut:
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        data = payload.model_dump(exclude_unset=True)
        if "url" in data and data["url"] is not None:
            data["url"] = str(data["url"])
        for k, v in data.items():
            setattr(src, k, v)
        src.updated_at = datetime.now(tz=UTC)
        session.flush()
        return SourceOut.model_validate(src)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: int) -> None:
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        # The FK on topics.source_id is ON DELETE RESTRICT; rather than
        # surface a raw IntegrityError, return 409 with a clear message
        # so the UI can show "delete the older runs first".
        in_use = session.execute(
            select(Topic.id).where(Topic.source_id == source_id).limit(1)
        ).first()
        if in_use is not None:
            raise HTTPException(
                status_code=409,
                detail="source has historical topic rows — delete those runs first",
            )
        session.delete(src)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- Test parse + run ----------------------------------------------------


@router.post("/{source_id}/test", response_model=SourceTestOut)
async def test_source(source_id: int, limit: int = 5) -> SourceTestOut:
    """Fetch the source once, return the first ``limit`` headlines.

    Doesn't write anything to admin.db and doesn't call the LLM.
    """
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        url = src.url
        source_type = src.source_type
        source_name = src.name

    if source_type != "rss":
        return SourceTestOut(
            parser_status="error",
            headlines=[],
            error=f"source_type={source_type!r} test-parse is only implemented for 'rss' in S1",
        )

    from pipeline.sources.rss import RssSource

    try:
        rss = RssSource(source_id=str(source_id), name=source_name, url=url)
        items = list(await rss.fetch())
    except Exception as exc:
        return SourceTestOut(
            parser_status="error", headlines=[], error=f"{type(exc).__name__}: {exc}"
        )

    return SourceTestOut(
        parser_status="ok",
        headlines=[
            {"title": it.title, "url": str(it.url)} for it in items[:limit]
        ],
    )


@router.post(
    "/{source_id}/run",
    response_model=RunTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_source(
    source_id: int,
    x_triggered_by: str | None = Header(default=None, alias="X-Triggered-By"),
) -> RunTriggerOut:
    triggered_by = _resolve_triggered_by(x_triggered_by)
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        brand_id_fk = src.brand_id_fk
    run_id = jobs.kick_off_pipeline_run(
        brand_id_fk=brand_id_fk, source_ids=[source_id], triggered_by=triggered_by
    )
    # NTS_074: launch as a detached subprocess, NOT a BackgroundTask sharing
    # the event loop. This sync handler runs in the threadpool and the
    # fork+exec is non-blocking, so the run never touches the API loop.
    jobs.spawn_pipeline_run(run_id)
    return RunTriggerOut(run_id=run_id)


@router.post(
    "/run-all",
    response_model=RunTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_all_sources(
    payload: RunAllIn,
    x_triggered_by: str | None = Header(default=None, alias="X-Triggered-By"),
) -> RunTriggerOut:
    """Schedule the pipeline for every active source of one brand.

    Refuses brands with status != 'active' (M4) — returns 409 so the UI
    can show 'Setup required' instead of silently producing nothing.

    ``X-Triggered-By`` records run provenance (cron|manual|cli) for the
    audit trail — the cron systemd unit sets ``cron`` (NTS_056 Task 1).
    """
    from pipeline.admin.models import Brand

    triggered_by = _resolve_triggered_by(x_triggered_by)

    with session_scope() as session:
        brand = session.get(Brand, payload.brand_id)
        if brand is None:
            raise HTTPException(status_code=404, detail="brand not found")
        if not brand.active:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"brand {brand.slug!r} is not active "
                    f"(status={brand.status!r}) — cannot run pipeline"
                ),
            )
        active_sources = session.scalars(
            select(Source).where(
                Source.brand_id_fk == payload.brand_id, Source.active.is_(True)
            )
        ).all()
        if not active_sources:
            raise HTTPException(
                status_code=409,
                detail=f"brand {brand.slug!r} has no active sources",
            )
        source_ids = [s.id for s in active_sources]
    run_id = jobs.kick_off_pipeline_run(
        brand_id_fk=payload.brand_id,
        source_ids=source_ids,
        triggered_by=triggered_by,
    )
    # NTS_074: detached subprocess (see run_source) — cron's POST contract is
    # unchanged; the run just no longer shares the admin-API event loop.
    jobs.spawn_pipeline_run(run_id)
    return RunTriggerOut(run_id=run_id)


# --- Health series (S5 Step 6) ------------------------------------------


@router.get("/{source_id}/health", response_model=SourceHealthOut)
def source_health(
    source_id: int,
    brand_id: int,
    days: int = 7,
) -> SourceHealthOut:
    """Per-day fetch health for the source's sparkline.

    Brand-scoped: 404 if the source belongs to a different brand than
    ``brand_id``. ``days`` is clamped to [1, 90].
    """
    from datetime import timedelta

    days = max(1, min(90, days))
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        if src.brand_id_fk != brand_id:
            raise HTTPException(
                status_code=404, detail="source not found for that brand"
            )

        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        rows = (
            session.query(SourceHealthRecord)
            .filter(
                SourceHealthRecord.source_id == source_id,
                SourceHealthRecord.fetched_at >= cutoff,
            )
            .order_by(SourceHealthRecord.fetched_at.asc())
            .all()
        )

        # Bucket by UTC date.
        buckets: dict[str, dict[str, int]] = {}
        for i in range(days):
            d = (datetime.now(tz=UTC) - timedelta(days=days - 1 - i)).date()
            buckets[d.isoformat()] = {
                "fetches": 0,
                "success_count": 0,
                "failure_count": 0,
                "articles_total": 0,
            }
        for r in rows:
            key = r.fetched_at.date().isoformat()
            if key not in buckets:
                continue
            b = buckets[key]
            b["fetches"] += 1
            if r.success:
                b["success_count"] += 1
            else:
                b["failure_count"] += 1
            b["articles_total"] += r.articles_count or 0

        last_fetch = rows[-1] if rows else None
        last_error: str | None = None
        for r in reversed(rows):
            if not r.success and r.error_msg:
                last_error = r.error_msg
                break
        total = sum(b["fetches"] for b in buckets.values())
        succ = sum(b["success_count"] for b in buckets.values())
        success_rate = (succ / total * 100.0) if total > 0 else 0.0

        series = [
            SourceHealthDayOut(
                date=date,
                fetches=b["fetches"],
                success_count=b["success_count"],
                failure_count=b["failure_count"],
                articles_total=b["articles_total"],
            )
            for date, b in buckets.items()
        ]

        return SourceHealthOut(
            source_id=source_id,
            days=days,
            success_rate_pct=round(success_rate, 1),
            last_fetch_at=last_fetch.fetched_at if last_fetch else None,
            last_error=last_error,
            series=series,
        )


@router.put("/{source_id}/registry", response_model=SourceRegistryOut)
def update_source_registry(
    source_id: int, brand_id: int, payload: SourceRegistryUpdate
) -> SourceRegistryOut:
    """Reclassify a source (role, class, licence, language, fetch method).

    NTS_108's DoD wants a `license_class` on every source, and migration 020
    started every pre-v3 feed at the most restrictive one. This is the endpoint
    that moves them up — deliberately separate from the generic source PUT so
    a licence change is a distinct, auditable action rather than a field in a
    form that also renames the feed.
    """
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None or src.brand_id_fk != brand_id:
            raise HTTPException(status_code=404, detail="source not found")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(src, field, value)
        src.updated_at = datetime.now(tz=UTC)
        try:
            session.flush()
        except IntegrityError as exc:
            # The CHECK constraints from 020 are the real vocabulary; the
            # schema Literals are the first line, this is the second.
            raise HTTPException(
                status_code=422, detail=f"invalid classification: {exc.orig}"
            ) from exc
        return SourceRegistryOut.model_validate(src)
