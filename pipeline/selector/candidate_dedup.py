"""Dedup for the portfolio: three windows, one document key (NTS_098 §3).

NTS_079's engine deduped *topics* against a single 7-day window before v2
generation. The portfolio needs something different, because a candidate lives
for up to three weeks and the answer to "have we seen this?" depends on what
happened to the thing we saw:

| Window | Compared against | Threshold | Outcome |
|---|---|---|---|
| live | candidates in a live status | ``dedup_threshold_live`` (0.90) | not created; a LATER stage supersedes instead |
| rejected | rejects from the last ``dedup_window_rejected_days`` (14) | ``dedup_threshold_rejected`` (0.92) | not created, and **the guard is not paid again** — the reason code is copied |
| published | published in the last ``dedup_window_published_days`` (60) | ``dedup_threshold_published`` (0.88) | duplicate if the stage is the same; a new stage becomes a candidate pointing back |

Plus, for ``input_kind='document'``, a normalised ``primary_doc_url``: one
document, one candidate, decided without an embedding at all.

**Why this is split across the guard call.** NTS_098 §3 makes the live/published
outcome depend on ``event_stage`` ("новая стадия — не дубль"), and the stage is
something only the guard knows. So:

* :func:`check_pre_guard` runs *before* the guard and decides only what can be
  decided without a verdict: an exact document-URL repeat, and a match in the
  rejected window — the one case where the spec explicitly says not to spend
  the money twice.
* :func:`check_post_guard` runs *after* the verdict, with ``event_stage`` in
  hand, and turns a live/published similarity into either a duplicate (same
  stage) or a supersede (later stage). A guard call is ~$0.0002; buying the
  stage with it is cheaper than guessing at it, and guessing wrong here means
  either dropping a follow-up story or writing the same one twice.

Fail-open, like NTS_079: any internal error resolves to "not a duplicate". A
duplicate that slips through costs one article a human can spot; a real story
dropped by a DB hiccup is invisible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from ..admin.models import CANDIDATE_LIVE_STATUSES
from ..common.logging import get_logger
from .dedup import cosine

log = get_logger(__name__)

DEDUP_WINDOWS = ("live", "rejected", "published", "doc_url")

# The live window is ``CANDIDATE_LIVE_STATUSES`` itself, imported rather than
# restated: a status added to one list and not the other would silently shrink
# the window, and the symptom would be duplicate articles, not an error.


@dataclass(frozen=True)
class CandidateDedupConfig:
    threshold_live: float = 0.90
    threshold_rejected: float = 0.92
    window_rejected_days: int = 14
    threshold_published: float = 0.88
    window_published_days: int = 60

    @classmethod
    def from_config(cls, config: Any) -> CandidateDedupConfig:
        return cls(
            threshold_live=float(getattr(config, "dedup_threshold_live", 0.90)),
            threshold_rejected=float(
                getattr(config, "dedup_threshold_rejected", 0.92)
            ),
            window_rejected_days=int(
                getattr(config, "dedup_window_rejected_days", 14)
            ),
            threshold_published=float(
                getattr(config, "dedup_threshold_published", 0.88)
            ),
            window_published_days=int(
                getattr(config, "dedup_window_published_days", 60)
            ),
        )


@dataclass(frozen=True)
class WindowItem:
    """One candidate in a dedup window, with its embedding."""

    candidate_id: int
    status: str
    event_stage: str | None
    reason_code: str | None
    reason: str | None
    embedding: np.ndarray


@dataclass(frozen=True)
class PreGuardDecision:
    """``action`` ∈ {guard, skip_doc_url, copy_rejected}."""

    action: str
    matched_candidate_id: int | None = None
    similarity: float = 0.0
    window: str | None = None
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PostGuardDecision:
    """``action`` ∈ {create, duplicate, supersede}."""

    action: str
    matched_candidate_id: int | None = None
    similarity: float = 0.0
    window: str | None = None


_GUARD_IT = PreGuardDecision("guard")
_CREATE_IT = PostGuardDecision("create")


def normalize_doc_url(url: str | None) -> str | None:
    """Lowercase host, drop the query, the fragment and a trailing slash.

    The query is dropped because regulator sites routinely append tracking and
    session parameters to the same PDF; keeping it would make one document look
    like five. The path case is preserved — plenty of document stores are
    case-sensitive, and folding it would collide two real documents.
    """
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    if not parts.netloc:
        return raw.rstrip("/") or None
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


# --- window loading --------------------------------------------------------


def _load_window(
    *,
    brand_id_fk: int,
    statuses: tuple[str, ...],
    since: datetime | None,
    since_column: str,
) -> list[WindowItem]:
    """Load candidates in ``statuses`` with their embeddings. ``[]`` on error.

    The embedding is joined through ``candidates.topic_embedding_ref`` →
    ``topic_embeddings.topic_id``, so a candidate whose embedding was never
    persisted simply does not participate in dedup — it is not skipped, and it
    is not compared against with a zero vector (which would match everything
    equally badly).
    """
    try:
        from sqlalchemy import select

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import Candidate, TopicEmbedding

        stmt = (
            select(
                Candidate.id,
                Candidate.status,
                Candidate.event_stage,
                Candidate.reason_code,
                Candidate.reason,
                TopicEmbedding.embedding,
            )
            .join(
                TopicEmbedding,
                TopicEmbedding.topic_id == Candidate.topic_embedding_ref,
            )
            .where(
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.status.in_(statuses),
            )
        )
        if since is not None:
            column = getattr(Candidate, since_column)
            stmt = stmt.where(column >= since)
        with get_session_factory()() as session:
            rows = session.execute(stmt).all()
        return [
            WindowItem(
                candidate_id=r[0],
                status=r[1],
                event_stage=r[2],
                reason_code=r[3],
                reason=r[4],
                embedding=np.frombuffer(r[5], dtype=np.float32),
            )
            for r in rows
            if r[5]
        ]
    # Fail open: a window we could not load means "not a duplicate".
    except Exception as exc:
        log.warning("candidate_dedup.window_load_failed", err=str(exc))
        return []


def _best_match(
    embedding: np.ndarray, window: list[WindowItem]
) -> tuple[WindowItem | None, float]:
    best: WindowItem | None = None
    best_sim = 0.0
    for item in window:
        try:
            sim = cosine(embedding, item.embedding)
        except ValueError:
            # Dimensionality mismatch — an embedding model change. Skipping the
            # vector is right: comparing across models produces noise, and
            # treating noise as a duplicate drops real stories.
            continue
        if sim > best_sim:
            best, best_sim = item, sim
    return best, best_sim


def _doc_url_match(*, brand_id_fk: int, doc_url: str) -> int | None:
    try:
        from sqlalchemy import select

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import Candidate

        with get_session_factory()() as session:
            rows = session.execute(
                select(Candidate.id, Candidate.primary_doc_url).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.primary_doc_url.is_not(None),
                )
            ).all()
        for candidate_id, url in rows:
            if normalize_doc_url(url) == doc_url:
                return int(candidate_id)
        return None
    # Fail open, as above.
    except Exception as exc:
        log.warning("candidate_dedup.doc_url_check_failed", err=str(exc))
        return None


# --- the two decisions -----------------------------------------------------


def check_pre_guard(
    *,
    brand_id_fk: int,
    embedding: np.ndarray,
    input_kind: str,
    primary_doc_url: str | None,
    config: CandidateDedupConfig,
    now: datetime | None = None,
) -> PreGuardDecision:
    """Decide what can be decided before paying the guard.

    ``skip_doc_url`` — this exact document already has a candidate (NTS_098 §3,
    "один документ — один кандидат"). ``copy_rejected`` — a near-identical item
    was rejected inside the rejected window, so the verdict is reused rather
    than re-bought. Anything else: ``guard``.
    """
    now = now or datetime.now(tz=UTC)

    if input_kind == "document":
        normalized = normalize_doc_url(primary_doc_url)
        if normalized:
            matched = _doc_url_match(brand_id_fk=brand_id_fk, doc_url=normalized)
            if matched is not None:
                return PreGuardDecision(
                    "skip_doc_url",
                    matched_candidate_id=matched,
                    similarity=1.0,
                    window="doc_url",
                )

    rejected_window = _load_window(
        brand_id_fk=brand_id_fk,
        statuses=("rejected",),
        since=now - timedelta(days=config.window_rejected_days),
        since_column="created_at",
    )
    match, sim = _best_match(embedding, rejected_window)
    if match is not None and sim >= config.threshold_rejected:
        return PreGuardDecision(
            "copy_rejected",
            matched_candidate_id=match.candidate_id,
            similarity=sim,
            window="rejected",
            reason_code=match.reason_code,
            reason=match.reason,
        )
    return _GUARD_IT


def check_post_guard(
    *,
    brand_id_fk: int,
    embedding: np.ndarray,
    event_stage: str | None,
    config: CandidateDedupConfig,
    now: datetime | None = None,
) -> PostGuardDecision:
    """Turn a live/published similarity into a duplicate or a supersede.

    Same stage → ``duplicate`` (no row). Different stage → ``supersede``: the
    caller creates the new candidate with ``supersedes_id`` set and, when the
    predecessor is still ``pending``/``doc_missing``, marks it ``superseded``
    (NTS_098 §2).

    An unknown stage on either side resolves to ``duplicate``, which is the
    conservative side: writing the same story twice is a visible editorial
    failure, while a missed follow-up is one candidate the next intake will see
    again if the story really has moved.
    """
    now = now or datetime.now(tz=UTC)

    live = _load_window(
        brand_id_fk=brand_id_fk,
        statuses=tuple(CANDIDATE_LIVE_STATUSES),
        since=None,
        since_column="created_at",
    )
    match, sim = _best_match(embedding, live)
    if match is not None and sim >= config.threshold_live:
        action = (
            "supersede"
            if event_stage and match.event_stage and event_stage != match.event_stage
            else "duplicate"
        )
        return PostGuardDecision(
            action,
            matched_candidate_id=match.candidate_id,
            similarity=sim,
            window="live",
        )

    published = _load_window(
        brand_id_fk=brand_id_fk,
        statuses=("published",),
        since=now - timedelta(days=config.window_published_days),
        since_column="published_at",
    )
    match, sim = _best_match(embedding, published)
    if match is not None and sim >= config.threshold_published:
        action = (
            "supersede"
            if event_stage and match.event_stage and event_stage != match.event_stage
            else "duplicate"
        )
        return PostGuardDecision(
            action,
            matched_candidate_id=match.candidate_id,
            similarity=sim,
            window="published",
        )

    return _CREATE_IT
