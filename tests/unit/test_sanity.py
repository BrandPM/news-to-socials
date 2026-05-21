"""Unit tests for the Sanity publisher helpers."""

from __future__ import annotations

from pipeline.publisher.sanity import (
    estimate_read_time,
    excerpt_from_body,
    extract_toc_from_body,
    markdown_to_portable_text,
    slugify,
)


# --- slug --------------------------------------------------------------


def test_slug_basic() -> None:
    assert slugify("Visa launches instant SEPA pilot in 2026") == "visa-launches-instant-sepa-pilot-in-2026"


def test_slug_strips_punctuation() -> None:
    assert slugify("M&A deals: a 2026 outlook!") == "ma-deals-a-2026-outlook"


def test_slug_truncates_long_titles() -> None:
    long = "very " * 50 + "long title"
    s = slugify(long, max_length=20)
    assert len(s) <= 20
    assert s.startswith("very")


def test_slug_handles_empty() -> None:
    assert slugify("") == "untitled"
    assert slugify("???") == "untitled"


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
