"""NTS_090 Task B — cover-image backfill candidate finding + grouping.

The Sanity HTTP layer is faked; these assert the orchestration decisions the
script makes (what counts as a candidate, one image per TOPIC not per
document, reuse before generate, dry-run writes nothing).
"""

from __future__ import annotations

import asyncio

import pytest

from scripts import backfill_cover_images as bf


class FakeClient:
    """Answers the script's two GROQ shapes from an in-memory dataset."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.mutations: list[list[dict]] = []

    async def query(self, groq, params=None):  # noqa: ANN001
        params = params or {}
        if "topicId in $topics" in groq:
            wanted = set(params.get("topics") or [])
            return [r for r in self.rows if r.get("topicId") in wanted]
        # The cover-less-posts query.
        return [r for r in self.rows if not r.get("coverImageRef")]

    async def mutate(self, mutations):  # noqa: ANN001
        self.mutations.append(mutations)
        return {}


def _row(sid, lang, topic="topic-1", cover=None, title=None):
    return {
        "_id": sid,
        "topicId": topic,
        "language": lang,
        "title": title or f"Title {lang}",
        "excerpt": f"Summary {lang}",
        "keyTakeaway": "k",
        "sourceUrl": "https://example.com/a",
        "slug": f"slug-{lang}",
        "coverImageRef": cover,
    }


def test_no_candidates_when_every_post_has_a_cover() -> None:
    client = FakeClient(
        [_row(f"post-{lang}", lang, cover="image-x") for lang in ("en", "ru")]
    )
    assert asyncio.run(bf.find_candidates(client)) == []


def test_drafts_are_not_candidates() -> None:
    """The GROQ excludes drafts — Task A's guard owns those. Asserted via the
    filter string so a future edit that drops it fails here."""
    assert '!(_id in path("drafts.**"))' in bf._PUBLISHED


def test_siblings_are_grouped_into_one_topic() -> None:
    client = FakeClient([_row(f"post-{lang}", lang) for lang in ("en", "ru", "uk", "pl")])
    groups = asyncio.run(bf.find_candidates(client))

    assert len(groups) == 1, "4 languages of one story = ONE image, not four"
    g = groups[0]
    assert g.topic_id == "topic-1"
    assert len(g.missing) == 4
    assert g.action == "generate"
    assert g.canonical.language == "en", "EN is the canonical prompt source"


def test_topic_with_one_covered_sibling_reuses_instead_of_generating() -> None:
    client = FakeClient(
        [
            _row("post-en", "en", cover="image-existing"),
            _row("post-ru", "ru"),
        ]
    )
    groups = asyncio.run(bf.find_candidates(client))

    assert len(groups) == 1
    g = groups[0]
    assert g.action == "reuse"
    assert g.existing_cover_ref == "image-existing"
    # Only the cover-less sibling is a target — a live cover is never replaced.
    assert [s.sanity_id for s in g.missing] == ["post-ru"]


def test_posts_without_a_topic_id_are_repaired_individually() -> None:
    client = FakeClient(
        [
            _row("post-orphan-1", "en", topic=None),
            _row("post-orphan-2", "ru", topic=None),
        ]
    )
    groups = asyncio.run(bf.find_candidates(client))

    assert len(groups) == 2
    assert all(g.topic_id is None for g in groups)
    assert all(len(g.missing) == 1 for g in groups)


def test_language_column_marks_which_siblings_lack_a_cover() -> None:
    client = FakeClient(
        [_row("post-en", "en", cover="image-existing"), _row("post-ru", "ru")]
    )
    groups = asyncio.run(bf.find_candidates(client))
    assert groups[0].languages == "EN,RU*"


def test_apply_reuse_patches_only_the_missing_sibling() -> None:
    client = FakeClient(
        [
            _row("post-en", "en", cover="image-existing"),
            _row("post-ru", "ru"),
            _row("post-uk", "uk"),
        ]
    )
    groups = asyncio.run(bf.find_candidates(client))
    outcome = asyncio.run(bf._apply_group(client, groups[0]))

    assert "reused image-existing" in outcome
    assert len(client.mutations) == 1, "one atomic transaction"
    patched = {m["patch"]["id"]: m["patch"]["set"]["coverImage"] for m in client.mutations[0]}
    assert set(patched) == {"post-ru", "post-uk"}
    assert all(
        p["asset"]["_ref"] == "image-existing" for p in patched.values()
    )


def test_apply_generate_uses_the_shared_cover_path_once(monkeypatch) -> None:
    """One generation per topic, applied to every sibling, driven by the EN
    title — the NTS_069 contract, reused rather than re-implemented."""
    client = FakeClient([_row(f"post-{lang}", lang) for lang in ("en", "ru", "uk")])
    groups = asyncio.run(bf.find_candidates(client))

    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return "image-NEW"

    from pipeline.admin import image_regenerate as ir

    monkeypatch.setattr(ir, "generate_and_apply_cover", fake_generate)
    monkeypatch.setattr(bf, "SanityPublisher", lambda **kw: object())

    outcome = asyncio.run(bf._apply_group(client, groups[0]))

    assert "generated image-NEW" in outcome
    assert len(calls) == 1
    assert calls[0]["title"] == "Title en"
    assert calls[0]["summary"] == "Summary en"
    assert calls[0]["topic_id"] == "topic-1"
    assert sorted(calls[0]["target_ids"]) == ["post-en", "post-ru", "post-uk"]
    # Cost attributed to a published id — these have no draft any more.
    assert calls[0]["cost_doc_id"] == "post-en"
    assert not calls[0]["cost_doc_id"].startswith("drafts.")


def test_dry_run_writes_nothing(capsys) -> None:
    client = FakeClient([_row(f"post-{lang}", lang) for lang in ("en", "ru")])
    rc = asyncio.run(bf._run(client, apply=False))

    assert rc == 0
    assert client.mutations == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "~$0.04" in out, "the estimate belongs in the dry-run header"


def test_dry_run_estimate_counts_generate_topics_only(capsys) -> None:
    client = FakeClient(
        [
            _row("post-a-en", "en", topic="topic-a"),
            _row("post-b-en", "en", topic="topic-b", cover="image-b"),
            _row("post-b-ru", "ru", topic="topic-b"),
        ]
    )
    asyncio.run(bf._run(client, apply=False))
    out = capsys.readouterr().out
    assert "1 topic(s) need a NEW image" in out
    assert "1 topic(s) can REUSE" in out
    assert "~$0.04 estimated" in out


def test_one_topic_failure_does_not_stop_the_rest(monkeypatch, capsys) -> None:
    client = FakeClient(
        [
            _row("post-a-en", "en", topic="topic-a", cover="image-a"),
            _row("post-a-ru", "ru", topic="topic-a"),
            _row("post-b-en", "en", topic="topic-b", cover="image-b"),
            _row("post-b-ru", "ru", topic="topic-b"),
        ]
    )

    async def flaky(cl, group):  # noqa: ANN001
        if group.topic_id == "topic-a":
            raise RuntimeError("sanity 500")
        return "patched"

    monkeypatch.setattr(bf, "_apply_group", flaky)
    rc = asyncio.run(bf._run(client, apply=True))

    assert rc == 1, "a partial run must exit non-zero"
    captured = capsys.readouterr()
    assert "FAIL [topic-a]" in captured.err
    assert "OK   [topic-b]" in captured.out
    assert "Repaired 1/2" in captured.out


@pytest.mark.parametrize("apply_flag", [True, False])
def test_publishedat_is_never_touched(monkeypatch, apply_flag) -> None:
    """NTS_089 owns publishedAt; this script writes coverImage and nothing
    else."""
    client = FakeClient(
        [_row("post-en", "en", cover="image-existing"), _row("post-ru", "ru")]
    )
    asyncio.run(bf._run(client, apply=apply_flag))
    for tx in client.mutations:
        for m in tx:
            assert set(m["patch"]["set"]) == {"coverImage"}
