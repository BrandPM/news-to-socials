"""Instagram adapter: caption ≤ 2200 chars, up to 30 hashtags.

Instagram is image-first: ``image_url`` is required. The publisher will
reject a Post without it. Caption format is plain text + hashtag block
on its own line at the end (Instagram convention).
"""

from __future__ import annotations

from ..common.models import Channel, Draft, Post, PostStatus

_CAPTION_LIMIT = 2200
_MAX_HASHTAGS = 30


def format_instagram(
    draft: Draft,
    hashtags: list[str] | None = None,
) -> Post:
    if draft.image_url is None:
        raise ValueError("Instagram adapter requires draft.image_url")

    parts = [draft.title.strip(), "", draft.body.strip()]
    if draft.key_takeaway:
        parts += ["", draft.key_takeaway.strip()]

    if hashtags:
        normalised = [f"#{h.lstrip('#')}" for h in hashtags[:_MAX_HASHTAGS]]
        parts += ["", ".\n.\n."] # IG-classic separator before tag block
        parts += [" ".join(normalised)]

    content = "\n".join(parts)
    if len(content) > _CAPTION_LIMIT:
        content = content[: _CAPTION_LIMIT - 1] + "…"

    return Post(
        draft_id=draft.topic_id,
        brand_id=draft.brand_id,
        language=draft.language,
        channel=Channel.instagram,
        content=content,
        image_url=draft.image_url,
        status=PostStatus.draft,
    )
