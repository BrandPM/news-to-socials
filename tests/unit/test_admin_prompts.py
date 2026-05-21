"""Integration tests for /api/v1/prompts routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.common import config as config_module

ADMIN_TOKEN = "tok-prompts"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    admin_db.reset_for_tests()


def _payload(**overrides):
    base = {
        "brand_id": "icon",
        "prompt_type": "writer_polish",
        "version_name": "v1",
        "content": "Polish this draft so it sounds like Icon.",
        "notes": "starter",
    }
    base.update(overrides)
    return base


def test_create_then_list_then_get(client) -> None:
    resp = client.post("/api/v1/prompts", headers=AUTH, json=_payload())
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    assert resp.json()["is_active"] is False  # new prompts are inactive by default

    resp = client.get("/api/v1/prompts?brand_id=icon", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get(f"/api/v1/prompts/{pid}", headers=AUTH)
    assert resp.json()["content"].startswith("Polish")


def test_activate_sets_active_and_deactivates_others(client) -> None:
    pid1 = client.post(
        "/api/v1/prompts", headers=AUTH, json=_payload(version_name="v1")
    ).json()["id"]
    pid2 = client.post(
        "/api/v1/prompts", headers=AUTH, json=_payload(version_name="v2")
    ).json()["id"]

    resp = client.post(f"/api/v1/prompts/{pid1}/activate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # Activating v2 must deactivate v1, otherwise the partial UNIQUE index
    # would reject it (and the operator would think activation failed).
    resp = client.post(f"/api/v1/prompts/{pid2}/activate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    listed = client.get("/api/v1/prompts?brand_id=icon", headers=AUTH).json()
    actives = [p for p in listed if p["is_active"]]
    assert len(actives) == 1
    assert actives[0]["id"] == pid2


def test_activate_does_not_touch_other_prompt_types(client) -> None:
    polish = client.post(
        "/api/v1/prompts", headers=AUTH, json=_payload(prompt_type="writer_polish")
    ).json()["id"]
    draft = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json=_payload(prompt_type="writer_draft", version_name="d1"),
    ).json()["id"]
    client.post(f"/api/v1/prompts/{polish}/activate", headers=AUTH)
    client.post(f"/api/v1/prompts/{draft}/activate", headers=AUTH)
    listed = client.get("/api/v1/prompts", headers=AUTH).json()
    actives = sorted(
        (p["prompt_type"] for p in listed if p["is_active"])
    )
    assert actives == ["writer_draft", "writer_polish"]


def test_delete_blocked_when_active(client) -> None:
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload()).json()["id"]
    client.post(f"/api/v1/prompts/{pid}/activate", headers=AUTH)
    resp = client.delete(f"/api/v1/prompts/{pid}", headers=AUTH)
    assert resp.status_code == 409


def test_delete_works_when_inactive(client) -> None:
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload()).json()["id"]
    resp = client.delete(f"/api/v1/prompts/{pid}", headers=AUTH)
    assert resp.status_code == 204


def test_test_endpoint_uses_mocked_llm(monkeypatch, client) -> None:
    """``POST /{id}/test`` returns the mocked LLM output + ai-tells count."""
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload()).json()["id"]

    from pipeline.admin import llm as llm_mod

    async def fake_test(*, prompt_type, prompt_content, sample_topic):  # noqa: ANN001
        assert prompt_type == "writer_polish"
        assert "Polish" in prompt_content
        assert "India" in sample_topic["title"]
        return llm_mod.PromptTestResult(
            text="A clean rewrite without filler.",
            cost_usd=0.012,
            ai_tells_count=0,
        )

    monkeypatch.setattr(llm_mod, "run_prompt_test", fake_test)
    resp = client.post(f"/api/v1/prompts/{pid}/test", headers=AUTH, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_text"].startswith("A clean rewrite")
    assert body["cost_usd"] == 0.012
    assert body["ai_tells_count"] == 0
