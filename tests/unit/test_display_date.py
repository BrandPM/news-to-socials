"""Tests for publication-date control (IT_PROJ_NTS_089 / spec NTS_084).

Covers:
* ``compute_display_date`` priority (RSS pubDate → creation) + clamps
  (future → today, missing → creation).
* ``compute_published_at`` (today → real time; past → noon UTC; none → now).
* ``publish_draft`` writes ``displayDate`` onto the Sanity doc.
* ``promote_draft_to_published`` sets ``publishedAt`` from ``displayDate`` and
  gives every sibling the identical stamp; never touches ``_updatedAt``.
* ``PATCH /drafts/{id}/display-date`` validation (rejects future) + shared
  sibling patch.
* ``pipeline_config.stale_draft_days`` GET/PUT.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import PipelineConfig
from pipeline.common import config as config_module
from pipeline.common.display_date import (
    compute_display_date,
    compute_published_at,
    parse_display_date,
)
from pipeline.publisher.sanity import SanityPostInput, SanityPublisher
from tests.unit.conftest import seed_icon_brand

# --- pure helpers ----------------------------------------------------------

NOW = datetime(2026, 7, 12, 15, 30, tzinfo=timezone.utc)


def test_display_date_uses_rss_pubdate() -> None:
    d, src = compute_display_date(datetime(2026, 7, 9, 8, 0, tzinfo=timezone.utc), NOW)
    assert d.isoformat() == "2026-07-09"
    assert src == "rss_pubdate"


def test_display_date_clamps_future_to_today() -> None:
    d, src = compute_display_date(datetime(2026, 7, 20, tzinfo=timezone.utc), NOW)
    assert d.isoformat() == "2026-07-12"
    assert src == "clamped_future"


def test_display_date_falls_back_to_creation_when_missing() -> None:
    d, src = compute_display_date(None, NOW)
    assert d.isoformat() == "2026-07-12"
    assert src == "fallback_creation"


def test_display_date_treats_naive_pubdate_as_utc() -> None:
    d, src = compute_display_date(datetime(2026, 7, 9, 8, 0), NOW)
    assert d.isoformat() == "2026-07-09"
    assert src == "rss_pubdate"


def test_published_at_today_uses_real_time() -> None:
    assert compute_published_at("2026-07-12", NOW) == NOW


def test_published_at_past_uses_noon_utc() -> None:
    got = compute_published_at("2026-07-09", NOW)
    assert got == datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_published_at_none_falls_back_to_now() -> None:
    assert compute_published_at(None, NOW) == NOW


def test_parse_display_date_rejects_garbage() -> None:
    assert parse_display_date("not-a-date") is None
    assert parse_display_date("") is None
    assert parse_display_date(None) is None
    assert parse_display_date("2026-07-09").isoformat() == "2026-07-09"


# --- publish_draft writes displayDate + promote sets publishedAt -----------


class _FakeClient:
    """Minimal SanityClient stand-in capturing mutate payloads."""

    def __init__(self, doc: dict | None = None, query_returns: list | None = None):
        self._doc = doc
        self._queue = list(query_returns or [])
        self.mutations: list[list[dict]] = []

    async def query(self, groq: str, params=None):  # noqa: ANN001
        if self._queue:
            return self._queue.pop(0)
        return self._doc

    async def mutate(self, mutations):  # noqa: ANN001
        self.mutations.append(mutations)
        return {}

    async def create_draft(self, doc):  # noqa: ANN001
        did = doc.get("_id") or "post-x"
        if not str(did).startswith("drafts."):
            did = f"drafts.{did}"
        doc["_id"] = did
        self.mutations.append([{"create": doc}])
        return did


def test_publish_draft_writes_display_date() -> None:
    from pipeline.common.models import Language

    # query() is used by slug dedup → return None (slug free).
    fake = _FakeClient(query_returns=[None])
    pub = SanityPublisher(client=fake)  # type: ignore[arg-type]
    post = SanityPostInput(
        title="T",
        body_markdown="Body.",
        language=Language.en,
        category="wealth",
        source_url="https://example.com/a",
        topic_id="topic-1",
        display_date="2026-07-09",
    )
    import asyncio

    asyncio.run(pub.publish_draft(post))
    created = fake.mutations[0][0]["create"]
    assert created["displayDate"] == "2026-07-09"


def test_promote_sets_published_at_from_display_date() -> None:
    import asyncio

    doc = {
        "_id": "drafts.post-1",
        "_type": "post",
        "_updatedAt": "2026-07-12T09:00:00Z",
        "title": "T",
        "displayDate": "2026-07-09",
        "publishedAt": "2026-07-12T09:00:00Z",  # stale (approval-time) value
    }
    fake = _FakeClient(doc=doc)
    pub = SanityPublisher(client=fake)  # type: ignore[arg-type]
    asyncio.run(pub.promote_draft_to_published("drafts.post-1"))
    replace = fake.mutations[0][0]["createOrReplace"]
    # publishedAt is the display date at noon UTC, NOT the approval time.
    assert replace["publishedAt"] == "2026-07-09T12:00:00+00:00"
    # _updatedAt is Sanity-managed — never carried over.
    assert "_updatedAt" not in replace


def test_promote_uses_shared_published_at_across_siblings() -> None:
    import asyncio

    shared = datetime(2026, 7, 12, 15, 30, tzinfo=timezone.utc)
    stamps = []
    for did in ("drafts.post-en", "drafts.post-ru", "drafts.post-uk", "drafts.post-pl"):
        doc = {"_id": did, "_type": "post", "displayDate": "2026-07-12"}
        fake = _FakeClient(doc=doc)
        pub = SanityPublisher(client=fake)  # type: ignore[arg-type]
        asyncio.run(pub.promote_draft_to_published(did, published_at=shared))
        stamps.append(fake.mutations[0][0]["createOrReplace"]["publishedAt"])
    assert len(set(stamps)) == 1
    assert stamps[0] == shared.isoformat()


# --- PATCH /display-date + config ------------------------------------------

ADMIN_TOKEN = "tok-dd"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand_id = seed_icon_brand(session, with_sanity_creds=True)
        session.add(
            PipelineConfig(
                brand_id_fk=brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases="[]",
                voice_profile="x\n",
            )
        )
        session.commit()
    from pipeline.admin.server import create_app

    c = TestClient(create_app())
    c._brand_id = brand_id  # type: ignore[attr-defined]
    yield c
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def test_patch_display_date_rejects_future(client) -> None:
    bid = client._brand_id  # type: ignore[attr-defined]
    resp = client.patch(
        f"/api/v1/drafts/post-x/display-date?brand_id={bid}",
        headers=AUTH,
        json={"display_date": "2099-01-01"},
    )
    assert resp.status_code == 422


def test_patch_display_date_rejects_bad_format(client) -> None:
    bid = client._brand_id  # type: ignore[attr-defined]
    resp = client.patch(
        f"/api/v1/drafts/post-x/display-date?brand_id={bid}",
        headers=AUTH,
        json={"display_date": "07/09/2026"},
    )
    assert resp.status_code == 422


def test_patch_display_date_patches_all_siblings(client, monkeypatch) -> None:
    from pipeline.publisher import sanity as sanity_mod

    bid = client._brand_id  # type: ignore[attr-defined]
    calls: dict[str, object] = {}

    # query() #1 → topicId of the target; #2 → sibling draft ids.
    query_queue = [{"topicId": "topic-42"}, ["drafts.post-en", "drafts.post-ru"]]

    async def fake_query(self, groq, params=None):  # noqa: ANN001
        return query_queue.pop(0)

    async def fake_mutate(self, mutations):  # noqa: ANN001
        calls["mutations"] = mutations
        return {}

    monkeypatch.setattr(sanity_mod.SanityClient, "query", fake_query)
    monkeypatch.setattr(sanity_mod.SanityClient, "mutate", fake_mutate)

    resp = client.patch(
        f"/api/v1/drafts/post-en/display-date?brand_id={bid}",
        headers=AUTH,
        json={"display_date": "2026-07-09"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_date"] == "2026-07-09"
    assert set(body["updated_draft_ids"]) == {"drafts.post-en", "drafts.post-ru"}
    muts = calls["mutations"]
    assert len(muts) == 2
    assert all(m["patch"]["set"]["displayDate"] == "2026-07-09" for m in muts)


def test_config_exposes_stale_draft_days_default(client) -> None:
    bid = client._brand_id  # type: ignore[attr-defined]
    resp = client.get(f"/api/v1/config?brand_id={bid}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["stale_draft_days"] == 3


def test_config_updates_stale_draft_days(client) -> None:
    bid = client._brand_id  # type: ignore[attr-defined]
    resp = client.put(
        f"/api/v1/config?brand_id={bid}",
        headers=AUTH,
        json={"stale_draft_days": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["stale_draft_days"] == 5


def test_config_rejects_out_of_range_stale_draft_days(client) -> None:
    bid = client._brand_id  # type: ignore[attr-defined]
    resp = client.put(
        f"/api/v1/config?brand_id={bid}",
        headers=AUTH,
        json={"stale_draft_days": 999},
    )
    assert resp.status_code == 422
