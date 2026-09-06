"""Internal links, per language sibling, after translation (NTS_093).

One link to the service page the article's category maps to, and up to three to
previously published articles on the same subject. The mapping is 1:1 and
already in ``brand_taxonomy`` (``service_url_path``), so choosing the service
is a lookup, not a model call — NTS_093 says as much, and a model asked to pick
between five fixed options would only introduce a way to pick wrong.

**Why this runs after translation, not before.** Two reasons, both from
NTS_093, both the kind that fail silently:

1. ``/en/services/family-office`` must become ``/ru/services/family-office``.
   Relying on a translation pass to rewrite a path inside a markdown link is an
   invitation to a broken URL that nobody notices — and NTS_065 made the
   translation *faithful*, which means it should not be touching URLs at all.
2. Article slugs are localised (NTS_081). A link to a related article is not
   translated; it is **re-resolved** from that article's sibling in the target
   language. No sibling in that language, no link.

Placement rules, also NTS_093: never in the lede, never in the closing
paragraph. The close rule from NTS_067 wants the final paragraph anchored to a
specific fact of this article; a service link there turns it straight back into
the generic call-to-action that rule was written to remove.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# NTS_093 — one service link, two or three article links. More is spam.
MAX_SERVICE_LINKS = 1
MAX_ARTICLE_LINKS = 3
# Cosine floor for "related enough to link". Below this the link costs the
# reader a click and teaches them the links are not worth following.
RELATED_MIN_SIMILARITY = 0.55

# The live domain. ``icon.finance`` is dead and must never be emitted
# (NTS_093 §Реализация).
DEFAULT_BASE_URL = "https://iconfinance.io"

_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_H2_RE = re.compile(r"^##\s", re.MULTILINE)


@dataclass(frozen=True)
class LinkTarget:
    """Somewhere worth sending the reader."""

    url: str
    anchor: str
    kind: str  # service | article


def service_url(
    *, service_path: str | None, language: str, base_url: str = DEFAULT_BASE_URL
) -> str | None:
    """``/{lang}{service_path}`` on the live domain.

    Service slugs are not localised — only the language prefix changes
    (NTS_093) — so this is a substitution, and getting it wrong is not a
    quality question but a 404.
    """
    if not service_path:
        return None
    path = service_path if service_path.startswith("/") else f"/{service_path}"
    return f"{base_url.rstrip('/')}/{language}{path}"


def paragraphs(body: str) -> list[str]:
    """The body split into paragraphs, headings included as their own entries."""
    return [p for p in re.split(r"\n\s*\n", body or "") if p.strip()]


def linkable_paragraph_indexes(body: str) -> list[int]:
    """Which paragraphs may carry a link (NTS_093 §Правила).

    Excludes the lede (the first paragraph), the final paragraph, headings, and
    any paragraph that already carries a link. The exclusions are what keep the
    close anchored and the piece from reading as an ad.
    """
    parts = paragraphs(body)
    if len(parts) < 4:
        # Too short to place a link anywhere that is neither the opening nor
        # the close. A note-length piece simply goes out without links.
        return []
    out: list[int] = []
    for index, part in enumerate(parts):
        if index == 0 or index == len(parts) - 1:
            continue
        if part.lstrip().startswith("#"):
            continue
        if _MD_LINK_RE.search(part):
            continue
        out.append(index)
    return out


def find_anchor(paragraph: str, candidates: Sequence[str]) -> str | None:
    """The first candidate phrase that appears verbatim in the paragraph.

    NTS_093: the anchor is a natural noun phrase from the text, never "our
    services" or a bare URL. No phrase present means no link — "лучше
    пропустить, чем вклеить абзац-обрубок".
    """
    lowered = paragraph.lower()
    for phrase in candidates:
        cleaned = (phrase or "").strip()
        if len(cleaned) < 4:
            continue
        position = lowered.find(cleaned.lower())
        if position >= 0:
            # Return the text as it appears, not as it was given: case matters
            # in the middle of a sentence.
            return paragraph[position : position + len(cleaned)]
    return None


def insert_link(body: str, index: int, anchor: str, url: str) -> str:
    """Wrap the first occurrence of ``anchor`` in paragraph ``index``."""
    parts = paragraphs(body)
    if index >= len(parts):
        return body
    target = parts[index]
    position = target.find(anchor)
    if position < 0:
        return body
    parts[index] = (
        target[:position]
        + f"[{anchor}]({url})"
        + target[position + len(anchor) :]
    )
    return "\n\n".join(parts)


def apply_links(
    body: str, targets: Sequence[LinkTarget], *, anchor_pool: Sequence[str]
) -> tuple[str, list[LinkTarget]]:
    """Place as many of ``targets`` as have somewhere natural to go.

    Returns the new body and the links actually placed. A target with no anchor
    in any linkable paragraph is skipped rather than forced: the rule this
    keeps is that the link reads as part of the sentence or does not exist.
    """
    placed: list[LinkTarget] = []
    used_paragraphs: set[int] = set()
    used_urls: set[str] = set()
    current = body
    for target in targets:
        if target.url in used_urls:
            continue
        for index in linkable_paragraph_indexes(current):
            if index in used_paragraphs:
                continue
            paragraph = paragraphs(current)[index]
            anchor = find_anchor(paragraph, [target.anchor, *anchor_pool])
            if anchor is None:
                continue
            current = insert_link(current, index, anchor, target.url)
            used_paragraphs.add(index)
            used_urls.add(target.url)
            placed.append(LinkTarget(target.url, anchor, target.kind))
            break
    return current, placed


def related_articles(
    *,
    brand_id_fk: int,
    topic_id: str,
    language: str,
    limit: int = MAX_ARTICLE_LINKS,
    min_similarity: float = RELATED_MIN_SIMILARITY,
) -> list[tuple[str, float]]:
    """Topic ids of published articles closest to this one, same brand.

    Costs nothing extra: the embedding was already computed for dedup
    (NTS_079/NTS_093). Returns ``(topic_id, similarity)`` — resolving each to a
    localised slug is the caller's job, because that lookup is a Sanity query
    and this module stays free of network.
    """
    try:
        import numpy as np
        from sqlalchemy import select

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import TopicEmbedding

        with get_session_factory()() as session:
            rows = (
                session.execute(
                    select(TopicEmbedding.topic_id, TopicEmbedding.embedding).where(
                        TopicEmbedding.brand_id_fk == brand_id_fk
                    )
                )
            ).all()
        vectors = {
            tid: np.frombuffer(blob, dtype=np.float32)
            for tid, blob in rows
            if blob
        }
        mine = vectors.get(topic_id)
        if mine is None:
            return []
        norm_mine = float(np.linalg.norm(mine)) or 1.0
        scored: list[tuple[str, float]] = []
        for other_id, vector in vectors.items():
            if other_id == topic_id or vector.shape != mine.shape:
                continue
            similarity = float(
                mine @ vector / (norm_mine * (float(np.linalg.norm(vector)) or 1.0))
            )
            if similarity >= min_similarity:
                scored.append((other_id, similarity))
        scored.sort(key=lambda pair: -pair[1])
        return scored[:limit]
    except Exception as exc:
        # A missing related-articles list costs two links; raising would cost
        # the article.
        log.warning("internal_links.related_failed", err=str(exc)[:200])
        return []


async def link_draft(
    *,
    body: str,
    language: str,
    category: str | None,
    brand_id_fk: int,
    topic_id: str,
    taxonomy: Sequence[Any] = (),
    anchor_pool: Sequence[str] = (),
    resolve_article_url: Any = None,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[str, list[LinkTarget]]:
    """Link one language sibling. Never raises.

    ``resolve_article_url(topic_id, language) -> str | None`` is injected
    because resolving a related article's *localised* slug is a Sanity query,
    and a sibling that does not exist in this language must produce no link at
    all rather than a link to the English one (NTS_093 §2).
    """
    targets: list[LinkTarget] = []

    service_path = None
    service_label = ""
    for row in taxonomy:
        if str(getattr(row, "key", row.get("key") if isinstance(row, dict) else "")) == (
            category or ""
        ):
            service_path = (
                getattr(row, "service_url_path", None)
                if not isinstance(row, dict)
                else row.get("service_url_path")
            )
            service_label = (
                getattr(row, "label", "")
                if not isinstance(row, dict)
                else row.get("label", "")
            )
            break
    url = service_url(service_path=service_path, language=language, base_url=base_url)
    if url and len(targets) < MAX_SERVICE_LINKS:
        targets.append(LinkTarget(url, service_label, "service"))

    if resolve_article_url is not None:
        for other_id, _score in related_articles(
            brand_id_fk=brand_id_fk, topic_id=topic_id, language=language
        ):
            try:
                resolved = await resolve_article_url(other_id, language)
            except Exception as exc:
                log.warning("internal_links.resolve_failed", err=str(exc)[:200])
                continue
            if not resolved:
                continue
            url, anchor = (
                resolved if isinstance(resolved, tuple) else (resolved, "")
            )
            targets.append(LinkTarget(url, anchor, "article"))
            if len([t for t in targets if t.kind == "article"]) >= MAX_ARTICLE_LINKS:
                break

    linked, placed = apply_links(body, targets, anchor_pool=anchor_pool)
    log.info(
        "internal_links.applied",
        language=language,
        placed=[t.kind for t in placed],
        offered=len(targets),
    )
    return linked, placed
