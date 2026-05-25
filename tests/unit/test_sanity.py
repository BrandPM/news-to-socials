"""Unit tests for the Sanity publisher helpers."""

from __future__ import annotations

from pipeline.publisher.sanity import (
    estimate_read_time,
    excerpt_from_body,
    extract_toc_from_body,
    markdown_to_portable_text,
)


# --- read time -------------------------------------------------------


def test_read_time_short_post() -> None:
    text = "word " * 250
    assert estimate_read_time(text) == 1


def test_read_time_longer_post() -> None:
    text = "word " * 750
    assert estimate_read_time(text) == 3


def test_read_time_floor_one() -> None:
    assert estimate_read_time("hi") == 1


def test_read_time_cap_60() -> None:
    text = "word " * 100_000
    assert estimate_read_time(text) == 60


# --- excerpt ---------------------------------------------------------


def test_excerpt_first_paragraph() -> None:
    body = "First paragraph here.\n\nSecond paragraph follows."
    assert excerpt_from_body(body) == "First paragraph here."


def test_excerpt_truncates_long_first_para() -> None:
    body = "x" * 500 + "\n\nSecond"
    out = excerpt_from_body(body, max_chars=100)
    assert len(out) <= 100
    assert out.endswith("…")


# --- portable text ---------------------------------------------------


def test_portable_text_simple_paragraph() -> None:
    blocks = markdown_to_portable_text("Hello world.")
    assert len(blocks) == 1
    assert blocks[0]["_type"] == "block"
    assert blocks[0]["style"] == "normal"
    assert blocks[0]["children"][0]["text"] == "Hello world."


def test_portable_text_h2_heading() -> None:
    blocks = markdown_to_portable_text("## A heading\n\nSome text.")
    assert blocks[0]["style"] == "h2"
    assert blocks[0]["children"][0]["text"] == "A heading"
    assert blocks[1]["style"] == "normal"


def test_portable_text_h3_heading() -> None:
    blocks = markdown_to_portable_text("### Subheading\n\nbody.")
    assert blocks[0]["style"] == "h3"


def test_portable_text_skips_empty_paragraphs() -> None:
    blocks = markdown_to_portable_text("First.\n\n\n\nSecond.\n\n")
    assert len(blocks) == 2


def test_portable_text_heading_glued_to_paragraph_is_split() -> None:
    """Real gpt-4o output sometimes emits ``## Heading\\nBody`` with a single
    newline, not the canonical ``## Heading\\n\\nBody``. We must still pull
    the heading into its own h2 block (IT_PROJ_NTS_013 Defect 3)."""
    md = (
        "Lede paragraph here.\n\n"
        "## A real heading\n"
        "Body that follows immediately without a blank line.\n\n"
        "## Another heading\nMore body."
    )
    blocks = markdown_to_portable_text(md)
    styles = [b["style"] for b in blocks]
    texts = [b["children"][0]["text"] for b in blocks]
    assert styles == ["normal", "h2", "normal", "h2", "normal"]
    assert texts[1] == "A real heading"
    assert texts[2].startswith("Body that follows")
    assert texts[3] == "Another heading"


def test_portable_text_joins_wrapped_paragraph_lines() -> None:
    """Multi-line paragraphs (no blank line between lines) collapse into
    a single normal block, not many."""
    md = "First line of the paragraph\nsecond line\nthird line.\n\nNext para."
    blocks = markdown_to_portable_text(md)
    assert [b["style"] for b in blocks] == ["normal", "normal"]
    assert blocks[0]["children"][0]["text"] == (
        "First line of the paragraph second line third line."
    )


def test_portable_text_each_block_has_unique_key() -> None:
    blocks = markdown_to_portable_text(
        "Para one.\n\nPara two.\n\nPara three."
    )
    keys = [b["_key"] for b in blocks]
    assert len(set(keys)) == len(keys)
    assert all(isinstance(k, str) and len(k) > 0 for k in keys)


# --- table of contents ----------------------------------------------


def test_toc_extracted_from_h2_and_h3() -> None:
    md = (
        "Lede paragraph here.\n\n"
        "## First section\n\n"
        "Body of first section.\n\n"
        "### A subheading\n\n"
        "More body.\n\n"
        "## Second section\n\n"
        "Closing thoughts."
    )
    blocks = markdown_to_portable_text(md)
    toc = extract_toc_from_body(blocks)
    assert toc == ["First section", "A subheading", "Second section"]


def test_toc_empty_when_no_headings() -> None:
    blocks = markdown_to_portable_text("Just a paragraph.\n\nAnother one.")
    assert extract_toc_from_body(blocks) == []


def test_toc_skips_empty_heading_text() -> None:
    blocks = [
        {"_type": "block", "style": "h2", "children": [{"_type": "span", "text": "  "}]},
        {"_type": "block", "style": "h2", "children": [{"_type": "span", "text": "Real heading"}]},
    ]
    assert extract_toc_from_body(blocks) == ["Real heading"]


def test_toc_tolerates_malformed_blocks() -> None:
    """A degenerate block list (missing children, non-dict entries) should
    not crash the publisher — we'd rather skip TOC than fail to publish."""
    blocks = [
        None,  # type: ignore[list-item]
        {"_type": "block", "style": "h2"},  # no children key
        {"_type": "block", "style": "h2", "children": [{"_type": "span"}]},  # no text
        {"_type": "block", "style": "h2", "children": [{"_type": "span", "text": "OK"}]},
    ]
    assert extract_toc_from_body(blocks) == ["OK"]


# --- Client integration tests (against mocked HTTP) -----------------


def _make_client():
    """Helper: build a SanityClient with explicit creds bypassing get_settings."""
    from pipeline.publisher.sanity import SanityClient
    return SanityClient(
        project_id="test-proj",
        dataset="production",
        api_version="2024-01-01",
        token="test-token",
    )


async def test_query_uses_post_with_json_body():
    """Regression: GROQ params must go through POST body, not URL.

    Previously we tried passing them as URL params with $ prefix — Sanity
    returned 400 for queries that reference parameters. POST with
    ``{"query": ..., "params": ...}`` is the supported transport.
    """
    import json as _json

    import respx
    from httpx import Response

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(
            "https://test-proj.api.sanity.io/v2024-01-01/data/query/production"
        ).mock(return_value=Response(200, json={"result": "ok-id"}))

        client = _make_client()
        result = await client.query(
            '*[_type=="post" && topicId==$tid][0]._id',
            {"tid": "abc123"},
        )

        assert result == "ok-id"
        request = route.calls[0].request
        payload = _json.loads(request.read())
        # The query string is sent verbatim.
        assert "$tid" in payload["query"]
        # Parameters are NOT $-prefixed in the JSON body (Sanity prefixes them
        # internally from the query references).
        assert payload["params"] == {"tid": "abc123"}


async def test_publish_draft_populates_table_of_contents():
    """publish_draft() must derive tableOfContents from body H2/H3 blocks."""
    from unittest.mock import AsyncMock

    from pipeline.common.models import Language
    from pipeline.publisher.sanity import SanityPostInput, SanityPublisher

    fake_client = AsyncMock()
    fake_client.create_draft = AsyncMock(return_value="drafts.post-xyz")
    # publish_draft() now runs a slug-uniqueness GROQ query before
    # creating the draft (S6 fix). Returning None = "no collision".
    fake_client.query = AsyncMock(return_value=None)

    publisher = SanityPublisher(client=fake_client)
    body_md = (
        "Lede paragraph.\n\n"
        "## The repricing of mezzanine credit\n\n"
        "Section body.\n\n"
        "## What allocators should do next\n\n"
        "Closing thought."
    )
    await publisher.publish_draft(
        SanityPostInput(
            title="A Title",
            body_markdown=body_md,
            language=Language.en,
            category="special",
            source_url="https://example.com/x",
            topic_id="t-1",
        )
    )

    fake_client.create_draft.assert_awaited_once()
    doc = fake_client.create_draft.await_args.args[0]
    assert doc["tableOfContents"] == [
        "The repricing of mezzanine credit",
        "What allocators should do next",
    ]


async def test_publish_draft_omits_toc_when_body_has_no_headings():
    from unittest.mock import AsyncMock

    from pipeline.common.models import Language
    from pipeline.publisher.sanity import SanityPostInput, SanityPublisher

    fake_client = AsyncMock()
    fake_client.create_draft = AsyncMock(return_value="drafts.post-xyz")
    # publish_draft() now runs a slug-uniqueness GROQ query before
    # creating the draft (S6 fix). Returning None = "no collision".
    fake_client.query = AsyncMock(return_value=None)

    publisher = SanityPublisher(client=fake_client)
    await publisher.publish_draft(
        SanityPostInput(
            title="t",
            body_markdown="Just paragraphs here.\n\nNo headings at all.",
            language=Language.en,
            category="special",
            source_url="https://example.com/x",
            topic_id="t-2",
        )
    )
    doc = fake_client.create_draft.await_args.args[0]
    # Absent rather than empty list — clearer in Studio.
    assert "tableOfContents" not in doc


async def test_publish_draft_sets_language_suffixed_slug():
    """RU draft must land at .../insights/<slug>-ru, not /insights/untitled."""
    from unittest.mock import AsyncMock

    from pipeline.common.models import Language
    from pipeline.publisher.sanity import SanityPostInput, SanityPublisher

    fake_client = AsyncMock()
    fake_client.create_draft = AsyncMock(return_value="drafts.post-ru-abc")
    fake_client.query = AsyncMock(return_value=None)

    publisher = SanityPublisher(client=fake_client)
    await publisher.publish_draft(
        SanityPostInput(
            title="Индия: новый кредитный фонд 500 млн",
            body_markdown="body",
            language=Language.ru,
            category="wealth",
            source_url="https://example.com/x",
            topic_id="t-ru-1",
        )
    )

    doc = fake_client.create_draft.await_args.args[0]
    slug_obj = doc["slug"]
    # Sanity expects the slug as an object {_type, current}, not a raw
    # string. (Previously the inline slugify produced a string-only field
    # that Sanity Studio refused to auto-populate.)
    assert slug_obj["_type"] == "slug"
    assert slug_obj["current"].endswith("-ru")
    assert slug_obj["current"].replace("-", "").isascii()


async def test_publish_draft_appends_dedupe_counter_on_slug_collision():
    """If the base slug is taken by a different doc, walk -2, -3, ..."""
    from unittest.mock import AsyncMock

    from pipeline.common.models import Language
    from pipeline.publisher.sanity import SanityPostInput, SanityPublisher

    fake_client = AsyncMock()
    fake_client.create_draft = AsyncMock(return_value="drafts.post-en-new")
    # First lookup returns a colliding id; second returns None → free.
    fake_client.query = AsyncMock(side_effect=["post-en-existing", None])

    publisher = SanityPublisher(client=fake_client)
    await publisher.publish_draft(
        SanityPostInput(
            title="India fund credit",
            body_markdown="body",
            language=Language.en,
            category="wealth",
            source_url="https://example.com/x",
            topic_id="t-en-1",
        )
    )

    doc = fake_client.create_draft.await_args.args[0]
    # Base "india-fund-credit" was taken → next candidate is "...-2".
    assert doc["slug"]["current"] == "india-fund-credit-2"
    # Two GROQ lookups: base + -2. Don't probe further than needed.
    assert fake_client.query.await_count == 2


async def test_publish_draft_skips_dedupe_when_collision_is_self():
    """Idempotent re-runs: a draft that finds its own _id should NOT bump to -2."""
    from unittest.mock import AsyncMock

    from pipeline.common.models import Language
    from pipeline.publisher.sanity import SanityPostInput, SanityPublisher

    fake_client = AsyncMock()
    fake_client.create_draft = AsyncMock(return_value="drafts.post-self")
    # query() is told to exclude $draft and $published — so a "real"
    # collision check returns None when the only existing match was us.
    fake_client.query = AsyncMock(return_value=None)

    publisher = SanityPublisher(client=fake_client)
    await publisher.publish_draft(
        SanityPostInput(
            title="An article",
            body_markdown="body",
            language=Language.en,
            category="wealth",
            source_url="https://example.com/x",
            topic_id="t-self-1",
        )
    )

    doc = fake_client.create_draft.await_args.args[0]
    # No "-2" appended.
    assert doc["slug"]["current"] == "an-article"
    # The GROQ payload must reference $draft and $published so the
    # uniqueness check can exclude our own document on retries.
    params = fake_client.query.await_args.args[1]
    assert "draft" in params and "published" in params


async def test_promote_draft_to_published_emits_create_or_replace_and_delete():
    """The new publish path issues a single mutate transaction with two
    operations: createOrReplace at the non-drafts. id, then delete the
    drafts. doc. Tests the happy path."""
    from unittest.mock import AsyncMock

    from pipeline.publisher.sanity import SanityPublisher

    draft_doc = {
        "_id": "drafts.post-abc",
        "_type": "post",
        "_rev": "rev-xyz",
        "_createdAt": "2026-05-25T10:00:00Z",
        "_updatedAt": "2026-05-25T11:00:00Z",
        "title": "T",
        "slug": {"_type": "slug", "current": "t-en"},
    }
    fake_client = AsyncMock()
    fake_client.query = AsyncMock(return_value=draft_doc)
    fake_client.mutate = AsyncMock(return_value={})

    publisher = SanityPublisher(client=fake_client)
    published_id = await publisher.promote_draft_to_published("drafts.post-abc")

    assert published_id == "post-abc"
    mutations = fake_client.mutate.await_args.args[0]
    # Transaction order matters: create-or-replace BEFORE delete, so an
    # interrupted call leaves the doc published even if the delete drops.
    assert mutations[0].keys() == {"createOrReplace"}
    assert mutations[1].keys() == {"delete"}
    new_doc = mutations[0]["createOrReplace"]
    # The non-draft id is set, system fields are stripped.
    assert new_doc["_id"] == "post-abc"
    assert "_rev" not in new_doc
    assert "_createdAt" not in new_doc
    assert new_doc["title"] == "T"
    assert new_doc["slug"]["current"] == "t-en"
    # Delete targets the original draft id.
    assert mutations[1]["delete"]["id"] == "drafts.post-abc"


async def test_promote_draft_to_published_raises_on_missing_draft():
    from unittest.mock import AsyncMock

    from pipeline.publisher.sanity import (
        SanityPublishError,
        SanityPublisher,
    )

    fake_client = AsyncMock()
    fake_client.query = AsyncMock(return_value=None)

    publisher = SanityPublisher(client=fake_client)
    import pytest

    with pytest.raises(SanityPublishError, match="not found"):
        await publisher.promote_draft_to_published("drafts.post-gone")


async def test_promote_draft_rejects_non_draft_id():
    from unittest.mock import AsyncMock

    from pipeline.publisher.sanity import (
        SanityPublishError,
        SanityPublisher,
    )

    publisher = SanityPublisher(client=AsyncMock())
    import pytest

    with pytest.raises(SanityPublishError, match="drafts"):
        await publisher.promote_draft_to_published("post-not-a-draft")


async def test_delete_draft_swallows_errors():
    """Reject is best-effort: a Sanity 5xx during delete must NOT raise —
    the local rejection row is the source of truth."""
    from unittest.mock import AsyncMock

    from pipeline.publisher.sanity import SanityPublisher

    fake_client = AsyncMock()
    fake_client.mutate = AsyncMock(side_effect=RuntimeError("network"))

    publisher = SanityPublisher(client=fake_client)
    # Must NOT raise.
    await publisher.delete_draft("drafts.post-ok")


async def test_query_without_params_still_works():
    """A parameter-less GROQ should also POST cleanly."""
    import respx
    from httpx import Response

    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            "https://test-proj.api.sanity.io/v2024-01-01/data/query/production"
        ).mock(return_value=Response(200, json={"result": ["item-1"]}))

        client = _make_client()
        result = await client.query('*[_type=="post"][0..2]._id')

        assert result == ["item-1"]
