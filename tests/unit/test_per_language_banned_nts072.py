"""Per-language banned phrases: editable via Settings + used by generation.

IT_PROJ_NTS_072. The lists generation uses live in
``voice.<lang>.banned_phrases``; the new brand endpoint reads/writes them
per language (the old Settings page edited the unrelated flat column).
"""
# ruff: noqa: RUF001 — Cyrillic test fixtures mixed with ASCII on purpose.

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Brand
from pipeline.admin.voice_banned import (
    read_banned_by_language,
    write_banned_for_language,
)
from pipeline.common import config as config_module
from pipeline.common.models import Draft, Language, RawItem, Topic
from pipeline.generator.comment_writer import CommentWriter, parse_voice_guardrails
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-banned-072"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}

VOICE = yaml.safe_dump(
    {
        "voice_principles": ["Lead with a specific consequence."],
        "topics_relevant": ["family office operations"],
        "voice": {
            "en": {
                "banned_phrases": ["english cliché"],
                "style_examples": {"good": ["good en"]},
                "glossary": {"family office": "family office"},
            },
            "ru": {"banned_phrases": ["российский штамп"]},
            "uk": {"banned_phrases": ["український штамп"]},
            "pl": {"banned_phrases": []},
        },
    },
    allow_unicode=True,
    sort_keys=False,
)

LANGS = ["en", "ru", "uk", "pl"]


# --- helpers --------------------------------------------------------------


def test_read_banned_by_language_raw_no_fallback():
    out = read_banned_by_language(VOICE, LANGS)
    assert out["en"] == ["english cliché"]
    assert out["ru"] == ["российский штамп"]
    assert out["uk"] == ["український штамп"]
    assert out["pl"] == []  # empty stays empty (no EN fallback in the editor)


def test_write_banned_sets_one_language_preserves_rest():
    new_yaml = write_banned_for_language(VOICE, "ru", ["новый штамп", "ещё", "новый штамп"])
    data = yaml.safe_load(new_yaml)
    # de-duped, order-preserving.
    assert data["voice"]["ru"]["banned_phrases"] == ["новый штамп", "ещё"]
    # other languages + structure untouched.
    assert data["voice"]["en"]["banned_phrases"] == ["english cliché"]
    assert data["voice"]["en"]["style_examples"]["good"] == ["good en"]
    assert data["voice"]["uk"]["banned_phrases"] == ["український штамп"]
    assert data["voice_principles"] == ["Lead with a specific consequence."]
    assert data["topics_relevant"] == ["family office operations"]


def test_write_banned_on_empty_profile_creates_section():
    out = write_banned_for_language("", "ru", ["x"])
    assert yaml.safe_load(out)["voice"]["ru"]["banned_phrases"] == ["x"]


# --- endpoint -------------------------------------------------------------


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
        brand = session.get(Brand, icon_id)
        brand.languages = json.dumps(LANGS)
        brand.voice_profile_yaml = VOICE
        session.commit()
    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def test_get_banned_by_language(client_and_icon):
    client, icon_id = client_and_icon
    resp = client.get(f"/api/v1/brands/{icon_id}/banned-phrases", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["languages"] == LANGS
    assert body["banned"]["ru"] == ["российский штамп"]
    assert body["banned"]["pl"] == []


def test_put_banned_writes_only_target_language(client_and_icon):
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}/banned-phrases",
        headers=AUTH,
        json={"language": "ru", "phrases": ["в условиях постоянных изменений", "штамп"]},
    )
    assert resp.status_code == 200, resp.text
    banned = resp.json()["banned"]
    assert banned["ru"] == ["в условиях постоянных изменений", "штамп"]
    assert banned["en"] == ["english cliché"]  # untouched

    # Persisted + voice_principles/topics_relevant preserved.
    with admin_db.get_session_factory()() as session:
        data = yaml.safe_load(session.get(Brand, icon_id).voice_profile_yaml)
    assert data["voice"]["ru"]["banned_phrases"] == [
        "в условиях постоянных изменений",
        "штамп",
    ]
    assert data["voice"]["en"]["banned_phrases"] == ["english cliché"]
    assert data["voice_principles"] == ["Lead with a specific consequence."]


def test_put_banned_rejects_language_outside_roster(client_and_icon):
    client, icon_id = client_and_icon
    resp = client.put(
        f"/api/v1/brands/{icon_id}/banned-phrases",
        headers=AUTH,
        json={"language": "de", "phrases": ["x"]},
    )
    assert resp.status_code == 400


# --- generation uses the per-language list --------------------------------


def _resp(payload: dict[str, Any]) -> Any:
    msg = type("M", (), {"content": json.dumps(payload)})()
    return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": None})()


def test_parse_voice_guardrails_is_per_language():
    assert parse_voice_guardrails(VOICE, Language.ru)[0] == ["российский штамп"]
    assert parse_voice_guardrails(VOICE, Language.uk)[0] == ["український штамп"]
    assert parse_voice_guardrails(VOICE, Language.en)[0] == ["english cliché"]


async def test_translate_uses_target_language_bans_not_en():
    en_draft = Draft(
        topic_id="t-1", brand_id="icon", language=Language.en,
        title="T", body="## H\n\nBody.", key_takeaway="K",
    )
    payload = {"title": "Т", "body": "## Х\n\nТело.", "key_takeaway": "К"}
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_resp(payload)])
    writer = CommentWriter(client=client)
    await writer.translate(en_draft, Language.ru, VOICE)
    prompt = client.chat.completions.create.await_args_list[0].kwargs["messages"][0]["content"]
    assert "российский штамп" in prompt
    assert "english cliché" not in prompt


async def test_polish_uses_draft_language_bans():
    clean = {"title": "T", "body": "## H\n\nClean body paragraph here.", "key_takeaway": "K"}
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_resp(clean), _resp(clean)])
    writer = CommentWriter(client=client)
    topic = Topic(
        id="t-1", brand_id="icon", relevance_score=8.0,
        raw=RawItem(source_id="s", source_name="s", url="https://e.com/x", title="X"),
    )
    await writer.write(topic, VOICE, Language.uk)
    polish_prompt = client.chat.completions.create.await_args_list[1].kwargs["messages"][0]["content"]
    assert "український штамп" in polish_prompt
    assert "english cliché" not in polish_prompt


async def test_empty_language_bans_do_not_break_generation():
    clean = {"title": "T", "body": "## H\n\nClean body paragraph here.", "key_takeaway": "K"}
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_resp(clean), _resp(clean)])
    writer = CommentWriter(client=client)
    topic = Topic(
        id="t-1", brand_id="icon", relevance_score=8.0,
        raw=RawItem(source_id="s", source_name="s", url="https://e.com/x", title="X"),
    )
    out = await writer.write(topic, VOICE, Language.pl)  # pl bans empty
    assert out.body == clean["body"]
    polish_prompt = client.chat.completions.create.await_args_list[1].kwargs["messages"][0]["content"]
    assert "(none specified)" in polish_prompt
