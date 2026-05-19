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
from dataclasses import dataclass

import replicate

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Topic
from ..common.retry import with_retry

log = get_logger(__name__)

_NEGATIVE = (
    "text, watermark, logo, signature, blurry, low quality, "
    "deformed, extra fingers, jpeg artifacts"
)


@dataclass(frozen=True)
class BrandVisual:
    """The slice of brand.visual_config we need here."""

    brand_id: str
    image_style_prompts: list[str]


class ImageGenerator:
    def __init__(self, model: str = "black-forest-labs/flux-1.1-pro") -> None:
        self.model = model
        # Replicate's SDK reads REPLICATE_API_TOKEN from env at call time;
        # we surface it explicitly via settings to fail fast on misconfig.
        settings = get_settings()
        if not settings.replicate_api_token:
            log.warning("image_generator.no_token", note="dry-run mode will be forced")

    @with_retry()
    async def generate(self, topic: Topic, brand_visual: BrandVisual) -> str:
        if not brand_visual.image_style_prompts:
            raise ValueError(f"brand {brand_visual.brand_id} has no image_style_prompts")

        style = random.choice(brand_visual.image_style_prompts)  # noqa: S311
        prompt = f"{topic.raw.title}, {style}"

        log.info("image.generate", brand=brand_visual.brand_id, prompt_len=len(prompt))
        # `replicate.async_run` is a thin async wrapper.
        output = await replicate.async_run(
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
        # Flux returns a single URL (string) or a list of strings depending
        # on the model version. Normalise.
        if isinstance(output, list):
            url = str(output[0])
        else:
            url = str(output)
        return url
