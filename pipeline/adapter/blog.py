"""Blog adapter: Markdown body + YAML frontmatter."""

from __future__ import annotations

from slugify import slugify

from ..common.models import Channel, Draft, Post, PostStatus


def format_blog(draft: Draft, source_url: str) -> Post:
    """Render a Draft as a blog Post with YAML frontmatter + markdown body."""
    slug = slugify(draft.title, max_length=80) or draft.topic_id
    front = "\n".join(
        [
            "---",
            f'title: "{_yaml_escape(draft.title)}"',
            f"slug: {slug}",
            f"language: {draft.language.value}",
            f"brand: {draft.brand_id}",
            f"source_url: {source_url}",
            f'image_alt: "{_yaml_escape(draft.image_alt or draft.title)}"',
            "---",
            "",
        ]
    )

    body_md = draft.body.strip()
    if draft.key_takeaway:
        body_md += f"\n\n**{draft.key_takeaway.strip()}**\n"

    return Post(
        draft_id=draft.topic_id,
        brand_id=draft.brand_id,
        language=draft.language,
        channel=Channel.blog,
        content=front + body_md,
        image_url=draft.image_url,
        status=PostStatus.draft,
    )


def _yaml_escape(s: str) -> str:
    return s.replace('"', '\\"')
