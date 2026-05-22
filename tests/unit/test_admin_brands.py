"""Integration tests for /api/v1/brands CRUD + lifecycle (NTS_025 Step 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import (
    Brand,
    PipelineConfig,
    Prompt,
    Run,
    Source,
)
from pipeline.common import config as config_module

ADMIN_TOKEN = "tok-brands"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
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

    yield TestClient(create_app())
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def _create(client, **overrides) -> dict:
    body = {"slug": "neovox", "name": "Neovox", **overrides}
    resp = client.post("/api/v1/brands", headers=AUTH, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- CRUD ---------------------------------------------------------------


def test_list_empty(client) -> None:
    resp = client.get("/api/v1/brands", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_brand_default_status_is_draft(client) -> None:
    body = _create(client)
    assert body["status"] == "draft"
    assert body["active"] is False
    assert body["slug"] == "neovox"
    assert body["has_sanity_api_token"] is False


def test_create_brand_encrypts_sanity_token(client) -> None:
    body = _create(
        client,
        sanity_project_id="abc",
        sanity_api_token="real-sanity-token",
    )
    # The plaintext token must NOT come back in the response.
    assert "sanity_api_token" not in body
    assert body["has_sanity_api_token"] is True
    # And the DB stores ciphertext, not plaintext.
    factory = admin_db.get_session_factory()
    with factory() as session:
        row = session.execute(select(Brand).where(Brand.slug == "neovox")).scalar_one()
        assert row.sanity_api_token_enc is not None
        assert row.sanity_api_token_enc != "real-sanity-token"


def test_create_brand_duplicate_slug_returns_409(client) -> None:
    _create(client)
    resp = client.post(
        "/api/v1/brands", headers=AUTH, json={"slug": "neovox", "name": "Other"}
    )
    assert resp.status_code == 409


def test_get_brand_detail_hides_sensitive_tokens(client) -> None:
    created = _create(
        client,
        sanity_project_id="abc",
        sanity_api_token="hidden",
        telegram_bot_token="tg-hidden",
    )
    resp = client.get(f"/api/v1/brands/{created['id']}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_sanity_api_token"] is True
    assert body["has_telegram_bot_token"] is True
    # Plaintext token must NEVER appear in the response.
    assert "hidden" not in str(body)
    assert "tg-hidden" not in str(body)


def test_put_brand_preserve_clear_replace_credentials(client) -> None:
    """The PUT endpoint follows preserve/clear/replace semantics for
    token fields per NTS_025 Step 4."""
    created = _create(
        client,
        sanity_project_id="abc",
        sanity_api_token="original-token",
    )
    bid = created["id"]

    # 1. Update non-token field — token preserved.
    resp = client.put(
        f"/api/v1/brands/{bid}", headers=AUTH, json={"name": "Renamed"}
    )
    assert resp.status_code == 200
    assert resp.json()["has_sanity_api_token"] is True

    # 2. Set sanity_api_token to "" → cleared.
    resp = client.put(
        f"/api/v1/brands/{bid}",
        headers=AUTH,
        json={"sanity_api_token": ""},
    )
    assert resp.json()["has_sanity_api_token"] is False

    # 3. Set sanity_api_token to a new value → replaced.
    resp = client.put(
        f"/api/v1/brands/{bid}",
        headers=AUTH,
        json={"sanity_api_token": "fresh-token"},
    )
    assert resp.json()["has_sanity_api_token"] is True


def test_delete_brand_blocks_when_not_draft(client) -> None:
    """M5: only status='draft' brands can be deleted."""
    created = _create(client)
    bid = created["id"]
    client.put(
        f"/api/v1/brands/{bid}", headers=AUTH, json={"status": "paused"}
    )
    resp = client.delete(f"/api/v1/brands/{bid}", headers=AUTH)
    assert resp.status_code == 409
    assert "draft" in resp.json()["detail"]


def test_delete_brand_blocks_when_related_rows_exist(client) -> None:
    """M5: deletion enumerates which tables block it in the 409 body."""
    created = _create(client)
    bid = created["id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            Source(
                brand_id_fk=bid,
                name="x",
                source_type="rss",
                url="https://example.com/feed",
                primary_category="wealth",
            )
        )
        session.commit()

    resp = client.delete(f"/api/v1/brands/{bid}", headers=AUTH)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "related" in detail
    assert "sources" in detail["related"]


def test_delete_brand_works_when_draft_and_no_related(client) -> None:
    created = _create(client)
    resp = client.delete(f"/api/v1/brands/{created['id']}", headers=AUTH)
    assert resp.status_code == 204
    resp = client.get(f"/api/v1/brands/{created['id']}", headers=AUTH)
    assert resp.status_code == 404


# --- Lifecycle ----------------------------------------------------------


def test_test_sanity_returns_error_when_no_creds(client) -> None:
    created = _create(client)
    resp = client.post(
        f"/api/v1/brands/{created['id']}/test-sanity", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "credentials" in body["error"].lower()


def test_test_sanity_pings_when_creds_present(client, monkeypatch) -> None:
    """A successful Sanity ping returns ok=True + document_count."""
    created = _create(
        client,
        sanity_project_id="real",
        sanity_api_token="token",
    )
    # Patch the SanityClient.query to avoid hitting the network.
    from pipeline.publisher import sanity as sanity_mod

    fake_query = AsyncMock(return_value=42)
    monkeypatch.setattr(sanity_mod.SanityClient, "query", fake_query)

    resp = client.post(
        f"/api/v1/brands/{created['id']}/test-sanity", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["document_count"] == 42


def test_activate_requires_successful_sanity_check(client, monkeypatch) -> None:
    created = _create(client)
    resp = client.post(
        f"/api/v1/brands/{created['id']}/activate", headers=AUTH
    )
    assert resp.status_code == 409
    assert "Sanity check failed" in resp.json()["detail"]


def test_activate_flips_status_and_active_when_check_ok(client, monkeypatch) -> None:
    created = _create(
        client, sanity_project_id="real", sanity_api_token="token"
    )
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", AsyncMock(return_value=10)
    )
    resp = client.post(
        f"/api/v1/brands/{created['id']}/activate", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["active"] is True


def test_pause_flips_status_to_paused(client, monkeypatch) -> None:
    created = _create(
        client, sanity_project_id="real", sanity_api_token="token"
    )
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", AsyncMock(return_value=1)
    )
    client.post(f"/api/v1/brands/{created['id']}/activate", headers=AUTH)
    resp = client.post(f"/api/v1/brands/{created['id']}/pause", headers=AUTH)
    assert resp.json()["status"] == "paused"
    assert resp.json()["active"] is False
