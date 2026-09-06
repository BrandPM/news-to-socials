"""Integration tests for /api/v1/prompts routes."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-prompts"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client_and_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


@pytest.fixture
def client(client_and_brand):
    return client_and_brand[0]


@pytest.fixture
def icon_brand_id(client_and_brand) -> int:
    return client_and_brand[1]


# A body that ACTIVATES. Since S3, ``POST /prompts/{id}/activate`` refuses a
# body whose placeholders the renderer cannot satisfy (NTS_063 pending, closed):
# activating one would silently fall back to the in-code constant. Saving an
# invalid draft is still allowed, so only the activating tests need this.
_VALID_BODIES = {
    "writer_polish": (
        "Polish this draft so it sounds like Icon, in {language_name}. "
        "Tells:{ai_tells} Banned:{banned_phrases} Good:{good_examples} "
        "Principles:{voice_principles} Topics:{topics_relevant} "
        # Migration 031 — the polish pass reads the same computed length
        # target the draft was written to.
        "Draft:{draft_json} Shape:{depth_guidance}"
    ),
    "writer_draft": (
        "Draft in {language_name} from {title} / {summary}. "
        "Voice:{voice_profile_yaml} Banned:{banned_phrases} Facts:{fact_pack} "
        # S6 required these three (NTS_102 v2): the plan, the computed length
        # target and the primary document. A body without them fails to
        # activate, which is the validator doing its job.
        "Plan:{plan} Shape:{depth_guidance} Doc:{primary_document}"
    ),
    "writer_translate": (
        "Translate {draft_json} into {language_name}. "
        "Banned:{banned_phrases} Good:{good_examples}"
    ),
}


def valid_body(prompt_type: str = "writer_polish", *, marker: str = "") -> str:
    """A renderable body for ``prompt_type``, optionally tagged so a test can
    tell two versions apart."""
    body = _VALID_BODIES.get(prompt_type, "Anything goes for this type.")
    return f"{marker}{body}" if marker else body


def _payload(icon_brand_id: int, **overrides):
    prompt_type = overrides.get("prompt_type", "writer_polish")
    base = {
        "brand_id": icon_brand_id,
        "prompt_type": prompt_type,
        "version_name": "v1",
        "content": valid_body(prompt_type),
        "notes": "starter",
    }
    base.update(overrides)
    return base


def test_create_then_list_then_get(client, icon_brand_id) -> None:
    resp = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    assert resp.json()["is_active"] is False

    resp = client.get(f"/api/v1/prompts?brand_id={icon_brand_id}", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get(f"/api/v1/prompts/{pid}", headers=AUTH)
    assert resp.json()["content"].startswith("Polish")


def test_activate_sets_active_and_deactivates_others(client, icon_brand_id) -> None:
    pid1 = client.post(
        "/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id, version_name="v1")
    ).json()["id"]
    pid2 = client.post(
        "/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id, version_name="v2")
    ).json()["id"]

    resp = client.post(f"/api/v1/prompts/{pid1}/activate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    resp = client.post(f"/api/v1/prompts/{pid2}/activate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    listed = client.get(f"/api/v1/prompts?brand_id={icon_brand_id}", headers=AUTH).json()
    actives = [p for p in listed if p["is_active"]]
    assert len(actives) == 1
    assert actives[0]["id"] == pid2


def test_activate_does_not_touch_other_prompt_types(client, icon_brand_id) -> None:
    polish = client.post(
        "/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id, prompt_type="writer_polish")
    ).json()["id"]
    draft = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json=_payload(icon_brand_id, prompt_type="writer_draft", version_name="d1"),
    ).json()["id"]
    client.post(f"/api/v1/prompts/{polish}/activate", headers=AUTH)
    client.post(f"/api/v1/prompts/{draft}/activate", headers=AUTH)
    listed = client.get("/api/v1/prompts", headers=AUTH).json()
    actives = sorted(
        p["prompt_type"] for p in listed if p["is_active"]
    )
    assert actives == ["writer_draft", "writer_polish"]


def test_delete_blocked_when_active(client, icon_brand_id) -> None:
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]
    client.post(f"/api/v1/prompts/{pid}/activate", headers=AUTH)
    resp = client.delete(f"/api/v1/prompts/{pid}", headers=AUTH)
    assert resp.status_code == 409


def test_delete_works_when_inactive(client, icon_brand_id) -> None:
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]
    resp = client.delete(f"/api/v1/prompts/{pid}", headers=AUTH)
    assert resp.status_code == 204


def test_test_endpoint_uses_mocked_llm(monkeypatch, client, icon_brand_id) -> None:
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]

    from pipeline.admin import llm as llm_mod

    async def fake_test(*, prompt_type, prompt_content, sample_topic, brand_id_fk=None):
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


# --- Save = clone + activate (NTS task 2) -------------------------------
#
# The redesigned editor "saves" an edit as a brand-new version and
# activates it, leaving the prior version intact for rollback. The UI
# composes the two existing endpoints (POST /prompts then
# POST /{id}/activate); this asserts the invariants that flow relies on.


def test_save_creates_new_active_version_and_old_survives(client, icon_brand_id) -> None:
    v1 = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json=_payload(
            icon_brand_id,
            version_name="v1",
            content=valid_body(marker="ORIGINAL. "),
        ),
    ).json()["id"]
    client.post(f"/api/v1/prompts/{v1}/activate", headers=AUTH)

    # "Save" an edit → a new version from the edited content, then activate.
    v2 = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json=_payload(
            icon_brand_id,
            version_name="v2 (edited)",
            content=valid_body(marker="EDITED. "),
        ),
    ).json()["id"]
    resp = client.post(f"/api/v1/prompts/{v2}/activate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # New version is active, old version still exists but is inactive.
    old = client.get(f"/api/v1/prompts/{v1}", headers=AUTH).json()
    assert old["is_active"] is False
    assert old["content"].startswith("ORIGINAL. ")
    new = client.get(f"/api/v1/prompts/{v2}", headers=AUTH).json()
    assert new["is_active"] is True
    assert new["content"].startswith("EDITED. ")


# --- Analyze (NTS task 3) -----------------------------------------------

_GOOD_ANALYSIS = {
    "strengths": ["Clear voice instructions"],
    "contradictions": [
        {
            "issue": "Asks for ## H2 in body but forbids markdown in title",
            "why": "The model can leak '##' into the title field",
            "suggestion": "Scope the markdown rule to the body only",
        }
    ],
    "risks": ["No explicit length cap"],
    "summary": "Solid prompt with one contradiction to resolve.",
}


class _FakeUsage:
    # Responses API usage shape. output_tokens includes reasoning_tokens.
    input_tokens = 800
    output_tokens = 200
    output_tokens_details = type("D", (), {"reasoning_tokens": 150})()


class _FakeResp:
    """Mimics the Responses API result (resp.output_text + resp.usage)."""

    def __init__(self, content: str) -> None:
        self.output_text = content
        self.usage = _FakeUsage()


def _fake_openai_returning(content: str):
    """Build a fake openai.AsyncOpenAI whose responses.create returns ``content``."""

    class _Responses:
        async def create(self, **_kwargs):
            return _FakeResp(content)

    class _Client:
        def __init__(self, **_kwargs):
            self.responses = _Responses()

    return _Client


@pytest.fixture
def _openai_key(monkeypatch):
    """Give the analyzer a (fake) API key so run_prompt_analysis proceeds."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    yield
    monkeypatch.setattr(config_module, "_settings", None)


def test_analyze_returns_valid_json_and_writes_cost(
    monkeypatch, client, icon_brand_id, _openai_key
) -> None:
    import openai

    monkeypatch.setattr(
        openai, "AsyncOpenAI", _fake_openai_returning(json.dumps(_GOOD_ANALYSIS))
    )
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]

    resp = client.post(f"/api/v1/prompts/{pid}/analyze", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["strengths"] == ["Clear voice instructions"]
    assert body["contradictions"][0]["suggestion"].startswith("Scope the markdown")
    assert body["risks"] == ["No explicit length cap"]
    assert body["summary"].startswith("Solid prompt")

    # A cost_records row with operation "prompt_analysis" was written.
    from pipeline.admin.models import CostRecord

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(CostRecord).filter_by(operation="prompt_analysis").all()
    assert len(rows) == 1
    assert rows[0].brand_id_fk == icon_brand_id
    assert rows[0].model == "gpt-5.5-2026-04-23"
    assert rows[0].cost_usd > 0


def test_analyze_calls_gpt55_responses_api_with_high_effort(
    monkeypatch, client, icon_brand_id, _openai_key
) -> None:
    """The analyze call targets gpt-5.5 via the Responses API at effort=high."""
    import openai

    captured: dict = {}

    class _Responses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeResp(json.dumps(_GOOD_ANALYSIS))

    class _Client:
        def __init__(self, **_kwargs):
            self.responses = _Responses()

    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]

    resp = client.post(f"/api/v1/prompts/{pid}/analyze", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert captured["model"] == "gpt-5.5-2026-04-23"
    assert captured["reasoning"] == {"effort": "high"}
    # Strict structured-output contract is enforced at the API layer.
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True


def test_analyze_malformed_llm_response_returns_422(
    monkeypatch, client, icon_brand_id, _openai_key
) -> None:
    import openai

    # Valid JSON, wrong shape (missing summary, contradictions not a list).
    bad = json.dumps({"strengths": [], "contradictions": "nope", "risks": []})
    monkeypatch.setattr(openai, "AsyncOpenAI", _fake_openai_returning(bad))
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]

    resp = client.post(f"/api/v1/prompts/{pid}/analyze", headers=AUTH)
    assert resp.status_code == 422, resp.text
    assert "unusable response" in resp.json()["detail"]


def test_analyze_not_found_returns_404(client, icon_brand_id, _openai_key) -> None:
    resp = client.post("/api/v1/prompts/999999/analyze", headers=AUTH)
    assert resp.status_code == 404


def test_analyze_rate_limited_per_version(
    monkeypatch, client, icon_brand_id, _openai_key
) -> None:
    import openai

    from pipeline.admin.routes import prompts as prompts_routes

    monkeypatch.setattr(
        openai, "AsyncOpenAI", _fake_openai_returning(json.dumps(_GOOD_ANALYSIS))
    )
    # Reset the in-memory window so prior tests don't bleed in.
    monkeypatch.setattr(prompts_routes, "_analyze_calls", {})
    monkeypatch.setattr(prompts_routes, "_ANALYZE_LIMIT", 2)
    pid = client.post("/api/v1/prompts", headers=AUTH, json=_payload(icon_brand_id)).json()["id"]

    assert client.post(f"/api/v1/prompts/{pid}/analyze", headers=AUTH).status_code == 200
    assert client.post(f"/api/v1/prompts/{pid}/analyze", headers=AUTH).status_code == 200
    resp = client.post(f"/api/v1/prompts/{pid}/analyze", headers=AUTH)
    assert resp.status_code == 429
