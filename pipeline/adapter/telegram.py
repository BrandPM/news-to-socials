"""Telegram adapter: HTML formatting + caption-vs-message routing.

Telegram limits:
* photo + caption ≤ 1024 chars
* plain message ≤ 4096 chars

If the content fits in a photo caption, we go with a single send_photo.
Otherwise we send the photo first (with a short caption: title + link to
full text) and the full body as a follow-up message. The publisher splits
this into two API calls; the adapter just returns the *content*, and the
publisher decides on send strategy.

Supported HTML tags (Telegram subset):
``<b> <i> <u> <s> <a href> <code> <pre> <blockquote>``
"""

from __future__ import annotations

from html import escape

from ..common.models import Channel, Draft, Post, PostStatus

_CAPTION_LIMIT = 1024
_MESSAGE_LIMIT = 4096


def format_telegram(draft: Draft, source_url: str) -> Post:
    """Build a Telegram-flavoured Post. Content is always HTML."""
    title_html = f"<b>{escape(draft.title)}</b>"
    body_html = _to_html(draft.body)

    takeaway_html = (
        f"\n\n<i>{escape(draft.key_takeaway)}</i>" if draft.key_takeaway else ""
    )
    source_html = f'\n\n<a href="{escape(source_url)}">Источник</a>'

    full = f"{title_html}\n\n{body_html}{takeaway_html}{source_html}"

    # Truncate to message limit just in case Sonnet returned a too-long body.
    if len(full) > _MESSAGE_LIMIT:
        full = full[: _MESSAGE_LIMIT - 1] + "…"

    return Post(
        draft_id=draft.topic_id,
        brand_id=draft.brand_id,
        language=draft.language,
        channel=Channel.telegram,
        content=full,
        image_url=draft.image_url,
        status=PostStatus.draft,
    )


def will_fit_caption(content: str) -> bool:
    """True if ``content`` fits in a single send_photo caption."""
    return len(content) <= _CAPTION_LIMIT


def _to_html(body: str) -> str:
    """Convert plain prose paragraphs into Telegram-safe HTML.

    The LLM returns plain text with double-newline paragraph breaks. We
    escape HTML special chars, then re-join paragraphs with double <br/>
    equivalent (Telegram renders \n\n as a paragraph break natively).
    """
    paragraphs = [escape(p.strip()) for p in body.split("\n\n") if p.strip()]
    return "\n\n".join(paragraphs)
