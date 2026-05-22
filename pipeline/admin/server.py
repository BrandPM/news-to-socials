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

from importlib.metadata import PackageNotFoundError, version

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pipeline.common.config import get_settings

from .auth import require_admin_token
from .routes import brands as brands_routes
from .routes import config as config_routes
from .routes import cost as cost_routes
from .routes import drafts as drafts_routes
from .routes import prompts as prompts_routes
from .routes import runs as runs_routes
from .routes import sources as sources_routes


def _pkg_version() -> str:
    try:
        return version("news-to-socials")
    except PackageNotFoundError:
        return "0.0.0+local"


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
    return app


app = create_app()
