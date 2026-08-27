"""IT_PROJ_NTS_067 — anti-generic guards + prompts-as-source-of-truth.

Two features:
* a generic-close detector + one-shot retry that re-anchors a topic-agnostic
  closing paragraph (Task A);
* generation sources the brand's ACTIVE ``prompts`` row, falling back to the
  in-code constant when absent or unsafe (Task B).

The LLM round-trip is mocked; the DB is real (tmp SQLite).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.models import Prompt
from pipeline.common import config as config_module
from pipeline.common.models import Language, RawItem, Topic
from pipeline.generator.anti_ai_check import close_lacks_anchor
from pipeline.generator.comment_writer import (
    _DRAFT_PROMPT,
    CommentWriter,
    parse_voice_principles,
)
from tests.unit.conftest import seed_icon_brand

# --- close_lacks_anchor (detector) ----------------------------------------


def test_close_anchored_to_body_entity_is_not_flagged():
    body = (
        "Acme raised $5m this quarter.\n\n"
        "## The raise\n\n"
        "Acme moved 5m into three funds.\n\n"
        "Acme now reprices its mezzanine book before year-end."
    )
    assert close_lacks_anchor(body) is False


def test_generic_close_is_flagged():
    body = (
        "Acme raised $5m this quarter.\n\n"
        "## The raise\n\n"
        "The firm allocated 5m across funds.\n\n"
        "Rising uncertainty creates challenges and requires immediate decisions."
    )
    assert close_lacks_anchor(body) is True


def test_close_not_flagged_when_body_has_no_anchors():
    # Nothing concrete to anchor to → upstream problem, don't flag the close.
    body = (
        "Some vague intro about things.\n\n"
        "## Overview\n\n"
        "More vague words without specifics.\n\n"
        "A generic forward-looking close."
    )
    assert close_lacks_anchor(body) is False


def test_close_not_flagged_when_single_paragraph():
    assert close_lacks_anchor("## H\n\nOnly one content paragraph here.") is False


# --- parse_voice_principles -----------------------------------------------


def test_parse_voice_principles_top_level():
    yaml_str = (
        "voice_principles:\n"
        "  - One concrete number per paragraph.\n"
        "  - Lead with a specific consequence.\n"
    )
    out = parse_voice_principles(yaml_str)
    assert out == [
        "One concrete number per paragraph.",
        "Lead with a specific consequence.",
    ]


def test_parse_voice_principles_missing_returns_empty():
    assert parse_voice_principles("mission: x\n") == []


# --- write() generic-close retry ------------------------------------------


def _topic() -> Topic:
    return Topic(
        id="t-1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="s",
            url="https://example.com/x",
            title="Acme raises a fund",
            summary="Acme raised five million.",
        ),
        relevance_score=8.0,
    )


def _resp(payload: dict[str, Any]) -> Any:
    msg = type("M", (), {"content": json.dumps(payload)})()
    choice = type("C", (), {"message": msg})()
    return type("R", (), {"choices": [choice], "usage": None})()


_DRAFT = {"title": "T", "body": "## A\n\nAcme raised $5m.", "key_takeaway": "K"}
_GENERIC_POLISH = {
    "title": "T",
    "body": (
        "Acme raised $5m this quarter.\n\n"
        "## The raise\n\n"
        "Acme allocated 5m across three funds.\n\n"
        "This requires immediate decisions."  # generic, unanchored close
    ),
    "key_takeaway": "K",
}
_ANCHORED_RETRY = {
    "title": "T",
    "body": (
        "Acme raised $5m this quarter.\n\n"
        "## The raise\n\n"
        "Acme allocated 5m across three funds.\n\n"
        "Acme's 5m reprices its mezzanine book before year-end."  # anchored
    ),
    "key_takeaway": "K",
}
_ANCHORED_POLISH = {
    "title": "T",
    "body": (
        "Acme raised $5m this quarter.\n\n"
        "## The raise\n\n"
        "Acme allocated 5m across three funds.\n\n"
        "Acme's 5m now reprices its mezzanine book."
    ),
    "key_takeaway": "K",
}


async def test_write_retries_once_on_generic_close():
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_resp(_DRAFT), _resp(_GENERIC_POLISH), _resp(_ANCHORED_RETRY)]
    )
    writer = CommentWriter(client=client)
    out = await writer.write(_topic(), "banned_phrases: []\n", Language.en)

    # draft + polish + ONE generic-close retry.
    assert client.chat.completions.create.await_count == 3
    assert out.body == _ANCHORED_RETRY["body"]
    # The retry prompt is the generic-close one.
    retry_prompt = client.chat.completions.create.await_args_list[2].kwargs[
        "messages"
    ][0]["content"]
    assert "generic close" in retry_prompt.lower()


async def test_write_no_generic_retry_when_close_is_anchored():
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_resp(_DRAFT), _resp(_ANCHORED_POLISH)]
    )
    writer = CommentWriter(client=client)
    out = await writer.write(_topic(), "banned_phrases: []\n", Language.en)

    assert client.chat.completions.create.await_count == 2
    assert out.body == _ANCHORED_POLISH["body"]


# --- prompts source of truth (Task B) -------------------------------------


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session, with_sanity_creds=True)
        session.commit()
    yield icon_id
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


_DRAFT_KWARGS = {
    "voice_profile_yaml": "x",
    "title": "t",
    "url": "u",
    "summary": "s",
    "language": "en",
    "language_name": "English",
    "banned_phrases": "  (none specified)",
    # NTS_092 — the research fact pack is rendered for every draft call.
    "fact_pack": "  (none)",
}

# A valid DB draft template: contains every required placeholder, no unknowns.
# {fact_pack} is required as of NTS_092 — a DB row without it drafts from the
# headline while the code constant researches, so the resolver rejects it.
_VALID_DB_DRAFT = (
    "DB-SOURCED draft for {language_name}. Voice:{voice_profile_yaml} "
    "Title:{title} Summary:{summary} Banned:{banned_phrases} "
    "Facts:{fact_pack}"
)


def _add_prompt(brand_id: int, prompt_type: str, content: str) -> None:
    with admin_db.get_session_factory()() as session:
        session.add(
            Prompt(
                brand_id_fk=brand_id,
                prompt_type=prompt_type,
                version_name="test",
                content=content,
                is_active=True,
                created_by="test",
            )
        )
        session.commit()


def test_resolver_uses_active_db_row_when_valid(fresh_db):
    _add_prompt(fresh_db, "writer_draft", _VALID_DB_DRAFT)
    writer = CommentWriter(client=AsyncMock(), brand_id_fk=fresh_db)
    tmpl = writer._resolve_template("writer_draft", _DRAFT_PROMPT, _DRAFT_KWARGS)
    assert tmpl == _VALID_DB_DRAFT


def test_resolver_falls_back_when_no_active_row(fresh_db):
    writer = CommentWriter(client=AsyncMock(), brand_id_fk=fresh_db)
    tmpl = writer._resolve_template("writer_draft", _DRAFT_PROMPT, _DRAFT_KWARGS)
    assert tmpl == _DRAFT_PROMPT  # in-code fallback


def test_resolver_falls_back_when_required_placeholder_missing(fresh_db):
    # Drops {title} — a required placeholder → unsafe → fallback to constant.
    _add_prompt(fresh_db, "writer_draft", "Bad template {language_name} {summary}")
    writer = CommentWriter(client=AsyncMock(), brand_id_fk=fresh_db)
    tmpl = writer._resolve_template("writer_draft", _DRAFT_PROMPT, _DRAFT_KWARGS)
    assert tmpl == _DRAFT_PROMPT


def test_resolver_falls_back_when_unknown_placeholder(fresh_db):
    # Introduces {bogus} we don't supply → would KeyError at format → fallback.
    bad = _VALID_DB_DRAFT + " {bogus}"
    _add_prompt(fresh_db, "writer_draft", bad)
    writer = CommentWriter(client=AsyncMock(), brand_id_fk=fresh_db)
    tmpl = writer._resolve_template("writer_draft", _DRAFT_PROMPT, _DRAFT_KWARGS)
    assert tmpl == _DRAFT_PROMPT


def test_resolver_always_constant_when_no_brand_id():
    writer = CommentWriter(client=AsyncMock(), brand_id_fk=None)
    tmpl = writer._resolve_template("writer_draft", _DRAFT_PROMPT, _DRAFT_KWARGS)
    assert tmpl == _DRAFT_PROMPT


async def test_generation_renders_db_sourced_draft_prompt(fresh_db):
    """End-to-end: with an active DB draft row, the prompt actually SENT to
    the model is the DB-sourced one (proves admin-UI edits drive generation)."""
    _add_prompt(fresh_db, "writer_draft", _VALID_DB_DRAFT)
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_resp(_DRAFT), _resp(_ANCHORED_POLISH)]
    )
    writer = CommentWriter(client=client, brand_id_fk=fresh_db)
    await writer.write(_topic(), "banned_phrases: []\n", Language.en)

    draft_prompt = client.chat.completions.create.await_args_list[0].kwargs[
        "messages"
    ][0]["content"]
    assert draft_prompt.startswith("DB-SOURCED draft for English.")
