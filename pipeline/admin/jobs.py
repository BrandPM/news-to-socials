"""Background-job helpers for the admin API.

Two kinds of work runs out-of-band from an HTTP request:

* ``kick_off_pipeline_run`` + ``spawn_pipeline_run`` — invoked by
  ``POST /sources/{id}/run`` and ``POST /sources/run-all``. The first creates
  a ``runs`` row immediately so the caller has a polling handle; the second
  launches the actual pipeline as a **detached OS subprocess** so the heavy /
  long / wedged run never shares the admin-API event loop (NTS_074, the lesson
  of NTS_073 where a blocking call in-loop took the whole admin down). The
  worker process is killable (``cancel_run``) and its death is reconciled at
  startup (``sweep_orphaned_runs``).

* ``kick_off_image_regenerate`` — invoked by
  ``POST /drafts/{sanity_id}/regenerate-image``. Pure in-memory job
  registry — no DB row, just a UUID the client polls. Sufficient for
  the single-operator use case.

Tests monkeypatch ``spawn_pipeline_run`` / ``execute_image_regenerate``
to short-circuit external work.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pipeline.admin.db import get_session_factory
from pipeline.admin.models import Run
from pipeline.common.config import get_settings
from pipeline.common.logging import get_logger

log = get_logger(__name__)


# --- Pipeline-run jobs (NTS_074: detached subprocess, off the event loop) --


def kick_off_pipeline_run(
    brand_id_fk: int,
    source_ids: list[int],
    triggered_by: str,
) -> int:
    """Create a 'running' row in ``runs`` and return its id.

    The caller then hands the id to :func:`spawn_pipeline_run` to launch the
    detached worker. Two steps (not one) so the caller gets a polling handle
    even if the spawn itself fails — that failure marks the row 'failed'.
    """
    factory = get_session_factory()
    with factory() as session:
        run = Run(
            brand_id_fk=brand_id_fk,
            triggered_by=triggered_by,
            source_ids=json.dumps(source_ids),
            started_at=datetime.now(tz=timezone.utc),
            status="running",
        )
        session.add(run)
        session.commit()
        return run.id


def _build_run_command(run_id: int) -> list[str]:
    """argv for the detached run-worker. Optionally wrapped in
    ``systemd-run --user --scope`` so the run gets its own cgroup + MemoryMax
    instead of sharing the admin-API unit's limit (NTS_074)."""
    settings = get_settings()
    base = [
        sys.executable,
        "-m",
        "pipeline.run",
        "for-run",
        "--run-id",
        str(run_id),
    ]
    if settings.admin_run_via_systemd_run:
        return [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit=nts-run-{run_id}",
            "-p",
            f"MemoryMax={settings.admin_run_memory_max}",
            *base,
        ]
    return base


def _run_worker_log_target():
    """Open the admin log in append mode so the detached worker's structlog
    stdout lands in the same file ``/runs/{id}/events`` reads. Falls back to
    DEVNULL when the path isn't writable (local dev / CI on Mac)."""
    try:
        path = Path(get_settings().admin_log_path)
        if path.parent.exists():
            return open(path, "a", encoding="utf-8")  # noqa: SIM115
    except OSError:
        pass
    return subprocess.DEVNULL


def spawn_pipeline_run(run_id: int) -> int | None:
    """Launch the pipeline for ``run_id`` as a DETACHED subprocess and record
    its pid on the run row. Returns the pid, or ``None`` if the spawn failed
    (in which case the run row is force-failed).

    ``start_new_session=True`` puts the worker in its own session / process
    group, detached from the API's controlling tty + stdio, so ``cancel_run``
    can signal the whole group cleanly. The fork+exec is cheap and
    non-blocking — safe to call from a sync route (FastAPI threadpool).

    NOTE on restart: under the admin-API systemd unit's default
    ``KillMode=control-group`` a service restart also kills this worker (it
    stays in the unit cgroup despite the new session); the startup
    ``sweep_orphaned_runs`` then force-fails the dead row. For runs that should
    survive a restart AND get their own cgroup/MemoryMax, set
    ``admin_run_via_systemd_run=True`` (wraps the spawn in
    ``systemd-run --user --scope``).
    """
    cmd = _build_run_command(run_id)
    log_target = _run_worker_log_target()
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is built from trusted parts
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_target,
            stderr=log_target,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("run.spawn_failed", run_id=run_id)
        _force_fail_run(run_id, f"spawn failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        # The child dup'd the fd; the parent's copy is no longer needed.
        if log_target not in (subprocess.DEVNULL, None) and hasattr(
            log_target, "close"
        ):
            log_target.close()
    _record_run_pid(run_id, proc.pid)
    log.info("run.spawned", run_id=run_id, pid=proc.pid)
    return proc.pid


def _record_run_pid(run_id: int, pid: int) -> None:
    factory = get_session_factory()
    with factory() as session:
        row = session.get(Run, run_id)
        if row is not None:
            row.pid = pid
            session.commit()


def _force_fail_run(run_id: int, note: str) -> None:
    factory = get_session_factory()
    with factory() as session:
        row = session.get(Run, run_id)
        if row is not None and row.status == "running":
            row.status = "failed"
            row.finished_at = datetime.now(tz=timezone.utc)
            row.log_excerpt = (
                f"{row.log_excerpt}\n{note}" if row.log_excerpt else note
            )
            session.commit()


# --- Cancel (NTS_074 Task 2) ----------------------------------------------

CANCEL_NOTE = "[NTS_074] cancelled by operator"


def _process_alive(pid: int | None) -> bool:
    """True if ``pid`` names a live process. ``os.kill(pid, 0)`` is a
    signal-nothing existence probe."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _terminate_process_group(pid: int) -> None:
    """Best-effort SIGTERM to the worker's process group (start_new_session
    makes pgid == pid). Swallows 'already dead' / permission errors."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def cancel_run(run_id: int) -> str:
    """Idempotently cancel a run. Returns one of:

    * ``"not_found"``        — no such run (route → 404)
    * ``"cancelled"``        — was running, now cancelled (worker killed)
    * ``"already:<status>"`` — already terminal, no-op (route → 200)

    Kills the worker process-group by stored pid BEFORE flipping the row to
    ``cancelled`` + ``finished_at`` (SIGTERM with no handler terminates the
    worker before it can write its own terminal status, so no race). All sync
    + cheap — call from a sync route (threadpool) or a worker thread, never
    the event loop.
    """
    factory = get_session_factory()
    with factory() as session:
        row = session.get(Run, run_id)
        if row is None:
            return "not_found"
        if row.status != "running":
            return f"already:{row.status}"
        if row.pid and row.pid > 0:
            _terminate_process_group(row.pid)
        row.status = "cancelled"
        row.finished_at = datetime.now(tz=timezone.utc)
        row.log_excerpt = (
            f"{row.log_excerpt}\n{CANCEL_NOTE}"
            if row.log_excerpt
            else CANCEL_NOTE
        )
        session.commit()
    log.info("run.cancelled", run_id=run_id)
    return "cancelled"


# --- Orphan sweep (NTS_074 Task 3) ----------------------------------------

# A 'running' row with NULL pid younger than this is presumed to be in the
# brief insert→spawn window, not an orphan — leave it for a later sweep.
ORPHAN_NULL_PID_GRACE_S = 120


def sweep_orphaned_runs(*, now: datetime | None = None) -> int:
    """Force-fail runs stuck in 'running' whose worker process is gone.

    Closes the class of orphans an API/box restart leaves behind (e.g. the
    historical Run #42):

    * pid present + process alive → genuine in-flight run, left alone.
    * pid present + process dead  → orphaned, marked failed immediately.
    * pid NULL + started >grace   → legacy in-process run, or the worker died
      before recording a pid; treated as orphaned. The grace window dodges the
      insert→spawn race for freshly-created rows.

    Marked ``failed`` (not ``cancelled``) to match the time-based
    :func:`close_stale_runs` backstop — both close zombies the same way; a
    deliberate operator stop is the only thing that yields ``cancelled``.
    Idempotent. Runs on the scheduler thread (off the event loop), same place
    as ``close_stale_runs``; the 6h ``close_stale_runs`` stays as a backstop
    for pid-reuse false-negatives.
    """
    now = now or datetime.now(tz=timezone.utc)
    factory = get_session_factory()
    closed = 0
    with factory() as session:
        running = session.scalars(
            select(Run).where(Run.status == "running")
        ).all()
        for run in running:
            if run.pid and _process_alive(run.pid):
                continue  # alive worker — real run in flight
            if run.pid is None:
                age = (now - _as_utc(run.started_at)).total_seconds()
                if age < ORPHAN_NULL_PID_GRACE_S:
                    continue  # too young — likely mid-spawn, not an orphan
            run.status = "failed"
            run.finished_at = now
            note = (
                "[NTS_074 orphan-sweep] marked failed — orphaned by restart "
                f"(worker pid {run.pid} not alive)"
            )
            run.log_excerpt = (
                f"{run.log_excerpt}\n{note}" if run.log_excerpt else note
            )
            closed += 1
        if closed:
            session.commit()
    return closed


# --- Stale-run cleanup (NTS_056 Task 3) -----------------------------------

# A run stuck in 'running' past this many hours is presumed dead — the
# worker crashed or the box rebooted mid-fanout (e.g. NTS_055 runs #13/#21).
# Lowered 24h -> 6h in NTS_058: when the event loop wedged and the unit was
# restarted, the in-flight Run row stayed 'running' and lingered in the Active
# panel for a full day. A real run never legitimately exceeds a few hours, so
# 6h closes zombies promptly while leaving genuine long fan-outs alone.
STALE_RUN_MAX_AGE_HOURS = 6


def _as_utc(dt: datetime) -> datetime:
    """Treat naive timestamps (SQLite DateTime) as UTC for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def close_stale_runs(
    *,
    max_age_hours: int = STALE_RUN_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> int:
    """Mark runs stuck in 'running' beyond ``max_age_hours`` as failed.

    Idempotent and safe to call on a schedule: only touches rows whose
    ``status='running'`` AND ``started_at`` is older than the cutoff. Each
    closure is appended to ``run.log_excerpt`` so the audit trail records
    why the run was force-failed. Returns the number of runs closed.
    """
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    factory = get_session_factory()
    closed = 0
    with factory() as session:
        running = session.scalars(
            select(Run).where(Run.status == "running")
        ).all()
        for run in running:
            if _as_utc(run.started_at) >= cutoff:
                continue  # still within the grace window — leave it alone
            run.status = "failed"
            run.finished_at = now
            note = (
                f"[NTS_056 cleanup] marked failed — stuck running "
                f">{max_age_hours}h (since {run.started_at})"
            )
            run.log_excerpt = (
                f"{run.log_excerpt}\n{note}" if run.log_excerpt else note
            )
            closed += 1
        if closed:
            session.commit()
    return closed


# --- Image-regenerate jobs ------------------------------------------------


@dataclass
class ImageJob:
    job_id: str
    state: str = "pending"  # 'pending' | 'done' | 'error'
    asset_id: str | None = None
    error: str | None = None


_IMAGE_JOBS: dict[str, ImageJob] = {}
_IMAGE_JOBS_LOCK = threading.Lock()


def register_image_job() -> ImageJob:
    job = ImageJob(job_id=uuid.uuid4().hex)
    with _IMAGE_JOBS_LOCK:
        _IMAGE_JOBS[job.job_id] = job
    return job


def get_image_job(job_id: str) -> ImageJob | None:
    with _IMAGE_JOBS_LOCK:
        return _IMAGE_JOBS.get(job_id)


def _set_image_job(job_id: str, **fields: Any) -> None:
    with _IMAGE_JOBS_LOCK:
        job = _IMAGE_JOBS.get(job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)


async def execute_image_regenerate(
    job_id: str,
    sanity_draft_id: str,
    custom_prompt: str | None = None,
) -> None:
    """Regenerate the cover image for a Sanity draft and patch its
    ``coverImage`` reference. Updates the in-memory job entry as it
    progresses. Imported lazily so a unit test of the dispatcher
    doesn't have to mock the whole image stack.
    """
    from pipeline.admin.image_regenerate import regenerate_cover_image  # noqa: PLC0415

    try:
        asset_id = await regenerate_cover_image(sanity_draft_id, custom_prompt)
        _set_image_job(job_id, state="done", asset_id=asset_id)
    except Exception as exc:  # noqa: BLE001
        _set_image_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")


def reset_image_jobs_for_tests() -> None:
    with _IMAGE_JOBS_LOCK:
        _IMAGE_JOBS.clear()


# --- Text-regenerate jobs (S5 Step 7) -------------------------------------


@dataclass
class TextJob:
    job_id: str
    state: str = "pending"  # 'pending' | 'done' | 'error'
    error: str | None = None


_TEXT_JOBS: dict[str, TextJob] = {}
_TEXT_JOBS_LOCK = threading.Lock()


def register_text_job() -> TextJob:
    job = TextJob(job_id=uuid.uuid4().hex)
    with _TEXT_JOBS_LOCK:
        _TEXT_JOBS[job.job_id] = job
    return job


def get_text_job(job_id: str) -> TextJob | None:
    with _TEXT_JOBS_LOCK:
        return _TEXT_JOBS.get(job_id)


def _set_text_job(job_id: str, **fields: Any) -> None:
    with _TEXT_JOBS_LOCK:
        job = _TEXT_JOBS.get(job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)


async def execute_text_regenerate(
    job_id: str,
    sanity_draft_id: str,
    brand_id_fk: int,
) -> None:
    from pipeline.admin.text_regenerate import regenerate_draft_text  # noqa: PLC0415

    try:
        await regenerate_draft_text(sanity_draft_id, brand_id_fk)
        _set_text_job(job_id, state="done")
    except Exception as exc:  # noqa: BLE001
        _set_text_job(job_id, state="error", error=f"{type(exc).__name__}: {exc}")


def reset_text_jobs_for_tests() -> None:
    with _TEXT_JOBS_LOCK:
        _TEXT_JOBS.clear()
