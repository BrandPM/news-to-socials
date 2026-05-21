"""X-Admin-Token authentication for the admin API.

All routes except ``/health`` require ``X-Admin-Token`` to match
``settings.admin_trigger_secret``. We compare with :func:`secrets.compare_digest`
to avoid leaking the token byte-by-byte via timing differences.

Admin-UI-Specific Invariant A (NTS_014): "X-Admin-Token validation on EVERY
API endpoint. No public endpoint except /health. Constant-time string
compare via secrets.compare_digest."
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from pipeline.common.config import get_settings

ADMIN_TOKEN_HEADER = "X-Admin-Token"


async def require_admin_token(
    x_admin_token: str | None = Header(default=None, alias=ADMIN_TOKEN_HEADER),
) -> None:
    """FastAPI dependency: raises 401 unless the header matches the secret.

    A missing-or-empty configured secret is also a 401 — we never want
    the API to silently accept any token because someone forgot to set
    ``ADMIN_TRIGGER_SECRET`` in ``.env``.
    """
    expected = (get_settings().admin_trigger_secret or "").strip()
    received = (x_admin_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin API is not configured (ADMIN_TRIGGER_SECRET unset).",
        )
    if not received:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {ADMIN_TOKEN_HEADER} header.",
        )
    if not secrets.compare_digest(expected, received):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token.",
        )
