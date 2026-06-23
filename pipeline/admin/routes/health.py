"""``/api/v1/health/deep`` — deep health probe (IT_PROJ_NTS_073).

Unlike the unauthenticated ``/health`` liveness probe (just "is the process
up"), this checks the *dependencies* the pipeline needs to actually work:

* ``db``      — ``SELECT 1`` against admin.db.
* ``sanity``  — a light GROQ ``count()`` against the CMS, with a short
  timeout. ``unconfigured`` when no brand has Sanity creds.
* ``last_successful_run_age_min`` — minutes since the most recent
  ``status='success'`` run (None if never).
* ``last_run_*`` — when the pipeline last *started* a run, and its status
  (the "last nts-run launch").

CRITICAL (incident 869duwx02): **nothing blocking on the event loop.** The
Sanity call is async ``httpx`` with a hard timeout and no retry. The SQLite
reads are trivial but still hopped off the loop via ``asyncio.to_thread`` so
a lock-contended DB can never wedge the API the way the original incident
did.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter
from sqlalchemy import select, text

from pipeline.admin.db import session_scope
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand, Run
from pipeline.common.config import get_settings
from pipeline.common.logging import get_logger

log = get_logger(__name__)

router = APIRouter()

# A successful run older than this flips the service to "degraded".
LAST_SUCCESS_DEGRADED_MIN = 24 * 60
# Hard ceiling on the Sanity probe — never let a slow CMS hold the loop.
SANITY_TIMEOUT_S = 5.0


def _age_min(then: datetime | None, now: datetime) -> int | None:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((now - then).total_seconds() // 60))


def _db_and_runs() -> dict:
    """Sync DB reads (run off the event loop via ``asyncio.to_thread``)."""
    now = datetime.now(tz=timezone.utc)
    out: dict = {
        "db": "error: unreachable",
        "last_successful_run_age_min": None,
        "last_run_age_min": None,
        "last_run_status": None,
    }
    with session_scope() as session:
        session.execute(text("SELECT 1"))
        out["db"] = "ok"

        last_success = (
            session.execute(
                select(Run.started_at)
                .where(Run.status == "success")
                .order_by(Run.started_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        out["last_successful_run_age_min"] = _age_min(last_success, now)

        last_run = session.execute(
            select(Run.started_at, Run.status)
            .order_by(Run.started_at.desc())
            .limit(1)
        ).first()
        if last_run is not None:
            out["last_run_age_min"] = _age_min(last_run[0], now)
            out["last_run_status"] = last_run[1]
    return out


def _sanity_creds() -> dict | None:
    """Pick Sanity creds: global settings first, else the first active brand.

    Returns ``{project_id, dataset, api_version, token}`` or ``None`` when
    nothing is configured. Token decryption happens here so the async caller
    stays free of DB/crypto work.
    """
    settings = get_settings()
    if settings.sanity_project_id and settings.sanity_api_token:
        return {
            "project_id": settings.sanity_project_id,
            "dataset": settings.sanity_dataset or "production",
            "api_version": settings.sanity_api_version or "2024-01-01",
            "token": settings.sanity_api_token,
        }
    with session_scope() as session:
        brand = (
            session.execute(
                select(Brand)
                .where(
                    Brand.active.is_(True),
                    Brand.sanity_project_id.is_not(None),
                    Brand.sanity_api_token_enc.is_not(None),
                )
                .limit(1)
            )
            .scalars()
            .first()
        )
        if brand is None:
            return None
        token = get_encryption().decrypt_or_none(brand.sanity_api_token_enc)
        if not token:
            return None
        return {
            "project_id": brand.sanity_project_id,
            "dataset": brand.sanity_dataset or "production",
            "api_version": brand.sanity_api_version or "2024-01-01",
            "token": token,
        }


async def _check_sanity() -> str:
    """Light GROQ ``count()`` with a hard timeout. Never raises."""
    try:
        creds = await asyncio.to_thread(_sanity_creds)
    except Exception:  # noqa: BLE001
        log.exception("health.sanity_creds_failed")
        return "error: creds"
    if creds is None:
        return "unconfigured"

    url = (
        f"https://{creds['project_id']}.api.sanity.io"
        f"/v{creds['api_version']}/data/query/{creds['dataset']}"
    )
    try:
        async with httpx.AsyncClient(timeout=SANITY_TIMEOUT_S) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {creds['token']}"},
                json={"query": 'count(*[_type == "post"])'},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"error: http_{exc.response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"
    return "ok"


@router.get("/deep")
async def health_deep() -> dict:
    try:
        components = await asyncio.to_thread(_db_and_runs)
    except Exception as exc:  # noqa: BLE001
        log.exception("health.db_failed")
        components = {
            "db": f"error: {type(exc).__name__}",
            "last_successful_run_age_min": None,
            "last_run_age_min": None,
            "last_run_status": None,
        }

    components["sanity"] = await _check_sanity()

    if components["db"] != "ok":
        status = "down"
    else:
        degraded = False
        if not str(components["sanity"]).startswith(("ok", "unconfigured")):
            degraded = True
        age = components["last_successful_run_age_min"]
        if age is None or age > LAST_SUCCESS_DEGRADED_MIN:
            degraded = True
        status = "degraded" if degraded else "ok"

    return {"status": status, "components": components}
