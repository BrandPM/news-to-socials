"""Background-job helpers for the admin API.

Two kinds of work runs out-of-band from an HTTP request:

* ``kick_off_pipeline_run`` — invoked by ``POST /sources/{id}/run`` and
  ``POST /sources/run-all``. Creates a ``runs`` row immediately so the
  caller has a polling handle, then runs the actual pipeline in a
  ``BackgroundTasks`` task that updates the row when done.

* ``kick_off_image_regenerate`` — invoked by
  ``POST /drafts/{sanity_id}/regenerate-image``. Pure in-memory job
  registry — no DB row, just a UUID the client polls. Sufficient for
  the single-operator use case.

Tests monkeypatch ``execute_pipeline_run`` / ``execute_image_regenerate``
to short-circuit external work.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from pipeline.admin.db import get_session_factory
from pipeline.admin.models import Run, Source


# --- Pipeline-run jobs ----------------------------------------------------


def kick_off_pipeline_run(
    brand_id_fk: int,
    source_ids: list[int],
    triggered_by: str,
) -> int:
    """Create a 'running' row in ``runs`` and return its id.

    The caller is expected to schedule ``execute_pipeline_run(run_id)`` via
    ``BackgroundTasks.add_task`` — we don't do that here because we don't
    want jobs.py to depend on FastAPI.
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


async def execute_pipeline_run(run_id: int) -> None:
    """Run the pipeline for the sources referenced by ``run_id`` and update
    the row when finished. Imported lazily so tests can monkeypatch the
    actual pipeline entry point without paying the import cost.
    """
    from pipeline.run import run_pipeline_for_run  # noqa: PLC0415

    factory = get_session_factory()
    try:
        await run_pipeline_for_run(run_id)
    except Exception as exc:  # noqa: BLE001
        with factory() as session:
            row = session.get(Run, run_id)
            if row is not None:
                row.status = "failed"
                row.finished_at = datetime.now(tz=timezone.utc)
                row.log_excerpt = (row.log_excerpt or "") + f"\nERROR: {exc!r}"
                session.commit()
        raise


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
