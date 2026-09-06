"""The daily passes over the portfolio: TTL, timeout, retention (NTS_098 §2, NTS_100 §6).

Four config keys sat in Settings with **no reader in the code** until this
module existed (NTS_121 §2): ``candidate_ttl_days`` had one for writing
``expires_at`` but none for acting on it, ``production_timeout_min`` and
``retention_days_rejected`` had neither. A TTL that is written and never
enforced is worse than none — the board shows candidates as live that the
policy says are dead, and ``expired`` never appears in the weekly ratio that is
supposed to tell the operator their caps are mistuned.

Three passes, each idempotent and each safe to run twice in a minute:

* **expire** — ``pending``/``doc_missing``/``selected`` past ``expires_at``.
  ``drafted``/``returned``/``ready`` are deliberately exempt: an article the
  editor is holding is the editor's to release, and a TTL that deleted their
  queue overnight would be indistinguishable from data loss.
* **timeout** — ``in_production`` older than ``production_timeout_min`` goes
  back to ``pending`` with ``attempts+1``, or to ``failed`` once attempts reach
  ``max_attempts``. This is what makes a crashed production run recoverable
  without a human: the candidate is stuck in a status nothing else will pick up.
* **prune** — ``rejected`` past ``retention_days_rejected``, and
  ``expired``/``failed``/``superseded`` past ``RETENTION_DAYS_TERMINAL``.
  ``published`` is never pruned.

The prune deliberately **skips any candidate a human decided on**: the FK from
``review_decisions`` is RESTRICT, because that table is the only dataset for
tuning the rubric (NTS_113) and a retention job that silently ate it would be a
slow, unnoticeable loss. Those rows stay, and the pass says how many it left.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# NTS_098 §2: "expired/failed/superseded — 90". Not a config key on purpose —
# it is a storage-hygiene number, and every knob in Settings is one more thing
# that can be set to a value nobody meant.
RETENTION_DAYS_TERMINAL = 90

# NTS_098 §2 — TTL applies to these three only.
EXPIRABLE_STATUSES: tuple[str, ...] = ("pending", "doc_missing", "selected")


def _session_factory() -> Any:
    from pipeline.admin.db import get_session_factory

    return get_session_factory()


def expire_stale_candidates(
    *, brand_id_fk: int, now: datetime | None = None
) -> int:
    """``pending``/``doc_missing``/``selected`` past ``expires_at`` → ``expired``."""
    from sqlalchemy import update

    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    with _session_factory()() as session:
        result = session.execute(
            update(Candidate)
            .where(
                Candidate.brand_id_fk == brand_id_fk,
                Candidate.status.in_(EXPIRABLE_STATUSES),
                Candidate.expires_at.is_not(None),
                Candidate.expires_at < now,
            )
            .values(status="expired")
        )
        session.commit()
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]
    if count:
        log.info("portfolio_sweep.expired", brand_id=brand_id_fk, count=count)
    return count


def sweep_production_timeouts(
    *,
    brand_id_fk: int,
    timeout_minutes: int,
    max_attempts: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """``in_production`` stuck past the timeout → ``pending`` or ``failed``.

    Returns ``{"released": n, "failed": m}``. The clock is ``selected_at``:
    production claims and starts within the same call, so the two stamps are
    seconds apart, and a separate ``production_started_at`` column would be a
    second source of truth for one instant.
    """
    from sqlalchemy import select

    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    cutoff = now - timedelta(minutes=max(1, int(timeout_minutes)))
    released = 0
    failed = 0
    with _session_factory()() as session:
        rows = (
            session.execute(
                select(Candidate).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.status == "in_production",
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            started = row.selected_at
            if started is None:
                # No stamp means the row predates the production path. Treat
                # the timeout as expired rather than leaving it stuck forever:
                # ``in_production`` is not a status anything else picks up.
                started = cutoff - timedelta(minutes=1)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if started > cutoff:
                continue
            row.attempts = int(row.attempts or 0) + 1
            row.last_error = (
                f"production timed out after {timeout_minutes} min "
                f"(attempt {row.attempts})"
            )
            if row.attempts >= max_attempts:
                row.status = "failed"
                row.failed_at = now
                failed += 1
            else:
                row.status = "pending"
                released += 1
        session.commit()
    if released or failed:
        log.warning(
            "portfolio_sweep.timed_out",
            brand_id=brand_id_fk,
            released=released,
            failed=failed,
            timeout_minutes=timeout_minutes,
        )
    return {"released": released, "failed": failed}


def prune_old_candidates(
    *,
    brand_id_fk: int,
    retention_days_rejected: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete aged-out terminal candidates. Returns counts per reason.

    ``{"rejected": n, "terminal": m, "kept_with_decisions": k}``. The third
    number is the point: rows a human ruled on are kept regardless of age, and
    a prune that quietly did otherwise would be indistinguishable from working.
    """
    from sqlalchemy import select

    from pipeline.admin.models import Candidate, ReviewDecision

    now = now or datetime.now(tz=UTC)
    rejected_cutoff = now - timedelta(days=max(1, int(retention_days_rejected)))
    terminal_cutoff = now - timedelta(days=RETENTION_DAYS_TERMINAL)
    counts = {"rejected": 0, "terminal": 0, "kept_with_decisions": 0}

    with _session_factory()() as session:
        decided = set(
            session.execute(select(ReviewDecision.candidate_id_fk)).scalars().all()
        )
        rows = (
            session.execute(
                select(Candidate).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.status.in_(
                        ("rejected", "expired", "failed", "superseded")
                    ),
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            created = row.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created is None:
                continue
            is_rejected = row.status == "rejected"
            cutoff = rejected_cutoff if is_rejected else terminal_cutoff
            if created >= cutoff:
                continue
            if row.id in decided:
                counts["kept_with_decisions"] += 1
                continue
            # ``supersedes_id`` is ON DELETE SET NULL and the two audit tables
            # that point here are SET NULL as well, so the delete is safe once
            # the RESTRICT side (review_decisions) is excluded above.
            session.delete(row)
            counts["rejected" if is_rejected else "terminal"] += 1
        session.commit()
    if any(counts.values()):
        log.info("portfolio_sweep.pruned", brand_id=brand_id_fk, **counts)
    return counts


def release_to_pending(
    *,
    candidate_id: int,
    error: str,
    max_attempts: int,
    now: datetime | None = None,
) -> str:
    """A production failure: back to ``pending`` (or ``failed``) with the reason.

    NTS_100 §4 — "Любое исключение в контуре 2 → откат к ``pending``,
    ``attempts+1``, ошибка в ``candidates.last_error``". Returns the status the
    candidate ended in, so the caller can alert on ``failed`` without a second
    read. Partially written artefacts (the fact pack, the document cache) are
    deliberately **not** cleaned up: the retry is supposed to reuse them rather
    than pay for research twice.
    """
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    with _session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is None:
            return "missing"
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = error[:2000]
        if row.attempts >= max_attempts:
            row.status = "failed"
            row.failed_at = now
        else:
            row.status = "pending"
        status = str(row.status)
        session.commit()
    log.warning(
        "portfolio_sweep.production_failed",
        candidate_id=candidate_id,
        status=status,
        err=error[:200],
    )
    return status


def run_sweep(
    brand_slug: str = "icon",
    *,
    brand_id: int | None = None,
    triggered_by: str = "cron",
    now: datetime | None = None,
) -> dict[str, int]:
    """All three passes, recorded as a ``run_type='ttl'`` run (NTS_100 §6).

    The production run does this too, at its own start — but production runs
    twice a week (NTS_123) and the spec says the passes are daily. Splitting
    them means a candidate never sits four days past its TTL waiting for
    Wednesday, and the ``ttl`` run row is the evidence that the pass happened
    at all on the days production did not.
    """
    from pipeline.admin.config_client import AdminConfigClient, get_brand

    now = now or datetime.now(tz=UTC)
    brand_row = get_brand(brand_id if brand_id is not None else brand_slug)
    client = AdminConfigClient(brand_slug=brand_row.slug)
    config = client.get_config()
    run_id = client.record_run_start(
        source_ids=[], triggered_by=triggered_by, run_type="ttl"
    )

    expired = expire_stale_candidates(brand_id_fk=brand_row.id, now=now)
    swept = sweep_production_timeouts(
        brand_id_fk=brand_row.id,
        timeout_minutes=int(getattr(config, "production_timeout_min", 60)),
        max_attempts=int(getattr(config, "max_attempts", 2)),
        now=now,
    )
    pruned = prune_old_candidates(
        brand_id_fk=brand_row.id,
        retention_days_rejected=int(getattr(config, "retention_days_rejected", 30)),
        now=now,
    )
    stats = {
        "expired": expired,
        "released": swept["released"],
        "failed": swept["failed"],
        "pruned_rejected": pruned["rejected"],
        "pruned_terminal": pruned["terminal"],
        "kept_with_decisions": pruned["kept_with_decisions"],
    }
    client.record_run_finish(
        run_id,
        status="success",
        stats=stats,
        log_excerpt=" · ".join(f"{k}={v}" for k, v in stats.items()),
    )
    log.info("portfolio_sweep.done", brand=brand_row.slug, run_id=run_id, **stats)
    return stats


def main() -> None:  # pragma: no cover — thin CLI wrapper
    """``python -m pipeline.selector.portfolio_sweep [brand_slug]``."""
    import json
    import sys

    from pipeline.common.logging import configure_logging

    configure_logging()
    brand = sys.argv[1] if len(sys.argv) > 1 else "icon"
    print(json.dumps(run_sweep(brand), indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
