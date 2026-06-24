"""Regenerate image applies ONE cover to all language siblings (NTS_069).

The cover lives only in Sanity, as a per-document ``coverImage`` asset ref.
The pipeline already shares one asset across a topic's languages at creation
(``generate_image_for_topic`` runs once per topic); the bug was that the
Regenerate-image action patched only the clicked draft, so siblings kept the
old asset. The fix patches every sibling in one Sanity transaction, and still
generates (pays for) exactly one image.

The image stack (Replicate/Sanity HTTP) is faked — these tests assert the
orchestration, not the model.
"""

from __future__ import annotations

import asyncio

import pytest

from pipeline.admin import image_regenerate as ir


@pytest.fixture
def fake_image_stack(monkeypatch, tmp_path):
    """Stub Replicate + Sanity so regenerate_cover_image runs offline.

    Returns a dict capturing the generate calls, uploads, and the Sanity
    mutation transactions issued.
    """
    # Keep the brand lookup hermetic (no icon row → None, swallowed).
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    from pipeline.common import config as config_module

    monkeypatch.setattr(config_module, "_settings", None)

    captured: dict = {"generate": [], "uploads": [], "transactions": [], "siblings": None}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def query(self, groq, params=None):  # noqa: ANN001
            params = params or {}
            if "tid" in params:
                return captured["siblings"]
            # The read for title/topicId.
            return {
                "title": "Tax Advisory",
                "topicId": "topic-1",
                "sourceUrl": "https://example.com/tax",
            }

        async def mutate(self, mutations):  # noqa: ANN001
            captured["transactions"].append(mutations)
            return {}

    class FakePublisher:
        def __init__(self, *a, **k) -> None:
            pass

        async def upload_cover_image(self, image_bytes, filename):  # noqa: ANN001
            captured["uploads"].append(filename)
            return "image-NEWASSET"

    class FakeGen:
        def __init__(self, *a, **k) -> None:
            pass

        async def generate(self, topic, visual, operation=None, scene=None):  # noqa: ANN001
            captured["generate"].append(operation)
            captured.setdefault("scenes", []).append(scene)
            return "https://replicate.test/master.png"

    async def fake_fetch_master(url):  # noqa: ANN001
        return b"masterpng"

    def fake_resize(data, channel):  # noqa: ANN001
        return b"resized"

    monkeypatch.setattr(ir, "SanityClient", FakeClient)
    monkeypatch.setattr(ir, "SanityPublisher", FakePublisher)
    monkeypatch.setattr(ir, "ImageGenerator", FakeGen)
    monkeypatch.setattr(ir, "fetch_master", fake_fetch_master)
    monkeypatch.setattr(ir, "resize_for_channel", fake_resize)
    return captured


def _coverref_ids(transaction: list[dict]) -> dict[str, str]:
    """Map patched doc id → coverImage asset _ref for one transaction."""
    out: dict[str, str] = {}
    for m in transaction:
        p = m["patch"]
        out[p["id"]] = p["set"]["coverImage"]["asset"]["_ref"]
    return out


def test_regenerate_applies_new_cover_to_all_siblings(fake_image_stack):
    fake_image_stack["siblings"] = [
        {"_id": "drafts.post-en"},
        {"_id": "post-pl"},
        {"_id": "drafts.post-ru"},
        {"_id": "post-uk"},
    ]

    asset = asyncio.run(ir.regenerate_cover_image("post-en"))

    assert asset == "image-NEWASSET"
    # Exactly ONE image generated (→ cost recorded once) and ONE upload.
    assert fake_image_stack["generate"] == ["image_regenerate"]
    assert len(fake_image_stack["uploads"]) == 1
    # ONE atomic Sanity transaction covering every sibling, all → new asset.
    assert len(fake_image_stack["transactions"]) == 1
    patched = _coverref_ids(fake_image_stack["transactions"][0])
    assert set(patched) == {"drafts.post-en", "post-pl", "drafts.post-ru", "post-uk"}
    assert set(patched.values()) == {"image-NEWASSET"}


def test_regenerate_with_missing_siblings_patches_what_exists(fake_image_stack):
    # Topic resolves but only the originating doc comes back.
    fake_image_stack["siblings"] = []

    asset = asyncio.run(ir.regenerate_cover_image("drafts.post-solo"))

    assert asset == "image-NEWASSET"
    assert len(fake_image_stack["transactions"]) == 1
    patched = _coverref_ids(fake_image_stack["transactions"][0])
    # Falls back to the originating draft form — no 500, no empty transaction.
    assert patched == {"drafts.post-solo": "image-NEWASSET"}


def test_regenerate_generates_and_uploads_exactly_once(fake_image_stack):
    """Cost is paid once per regeneration regardless of sibling count."""
    fake_image_stack["siblings"] = [{"_id": f"post-{i}"} for i in range(4)]

    asyncio.run(ir.regenerate_cover_image("post-0"))

    assert len(fake_image_stack["generate"]) == 1
    assert len(fake_image_stack["uploads"]) == 1
    # Re-running generates a fresh image (a new paid op) but still exactly one,
    # and patches all siblings to the latest asset — no partial/dup state.
    asyncio.run(ir.regenerate_cover_image("post-0"))
    assert len(fake_image_stack["generate"]) == 2
    assert len(fake_image_stack["transactions"]) == 2
    for tx in fake_image_stack["transactions"]:
        assert {m["patch"]["id"] for m in tx} == {f"post-{i}" for i in range(4)}
