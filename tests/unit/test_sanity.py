"""Unit tests for the Sanity publisher helpers."""

from __future__ import annotations

from pipeline.publisher.sanity import (
    estimate_read_time,
    excerpt_from_body,
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


def test_portable_text_each_block_has_unique_key() -> None:
    blocks = markdown_to_portable_text(
        "Para one.\n\nPara two.\n\nPara three."
    )
    keys = [b["_key"] for b in blocks]
    assert len(set(keys)) == len(keys)
    assert all(isinstance(k, str) and len(k) > 0 for k in keys)
