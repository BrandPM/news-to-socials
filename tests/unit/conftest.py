"""Shared admin-test helpers.

After the multi-brand refactor (NTS_025) every Source/Prompt/Run row
needs a ``brand_id_fk`` pointing at an existing brand. Test fixtures
that previously hardcoded ``brand_id="icon"`` now seed a Brand row
first and use its id.

This conftest exposes ``seed_icon_brand`` as a callable helper rather
than a fixture so individual test fixtures can decide when to call it
(some tests want an empty admin.db without any brand row to verify
fallback behaviour).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from pipeline.admin.models import Brand


def seed_brand(
    session: Session,
    *,
    slug: str = "icon",
    name: str | None = None,
    status: str = "active",
    active: bool = True,
) -> Brand:
    """Insert a single brand row and return it (committed in the caller's session)."""
    now = datetime.now(tz=timezone.utc)
    brand = Brand(
        slug=slug,
        name=name or slug.title(),
        language="en",
        timezone="Europe/Madrid",
        status=status,
        active=active,
        created_at=now,
        updated_at=now,
    )
    session.add(brand)
    session.flush()
    return brand


def seed_icon_brand(session: Session) -> int:
    """Convenience: seed the 'icon' brand and return its id."""
    brand = seed_brand(session, slug="icon", name="Icon Finance")
    return brand.id
