"""Schema tests for the Brand model + FK behaviour on Source/Prompt/Run."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    Brand,
    PipelineConfig,
    Prompt,
    Run,
    Source,
)
from tests.unit.conftest import seed_brand, seed_icon_brand


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "admin.db"
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=db_path)
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
        admin_db.reset_for_tests()


def test_brands_table_exists(session) -> None:
    names = set(inspect(session.bind).get_table_names())
    assert "brands" in names


def test_brand_slug_is_unique(session) -> None:
    seed_brand(session, slug="icon")
    session.add(
        Brand(
            slug="icon",  # duplicate
            name="Another Icon",
            language="en",
            timezone="Europe/Madrid",
            status="draft",
            active=False,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_brand_status_check_constraint_rejects_unknown(session) -> None:
    bad = Brand(
        slug="bad",
        name="Bad",
        language="en",
        timezone="Europe/Madrid",
        status="zombie",  # not in {draft, active, paused, archived}
        active=False,
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_source_requires_existing_brand_id_fk(session) -> None:
    src = Source(
        brand_id_fk=999_999,  # no such brand
        name="orphan",
        source_type="rss",
        url="https://example.com/feed",
        primary_category="wealth",
    )
    session.add(src)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_deleting_brand_with_sources_is_restricted(session) -> None:
    brand_id = seed_icon_brand(session)
    session.add(
        Source(
            brand_id_fk=brand_id,
            name="x",
            source_type="rss",
            url="https://example.com/feed",
            primary_category="wealth",
        )
    )
    session.flush()
    brand = session.get(Brand, brand_id)
    session.delete(brand)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_two_brands_can_coexist(session) -> None:
    icon_id = seed_brand(session, slug="icon", name="Icon").id
    neovox_id = seed_brand(session, slug="neovox", name="Neovox", status="draft", active=False).id
    assert icon_id != neovox_id
    brands = session.scalars(select(Brand).order_by(Brand.slug)).all()
    assert [b.slug for b in brands] == ["icon", "neovox"]


def test_brand_credential_columns_default_to_none(session) -> None:
    bid = seed_brand(session, slug="bare", name="Bare", status="draft", active=False).id
    row = session.get(Brand, bid)
    assert row.sanity_project_id is None
    assert row.sanity_api_token_enc is None
    assert row.telegram_bot_token_enc is None
    assert row.meta_access_token_enc is None
    assert row.voice_profile_yaml is None


def test_pipeline_config_pk_is_brand_id_fk(session) -> None:
    icon_id = seed_icon_brand(session)
    neovox_id = seed_brand(session, slug="neovox", name="Neovox").id
    # Commit brand rows so a later rollback doesn't undo them.
    session.commit()
    # Same brand twice → PK collision.
    session.add(
        PipelineConfig(
            brand_id_fk=icon_id,
            scoring_threshold=7,
            topics_per_run=3,
            voice_profile="mission: x",
        )
    )
    session.flush()
    session.add(
        PipelineConfig(
            brand_id_fk=icon_id,  # PK collision
            scoring_threshold=8,
            topics_per_run=4,
            voice_profile="y",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    # But two different brands each get their own row.
    session.add(
        PipelineConfig(
            brand_id_fk=neovox_id,
            scoring_threshold=5,
            topics_per_run=2,
            voice_profile="neovox: y",
        )
    )
    session.flush()
    assert session.scalars(select(PipelineConfig)).all()
