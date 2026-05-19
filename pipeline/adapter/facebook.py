"""Facebook adapter: plain text + optional link preview.

Facebook auto-generates a link preview from the first URL in the post body,
so we put the source URL on its own line at the end and skip our own
hashtag rendering when the brand config disables it.

There's no hard text limit for FB page posts in practice, but anything over
~500 chars truncates on mobile. We keep the body but recommend the LLM stay
under 600 chars at the prompt level.
"""

from __future__ import annotations

from ..common.models import Channel, Draft, Post, PostStatus


def format_facebook(
    draft: Draft,
    source_url: str,
    hashtags: list[str] | None = None,
) -> Post:
    parts = [draft.title.strip(), "", draft.body.strip()]
    if draft.key_takeaway:
        parts += ["", draft.key_takeaway.strip()]
    parts += ["", source_url]
    if hashtags:
        parts += ["", " ".join(f"#{h.lstrip('#')}" for h in hashtags)]

    return Post(
        draft_id=draft.topic_id,
        brand_id=draft.brand_id,
        language=draft.language,
        channel=Channel.facebook,
        content="\n".join(parts),
        image_url=draft.image_url,
        status=PostStatus.draft,
    )
