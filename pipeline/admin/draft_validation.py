"""Structural completeness guard for Sanity drafts (IT_PROJ_NTS_090).

The incident this exists for: articles went live with ``coverImage: null``.
Nothing server-side stopped it — the UI happened to allow Approve and the
publish endpoint promoted the draft as-is. This module is the authoritative
answer to "is this document whole enough to publish?", and the publish path
in :mod:`pipeline.admin.routes.drafts` refuses to promote when it says no.

Scope is deliberately **structural**: a component is either present or it is
not. Quality gates (banned phrases, AI-tells, the LLM judge) stay where they
are — at generation time. Re-running them here would turn a transient model
opinion into a hard publish block, and a false positive there costs more than
it saves.

The validator accepts either shape of a Sanity doc:

* the raw document (``coverImage.asset._ref``, ``body`` as portable text), or
* the cheap projection in :data:`COMPLETENESS_PROJECTION` (``coverImageRef``,
  ``bodyBlockCount`` / ``bodyH2Count`` counted server-side by GROQ)

so the list view can compute completeness for 50 rows without shipping 50
article bodies over the wire.
"""

from __future__ import annotations

from typing import Any

# GROQ projection that carries exactly what :func:`validate_draft_complete`
# needs — nothing else. ``count(body[...])`` is evaluated by Sanity, so a
# 3000-word body costs two integers here.
COMPLETENESS_PROJECTION = (
    "{_id, language, title, displayDate, "
    '"slug": slug.current, '
    '"coverImageRef": coverImage.asset._ref, '
    '"bodyBlockCount": count(body), '
    '"bodyH2Count": count(body[style == "h2"])}'
)

# Canonical order for the returned codes — stable so tests and the UI banner
# read the same way every time (cover first: it is the incident).
COMPONENT_CODES = ("coverImage", "title", "slug", "body", "body_h2", "displayDate")


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _cover_ref(doc: dict) -> Any:
    """Asset ref from either the projection or the raw ``coverImage`` object."""
    if "coverImageRef" in doc:
        return doc.get("coverImageRef")
    cover = doc.get("coverImage")
    if not isinstance(cover, dict):
        return None
    asset = cover.get("asset")
    if not isinstance(asset, dict):
        return None
    return asset.get("_ref")


def _slug_value(doc: dict) -> Any:
    """``slug`` is a string in the projection, ``{current: ...}`` when raw."""
    slug = doc.get("slug")
    if isinstance(slug, dict):
        return slug.get("current")
    return slug


def _body_counts(doc: dict) -> tuple[int, int]:
    """``(block_count, h2_count)`` from the projection or the raw body array."""
    if "bodyBlockCount" in doc or "bodyH2Count" in doc:
        blocks = doc.get("bodyBlockCount")
        h2s = doc.get("bodyH2Count")
        return (
            int(blocks) if isinstance(blocks, (int, float)) else 0,
            int(h2s) if isinstance(h2s, (int, float)) else 0,
        )
    body = doc.get("body")
    if not isinstance(body, list):
        return 0, 0
    h2 = sum(
        1
        for b in body
        if isinstance(b, dict) and b.get("style") == "h2"
    )
    return len(body), h2


def validate_draft_complete(doc: dict | None) -> list[str]:
    """Return the codes of every missing required component (``[]`` = OK).

    ``None`` / a non-dict means the document could not be resolved at all;
    that is reported as every component missing rather than "fine", so a
    failed read can never be mistaken for a passing check.
    """
    if not isinstance(doc, dict) or not doc:
        # Everything except ``body_h2`` — an absent body already implies it.
        return ["coverImage", "title", "slug", "body", "displayDate"]

    missing: list[str] = []

    if not _non_empty_str(_cover_ref(doc)):
        missing.append("coverImage")
    if not _non_empty_str(doc.get("title")):
        missing.append("title")
    if not _non_empty_str(_slug_value(doc)):
        missing.append("slug")

    block_count, h2_count = _body_counts(doc)
    if block_count <= 0:
        # No body at all — "no H2" would be noise on top of it.
        missing.append("body")
    elif h2_count <= 0:
        missing.append("body_h2")

    if not _non_empty_str(doc.get("displayDate")):
        missing.append("displayDate")

    return missing


async def fetch_draft_for_validation(client: Any, sanity_id: str) -> dict | None:
    """Read the completeness projection for one document id.

    Returns ``None`` when Sanity has no such document — the caller decides
    what that means (the publish path lets the existing "draft not found"
    error surface rather than reporting it as an incomplete draft).
    """
    doc = await client.query(
        f"*[_id == $id][0]{COMPLETENESS_PROJECTION}", {"id": sanity_id}
    )
    return doc if isinstance(doc, dict) and doc else None
