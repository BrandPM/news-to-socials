"""The event cluster: one news event, every material about it (NTS_131 P3 Ф2).

The unit of work used to be the feed item. One item became one candidate
became one article, written from one document — and the second outlet on the
same decision was a *duplicate*, which the dedup dropped. So the material the
event actually had (another regulator's notice, the law-firm alerts, the trade
press with the numbers the press release left out) never reached the fact
pack, and the article came out with six facts where the event held fifteen.

Now a candidate is the **anchor** of a cluster, and every other material about
the same event is a cluster source:

* **intake** joins an item to a live candidate's cluster instead of dropping
  it, when :func:`same_event` says it is the same event;
* **sourcing** (a person or an agent reading the regulators' sites directly)
  adds sources with :func:`add_source`, ``origin='sourcing'``;
* **production** reads them back with :func:`gather_documents` and builds one
  fact pack across all of them.

Same event is *similarity plus identifiers*. Two outlets writing about one
decision are not near-duplicates of each other — a law-firm alert and the
regulator's notice share a subject, not a text — so the embedding threshold
here is lower than the dedup one, and what makes up the difference is shared
identifiers: instrument names (DAC8, CARF), case and notice numbers, the
figures themselves. Similarity alone at 0.80 would glue together every FINMA
notice of the month; identifiers alone would glue every article that mentions
"2027".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from ..common.logging import get_logger
from .candidate_dedup import normalize_doc_url
from .dedup import cosine

log = get_logger(__name__)

# Candidates that can still take a source. A drafted article is not reopened
# by a new outlet — that would be a new article, and the dedup decides that.
JOINABLE_STATUSES: tuple[str, ...] = ("pending", "doc_missing", "selected")

# Two shared identifiers, not one: a single shared "2026" or "FATF" is what
# every article of the month has in common.
MIN_SHARED_KEYS = 2

_NUMBER_RE = re.compile(r"\d[\d.,/\-]*\d|\d")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# Acronyms and instrument names: FINMA, DAC8, CARF, MiCA, AIFMD II, 2024/1640.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z0-9]*\b|\b[A-Z]{2,}\d*\b")
# Identifiers every item shares, which therefore identify nothing.
_GENERIC_KEYS = frozenset(
    {
        "EU", "UK", "US", "USA", "UAE", "EUR", "USD", "GBP", "CHF", "AED",
        "SGD", "CEO", "CFO", "PDF", "FAQ", "FAQS", "NEWS", "PRESS", "II",
        "III", "IV", "OK", "AI", "HTML", "Q1", "Q2", "Q3", "Q4",
    }
)


def entity_keys(text: str) -> set[str]:
    """The identifiers in ``text`` that could tie two items to one event.

    Bare years are dropped — "2026" identifies a year, not an event — and so
    are single digits. Numbers are normalised to their digits, so "EUR 5,000"
    and "EUR 5000" share a key.
    """
    keys: set[str] = set()
    for match in _NUMBER_RE.findall(text or ""):
        digits = re.sub(r"[^\d]", "", match)
        if len(digits) < 2 or _YEAR_RE.match(digits):
            continue
        keys.add(f"#{digits}")
    for match in _ACRONYM_RE.findall(text or ""):
        token = match.strip()
        if token.upper() in _GENERIC_KEYS or len(token) < 3:
            continue
        keys.add(token.upper())
    return keys


def same_event(
    similarity: float,
    keys_a: Iterable[str],
    keys_b: Iterable[str],
    *,
    live_threshold: float,
    cluster_threshold: float,
) -> bool:
    """Is the new item about the anchor's event?

    At or above the dedup threshold it is, whatever the identifiers say — that
    is the case the old dedup dropped as a duplicate. Between the cluster
    threshold and the dedup threshold it needs :data:`MIN_SHARED_KEYS` shared
    identifiers. Below the cluster threshold it is a different story.
    """
    if similarity >= live_threshold:
        return True
    if similarity < cluster_threshold:
        return False
    return len(set(keys_a) & set(keys_b)) >= MIN_SHARED_KEYS


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterSource:
    url: str
    title: str
    publisher: str
    summary: str
    role: str
    origin: str
    similarity: float | None = None


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def ensure_cluster(*, anchor_candidate_id: int) -> int | None:
    """The anchor's cluster id, creating the cluster on first use."""
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, EventCluster

    with get_session_factory()() as session:
        existing = session.execute(
            select(EventCluster.id).where(
                EventCluster.anchor_candidate_id == anchor_candidate_id
            )
        ).scalar()
        if existing is not None:
            return int(existing)
        anchor = session.get(Candidate, anchor_candidate_id)
        if anchor is None:
            return None
        cluster = EventCluster(
            brand_id_fk=anchor.brand_id_fk,
            anchor_candidate_id=anchor_candidate_id,
            title=anchor.source_title or "(untitled)",
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(cluster)
        session.commit()
        return int(cluster.id)


def add_source(
    *,
    anchor_candidate_id: int,
    url: str,
    title: str = "",
    publisher: str = "",
    summary: str = "",
    role: str = "secondary",
    origin: str = "manual",
    similarity: float | None = None,
    source_id_fk: int | None = None,
    published_at: datetime | None = None,
    max_sources: int | None = None,
) -> bool:
    """Attach one material to the anchor's event. ``False`` if not added.

    Not added when it is the anchor's own document, when the cluster already
    holds the URL, or when the cluster is full (``max_sources``). A full
    cluster is not an error: past five documents the fact pack stops getting
    richer and starts getting slower.
    """
    from sqlalchemy import func, select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, EventClusterSource

    key = normalize_doc_url(url)
    if not key:
        return False
    cluster_id = ensure_cluster(anchor_candidate_id=anchor_candidate_id)
    if cluster_id is None:
        return False
    with get_session_factory()() as session:
        anchor = session.get(Candidate, anchor_candidate_id)
        own = {
            normalize_doc_url(getattr(anchor, "primary_doc_url", None)),
            normalize_doc_url(getattr(anchor, "source_url", None)),
        }
        if key in own:
            return False
        dup = session.execute(
            select(EventClusterSource.id).where(
                EventClusterSource.cluster_id == cluster_id,
                EventClusterSource.url_key == key,
            )
        ).scalar()
        if dup is not None:
            return False
        if max_sources is not None:
            held = session.execute(
                select(func.count(EventClusterSource.id)).where(
                    EventClusterSource.cluster_id == cluster_id
                )
            ).scalar()
            if int(held or 0) >= max_sources:
                log.info(
                    "event_cluster.full",
                    anchor=anchor_candidate_id,
                    held=held,
                    url=url,
                )
                return False
        session.add(
            EventClusterSource(
                cluster_id=cluster_id,
                url=url,
                url_key=key,
                title=title or None,
                publisher=publisher or None,
                summary=summary or None,
                role=role,
                origin=origin,
                similarity=similarity,
                source_id_fk=source_id_fk,
                published_at=published_at,
                added_at=_now(),
            )
        )
        session.commit()
    log.info(
        "event_cluster.source_added",
        anchor=anchor_candidate_id,
        url=url,
        role=role,
        origin=origin,
        sim=round(similarity, 3) if similarity is not None else None,
    )
    return True


def load_sources(anchor_candidate_id: int) -> list[ClusterSource]:
    """The cluster's sources, primary ones first, oldest first within a role."""
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import EventCluster, EventClusterSource

    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(EventClusterSource)
                .join(EventCluster, EventCluster.id == EventClusterSource.cluster_id)
                .where(EventCluster.anchor_candidate_id == anchor_candidate_id)
                .order_by(EventClusterSource.role, EventClusterSource.id)
            ).scalars().all()
            return [
                ClusterSource(
                    url=r.url,
                    title=r.title or "",
                    publisher=r.publisher or "",
                    summary=r.summary or "",
                    role=r.role,
                    origin=r.origin,
                    similarity=r.similarity,
                )
                for r in rows
            ]
    except Exception as exc:
        # A cluster we cannot read is a single-document article, not a failure.
        log.warning("event_cluster.load_failed", anchor=anchor_candidate_id, err=str(exc))
        return []


# --------------------------------------------------------------------------
# intake: join instead of drop
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Joinable:
    candidate_id: int
    embedding: np.ndarray
    keys: frozenset[str]


def _joinable_window(brand_id_fk: int) -> list[_Joinable]:
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate, TopicEmbedding

    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(
                    Candidate.id,
                    Candidate.source_title,
                    Candidate.source_summary,
                    TopicEmbedding.embedding,
                )
                .join(
                    TopicEmbedding,
                    TopicEmbedding.topic_id == Candidate.topic_embedding_ref,
                )
                .where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.status.in_(JOINABLE_STATUSES),
                )
            ).all()
    except Exception as exc:
        log.warning("event_cluster.window_failed", err=str(exc))
        return []
    return [
        _Joinable(
            candidate_id=int(r[0]),
            embedding=np.frombuffer(r[3], dtype=np.float32),
            keys=frozenset(entity_keys(f"{r[1] or ''} {r[2] or ''}")),
        )
        for r in rows
        if r[3]
    ]


def find_anchor(
    *,
    brand_id_fk: int,
    embedding: np.ndarray,
    text: str,
    live_threshold: float,
    cluster_threshold: float,
    exclude: Sequence[int] = (),
) -> tuple[int, float] | None:
    """The live candidate whose event this item is about, if any.

    Fail-open like the dedup: a window that cannot be loaded means "no
    cluster", and the item goes on to become a candidate of its own — the
    visible failure (two articles on one event) rather than the invisible one
    (an item silently attached to the wrong story).
    """
    keys = entity_keys(text)
    best: tuple[int, float] | None = None
    for item in _joinable_window(brand_id_fk):
        if item.candidate_id in exclude:
            continue
        sim = float(cosine(embedding, item.embedding))
        if not same_event(
            sim,
            keys,
            item.keys,
            live_threshold=live_threshold,
            cluster_threshold=cluster_threshold,
        ):
            continue
        if best is None or sim > best[1]:
            best = (item.candidate_id, sim)
    return best


# --------------------------------------------------------------------------
# production: the documents the pack is built across
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterDocument:
    """One cluster source, fetched and cut to its share of the budget."""

    url: str
    publisher: str
    role: str
    title: str
    text: str
    as_of: str = ""


async def gather_documents(
    *,
    anchor_candidate_id: int,
    headline: str,
    budget: Any,
    max_sources: int = 5,
    tokens_per_document: int = 4000,
    now: datetime | None = None,
) -> list[ClusterDocument]:
    """Fetch every cluster source that can be read; skip the ones that cannot.

    A source that 403s or extracts to nothing is logged and left out: the
    anchor's document is the requirement, the rest is enrichment, and one
    law firm's CDN must not cost the article.
    """
    from pipeline.sources.document_fetcher import fetch_document, select_sections

    out: list[ClusterDocument] = []
    for source in load_sources(anchor_candidate_id)[:max_sources]:
        try:
            doc = await fetch_document(
                source.url,
                budget=budget,
                source_class=None,
                now=now,
            )
        except Exception as exc:
            log.warning(
                "event_cluster.source_unreadable",
                anchor=anchor_candidate_id,
                url=source.url,
                err=str(exc)[:200],
            )
            continue
        selection = select_sections(
            doc.text,
            hint=None,
            headline=headline,
            max_tokens=tokens_per_document,
        )
        out.append(
            ClusterDocument(
                url=doc.url,
                publisher=source.publisher,
                role=source.role,
                title=source.title or (doc.title or ""),
                text=selection.text,
                as_of=doc.as_of.date().isoformat() if getattr(doc, "as_of", None) else "",
            )
        )
    log.info(
        "event_cluster.documents",
        anchor=anchor_candidate_id,
        read=len(out),
    )
    return out
