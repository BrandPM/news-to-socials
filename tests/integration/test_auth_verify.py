"""NTS_058 Task 6 — POST /api/v1/admin/auth/verify tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import AdminUser
from pipeline.admin.passwords import hash_password
from pipeline.admin.rate_limit import login_verify_limiter
from pipeline.common import config as config_module

ADMIN_TOKEN = "tok-verify"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}
VERIFY = "/api/v1/admin/auth/verify"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    login_verify_limiter.reset()  # isolate rate-limit state per test
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.add(
            AdminUser(
                username="andriy",
                password_hash=hash_password("owner-password-xx"),
                created_at=datetime.now(tz=timezone.utc),
                is_active=True,
            )
        )
        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    login_verify_limiter.reset()
    admin_db.reset_for_tests()


def test_valid_credentials_200(client) -> None:
    resp = client.post(
        VERIFY, headers=AUTH, json={"username": "andriy", "password": "owner-password-xx"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"user_id": 1, "username": "andriy"}


def test_invalid_password_401(client) -> None:
    resp = client.post(
        VERIFY, headers=AUTH, json={"username": "andriy", "password": "wrong-password-x"}
    )
    assert resp.status_code == 401


def test_unknown_user_401(client) -> None:
    resp = client.post(
        VERIFY, headers=AUTH, json={"username": "ghost", "password": "owner-password-xx"}
    )
    assert resp.status_code == 401


def test_inactive_user_cannot_login_401(client) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.get(AdminUser, 1).is_active = False
        session.commit()
    resp = client.post(
        VERIFY, headers=AUTH, json={"username": "andriy", "password": "owner-password-xx"}
    )
    assert resp.status_code == 401


def test_last_login_updated_on_success(client) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        assert session.get(AdminUser, 1).last_login_at is None
    client.post(
        VERIFY, headers=AUTH, json={"username": "andriy", "password": "owner-password-xx"}
    )
    with factory() as session:
        assert session.get(AdminUser, 1).last_login_at is not None


def test_rate_limit_429_after_5_attempts(client) -> None:
    # 5 allowed (these fail auth → 401), the 6th is rate-limited → 429.
    for _ in range(5):
        r = client.post(
            VERIFY, headers=AUTH, json={"username": "andriy", "password": "bad-password-x"}
        )
        assert r.status_code == 401
    r6 = client.post(
        VERIFY, headers=AUTH, json={"username": "andriy", "password": "bad-password-x"}
    )
    assert r6.status_code == 429, r6.text


def test_verify_requires_admin_token(client) -> None:
    resp = client.post(
        VERIFY, json={"username": "andriy", "password": "owner-password-xx"}
    )
    assert resp.status_code == 401
