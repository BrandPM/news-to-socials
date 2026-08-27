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
    """Answers the script's two GROQ shapes from an in-memory dataset.

    The filter strings are the script's own constants, so the fake honours the
    same scope the real GROQ would: a ``published`` query never sees drafts,
    and the drafts pass sees published siblings only as reuse donors.
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.mutations: list[list[dict]] = []

    @staticmethod
    def _in_scope(groq: str, row: dict) -> bool:
        if bf._PENDING_DRAFT in groq:
            return bf._row_is_pending_draft(row)
        if bf._PUBLISHED in groq:
            return not row.get("isDraft")
        # The drafts pass's donor query: pending drafts + every published post.
        return bf._row_is_pending_draft(row) or not row.get("isDraft")

    async def query(self, groq, params=None):  # noqa: ANN001
        params = params or {}
        rows = [r for r in self.rows if self._in_scope(groq, r)]
        if "topicId in $topics" in groq:
            wanted = set(params.get("topics") or [])
            return [r for r in rows if r.get("topicId") in wanted]
        # The cover-less-posts query.
        return [r for r in rows if not r.get("coverImageRef")]

    async def mutate(self, mutations):  # noqa: ANN001
        self.mutations.append(mutations)
        return {}


def _row(sid, lang, topic="topic-1", cover=None, title=None, status=None):
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
        "status": status,
        "isDraft": str(sid).startswith("drafts."),
    }


def _draft_row(lang, topic="topic-1", cover=None, status=None, title=None):
    return _row(
        f"drafts.post-{lang}", lang, topic=topic, cover=cover, title=title,
        status=status,
    )


def test_no_candidates_when_every_post_has_a_cover() -> None:
    client = FakeClient(
        [_row(f"post-{lang}", lang, cover="image-x") for lang in ("en", "ru")]
    )
    assert asyncio.run(bf.find_candidates(client)) == []


def test_drafts_are_not_candidates_for_the_default_target() -> None:
    """The default GROQ excludes drafts — ``--target drafts`` owns those.
    Asserted via the filter string so a future edit that drops it fails here."""
    assert '!(_id in path("drafts.**"))' in bf._PUBLISHED


def test_default_target_ignores_cover_less_drafts() -> None:
    """A cover-less pending draft is invisible to the published sweep, so the
    default invocation's behaviour is unchanged by NTS_091."""
    client = FakeClient([_draft_row("en"), _draft_row("ru")])
    assert asyncio.run(bf.find_candidates(client)) == []


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


# ---------------------------------------------------------------------------
# NTS_091 Task A — ``--target drafts``: sweep the pending drafts the publish
# guard now blocks for a missing cover.
# ---------------------------------------------------------------------------

DRAFTS = bf.TARGETS["drafts"]


def test_drafts_target_finds_cover_less_pending_drafts() -> None:
    client = FakeClient([_draft_row(lang) for lang in ("en", "ru", "uk", "pl")])
    groups = asyncio.run(bf.find_candidates(client, DRAFTS))

    assert len(groups) == 1, "4 languages of one story = ONE image, not four"
    g = groups[0]
    assert g.action == "generate"
    assert [s.sanity_id for s in g.missing] == [
        "drafts.post-en",
        "drafts.post-ru",
        "drafts.post-uk",
        "drafts.post-pl",
    ]
    assert g.canonical.language == "en", "EN still drives the prompt"


def test_drafts_target_skips_rejected_drafts() -> None:
    """A rejected draft is heading for deletion — no cover, no spend."""
    client = FakeClient(
        [_draft_row("en", status="rejected"), _draft_row("ru", status="rejected")]
    )
    assert asyncio.run(bf.find_candidates(client, DRAFTS)) == []


def test_drafts_target_treats_absent_status_as_pending() -> None:
    """Pre-NTS_052 drafts carry no ``status`` field at all."""
    client = FakeClient([_draft_row("en", status=None)])
    groups = asyncio.run(bf.find_candidates(client, DRAFTS))
    assert [s.sanity_id for s in groups[0].missing] == ["drafts.post-en"]


def test_drafts_target_reuses_a_published_siblings_cover() -> None:
    """One cover per topic (NTS_069) spans the draft/published boundary: if EN
    is already live with a cover, the RU draft adopts it instead of paying for
    a second image that would show a different picture for the same story."""
    client = FakeClient(
        [
            _row("post-en", "en", cover="image-live"),
            _draft_row("ru"),
        ]
    )
    groups = asyncio.run(bf.find_candidates(client, DRAFTS))

    assert len(groups) == 1
    g = groups[0]
    assert g.action == "reuse"
    assert g.existing_cover_ref == "image-live"
    # The published sibling is a donor only — never a patch target here.
    assert [s.sanity_id for s in g.missing] == ["drafts.post-ru"]

    outcome = asyncio.run(bf._apply_group(client, g))
    assert "reused image-live" in outcome
    assert [m["patch"]["id"] for m in client.mutations[0]] == ["drafts.post-ru"]


def test_drafts_target_does_not_patch_a_cover_less_published_sibling() -> None:
    """A published post with no cover belongs to the ``published`` pass. The
    drafts pass lists it (``!``) but leaves it alone."""
    client = FakeClient([_row("post-en", "en"), _draft_row("ru")])
    groups = asyncio.run(bf.find_candidates(client, DRAFTS))

    g = groups[0]
    assert [s.sanity_id for s in g.missing] == ["drafts.post-ru"]
    assert g.languages == "EN!,RU*"


def test_drafts_target_generate_attributes_cost_to_the_draft(monkeypatch) -> None:
    client = FakeClient([_draft_row(lang) for lang in ("en", "ru")])
    groups = asyncio.run(bf.find_candidates(client, DRAFTS))

    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return "image-NEW"

    from pipeline.admin import image_regenerate as ir

    monkeypatch.setattr(ir, "generate_and_apply_cover", fake_generate)
    monkeypatch.setattr(bf, "SanityPublisher", lambda **kw: object())

    asyncio.run(bf._apply_group(client, groups[0]))

    assert len(calls) == 1, "one image for the whole topic"
    assert calls[0]["target_ids"] == ["drafts.post-en", "drafts.post-ru"]
    # The spend lands on the draft the manager is about to review.
    assert calls[0]["cost_doc_id"] == "drafts.post-en"


def test_drafts_dry_run_writes_nothing_and_estimates(capsys) -> None:
    client = FakeClient(
        [
            _draft_row("en", topic="topic-a"),
            _draft_row("ru", topic="topic-a"),
            _row("post-b-en", "en", topic="topic-b", cover="image-b"),
            _row("drafts.post-b-ru", "ru", topic="topic-b"),
        ]
    )
    rc = asyncio.run(bf._run(client, apply=False, target="drafts"))

    assert rc == 0
    assert client.mutations == []
    out = capsys.readouterr().out
    assert "[drafts]" in out
    assert "2 topic(s) / 3 document(s)" in out
    assert "1 topic(s) need a NEW image" in out
    assert "1 topic(s) can REUSE" in out
    assert "~$0.04 estimated" in out
    assert "DRY RUN" in out


def test_drafts_dry_run_reports_an_empty_queue(capsys) -> None:
    client = FakeClient([_draft_row("en", cover="image-x")])
    asyncio.run(bf._run(client, apply=False, target="drafts"))
    assert "No pending drafts are missing a cover image" in capsys.readouterr().out


def test_target_all_runs_both_passes(capsys) -> None:
    client = FakeClient([_row("post-en", "en", topic="topic-a"), _draft_row("ru", topic="topic-b")])
    asyncio.run(bf._run(client, apply=False, target="all"))

    out = capsys.readouterr().out
    assert "[published]" in out
    assert "[drafts]" in out
    # Published first — a cover it gains is a free donor for the drafts pass.
    assert out.index("[published]") < out.index("[drafts]")


def test_sweep_never_approves_or_publishes_a_draft(monkeypatch) -> None:
    """The whole point of the drafts sweep: it sets coverImage and nothing
    else. No status flip, no publish — the draft goes back in the queue."""
    client = FakeClient(
        [_row("post-en", "en", cover="image-live"), _draft_row("ru")]
    )
    asyncio.run(bf._run(client, apply=True, target="drafts"))

    assert client.mutations, "the draft must actually be patched"
    for tx in client.mutations:
        for m in tx:
            assert set(m["patch"]) == {"id", "set"}
            assert set(m["patch"]["set"]) == {"coverImage"}


# --- NTS_094 guard rail ---------------------------------------------------
#
# `--target drafts` is exactly the command that would mass-generate the covers
# cover-on-demand decided not to buy. The published pass is untouched: a live
# article with no cover is damage whatever the pipeline does.


def _cover_less_draft_pair() -> FakeClient:
    return FakeClient([_draft_row("en", topic="topic-a"), _draft_row("ru", topic="topic-a")])


def test_drafts_dry_run_warns_loudly_under_images_on_demand(capsys) -> None:
    client = _cover_less_draft_pair()
    rc = asyncio.run(
        bf._run(client, apply=False, target="drafts", images_on_demand=True)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "images_on_demand ON" in out
    assert "decided not to buy" in out
    # Still lists the candidates — seeing what WOULD be generated is the point.
    assert "[drafts]" in out
    assert client.mutations == []


def test_drafts_apply_is_refused_without_the_override(capsys) -> None:
    client = _cover_less_draft_pair()
    rc = asyncio.run(
        bf._run(client, apply=True, target="drafts", images_on_demand=True)
    )

    assert client.mutations == [], "not one cover may be written"
    out = capsys.readouterr().out
    assert "REFUSING to --apply" in out
    assert "--override-images-on-demand" in out
    # Non-zero so a scripted caller cannot read the refusal as "nothing to do".
    assert rc == 2


def test_the_override_lets_a_deliberate_sweep_through(monkeypatch, capsys) -> None:
    """The legitimate case: drafts stranded from before the flag went on."""
    client = _cover_less_draft_pair()

    async def fake_generate(**kwargs):
        return "image-NEW"

    from pipeline.admin import image_regenerate as ir

    monkeypatch.setattr(ir, "generate_and_apply_cover", fake_generate)
    monkeypatch.setattr(bf, "SanityPublisher", lambda **kw: object())

    rc = asyncio.run(
        bf._run(
            client,
            apply=True,
            target="drafts",
            images_on_demand=True,
            override_on_demand=True,
        )
    )

    assert rc == 0
    # The warning is still printed — the override permits, it does not silence.
    assert "images_on_demand ON" in capsys.readouterr().out


def test_published_backfill_is_unaffected_by_the_flag(monkeypatch, capsys) -> None:
    """A live article with no cover is damage regardless of how covers are
    made now, so the published pass neither warns nor refuses."""
    client = FakeClient([_row("post-en", "en", topic="topic-a")])

    async def fake_generate(**kwargs):
        return "image-NEW"

    from pipeline.admin import image_regenerate as ir

    monkeypatch.setattr(ir, "generate_and_apply_cover", fake_generate)
    monkeypatch.setattr(bf, "SanityPublisher", lambda **kw: object())

    rc = asyncio.run(
        bf._run(client, apply=True, target="published", images_on_demand=True)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "images_on_demand ON" not in out
    assert "REFUSING" not in out


def test_target_all_repairs_published_and_still_refuses_drafts(
    monkeypatch, capsys
) -> None:
    """``--target all --apply`` must not be an all-or-nothing choice: the live
    site still gets repaired, only the drafts half is held back."""
    client = FakeClient(
        [_row("post-en", "en", topic="topic-a"), _draft_row("ru", topic="topic-b")]
    )
    generated: list[str] = []

    async def fake_generate(**kwargs):
        generated.append(kwargs["cost_doc_id"])
        return "image-NEW"

    from pipeline.admin import image_regenerate as ir

    monkeypatch.setattr(ir, "generate_and_apply_cover", fake_generate)
    monkeypatch.setattr(bf, "SanityPublisher", lambda **kw: object())

    rc = asyncio.run(
        bf._run(client, apply=True, target="all", images_on_demand=True)
    )

    assert rc == 2
    assert generated == ["post-en"], "only the published article was repaired"
    assert "REFUSING to --apply" in capsys.readouterr().out


def test_flag_off_leaves_the_drafts_sweep_exactly_as_it_was(capsys) -> None:
    client = _cover_less_draft_pair()
    rc = asyncio.run(bf._run(client, apply=False, target="drafts"))

    out = capsys.readouterr().out
    assert rc == 0
    assert "images_on_demand ON" not in out
    assert "DRY RUN" in out


def test_flag_read_is_fail_safe_when_the_db_is_unreadable(monkeypatch) -> None:
    """A broken admin.db must not block a published repair — and must never
    be read as 'on demand is off, go generate more'. It returns False, which
    only removes the guard rail; nothing downstream generates because of it."""
    from pipeline.admin import db as admin_db_mod

    def boom():
        raise RuntimeError("no admin.db here")

    monkeypatch.setattr(admin_db_mod, "get_session_factory", boom)
    assert bf._images_on_demand("icon") is False
