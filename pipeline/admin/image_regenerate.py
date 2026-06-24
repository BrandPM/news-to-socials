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
from pipeline.generator.image_prompt import build_scene_prompt
from pipeline.generator.image_resizer import fetch_master, resize_for_channel
from pipeline.publisher.sanity import SanityClient, SanityPublisher

log = get_logger(__name__)


# Module-level so tests can monkeypatch a fake brand visual without
# importing the whole brand config.
def _brand_visual_for(brand_id: str) -> BrandVisual:
    """Build the brand's image visual, reading styles from its voice profile.

    NTS_075 L3 — styles live in ``brand.voice_profile_yaml`` (``image.
    style_prompts``), same as a real run, so Regenerate honours operator
    edits. Falls back to the built-in default set if the profile carries
    none / the DB read fails (never blocks a regenerate)."""
    from pipeline.run import _resolve_brand_image_styles  # noqa: PLC0415

    if brand_id != "icon":
        raise NotImplementedError(
            f"brand {brand_id!r} not supported yet (S5)"
        )

    voice_yaml: str | None = None
    try:
        from sqlalchemy import select  # noqa: PLC0415

        from pipeline.admin.db import session_scope  # noqa: PLC0415
        from pipeline.admin.models import Brand  # noqa: PLC0415

        with session_scope() as session:
            row = session.execute(
                select(Brand).where(Brand.slug == "icon")
            ).scalar_one_or_none()
            voice_yaml = row.voice_profile_yaml if row is not None else None
    except Exception:  # noqa: BLE001
        voice_yaml = None

    return BrandVisual(
        brand_id="icon",
        image_style_prompts=_resolve_brand_image_styles(voice_yaml),
    )


async def regenerate_cover_image(
    sanity_draft_id: str,
    custom_prompt: str | None = None,
) -> str:
    """Regenerate and re-attach a draft's cover image. Returns the new asset _id."""
    client = SanityClient()
    publisher = SanityPublisher(client=client)

    # 1. Read the existing draft (need the title + topicId). Accept either the
    #    draft id ``drafts.post-xxx`` or the published id ``post-xxx`` — Sanity
    #    stores both, and a language sibling may be in either state.
    stripped = (
        sanity_draft_id[len("drafts.") :]
        if sanity_draft_id.startswith("drafts.")
        else sanity_draft_id
    )
    draft_form = f"drafts.{stripped}"
    groq = "*[_id == $a || _id == $b][0]{title, topicId, sourceUrl}"
    doc = await client.query(groq, {"a": draft_form, "b": stripped})
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
    from sqlalchemy import select  # noqa: PLC0415

    from pipeline.admin.cost_recorder import CostContext, cost_context  # noqa: PLC0415
    from pipeline.admin.db import session_scope  # noqa: PLC0415
    from pipeline.admin.models import Brand  # noqa: PLC0415

    icon_brand_id_fk: int | None = None
    try:
        with session_scope() as session:
            row = session.execute(
                select(Brand).where(Brand.slug == "icon")
            ).scalar_one_or_none()
            icon_brand_id_fk = row.id if row is not None else None
    except Exception:  # noqa: BLE001
        icon_brand_id_fk = None

    # Cost is recorded ONCE per regeneration (one image generated), attributed
    # to the originating draft — applying it to siblings is a free patch.
    ctx = CostContext(brand_id_fk=icon_brand_id_fk, draft_id=draft_form)
    with cost_context(ctx):
        # NTS_075 L2: Regenerate uses the same smart per-topic scene as a run.
        # Exception: when the operator supplied a custom_prompt, that is the
        # whole point of the override — send it verbatim (no LLM scene). Both
        # the scene LLM call and the Replicate call record against this draft.
        scene: str | None = None
        if not custom_prompt:
            scene = await build_scene_prompt(title, brand_id_fk=icon_brand_id_fk)
        gen = ImageGenerator()
        master_url = await gen.generate(
            fake_topic, visual, operation="image_regenerate", scene=scene
        )
        master_bytes = await fetch_master(master_url)
        resized = resize_for_channel(master_bytes, Channel.blog)
        asset_id = await publisher.upload_cover_image(
            resized, filename=f"icon-{topic_id}-regen.png"
        )

    # 4. Apply the new cover to ALL language siblings of this topic — one cover
    #    per topic (NTS_069). The cover lives only in Sanity (admin.db stores no
    #    image ref), as a per-document ``coverImage`` asset reference, so before
    #    this fix Regenerate patched a single language and the siblings kept the
    #    old asset. We patch every sibling in ONE Sanity transaction → atomic,
    #    no partial state (same posture as the NTS_062 delete-sync). Falls back
    #    to the originating doc when topicId is unknown.
    cover_ref = {
        "_type": "image",
        "asset": {"_type": "reference", "_ref": asset_id},
    }
    sibling_ids: list[str] = []
    if topic_id and topic_id != "unknown":
        rows = await client.query(
            '*[_type == "post" && topicId == $tid]{_id}', {"tid": topic_id}
        )
        if isinstance(rows, list):
            sibling_ids = [
                r["_id"] for r in rows if isinstance(r, dict) and r.get("_id")
            ]
    if not sibling_ids:
        sibling_ids = [draft_form]

    await client.mutate(
        [{"patch": {"id": sid, "set": {"coverImage": cover_ref}}} for sid in sibling_ids]
    )
    log.info(
        "image.regenerated",
        asset_id=asset_id,
        topic_id=topic_id,
        applied_to=len(sibling_ids),
        ids=sibling_ids,
    )
    return asset_id
