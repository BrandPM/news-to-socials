"""``/api/v1/drafts`` route group — preview + image/text regen + approvals."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from pipeline.admin import jobs
from pipeline.admin.db import session_scope
from pipeline.admin.draft_validation import (
    COMPLETENESS_PROJECTION,
    fetch_draft_for_validation,
    validate_draft_complete,
)
from pipeline.admin.models import Brand, CostRecord, DraftApproval, DraftScore, Topic
from pipeline.admin.schemas import (
    BatchApprovalOut,
    BatchApprovalResult,
    CostBreakdownItem,
    DisplayDatePatchIn,
    DisplayDatePatchOut,
    DraftApprovalIn,
    DraftApprovalOut,
    DraftDetailOut,
    DraftScoreOut,
    DraftListItem,
    DraftListOut,
    DraftListSibling,
    DraftStateOut,
    ImageRegenerateIn,
    JobAcceptedOut,
    JobStatusOut,
    PublicationInfoOut,
    PublishedDocOut,
    RejectionInfoOut,
)
from pipeline.common.config import get_settings
from pipeline.common.display_date import compute_published_at, parse_display_date
from pipeline.common.logging import get_logger

log = get_logger(__name__)

SUPPORTED_LANGUAGES = ("en", "ru", "uk", "pl")

router = APIRouter()


def _normalise_draft_id(sanity_draft_id: str) -> str:
    if not sanity_draft_id.startswith("drafts."):
        return f"drafts.{sanity_draft_id}"
    return sanity_draft_id


def purge_draft_local_refs(
    session: Session, sanity_draft_id: str, brand_id: int
) -> dict[str, int]:
    """Remove every admin.db reference to ``sanity_draft_id`` for one brand.

    IT_PROJ_NTS_062. The permanent-delete endpoint deleted the Sanity doc
    but left the local pointers dangling, so a deleted draft lingered as
    desync in three places. This unlinks all of them in a single session
    (the caller's transaction), brand-scoped so we never touch another
    brand's rows:

      * ``topics.draft_id``        → NULL  (keep the run/topic history)
      * ``cost_records.draft_id``  → NULL  (keep the historical spend)
      * ``draft_approvals`` row    → DELETE (the audit decision now points
        at a doc that no longer exists; ``sanity_draft_id`` is NOT NULL so
        we drop the row rather than orphan it)

    Idempotent: matches both the normalised ``drafts.<id>`` form and the
    bare ``<id>`` form, and returns row counts so callers/backfill can
    report exactly what was cleaned. ``cost_records`` and
    ``draft_approvals`` are scoped by their brand FK; ``topics`` has no
    direct brand column but a Sanity draft id is globally unique and owned
    by exactly one brand, so matching on ``draft_id`` alone stays within
    the brand by construction.
    """
    normalised = _normalise_draft_id(sanity_draft_id)
    bare = normalised[len("drafts.") :]
    id_forms = [normalised, bare]

    topics_res = session.execute(
        sa_update(Topic)
        .where(Topic.draft_id.in_(id_forms))
        .values(draft_id=None)
    )
    costs_res = session.execute(
        sa_update(CostRecord)
        .where(
            CostRecord.draft_id.in_(id_forms),
            CostRecord.brand_id_fk == brand_id,
        )
        .values(draft_id=None)
    )
    approvals_res = session.execute(
        sa_delete(DraftApproval).where(
            DraftApproval.sanity_draft_id.in_(id_forms),
            DraftApproval.brand_id_fk == brand_id,
        )
    )
    return {
        "topics": topics_res.rowcount or 0,
        "cost_records": costs_res.rowcount or 0,
        "draft_approvals": approvals_res.rowcount or 0,
    }


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


def _latest_scores(session, draft_ids: list[str]) -> dict[str, DraftScore]:
    """Latest ``draft_scores`` row per draft_id (NTS_091). Bulk to avoid N+1."""
    out: dict[str, DraftScore] = {}
    if not draft_ids:
        return out
    for row in session.scalars(
        select(DraftScore)
        .where(DraftScore.draft_id.in_(draft_ids))
        .order_by(DraftScore.created_at.desc())
    ):
        # First seen per draft_id wins (query is newest-first).
        if row.draft_id not in out:
            out[row.draft_id] = row
            session.expunge(row)
    return out


def _score_to_out(row: DraftScore | None) -> DraftScoreOut | None:
    if row is None:
        return None
    import json as _json  # noqa: PLC0415

    try:
        rubric = _json.loads(row.rubric_json or "{}")
    except (ValueError, TypeError):
        rubric = {}
    return DraftScoreOut(
        total=row.total,
        flagged=row.flagged,
        model=row.model,
        judge_prompt_version=row.judge_prompt_version,
        axes={k: float(v) for k, v in (rubric.get("axes") or {}).items()},
        feedback=rubric.get("feedback"),
        worst_axis=rubric.get("worst_axis"),
        banned_hits=list(rubric.get("banned_hits") or []),
        created_at=row.created_at,
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


def _status_filter_clause(status: str) -> str:
    """GROQ fragment that scopes ``*[…]`` to one of the three tabs.

    Sanity stores rejection as a ``status`` field on the draft document
    (IT_PROJ_NTS_052). Pending is encoded by *absence* of that field
    (back-compat with every draft written before this commit) OR an
    explicit ``status == "pending"``. Published is the absence of the
    ``drafts.`` prefix entirely.
    """
    if status == "pending":
        return (
            '_type == "post" && _id in path("drafts.**") && '
            '(!defined(status) || status == "pending")'
        )
    if status == "published":
        # ``!(_id in path("drafts.**"))`` matches non-drafts; pair with
        # the type guard so non-post documents don't bleed in.
        return '_type == "post" && !(_id in path("drafts.**"))'
    if status == "rejected":
        return (
            '_type == "post" && _id in path("drafts.**") && '
            'status == "rejected"'
        )
    raise HTTPException(
        status_code=422,
        detail=f"invalid status {status!r}; expected pending|published|rejected",
    )


def _selection_for_status(status: str) -> str:
    """GROQ projection per tab.

    Pending / rejected stay on the ``drafts.*`` shape (no slug yet on
    fresh drafts, but populated by NTS_051 backfill).  The published
    branch also pulls ``slug.current`` for the ``Open live`` link
    construction. ``rejection`` block is selected for rejected items so
    the card UI can render the timestamp + reason inline.
    """
    base = (
        "{_id, title, language, topicId, _createdAt, displayDate, publishedAt, "
        '"coverImageUrl": coverImage.asset->url, '
        # NTS_090 — completeness inputs. Counted by Sanity so a 50-row page
        # costs two integers per row instead of 50 article bodies.
        '"coverImageRef": coverImage.asset._ref, '
        '"bodyBlockCount": count(body), '
        '"bodyH2Count": count(body[style == "h2"]), '
        '"slug": slug.current'
    )
    if status == "rejected":
        base += ', "rejectedAt": rejectedAt, "rejectionReason": rejectionReason'
    base += "}"
    return base


@router.get("", response_model=DraftListOut)
async def list_drafts(
    brand_id: int = Query(..., description="Active brand id from the UI session"),
    status: str = Query(
        default="pending",
        pattern="^(pending|published|rejected)$",
        description="Content hub tab — Pending, Published or Rejected.",
    ),
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
    sort: str = Query(
        default="recent",
        pattern="^(recent|score_asc|score_desc)$",
        description="NTS_091 — 'score_asc' (weakest first) / 'score_desc' sorts the page by judge score; 'recent' keeps _createdAt order.",
    ),
) -> DraftListOut:
    """List Sanity drafts for ``brand_id``, scoped by Content-hub status.

    IT_PROJ_NTS_052 wraps S6.7's multilingual list with a three-tab
    breakdown (Pending / Published / Rejected). Each tab issues a
    distinct GROQ — see :func:`_status_filter_clause` — so we never
    confuse "rejected and kept in Sanity" with "deleted from Sanity".

    Counts:
    * ``by_language`` covers the brand-wide pending set (back-compat
      with the existing language-tab strip on /drafts).
    * ``by_status`` covers the active language filter so the three-tab
      counts reflect what the operator is currently looking at.

    ``items`` is the filtered + paginated slice for the active tab.
    """
    client, brand_slug = _build_sanity_client_for_brand(brand_id)

    # Brand scope: brandSlug match OR legacy docs without the field.
    brand_clause = (
        "(generatedBy.brandSlug == $slug || !defined(generatedBy.brandSlug))"
    )

    base_filter = f"{_status_filter_clause(status)} && {brand_clause}"
    if topic_id:
        base_filter += " && topicId == $topic"

    # Per-status count breakdown for the three Content-hub tabs. Honours
    # the current language filter so EN-only viewers see EN-only counts.
    lang_clause = f' && language == "{language}"' if language else ""
    pending_clause = _status_filter_clause("pending")
    published_clause = _status_filter_clause("published")
    rejected_clause = _status_filter_clause("rejected")
    counts_groq = (
        "{"
        f'"total": count(*[{base_filter}]),'
        f'"pending": count(*[{pending_clause} && {brand_clause}{lang_clause}]),'
        f'"published": count(*[{published_clause} && {brand_clause}{lang_clause}]),'
        f'"rejected": count(*[{rejected_clause} && {brand_clause}{lang_clause}]),'
        # by_language stays scoped to the active tab so the language
        # strip lines up with the tab the operator is on.
        f'"en": count(*[{base_filter} && language == "en"]),'
        f'"ru": count(*[{base_filter} && language == "ru"]),'
        f'"uk": count(*[{base_filter} && language == "uk"]),'
        f'"pl": count(*[{base_filter} && language == "pl"])'
        "}"
    )

    items_filter = base_filter
    if language:
        items_filter += f' && language == "{language}"'
    items_groq = (
        f'*[{items_filter}] | order(_createdAt desc) '
        f'[{offset}...{offset + limit}] {_selection_for_status(status)}'
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
    by_status: dict[str, int] = {}
    if isinstance(counts, dict):
        for code in SUPPORTED_LANGUAGES:
            by_language[code] = int(counts.get(code) or 0)
        for key in ("pending", "published", "rejected"):
            by_status[key] = int(counts.get(key) or 0)
        total = int(counts.get("total") or 0)
    else:
        total = 0

    items_raw: list[dict] = rows if isinstance(rows, list) else []
    sanity_ids = [r.get("_id") for r in items_raw if isinstance(r, dict) and r.get("_id")]

    # Bulk-load approval status so the list view renders without N+1.
    approvals: dict[str, str] = {}
    approval_rows: dict[str, DraftApproval] = {}
    scores: dict[str, DraftScore] = {}
    if sanity_ids:
        with session_scope() as session:
            for row in session.scalars(
                select(DraftApproval).where(
                    DraftApproval.brand_id_fk == brand_id,
                    DraftApproval.sanity_draft_id.in_(sanity_ids),
                )
            ):
                approvals[row.sanity_draft_id] = row.status
                session.expunge(row)
                approval_rows[row.sanity_draft_id] = row
            # NTS_091 — latest judge score per draft, for the badge + sort.
            scores = _latest_scores(session, list(sanity_ids))

    # Sibling lookup: one extra GROQ for every topic the current page
    # touches, returning the language + status mix per topic. Bulk so the
    # response stays one-round-trip-ish even on a 50-item page.
    siblings_by_topic: dict[str, list[dict]] = {}
    topic_ids = sorted({
        str(r.get("topicId")) for r in items_raw
        if isinstance(r, dict) and r.get("topicId")
    })
    if topic_ids:
        siblings_groq = (
            '*[_type == "post" && topicId in $topics && '
            f"{brand_clause}]"
            '{_id, language, topicId, '
            '"isDraft": _id in path("drafts.**"), '
            '"rejected": defined(status) && status == "rejected"}'
        )
        try:
            sib_rows = await client.query(  # type: ignore[attr-defined]
                siblings_groq, {"slug": brand_slug, "topics": topic_ids}
            )
        except Exception:  # noqa: BLE001
            sib_rows = []
        if isinstance(sib_rows, list):
            for s in sib_rows:
                if not isinstance(s, dict):
                    continue
                tid = str(s.get("topicId") or "")
                if not tid:
                    continue
                siblings_by_topic.setdefault(tid, []).append(s)

    items: list[DraftListItem] = []
    for raw in items_raw:
        if not isinstance(raw, dict) or not raw.get("_id"):
            continue
        sid = str(raw["_id"])
        approval_row = approval_rows.get(sid)

        slug_val = raw.get("slug")
        live_url: str | None = None
        published_at = None
        rejected_at = None
        rejection_reason = None
        if status == "published":
            live_url = _build_live_url(
                brand_slug, str(raw.get("language") or "en"), slug_val
            )
            if approval_row is not None:
                published_at = approval_row.published_at
        elif status == "rejected":
            # Prefer Sanity-side timestamp/reason (source of truth for
            # the doc), fall back to the local DraftApproval row.
            rejected_at = raw.get("rejectedAt")
            rejection_reason = raw.get("rejectionReason")
            if approval_row is not None:
                if rejected_at is None:
                    rejected_at = approval_row.decided_at
                if rejection_reason is None:
                    rejection_reason = approval_row.note

        # Siblings: drop the row itself, normalise status.
        tid = raw.get("topicId")
        sibs: list[DraftListSibling] = []
        if tid:
            for s in siblings_by_topic.get(str(tid), []):
                if s.get("_id") == sid:
                    continue
                if s.get("isDraft") and s.get("rejected"):
                    s_status: str = "rejected"
                elif s.get("isDraft"):
                    s_status = "pending"
                else:
                    s_status = "published"
                sibs.append(
                    DraftListSibling(
                        sanity_id=str(s.get("_id")),
                        language=str(s.get("language") or "en"),
                        status=s_status,  # type: ignore[arg-type]
                    )
                )

        _score_row = scores.get(sid)
        items.append(
            DraftListItem(
                sanity_id=sid,
                title=raw.get("title"),
                language=str(raw.get("language") or "en"),
                topic_id=raw.get("topicId"),
                created_at=raw.get("_createdAt"),
                cover_image_url=raw.get("coverImageUrl"),
                approval_status=approvals.get(sid, "draft"),  # type: ignore[arg-type]
                slug=slug_val,
                status=status,  # type: ignore[arg-type]
                published_at=published_at,
                display_date=raw.get("displayDate"),
                score_total=_score_row.total if _score_row else None,
                score_flagged=_score_row.flagged if _score_row else False,
                rejected_at=rejected_at,
                rejection_reason=rejection_reason,
                live_url=live_url,
                siblings=sibs,
                missing=validate_draft_complete(raw),
            )
        )

    # NTS_091 — optional score sort of the fetched page (weakest/strongest
    # first). Items without a score sort last regardless of direction so the
    # scored ones lead. Pagination stays _createdAt-based in GROQ; this
    # reorders the current page for the "start with the weak ones" workflow.
    if sort in ("score_asc", "score_desc"):
        _miss = float("inf") if sort == "score_asc" else float("-inf")
        items.sort(
            key=lambda it: it.score_total if it.score_total is not None else _miss,
            reverse=(sort == "score_desc"),
        )

    # ``has_more`` reflects pagination of the *filtered* slice, not the
    # brand-wide total — so a language tab knows whether to show
    # "Load more" while the tab strip keeps showing brand-wide counts.
    effective_total = by_language.get(language, 0) if language else total
    return DraftListOut(
        items=items,
        total=total,
        by_language=by_language,
        by_status=by_status,
        has_more=offset + len(items) < effective_total,
    )


# ---------------------------------------------------------------------------
# GET /drafts/{sanity_id} — lifecycle-aware (draft / published / both)
# ---------------------------------------------------------------------------


def _portable_text_to_markdown(body: object) -> str | None:
    """Flatten Sanity portable-text into the same lightweight markdown the
    admin preview renderer understands. Returns ``None`` if there is no
    body content to flatten. Extracted so the published-doc branch can
    skip it cleanly (the success view has no body).
    """
    if isinstance(body, str):
        return body
    if not isinstance(body, list):
        return None
    chunks: list[str] = []
    for block in body:
        if not isinstance(block, dict):
            continue
        if block.get("_type") != "block":
            continue
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
    return "\n\n".join(chunks) if chunks else None


def _build_live_url(
    brand_slug: str, language: str | None, slug: str | None
) -> str | None:
    """Public URL for the published post. Currently only Icon Finance has
    one — other brands return ``None`` rather than guessing a domain
    we can't verify. Worth promoting to a Brand column when a second
    brand needs the link.

    Canonical shape is ``{base}/{language}/insights/{slug}`` for every
    language (including ``en`` → ``/en/insights/<slug>``); the slug is
    already localised per-language in Sanity. The base domain comes from
    ``settings.public_site_base_url`` (NTS_075) — no hardcoded domains.
    """
    if not slug or not language:
        return None
    if brand_slug == "icon":
        base = get_settings().public_site_base_url.rstrip("/")
        return f"{base}/{language}/insights/{slug}"
    return None


def _build_draft_detail_out(
    doc: dict,
    sanity_draft_id_normalised: str,
    brand_id: int,
    fallback_brand_slug: str,
) -> DraftDetailOut:
    """Build the full DraftDetailOut from a Sanity draft doc. Includes the
    AI-tells score + cost rollup + approval row — the ``draft_only`` /
    ``both`` view needs all of it."""
    generated_by = doc.get("generatedBy") or {}
    if isinstance(generated_by, dict):
        draft_brand_slug = generated_by.get("brandSlug")
        generated_by_str = generated_by.get("name") or generated_by.get(
            "brandSlug"
        )
    else:
        draft_brand_slug = None
        generated_by_str = str(generated_by) if generated_by else None

    body_markdown = _portable_text_to_markdown(doc.get("body"))

    ai_tells_score: int | None = None
    ai_tells: list[str] = []
    if body_markdown:
        try:
            from pipeline.generator.anti_ai_check import (  # noqa: PLC0415
                score_ai_tells,
            )

            score, tells = score_ai_tells(body_markdown)
            ai_tells_score = int(round(score))
            ai_tells = tells
        except Exception:  # noqa: BLE001
            ai_tells_score = None
            ai_tells = []

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
        brand_slug=draft_brand_slug or fallback_brand_slug,
        created_at=doc.get("_createdAt"),
        language=doc.get("language"),
        topic_id=doc.get("topicId"),
        cost_total_usd=round(total, 6),
        cost_breakdown=breakdown,
        approval=approval_out,
        ai_tells_score=ai_tells_score,
        ai_tells=ai_tells,
        display_date=doc.get("displayDate"),
        missing=validate_draft_complete(doc),
    )


@router.get("/{sanity_draft_id}", response_model=DraftStateOut)
async def get_draft(
    sanity_draft_id: str,
    brand_id: int = Query(..., description="Active brand id from the UI session"),
) -> DraftStateOut:
    """Fetch a draft + its published mirror from Sanity in one round-trip.

    Returns a Content-hub lifecycle envelope so the admin UI can pick
    the right per-state view:

    * ``pending``   — ``drafts.{id}`` exists, not flagged rejected. UI
                       shows the preview + approve/reject controls.
                       ``publication_info`` is populated iff a published
                       mirror also exists (rare re-edit case → warning).
    * ``published`` — only the published doc exists; the approve chain
                       deleted ``drafts.{id}`` (NTS_051). UI shows the
                       success card with live + Studio deep-links.
    * ``rejected``  — ``drafts.{id}`` exists with ``status='rejected'``.
                       UI shows the rejection panel with Restore / Delete
                       permanently.
    * ``neither``   — genuine 404 (typo, hard-deleted in Studio).

    Cross-brand guard: ``generatedBy.brandSlug`` on either doc, when
    present, must match the active brand's slug — otherwise 403.
    """
    client, slug = _build_sanity_client_for_brand(brand_id)

    sanity_draft_id_normalised = _normalise_draft_id(sanity_draft_id)
    sanity_published_id = sanity_draft_id_normalised[len("drafts.") :]

    # Single round-trip for both docs. Sanity returns null per branch
    # when nothing matches the predicate. Pull ``status`` + ``rejectedAt``
    # on the draft branch so the Content-hub rejected-state view has
    # everything it needs without a second query.
    groq = (
        "{"
        '"draft": *[_id == $draft_id][0]{title, body, keyTakeaway, '
        'generatedBy, language, topicId, _createdAt, status, '
        'rejectedAt, rejectionReason, rejectedBy, displayDate, '
        # NTS_090 — completeness inputs for the "why can't I publish" banner.
        # ``body`` is already selected above, so the validator counts blocks
        # locally; the cover needs the raw ref (the resolved URL is null both
        # when there is no cover AND when the asset itself is missing).
        '"slug": slug.current, '
        '"coverImageRef": coverImage.asset._ref, '
        '"coverImageUrl": coverImage.asset->url},'
        '"published": *[_id == $pub_id][0]{_id, title, language, '
        '_createdAt, _updatedAt, generatedBy, '
        '"slug": slug.current, '
        '"coverImageUrl": coverImage.asset->url}'
        "}"
    )
    try:
        combined = await client.query(
            groq,
            {
                "draft_id": sanity_draft_id_normalised,
                "pub_id": sanity_published_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity query failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    if not isinstance(combined, dict):
        combined = {}
    draft_doc = combined.get("draft")
    published_doc = combined.get("published")

    # Determine lifecycle state.
    has_draft = isinstance(draft_doc, dict) and bool(draft_doc)
    has_published = isinstance(published_doc, dict) and bool(published_doc)
    is_rejected = (
        has_draft
        and isinstance(draft_doc, dict)
        and draft_doc.get("status") == "rejected"
    )

    if not has_draft and not has_published:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no draft or published document found for "
                f"{sanity_published_id!r} under brand {slug!r}"
            ),
        )

    if is_rejected:
        state: str = "rejected"
    elif has_draft:
        # The re-edit "both" case also lands here: state stays "pending"
        # but ``publication_info`` is populated below so the UI can show
        # the "Editing published post" warning.
        state = "pending"
    else:
        state = "published"

    # Cross-brand guard. If either doc has a brandSlug set and it does
    # not match, refuse — same rule that protected the legacy endpoint.
    for doc in (draft_doc, published_doc):
        if not isinstance(doc, dict):
            continue
        gb = doc.get("generatedBy")
        if isinstance(gb, dict) and gb.get("brandSlug") and gb.get(
            "brandSlug"
        ) != slug:
            raise HTTPException(
                status_code=403,
                detail="cross-brand draft access not allowed",
            )

    draft_out: DraftDetailOut | None = None
    if has_draft:
        draft_out = _build_draft_detail_out(
            draft_doc,  # type: ignore[arg-type]
            sanity_draft_id_normalised,
            brand_id,
            fallback_brand_slug=slug,
        )

    published_out: PublishedDocOut | None = None
    publication_info: PublicationInfoOut | None = None
    if has_published:
        pub_generated_by = published_doc.get("generatedBy")  # type: ignore[union-attr]
        pub_brand_slug = (
            pub_generated_by.get("brandSlug")
            if isinstance(pub_generated_by, dict)
            else None
        ) or slug
        pub_slug = published_doc.get("slug")  # type: ignore[union-attr]
        published_out = PublishedDocOut(
            sanity_id=str(published_doc.get("_id") or sanity_published_id),  # type: ignore[union-attr]
            title=published_doc.get("title"),  # type: ignore[union-attr]
            slug=pub_slug,
            language=published_doc.get("language"),  # type: ignore[union-attr]
            cover_image_url=published_doc.get("coverImageUrl"),  # type: ignore[union-attr]
            brand_slug=pub_brand_slug,
            updated_at=published_doc.get("_updatedAt"),  # type: ignore[union-attr]
        )

        # ``draft_approvals`` is keyed on ``drafts.{id}`` (the value at
        # approve-time). Load it for the published view's timestamps and
        # approver name.
        with session_scope() as session:
            approval_row = session.execute(
                select(DraftApproval).where(
                    DraftApproval.sanity_draft_id
                    == sanity_draft_id_normalised,
                    DraftApproval.brand_id_fk == brand_id,
                )
            ).scalar_one_or_none()
            if approval_row is not None:
                session.expunge(approval_row)

        publication_info = PublicationInfoOut(
            sanity_published_id=sanity_published_id,
            published_at=approval_row.published_at if approval_row else None,
            approver=approval_row.decided_by if approval_row else None,
            note=approval_row.note if approval_row else None,
            live_url=_build_live_url(
                pub_brand_slug,
                str(published_doc.get("language") or "en"),  # type: ignore[union-attr]
                pub_slug,
            ),
        )

    rejection_info: RejectionInfoOut | None = None
    if is_rejected and isinstance(draft_doc, dict):
        # Prefer Sanity-side fields (the doc IS the source of truth for
        # the rejection record); fall back to the DraftApproval row
        # written at reject-time.
        rejected_at_raw = draft_doc.get("rejectedAt")
        rejected_at_parsed: datetime | None = None
        if isinstance(rejected_at_raw, str) and rejected_at_raw:
            try:
                rejected_at_parsed = datetime.fromisoformat(
                    rejected_at_raw.replace("Z", "+00:00")
                )
            except ValueError:
                rejected_at_parsed = None
        reason = draft_doc.get("rejectionReason")
        rejected_by = draft_doc.get("rejectedBy")
        if rejected_at_parsed is None or reason is None or rejected_by is None:
            with session_scope() as session:
                row = session.execute(
                    select(DraftApproval).where(
                        DraftApproval.sanity_draft_id
                        == sanity_draft_id_normalised,
                        DraftApproval.brand_id_fk == brand_id,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    if rejected_at_parsed is None:
                        rejected_at_parsed = row.decided_at
                    if reason is None:
                        reason = row.note
                    if rejected_by is None:
                        rejected_by = row.decided_by
        rejection_info = RejectionInfoOut(
            rejected_at=rejected_at_parsed,
            reason=reason,
            rejected_by=rejected_by,
        )

    # NTS_091 — attach the latest judge score for this draft (its language).
    score_out: DraftScoreOut | None = None
    with session_scope() as session:
        score_out = _score_to_out(
            _latest_scores(session, [sanity_draft_id_normalised]).get(
                sanity_draft_id_normalised
            )
        )

    return DraftStateOut(
        sanity_id=sanity_published_id,
        state=state,  # type: ignore[arg-type]
        draft=draft_out,
        published=published_out,
        publication_info=publication_info,
        rejection_info=rejection_info,
        score=score_out,
    )


# ---------------------------------------------------------------------------
# POST /drafts/{id}/approve and /reject — upsert into draft_approvals
# ---------------------------------------------------------------------------


async def _publish_one_draft(
    sanity_draft_id: str,
    brand_id: int,
    note: str | None,
    published_at: datetime | None = None,
) -> tuple[DraftApproval, str | None, str | None]:
    """Approve + publish a single draft. Used by both /approve and the
    /approve-all-siblings batch handler.

    ``published_at`` (NTS_089) is the display-date-derived stamp for the
    site's ``publishedAt``; the batch handler passes one shared value so all
    language siblings publish under the identical date. When ``None`` the
    publisher computes it from this draft's own ``displayDate``.

    Returns ``(row, published_id, failure_detail)``. ``published_id`` is
    set on Sanity-publish success; ``failure_detail`` is set on
    Sanity-publish failure (the local approval row is still written —
    operator can retry by re-clicking Approve, the upsert lets it).
    """
    from pipeline.publisher.sanity import SanityPublishError  # noqa: PLC0415

    sanity_client, _slug = _build_sanity_client_for_brand(brand_id)
    from pipeline.publisher.sanity import SanityPublisher  # noqa: PLC0415

    publisher = SanityPublisher(client=sanity_client)

    # 0. NTS_090 — completeness guard. Authoritative and server-side: it sits
    #    on the only code path that promotes a draft, so a direct API call, a
    #    double-click or a stale UI cannot get around it. Runs BEFORE the
    #    approval row is written and before any Sanity write, so a blocked
    #    draft is left exactly as it was. A doc that Sanity cannot resolve at
    #    all is not reported here — ``promote_draft_to_published`` raises its
    #    own "draft not found", which is the more accurate error.
    try:
        guard_doc = await fetch_draft_for_validation(sanity_client, sanity_draft_id)
    except Exception as exc:  # noqa: BLE001
        # Fail closed: an unverifiable draft is not a publishable draft.
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not verify draft completeness: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        ) from exc
    if guard_doc is not None:
        missing = validate_draft_complete(guard_doc)
        if missing:
            log.warning(
                "approve.blocked_incomplete",
                draft_id=sanity_draft_id,
                missing=missing,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "draft_incomplete",
                    "sanity_id": sanity_draft_id,
                    "language": guard_doc.get("language"),
                    "missing": missing,
                },
            )

    # 1. Record approval. Done first so an interrupted publish still
    #    leaves an audit trail.
    _upsert_approval(sanity_draft_id, brand_id, "approved", note)

    # 2. Publish to Sanity.
    try:
        published_id = await publisher.promote_draft_to_published(
            sanity_draft_id, published_at=published_at
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
    # The projection doubles as the completeness input (NTS_090) so the batch
    # guard costs no extra round-trip — ``COMPLETENESS_PROJECTION`` already
    # carries _id / language / displayDate.
    drafts_groq = (
        '*[_type == "post" && _id in path("drafts.**") && topicId == $tid && '
        '(generatedBy.brandSlug == $slug || !defined(generatedBy.brandSlug))]'
        f"{COMPLETENESS_PROJECTION}"
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

    # NTS_090 — validate EVERY sibling before promoting ANY of them. The
    # siblings ship as one unit (they share a cover and one publishedAt), so
    # a half-published topic with one language missing its cover is exactly
    # the state this guard exists to prevent. All-or-nothing, before the
    # first Sanity write.
    missing_by_language: dict[str, list[str]] = {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("_id"):
            continue
        sibling_missing = validate_draft_complete(r)
        if sibling_missing:
            lang = str(r.get("language") or r.get("_id"))
            missing_by_language[lang] = sibling_missing
    if missing_by_language:
        log.warning(
            "approve_all.blocked_incomplete",
            topic_id=topic_id,
            missing_by_language=missing_by_language,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "error": "topic_incomplete",
                "topic_id": topic_id,
                "missing_by_language": missing_by_language,
            },
        )

    # NTS_089 — one shared publishedAt for every sibling of the topic, so all
    # languages publish under the identical date (same intent as the shared
    # cover image). Siblings share the same displayDate by construction; take
    # the first non-empty as the topic's date.
    shared_display_date = next(
        (
            r.get("displayDate")
            for r in rows
            if isinstance(r, dict) and r.get("displayDate")
        ),
        None,
    )
    shared_published_at = compute_published_at(
        shared_display_date, datetime.now(tz=timezone.utc)
    )

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
            draft_id, brand_id, None, published_at=shared_published_at
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


@router.patch(
    "/{sanity_draft_id}/display-date", response_model=DisplayDatePatchOut
)
async def patch_display_date(
    sanity_draft_id: str,
    payload: DisplayDatePatchIn,
    brand_id: int = Query(..., description="Active brand id"),
) -> DisplayDatePatchOut:
    """Set the displayed publication date on a draft + all its language
    siblings (NTS_089).

    One date per topic — the same shared-mutate pattern as the cover image
    (NTS_069): editing the date on any language card updates every pending
    sibling in a single Sanity transaction, so the topic never ends up with
    mismatched dates. Future dates are rejected (scheduled publishing is out
    of scope — NTS_085). Only pending ``drafts.*`` docs are patched; a
    published post's date is fixed at publish time.
    """
    _ensure_brand_owns_draft(brand_id)
    parsed = parse_display_date(payload.display_date)
    if parsed is None:
        raise HTTPException(
            status_code=422, detail="display_date must be YYYY-MM-DD"
        )
    if parsed > datetime.now(tz=timezone.utc).date():
        raise HTTPException(
            status_code=422,
            detail="display_date cannot be in the future (scheduled publishing is out of scope)",
        )

    normalised = _normalise_draft_id(sanity_draft_id)
    client, brand_slug = _build_sanity_client_for_brand(brand_id)

    # Resolve the topic, then every pending sibling draft under it.
    try:
        doc = await client.query(  # type: ignore[attr-defined]
            "*[_id == $id][0]{topicId}", {"id": normalised}
        )
        topic_id = doc.get("topicId") if isinstance(doc, dict) else None
        if topic_id:
            sib_groq = (
                '*[_type == "post" && _id in path("drafts.**") && '
                "topicId == $tid && (generatedBy.brandSlug == $slug || "
                "!defined(generatedBy.brandSlug))]._id"
            )
            ids = await client.query(  # type: ignore[attr-defined]
                sib_groq, {"tid": topic_id, "slug": brand_slug}
            )
            target_ids = (
                [str(i) for i in ids if i] if isinstance(ids, list) else []
            )
        else:
            # No topicId (legacy draft) — patch just this doc if it exists.
            target_ids = [normalised] if isinstance(doc, dict) and doc else []
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity query failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    if not target_ids:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no pending draft found for {normalised!r} — a published "
                "post's date is fixed at publish time"
            ),
        )

    iso = parsed.isoformat()
    mutations = [
        {"patch": {"id": tid, "set": {"displayDate": iso}}} for tid in target_ids
    ]
    try:
        await client.mutate(mutations)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity mutate failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    log.info(
        "draft.display_date_patched",
        draft_id=normalised,
        display_date=iso,
        siblings=len(target_ids),
    )
    return DisplayDatePatchOut(display_date=iso, updated_draft_ids=target_ids)


@router.post("/{sanity_draft_id}/reject", response_model=DraftApprovalOut)
async def reject_draft(
    sanity_draft_id: str,
    payload: DraftApprovalIn,
    brand_id: int = Query(..., description="Active brand id"),
) -> DraftApprovalOut:
    """Record rejection AND mark the Sanity draft ``status='rejected'``.

    IT_PROJ_NTS_052 Content hub: rejection no longer deletes the
    document. Instead we PATCH ``drafts.{id}`` to add
    ``status: "rejected"`` + ``rejectedAt`` (and ``rejectionReason``
    when the operator provided a note). The Rejected tab queries on
    that field; Restore unsets it. The legacy delete path is kept
    behind ``DELETE_REJECTED_FROM_SANITY=true`` for environments that
    explicitly want the old behaviour (default now ``false``).

    Sanity-side write failures don't fail the request — the local
    DraftApproval row is the source of truth for audit, the Sanity
    patch is the consumable signal for the UI's Rejected tab.
    """
    _ensure_brand_owns_draft(brand_id)
    normalised = _normalise_draft_id(sanity_draft_id)
    row = _upsert_approval(normalised, brand_id, "rejected", payload.note)

    settings = get_settings()
    sanity_client, _ = _build_sanity_client_for_brand(brand_id)
    from pipeline.publisher.sanity import SanityPublisher  # noqa: PLC0415

    publisher = SanityPublisher(client=sanity_client)

    if settings.delete_rejected_from_sanity:
        # Back-compat: legacy NTS_051 path — hard delete.
        try:
            await publisher.delete_draft(normalised)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "reject.delete_failed",
                draft_id=normalised,
                err=f"{type(exc).__name__}: {exc!s}",
            )
    else:
        # NTS_052 default: PATCH ``status`` + ``rejectedAt`` so the doc
        # stays in Sanity for audit / Restore. Best-effort: failures log
        # but do not bubble — the local approval row is authoritative.
        set_fields: dict[str, object] = {
            "status": "rejected",
            "rejectedAt": row.decided_at.isoformat(),
            "rejectedBy": row.decided_by,
        }
        if payload.note:
            set_fields["rejectionReason"] = payload.note
        try:
            await sanity_client.patch(normalised, set_fields=set_fields)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "reject.patch_failed",
                draft_id=normalised,
                err=f"{type(exc).__name__}: {exc!s}",
            )

    return _approval_to_out(row)  # type: ignore[return-value]


@router.post("/{sanity_draft_id}/unreject", response_model=DraftApprovalOut)
async def unreject_draft(
    sanity_draft_id: str,
    brand_id: int = Query(..., description="Active brand id"),
) -> DraftApprovalOut:
    """Move a rejected draft back to pending.

    IT_PROJ_NTS_052 Content hub: the Rejected tab's "Restore" action
    clears the Sanity-side rejection flag and resets the local approval
    row. After this call the draft is identical to one that was never
    rejected — it shows up in the Pending tab and the approve/reject
    controls are live again.
    """
    _ensure_brand_owns_draft(brand_id)
    normalised = _normalise_draft_id(sanity_draft_id)

    # Reset the DB row to ``draft`` so audit history is preserved (the
    # row stays, ``decided_at`` updates) but the tab logic flips.
    row = _upsert_approval(normalised, brand_id, "draft", None)

    sanity_client, _ = _build_sanity_client_for_brand(brand_id)
    try:
        await sanity_client.patch(
            normalised,
            unset_fields=[
                "status",
                "rejectedAt",
                "rejectionReason",
                "rejectedBy",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "unreject.patch_failed",
            draft_id=normalised,
            err=f"{type(exc).__name__}: {exc!s}",
        )

    return _approval_to_out(row)  # type: ignore[return-value]


@router.delete("/{sanity_draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def permanently_delete_draft(
    sanity_draft_id: str,
    brand_id: int = Query(..., description="Active brand id"),
) -> None:
    """Permanently remove a rejected draft from Sanity.

    IT_PROJ_NTS_052 Content hub explicit-cleanup action. Safety gate:
    only documents currently flagged ``status='rejected'`` can be
    permanently deleted via this endpoint — any other state returns
    400 so an operator can't accidentally lose a pending or published
    document by hitting Delete in the wrong UI.

    Atomic + idempotent (IT_PROJ_NTS_062). A single Delete snips the doc
    out of Sanity AND every local pointer to it (``topics.draft_id``,
    ``cost_records.draft_id``, the ``draft_approvals`` audit row) so no
    desync survives. If Sanity already has no such doc (already deleted,
    or a stale UI hitting Delete twice) that is NOT an error: we skip the
    Sanity mutation, still purge the local refs, and return 204. The local
    cleanup is committed only after the Sanity side has settled, so we
    never orphan the doc by failing the DB write first.
    """
    _ensure_brand_owns_draft(brand_id)
    normalised = _normalise_draft_id(sanity_draft_id)
    sanity_client, _ = _build_sanity_client_for_brand(brand_id)

    # Guard: re-read the Sanity doc and confirm it's rejected before
    # deleting. We do NOT trust the local DB row — Sanity is source of
    # truth for the doc's existence + status.
    try:
        doc = await sanity_client.query(  # type: ignore[attr-defined]
            '*[_id == $id][0]{_id, status, "isDraft": _id in path("drafts.**")}',
            {"id": normalised},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Sanity query failed: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    doc_exists = isinstance(doc, dict) and bool(doc)
    if doc_exists:
        # Safety gate only applies to a doc that still exists: an operator
        # must not lose a pending/published doc by mis-clicking Delete.
        if not doc.get("isDraft") or doc.get("status") != "rejected":
            raise HTTPException(
                status_code=400,
                detail=(
                    "refusing to delete: document is not in the rejected "
                    "state. Reject it first, then retry."
                ),
            )

        from pipeline.publisher.sanity import SanityPublisher  # noqa: PLC0415

        publisher = SanityPublisher(client=sanity_client)
        try:
            await publisher.delete_draft(normalised)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"Sanity delete failed: {type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
    else:
        # Idempotent path: Sanity has no such doc. Don't 404 — the caller
        # wants this draft gone, and it already is on the Sanity side. Fall
        # through to clean any local refs that outlived it.
        log.info("draft.delete_already_absent_in_sanity", draft_id=normalised)

    with session_scope() as session:
        purged = purge_draft_local_refs(session, normalised, brand_id)
    if any(purged.values()):
        log.info("draft.local_refs_purged", draft_id=normalised, **purged)
    return None


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
    """Generate one cover for this draft's topic and patch every sibling.

    NTS_091 Task B: this is also the endpoint behind the publish-guard
    banner's «Сгенерировать обложку», i.e. it runs on drafts whose
    ``coverImage`` is *null*. There is deliberately no
    already-has-a-cover precondition — creating the first cover and
    replacing an existing one are the same operation here (pinned by
    ``test_regenerate_generates_the_first_cover_when_there_is_none``).
    """
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
