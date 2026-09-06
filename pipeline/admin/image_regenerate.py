"""Regenerate the cover image for an existing Sanity document.

Flow:
1. Fetch the draft from Sanity (need title + topic id for a prompt).
2. Run ImageGenerator → master PNG URL on Replicate's CDN.
3. Resize for the blog channel, upload as a Sanity asset.
4. Patch ``coverImage`` on every language sibling of the topic, atomically.

Steps 2-4 live in :func:`generate_and_apply_cover`, which takes the target
document ids explicitly and neither knows nor cares whether they are drafts
or published posts. :func:`regenerate_cover_image` is the draft-facing entry
point used by ``jobs.execute_image_regenerate``; ``scripts/
backfill_cover_images.py`` (NTS_090) drives the same core over PUBLISHED ids
to repair articles that went live without a cover.
"""

from __future__ import annotations

from typing import Any

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
    from pipeline.run import _resolve_brand_image_styles

    if brand_id != "icon":
        raise NotImplementedError(
            f"brand {brand_id!r} not supported yet (S5)"
        )

    voice_yaml: str | None = None
    try:
        from sqlalchemy import select

        from pipeline.admin.db import session_scope
        from pipeline.admin.models import Brand

        with session_scope() as session:
            row = session.execute(
                select(Brand).where(Brand.slug == "icon")
            ).scalar_one_or_none()
            voice_yaml = row.voice_profile_yaml if row is not None else None
    except Exception:
        voice_yaml = None

    return BrandVisual(
        brand_id="icon",
        image_style_prompts=_resolve_brand_image_styles(voice_yaml),
    )


def _icon_brand_id_fk() -> int | None:
    """The ``brands.id`` for icon, or None when admin.db can't be read.

    Only used to attribute cost rows and resolve the brand's image prompt —
    never load-bearing, so every failure degrades to None.
    """
    try:
        from sqlalchemy import select

        from pipeline.admin.db import session_scope
        from pipeline.admin.models import Brand

        with session_scope() as session:
            row = session.execute(
                select(Brand).where(Brand.slug == "icon")
            ).scalar_one_or_none()
            return row.id if row is not None else None
    except Exception:
        return None


async def generate_and_apply_cover(
    *,
    title: str,
    topic_id: str,
    source_url: str,
    target_ids: list[str],
    client: SanityClient | None = None,
    publisher: SanityPublisher | None = None,
    cost_doc_id: str | None = None,
    custom_prompt: str | None = None,
    summary: str | None = None,
    filename_suffix: str = "regen",
    mode: str = "flux",
    cover_data: Any = None,
) -> str:
    """Generate ONE cover and attach it to every id in ``target_ids``.

    The ids are used verbatim — draft (``drafts.post-x``) or published
    (``post-x``), mixed freely — and patched in a single Sanity transaction
    so a topic never ends up half-covered. Returns the new asset ``_id``.

    Exactly one image is generated regardless of how many siblings receive
    it (NTS_069: one cover per topic); the cost row is attributed to
    ``cost_doc_id``.
    """
    if not target_ids:
        raise ValueError("generate_and_apply_cover needs at least one target id")

    client = client or SanityClient()
    publisher = publisher or SanityPublisher(client=client)

    # NTS_112 — ``data`` draws the cover from the article's own figures: SVG
    # through resvg, deterministic on the candidate id, $0 and no model call.
    # ``flux`` is the old diffusion path, kept as the operator's button for the
    # cases where a picture is genuinely wanted. An explicit custom prompt is
    # by definition a request for the artistic one.
    if mode == "data" and cover_data is not None and not custom_prompt:
        asset_id = await _apply_data_cover(
            cover_data=cover_data,
            topic_id=topic_id,
            target_ids=target_ids,
            client=client,
            publisher=publisher,
            filename_suffix=filename_suffix,
            cost_doc_id=cost_doc_id,
        )
        return asset_id

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

    from pipeline.admin.cost_recorder import CostContext, cost_context

    icon_brand_id_fk = _icon_brand_id_fk()

    ctx = CostContext(brand_id_fk=icon_brand_id_fk, draft_id=cost_doc_id)
    with cost_context(ctx):
        # NTS_075 L2: the same smart per-topic scene a real run builds.
        # Exception: an operator-supplied custom_prompt is the whole point of
        # the override — send it verbatim (no LLM scene).
        scene: str | None = None
        if not custom_prompt:
            scene = await build_scene_prompt(
                title, summary or "", brand_id_fk=icon_brand_id_fk
            )
        gen = ImageGenerator()
        master_url = await gen.generate(
            fake_topic, visual, operation="image_regenerate", scene=scene
        )
        master_bytes = await fetch_master(master_url)
        resized = resize_for_channel(master_bytes, Channel.blog)
        asset_id = await publisher.upload_cover_image(
            resized, filename=f"icon-{topic_id}-{filename_suffix}.png"
        )

    # One cover per topic (NTS_069). The cover lives only in Sanity (admin.db
    # stores no image ref) as a per-document ``coverImage`` asset reference,
    # so before this fix Regenerate patched a single language and the siblings
    # kept the old asset. Every sibling is patched in ONE Sanity transaction →
    # atomic, no partial state (same posture as the NTS_062 delete-sync).
    cover_ref = {
        "_type": "image",
        "asset": {"_type": "reference", "_ref": asset_id},
    }
    await client.mutate(
        [
            {"patch": {"id": sid, "set": {"coverImage": cover_ref}}}
            for sid in target_ids
        ]
    )
    log.info(
        "image.cover_applied",
        asset_id=asset_id,
        topic_id=topic_id,
        applied_to=len(target_ids),
        ids=target_ids,
    )
    return asset_id


async def _apply_data_cover(
    *,
    cover_data: Any,
    topic_id: str,
    target_ids: list[str],
    client: SanityClient,
    publisher: SanityPublisher,
    filename_suffix: str,
    cost_doc_id: str | None,
) -> str:
    """Draw the cover from data and patch every sibling (NTS_112).

    A cost row is written with ``cost_usd=0`` rather than skipped: NTS_112's
    DoD asks for the operation to be recorded, and an operation missing from
    the ledger is indistinguishable from one that never ran when somebody asks
    later why a cover looks the way it does.
    """
    from pipeline.admin.cost_recorder import CostContext, cost_context, record_cost
    from pipeline.generator.cover_svg import build_svg, render_png

    svg = build_svg(cover_data)
    png = render_png(svg)
    with cost_context(
        CostContext(brand_id_fk=_icon_brand_id_fk(), draft_id=cost_doc_id)
    ):
        record_cost(
            provider="local",
            operation="cover_data",
            model="cover_svg/1",
            cost_usd=0.0,
        )
        asset_id = await publisher.upload_cover_image(
            png, filename=f"icon-{topic_id}-{filename_suffix}.png"
        )
    cover_ref = {
        "_type": "image",
        "asset": {"_type": "reference", "_ref": asset_id},
    }
    await client.mutate(
        [
            {"patch": {"id": sid, "set": {"coverImage": cover_ref}}}
            for sid in target_ids
        ]
    )
    log.info(
        "image.data_cover_applied",
        asset_id=asset_id,
        topic_id=topic_id,
        applied_to=len(target_ids),
        service=getattr(cover_data, "service", None),
        stamp=getattr(cover_data, "stamp", ""),
    )
    return asset_id


async def regenerate_cover_image(
    sanity_draft_id: str,
    custom_prompt: str | None = None,
) -> str:
    """Regenerate and re-attach a draft's cover image. Returns the new asset _id.

    Works on a draft that has NO cover yet — the read below asks for title /
    topicId / sourceUrl and nothing about ``coverImage``, so "generate the
    first cover" and "replace the cover" are one path (NTS_091 Task B: the
    publish-guard banner's one-click fix calls exactly this).
    """
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

    # 2. Resolve every language sibling of the topic BEFORE paying for an
    #    image. Falls back to the originating doc when topicId is unknown.
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

    # 3. Generate one image and apply it to all of them. Cost is attributed to
    #    the originating draft — applying it to siblings is a free patch.
    return await generate_and_apply_cover(
        title=title,
        topic_id=topic_id,
        source_url=source_url,
        target_ids=sibling_ids,
        client=client,
        publisher=publisher,
        cost_doc_id=draft_form,
        custom_prompt=custom_prompt,
    )
