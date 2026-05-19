"""Unit tests for the channel adapters.

We verify only the *contract*: output Post matches channel + length
constraints, image_url is propagated, slug is filesystem-safe. No external
APIs touched.
"""

from __future__ import annotations

import pytest
from pydantic import HttpUrl

from pipeline.adapter.blog import format_blog
from pipeline.adapter.facebook import format_facebook
from pipeline.adapter.instagram import format_instagram
from pipeline.adapter.telegram import format_telegram, will_fit_caption
from pipeline.common.models import Channel, Draft, Language


def _draft(**overrides: object) -> Draft:
    base = dict(
        topic_id="t1",
        brand_id="icon",
        language=Language.en,
        title="Visa launches instant SEPA",
        body="Two paragraphs of original commentary.\n\nSecond paragraph here.",
        key_takeaway="Worth watching.",
        image_url=HttpUrl("https://cdn.example.com/img/x.png"),
        image_alt="abstract finance scene",
    )
    base.update(overrides)
    return Draft(**base)  # type: ignore[arg-type]


def test_blog_includes_frontmatter() -> None:
    post = format_blog(_draft(), source_url="https://src.example.com/a")
    assert post.channel is Channel.blog
    assert post.content.startswith("---\n")
    assert 'title: "Visa launches instant SEPA"' in post.content
    assert "slug: visa-launches-instant-sepa" in post.content
    assert "**Worth watching.**" in post.content


def test_blog_slug_falls_back_to_topic_id_when_title_unslugifiable() -> None:
    post = format_blog(_draft(title="???"), source_url="https://x.example.com/")
    assert "slug: t1" in post.content


def test_telegram_html_safe_and_under_limit() -> None:
    post = format_telegram(_draft(), source_url="https://src.example.com/")
    assert post.channel is Channel.telegram
    assert post.content.startswith("<b>")
    assert "&" not in post.content or "&amp;" in post.content  # nothing unescaped
    assert len(post.content) <= 4096


def test_telegram_caption_fit_helper() -> None:
    assert will_fit_caption("a" * 1024)
    assert not will_fit_caption("a" * 1025)


def test_facebook_includes_link_and_optional_hashtags() -> None:
    post = format_facebook(
        _draft(),
        source_url="https://src.example.com/a",
        hashtags=["fintech", "#payments"],  # mixed `#` handling
    )
    assert post.channel is Channel.facebook
    assert "https://src.example.com/a" in post.content
    assert "#fintech #payments" in post.content


def test_instagram_requires_image() -> None:
    with pytest.raises(ValueError, match="image_url"):
        format_instagram(_draft(image_url=None))


def test_instagram_caps_caption() -> None:
    long_body = "x" * 5000
    post = format_instagram(_draft(body=long_body), hashtags=["a", "b"])
    assert post.channel is Channel.instagram
    assert len(post.content) <= 2200


def test_instagram_caps_hashtag_count() -> None:
    tags = [f"tag{i}" for i in range(50)]
    post = format_instagram(_draft(), hashtags=tags)
    # We don't enforce exact rendering, but content must not contain tag49.
    assert "#tag49" not in post.content
    assert "#tag29" in post.content  # 30 max → indices 0..29
