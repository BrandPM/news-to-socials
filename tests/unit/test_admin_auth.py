"""Auth middleware tests for the admin API.

We exercise the dependency through a tiny FastAPI app rather than the
real ``pipeline.admin.server`` so the test stays focused on the auth
behaviour and isn't entangled with route imports.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pipeline.admin.auth import ADMIN_TOKEN_HEADER, require_admin_token
from pipeline.common import config as config_module


@pytest.fixture
def app_with_token(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", "super-secret-token")

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_admin_token)])
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_returns_200_with_valid_token(app_with_token) -> None:
    resp = app_with_token.get(
        "/protected", headers={ADMIN_TOKEN_HEADER: "super-secret-token"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}


def test_returns_401_without_header(app_with_token) -> None:
    resp = app_with_token.get("/protected")
    assert resp.status_code == 401


def test_returns_401_with_wrong_token(app_with_token) -> None:
    resp = app_with_token.get(
        "/protected", headers={ADMIN_TOKEN_HEADER: "nope"}
    )
    assert resp.status_code == 401


def test_returns_401_with_empty_token(app_with_token) -> None:
    resp = app_with_token.get(
        "/protected", headers={ADMIN_TOKEN_HEADER: ""}
    )
    assert resp.status_code == 401


def test_returns_401_if_secret_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", "")

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_admin_token)])
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    resp = client.get(
        "/protected", headers={ADMIN_TOKEN_HEADER: "anything"}
    )
    assert resp.status_code == 401
    # Distinct error message so we know which branch fired.
    assert "not configured" in resp.json()["detail"]


def test_uses_constant_time_compare(monkeypatch) -> None:
    """The auth dep must reach secrets.compare_digest — patch it and prove."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", "the-real-token")

    calls: list[tuple[str, str]] = []
    real_compare = secrets.compare_digest

    def spy(a, b):  # noqa: ANN001
        calls.append((a, b))
        return real_compare(a, b)

    # Patch the symbol imported inside the auth module.
    import pipeline.admin.auth as auth_mod

    monkeypatch.setattr(auth_mod.secrets, "compare_digest", spy)

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_admin_token)])
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    client.get("/protected", headers={ADMIN_TOKEN_HEADER: "wrong-but-same-length"})
    assert calls, "secrets.compare_digest must be called"
    # The spy captured both arguments — confirms we are NOT short-circuiting
    # on prefix.
    assert calls[0][0] == "the-real-token"


def test_health_endpoint_is_public(monkeypatch) -> None:
    """/health must work without any header — used by Caddy + uptime probes."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", "x")
    from pipeline.admin.server import create_app

    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
