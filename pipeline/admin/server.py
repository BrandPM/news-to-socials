"""FastAPI app for the News-to-Socials admin UI (IT_PROJ_NTS_014).

Runs on the VPS at 127.0.0.1:8080 behind Caddy reverse proxy in S3. For
local dev: ``uvicorn pipeline.admin.server:app --reload``.

Endpoints (all under ``/api/v1`` except ``/health``):

* ``GET /health``       — unauthenticated liveness probe
* ``/api/v1/sources``   — CRUD + test + run
* ``/api/v1/prompts``   — CRUD + activate + test
* ``/api/v1/config``    — GET + PUT
* ``/api/v1/runs``      — list + detail + log
* ``/api/v1/drafts/*``  — image regenerate (async via BackgroundTasks)

Every route except ``/health`` requires ``X-Admin-Token`` (see
``pipeline/admin/auth.py``). Routers are mounted via include_router with a
shared dependency so we cannot forget to authenticate a new route.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pipeline.common.config import get_settings

from . import jobs, watchdog
from .auth import require_admin_token

logger = logging.getLogger(__name__)
from .routes import brands as brands_routes
from .routes import config as config_routes
from .routes import cost as cost_routes
from .routes import dashboard as dashboard_routes
from .routes import drafts as drafts_routes
from .routes import health as health_routes
from .routes import prompts as prompts_routes
from .routes import runs as runs_routes
from .routes import sources as sources_routes
from .routes import notifications as notifications_routes
from .routes import topics as topics_routes


def _pkg_version() -> str:
    try:
        return version("news-to-socials")
    except PackageNotFoundError:
        return "0.0.0+local"


def _build_lifespan(settings):
    """Lifespan that runs the hourly stale-run cleanup (NTS_056 Task 3).

    CRITICAL (NTS_058 hotfix): the cleanup does **synchronous SQLite I/O**,
    which must never run on the asyncio event loop — a blocked sync DB call
    (lock contention, busy-wait) would freeze the whole API and time out
    ``/health``. So:

      * The sweep runs **only** inside APScheduler's ``BackgroundScheduler``,
        which executes jobs in its own thread pool — off the event loop.
      * There is **no** synchronous sweep call in the async startup path.
        The first sweep is scheduled a few seconds out (still on the
        scheduler thread) so a freshly-booted box clears orphaned runs
        without blocking startup.
      * The entire scheduler setup is wrapped so a scheduler failure can
        never take the API down.

    Disabled when ``admin_run_scheduler`` is False (the test suite) so no
    scheduler thread leaks across tests.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        # NTS_058 Task 2: feed the systemd watchdog from the event loop. If the
        # loop wedges (the original incident), the pings stop and systemd
        # restarts the unit. No-ops when not running under systemd.
        watchdog_task = watchdog.start_watchdog()
        if settings.admin_run_scheduler:
            try:
                from apscheduler.schedulers.background import (  # noqa: PLC0415
                    BackgroundScheduler,
                )

                max_age = settings.stale_run_max_age_hours

                def _cleanup() -> None:
                    # Runs on a BackgroundScheduler worker thread — NEVER the
                    # event loop. Swallows everything so a bad sweep can't
                    # crash the scheduler.
                    #
                    # NTS_074: the pid-based orphan sweep runs FIRST so a
                    # restart-orphaned run (dead worker pid) is force-failed
                    # promptly — the ~15s-after-boot first tick closes the
                    # Run #42 class without blocking startup. close_stale_runs
                    # stays as the 6h time-based backstop (pid-reuse cases).
                    try:
                        orphaned = jobs.sweep_orphaned_runs()
                        if orphaned:
                            logger.info(
                                "orphan-sweep force-failed %d run(s)", orphaned
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception("orphan-sweep failed")
                    try:
                        closed = jobs.close_stale_runs(max_age_hours=max_age)
                        if closed:
                            logger.info("stale-run cleanup closed %d run(s)", closed)
                    except Exception:  # noqa: BLE001
                        logger.exception("stale-run cleanup failed")

                scheduler = BackgroundScheduler(timezone="UTC")
                scheduler.add_job(
                    _cleanup,
                    trigger="interval",
                    hours=1,
                    id="close_stale_runs",
                    # First sweep ~15s after boot, on the scheduler thread —
                    # clears reboot-orphaned runs without blocking startup.
                    next_run_time=datetime.now(timezone.utc) + timedelta(seconds=15),
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )
                scheduler.start()
            except Exception:  # noqa: BLE001 — scheduler must never block boot
                logger.exception("stale-run scheduler failed to start; continuing")
                scheduler = None
        try:
            yield
        finally:
            if watchdog_task is not None:
                watchdog_task.cancel()
            if scheduler is not None:
                try:
                    scheduler.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    logger.exception("scheduler shutdown failed")

    return lifespan


def create_app() -> FastAPI:
    """Factory so tests can spin up a fresh app instance."""
    settings = get_settings()
    app = FastAPI(
        title="news-to-socials admin API",
        version=_pkg_version(),
        # Hide schema by default — anyone who needs it can ssh to the VPS
        # and hit it locally. S3 may flip these on after Caddy is fronted
        # with Cloudflare Access.
        docs_url="/docs",
        redoc_url=None,
        lifespan=_build_lifespan(settings),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.admin_cors_origin, "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["public"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": _pkg_version()}

    auth = [Depends(require_admin_token)]
    app.include_router(
        brands_routes.router, prefix="/api/v1/brands",
        tags=["brands"], dependencies=auth,
    )
    app.include_router(
        sources_routes.router, prefix="/api/v1/sources",
        tags=["sources"], dependencies=auth,
    )
    app.include_router(
        prompts_routes.router, prefix="/api/v1/prompts",
        tags=["prompts"], dependencies=auth,
    )
    app.include_router(
        config_routes.router, prefix="/api/v1/config",
        tags=["config"], dependencies=auth,
    )
    app.include_router(
        runs_routes.router, prefix="/api/v1/runs",
        tags=["runs"], dependencies=auth,
    )
    app.include_router(
        drafts_routes.router, prefix="/api/v1/drafts",
        tags=["drafts"], dependencies=auth,
    )
    app.include_router(
        cost_routes.router, prefix="/api/v1/cost",
        tags=["cost"], dependencies=auth,
    )
    app.include_router(
        dashboard_routes.router, prefix="/api/v1/dashboard",
        tags=["dashboard"], dependencies=auth,
    )
    app.include_router(
        topics_routes.router, prefix="/api/v1/topics",
        tags=["topics"], dependencies=auth,
    )
    app.include_router(
        notifications_routes.router, prefix="/api/v1/notifications",
        tags=["notifications"], dependencies=auth,
    )
    app.include_router(
        health_routes.router, prefix="/api/v1/health",
        tags=["health"], dependencies=auth,
    )
    return app


app = create_app()
