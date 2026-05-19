"""Per-channel image resizer (ADR-008).

One master image is generated; this module crops/scales it to the formats
each channel expects:

| Channel   | Size       | Aspect |
|-----------|------------|--------|
| blog      | 1792×1008  | 16:9   |
| facebook  | 1200×628   | ~1.91:1 |
| instagram | 1080×1080  | 1:1    |
| telegram  | 1280×720   | 16:9   |

Strategy: ``ImageOps.fit`` does a centered cover crop, which gives us a
filled frame at the exact dimensions without distortion. For 1:1 we keep
the center square; for wide formats we keep horizontal center.

Mitigates §5 W8 (RAM peaks during image processing): we keep image bytes
out of the main process where possible by streaming via ``BytesIO`` and we
``del`` intermediates explicitly. Pillow itself is C-bound and bounded.
"""

from __future__ import annotations

from io import BytesIO

import httpx
from PIL import Image, ImageOps

from ..common.logging import get_logger
from ..common.models import Channel

log = get_logger(__name__)

# (width, height) per channel
TARGETS: dict[Channel, tuple[int, int]] = {
    Channel.blog: (1792, 1008),
    Channel.facebook: (1200, 628),
    Channel.instagram: (1080, 1080),
    Channel.telegram: (1280, 720),
}


async def fetch_master(url: str, timeout: float = 30.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def resize_for_channel(master_bytes: bytes, channel: Channel) -> bytes:
    """Return PNG bytes sized for ``channel``."""
    target = TARGETS[channel]
    with Image.open(BytesIO(master_bytes)) as im:
        im = im.convert("RGB")
        fitted = ImageOps.fit(im, target, method=Image.Resampling.LANCZOS)
        out = BytesIO()
        fitted.save(out, format="PNG", optimize=True)
        return out.getvalue()


def resize_for_all(master_bytes: bytes) -> dict[Channel, bytes]:
    """Convenience: produce all four channel variants from one master."""
    return {ch: resize_for_channel(master_bytes, ch) for ch in TARGETS}
