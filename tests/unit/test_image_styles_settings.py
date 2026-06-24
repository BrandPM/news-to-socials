"""NTS_075 L3 — cover-image styles live in brand settings, not hardcode.

Covers:
* ``parse_image_style_prompts`` / ``read_image_styles`` shapes + malformed.
* ``write_image_styles`` round-trip preserving the rest of the voice profile.
* ``run._resolve_brand_image_styles`` falls back to the default set when the
  profile carries none (back-compat — never zero styles).
* GET / PUT ``/api/v1/brands/{id}/image-styles`` endpoints.
"""

from __future__ import annotations

import json

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.image_styles import read_image_styles, write_image_styles
from pipeline.admin.models import Brand
from pipeline.common import config as config_module
from pipeline.generator.comment_writer import parse_image_style_prompts
from pipeline.generator.image import DEFAULT_ICON_IMAGE_STYLES
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-imgstyles"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}

VOICE = (
    "voice:\n"
    "  en:\n"
    "    banned_phrases:\n"
    "      - english cliché\n"
    "voice_principles:\n"
    "  - Lead with a specific consequence.\n"
    "image:\n"
    "  style_prompts:\n"
    "    - abstract finance geometry, navy and gold\n"
    "    - urban skyline at golden hour\n"
)


# --- parsing / helpers ----------------------------------------------------


def test_parse_nested_and_flat_shapes():
    assert parse_image_style_prompts(VOICE) == [
        "abstract finance geometry, navy and gold",
        "urban skyline at golden hour",
    ]
    assert parse_image_style_prompts('image_style_prompts: ["x", "y"]') == ["x", "y"]


def test_parse_missing_and_malformed_returns_empty():
    assert parse_image_style_prompts("voice: {}") == []
    assert parse_image_style_prompts(": : [") == []
    assert parse_image_style_prompts("") == []


def test_read_image_styles_raw():
    assert read_image_styles(VOICE) == [
        "abstract finance geometry, navy and gold",
        "urban skyline at golden hour",
    ]
    assert read_image_styles("") == []


def test_write_image_styles_preserves_rest_and_dedupes():
    new_yaml = write_image_styles(VOICE, ["one", "two", "two", " one ", " "])
    data = yaml.safe_load(new_yaml)
    # De-duped, trimmed, order-preserving.
    assert data["image"]["style_prompts"] == ["one", "two"]
    # Voice + principles untouched.
    assert data["voice"]["en"]["banned_phrases"] == ["english cliché"]
    assert data["voice_principles"] == ["Lead with a specific consequence."]


def test_write_image_styles_on_empty_profile_creates_section():
    new_yaml = write_image_styles("", ["solo style"])
    assert yaml.safe_load(new_yaml)["image"]["style_prompts"] == ["solo style"]


def test_resolve_brand_image_styles_fallback_to_default():
    from pipeline.run import _resolve_brand_image_styles

    # Populated profile → its styles.
    assert _resolve_brand_image_styles(VOICE) == [
        "abstract finance geometry, navy and gold",
        "urban skyline at golden hour",
    ]
    # Empty / legacy profile → the rich built-in default (never zero).
    assert _resolve_brand_image_styles("") == list(DEFAULT_ICON_IMAGE_STYLES)
    assert _resolve_brand_image_styles("voice: {}") == list(DEFAULT_ICON_IMAGE_STYLES)
    assert len(DEFAULT_ICON_IMAGE_STYLES) >= 12


# --- endpoints ------------------------------------------------------------


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
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session)
        session.get(Brand, icon_id).voice_profile_yaml = VOICE
        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def test_get_image_styles(client_and_icon):
    client, icon_id = client_and_icon
    resp = client.get(f"/api/v1/brands/{icon_id}/image-styles", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["styles"] == [
        "abstract finance geometry, navy and gold",
        "urban skyline at golden hour",
    ]


def test_put_image_styles_replaces_and_preserves_voice(client_and_icon):
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}/image-styles",
        headers=AUTH,
        json={"styles": ["macro texture, raking light", "editorial flat illustration"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["styles"] == [
        "macro texture, raking light",
        "editorial flat illustration",
    ]
    # Persisted + voice section preserved.
    with admin_db.get_session_factory()() as session:
        data = yaml.safe_load(session.get(Brand, icon_id).voice_profile_yaml)
    assert data["image"]["style_prompts"] == [
        "macro texture, raking light",
        "editorial flat illustration",
    ]
    assert data["voice"]["en"]["banned_phrases"] == ["english cliché"]


def test_get_image_styles_404_for_missing_brand(client_and_icon):
    client, _ = client_and_icon
    resp = client.get("/api/v1/brands/9999/image-styles", headers=AUTH)
    assert resp.status_code == 404
