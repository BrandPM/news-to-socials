"""NTS_133 P5 Ф4 — a run label never reaches a published title or slug."""

from __future__ import annotations

import pytest

from pipeline.admin.draft_validation import has_run_tag, validate_draft_complete

_OK = {
    "title": "OFSI doubles the maximum penalty",
    "slug": "ofsi-doubles-the-maximum-penalty",
    "coverImageRef": "image-abc",
    "bodyBlockCount": 12,
    "bodyH2Count": 3,
    "displayDate": "2026-10-14",
}


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("[nts131] OFSI doubles the maximum penalty", "ofsi"),
        ("[e2e-2026-09] OFSI", "ofsi"),
        ("OFSI doubles", "nts131-ofsi-doubles"),
        ("OFSI doubles", "e2e-2026-09-ofsi"),
    ],
)
def test_a_tagged_title_or_slug_is_refused(title, slug):
    assert has_run_tag(title, slug)
    assert "run_tag" in validate_draft_complete({**_OK, "title": title, "slug": slug})


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("EU [DAC8] reporting starts", "eu-dac8-reporting-starts"),
        ("Testing times for private banks", "testing-times-for-private-banks"),
        ("NTS rules in Cyprus", "nts-rules-in-cyprus"),
    ],
)
def test_ordinary_brackets_and_words_are_not_run_tags(title, slug):
    assert not has_run_tag(title, slug)
    assert validate_draft_complete({**_OK, "title": title, "slug": slug}) == []


async def test_the_publisher_writes_run_tag_as_its_own_field():
    from pipeline.common.models import Language
    from pipeline.publisher.sanity import SanityPostInput, SanityPublisher

    class _Client:
        async def query(self, *_a, **_k):
            return None

    publisher = SanityPublisher(client=_Client())  # type: ignore[arg-type]
    doc = await publisher._build_draft_doc(
        SanityPostInput(
            title="OFSI doubles the maximum penalty",
            body_markdown="## What changed\n\nText.",
            language=Language.en,
            category="special",
            source_url="https://gov.uk/x",
            topic_id="t1",
            run_tag="nts133",
        )
    )
    assert doc["runTag"] == "nts133"
    assert "nts133" not in doc["title"]
    assert not doc["slug"]["current"].startswith("nts133")
