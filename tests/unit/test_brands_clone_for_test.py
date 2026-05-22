"""Tests for /api/v1/brands/{id}/clone-for-test.

This endpoint backs the Step 9 autonomous E2E: the agent never sees
plaintext credentials, the backend clones encrypted blobs verbatim
into a new draft brand. NTS_025 Step 9 § "clone-for-test".
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Brand
from pipeline.common import config as config_module
from sqlalchemy import select

ADMIN_TOKEN = "tok-clone"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client_and_source_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv(
        "BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    from pipeline.admin.server import create_app

    client = TestClient(create_app())
    # Create a fully-credentialed Icon brand to clone from.
    icon = client.post(
        "/api/v1/brands",
        headers=AUTH,
        json={
            "slug": "icon",
            "name": "Icon Finance",
            "sanity_project_id": "icon-project",
            "sanity_dataset": "production",
            "sanity_api_token": "icon-secret-token",
            "telegram_bot_token": "icon-tg-token",
        },
    ).json()
    yield client, icon["id"]
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def test_clone_for_test_creates_draft_with_same_ciphertext(client_and_source_brand) -> None:
    client, icon_id = client_and_source_brand
    resp = client.post(
        f"/api/v1/brands/{icon_id}/clone-for-test",
        headers=AUTH,
        json={"slug": "testbrand", "name": "Test Brand"},
    )
    assert resp.status_code == 201, resp.text
    cloned = resp.json()
    assert cloned["slug"] == "testbrand"

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon = session.get(Brand, icon_id)
        cloned_row = session.get(Brand, cloned["id"])
    # Status must be 'draft', credentials must be byte-identical to the
    # source's encrypted blobs (the backend copied them without
    # round-tripping through plaintext).
    assert cloned_row.status == "draft"
    assert cloned_row.active is False
    assert cloned_row.sanity_api_token_enc == icon.sanity_api_token_enc
    assert cloned_row.telegram_bot_token_enc == icon.telegram_bot_token_enc
    assert cloned_row.sanity_project_id == icon.sanity_project_id


def test_clone_for_test_rejects_duplicate_slug(client_and_source_brand) -> None:
    client, icon_id = client_and_source_brand
    client.post(
        f"/api/v1/brands/{icon_id}/clone-for-test",
        headers=AUTH,
        json={"slug": "testbrand", "name": "Test"},
    )
    resp = client.post(
        f"/api/v1/brands/{icon_id}/clone-for-test",
        headers=AUTH,
        json={"slug": "testbrand", "name": "Again"},
    )
    assert resp.status_code == 409


def test_clone_for_test_404_when_source_brand_missing(client_and_source_brand) -> None:
    client, _ = client_and_source_brand
    resp = client.post(
        "/api/v1/brands/999999/clone-for-test",
        headers=AUTH,
        json={"slug": "testbrand", "name": "T"},
    )
    assert resp.status_code == 404
