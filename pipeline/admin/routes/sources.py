"""``/api/v1/sources`` route group."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pipeline.admin import jobs
from pipeline.admin.db import session_scope
from pipeline.admin.models import Source, Topic
from pipeline.admin.schemas import (
    RunAllIn,
    RunTriggerOut,
    SourceIn,
    SourceOut,
    SourceTestOut,
    SourceUpdate,
)

router = APIRouter()


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
        src.updated_at = datetime.now(tz=timezone.utc)
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

    from pipeline.sources.rss import RssSource  # noqa: PLC0415

    try:
        rss = RssSource(source_id=str(source_id), name=source_name, url=url)
        items = list(await rss.fetch())
    except Exception as exc:  # noqa: BLE001
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
def run_source(source_id: int, background: BackgroundTasks) -> RunTriggerOut:
    with session_scope() as session:
        src = session.get(Source, source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source not found")
        brand_id_fk = src.brand_id_fk
    run_id = jobs.kick_off_pipeline_run(
        brand_id_fk=brand_id_fk, source_ids=[source_id], triggered_by="manual"
    )
    background.add_task(jobs.execute_pipeline_run, run_id)
    return RunTriggerOut(run_id=run_id)


@router.post(
    "/run-all",
    response_model=RunTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_all_sources(payload: RunAllIn, background: BackgroundTasks) -> RunTriggerOut:
    """Schedule the pipeline for every active source of one brand.

    Refuses brands with status != 'active' (M4) — returns 409 so the UI
    can show 'Setup required' instead of silently producing nothing.
    """
    from pipeline.admin.models import Brand  # noqa: PLC0415

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
        triggered_by="manual",
    )
    background.add_task(jobs.execute_pipeline_run, run_id)
    return RunTriggerOut(run_id=run_id)
