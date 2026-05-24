"""Integration tests for /api/v1/drafts/* routes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin import jobs as admin_jobs
from pipeline.common import config as config_module
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "tok-drafts"
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

    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()
    admin_jobs.reset_image_jobs_for_tests()


@pytest.fixture
def icon_with_creds(client):
    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session, with_sanity_creds=True)
        session.commit()
    return icon_id


# --- Image regenerate (unchanged from S1) -------------------------------


def test_regenerate_image_returns_202_and_job_completes(monkeypatch, client) -> None:
    captured: dict = {}

    async def fake_regenerate(draft_id: str, custom_prompt):  # noqa: ANN001
        captured["draft_id"] = draft_id
        captured["custom_prompt"] = custom_prompt
        return "image-asset-xyz"

    from pipeline.admin import image_regenerate

    monkeypatch.setattr(image_regenerate, "regenerate_cover_image", fake_regenerate)

    resp = client.post(
        "/api/v1/drafts/post-abc123/regenerate-image",
        headers=AUTH,
        json={"custom_prompt": "warm marble texture"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert isinstance(job_id, str) and len(job_id) >= 8

    resp = client.get(f"/api/v1/drafts/jobs/{job_id}/status", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "done"
    assert body["asset_id"] == "image-asset-xyz"
    assert body["error"] is None
    assert captured["custom_prompt"] == "warm marble texture"


def test_regenerate_image_error_state(monkeypatch, client) -> None:
    async def boom(draft_id: str, custom_prompt):  # noqa: ANN001
        raise RuntimeError("replicate 500")

    from pipeline.admin import image_regenerate

    monkeypatch.setattr(image_regenerate, "regenerate_cover_image", boom)

    resp = client.post(
        "/api/v1/drafts/post-broken/regenerate-image", headers=AUTH, json={}
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    resp = client.get(f"/api/v1/drafts/jobs/{job_id}/status", headers=AUTH)
    body = resp.json()
    assert body["state"] == "error"
    assert "replicate 500" in body["error"]


def test_status_for_unknown_job_is_404(client) -> None:
    resp = client.get("/api/v1/drafts/jobs/nonexistent/status", headers=AUTH)
    assert resp.status_code == 404


# --- GET /drafts/{sanity_id} --------------------------------------------


def test_get_draft_404_when_brand_missing(client) -> None:
    resp = client.get(
        "/api/v1/drafts/drafts.post-x?brand_id=999", headers=AUTH
    )
    assert resp.status_code == 404


def test_get_draft_409_when_brand_has_no_credentials(client) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        bid = seed_icon_brand(session, with_sanity_creds=False)
        session.commit()
    resp = client.get(
        f"/api/v1/drafts/drafts.post-x?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 409


def test_get_draft_fetches_and_returns_detail(monkeypatch, client, icon_with_creds) -> None:
    bid = icon_with_creds
    fake_doc = {
        "title": "India credit fund regime",
        "body": [
            {
                "_type": "block",
                "style": "normal",
                "children": [{"text": "The proposal moves the discussion."}],
            },
            {
                "_type": "block",
                "style": "h2",
                "children": [{"text": "Mezzanine repricing"}],
            },
        ],
        "keyTakeaway": "Allocators should revisit assumptions.",
        "generatedBy": {"name": "pipeline", "brandSlug": "icon"},
        "_createdAt": "2026-05-21T10:00:00Z",
        "coverImageUrl": "https://cdn.sanity.io/x.png",
    }
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", AsyncMock(return_value=fake_doc)
    )

    resp = client.get(
        f"/api/v1/drafts/post-aaa?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "India credit fund regime"
    assert "## Mezzanine repricing" in body["body_markdown"]
    assert body["brand_slug"] == "icon"
    assert body["cover_image_url"].endswith(".png")
    assert body["cost_total_usd"] == 0.0
    assert body["cost_breakdown"] == []


def test_get_draft_cross_brand_guard_returns_403(
    monkeypatch, client, icon_with_creds
) -> None:
    """A draft tagged generatedBy.brandSlug='neovox' must be rejected
    when accessed via brand_id of icon. NTS_025 Step 4 guard."""
    bid = icon_with_creds
    fake_doc = {
        "title": "Cross-brand draft",
        "body": [],
        "keyTakeaway": None,
        "generatedBy": {"name": "pipeline", "brandSlug": "neovox"},
        "_createdAt": "2026-05-21T10:00:00Z",
        "coverImageUrl": None,
    }
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", AsyncMock(return_value=fake_doc)
    )
    resp = client.get(
        f"/api/v1/drafts/post-mismatch?brand_id={bid}", headers=AUTH
    )
    assert resp.status_code == 403
    assert "cross-brand" in resp.json()["detail"].lower()


def test_get_draft_aggregates_cost_records(monkeypatch, client, icon_with_creds) -> None:
    """When cost_records reference the draft_id, they're rolled up into
    cost_total_usd + breakdown."""
    bid = icon_with_creds
    fake_doc = {
        "title": "X",
        "body": [],
        "keyTakeaway": None,
        "generatedBy": {"name": "pipeline", "brandSlug": "icon"},
        "_createdAt": "2026-05-21T10:00:00Z",
        "coverImageUrl": None,
    }
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", AsyncMock(return_value=fake_doc)
    )
    from pipeline.admin.config_client import AdminConfigClient

    for op, amt in (("draft", 0.10), ("polish", 0.15), ("image_master", 0.04)):
        AdminConfigClient.record_cost(
            brand_id_fk=bid,
            draft_id="drafts.post-aaa",
            provider="openai" if "image" not in op else "replicate",
            operation=op,
            cost_usd=amt,
        )

    resp = client.get(
        f"/api/v1/drafts/post-aaa?brand_id={bid}", headers=AUTH
    )
    body = resp.json()
    assert body["cost_total_usd"] == pytest.approx(0.29)
    ops = {item["operation"] for item in body["cost_breakdown"]}
    assert ops == {"draft", "polish", "image_master"}


def test_get_draft_surfaces_language_and_topic_id(
    monkeypatch, client, icon_with_creds
) -> None:
    """S6.7 — detail endpoint must return ``language`` and ``topic_id`` so
    the frontend header can render a language badge + Siblings link
    without a second round-trip."""
    bid = icon_with_creds
    fake_doc = {
        "title": "Polish credit fund piece",
        "body": [],
        "keyTakeaway": None,
        "generatedBy": {"name": "pipeline", "brandSlug": "icon"},
        "_createdAt": "2026-05-24T19:15:25Z",
        "coverImageUrl": None,
        "language": "pl",
        "topicId": "6b640357e6a87b25",
    }
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", AsyncMock(return_value=fake_doc)
    )
    resp = client.get(
        f"/api/v1/drafts/post-pl?brand_id={bid}", headers=AUTH
    )
    body = resp.json()
    assert body["language"] == "pl"
    assert body["topic_id"] == "6b640357e6a87b25"


# --- S6.7 list endpoint --------------------------------------------------


def _make_list_query_mock(counts: dict, items: list[dict]) -> AsyncMock:
    """Two sequential calls: counts faceting, then items page."""
    return AsyncMock(side_effect=[counts, items])


def test_list_drafts_returns_items_with_language_counts(
    monkeypatch, client, icon_with_creds
) -> None:
    bid = icon_with_creds
    counts = {"total": 7, "en": 3, "ru": 2, "uk": 1, "pl": 1}
    items = [
        {
            "_id": "drafts.post-pl",
            "title": "Polski",
            "language": "pl",
            "topicId": "topic-1",
            "_createdAt": "2026-05-24T19:15:25Z",
            "coverImageUrl": "https://cdn/x.jpg",
        },
        {
            "_id": "drafts.post-uk",
            "title": "Українська",
            "language": "uk",
            "topicId": "topic-1",
            "_createdAt": "2026-05-24T19:14:55Z",
            "coverImageUrl": None,
        },
    ]
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", _make_list_query_mock(counts, items)
    )
    resp = client.get(f"/api/v1/drafts?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 7
    assert body["by_language"] == {"en": 3, "ru": 2, "uk": 1, "pl": 1}
    assert [it["sanity_id"] for it in body["items"]] == [
        "drafts.post-pl",
        "drafts.post-uk",
    ]
    assert body["items"][0]["language"] == "pl"
    assert body["items"][0]["topic_id"] == "topic-1"
    assert body["items"][0]["approval_status"] == "draft"
    assert body["has_more"] is True


def test_list_drafts_filters_by_language(
    monkeypatch, client, icon_with_creds
) -> None:
    """The ``language`` query param narrows the items slice but the
    ``by_language`` totals stay brand-wide so the tab strip stays stable."""
    bid = icon_with_creds
    counts = {"total": 4, "en": 1, "ru": 1, "uk": 1, "pl": 1}
    items_pl = [
        {
            "_id": "drafts.post-pl",
            "title": "Polski",
            "language": "pl",
            "topicId": "topic-1",
            "_createdAt": "2026-05-24T19:15:25Z",
            "coverImageUrl": None,
        }
    ]
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", _make_list_query_mock(counts, items_pl)
    )
    resp = client.get(
        f"/api/v1/drafts?brand_id={bid}&language=pl", headers=AUTH
    )
    body = resp.json()
    assert body["total"] == 4
    assert body["by_language"]["pl"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["language"] == "pl"
    assert body["has_more"] is False


def test_list_drafts_filters_by_topic_id_for_siblings(
    monkeypatch, client, icon_with_creds
) -> None:
    """The detail page's Siblings panel queries with ``topic_id`` to find
    every language sibling of one topic."""
    bid = icon_with_creds
    counts = {"total": 2, "en": 0, "ru": 0, "uk": 1, "pl": 1}
    items = [
        {
            "_id": "drafts.post-pl",
            "title": "Polski",
            "language": "pl",
            "topicId": "shared-topic",
            "_createdAt": "2026-05-24T19:15:25Z",
            "coverImageUrl": None,
        },
        {
            "_id": "drafts.post-uk",
            "title": "Українська",
            "language": "uk",
            "topicId": "shared-topic",
            "_createdAt": "2026-05-24T19:14:55Z",
            "coverImageUrl": None,
        },
    ]
    from pipeline.publisher import sanity as sanity_mod

    mock = _make_list_query_mock(counts, items)
    monkeypatch.setattr(sanity_mod.SanityClient, "query", mock)
    resp = client.get(
        f"/api/v1/drafts?brand_id={bid}&topic_id=shared-topic", headers=AUTH
    )
    body = resp.json()
    assert body["total"] == 2
    assert {it["language"] for it in body["items"]} == {"pl", "uk"}
    # Both Sanity queries were issued with the topic param.
    first_call = mock.await_args_list[0]
    assert first_call.args[1]["topic"] == "shared-topic"


def test_list_drafts_rejects_unknown_brand(monkeypatch, client) -> None:
    resp = client.get("/api/v1/drafts?brand_id=99999", headers=AUTH)
    assert resp.status_code == 404


def test_list_drafts_409_when_brand_has_no_sanity_creds(
    monkeypatch, client
) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        bid = seed_brand(
            session, slug="neovox", with_sanity_creds=False
        ).id
        session.commit()
    resp = client.get(f"/api/v1/drafts?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 409


def test_list_drafts_merges_approval_status(
    monkeypatch, client, icon_with_creds
) -> None:
    """Approval rows for drafts in the slice get folded into
    ``approval_status`` so the list row can render a badge with no
    second round-trip."""
    bid = icon_with_creds
    counts = {"total": 1, "en": 0, "ru": 0, "uk": 0, "pl": 1}
    items = [
        {
            "_id": "drafts.post-pl-approved",
            "title": "Polski",
            "language": "pl",
            "topicId": "topic-1",
            "_createdAt": "2026-05-24T19:15:25Z",
            "coverImageUrl": None,
        }
    ]
    from pipeline.publisher import sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod.SanityClient, "query", _make_list_query_mock(counts, items)
    )
    # Approve the draft first.
    client.post(
        f"/api/v1/drafts/post-pl-approved/approve?brand_id={bid}",
        headers=AUTH,
        json={"note": "ok"},
    )
    resp = client.get(f"/api/v1/drafts?brand_id={bid}", headers=AUTH)
    body = resp.json()
    assert body["items"][0]["approval_status"] == "approved"
