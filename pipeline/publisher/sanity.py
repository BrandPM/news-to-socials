"""Sanity publisher (ADR-018).

This is the writer-only client for the existing Sanity CMS that already
powers icon.finance/insights and /studio/* in the Lovable project.

We use Sanity's HTTP API directly (not the official @sanity/client TypeScript
SDK, which is JS-only). Endpoints we hit:

* ``POST /v2024-01-01/data/mutate/{dataset}`` — create/patch documents
* ``POST /v2024-01-01/assets/images/{dataset}`` — upload images
* ``GET  /v2024-01-01/data/query/{dataset}?query=...`` — GROQ queries

Auth: Bearer token with Editor permissions
(see ADR-018 for token provisioning).

Document workflow:
* New posts are created as **drafts** (``_id`` prefixed with ``drafts.``).
  Andriy reviews in /studio and presses Publish, which Sanity handles
  natively by promoting ``drafts.{id}`` → ``{id}``.
* Optional ``initialValue`` for ``status`` not used — we rely on Sanity's
  built-in draft vs published distinction.
* For multilingual posts we create N drafts (one per language) and link
  them via @sanity/document-internationalization metadata.

References:
* Sanity HTTP API: https://www.sanity.io/docs/http-api
* Mutations: https://www.sanity.io/docs/http-mutations
* document-internationalization plugin behaviour: each translation is a
  separate document; a metadata document binds them by translationId.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Channel, Draft, Language, Post, PostStatus
from ..common.retry import with_retry

log = get_logger(__name__)


# --- Sanity REST helpers ---------------------------------------------------


@dataclass(frozen=True)
class SanityCategoryMapping:
    """Brand-specific category mapping for the topic_picker prompt.

    For Icon, the live values are: wealth / family / structuring / ma / special.
    For other brands (Stage 5+), supply a different list via the brand record
    in the Sanity ``brand`` collection.
    """

    values: tuple[str, ...]
    titles: dict[str, str]

    @classmethod
    def icon_default(cls) -> "SanityCategoryMapping":
        return cls(
            values=("wealth", "family", "structuring", "ma", "special"),
            titles={
                "wealth": "Wealth Management",
                "family": "Family Office",
                "structuring": "Structuring & Tax",
                "ma": "M&A & Corporate",
                "special": "Special Solutions",
            },
        )


class SanityClient:
    """Thin HTTP client for the Sanity REST API."""

    def __init__(
        self,
        project_id: str | None = None,
        dataset: str | None = None,
        api_version: str | None = None,
        token: str | None = None,
    ) -> None:
        s = get_settings()
        self.project_id = project_id or s.sanity_project_id
        self.dataset = dataset or s.sanity_dataset
        self.api_version = api_version or s.sanity_api_version
        self.token = token or s.sanity_api_token

        if not self.project_id:
            raise RuntimeError("SANITY_PROJECT_ID is not set")
        if not self.token:
            raise RuntimeError("SANITY_API_TOKEN is not set (need Editor perms)")

        self.base = f"https://{self.project_id}.api.sanity.io/v{self.api_version}"

    def _headers(self, *, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
        }

    # --- Queries (GROQ) ---------------------------------------------------

    @with_retry()
    async def query(self, groq: str, params: dict[str, Any] | None = None) -> Any:
        # Sanity supports two transports for GROQ:
        #  1. GET ?query=...&$key=value — URL gets long fast, special-char
        #     escaping in zsh/curl is fragile, $-prefixed param names
        #     conflict with shell variable expansion in some contexts.
        #  2. POST body {"query": ..., "params": {...}} — clean and
        #     recommended for anything beyond trivial scripts.
        # We use POST. Reference: https://www.sanity.io/docs/http-query
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base}/data/query/{self.dataset}",
                headers=self._headers(),
                json={"query": groq, "params": params or {}},
            )
            resp.raise_for_status()
            return resp.json().get("result")

    # --- Mutations --------------------------------------------------------

    @with_retry()
    async def mutate(self, mutations: list[dict[str, Any]]) -> dict[str, Any]:
        """Run a transaction of one-or-more mutations."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base}/data/mutate/{self.dataset}",
                headers=self._headers(),
                json={"mutations": mutations},
            )
            resp.raise_for_status()
            return resp.json()

    async def create_draft(self, doc: dict[str, Any]) -> str:
        """Create a document in draft form (Andriy approves through Studio).

        Sanity treats documents with ``_id`` prefixed ``drafts.`` as drafts.
        We let Sanity generate the published ID by giving the draft an
        explicit one; on publish, ``drafts.{id}`` is promoted to ``{id}``.
        """
        if "_type" not in doc:
            raise ValueError("Document must have _type")
        # Generate stable published-state id; draft prefix added below.
        if "_id" not in doc:
            doc["_id"] = f"post-{uuid.uuid4().hex[:12]}"
        draft_id = doc["_id"] if doc["_id"].startswith("drafts.") else f"drafts.{doc['_id']}"
        doc["_id"] = draft_id

        result = await self.mutate([{"create": doc}])
        log.info("sanity.draft_created", id=draft_id)
        return draft_id

    async def patch(self, doc_id: str, set_fields: dict[str, Any]) -> dict[str, Any]:
        """Update fields on an existing document (draft or published)."""
        result = await self.mutate(
            [{"patch": {"id": doc_id, "set": set_fields}}]
        )
        return result

    # --- Asset upload -----------------------------------------------------

    @with_retry()
    async def upload_image(self, image_bytes: bytes, filename: str) -> dict[str, Any]:
        """Upload an image and return the asset document.

        The returned dict contains ``_id``, ``url``, ``metadata`` etc.
        Reference this in a cover image field as:
            {"_type": "image", "asset": {"_type": "reference", "_ref": asset_id}}
        """
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base}/assets/images/{self.dataset}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "image/png",
                },
                content=image_bytes,
                params={"filename": filename},
            )
            resp.raise_for_status()
            data = resp.json()
            asset_id = data.get("document", {}).get("_id")
            log.info("sanity.image_uploaded", asset_id=asset_id, filename=filename)
            return data["document"]


# --- Portable Text conversion ---------------------------------------------


_HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")


def markdown_to_portable_text(markdown: str) -> list[dict[str, Any]]:
    """Convert plain markdown body produced by comment_writer into Sanity's
    Portable Text block format.

    The LLM output is paragraph-based prose with occasional headings. We:

    * Treat ``## ...`` and ``### ...`` lines as H2/H3 blocks, whether or not
      they are separated from surrounding prose by a blank line (gpt-4o
      sometimes emits ``## Heading\\nBody`` with a single newline).
    * Otherwise emit normal paragraph blocks. Consecutive non-heading lines
      separated by a single newline are joined with a space; a blank line
      flushes the current paragraph.
    * Strip surrounding whitespace.
    * Generate stable keys per block (Sanity requires ``_key``).

    Bold/italic markdown isn't handled here — the LLM is instructed to
    write clean prose, and inline marks add a lot of complexity for
    minimal gain. If needed later, see Sanity's ``@sanity/block-tools``
    in JS to be ported.
    """
    blocks: list[dict[str, Any]] = []
    paragraph_buf: list[str] = []
    idx = 0

    def flush_paragraph() -> None:
        nonlocal idx
        if not paragraph_buf:
            return
        text = " ".join(paragraph_buf).strip()
        paragraph_buf.clear()
        if not text:
            return
        blocks.append(_make_block(idx, text, style="normal"))
        idx += 1

    for raw_line in markdown.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush_paragraph()
            hashes, rest = m.groups()
            style = {2: "h2", 3: "h3"}.get(len(hashes), "h2")
            blocks.append(_make_block(idx, rest.strip(), style=style))
            idx += 1
            continue
        paragraph_buf.append(line)
    flush_paragraph()

    return blocks


def _make_block(idx: int, text: str, *, style: str) -> dict[str, Any]:
    return {
        "_type": "block",
        "_key": _block_key(idx, text),
        "style": style,
        "markDefs": [],
        "children": [
            {
                "_type": "span",
                "_key": _block_key(idx, text, "span"),
                "text": text,
                "marks": [],
            }
        ],
    }


def _block_key(idx: int, text: str, suffix: str = "") -> str:
    seed = f"{idx}-{text}-{suffix}".encode("utf-8")
    return hashlib.sha1(seed).hexdigest()[:12]


# --- Table of contents ----------------------------------------------------


_TOC_STYLES = ("h2", "h3")


def extract_toc_from_body(blocks: list[dict[str, Any]]) -> list[str]:
    """Collect heading text from Portable Text blocks to populate a TOC.

    Walks the block list, picks blocks with ``style`` in ``("h2", "h3")``,
    and joins each block's child span text. Empty headings are skipped so
    we don't render bullets pointing at nothing.
    """
    toc: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("style") not in _TOC_STYLES:
            continue
        children = block.get("children") or []
        text = "".join(
            (child.get("text") or "")
            for child in children
            if isinstance(child, dict)
        ).strip()
        if text:
            toc.append(text)
    return toc


# --- Post conversion -------------------------------------------------------


def estimate_read_time(body: str) -> int:
    """~250 words per minute, minimum 1, maximum 60."""
    words = max(1, len(body.split()))
    return max(1, min(60, round(words / 250)))


def slugify(s: str, max_length: int = 96) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = s.strip()
    s = re.sub(r"\s+", "-", s)
    return s[:max_length] or "untitled"


def excerpt_from_body(body: str, max_chars: int = 280) -> str:
    """First paragraph or first ``max_chars`` chars, whichever shorter."""
    first_para = (body.split("\n\n", 1)[0] or "").strip()
    if len(first_para) <= max_chars:
        return first_para
    return first_para[: max_chars - 1].rstrip() + "…"


# --- Publisher -------------------------------------------------------------


@dataclass(frozen=True)
class SanityPostInput:
    """Minimal info needed to create a Sanity post draft."""

    title: str
    body_markdown: str
    language: Language
    category: str
    source_url: str
    topic_id: str
    key_takeaway: str = ""
    cover_image_asset_id: str | None = None
    cover_image_alt: str = ""


class SanityPublisher:
    """Publishes a draft of a post into Sanity for Andriy to approve."""

    def __init__(self, client: SanityClient | None = None) -> None:
        self.client = client or SanityClient()

    async def publish_draft(self, post: SanityPostInput) -> str:
        """Create a draft document in Sanity. Returns the draft's ``_id``."""
        body_pt = markdown_to_portable_text(post.body_markdown)
        read_time = estimate_read_time(post.body_markdown)
        slug = slugify(post.title)

        doc: dict[str, Any] = {
            "_type": "post",
            "title": post.title[:200],
            "slug": {"_type": "slug", "current": slug},
            "language": post.language.value,
            "category": post.category,
            "excerpt": post.key_takeaway[:280] or excerpt_from_body(post.body_markdown),
            "readTime": read_time,
            "publishedAt": datetime.now(tz=timezone.utc).isoformat(),
            "body": body_pt,
            # NEW fields from the post.ts patch (see docs/sanity-post-patch.md)
            "keyTakeaway": post.key_takeaway[:280],
            "sourceUrl": post.source_url,
            "topicId": post.topic_id,
            "generatedBy": "pipeline",
        }

        toc = extract_toc_from_body(body_pt)
        if toc:
            doc["tableOfContents"] = toc

        if post.cover_image_asset_id:
            doc["coverImage"] = {
                "_type": "image",
                "asset": {"_type": "reference", "_ref": post.cover_image_asset_id},
            }
            if post.cover_image_alt:
                doc["coverImageAlt"] = post.cover_image_alt[:200]

        draft_id = await self.client.create_draft(doc)
        return draft_id

    # --- Helpers used by run_pipeline.py ---------------------------------

    async def is_topic_already_posted(self, topic_id: str, language: Language) -> bool:
        """Cheap dedup check against Sanity itself.

        Note: this is the *secondary* dedup — primary is in
        ``pipeline/selector/dedup.py``. This guards the case where the
        local SQLite ``seen`` table was wiped and we'd otherwise produce
        a true duplicate in Sanity.
        """
        groq = (
            '*[_type == "post" && topicId == $tid && language == $lang][0]._id'
        )
        result = await self.client.query(
            groq, {"tid": topic_id, "lang": language.value}
        )
        return result is not None

    async def upload_cover_image(
        self, image_bytes: bytes, filename: str
    ) -> str:
        """Upload an image, return its asset _id."""
        asset = await self.client.upload_image(image_bytes, filename)
        return str(asset["_id"])


# --- Adapter to legacy publisher.Post object -------------------------------


async def publish_post_object(post: Post, draft: Draft, source_url: str) -> str:
    """Bridge between the legacy ``Post``/``Draft`` model (channel=blog)
    and Sanity.

    Currently used only for the blog channel; for Telegram / FB / IG the
    existing publishers in ``pipeline/publisher/`` are unchanged.
    """
    if post.channel is not Channel.blog:
        raise ValueError(f"SanityPublisher only supports blog, got {post.channel}")

    sanity = SanityPublisher()

    # If we already have an image_url it's from Replicate. Upload to Sanity.
    cover_asset_id = None
    if draft.image_url:
        import httpx as _httpx

        async with _httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(str(draft.image_url))
            resp.raise_for_status()
            cover_asset_id = await sanity.upload_cover_image(
                resp.content, filename=f"{draft.topic_id}.png"
            )

    # For Icon, infer category from the topic if not supplied. Fallback "special".
    category = "special"  # Will be overridden by run_pipeline once it loads brand config.

    inp = SanityPostInput(
        title=draft.title,
        body_markdown=draft.body,
        language=draft.language,
        category=category,
        source_url=source_url,
        topic_id=draft.topic_id,
        key_takeaway=draft.key_takeaway,
        cover_image_asset_id=cover_asset_id,
        cover_image_alt=draft.image_alt,
    )
    return await sanity.publish_draft(inp)
