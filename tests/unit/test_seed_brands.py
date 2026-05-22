"""Tests for the brand-seeding logic in scripts/seed_admin_db."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Brand
from pipeline.common import config as config_module
from scripts.seed_admin_db import seed


@pytest.fixture
def tmp_admin_db(tmp_path, monkeypatch):
    db_path = tmp_path / "admin.db"
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(db_path))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=db_path)
    admin_db.Base.metadata.create_all(engine)
    yield db_path
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def _all_brands() -> list[Brand]:
    factory = admin_db.get_session_factory()
    with factory() as s:
        return s.scalars(select(Brand).order_by(Brand.slug)).all()


def test_seeds_five_brands_on_empty_db(tmp_admin_db) -> None:
    seed(brand_slug="icon")
    brands = _all_brands()
    assert {b.slug for b in brands} == {
        "icon", "neovox", "creolix", "vilatrix", "nexora"
    }


def test_icon_brand_is_active_or_draft_based_on_creds(tmp_admin_db, monkeypatch) -> None:
    """If Sanity creds are configured in settings, Icon gets status='active';
    if not, status='draft'."""
    monkeypatch.setenv("SANITY_API_TOKEN", "fake-test-token")
    monkeypatch.setenv("SANITY_PROJECT_ID", "fake-project")
    monkeypatch.setattr(config_module, "_settings", None)

    seed(brand_slug="icon")
    factory = admin_db.get_session_factory()
    with factory() as s:
        icon = s.execute(select(Brand).where(Brand.slug == "icon")).scalar_one()
    assert icon.status == "active"
    assert icon.active is True
    assert icon.sanity_project_id == "fake-project"
    assert icon.sanity_api_token_enc is not None
    # Token IS encrypted (not stored as plaintext).
    assert icon.sanity_api_token_enc != "fake-test-token"


def test_placeholder_brands_are_draft_with_no_creds(tmp_admin_db) -> None:
    seed(brand_slug="icon")
    brands = _all_brands()
    placeholders = [b for b in brands if b.slug != "icon"]
    assert len(placeholders) == 4
    for b in placeholders:
        assert b.status == "draft"
        assert b.active is False
        assert b.sanity_project_id is None
        assert b.sanity_api_token_enc is None


def test_seed_idempotent_on_brands(tmp_admin_db) -> None:
    first = seed(brand_slug="icon")
    # First run inserts all 5 brands.
    brand_inserts = [line for line in first.inserted if line.startswith("brand ")]
    assert len(brand_inserts) == 5
    second = seed(brand_slug="icon")
    brand_inserts2 = [line for line in second.inserted if line.startswith("brand ")]
    assert brand_inserts2 == []
    brand_skips = [line for line in second.skipped if line.startswith("brand ")]
    assert len(brand_skips) == 5
    # Still exactly 5 brand rows.
    assert len(_all_brands()) == 5


def test_seed_does_not_overwrite_existing_brand_credentials(
    tmp_admin_db, monkeypatch
) -> None:
    """If an operator has edited Icon's credentials via the UI and then
    the seed script is re-run, we MUST NOT clobber their edits."""
    # First seed: token from initial env.
    monkeypatch.setenv("SANITY_API_TOKEN", "original-token")
    monkeypatch.setenv("SANITY_PROJECT_ID", "original-project")
    monkeypatch.setattr(config_module, "_settings", None)
    seed(brand_slug="icon")

    # Simulate operator editing the brand row.
    factory = admin_db.get_session_factory()
    with factory() as s:
        icon = s.execute(select(Brand).where(Brand.slug == "icon")).scalar_one()
        icon.sanity_api_token_enc = "OPERATOR_EDITED_TOKEN"
        icon.sanity_project_id = "operator-edited"
        s.commit()

    # Change the env, re-run the seed script.
    monkeypatch.setenv("SANITY_API_TOKEN", "different-token")
    monkeypatch.setenv("SANITY_PROJECT_ID", "different-project")
    monkeypatch.setattr(config_module, "_settings", None)
    seed(brand_slug="icon")

    # Operator's edits MUST survive.
    with factory() as s:
        icon = s.execute(select(Brand).where(Brand.slug == "icon")).scalar_one()
    assert icon.sanity_api_token_enc == "OPERATOR_EDITED_TOKEN"
    assert icon.sanity_project_id == "operator-edited"
