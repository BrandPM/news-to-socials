"""NTS_058 Task 6 — /api/v1/admin/users CRUD route tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import AdminUser
from pipeline.admin.passwords import hash_password, verify_password
from pipeline.common import config as config_module

ADMIN_TOKEN = "tok-users"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}
USERS = "/api/v1/admin/users"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    # Seed the owner as id=1 (the lock-out-protected user).
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
    admin_db.reset_for_tests()


def test_create_returns_201_without_password(client) -> None:
    resp = client.post(
        USERS, headers=AUTH, json={"username": "teammate", "password": "long-enough-pw"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "teammate"
    assert body["is_active"] is True
    assert "password" not in body and "password_hash" not in body


def test_create_duplicate_username_409(client) -> None:
    client.post(USERS, headers=AUTH, json={"username": "dup", "password": "long-enough-pw"})
    resp = client.post(
        USERS, headers=AUTH, json={"username": "dup", "password": "another-long-pw"}
    )
    assert resp.status_code == 409, resp.text


def test_create_short_password_400(client) -> None:
    resp = client.post(USERS, headers=AUTH, json={"username": "shorty", "password": "short"})
    assert resp.status_code == 400, resp.text


def test_create_bad_username_400(client) -> None:
    resp = client.post(
        USERS, headers=AUTH, json={"username": "Bad Name", "password": "long-enough-pw"}
    )
    assert resp.status_code == 400, resp.text


def test_list_returns_all_without_hash(client) -> None:
    client.post(USERS, headers=AUTH, json={"username": "tuser2", "password": "long-enough-pw"})
    resp = client.get(USERS, headers=AUTH)
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["username"] for r in rows} >= {"andriy", "tuser2"}
    for r in rows:
        assert "password_hash" not in r


def test_delete_non_seed_user_soft_deletes(client) -> None:
    created = client.post(
        USERS, headers=AUTH, json={"username": "tempuser", "password": "long-enough-pw"}
    ).json()
    resp = client.delete(f"{USERS}/{created['id']}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


def test_delete_seed_user_403(client) -> None:
    resp = client.delete(f"{USERS}/1", headers=AUTH)
    assert resp.status_code == 403, resp.text
    # Seed user still active.
    factory = admin_db.get_session_factory()
    with factory() as session:
        assert session.get(AdminUser, 1).is_active is True


def test_reset_password_changes_hash(client) -> None:
    created = client.post(
        USERS, headers=AUTH, json={"username": "resetme", "password": "original-pw-12"}
    ).json()
    resp = client.post(
        f"{USERS}/{created['id']}/reset-password",
        headers=AUTH,
        json={"password": "brand-new-pw-12"},
    )
    assert resp.status_code == 200, resp.text
    factory = admin_db.get_session_factory()
    with factory() as session:
        h = session.get(AdminUser, created["id"]).password_hash
        assert verify_password("brand-new-pw-12", h)
        assert not verify_password("original-pw-12", h)


def test_reset_password_short_400(client) -> None:
    created = client.post(
        USERS, headers=AUTH, json={"username": "resetshort", "password": "original-pw-12"}
    ).json()
    resp = client.post(
        f"{USERS}/{created['id']}/reset-password", headers=AUTH, json={"password": "x"}
    )
    assert resp.status_code == 400


def test_requires_admin_token(client) -> None:
    assert client.get(USERS).status_code == 401
