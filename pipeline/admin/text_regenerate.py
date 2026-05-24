"""Re-polish the body of an existing Sanity draft (S5 Step 7).

Smaller scope than the original pipeline run: we read the current
title/body from Sanity, run only the polish stage of CommentWriter
against the active brand's voice profile + banned phrases, and patch
the new body back onto the same draft id.

Cost is recorded against the draft via ``cost_recorder`` so the
regenerated polish shows up on the draft's cost breakdown.
"""

from __future__ import annotations

from pipeline.common.logging import get_logger

log = get_logger(__name__)


async def regenerate_draft_text(
    sanity_draft_id: str,
    brand_id_fk: int,
) -> None:
    """Read draft → re-polish body → patch.

    Lazy imports for the same reason as ``image_regenerate``: tests of
    the dispatcher monkeypatch this whole function, so we don't want
    the OpenAI/Sanity stacks loaded just to verify route plumbing.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from pipeline.admin.cost_recorder import CostContext, cost_context  # noqa: PLC0415
    from pipeline.admin.db import session_scope  # noqa: PLC0415
    from pipeline.admin.encryption import get_encryption  # noqa: PLC0415
    from pipeline.admin.models import Brand, PipelineConfig  # noqa: PLC0415
    from pipeline.generator.anti_ai_check import score_ai_tells  # noqa: PLC0415
    from pipeline.generator.comment_writer import (  # noqa: PLC0415
        _DraftJSON,
        CommentWriter,
        parse_voice_guardrails,
    )
    from pipeline.publisher.sanity import SanityClient  # noqa: PLC0415

    # --- 1. Load brand creds + voice profile + banned phrases ------------
    with session_scope() as session:
        brand = session.get(Brand, brand_id_fk)
        if brand is None:
            raise LookupError(f"brand {brand_id_fk} not found")
        project_id = brand.sanity_project_id
        dataset = brand.sanity_dataset or "production"
        api_version = brand.sanity_api_version or "2024-01-01"
        token_enc = brand.sanity_api_token_enc
        voice_profile_yaml = brand.voice_profile_yaml or ""

        cfg = session.execute(
            select(PipelineConfig).where(
                PipelineConfig.brand_id_fk == brand_id_fk
            )
        ).scalar_one_or_none()
        # Banned phrases live in PipelineConfig as a JSON string, but the
        # polish prompt parses them out of the voice profile YAML directly.
        # Pull both — use whichever has content.

    if not project_id or not token_enc:
        raise LookupError(
            f"brand {brand_id_fk} has no Sanity credentials configured"
        )

    token = get_encryption().decrypt(token_enc)
    client = SanityClient(
        project_id=project_id,
        dataset=dataset,
        api_version=api_version,
        token=token,
    )
    del token

    # --- 2. Fetch existing draft -----------------------------------------
    groq = '*[_id == $id][0]{title, body, keyTakeaway}'
    doc = await client.query(groq, {"id": sanity_draft_id})
    if not doc:
        raise LookupError(f"draft {sanity_draft_id!r} not found in Sanity")

    # Reduce Portable Text → markdown-ish for the polish input.
    body = doc.get("body")
    body_md_parts: list[str] = []
    if isinstance(body, list):
        for block in body:
            if not isinstance(block, dict) or block.get("_type") != "block":
                continue
            style = block.get("style", "normal")
            text_parts = [
                c.get("text", "")
                for c in block.get("children", [])
                if isinstance(c, dict)
            ]
            joined = "".join(text_parts)
            if style == "h2":
                body_md_parts.append(f"## {joined}")
            elif style == "h3":
                body_md_parts.append(f"### {joined}")
            else:
                body_md_parts.append(joined)
    elif isinstance(body, str):
        body_md_parts.append(body)
    body_md = "\n\n".join(body_md_parts)

    title = doc.get("title") or "Untitled"
    key_takeaway = doc.get("keyTakeaway") or ""

    # --- 3. Run polish only ----------------------------------------------
    score, tells = score_ai_tells(body_md)
    log.info(
        "text_regenerate.ai_score",
        draft_id=sanity_draft_id,
        score=score,
        tells=tells,
    )
    banned_phrases, good_examples = parse_voice_guardrails(voice_profile_yaml)

    writer = CommentWriter()
    pre_draft = _DraftJSON(title=title, body=body_md, key_takeaway=key_takeaway)
    ctx = CostContext(brand_id_fk=brand_id_fk, draft_id=sanity_draft_id)
    with cost_context(ctx):
        polished = await writer._polish(  # noqa: SLF001
            pre_draft, tells, banned_phrases, good_examples
        )

    # --- 4. Convert polished body back to Portable Text + patch -----------
    new_blocks = _markdown_to_portable_text(polished.body)
    await client.patch(
        sanity_draft_id,
        {
            "title": polished.title,
            "body": new_blocks,
            "keyTakeaway": polished.key_takeaway,
        },
    )
    log.info(
        "text_regenerate.done",
        draft_id=sanity_draft_id,
        new_length=len(polished.body),
    )


def _markdown_to_portable_text(md: str) -> list[dict]:
    """Naïve markdown → Portable Text. Handles paragraphs + ``##`` / ``###``.

    Faithful to the structure the writer emits (paragraphs separated by
    blank lines, H2/H3 prefixes). More exotic markdown (lists, links)
    falls through as plain paragraph text — acceptable because the
    polish stage almost never introduces those.
    """
    blocks: list[dict] = []
    for chunk in md.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        style = "normal"
        text = chunk
        if chunk.startswith("## "):
            style = "h2"
            text = chunk[3:].strip()
        elif chunk.startswith("### "):
            style = "h3"
            text = chunk[4:].strip()
        blocks.append(
            {
                "_type": "block",
                "style": style,
                "children": [{"_type": "span", "text": text, "marks": []}],
                "markDefs": [],
            }
        )
    return blocks
