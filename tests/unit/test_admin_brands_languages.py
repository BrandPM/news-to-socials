"""NTS_056 Task 2 — BrandDetail.languages serialization + PUT validation.

Closes the cosmetic half of the NTS_055 regression: the multilingual
roster lives in ``brands.languages`` (JSON-as-TEXT) but was never surfaced
on the ``GET /brands/{id}`` detail wire, so the edit UI could only show a
single-language dropdown. These tests pin the contract:

* GET detail includes ``languages`` (list, default ``["en"]``).
* PUT accepts a valid subset and persists it.
* PUT rejects a roster without ``en`` (400).
* PUT rejects an unsupported language (400).
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Brand
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-brands-lang"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client_and_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        brand = session.get(Brand, icon_id)
        brand.languages = json.dumps(["en", "ru", "uk", "pl"])
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def test_get_detail_includes_languages(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.get(f"/api/v1/brands/{icon_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "languages" in body
    assert body["languages"] == ["en", "ru", "uk", "pl"]


def test_detail_languages_defaults_to_en_when_blank(client_and_icon) -> None:
    client, icon_id = client_and_icon
    # The column is NOT NULL (default '["en"]'), but a blank/garbage value
    # could slip in from a botched migration — the wire schema must still
    # fall back to ["en"] rather than 500 (NTS_055 lesson).
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.get(Brand, icon_id).languages = ""
        session.commit()
    resp = client.get(f"/api/v1/brands/{icon_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["languages"] == ["en"]


def test_put_languages_subset_persists(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}", headers=AUTH, json={"languages": ["en", "ru"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["languages"] == ["en", "ru"]

    # Round-trip through the DB to confirm it's stored as JSON text.
    factory = admin_db.get_session_factory()
    with factory() as session:
        assert json.loads(session.get(Brand, icon_id).languages) == ["en", "ru"]


def test_put_languages_dedupes_and_lowercases(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}",
        headers=AUTH,
        json={"languages": ["EN", "ru", "ru", "PL"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["languages"] == ["en", "ru", "pl"]


def test_put_languages_without_en_is_400(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}",
        headers=AUTH,
        json={"languages": ["ru", "uk", "pl"]},
    )
    assert resp.status_code == 400, resp.text
    assert "en" in resp.json()["detail"]


def test_put_languages_unknown_language_is_400(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}", headers=AUTH, json={"languages": ["xx"]}
    )
    assert resp.status_code == 400, resp.text
    assert "unsupported" in resp.json()["detail"].lower()


def test_put_languages_empty_list_is_400(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}", headers=AUTH, json={"languages": []}
    )
    assert resp.status_code == 400, resp.text


def test_put_without_languages_preserves_roster(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}", headers=AUTH, json={"name": "Icon Finance v2"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["languages"] == ["en", "ru", "uk", "pl"]
