"""Regenerate the cover image for an existing Sanity draft.

Flow:
1. Fetch the draft from Sanity (need title + topic id for a prompt).
2. Run ImageGenerator → master PNG URL on Replicate's CDN.
3. Resize for the blog channel, upload as a Sanity asset.
4. Patch the draft's ``coverImage`` reference to the new asset.

Returns the new asset _id on success. Used by ``jobs.execute_image_regenerate``.
"""

from __future__ import annotations

from pipeline.common.logging import get_logger
from pipeline.common.models import Channel, RawItem, Topic
from pipeline.generator.image import BrandVisual, ImageGenerator
from pipeline.generator.image_resizer import fetch_master, resize_for_channel
from pipeline.publisher.sanity import SanityClient, SanityPublisher

log = get_logger(__name__)


# Module-level so tests can monkeypatch a fake brand visual without
# importing the whole brand config.
def _brand_visual_for(brand_id: str) -> BrandVisual:
    from pipeline.run import icon_brand_config  # noqa: PLC0415

    if brand_id != "icon":
        raise NotImplementedError(
            f"brand {brand_id!r} not supported yet (S5)"
        )
    return icon_brand_config().visual


async def regenerate_cover_image(
    sanity_draft_id: str,
    custom_prompt: str | None = None,
) -> str:
    """Regenerate and re-attach a draft's cover image. Returns the new asset _id."""
    client = SanityClient()
    publisher = SanityPublisher(client=client)

    # 1. Read the existing draft (need the title to build a prompt). We
    #    accept either the draft id ``drafts.post-xxx`` or the published id
    #    ``post-xxx`` — Sanity stores both. Normalise to draft id.
    if not sanity_draft_id.startswith("drafts."):
        sanity_draft_id = f"drafts.{sanity_draft_id}"

    groq = '*[_id == $id][0]{title, topicId, sourceUrl}'
    doc = await client.query(groq, {"id": sanity_draft_id})
    if not doc:
        raise LookupError(f"draft {sanity_draft_id!r} not found in Sanity")

    title = doc.get("title") or "Untitled"
    topic_id = doc.get("topicId") or "unknown"
    source_url = doc.get("sourceUrl") or "https://example.com/"

    # 2. Build a synthetic Topic so ImageGenerator has its expected input.
    fake_topic = Topic(
        id=topic_id,
        brand_id="icon",
        raw=RawItem(
            source_id="regen",
            source_name="regen",
            url=source_url,
            title=title,
        ),
        relevance_score=10.0,
    )
    visual = _brand_visual_for("icon")
    if custom_prompt:
        visual = BrandVisual(
            brand_id=visual.brand_id, image_style_prompts=[custom_prompt]
        )

    # 3. Generate + resize + upload — wrap in a cost context so the
    #    Replicate call inside ImageGenerator records against this draft.
    #    Brand resolution is naïve here (icon-only); Step 4 broadens this.
    from pipeline.admin.cost_recorder import CostContext, cost_context  # noqa: PLC0415
    from pipeline.admin.db import session_scope  # noqa: PLC0415
    from pipeline.admin.models import Brand  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    icon_brand_id_fk: int | None = None
    try:
        with session_scope() as session:
            row = session.execute(
                select(Brand).where(Brand.slug == "icon")
            ).scalar_one_or_none()
            icon_brand_id_fk = row.id if row is not None else None
    except Exception:  # noqa: BLE001
        icon_brand_id_fk = None

    ctx = CostContext(brand_id_fk=icon_brand_id_fk, draft_id=sanity_draft_id)
    with cost_context(ctx):
        gen = ImageGenerator()
        master_url = await gen.generate(
            fake_topic, visual, operation="image_regenerate"
        )
        master_bytes = await fetch_master(master_url)
        resized = resize_for_channel(master_bytes, Channel.blog)
        asset_id = await publisher.upload_cover_image(
            resized, filename=f"icon-{topic_id}-regen.png"
        )

    # 4. Patch the draft's coverImage reference.
    await client.patch(
        sanity_draft_id,
        {
            "coverImage": {
                "_type": "image",
                "asset": {"_type": "reference", "_ref": asset_id},
            }
        },
    )
    log.info(
        "image.regenerated",
        draft_id=sanity_draft_id,
        asset_id=asset_id,
        topic_id=topic_id,
    )
    return asset_id
