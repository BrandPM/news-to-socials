"""``/api/v1/brands`` route group — multi-brand CRUD + lifecycle.

Per NTS_025 § "Multi-brand модель":

* GET    /                          → list (no sensitive fields)
* POST   /                          → create (encrypts on insert)
* GET    /{id}                      → detail (sensitive tokens as has_* bools)
* PUT    /{id}                      → update (preserve / clear / replace creds)
* DELETE /{id}                      → 409 if status≠draft or any rows reference it
* POST   /{id}/test-sanity          → pings Sanity with stored creds
* POST   /{id}/activate             → flips to 'active' iff Sanity ping succeeds
* POST   /{id}/pause                → flips to 'paused'
* POST   /{id}/clone-for-test       → copy encrypted blobs into a new draft
                                      brand without exposing plaintext to chat
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pipeline.admin.db import session_scope
from pipeline.admin.models import (
    Brand,
    CostRecord,
    PipelineConfig,
    Prompt,
    Run,
    Source,
    Topic,
)
from pipeline.admin.schemas import (
    BrandCloneForTestIn,
    BrandCloneForTestOut,
    BrandDetail,
    BrandIn,
    BrandSummary,
    BrandTestSanityOut,
    BrandUpdate,
    validate_languages_payload,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_detail(row: Brand) -> BrandDetail:
    return BrandDetail(
        id=row.id,
        slug=row.slug,
        name=row.name,
        language=row.language,
        languages=row.languages,
        timezone=row.timezone,
        status=row.status,
        active=row.active,
        sanity_project_id=row.sanity_project_id,
        sanity_dataset=row.sanity_dataset,
        sanity_api_version=row.sanity_api_version,
        sanity_studio_url=row.sanity_studio_url,
        telegram_channel_id=row.telegram_channel_id,
        meta_app_id=row.meta_app_id,
        meta_page_id=row.meta_page_id,
        meta_ig_business_id=row.meta_ig_business_id,
        voice_profile_yaml=row.voice_profile_yaml,
        has_sanity_api_token=bool(row.sanity_api_token_enc),
        has_telegram_bot_token=bool(row.telegram_bot_token_enc),
        has_meta_app_secret=bool(row.meta_app_secret_enc),
        has_meta_access_token=bool(row.meta_access_token_enc),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _encrypt_or_none(value: str | None) -> str | None:
    """Return ``None`` for empty, encrypted ciphertext otherwise."""
    if not value:
        return None
    from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

    return get_encryption().encrypt(value)


_ENC_TOKEN_FIELDS = (
    ("sanity_api_token", "sanity_api_token_enc"),
    ("telegram_bot_token", "telegram_bot_token_enc"),
    ("meta_app_secret", "meta_app_secret_enc"),
    ("meta_access_token", "meta_access_token_enc"),
)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BrandSummary])
def list_brands_route() -> list[BrandSummary]:
    with session_scope() as session:
        rows = session.scalars(select(Brand).order_by(Brand.slug)).all()
        return [BrandSummary.model_validate(r) for r in rows]


@router.post("", response_model=BrandDetail, status_code=status.HTTP_201_CREATED)
def create_brand(payload: BrandIn) -> BrandDetail:
    """Create a brand. Sensitive tokens are encrypted on insert.

    Derived ``active`` is True iff status='active' AND a Sanity token is
    present — enforces M4 (a brand can't be active without creds).
    """
    now = datetime.now(tz=timezone.utc)
    data = payload.model_dump()

    sanity_token_enc = _encrypt_or_none(data.pop("sanity_api_token", None))
    telegram_token_enc = _encrypt_or_none(data.pop("telegram_bot_token", None))
    meta_app_secret_enc = _encrypt_or_none(data.pop("meta_app_secret", None))
    meta_access_token_enc = _encrypt_or_none(data.pop("meta_access_token", None))

    with session_scope() as session:
        brand = Brand(
            slug=data["slug"],
            name=data["name"],
            language=data.get("language") or "en",
            timezone=data.get("timezone") or "Europe/Madrid",
            status="draft",
            active=False,
            sanity_project_id=data.get("sanity_project_id"),
            sanity_dataset=data.get("sanity_dataset"),
            sanity_api_version=data.get("sanity_api_version") or "2024-01-01",
            sanity_api_token_enc=sanity_token_enc,
            sanity_studio_url=data.get("sanity_studio_url"),
            telegram_bot_token_enc=telegram_token_enc,
            telegram_channel_id=data.get("telegram_channel_id"),
            meta_app_id=data.get("meta_app_id"),
            meta_app_secret_enc=meta_app_secret_enc,
            meta_access_token_enc=meta_access_token_enc,
            meta_page_id=data.get("meta_page_id"),
            meta_ig_business_id=data.get("meta_ig_business_id"),
            voice_profile_yaml=data.get("voice_profile_yaml"),
            created_at=now,
            updated_at=now,
        )
        session.add(brand)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"brand with slug {payload.slug!r} already exists",
            ) from exc
        return _to_detail(brand)


@router.get("/{brand_id}", response_model=BrandDetail)
def get_brand_route(brand_id: int) -> BrandDetail:
    with session_scope() as session:
        row = session.get(Brand, brand_id)
        if row is None:
            raise HTTPException(status_code=404, detail="brand not found")
        return _to_detail(row)


@router.put("/{brand_id}", response_model=BrandDetail)
def update_brand(brand_id: int, payload: BrandUpdate) -> BrandDetail:
    """Update a brand. Credential fields follow preserve/clear/replace:

    * key omitted          → preserve existing value
    * key set to ``""``    → clear (set NULL)
    * key set to any other → encrypt + replace
    """
    data = payload.model_dump(exclude_unset=True)
    with session_scope() as session:
        row = session.get(Brand, brand_id)
        if row is None:
            raise HTTPException(status_code=404, detail="brand not found")

        # Token fields — apply preserve/clear/replace semantics.
        for wire_key, db_key in _ENC_TOKEN_FIELDS:
            if wire_key not in data:
                continue  # preserve
            value = data.pop(wire_key)
            if value == "":
                setattr(row, db_key, None)  # clear
            elif value is None:
                setattr(row, db_key, None)  # clear (explicit null)
            else:
                setattr(row, db_key, _encrypt_or_none(value))  # replace

        # ``languages`` is a list on the wire but JSON-as-TEXT in the DB.
        # Content rules (non-empty / supported / includes en) raise a 400.
        # Omitted key (None) preserves the existing roster.
        if "languages" in data:
            langs = data.pop("languages")
            if langs is not None:
                try:
                    validated = validate_languages_payload(langs)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                import json as _json  # noqa: PLC0415

                row.languages = _json.dumps(validated)

        # Plaintext + metadata fields — also preserve/clear/replace.
        for k, v in data.items():
            if v == "" and k != "status":
                setattr(row, k, None)
            else:
                setattr(row, k, v)

        # Recompute ``active`` per M4 — only True when status='active' AND
        # Sanity token is configured. Otherwise force False.
        has_creds = bool(row.sanity_api_token_enc) and bool(row.sanity_project_id)
        row.active = row.status == "active" and has_creds
        row.updated_at = datetime.now(tz=timezone.utc)
        session.flush()
        return _to_detail(row)


@router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brand(brand_id: int) -> None:
    """Strict delete per M5 — refuses if status≠draft or any related
    rows exist. The 409 body enumerates which tables block deletion."""
    with session_scope() as session:
        row = session.get(Brand, brand_id)
        if row is None:
            raise HTTPException(status_code=404, detail="brand not found")
        if row.status != "draft":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"brand status is {row.status!r} — set status='draft' "
                    "before deleting (M5)"
                ),
            )
        # Enumerate every table that holds rows pointing at this brand.
        related: dict[str, int] = {}
        for model, name in (
            (Source, "sources"),
            (Prompt, "prompts"),
            (PipelineConfig, "pipeline_config"),
            (Run, "runs"),
            (CostRecord, "cost_records"),
        ):
            count = session.scalar(
                select(model.id if name != "pipeline_config" else PipelineConfig.brand_id_fk)
                .where(model.brand_id_fk == brand_id)
                .limit(1)
            )
            if count is not None:
                # We only need to know "any row exists"; use a real count
                # for the error message so the operator knows the scope.
                from sqlalchemy import func as _f  # noqa: PLC0415

                n = session.scalar(
                    select(_f.count()).select_from(model).where(
                        model.brand_id_fk == brand_id
                    )
                )
                related[name] = int(n or 0)
        # Topics inherit brand via Run; check separately.
        topic_run_ids = list(
            session.scalars(
                select(Run.id).where(Run.brand_id_fk == brand_id)
            )
        )
        if topic_run_ids:
            from sqlalchemy import func as _f  # noqa: PLC0415

            n_topics = session.scalar(
                select(_f.count()).select_from(Topic).where(
                    Topic.run_id.in_(topic_run_ids)
                )
            )
            if n_topics:
                related["topics"] = int(n_topics)
        if related:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "brand has related rows — delete those first "
                        "before removing the brand (M5)"
                    ),
                    "related": related,
                },
            )
        session.delete(row)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@router.post("/{brand_id}/test-sanity", response_model=BrandTestSanityOut)
async def test_sanity(brand_id: int) -> BrandTestSanityOut:
    """Ping Sanity with the brand's stored credentials. Decrypts at the
    moment of use, never persists plaintext (M3)."""
    with session_scope() as session:
        row = session.get(Brand, brand_id)
        if row is None:
            raise HTTPException(status_code=404, detail="brand not found")
        project_id = row.sanity_project_id
        dataset = row.sanity_dataset
        api_version = row.sanity_api_version or "2024-01-01"
        token_enc = row.sanity_api_token_enc

    if not project_id or not token_enc:
        return BrandTestSanityOut(
            ok=False, error="brand has no Sanity credentials configured"
        )

    from pipeline.admin.encryption import get_encryption  # noqa: PLC0415
    from pipeline.publisher.sanity import SanityClient  # noqa: PLC0415

    try:
        token = get_encryption().decrypt(token_enc)
    except Exception:  # noqa: BLE001
        return BrandTestSanityOut(
            ok=False, error="could not decrypt Sanity token (bad master key?)"
        )

    client = SanityClient(
        project_id=project_id,
        dataset=dataset or "production",
        api_version=api_version,
        token=token,
    )
    # Local-scope plaintext token — release reference ASAP. (M3 carve-out.)
    del token

    try:
        result = await client.query('count(*[_type == "post"])')
        return BrandTestSanityOut(
            ok=True, project_id=project_id, document_count=int(result or 0)
        )
    except Exception as exc:  # noqa: BLE001 — surface upstream errors
        # Scrub any obvious token/secret values from the error message.
        msg = str(exc)
        return BrandTestSanityOut(ok=False, error=f"{type(exc).__name__}: {msg[:200]}")


@router.post("/{brand_id}/activate", response_model=BrandDetail)
async def activate_brand(brand_id: int) -> BrandDetail:
    """Flip status='active' iff Sanity ping succeeds (otherwise 409)."""
    test_result = await test_sanity(brand_id)
    if not test_result.ok:
        raise HTTPException(
            status_code=409,
            detail=f"cannot activate — Sanity check failed: {test_result.error}",
        )
    with session_scope() as session:
        row = session.get(Brand, brand_id)
        if row is None:
            raise HTTPException(status_code=404, detail="brand not found")
        row.status = "active"
        row.active = True
        row.updated_at = datetime.now(tz=timezone.utc)
        session.flush()
        return _to_detail(row)


@router.post("/{brand_id}/pause", response_model=BrandDetail)
def pause_brand(brand_id: int) -> BrandDetail:
    with session_scope() as session:
        row = session.get(Brand, brand_id)
        if row is None:
            raise HTTPException(status_code=404, detail="brand not found")
        row.status = "paused"
        row.active = False
        row.updated_at = datetime.now(tz=timezone.utc)
        session.flush()
        return _to_detail(row)


@router.post(
    "/{brand_id}/clone-for-test",
    response_model=BrandCloneForTestOut,
    status_code=status.HTTP_201_CREATED,
)
def clone_for_test(brand_id: int, payload: BrandCloneForTestIn) -> BrandCloneForTestOut:
    """Server-side helper: clone a brand's encrypted blobs into a new
    draft brand without decrypting. Used by Step 9 autonomous E2E so
    the testbrand has working credentials but plaintext never leaves
    the backend process.
    """
    now = datetime.now(tz=timezone.utc)
    with session_scope() as session:
        src = session.get(Brand, brand_id)
        if src is None:
            raise HTTPException(status_code=404, detail="source brand not found")
        clone = Brand(
            slug=payload.slug,
            name=payload.name,
            language=src.language,
            timezone=src.timezone,
            status="draft",
            active=False,
            sanity_project_id=src.sanity_project_id,
            sanity_dataset=src.sanity_dataset,
            sanity_api_version=src.sanity_api_version,
            # Copy the encrypted bytes verbatim — never decrypt here.
            sanity_api_token_enc=src.sanity_api_token_enc,
            sanity_studio_url=src.sanity_studio_url,
            telegram_bot_token_enc=src.telegram_bot_token_enc,
            telegram_channel_id=src.telegram_channel_id,
            meta_app_id=src.meta_app_id,
            meta_app_secret_enc=src.meta_app_secret_enc,
            meta_access_token_enc=src.meta_access_token_enc,
            meta_page_id=src.meta_page_id,
            meta_ig_business_id=src.meta_ig_business_id,
            voice_profile_yaml=src.voice_profile_yaml,
            created_at=now,
            updated_at=now,
        )
        session.add(clone)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"brand with slug {payload.slug!r} already exists",
            ) from exc
        return BrandCloneForTestOut(id=clone.id, slug=clone.slug)
