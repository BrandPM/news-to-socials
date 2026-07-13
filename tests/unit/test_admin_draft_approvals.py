"""Tests for /drafts/{id}/approve|reject + /regenerate-text + GET extension."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import (
    CostRecord,
    DraftApproval,
    Run,
    Source,
    Topic,
)
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
def mock_sanity_publish(monkeypatch):
    """Stub the Sanity side of approve/reject so unit tests don't try to
    reach api.sanity.io with fake creds.

    IT_PROJ_NTS_051 Task 3: ``/approve`` and ``/reject`` now mutate
    Sanity. Tests that exercise *only* the DB-write side need the
    publish step to no-op cleanly. Tests that assert on Sanity behaviour
    re-monkeypatch on top of this fixture.
    """
    from pipeline.publisher import sanity as sanity_mod

    async def fake_promote(self, draft_id, *, published_at=None):  # noqa: ANN001
        return draft_id.replace("drafts.", "")

    async def fake_delete(self, draft_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "promote_draft_to_published", fake_promote
    )
    monkeypatch.setattr(sanity_mod.SanityPublisher, "delete_draft", fake_delete)


@pytest.fixture
def icon_with_creds(client, mock_sanity_publish):
    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session, with_sanity_creds=True)
        session.commit()
    return icon_id


@pytest.fixture
def two_brands(client, mock_sanity_publish):
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


def _wrap_draft(doc: dict | None) -> AsyncMock:
    """IT_PROJ_NTS_052: GET /drafts/{id} now returns
    ``{"draft": ..., "published": ...}`` from a single GROQ. Wraps the
    legacy single-doc helper so the approval tests keep their setup."""
    return AsyncMock(return_value={"draft": doc, "published": None})


def test_get_draft_includes_approval_when_present(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        _wrap_draft(_draft_doc()),
    )
    client.post(
        f"/api/v1/drafts/post-app/approve?brand_id={bid}",
        headers=AUTH,
        json={"note": "ship it"},
    )

    resp = client.get(f"/api/v1/drafts/post-app?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    draft = body["draft"]
    assert draft is not None
    assert draft["approval"] is not None
    assert draft["approval"]["status"] == "approved"
    assert draft["approval"]["note"] == "ship it"


def test_get_draft_approval_is_null_when_undecided(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        _wrap_draft(_draft_doc()),
    )
    resp = client.get(f"/api/v1/drafts/post-und?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["draft"]["approval"] is None


def test_get_draft_includes_ai_tells_score(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        _wrap_draft(_draft_doc(body_text="A pleasant body.")),
    )
    resp = client.get(f"/api/v1/drafts/post-ai?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200
    draft = resp.json()["draft"]
    assert "ai_tells_score" in draft
    assert "ai_tells" in draft
    # Score may be 0 for short clean body — the contract is the keys present.
    assert isinstance(draft["ai_tells"], list)


def test_get_draft_approval_brand_scoped(
    monkeypatch, client, two_brands
) -> None:
    """Approval on (drafts.x, icon) must not leak into other-brand GET."""
    icon_id, other_id = two_brands
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient,
        "query",
        _wrap_draft(_draft_doc(brand_slug="other")),
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
    assert resp.json()["draft"]["approval"] is None


# --- IT_PROJ_NTS_051 Task 3 — approve publishes, reject deletes -----------


def test_approve_publishes_to_sanity_and_records_published_id(
    monkeypatch, client, icon_with_creds
) -> None:
    """The new approve flow should: record approval, call
    promote_draft_to_published, persist the resulting published id +
    timestamp. Response surface includes both."""
    bid = icon_with_creds
    captured: dict = {}

    async def capture_promote(self, draft_id, *, published_at=None):  # noqa: ANN001
        captured["draft_id"] = draft_id
        return draft_id.replace("drafts.", "")

    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityPublisher,
        "promote_draft_to_published",
        capture_promote,
    )

    resp = client.post(
        f"/api/v1/drafts/post-pub/approve?brand_id={bid}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    # New fields surface published metadata.
    assert body["sanity_published_id"] == "post-pub"
    assert body["published_at"] is not None
    # Promote was called with the normalised draft id (drafts. prefix).
    assert captured["draft_id"] == "drafts.post-pub"

    # DB row carries the publish info too.
    factory = admin_db.get_session_factory()
    with factory() as session:
        row = session.query(DraftApproval).first()
        assert row is not None
        assert row.sanity_published_id == "post-pub"
        assert row.published_at is not None


def test_approve_returns_502_when_sanity_publish_fails(
    monkeypatch, client, icon_with_creds
) -> None:
    """A Sanity 5xx must NOT lose the approval — the row gets saved, the
    response is 502 so the UI can show "approved, publish pending"."""
    bid = icon_with_creds

    from pipeline.publisher import sanity as sanity_mod

    async def raise_publish(self, draft_id, *, published_at=None):  # noqa: ANN001
        raise sanity_mod.SanityPublishError("503 Sanity unreachable")

    monkeypatch.setattr(
        sanity_mod.SanityPublisher,
        "promote_draft_to_published",
        raise_publish,
    )

    resp = client.post(
        f"/api/v1/drafts/post-down/approve?brand_id={bid}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["error"] == "sanity_publish_failed"
    assert body["detail"]["approval_status"] == "approved"

    # The DB row is still written (approval recorded, just not published).
    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1
    assert rows[0].status == "approved"
    assert rows[0].sanity_published_id is None
    assert rows[0].published_at is None


def test_reject_patches_status_field_by_default(
    monkeypatch, client, icon_with_creds
) -> None:
    """IT_PROJ_NTS_052 Content hub: reject's default path PATCHes the
    Sanity draft with ``status: "rejected"`` + ``rejectedAt`` instead of
    deleting it. The Rejected tab queries on that field; Restore unsets
    it (see test_unreject_clears_status_field)."""
    bid = icon_with_creds
    patches: list[tuple[str, dict, list[str] | None]] = []
    deleted: list[str] = []

    from pipeline.publisher import sanity as sanity_mod

    async def capture_patch(
        self, doc_id, set_fields=None, unset_fields=None
    ):  # noqa: ANN001
        patches.append((doc_id, set_fields or {}, unset_fields))
        return {}

    async def capture_delete(self, draft_id):  # noqa: ANN001
        deleted.append(draft_id)

    monkeypatch.setattr(sanity_mod.SanityClient, "patch", capture_patch)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "delete_draft", capture_delete
    )

    resp = client.post(
        f"/api/v1/drafts/post-junk/reject?brand_id={bid}",
        headers=AUTH,
        json={"note": "off-brand"},
    )
    assert resp.status_code == 200
    assert deleted == []  # default flow does NOT delete
    assert len(patches) == 1
    doc_id, set_fields, _ = patches[0]
    assert doc_id == "drafts.post-junk"
    assert set_fields["status"] == "rejected"
    assert set_fields["rejectionReason"] == "off-brand"
    assert "rejectedAt" in set_fields


def test_reject_deletes_draft_from_sanity_when_flag_on(
    monkeypatch, client, icon_with_creds
) -> None:
    """Back-compat: with the legacy ``DELETE_REJECTED_FROM_SANITY=true``
    env flag, reject reverts to NTS_051 behaviour and hard-deletes the
    draft document. Used by environments that don't want a Rejected tab
    audit trail."""
    bid = icon_with_creds
    deleted: list[str] = []

    from pipeline.common import config as config_module

    monkeypatch.setenv("DELETE_REJECTED_FROM_SANITY", "true")
    config_module._settings = None  # re-read settings

    from pipeline.publisher import sanity as sanity_mod

    async def capture_delete(self, draft_id):  # noqa: ANN001
        deleted.append(draft_id)

    async def capture_patch(
        self, doc_id, set_fields=None, unset_fields=None
    ):  # noqa: ANN001
        # Shouldn't be called in the legacy delete path.
        raise AssertionError("patch should not be called when flag is on")

    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "delete_draft", capture_delete
    )
    monkeypatch.setattr(sanity_mod.SanityClient, "patch", capture_patch)

    resp = client.post(
        f"/api/v1/drafts/post-legacy-junk/reject?brand_id={bid}",
        headers=AUTH,
        json={"note": "off-brand"},
    )
    assert resp.status_code == 200
    assert deleted == ["drafts.post-legacy-junk"]


def test_reject_preserves_doc_and_records_db_row_when_sanity_patch_fails(
    monkeypatch, client, icon_with_creds
) -> None:
    """A Sanity 5xx during the PATCH must NOT lose the rejection — the
    local DraftApproval row is the source of truth for the audit log."""
    bid = icon_with_creds

    from pipeline.publisher import sanity as sanity_mod

    async def boom_patch(
        self, doc_id, set_fields=None, unset_fields=None
    ):  # noqa: ANN001
        raise RuntimeError("503 sanity unreachable")

    monkeypatch.setattr(sanity_mod.SanityClient, "patch", boom_patch)

    resp = client.post(
        f"/api/v1/drafts/post-flaky/reject?brand_id={bid}",
        headers=AUTH,
        json={"note": "fyi"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1 and rows[0].status == "rejected"


def test_unreject_clears_status_field_and_resets_db_row(
    monkeypatch, client, icon_with_creds
) -> None:
    """Restore action: the previously rejected draft returns to
    ``draft`` (pending) — Sanity ``status`` + ``rejectedAt`` are unset
    and the DB row's status flips back."""
    bid = icon_with_creds

    from pipeline.publisher import sanity as sanity_mod

    patches: list[tuple[str, dict | None, list[str] | None]] = []

    async def capture_patch(
        self, doc_id, set_fields=None, unset_fields=None
    ):  # noqa: ANN001
        patches.append((doc_id, set_fields, unset_fields))
        return {}

    monkeypatch.setattr(sanity_mod.SanityClient, "patch", capture_patch)

    # Reject first (so there's a row to flip).
    client.post(
        f"/api/v1/drafts/post-restore/reject?brand_id={bid}",
        headers=AUTH,
        json={"note": "wrong"},
    )

    resp = client.post(
        f"/api/v1/drafts/post-restore/unreject?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"

    # The unreject patch should be the most recent — set_fields None,
    # unset_fields contains the rejection markers.
    assert patches, "expected at least one patch call"
    last_doc, last_set, last_unset = patches[-1]
    assert last_doc == "drafts.post-restore"
    assert last_set is None
    assert last_unset is not None
    assert "status" in last_unset
    assert "rejectedAt" in last_unset

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.query(DraftApproval).all()
    assert len(rows) == 1 and rows[0].status == "draft"


def test_unreject_unknown_brand_returns_404(client) -> None:
    resp = client.post(
        "/api/v1/drafts/post-x/unreject?brand_id=9999", headers=AUTH
    )
    assert resp.status_code == 404


def test_permanent_delete_only_works_when_rejected(
    monkeypatch, client, icon_with_creds
) -> None:
    """Safety gate: DELETE /drafts/{id} refuses unless Sanity confirms
    the doc is currently in the rejected state."""
    bid = icon_with_creds
    deleted: list[str] = []

    from pipeline.publisher import sanity as sanity_mod

    async def query_pending(self, groq, params=None):  # noqa: ANN001
        return {
            "_id": "drafts.post-pending-x",
            "status": None,
            "isDraft": True,
        }

    async def capture_delete(self, draft_id):  # noqa: ANN001
        deleted.append(draft_id)

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query_pending)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "delete_draft", capture_delete
    )

    resp = client.delete(
        f"/api/v1/drafts/post-pending-x?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 400
    assert "rejected" in resp.json()["detail"].lower()
    assert deleted == []


def test_permanent_delete_succeeds_for_rejected_doc(
    monkeypatch, client, icon_with_creds
) -> None:
    """Happy path: the doc is currently rejected → delete proceeds and
    returns 204."""
    bid = icon_with_creds
    deleted: list[str] = []

    from pipeline.publisher import sanity as sanity_mod

    async def query_rejected(self, groq, params=None):  # noqa: ANN001
        return {
            "_id": "drafts.post-bye",
            "status": "rejected",
            "isDraft": True,
        }

    async def capture_delete(self, draft_id):  # noqa: ANN001
        deleted.append(draft_id)

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query_rejected)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "delete_draft", capture_delete
    )

    resp = client.delete(
        f"/api/v1/drafts/post-bye?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 204
    assert deleted == ["drafts.post-bye"]


def _seed_local_refs(brand_id: int, draft_id: str) -> None:
    """Insert a topic + cost_record + draft_approval all pointing at
    ``draft_id`` (normalised) so deletion/backfill purge logic has
    something to clean (IT_PROJ_NTS_062)."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    factory = admin_db.get_session_factory()
    with factory() as session:
        src = Source(
            brand_id_fk=brand_id,
            name="seed-source",
            source_type="rss",
            url="https://example.com/feed",
            primary_category="markets",
        )
        session.add(src)
        session.flush()
        run = Run(
            brand_id_fk=brand_id,
            triggered_by="test",
            source_ids="[]",
            started_at=now,
            status="success",
        )
        session.add(run)
        session.flush()
        session.add(
            Topic(
                run_id=run.id,
                topic_id="topic-seed",
                source_id=src.id,
                title="Seed topic",
                status="passed",
                draft_id=draft_id,
                language="en",
            )
        )
        session.add(
            CostRecord(
                brand_id_fk=brand_id,
                draft_id=draft_id,
                provider="anthropic",
                operation="completion",
                cost_usd=0.0123,
            )
        )
        session.add(
            DraftApproval(
                sanity_draft_id=draft_id,
                brand_id_fk=brand_id,
                status="rejected",
                decided_by="admin",
            )
        )
        session.commit()


def _count_local_refs(draft_id: str) -> tuple[int, int, int]:
    """(topics, cost_records, draft_approvals) still pointing at draft_id."""
    factory = admin_db.get_session_factory()
    with factory() as session:
        topics = (
            session.query(Topic).filter(Topic.draft_id == draft_id).count()
        )
        costs = (
            session.query(CostRecord)
            .filter(CostRecord.draft_id == draft_id)
            .count()
        )
        approvals = (
            session.query(DraftApproval)
            .filter(DraftApproval.sanity_draft_id == draft_id)
            .count()
        )
    return topics, costs, approvals


def test_permanent_delete_purges_local_refs_for_rejected_doc(
    monkeypatch, client, icon_with_creds
) -> None:
    """Delete of an existing rejected draft removes it from Sanity AND
    snips every admin.db reference (IT_PROJ_NTS_062)."""
    bid = icon_with_creds
    draft_id = "drafts.post-bye"
    _seed_local_refs(bid, draft_id)
    assert _count_local_refs(draft_id) == (1, 1, 1)

    deleted: list[str] = []
    from pipeline.publisher import sanity as sanity_mod

    async def query_rejected(self, groq, params=None):  # noqa: ANN001
        return {"_id": draft_id, "status": "rejected", "isDraft": True}

    async def capture_delete(self, did):  # noqa: ANN001
        deleted.append(did)

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query_rejected)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "delete_draft", capture_delete
    )

    resp = client.delete(
        f"/api/v1/drafts/post-bye?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 204
    assert deleted == [draft_id]
    # topics/cost_records keep the row but lose the dead pointer;
    # the approval row is dropped entirely.
    assert _count_local_refs(draft_id) == (0, 0, 0)


def test_permanent_delete_is_idempotent_when_doc_missing(
    monkeypatch, client, icon_with_creds
) -> None:
    """If Sanity already has no such doc, Delete is NOT an error: it skips
    the Sanity mutation, still purges local refs, and returns 204
    (idempotency — IT_PROJ_NTS_062)."""
    bid = icon_with_creds
    draft_id = "drafts.post-4670c339e90e"
    _seed_local_refs(bid, draft_id)
    assert _count_local_refs(draft_id) == (1, 1, 1)

    deleted: list[str] = []
    from pipeline.publisher import sanity as sanity_mod

    async def query_empty(self, groq, params=None):  # noqa: ANN001
        return None

    async def capture_delete(self, did):  # noqa: ANN001
        deleted.append(did)

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query_empty)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "delete_draft", capture_delete
    )

    resp = client.delete(
        f"/api/v1/drafts/post-4670c339e90e?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 204
    # No Sanity mutation attempted — the doc was already gone.
    assert deleted == []
    # Local refs cleaned up all the same.
    assert _count_local_refs(draft_id) == (0, 0, 0)


def test_get_deleted_draft_returns_404_not_500(
    monkeypatch, client, icon_with_creds
) -> None:
    """GET on a draft Sanity no longer has → semantic 404, never a 500."""
    bid = icon_with_creds
    from pipeline.publisher import sanity as sanity_mod

    async def query_gone(self, groq, params=None):  # noqa: ANN001
        # Detail endpoint's combined GROQ — neither draft nor published.
        return {"draft": None, "published": None}

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query_gone)

    resp = client.get(
        f"/api/v1/drafts/post-4670c339e90e?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 404, resp.text


def test_approve_all_siblings_publishes_each_and_reports_per_language(
    monkeypatch, client, icon_with_creds
) -> None:
    """Batch endpoint: every pending sibling for a topic gets approved +
    published; response enumerates per-language status."""
    bid = icon_with_creds

    from pipeline.publisher import sanity as sanity_mod

    # Sanity returns 4 sibling drafts for topic-001. The published-mirror
    # query returns None for each (so all are "fresh").
    drafts_rows = [
        {"_id": "drafts.post-en-001", "language": "en"},
        {"_id": "drafts.post-ru-001", "language": "ru"},
        {"_id": "drafts.post-uk-001", "language": "uk"},
        {"_id": "drafts.post-pl-001", "language": "pl"},
    ]
    publish_calls: list[str] = []

    async def query(self, groq, params=None):  # noqa: ANN001
        # Two query shapes: "find siblings", and "find published mirror".
        if "topicId" in groq:
            return drafts_rows
        return None  # mirror check: no published doc yet

    async def fake_promote(self, draft_id, *, published_at=None):  # noqa: ANN001
        publish_calls.append(draft_id)
        return draft_id.replace("drafts.", "")

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher,
        "promote_draft_to_published",
        fake_promote,
    )

    resp = client.post(
        f"/api/v1/drafts/topic/topic-001/approve-all-siblings?brand_id={bid}",
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topic_id"] == "topic-001"
    assert body["ok_count"] == 4
    assert body["fail_count"] == 0
    statuses = {r["language"]: r["status"] for r in body["results"]}
    assert statuses == {
        "en": "published",
        "ru": "published",
        "uk": "published",
        "pl": "published",
    }
    # promote_draft_to_published called 4 times — one per sibling.
    assert sorted(publish_calls) == sorted(d["_id"] for d in drafts_rows)


def test_approve_all_siblings_skips_already_published(
    monkeypatch, client, icon_with_creds
) -> None:
    """A sibling whose published mirror already exists must be reported
    as 'skipped' instead of re-promoted (would clobber operator edits)."""
    bid = icon_with_creds

    from pipeline.publisher import sanity as sanity_mod

    drafts_rows = [
        {"_id": "drafts.post-en-002", "language": "en"},
        {"_id": "drafts.post-ru-002", "language": "ru"},
    ]
    promote_calls: list[str] = []

    async def query(self, groq, params=None):  # noqa: ANN001
        if "topicId" in groq:
            return drafts_rows
        # mirror check: en-002 already published; ru-002 not yet.
        return (
            "post-en-002"
            if params and params.get("id") == "post-en-002"
            else None
        )

    async def fake_promote(self, draft_id, *, published_at=None):  # noqa: ANN001
        promote_calls.append(draft_id)
        return draft_id.replace("drafts.", "")

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher,
        "promote_draft_to_published",
        fake_promote,
    )

    resp = client.post(
        f"/api/v1/drafts/topic/topic-002/approve-all-siblings?brand_id={bid}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok_count"] == 1
    statuses = {r["language"]: r["status"] for r in body["results"]}
    assert statuses == {"en": "skipped", "ru": "published"}
    # Only the un-published sibling got promoted.
    assert promote_calls == ["drafts.post-ru-002"]


def test_approve_all_siblings_partial_failure_does_not_rollback(
    monkeypatch, client, icon_with_creds
) -> None:
    """If 3 publish OK and 1 fails, the 3 stay published — we don't try
    to undo. The 1 failure surfaces as 'approved_publish_pending'."""
    bid = icon_with_creds

    from pipeline.publisher import sanity as sanity_mod

    drafts_rows = [
        {"_id": "drafts.post-en-003", "language": "en"},
        {"_id": "drafts.post-ru-003", "language": "ru"},
        {"_id": "drafts.post-uk-003", "language": "uk"},
        {"_id": "drafts.post-pl-003", "language": "pl"},
    ]

    async def query(self, groq, params=None):  # noqa: ANN001
        if "topicId" in groq:
            return drafts_rows
        return None

    async def selective_promote(self, draft_id, *, published_at=None):  # noqa: ANN001
        if "uk-003" in draft_id:
            raise sanity_mod.SanityPublishError("uk-specific failure")
        return draft_id.replace("drafts.", "")

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)
    monkeypatch.setattr(
        sanity_mod.SanityPublisher,
        "promote_draft_to_published",
        selective_promote,
    )

    resp = client.post(
        f"/api/v1/drafts/topic/topic-003/approve-all-siblings?brand_id={bid}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok_count"] == 3
    assert body["fail_count"] == 1
    statuses = {r["language"]: r["status"] for r in body["results"]}
    assert statuses["uk"] == "approved_publish_pending"
    assert statuses["en"] == "published"
    # uk-specific failure detail bubbled through
    uk_result = next(r for r in body["results"] if r["language"] == "uk")
    assert "uk-specific" in uk_result["detail"]


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
