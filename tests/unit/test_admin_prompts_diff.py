"""Tests for /api/v1/prompts/diff (S5 Step 8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "tok-prompt-diff"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        other_id = seed_brand(session, slug="other", name="Other").id
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id, other_id
    admin_db.reset_for_tests()


def _create(client, brand_id, *, content: str, prompt_type: str = "writer_polish",
            version_name: str = "v1") -> int:
    resp = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json={
            "brand_id": brand_id,
            "prompt_type": prompt_type,
            "version_name": version_name,
            "content": content,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_diff_returns_both_prompts_and_unified_diff(env) -> None:
    client, icon_id, _ = env
    a_id = _create(client, icon_id, content="alpha\nbeta\ngamma", version_name="v1")
    b_id = _create(client, icon_id, content="alpha\nBETA\ngamma\ndelta", version_name="v2")

    resp = client.get(f"/api/v1/prompts/diff?a={a_id}&b={b_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["a"]["id"] == a_id
    assert body["b"]["id"] == b_id
    assert body["same_brand"] is True
    assert body["same_prompt_type"] is True
    assert "-beta" in body["unified_diff"]
    assert "+BETA" in body["unified_diff"]
    assert "+delta" in body["unified_diff"]


def test_diff_identical_prompts_returns_empty_diff(env) -> None:
    client, icon_id, _ = env
    a_id = _create(client, icon_id, content="same content here")
    resp = client.get(f"/api/v1/prompts/diff?a={a_id}&b={a_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["unified_diff"] == ""


def test_diff_404_when_one_id_missing(env) -> None:
    client, icon_id, _ = env
    a_id = _create(client, icon_id, content="x")
    resp = client.get(f"/api/v1/prompts/diff?a={a_id}&b=9999", headers=AUTH)
    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_diff_flags_cross_brand(env) -> None:
    client, icon_id, other_id = env
    a_id = _create(client, icon_id, content="icon prompt")
    b_id = _create(client, other_id, content="other prompt")
    resp = client.get(f"/api/v1/prompts/diff?a={a_id}&b={b_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["same_brand"] is False


def test_diff_flags_cross_type(env) -> None:
    client, icon_id, _ = env
    a_id = _create(client, icon_id, content="x", prompt_type="writer_polish")
    b_id = _create(client, icon_id, content="y", prompt_type="writer_draft")
    resp = client.get(f"/api/v1/prompts/diff?a={a_id}&b={b_id}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["same_brand"] is True
    assert body["same_prompt_type"] is False
