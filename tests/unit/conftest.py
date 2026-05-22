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
    with_sanity_creds: bool = False,
) -> Brand:
    """Insert a single brand row and return it (committed in the caller's session).

    When ``with_sanity_creds=True`` a fake encrypted Sanity token is
    stored so ``run_pipeline`` passes its M4 check. The encryption key
    is configured via the ``BRANDS_ENCRYPTION_KEY`` env var; tests that
    need this must set the env var first.
    """
    now = datetime.now(tz=timezone.utc)
    sanity_project_id = None
    sanity_dataset = None
    sanity_api_version = None
    sanity_api_token_enc = None
    if with_sanity_creds:
        from pipeline.admin.encryption import get_encryption  # noqa: PLC0415

        sanity_project_id = "fake-project"
        sanity_dataset = "production"
        sanity_api_version = "2024-01-01"
        sanity_api_token_enc = get_encryption().encrypt("fake-sanity-token")
    brand = Brand(
        slug=slug,
        name=name or slug.title(),
        language="en",
        timezone="Europe/Madrid",
        status=status,
        active=active,
        sanity_project_id=sanity_project_id,
        sanity_dataset=sanity_dataset,
        sanity_api_version=sanity_api_version,
        sanity_api_token_enc=sanity_api_token_enc,
        created_at=now,
        updated_at=now,
    )
    session.add(brand)
    session.flush()
    return brand


def seed_icon_brand(session: Session, *, with_sanity_creds: bool = False) -> int:
    """Convenience: seed the 'icon' brand and return its id."""
    brand = seed_brand(
        session, slug="icon", name="Icon Finance", with_sanity_creds=with_sanity_creds
    )
    return brand.id
