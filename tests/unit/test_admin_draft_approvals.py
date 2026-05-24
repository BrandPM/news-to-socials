"""Tests for /drafts/{id}/approve|reject + /regenerate-text + GET extension."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import DraftApproval
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "tok-approvals"
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
    admin_jobs.reset_image_jobs_for_tests()
    admin_jobs.reset_text_jobs_for_tests()

    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()
    admin_jobs.reset_image_jobs_for_tests()
    admin_jobs.reset_text_jobs_for_tests()


@pytest.fixture
def icon_with_creds(client):
    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session, with_sanity_creds=True)
        session.commit()
    return icon_id


@pytest.fixture
def two_brands(client):
    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session, with_sanity_creds=True)
        other_id = seed_brand(
            session, slug="other", name="Other", with_sanity_creds=True
        ).id
        session.commit()
    return icon_id, other_id


def _draft_doc(brand_slug: str = "icon", body_text: str = "Body text."):
    return {
        "title": "T",
        "body": [
            {
                "_type": "block",
                "style": "normal",
                "children": [{"text": body_text}],
            }
        ],
        "keyTakeaway": "k",
        "generatedBy": {"name": "pipeline", "brandSlug": brand_slug},
        "_createdAt": "2026-05-21T10:00:00Z",
        "coverImageUrl": None,
    }


# --- POST /drafts/{id}/approve | /reject ----------------------------------


def test_approve_creates_row_and_returns_status(client, icon_with_creds) -> None:
    bid = icon_with_creds
    resp = client.post(
        f"/api/v1/drafts/post-abc/approve?brand_id={bid}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "admin"
    assert body["decided_at"].startswith("2")

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1
    assert rows[0].sanity_draft_id == "drafts.post-abc"
    assert rows[0].status == "approved"
    assert rows[0].brand_id_fk == bid


def test_reject_creates_row(client, icon_with_creds) -> None:
    bid = icon_with_creds
    resp = client.post(
        f"/api/v1/drafts/post-bad/reject?brand_id={bid}",
        headers=AUTH,
        json={"note": "off-brand"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["note"] == "off-brand"


def test_approve_then_reject_upserts_same_row(client, icon_with_creds) -> None:
    bid = icon_with_creds
    client.post(
        f"/api/v1/drafts/post-z/approve?brand_id={bid}", headers=AUTH, json={}
    )
    resp = client.post(
        f"/api/v1/drafts/post-z/reject?brand_id={bid}",
        headers=AUTH,
        json={"note": "changed mind"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert resp.json()["note"] == "changed mind"

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1  # upserted, not duplicated


def test_approve_idempotent_same_status(client, icon_with_creds) -> None:
    bid = icon_with_creds
    r1 = client.post(
        f"/api/v1/drafts/post-id/approve?brand_id={bid}", headers=AUTH, json={}
    )
    r2 = client.post(
        f"/api/v1/drafts/post-id/approve?brand_id={bid}", headers=AUTH, json={}
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["status"] == "approved"

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1


def test_approve_with_unknown_brand_returns_404(client) -> None:
    resp = client.post(
        "/api/v1/drafts/post-x/approve?brand_id=9999", headers=AUTH, json={}
    )
    assert resp.status_code == 404


def test_approve_separate_rows_per_brand(client, two_brands) -> None:
    icon_id, other_id = two_brands
    # Same draft id, different brands → two rows allowed by composite unique.
    r1 = client.post(
        f"/api/v1/drafts/post-multi/approve?brand_id={icon_id}",
        headers=AUTH,
        json={},
    )
    r2 = client.post(
        f"/api/v1/drafts/post-multi/reject?brand_id={other_id}",
        headers=AUTH,
        json={},
    )
    assert r1.status_code == 200 and r2.status_code == 200

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).order_by(DraftApproval.id).all()
    assert len(rows) == 2
    assert {r.status for r in rows} == {"approved", "rejected"}


def test_approve_normalises_draft_id(client, icon_with_creds) -> None:
    """Both ``post-x`` and ``drafts.post-x`` should target the same row."""
    bid = icon_with_creds
    client.post(
        f"/api/v1/drafts/post-norm/approve?brand_id={bid}",
        headers=AUTH,
        json={},
    )
    r = client.post(
        f"/api/v1/drafts/drafts.post-norm/reject?brand_id={bid}",
        headers=AUTH,
        json={},
    )
    assert r.status_code == 200
    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1
    assert rows[0].status == "rejected"


def test_approve_note_too_long_rejected(client, icon_with_creds) -> None:
    bid = icon_with_creds
    resp = client.post(
        f"/api/v1/drafts/post-y/approve?brand_id={bid}",
        headers=AUTH,
        json={"note": "x" * 3000},
    )
    assert resp.status_code == 422


# --- GET /drafts/{id} extension (approval + AI tells) ---------------------


def test_get_draft_includes_approval_when_present(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        AsyncMock(return_value=_draft_doc()),
    )
    client.post(
        f"/api/v1/drafts/post-app/approve?brand_id={bid}",
        headers=AUTH,
        json={"note": "ship it"},
    )

    resp = client.get(f"/api/v1/drafts/post-app?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approval"] is not None
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["note"] == "ship it"


def test_get_draft_approval_is_null_when_undecided(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        AsyncMock(return_value=_draft_doc()),
    )
    resp = client.get(f"/api/v1/drafts/post-und?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["approval"] is None


def test_get_draft_includes_ai_tells_score(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        AsyncMock(return_value=_draft_doc(body_text="A pleasant body.")),
    )
    resp = client.get(f"/api/v1/drafts/post-ai?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "ai_tells_score" in body
    assert "ai_tells" in body
    # Score may be 0 for short clean body — the contract is the keys present.
    assert isinstance(body["ai_tells"], list)


def test_get_draft_approval_brand_scoped(
    monkeypatch, client, two_brands
) -> None:
    """Approval on (drafts.x, icon) must not leak into other-brand GET."""
    icon_id, other_id = two_brands
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        AsyncMock(return_value=_draft_doc(brand_slug="other")),
    )
    # Approve under "icon".
    client.post(
        f"/api/v1/drafts/post-leak/approve?brand_id={icon_id}",
        headers=AUTH,
        json={},
    )
    # GET under "other" must not see the icon approval.
    resp = client.get(
        f"/api/v1/drafts/post-leak?brand_id={other_id}", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["approval"] is None


# --- POST /drafts/{id}/regenerate-text ------------------------------------


def test_regenerate_text_returns_202_and_completes(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    captured: dict = {}

    async def fake_regen(draft_id: str, brand_id_fk: int) -> None:
        captured["draft_id"] = draft_id
        captured["brand_id_fk"] = brand_id_fk

    from pipeline.admin import text_regenerate as tr_mod

    monkeypatch.setattr(tr_mod, "regenerate_draft_text", fake_regen)

    resp = client.post(
        f"/api/v1/drafts/post-txt/regenerate-text?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    status_resp = client.get(
        f"/api/v1/drafts/text-jobs/{job_id}/status", headers=AUTH
    )
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["state"] == "done"
    assert body["error"] is None
    assert captured["draft_id"] == "drafts.post-txt"
    assert captured["brand_id_fk"] == bid


def test_regenerate_text_error_state(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds

    async def boom(draft_id: str, brand_id_fk: int) -> None:
        raise RuntimeError("openai 500")

    from pipeline.admin import text_regenerate as tr_mod

    monkeypatch.setattr(tr_mod, "regenerate_draft_text", boom)

    resp = client.post(
        f"/api/v1/drafts/post-bad/regenerate-text?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status_resp = client.get(
        f"/api/v1/drafts/text-jobs/{job_id}/status", headers=AUTH
    )
    body = status_resp.json()
    assert body["state"] == "error"
    assert "openai 500" in body["error"]


def test_regenerate_text_unknown_brand_returns_404(client) -> None:
    resp = client.post(
        "/api/v1/drafts/post-x/regenerate-text?brand_id=9999", headers=AUTH
    )
    assert resp.status_code == 404


def test_text_job_status_unknown_id_returns_404(client) -> None:
    resp = client.get("/api/v1/drafts/text-jobs/nope/status", headers=AUTH)
    assert resp.status_code == 404
