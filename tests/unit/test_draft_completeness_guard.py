"""NTS_090 — pre-publish completeness guard.

The incident: articles went live with ``coverImage: null``. These tests pin
the two halves of the fix — the validator itself, and the fact that the
publish path refuses to mutate Sanity when the validator objects.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.draft_validation import validate_draft_complete
from pipeline.admin.models import DraftApproval
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-guard"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


def complete_doc(**overrides) -> dict:
    """A document that passes every check; override one key per test."""
    doc = {
        "_id": "drafts.post-ok",
        "language": "en",
        "title": "A real title",
        "displayDate": "2026-08-05",
        "slug": "a-real-title-en",
        "coverImageRef": "image-deadbeef-1792x1008-png",
        "bodyBlockCount": 8,
        "bodyH2Count": 3,
    }
    doc.update(overrides)
    return doc


# --- validator -------------------------------------------------------------


def test_complete_doc_has_nothing_missing() -> None:
    assert validate_draft_complete(complete_doc()) == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"coverImageRef": None}, "coverImage"),
        ({"coverImageRef": ""}, "coverImage"),
        ({"title": None}, "title"),
        ({"title": "   "}, "title"),
        ({"slug": None}, "slug"),
        ({"bodyBlockCount": 0, "bodyH2Count": 0}, "body"),
        ({"bodyH2Count": 0}, "body_h2"),
        ({"displayDate": None}, "displayDate"),
    ],
)
def test_each_missing_component_is_reported(overrides, code) -> None:
    assert validate_draft_complete(complete_doc(**overrides)) == [code]


def test_empty_body_reports_body_not_body_h2() -> None:
    """An absent body already implies "no H2" — reporting both is noise."""
    missing = validate_draft_complete(
        complete_doc(bodyBlockCount=0, bodyH2Count=0)
    )
    assert missing == ["body"]


def test_missing_everything_lists_every_component() -> None:
    assert validate_draft_complete({"_id": "drafts.post-empty"}) == [
        "coverImage",
        "title",
        "slug",
        "body",
        "displayDate",
    ]


def test_unresolvable_doc_is_never_reported_as_complete() -> None:
    """A failed read must not read as a pass."""
    assert validate_draft_complete(None)
    assert validate_draft_complete({})


def test_validator_accepts_the_raw_sanity_shape() -> None:
    """The detail view passes the raw doc (nested cover ref, real body)."""
    raw = {
        "title": "Raw",
        "displayDate": "2026-08-05",
        "slug": {"current": "raw-en"},
        "coverImage": {
            "_type": "image",
            "asset": {"_type": "reference", "_ref": "image-abc"},
        },
        "body": [
            {"_type": "block", "style": "normal", "children": []},
            {"_type": "block", "style": "h2", "children": []},
        ],
    }
    assert validate_draft_complete(raw) == []
    del raw["coverImage"]
    assert validate_draft_complete(raw) == ["coverImage"]


def test_raw_body_without_h2_reports_body_h2() -> None:
    raw = {
        "title": "Raw",
        "displayDate": "2026-08-05",
        "slug": {"current": "raw-en"},
        "coverImageRef": "image-abc",
        "body": [{"_type": "block", "style": "normal", "children": []}],
    }
    assert validate_draft_complete(raw) == ["body_h2"]


def test_cover_object_without_asset_ref_is_missing() -> None:
    """``coverImage: {}`` with no asset ref is as missing as no field."""
    doc = complete_doc()
    del doc["coverImageRef"]
    doc["coverImage"] = {"_type": "image"}
    assert validate_draft_complete(doc) == ["coverImage"]


# --- publish route ---------------------------------------------------------


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
def brand_id(client):
    factory = admin_db.get_session_factory()
    with factory() as session:
        bid = seed_icon_brand(session, with_sanity_creds=True)
        session.commit()
    return bid


@pytest.fixture
def no_sanity_writes(monkeypatch):
    """Record any attempt to mutate Sanity. The guard's whole promise is
    that a blocked publish performs none of these."""
    from pipeline.publisher import sanity as sanity_mod

    calls: list[str] = []

    async def spy_promote(self, draft_id, *, published_at=None):  # noqa: ANN001
        calls.append(draft_id)
        return draft_id.replace("drafts.", "")

    async def spy_mutate(self, mutations):  # noqa: ANN001
        calls.append("mutate")
        return {}

    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "promote_draft_to_published", spy_promote
    )
    monkeypatch.setattr(sanity_mod.SanityClient, "mutate", spy_mutate)
    return calls


def _stub_guard_fetch(monkeypatch, doc):
    from pipeline.admin.routes import drafts as drafts_routes

    async def fake_fetch(client, sanity_id):  # noqa: ANN001
        return None if doc is None else {**doc, "_id": sanity_id}

    monkeypatch.setattr(
        drafts_routes, "fetch_draft_for_validation", fake_fetch
    )


def test_approve_blocked_with_422_when_cover_is_null(
    monkeypatch, client, brand_id, no_sanity_writes
) -> None:
    """The incident, as a test: no cover → 422, no publish, no approval row."""
    _stub_guard_fetch(monkeypatch, complete_doc(coverImageRef=None))

    resp = client.post(
        f"/api/v1/drafts/post-nocover/approve?brand_id={brand_id}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "draft_incomplete"
    assert detail["sanity_id"] == "drafts.post-nocover"
    assert detail["language"] == "en"
    assert detail["missing"] == ["coverImage"]

    # Nothing was written: not to Sanity, not to admin.db.
    assert no_sanity_writes == []
    factory = admin_db.get_session_factory()
    with factory() as session:
        assert session.query(DraftApproval).count() == 0


def test_approve_proceeds_when_complete(
    monkeypatch, client, brand_id, no_sanity_writes
) -> None:
    _stub_guard_fetch(monkeypatch, complete_doc())

    resp = client.post(
        f"/api/v1/drafts/post-ok/approve?brand_id={brand_id}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert no_sanity_writes == ["drafts.post-ok"]


def test_approve_reports_every_missing_component(
    monkeypatch, client, brand_id, no_sanity_writes
) -> None:
    _stub_guard_fetch(
        monkeypatch,
        complete_doc(coverImageRef=None, displayDate=None, bodyH2Count=0),
    )

    resp = client.post(
        f"/api/v1/drafts/post-bad/approve?brand_id={brand_id}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["missing"] == [
        "coverImage",
        "body_h2",
        "displayDate",
    ]


def test_approve_fails_closed_when_sanity_read_fails(
    monkeypatch, client, brand_id, no_sanity_writes
) -> None:
    """If completeness can't be verified we don't publish and hope."""
    from pipeline.admin.routes import drafts as drafts_routes

    async def boom(client, sanity_id):  # noqa: ANN001
        raise RuntimeError("sanity is down")

    monkeypatch.setattr(drafts_routes, "fetch_draft_for_validation", boom)

    resp = client.post(
        f"/api/v1/drafts/post-x/approve?brand_id={brand_id}",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 502
    assert "completeness" in resp.json()["detail"]
    assert no_sanity_writes == []


def test_approve_all_siblings_blocked_when_one_sibling_incomplete(
    monkeypatch, client, brand_id, no_sanity_writes
) -> None:
    """Siblings ship as one unit — one incomplete language blocks the batch,
    and the response names which language is missing what."""
    from pipeline.publisher import sanity as sanity_mod

    rows = [
        complete_doc(_id="drafts.post-en-1", language="en"),
        complete_doc(_id="drafts.post-ru-1", language="ru", coverImageRef=None),
        complete_doc(_id="drafts.post-uk-1", language="uk"),
        complete_doc(_id="drafts.post-pl-1", language="pl"),
    ]

    async def query(self, groq, params=None):  # noqa: ANN001
        if "topicId" in groq:
            return rows
        return None

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)

    resp = client.post(
        f"/api/v1/drafts/topic/topic-1/approve-all-siblings?brand_id={brand_id}",
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "topic_incomplete"
    assert detail["topic_id"] == "topic-1"
    assert detail["missing_by_language"] == {"ru": ["coverImage"]}

    # Not one sibling was promoted — including the three complete ones.
    assert no_sanity_writes == []
    factory = admin_db.get_session_factory()
    with factory() as session:
        assert session.query(DraftApproval).count() == 0


def test_approve_all_siblings_proceeds_when_all_complete(
    monkeypatch, client, brand_id, no_sanity_writes
) -> None:
    from pipeline.publisher import sanity as sanity_mod

    rows = [
        complete_doc(_id="drafts.post-en-2", language="en"),
        complete_doc(_id="drafts.post-ru-2", language="ru"),
    ]

    async def query(self, groq, params=None):  # noqa: ANN001
        if "topicId" in groq:
            return rows
        return None

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)
    _stub_guard_fetch(monkeypatch, complete_doc())

    resp = client.post(
        f"/api/v1/drafts/topic/topic-2/approve-all-siblings?brand_id={brand_id}",
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok_count"] == 2
    assert sorted(no_sanity_writes) == ["drafts.post-en-2", "drafts.post-ru-2"]


def test_draft_detail_surfaces_missing_components(
    monkeypatch, client, brand_id
) -> None:
    """The manager sees WHY before clicking anything."""
    from pipeline.publisher import sanity as sanity_mod

    async def query(self, groq, params=None):  # noqa: ANN001
        return {
            "draft": {
                "title": "No cover here",
                "body": [
                    {"_type": "block", "style": "h2", "children": []},
                    {"_type": "block", "style": "normal", "children": []},
                ],
                "generatedBy": {"name": "pipeline", "brandSlug": "icon"},
                "language": "en",
                "displayDate": "2026-08-05",
                "slug": "no-cover-here",
                "coverImageRef": None,
                "coverImageUrl": None,
                "_createdAt": "2026-08-05T10:00:00Z",
            },
            "published": None,
        }

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)

    resp = client.get(
        f"/api/v1/drafts/post-nocover?brand_id={brand_id}", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft"]["missing"] == ["coverImage"]


def test_draft_list_surfaces_missing_components(
    monkeypatch, client, brand_id
) -> None:
    from pipeline.publisher import sanity as sanity_mod

    counts = {"total": 1, "pending": 1, "published": 0, "rejected": 0,
              "en": 1, "ru": 0, "uk": 0, "pl": 0}
    items = [
        {
            "_id": "drafts.post-listed",
            "title": "Listed",
            "language": "en",
            "topicId": "topic-9",
            "_createdAt": "2026-08-05T10:00:00Z",
            "displayDate": "2026-08-05",
            "slug": "listed-en",
            "coverImageUrl": None,
            "coverImageRef": None,
            "bodyBlockCount": 5,
            "bodyH2Count": 2,
        }
    ]

    async def query(self, groq, params=None):  # noqa: ANN001
        if groq.startswith("{"):
            return counts
        if "topicId in $topics" in groq:
            return []
        return items

    monkeypatch.setattr(sanity_mod.SanityClient, "query", query)

    resp = client.get(f"/api/v1/drafts?brand_id={brand_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["missing"] == ["coverImage"]
