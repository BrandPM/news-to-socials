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

    def with_topic(self, topic_id: int | None) -> "CostContext":
        return replace(self, topic_id=topic_id)

    def with_draft(self, draft_id: str | None) -> "CostContext":
        return replace(self, draft_id=draft_id)


_current: contextvars.ContextVar[CostContext | None] = contextvars.ContextVar(
    "cost_recorder_context", default=None
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


def record_cost(
    *,
    provider: str,
    operation: str,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_seconds: float | None = None,
    cost_usd: float,
) -> None:
    """Persist one row in ``cost_records`` using the active context.

    No-op when no context is set OR brand_id_fk is None (e.g. a test
    that exercises CommentWriter without seeding a brand). The
    ``cost_usd`` argument is required even if 0.0 — the row exists so
    we can audit operation counts even when pricing is unknown.
    """
    ctx = get_context()
    if ctx.brand_id_fk is None:
        return
    # Lazy import — keeps the cost_recorder module importable from pipeline
    # code that may run before AdminConfigClient's import side effects.
    from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415

    try:
        AdminConfigClient.record_cost(
            brand_id_fk=ctx.brand_id_fk,
            run_id=ctx.run_id,
            topic_id=ctx.topic_id,
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
