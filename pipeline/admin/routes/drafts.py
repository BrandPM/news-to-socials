"""``/api/v1/drafts`` route group — full draft preview + image regenerate."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import func, select

from pipeline.admin import jobs
from pipeline.admin.db import session_scope
from pipeline.admin.models import Brand, CostRecord
from pipeline.admin.schemas import (
    CostBreakdownItem,
    DraftDetailOut,
    ImageRegenerateIn,
    JobAcceptedOut,
    JobStatusOut,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /drafts/{sanity_id} — full preview (NTS_024 open question #1)
# ---------------------------------------------------------------------------


@router.get("/{sanity_draft_id}", response_model=DraftDetailOut)
async def get_draft(
    sanity_draft_id: str,
    brand_id: int = Query(..., description="Active brand id from the UI session"),
) -> DraftDetailOut:
    """Fetch a draft from Sanity using ``brand_id``'s decrypted creds.

    Cross-brand guard: ``generatedBy.brandSlug`` (when present on the
    draft document) must match the active brand's slug — otherwise 403
    "cross-brand draft access not allowed". Prevents Brand A's session
    from peeking at Brand B's drafts.
    """
    with session_scope() as session:
        brand = session.get(Brand, brand_id)
        if brand is None:
            raise HTTPException(status_code=404, detail="brand not found")
        slug = brand.slug
        project_id = brand.sanity_project_id
        dataset = brand.sanity_dataset
        api_version = brand.sanity_api_version or "2024-01-01"
        token_enc = brand.sanity_api_token_enc

    if not project_id or not token_enc:
        raise HTTPException(
            status_code=409,
            detail=f"brand {slug!r} has no Sanity credentials configured",
        )

    if not sanity_draft_id.startswith("drafts."):
        sanity_draft_id_normalised = f"drafts.{sanity_draft_id}"
    else:
        sanity_draft_id_normalised = sanity_draft_id

    from pipeline.admin.encryption import get_encryption  # noqa: PLC0415
    from pipeline.publisher.sanity import SanityClient  # noqa: PLC0415

    try:
        token = get_encryption().decrypt(token_enc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail="could not decrypt Sanity token (bad master key?)",
        ) from exc

    client = SanityClient(
        project_id=project_id,
        dataset=dataset or "production",
        api_version=api_version,
        token=token,
    )
    del token  # M3 — release plaintext reference early

    groq = (
        '*[_id == $id][0]{title, body, keyTakeaway, generatedBy, '
        '_createdAt, "coverImageUrl": coverImage.asset->url}'
    )
    try:
        doc = await client.query(groq, {"id": sanity_draft_id_normalised})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity query failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"draft {sanity_draft_id_normalised!r} not found",
        )

    # --- cross-brand guard: brandSlug on the draft must match active brand ---
    generated_by = doc.get("generatedBy") or {}
    if isinstance(generated_by, dict):
        draft_brand_slug = generated_by.get("brandSlug")
        generated_by_str = generated_by.get("name") or generated_by.get("brandSlug")
    else:
        draft_brand_slug = None
        generated_by_str = str(generated_by) if generated_by else None
    if draft_brand_slug and draft_brand_slug != slug:
        raise HTTPException(
            status_code=403,
            detail="cross-brand draft access not allowed",
        )

    body = doc.get("body")
    # Portable Text → simple markdown-ish join (UI renders rich preview later).
    body_markdown: str | None = None
    if isinstance(body, list):
        chunks: list[str] = []
        for block in body:
            if not isinstance(block, dict):
                continue
            if block.get("_type") == "block":
                style = block.get("style", "normal")
                text_parts = [
                    c.get("text", "")
                    for c in block.get("children", [])
                    if isinstance(c, dict)
                ]
                joined = "".join(text_parts)
                if style == "h2":
                    chunks.append(f"## {joined}")
                elif style == "h3":
                    chunks.append(f"### {joined}")
                else:
                    chunks.append(joined)
        body_markdown = "\n\n".join(chunks)
    elif isinstance(body, str):
        body_markdown = body

    # --- cost data for this draft -----------------------------------------
    total = 0.0
    by_op: dict[str, tuple[float, int]] = {}
    with session_scope() as session:
        rows = session.scalars(
            select(CostRecord).where(
                CostRecord.draft_id == sanity_draft_id_normalised
            )
        ).all()
        for r in rows:
            total += r.cost_usd
            agg = by_op.get(r.operation, (0.0, 0))
            by_op[r.operation] = (agg[0] + r.cost_usd, agg[1] + 1)
    breakdown = [
        CostBreakdownItem(operation=op, cost_usd=round(c, 6), count=n)
        for op, (c, n) in sorted(by_op.items())
    ]

    return DraftDetailOut(
        sanity_id=sanity_draft_id_normalised,
        title=doc.get("title"),
        body_markdown=body_markdown,
        key_takeaway=doc.get("keyTakeaway"),
        cover_image_url=doc.get("coverImageUrl"),
        generated_by=generated_by_str,
        brand_slug=draft_brand_slug or slug,
        created_at=doc.get("_createdAt"),
        cost_total_usd=round(total, 6),
        cost_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Image regenerate (unchanged from S1)
# ---------------------------------------------------------------------------


@router.post(
    "/{sanity_draft_id}/regenerate-image",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_image(
    sanity_draft_id: str,
    payload: ImageRegenerateIn,
    background: BackgroundTasks,
) -> JobAcceptedOut:
    job = jobs.register_image_job()
    background.add_task(
        jobs.execute_image_regenerate,
        job.job_id,
        sanity_draft_id,
        payload.custom_prompt,
    )
    return JobAcceptedOut(job_id=job.job_id)


@router.get("/jobs/{job_id}/status", response_model=JobStatusOut)
def regenerate_image_status(job_id: str) -> JobStatusOut:
    job = jobs.get_image_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusOut(state=job.state, asset_id=job.asset_id, error=job.error)
