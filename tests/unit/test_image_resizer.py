"""Unit tests for generator/image_resizer.py."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from pipeline.common.models import Channel
from pipeline.generator.image_resizer import TARGETS, resize_for_all, resize_for_channel


def _make_master(width: int = 1920, height: int = 1080) -> bytes:
    im = Image.new("RGB", (width, height), color=(120, 130, 200))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_resize_blog_exact_dimensions() -> None:
    out = resize_for_channel(_make_master(), Channel.blog)
    with Image.open(BytesIO(out)) as im:
        assert im.size == TARGETS[Channel.blog]


def test_resize_all_channels_produce_targets() -> None:
    results = resize_for_all(_make_master())
    assert set(results.keys()) == set(TARGETS.keys())
    for channel, png in results.items():
        with Image.open(BytesIO(png)) as im:
            assert im.size == TARGETS[channel]


def test_resize_from_tall_master() -> None:
    """A portrait master should still produce correct landscape crops."""
    out = resize_for_channel(_make_master(width=600, height=1200), Channel.facebook)
    with Image.open(BytesIO(out)) as im:
        assert im.size == TARGETS[Channel.facebook]
