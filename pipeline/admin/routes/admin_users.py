"""``/api/v1/admin`` route group — multi-user admin (NTS_058).

Flat model: every active user has identical full rights. No roles/scopes.

* GET    /admin/users                      → list (never returns password_hash)
* POST   /admin/users                      → create (username+password)
* DELETE /admin/users/{id}                 → soft-delete (is_active=false);
                                             refuses the seed user id=1 (403)
* POST   /admin/users/{id}/reset-password  → set a new password
* POST   /admin/auth/verify                → login check (bcrypt), rate-limited

All routes sit behind the shared ``X-Admin-Token`` dependency (mounted in
server.py). ``/auth/verify`` is called by the NextAuth ``authorize()``
callback server-side, which holds the admin token — so login brute-force
from the public internet can't even reach it. The per-IP rate limit is
defense-in-depth on top of that.

SEED PROTECTION: the seed user (id=1, Andriy) can never be deactivated —
that's the lock-out backstop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pipeline.admin.db import session_scope
from pipeline.admin.models import AdminUser
from pipeline.admin.passwords import (
    ValidationError,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from pipeline.admin.rate_limit import login_verify_limiter
from pipeline.admin.schemas import (
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserResetPasswordIn,
    AuthVerifyIn,
    AuthVerifyOut,
)

logger = logging.getLogger("pipeline.admin.auth_events")

SEED_USER_ID = 1

router = APIRouter()


# --- User CRUD ----------------------------------------------------------


@router.get("/users", response_model=list[AdminUserOut])
def list_users() -> list[AdminUserOut]:
    with session_scope() as session:
        rows = session.scalars(select(AdminUser).order_by(AdminUser.id)).all()
        return [AdminUserOut.model_validate(r) for r in rows]


@router.post(
    "/users", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED
)
def create_user(payload: AdminUserCreateIn) -> AdminUserOut:
    try:
        username = validate_username(payload.username)
        validate_password(payload.password)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with session_scope() as session:
        user = AdminUser(
            username=username,
            password_hash=hash_password(payload.password),
            created_at=datetime.now(tz=timezone.utc),
            is_active=True,
        )
        session.add(user)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"username {username!r} is already taken",
            ) from exc
        return AdminUserOut.model_validate(user)


@router.delete("/users/{user_id}", response_model=AdminUserOut)
def delete_user(user_id: int) -> AdminUserOut:
    """Soft-delete (is_active=false). Refuses the seed user (403)."""
    if user_id == SEED_USER_ID:
        raise HTTPException(
            status_code=403,
            detail="the seed user cannot be disabled (lock-out protection)",
        )
    with session_scope() as session:
        user = session.get(AdminUser, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.is_active = False
        session.flush()
        return AdminUserOut.model_validate(user)


@router.post("/users/{user_id}/reset-password", response_model=AdminUserOut)
def reset_password(
    user_id: int, payload: AdminUserResetPasswordIn
) -> AdminUserOut:
    try:
        validate_password(payload.password)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with session_scope() as session:
        user = session.get(AdminUser, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.password_hash = hash_password(payload.password)
        session.flush()
        return AdminUserOut.model_validate(user)


# --- Login verify (called by NextAuth authorize) ------------------------


@router.post("/auth/verify", response_model=AuthVerifyOut)
def verify_login(payload: AuthVerifyIn, request: Request) -> AuthVerifyOut:
    """Verify username+password. 401 on bad creds, 429 when rate-limited.

    NEVER logs the password — only username + client IP + outcome.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not login_verify_limiter.allow(client_ip):
        logger.warning("auth.verify rate-limited ip=%s", client_ip)
        raise HTTPException(
            status_code=429, detail="too many login attempts; slow down"
        )

    # Username is validated leniently here (we only look it up); invalid
    # shapes simply won't match a row → 401, no info leak.
    username = (payload.username or "").strip().lower()

    with session_scope() as session:
        user = session.scalar(
            select(AdminUser).where(
                AdminUser.username == username, AdminUser.is_active.is_(True)
            )
        )
        ok = user is not None and verify_password(payload.password, user.password_hash)
        if not ok:
            logger.info(
                "auth.verify FAIL user=%r ip=%s", username, client_ip
            )
            raise HTTPException(status_code=401, detail="invalid credentials")
        user.last_login_at = datetime.now(tz=timezone.utc)
        result = AuthVerifyOut(user_id=user.id, username=user.username)
        session.flush()

    logger.info("auth.verify OK user=%r ip=%s", username, client_ip)
    return result
