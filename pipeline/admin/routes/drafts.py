"""``/api/v1/drafts`` route group — preview + image/text regen + approvals."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import select

from pipeline.admin import jobs
from pipeline.admin.db import session_scope
from pipeline.admin.models import Brand, CostRecord, DraftApproval
from pipeline.admin.schemas import (
    BatchApprovalOut,
    BatchApprovalResult,
    CostBreakdownItem,
    DraftApprovalIn,
    DraftApprovalOut,
    DraftDetailOut,
    DraftListItem,
    DraftListOut,
    ImageRegenerateIn,
    JobAcceptedOut,
    JobStatusOut,
)
from pipeline.common.config import get_settings
from pipeline.common.logging import get_logger

log = get_logger(__name__)

SUPPORTED_LANGUAGES = ("en", "ru", "uk", "pl")

router = APIRouter()


def _normalise_draft_id(sanity_draft_id: str) -> str:
    if not sanity_draft_id.startswith("drafts."):
        return f"drafts.{sanity_draft_id}"
    return sanity_draft_id


def _approval_to_out(row: DraftApproval | None) -> DraftApprovalOut | None:
    if row is None:
        return None
    return DraftApprovalOut(
        status=row.status,  # type: ignore[arg-type]
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        note=row.note,
        published_at=row.published_at,
        sanity_published_id=row.sanity_published_id,
    )


def _upsert_approval(
    sanity_draft_id: str,
    brand_id_fk: int,
    new_status: str,
    note: str | None,
    *,
    published_at: datetime | None = None,
    sanity_published_id: str | None = None,
) -> DraftApproval:
    """Insert or update the approval row for (draft, brand). Returns row.

    ``published_at`` + ``sanity_published_id`` are set when the
    approve path also publishes to Sanity (IT_PROJ_NTS_051 Task 3).
    Leaving them ``None`` preserves prior behaviour for the reject path
    and back-compat callers.
    """
    now = datetime.now(tz=timezone.utc)
    with session_scope() as session:
        row = session.execute(
            select(DraftApproval).where(
                DraftApproval.sanity_draft_id == sanity_draft_id,
                DraftApproval.brand_id_fk == brand_id_fk,
            )
        ).scalar_one_or_none()
        if row is None:
            row = DraftApproval(
                sanity_draft_id=sanity_draft_id,
                brand_id_fk=brand_id_fk,
                status=new_status,
                decided_at=now,
                decided_by="admin",
                note=note,
                published_at=published_at,
                sanity_published_id=sanity_published_id,
            )
            session.add(row)
        else:
            row.status = new_status
            row.decided_at = now
            row.note = note
            if published_at is not None:
                row.published_at = published_at
            if sanity_published_id is not None:
                row.sanity_published_id = sanity_published_id
        session.commit()
        session.refresh(row)
        # Detach to outlive the session
        session.expunge(row)
        return row


def _ensure_brand_owns_draft(brand_id: int) -> Brand:
    """Returns the brand row or raises 404. Brand-scope guard for mutations."""
    with session_scope() as session:
        brand = session.get(Brand, brand_id)
        if brand is None:
            raise HTTPException(status_code=404, detail="brand not found")
        session.expunge(brand)
        return brand


# ---------------------------------------------------------------------------
# GET /drafts — brand-scoped list, language tabs + sibling lookup (S6.7)
# ---------------------------------------------------------------------------


def _build_sanity_client_for_brand(brand_id: int) -> tuple[object, str]:
    """Return ``(SanityClient, brand_slug)`` or raise the right HTTPException.

    Shared by the list endpoint and the detail endpoint so credential
    resolution and brand-existence guards live in one place.
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
    return client, slug


@router.get("", response_model=DraftListOut)
async def list_drafts(
    brand_id: int = Query(..., description="Active brand id from the UI session"),
    language: str | None = Query(
        default=None,
        pattern="^(en|ru|uk|pl)$",
        description="Filter by language (en/ru/uk/pl). Omit for all.",
    ),
    topic_id: str | None = Query(
        default=None,
        description="When set, returns only drafts that share this topicId — used by the /drafts/[id] siblings panel.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DraftListOut:
    """List Sanity drafts for ``brand_id``, with language counts.

    S6.7 powers the multilingual /drafts list. Counts in ``by_language``
    are always brand-wide so the tab strip doesn't jitter as the user
    clicks a filter. ``items`` is the filtered + paginated slice.
    """
    client, brand_slug = _build_sanity_client_for_brand(brand_id)

    # The brand-scope filter relies on the post.ts patch
    # (``generatedBy.brandSlug``). Older posts that lack the field show
    # in the list too (the cross-brand guard on the detail route catches
    # any stray edits). This is the same logic the dedup helper uses.
    base_filter = (
        '_type == "post" && _id in path("drafts.**") && '
        '(generatedBy.brandSlug == $slug || !defined(generatedBy.brandSlug))'
    )
    if topic_id:
        base_filter += " && topicId == $topic"

    selection = (
        "{_id, title, language, topicId, _createdAt, "
        '"coverImageUrl": coverImage.asset->url, '
        '"slug": slug.current}'
    )

    counts_groq = (
        f'*[{base_filter}]{{language}} | '
        '{"by_language": @[].language}'
    )
    # Compose the count query as a faceting GROQ so we get totals in one
    # round-trip alongside the slice.
    counts_groq = (
        '{'
        f'"total": count(*[{base_filter}]),'
        f'"en": count(*[{base_filter} && language == "en"]),'
        f'"ru": count(*[{base_filter} && language == "ru"]),'
        f'"uk": count(*[{base_filter} && language == "uk"]),'
        f'"pl": count(*[{base_filter} && language == "pl"])'
        '}'
    )

    items_filter = base_filter
    if language:
        items_filter += f' && language == "{language}"'
    items_groq = (
        f'*[{items_filter}] | order(_createdAt desc) '
        f'[{offset}...{offset + limit}] {selection}'
    )

    params: dict[str, object] = {"slug": brand_slug}
    if topic_id:
        params["topic"] = topic_id

    try:
        counts = await client.query(counts_groq, params)  # type: ignore[attr-defined]
        rows = await client.query(items_groq, params)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity query failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    by_language: dict[str, int] = {}
    if isinstance(counts, dict):
        for code in SUPPORTED_LANGUAGES:
            by_language[code] = int(counts.get(code) or 0)
        total = int(counts.get("total") or 0)
    else:
        total = 0

    items_raw: list[dict] = rows if isinstance(rows, list) else []
    sanity_ids = [r.get("_id") for r in items_raw if isinstance(r, dict) and r.get("_id")]

    # Bulk-load approval status so the list view renders without N+1.
    approvals: dict[str, str] = {}
    if sanity_ids:
        with session_scope() as session:
            for row in session.scalars(
                select(DraftApproval).where(
                    DraftApproval.brand_id_fk == brand_id,
                    DraftApproval.sanity_draft_id.in_(sanity_ids),
                )
            ):
                approvals[row.sanity_draft_id] = row.status

    items: list[DraftListItem] = []
    for raw in items_raw:
        if not isinstance(raw, dict) or not raw.get("_id"):
            continue
        sid = str(raw["_id"])
        items.append(
            DraftListItem(
                sanity_id=sid,
                title=raw.get("title"),
                language=str(raw.get("language") or "en"),
                topic_id=raw.get("topicId"),
                created_at=raw.get("_createdAt"),
                cover_image_url=raw.get("coverImageUrl"),
                approval_status=approvals.get(sid, "draft"),  # type: ignore[arg-type]
                slug=raw.get("slug"),
            )
        )

    # ``has_more`` reflects pagination of the *filtered* slice, not the
    # brand-wide total — so a language tab knows whether to show
    # "Load more" while the tab strip keeps showing brand-wide counts.
    effective_total = by_language.get(language, 0) if language else total
    return DraftListOut(
        items=items,
        total=total,
        by_language=by_language,
        has_more=offset + len(items) < effective_total,
    )


# ---------------------------------------------------------------------------
# GET /drafts/{sanity_id} — full preview (extended with approval + AI tells)
# ---------------------------------------------------------------------------


@router.get("/{sanity_draft_id}", response_model=DraftDetailOut)
async def get_draft(
    sanity_draft_id: str,
    brand_id: int = Query(..., description="Active brand id from the UI session"),
) -> DraftDetailOut:
    """Fetch a draft from Sanity using ``brand_id``'s decrypted creds.

    Cross-brand guard: ``generatedBy.brandSlug`` (when present on the
    draft document) must match the active brand's slug — otherwise 403
    "cross-brand draft access not allowed".

    Extended in S5 Step 7 with ``approval`` (latest decision row from
    ``draft_approvals``) and ``ai_tells_score`` / ``ai_tells`` (computed
    on the polished body, no extra LLM call).
    """
    client, slug = _build_sanity_client_for_brand(brand_id)

    sanity_draft_id_normalised = _normalise_draft_id(sanity_draft_id)

    groq = (
        '*[_id == $id][0]{title, body, keyTakeaway, generatedBy, '
        'language, topicId, _createdAt, '
        '"coverImageUrl": coverImage.asset->url}'
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

    # --- cross-brand guard ---
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

    # --- AI tells score (no extra LLM cost — pure string analysis) ---
    ai_tells_score: int | None = None
    ai_tells: list[str] = []
    if body_markdown:
        try:
            from pipeline.generator.anti_ai_check import score_ai_tells  # noqa: PLC0415

            score, tells = score_ai_tells(body_markdown)
            ai_tells_score = int(round(score))
            ai_tells = tells
        except Exception:  # noqa: BLE001
            ai_tells_score = None
            ai_tells = []

    # --- cost rollup + approval load ---
    total = 0.0
    by_op: dict[str, tuple[float, int]] = {}
    approval_out: DraftApprovalOut | None = None
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

        approval_row = session.execute(
            select(DraftApproval).where(
                DraftApproval.sanity_draft_id == sanity_draft_id_normalised,
                DraftApproval.brand_id_fk == brand_id,
            )
        ).scalar_one_or_none()
        approval_out = _approval_to_out(approval_row)

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
        language=doc.get("language"),
        topic_id=doc.get("topicId"),
        cost_total_usd=round(total, 6),
        cost_breakdown=breakdown,
        approval=approval_out,
        ai_tells_score=ai_tells_score,
        ai_tells=ai_tells,
    )


# ---------------------------------------------------------------------------
# POST /drafts/{id}/approve and /reject — upsert into draft_approvals
# ---------------------------------------------------------------------------


async def _publish_one_draft(
    sanity_draft_id: str, brand_id: int, note: str | None
) -> tuple[DraftApproval, str | None, str | None]:
    """Approve + publish a single draft. Used by both /approve and the
    /approve-all-siblings batch handler.

    Returns ``(row, published_id, failure_detail)``. ``published_id`` is
    set on Sanity-publish success; ``failure_detail`` is set on
    Sanity-publish failure (the local approval row is still written —
    operator can retry by re-clicking Approve, the upsert lets it).
    """
    from pipeline.publisher.sanity import SanityPublishError  # noqa: PLC0415

    sanity_client, _slug = _build_sanity_client_for_brand(brand_id)
    from pipeline.publisher.sanity import SanityPublisher  # noqa: PLC0415

    publisher = SanityPublisher(client=sanity_client)

    # 1. Record approval. Done first so an interrupted publish still
    #    leaves an audit trail.
    _upsert_approval(sanity_draft_id, brand_id, "approved", note)

    # 2. Publish to Sanity.
    try:
        published_id = await publisher.promote_draft_to_published(
            sanity_draft_id
        )
    except SanityPublishError as exc:
        log.error(
            "approve.publish_failed",
            draft_id=sanity_draft_id,
            err=str(exc),
        )
        # Re-read the row for the response — it's still in "approved" but
        # without published_at/sanity_published_id.
        with session_scope() as session:
            row = session.execute(
                select(DraftApproval).where(
                    DraftApproval.sanity_draft_id == sanity_draft_id,
                    DraftApproval.brand_id_fk == brand_id,
                )
            ).scalar_one()
            session.expunge(row)
        return row, None, str(exc)

    # 3. Record publish completion alongside the existing approval row.
    row = _upsert_approval(
        sanity_draft_id,
        brand_id,
        "approved",
        note,
        published_at=datetime.now(tz=timezone.utc),
        sanity_published_id=published_id,
    )
    return row, published_id, None


@router.post("/{sanity_draft_id}/approve", response_model=DraftApprovalOut)
async def approve_draft(
    sanity_draft_id: str,
    payload: DraftApprovalIn,
    brand_id: int = Query(..., description="Active brand id"),
) -> DraftApprovalOut:
    """Record approval AND publish to Sanity.

    Previously this only inserted a row into ``draft_approvals`` —
    Andriy then had to open Sanity Studio to actually publish. With
    IT_PROJ_NTS_051 Task 3 the approve action does both: write the
    approval, then mutate the draft into a published doc via Sanity's
    REST API. If Sanity rejects the mutate the approval row is still
    saved (operator retries by re-clicking Approve).
    """
    _ensure_brand_owns_draft(brand_id)
    normalised = _normalise_draft_id(sanity_draft_id)
    row, _published_id, failure = await _publish_one_draft(
        normalised, brand_id, payload.note
    )
    if failure is not None:
        # Approval is recorded; surface the publish failure as 502 so
        # the UI can show "approved, publish pending" with detail.
        raise HTTPException(
            status_code=502,
            detail={
                "error": "sanity_publish_failed",
                "reason": failure,
                "approval_status": "approved",
            },
        )
    return _approval_to_out(row)  # type: ignore[return-value]


@router.post(
    "/topic/{topic_id}/approve-all-siblings", response_model=BatchApprovalOut
)
async def approve_all_siblings(
    topic_id: str,
    brand_id: int = Query(..., description="Active brand id"),
) -> BatchApprovalOut:
    """Approve every pending sibling draft for ``topic_id`` and publish each.

    S6.9 added the batch-approve UI but only fired N parallel /approve
    requests. With IT_PROJ_NTS_051 Task 3 the per-draft approve also
    publishes, so we want this one endpoint to drive the whole fanout
    — clearer audit log (one request id) and predictable partial-
    failure reporting.

    Partial failure semantics: an HTTP 502 from a single sibling does
    NOT roll back the siblings that already published. The response
    enumerates per-language ``status`` so the UI toast can show
    "approved 3, 1 pending" rather than "all-or-nothing".
    """
    _ensure_brand_owns_draft(brand_id)
    sanity_client, brand_slug = _build_sanity_client_for_brand(brand_id)

    # Find every pending draft sharing topic_id (skip already-published
    # — those need no work, marked as 'skipped' in the response so the
    # UI can count "X already published, Y now published").
    drafts_groq = (
        '*[_type == "post" && _id in path("drafts.**") && topicId == $tid && '
        '(generatedBy.brandSlug == $slug || !defined(generatedBy.brandSlug))]'
        "{_id, language}"
    )
    try:
        rows = await sanity_client.query(  # type: ignore[attr-defined]
            drafts_groq, {"tid": topic_id, "slug": brand_slug}
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity query failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    if not isinstance(rows, list):
        rows = []

    results: list[BatchApprovalResult] = []
    ok = 0
    fail = 0

    for r in rows:
        if not isinstance(r, dict) or not r.get("_id"):
            continue
        draft_id = str(r["_id"])
        language = str(r.get("language") or "")

        # Skip drafts whose mirror already exists as a published doc.
        # Re-publishing would either no-op (createOrReplace) or
        # potentially overwrite operator edits in Studio — better to
        # leave alone and report it explicitly.
        published_mirror = draft_id[len("drafts.") :]
        try:
            mirror_exists = await sanity_client.query(  # type: ignore[attr-defined]
                "*[_id == $id][0]._id", {"id": published_mirror}
            )
        except Exception:  # noqa: BLE001
            mirror_exists = None
        if mirror_exists:
            results.append(
                BatchApprovalResult(
                    sanity_draft_id=draft_id,
                    language=language,
                    status="skipped",
                    sanity_published_id=published_mirror,
                    detail="already_published",
                )
            )
            continue

        _row, published_id, failure = await _publish_one_draft(
            draft_id, brand_id, None
        )
        if failure is not None:
            fail += 1
            results.append(
                BatchApprovalResult(
                    sanity_draft_id=draft_id,
                    language=language,
                    status="approved_publish_pending",
                    detail=failure,
                )
            )
        else:
            ok += 1
            results.append(
                BatchApprovalResult(
                    sanity_draft_id=draft_id,
                    language=language,
                    status="published",
                    sanity_published_id=published_id,
                )
            )

    return BatchApprovalOut(
        topic_id=topic_id,
        ok_count=ok,
        fail_count=fail,
        results=results,
    )


@router.post("/{sanity_draft_id}/reject", response_model=DraftApprovalOut)
async def reject_draft(
    sanity_draft_id: str,
    payload: DraftApprovalIn,
    brand_id: int = Query(..., description="Active brand id"),
) -> DraftApprovalOut:
    """Record rejection and (optionally) delete the draft from Sanity.

    Deletion is gated on ``DELETE_REJECTED_FROM_SANITY=true`` (default).
    Failure to delete in Sanity does NOT fail the request — the local
    rejection row is the source of truth; the Sanity-side delete is a
    cleanup convenience.
    """
    _ensure_brand_owns_draft(brand_id)
    normalised = _normalise_draft_id(sanity_draft_id)
    row = _upsert_approval(normalised, brand_id, "rejected", payload.note)

    if get_settings().delete_rejected_from_sanity:
        try:
            from pipeline.publisher.sanity import SanityPublisher  # noqa: PLC0415

            sanity_client, _ = _build_sanity_client_for_brand(brand_id)
            publisher = SanityPublisher(client=sanity_client)
            await publisher.delete_draft(normalised)
        except Exception as exc:  # noqa: BLE001
            # delete_draft already swallows internally but if the
            # client build itself fails we still want a successful reject.
            log.warning(
                "reject.delete_failed",
                draft_id=normalised,
                err=f"{type(exc).__name__}: {exc!s}",
            )

    return _approval_to_out(row)  # type: ignore[return-value]


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


# ---------------------------------------------------------------------------
# Text regenerate (S5 Step 7) — re-polish the existing body, patch Sanity
# ---------------------------------------------------------------------------


@router.post(
    "/{sanity_draft_id}/regenerate-text",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_text(
    sanity_draft_id: str,
    background: BackgroundTasks,
    brand_id: int = Query(..., description="Active brand id"),
) -> JobAcceptedOut:
    _ensure_brand_owns_draft(brand_id)
    job = jobs.register_text_job()
    background.add_task(
        jobs.execute_text_regenerate,
        job.job_id,
        _normalise_draft_id(sanity_draft_id),
        brand_id,
    )
    return JobAcceptedOut(job_id=job.job_id)


@router.get("/text-jobs/{job_id}/status", response_model=JobStatusOut)
def regenerate_text_status(job_id: str) -> JobStatusOut:
    job = jobs.get_text_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusOut(state=job.state, asset_id=None, error=job.error)
