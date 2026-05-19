"""Lightweight HTTP health endpoint.

Optional — only loaded when the ``api`` extra is installed. Mount as a
systemd service on a private port (e.g. 8080) and poke it from UptimeRobot
or a Hetzner Cloud monitor.
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from fastapi import FastAPI
except ImportError:  # pragma: no cover
    FastAPI = None  # type: ignore

import httpx

from ..common.config import get_settings


def _build() -> "FastAPI":  # type: ignore
    if FastAPI is None:
        raise RuntimeError("Install with the [api] extra to use the health server")

    app = FastAPI(title="news-to-socials health", version="0.0.1")
    started_at = datetime.now(tz=timezone.utc)

    @app.get("/health")
    async def health() -> dict:
        settings = get_settings()
        out: dict = {
            "service": "news-to-socials",
            "uptime_seconds": (datetime.now(tz=timezone.utc) - started_at).total_seconds(),
            "directus": "unknown",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{settings.directus_url}/server/health")
                out["directus"] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
        except Exception as exc:  # noqa: BLE001
            out["directus"] = f"error: {exc}"
        return out

    return app


app = _build() if FastAPI is not None else None  # type: ignore
