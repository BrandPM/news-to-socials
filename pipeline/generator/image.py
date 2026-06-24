"""Master image generation via Replicate Flux Pro (ADR-012).

Strategy:
* Pick a random ``image_style_prompt`` from the brand's visual config (W6
  mitigation — at least 3 styles per brand).
* Build the prompt as ``{topic_title}, {style_prompt}``.
* Send a strong negative prompt to avoid embedded text / watermarks /
  signatures, which would conflict with our captions/CTAs.
* Returns the URL of the generated PNG hosted on Replicate's CDN. The
  caller is responsible for downloading and re-uploading into Directus
  ``/files`` so we don't depend on the temporary CDN past the post lifetime.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from replicate.client import Client as ReplicateClient

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Topic
from ..common.retry import with_retry

log = get_logger(__name__)

_NEGATIVE = (
    "text, words, letters, captions, watermark, logo, signature, blurry, "
    "low quality, deformed, extra fingers, jpeg artifacts"
)

# NTS_075 L1 — the built-in fallback style set for Icon, used when the brand's
# voice profile carries no ``image.style_prompts`` (L3). Deliberately diverse:
# not just luxury interiors (the W6 failure mode that made every cover a blank
# marble room) but abstract finance, urban architecture, macro texture,
# editorial illustration, and documentary photography across palettes, angles,
# and times of day. Each entry is a self-contained visual directive that wraps
# the per-topic scene as ``{scene}, {style}``.
DEFAULT_ICON_IMAGE_STYLES: list[str] = [
    "abstract financial geometry, intersecting gold and graphite lines on a "
    "deep navy field, precise and minimal, soft studio gradient",
    "flowing data-network visualization, luminous nodes and connective threads, "
    "dark teal background, cinematic depth, no text",
    "modern business district, glass skyscrapers shot from below at golden hour, "
    "warm reflective façades, crisp architectural photography",
    "downtown financial quarter at blue hour, illuminated office towers, cool "
    "blue and amber palette, long-exposure calm, wide cityscape",
    "macro detail of brushed metal and stone, raking light revealing texture, "
    "neutral monochrome palette, shallow depth of field",
    "macro still life of layered paper and ledger surfaces, warm desk light, "
    "muted sepia tones, tactile and editorial",
    "minimalist concept composition, a single sculptural object centred in vast "
    "negative space, soft daylight, restrained and quiet",
    "editorial flat-vector illustration, geometric financial motifs, muted earth "
    "tones with one accent colour, clean magazine aesthetic",
    "documentary photograph of a calm modern boardroom, natural window light, "
    "neutral palette, candid unposed framing",
    "aerial documentary view of a coastal financial capital, soft haze, balanced "
    "natural colour, sense of scale and distance",
    "abstract market-flow artwork, sweeping translucent ribbons suggesting "
    "capital movement, charcoal background with cool highlights",
    "architectural interior of a contemporary atrium, clean lines and columns, "
    "diffused daylight, airy and spacious, understated palette",
    "cinematic landscape metaphor, distant mountain horizon at dawn, vast open "
    "sky, muted gradient, contemplative and premium",
    "macro of polished currency and metal coins arranged abstractly, dramatic "
    "side lighting, warm metallic tones, no readable text",
]


@dataclass(frozen=True)
class BrandVisual:
    """The slice of brand.visual_config we need here."""

    brand_id: str
    image_style_prompts: list[str]


class ImageGenerator:
    def __init__(self, model: str = "black-forest-labs/flux-1.1-pro") -> None:
        self.model = model
        # pydantic-settings loads .env into the Settings object but not os.environ,
        # so we must construct an explicit Replicate client with the token.
        settings = get_settings()
        if not settings.replicate_api_token:
            log.warning("image_generator.no_token", note="dry-run mode will be forced")
            self._client = None
        else:
            self._client = ReplicateClient(api_token=settings.replicate_api_token)

    @with_retry()
    async def generate(
        self,
        topic: Topic,
        brand_visual: BrandVisual,
        *,
        operation: str = "image_master",
        scene: str | None = None,
    ) -> str:
        if not brand_visual.image_style_prompts:
            raise ValueError(f"brand {brand_visual.brand_id} has no image_style_prompts")

        style = random.choice(brand_visual.image_style_prompts)  # noqa: S311
        # NTS_075 L2: prefer the LLM-derived visual scene (relevant to THIS
        # article) over the bare headline, which was a weak signal that let
        # Flux collapse into its default luxury interior. Fall back to the
        # title when no scene was built (LLM off/failed) — image generation
        # must never be blocked by the scene step.
        base = (scene or topic.raw.title or "").strip()
        prompt = f"{base}, {style}"

        log.info(
            "image.generate",
            brand=brand_visual.brand_id,
            prompt_len=len(prompt),
            has_scene=bool(scene),
        )
        if self._client is None:
            raise RuntimeError("REPLICATE_API_TOKEN missing; cannot generate image")
        start = time.monotonic()
        output = await self._client.async_run(
            self.model,
            input={
                "prompt": prompt,
                "negative_prompt": _NEGATIVE,
                "aspect_ratio": "16:9",
                "output_format": "png",
                "output_quality": 90,
                "safety_tolerance": 2,
            },
        )
        duration = round(time.monotonic() - start, 3)
        # NTS_025 C1: record Replicate cost (one row per generated image).
        from pipeline.admin.cost_recorder import record_cost  # noqa: PLC0415
        from pipeline.common.pricing import replicate_image_cost  # noqa: PLC0415

        record_cost(
            provider="replicate",
            operation=operation,
            model=self.model,
            duration_seconds=duration,
            cost_usd=replicate_image_cost(self.model),
        )
        # Flux returns a single URL (string) or a list of strings depending
        # on the model version. Normalise.
        if isinstance(output, list):
            url = str(output[0])
        else:
            url = str(output)
        return url
