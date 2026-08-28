"""``/api/v1/taxonomy`` — a brand's services (NTS_109, NTS_111 §Редполитика).

`brand_taxonomy` is what the guard's `{services}` placeholder renders, so
editing it is editing the rubric's vocabulary — which is why it belongs on the
Editorial Policy screen and not in a settings blob.

Two rules the endpoints enforce, both of them about the guard rather than about
CRUD hygiene:

* **A key cannot be renamed, only added or removed.** `candidates.service_category`
  stores the key, so renaming `wealth` to `private_banking` would orphan every
  candidate that had it — silently, since the column has no FK. Add the new
  service, move what matters, delete the old one.
* **A service in use cannot be deleted.** Same reason, made loud: a 409 naming
  the count beats a screen full of candidates whose service no longer exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from pipeline.admin.db import session_scope
from pipeline.admin.models import Brand, BrandTaxonomy, Candidate
from pipeline.admin.schemas import (
    BrandTaxonomyIn,
    BrandTaxonomyOut,
    BrandTaxonomyUpdate,
)

router = APIRouter()


@router.get("", response_model=list[BrandTaxonomyOut])
def list_taxonomy(brand_id: int) -> list[BrandTaxonomyOut]:
    """A brand's services, ordered by key — the same order `{services}` renders
    in, so the screen and the prompt read alike."""
    with session_scope() as session:
        if session.get(Brand, brand_id) is None:
            raise HTTPException(status_code=404, detail="brand not found")
        return [
            BrandTaxonomyOut.model_validate(row)
            for row in session.scalars(
                select(BrandTaxonomy)
                .where(BrandTaxonomy.brand_id_fk == brand_id)
                .order_by(BrandTaxonomy.key)
            )
        ]


@router.post("", response_model=BrandTaxonomyOut, status_code=status.HTTP_201_CREATED)
def create_service(payload: BrandTaxonomyIn) -> BrandTaxonomyOut:
    with session_scope() as session:
        if session.get(Brand, payload.brand_id) is None:
            raise HTTPException(status_code=404, detail="brand not found")
        row = BrandTaxonomy(
            brand_id_fk=payload.brand_id,
            key=payload.key,
            label=payload.label,
            description_for_guard=payload.description_for_guard,
            service_url_path=payload.service_url_path,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"service key {payload.key!r} already exists for this brand",
            ) from exc
        return BrandTaxonomyOut.model_validate(row)


@router.put("/{service_id}", response_model=BrandTaxonomyOut)
def update_service(
    service_id: int, brand_id: int, payload: BrandTaxonomyUpdate
) -> BrandTaxonomyOut:
    """Edit a service's label, guard description or URL path.

    `key` is deliberately absent from the update schema: `candidates.service_category`
    stores it with no foreign key, so a rename would orphan history silently.
    """
    with session_scope() as session:
        row = session.get(BrandTaxonomy, service_id)
        if row is None or row.brand_id_fk != brand_id:
            raise HTTPException(status_code=404, detail="service not found")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        row.updated_at = datetime.now(tz=UTC)
        session.flush()
        return BrandTaxonomyOut.model_validate(row)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service(service_id: int, brand_id: int) -> None:
    with session_scope() as session:
        row = session.get(BrandTaxonomy, service_id)
        if row is None or row.brand_id_fk != brand_id:
            raise HTTPException(status_code=404, detail="service not found")
        in_use = int(
            session.execute(
                select(func.count(Candidate.id)).where(
                    Candidate.brand_id_fk == brand_id,
                    Candidate.service_category == row.key,
                )
            ).scalar()
            or 0
        )
        if in_use:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"service {row.key!r} is on {in_use} candidate(s) — "
                    "deleting it would leave them pointing at a service that "
                    "no longer exists"
                ),
            )
        session.delete(row)
