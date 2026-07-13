"""Persistent two-level dedup engine (IT_PROJ_NTS_090 / spec NTS_079).

Runs at **topic selection, before any generation spend**. Two levels:

* **L1 — deterministic:** normalise the source title, then exact/token-Jaccard
  (> 0.7) against current-run candidates + the persisted window. Catches literal
  reprints for ~free.
* **L2 — embeddings:** cosine of the source EN text embedding vs current-run
  candidates + the persisted window. ``>= threshold`` → duplicate (skip);
  ``[0.75, threshold)`` → *yellow* (kept, logged for calibration).

Why persistent (``topic_embeddings``): the pipeline processes each source in its
own ``run_pipeline`` invocation (NTS_074 isolation), so an in-memory set can't
see a sibling source's candidates. The table is the shared memory across
sources within a run AND across runs within ``dedup_window_days``.

**Fail-open contract:** every public entry point catches its own errors and
degrades to "not a duplicate" / "no-op". A dead embedding API or a DB hiccup
must never skip a topic wrongly or crash the run.

First-seen wins: the earliest stored topic is canonical; a later match is the
duplicate. The caller feeds candidates longest-summary-first so the richest
input becomes canonical on an intra-batch tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import delete, select

from ..common.logging import get_logger
from .dedup import cosine, jaccard, normalize_title

log = get_logger(__name__)

# Cosine floor for the "yellow" calibration zone. Below this → clearly not a
# dup; between this and the (configurable) skip threshold → logged, not skipped.
YELLOW_FLOOR = 0.75

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
_JACCARD_DUP = 0.70  # L1: strictly greater than this → duplicate title


@dataclass(frozen=True)
class DedupDecision:
    is_duplicate: bool
    matched_topic_id: str | None
    similarity: float
    level: int  # 0 = none, 1 = title, 2 = embedding
    action: str | None  # "skipped" | "yellow" | None


_NOT_DUP = DedupDecision(False, None, 0.0, 0, None)


@dataclass
class _WindowItem:
    topic_id: str
    embedding: np.ndarray
    title_norm: frozenset[str]


class DedupEngine:
    """Stateful, brand-scoped. One instance per source-processing pass.

    On construction it loads the persisted window from ``topic_embeddings``.
    ``check`` compares a candidate; ``remember`` marks it canonical (in-memory +
    persisted); ``record`` writes a ``dedup_log`` row. All are best-effort.
    """

    def __init__(
        self,
        *,
        brand_id_fk: int,
        threshold: float = 0.85,
        window_days: int = 7,
        model: str = DEFAULT_EMBED_MODEL,
        run_id: int | None = None,
    ) -> None:
        self.brand_id_fk = brand_id_fk
        self.threshold = threshold
        self.window_days = window_days
        self.model = model
        self.run_id = run_id
        self._window: list[_WindowItem] = []
        self._run_kept: list[_WindowItem] = []
        self._load_window()

    # --- window load -------------------------------------------------------

    def _load_window(self) -> None:
        try:
            from pipeline.admin.db import session_scope  # noqa: PLC0415
            from pipeline.admin.models import TopicEmbedding  # noqa: PLC0415

            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.window_days)
            with session_scope() as session:
                rows = (
                    session.execute(
                        select(TopicEmbedding).where(
                            TopicEmbedding.brand_id_fk == self.brand_id_fk,
                            TopicEmbedding.created_at >= cutoff,
                        )
                    )
                    .scalars()
                    .all()
                )
                for r in rows:
                    self._window.append(
                        _WindowItem(
                            topic_id=r.topic_id,
                            embedding=np.frombuffer(r.embedding, dtype=np.float32),
                            title_norm=frozenset(
                                (r.title_norm or "").split()
                            ),
                        )
                    )
            log.info("dedup.window_loaded", n=len(self._window), days=self.window_days)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("dedup.window_load_failed", err=str(exc))
            self._window = []

    # --- decision ----------------------------------------------------------

    def check(
        self, topic_id: str, title: str, embedding: np.ndarray
    ) -> DedupDecision:
        """Compare a candidate against kept + window items. Never raises."""
        try:
            candidates = self._run_kept + self._window
            title_toks = normalize_title(title)

            # L1 — deterministic title match.
            for item in candidates:
                if item.topic_id == topic_id:
                    continue
                j = jaccard(title_toks, item.title_norm)
                if j > _JACCARD_DUP:
                    return DedupDecision(True, item.topic_id, j, 1, "skipped")

            # L2 — embedding cosine (max over candidates).
            best_sim = 0.0
            best_id: str | None = None
            for item in candidates:
                if item.topic_id == topic_id:
                    continue
                try:
                    sim = cosine(embedding, item.embedding)
                except ValueError:
                    # dimensionality mismatch (model change) — skip that vector
                    continue
                if sim > best_sim:
                    best_sim, best_id = sim, item.topic_id

            if best_id is not None and best_sim >= self.threshold:
                return DedupDecision(True, best_id, best_sim, 2, "skipped")
            if best_id is not None and best_sim >= YELLOW_FLOOR:
                return DedupDecision(False, best_id, best_sim, 2, "yellow")
            return DedupDecision(False, best_id, best_sim, 0, None)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("dedup.check_failed", topic_id=topic_id, err=str(exc))
            return _NOT_DUP

    # --- persistence -------------------------------------------------------

    def remember(self, topic_id: str, title: str, embedding: np.ndarray) -> None:
        """Mark a candidate canonical: in-memory (so later candidates in this
        pass see it) + persisted (so sibling sources / later runs see it)."""
        title_toks = normalize_title(title)
        self._run_kept.append(
            _WindowItem(topic_id, np.asarray(embedding, dtype=np.float32), title_toks)
        )
        try:
            from pipeline.admin.db import session_scope  # noqa: PLC0415
            from pipeline.admin.models import TopicEmbedding  # noqa: PLC0415

            with session_scope() as session:
                session.add(
                    TopicEmbedding(
                        topic_id=topic_id,
                        brand_id_fk=self.brand_id_fk,
                        embedding=np.asarray(embedding, dtype=np.float32).tobytes(),
                        model=self.model,
                        title_norm=" ".join(sorted(title_toks)),
                        created_at=datetime.now(tz=timezone.utc),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("dedup.remember_failed", topic_id=topic_id, err=str(exc))

    def record(self, topic_id: str, decision: DedupDecision) -> None:
        """Write a dedup_log row for a skipped/yellow decision. Best-effort."""
        if decision.action not in ("skipped", "yellow"):
            return
        try:
            from pipeline.admin.db import session_scope  # noqa: PLC0415
            from pipeline.admin.models import DedupLog  # noqa: PLC0415

            with session_scope() as session:
                session.add(
                    DedupLog(
                        run_id=self.run_id,
                        topic_id=topic_id,
                        matched_topic_id=decision.matched_topic_id,
                        similarity=float(decision.similarity),
                        level=decision.level or 2,
                        action=decision.action,
                        created_at=datetime.now(tz=timezone.utc),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — fail open
            log.warning("dedup.log_failed", topic_id=topic_id, err=str(exc))


def cleanup_old_embeddings(brand_id_fk: int, window_days: int) -> int:
    """Delete ``topic_embeddings`` older than the window. Returns rows removed.

    Called on pipeline start. Best-effort — a cleanup failure never blocks a run.
    """
    try:
        from pipeline.admin.db import session_scope  # noqa: PLC0415
        from pipeline.admin.models import TopicEmbedding  # noqa: PLC0415

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
        with session_scope() as session:
            result = session.execute(
                delete(TopicEmbedding).where(
                    TopicEmbedding.brand_id_fk == brand_id_fk,
                    TopicEmbedding.created_at < cutoff,
                )
            )
            removed = result.rowcount or 0
        if removed:
            log.info("dedup.cleanup", removed=removed, days=window_days)
        return removed
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("dedup.cleanup_failed", err=str(exc))
        return 0
