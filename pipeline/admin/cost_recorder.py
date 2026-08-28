"""Context-var cost recorder used by every paid call site (NTS_025 C1).

Every LLM call / image gen / paid API call records one row in
``cost_records``. To keep callers simple, the brand / run / topic /
draft context is set once at the top of a flow (e.g. ``run_pipeline``)
via the ``cost_context`` context-manager and read implicitly inside
each LLM call site by ``record_cost``.

If no context is set (tests, standalone tools), ``record_cost`` is a
silent no-op — the call site still runs but no DB row is written.
This lets unit tests of CommentWriter / TopicPicker keep mocking LLM
responses without seeding a brand row first.

Async-safe via ``contextvars`` — each asyncio task carries its own
context, so concurrent pipeline runs for different brands don't leak
into each other.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from pipeline.common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CostContext:
    brand_id_fk: int | None = None
    run_id: int | None = None
    topic_id: int | None = None
    draft_id: str | None = None
    # NTS_106 §3 — set by the production path, which knows the candidate before
    # it spends anything. The intake cannot: the guard call that decides whether
    # a candidate should exist happens before the row does, so intake uses
    # :func:`collect_cost_rows` + :func:`attach_candidate` instead.
    candidate_id: int | None = None

    def with_topic(self, topic_id: int | None) -> "CostContext":
        return replace(self, topic_id=topic_id)

    def with_draft(self, draft_id: str | None) -> "CostContext":
        return replace(self, draft_id=draft_id)

    def with_candidate(self, candidate_id: int | None) -> "CostContext":
        return replace(self, candidate_id=candidate_id)


_current: contextvars.ContextVar[CostContext | None] = contextvars.ContextVar(
    "cost_recorder_context", default=None
)

# Ids of rows written inside the innermost :func:`collect_cost_rows` block.
# A contextvar rather than an argument so no call site has to learn about it —
# the guard and the embedder keep their signatures.
_collector: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "cost_recorder_collector", default=None
)


def get_context() -> CostContext:
    """Return the current context, or an empty one if none set."""
    return _current.get() or CostContext()


@contextmanager
def cost_context(ctx: CostContext) -> Iterator[CostContext]:
    """Set the cost context for the duration of the ``with`` block."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextmanager
def collect_cost_rows() -> Iterator[list[int]]:
    """Collect the ids of ``cost_records`` rows written inside the block.

    For the ordering problem the intake has (NTS_106 §3): the guard completion
    and the embedding are paid for while deciding *whether* a candidate should
    exist, so neither can name it. The caller wraps one item's processing in
    this, and once the candidate id is known passes the collected ids to
    :func:`attach_candidate`.

    The yielded list is live — it fills as rows are written.
    """
    rows: list[int] = []
    token = _collector.set(rows)
    try:
        yield rows
    finally:
        _collector.reset(token)


def attach_candidate(row_ids: list[int], candidate_id: int) -> int:
    """Back-fill ``cost_records.candidate_id_fk``. Returns rows updated.

    Never raises: cost accounting must not be able to break an intake, which is
    the same contract :func:`record_cost` keeps.
    """
    if not row_ids or candidate_id is None:
        return 0
    try:
        from sqlalchemy import update

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import CostRecord

        with get_session_factory()() as session:
            result = session.execute(
                update(CostRecord)
                .where(CostRecord.id.in_(list(row_ids)))
                .values(candidate_id_fk=candidate_id)
            )
            session.commit()
            return int(result.rowcount or 0)  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning(
            "cost_recorder.attach_candidate_failed",
            err=str(exc),
            candidate_id=candidate_id,
        )
        return 0


def record_cost(
    *,
    provider: str,
    operation: str,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_seconds: float | None = None,
    cost_usd: float,
) -> int | None:
    """Persist one row in ``cost_records`` using the active context.

    No-op when no context is set OR brand_id_fk is None (e.g. a test
    that exercises CommentWriter without seeding a brand). The
    ``cost_usd`` argument is required even if 0.0 — the row exists so
    we can audit operation counts even when pricing is unknown.

    Returns the inserted row id (``None`` when nothing was written) and
    appends it to the innermost :func:`collect_cost_rows` list, so the intake
    can charge the row to a candidate that did not exist yet at call time.
    """
    ctx = get_context()
    if ctx.brand_id_fk is None:
        return None
    # Lazy import — keeps the cost_recorder module importable from pipeline
    # code that may run before AdminConfigClient's import side effects.
    from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415

    try:
        row_id = AdminConfigClient.record_cost(
            brand_id_fk=ctx.brand_id_fk,
            run_id=ctx.run_id,
            topic_id=ctx.topic_id,
            candidate_id_fk=ctx.candidate_id,
            draft_id=ctx.draft_id,
            provider=provider,
            operation=operation,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=duration_seconds,
            cost_usd=cost_usd,
        )
    except Exception as exc:  # noqa: BLE001
        # Cost recording must never break the pipeline. Log and continue.
        log.warning(
            "cost_recorder.write_failed",
            err=str(exc),
            provider=provider,
            operation=operation,
        )
        return None
    collector = _collector.get()
    if collector is not None and row_id is not None:
        collector.append(row_id)
    return row_id
