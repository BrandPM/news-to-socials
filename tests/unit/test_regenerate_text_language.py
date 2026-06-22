"""Regenerate-text is language-aware (IT_PROJ_NTS_066).

Bug: "Regenerate text" ran the polish stage with the default
``Language.en`` regardless of the draft's language, so regenerating a RU/UK/PL
draft rewrote it in English. Fix: EN drafts re-polish (canon); non-EN drafts
re-translate from the canonical EN draft of the same topic, result always in
the draft's own language.

Only the network boundary (the OpenAI client + Sanity HTTP) is mocked, so the
real ``CommentWriter.translate`` / ``_polish`` / ``_parse`` and the
``translation_check`` fidelity gates run.
"""
# ruff: noqa: RUF001 — RU fixtures mix Cyrillic with ASCII figures on purpose.

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin.text_regenerate import RegenerateError, regenerate_draft_text
from pipeline.common import config as config_module
from pipeline.generator import comment_writer as cw
from pipeline.generator import translation_check as tc
from tests.unit.conftest import seed_icon_brand

# --- canonical EN source used across the non-EN tests ---------------------

EN_BODY = (
    "Icon sees a shift.\n\n"
    "## The repricing\n\n"
    "A $2.4m allocation moved into 3 funds, up 67% on the quarter.\n\n"
    "## What changes next\n\nBase rates held at 50bp.\n"
)
EN_TITLE = "The repricing of mezzanine credit"

# A faithful RU translation: Cyrillic, same 2 H2, same numbers, ~same length.
RU_GOOD = {
    "title": "Переоценка мезонинного кредита",
    "body": (
        "Icon видит сдвиг.\n\n"
        "## Переоценка\n\n"
        "Аллокация $2,4 млн ушла в 3 фонда, рост на 67% за квартал.\n\n"
        "## Что меняется дальше\n\nБазовые ставки на уровне 50bp.\n"
    ),
    "key_takeaway": "Пересмотрите допущения по доходности.",
}
# A RU translation that fabricates a figure (85%) the EN never had.
RU_FABRICATED = {
    "title": "Переоценка мезонинного кредита",
    "body": (
        "## Переоценка\n\n"
        "85% клиентов перевели $2,4 млн в 3 фонда, рост 67%, ставки 50bp.\n\n"
        "## Что меняется дальше\n\nДалее.\n"
    ),
    "key_takeaway": "Пересмотрите допущения.",
}
EN_POLISHED = {
    "title": "The repricing of mezzanine credit",
    "body": EN_BODY,
    "key_takeaway": "Allocators should revisit yield assumptions.",
}


# --- fakes ----------------------------------------------------------------


class _FakeResp:
    def __init__(self, content: str) -> None:
        msg = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": msg})()]
        self.usage = None


@pytest.fixture
def fake_llm(monkeypatch):
    """Stub the OpenAI client inside CommentWriter; capture sent prompts."""
    holder: dict = {"response": "{}", "prompts": []}

    class _Completions:
        async def create(self, **kwargs):
            holder["prompts"].append(kwargs["messages"][0]["content"])
            return _FakeResp(holder["response"])

    class _Chat:
        completions = _Completions()

    class _FakeOpenAI:
        def __init__(self, *a, **kw) -> None:
            self.chat = _Chat()

    monkeypatch.setattr(cw, "AsyncOpenAI", _FakeOpenAI)
    return holder


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


def _mock_sanity(monkeypatch, *, draft_doc, en_rows):
    """Patch SanityClient.query (dispatch by params) + capture patches."""
    from pipeline.publisher import sanity as sanity_mod

    patches: list[tuple[str, dict]] = []

    async def fake_query(self, groq, params=None):  # noqa: ANN001
        params = params or {}
        if "id" in params:
            return draft_doc
        if "tid" in params:
            return en_rows
        return None

    async def fake_patch(self, doc_id, set_fields=None, unset_fields=None):  # noqa: ANN001
        patches.append((doc_id, set_fields or {}))
        return {}

    monkeypatch.setattr(sanity_mod.SanityClient, "query", fake_query)
    monkeypatch.setattr(sanity_mod.SanityClient, "patch", fake_patch)
    return patches


def _en_sibling(doc_id="post-en1"):
    return {
        "_id": doc_id,
        "title": EN_TITLE,
        "body": [
            {"_type": "block", "style": "normal",
             "children": [{"_type": "span", "text": "Icon sees a shift."}]},
            {"_type": "block", "style": "h2",
             "children": [{"_type": "span", "text": "The repricing"}]},
            {"_type": "block", "style": "normal",
             "children": [{"_type": "span",
                           "text": "A $2.4m allocation moved into 3 funds, up 67% on the quarter."}]},
            {"_type": "block", "style": "h2",
             "children": [{"_type": "span", "text": "What changes next"}]},
            {"_type": "block", "style": "normal",
             "children": [{"_type": "span", "text": "Base rates held at 50bp."}]},
        ],
        "keyTakeaway": "Allocators should revisit yield assumptions.",
    }


# --- tests ----------------------------------------------------------------


async def test_regenerate_ru_draft_translates_from_en_not_english(
    fresh_db, fake_llm, monkeypatch
):
    """RU draft → result is Russian (Cyrillic), structure/number parity with
    EN, and the TRANSLATE path is used (not polish)."""
    fake_llm["response"] = json.dumps(RU_GOOD)
    ru_draft = {
        "title": "stale title",
        "body": [{"_type": "block", "style": "normal",
                  "children": [{"_type": "span", "text": "старое тело"}]}],
        "language": "ru",
        "topicId": "t-1",
    }
    patches = _mock_sanity(monkeypatch, draft_doc=ru_draft, en_rows=[_en_sibling()])

    await regenerate_draft_text("drafts.post-ru1", fresh_db)

    # One patch, on the RU draft itself (EN sibling untouched).
    assert len(patches) == 1
    doc_id, fields = patches[0]
    assert doc_id == "drafts.post-ru1"
    # The translate prompt — not the polish prompt — was sent.
    assert any("faithful translator" in p for p in fake_llm["prompts"])
    assert all("Rewrite this draft to sound more natural" not in p for p in fake_llm["prompts"])
    assert any("OUTPUT LANGUAGE: Russian" in p for p in fake_llm["prompts"])
    # Reconstruct the patched body markdown to assert fidelity.
    patched_md = "\n\n".join(
        ("## " if b["style"] == "h2" else "")
        + b["children"][0]["text"]
        for b in fields["body"]
    )
    assert tc.is_mostly_cyrillic(patched_md)
    assert not tc.is_mostly_cyrillic(EN_BODY)  # sanity: EN would have failed
    assert tc.invented_numbers(EN_BODY, patched_md) == []
    assert tc.h2_count(patched_md) == tc.h2_count(EN_BODY) == 2
    assert fields["title"] == "Переоценка мезонинного кредита"
    assert not tc.has_markdown_in_title(fields["title"])


async def test_regenerate_en_draft_stays_english_canon(
    fresh_db, fake_llm, monkeypatch
):
    """EN draft → polish path, output stays English (canon not broken)."""
    fake_llm["response"] = json.dumps(EN_POLISHED)
    en_draft = {
        "title": EN_TITLE,
        "body": _en_sibling()["body"],
        "language": "en",
        "topicId": "t-1",
    }
    patches = _mock_sanity(monkeypatch, draft_doc=en_draft, en_rows=[_en_sibling()])

    await regenerate_draft_text("drafts.post-en1", fresh_db)

    assert len(patches) == 1
    # Polish prompt used, NOT the translate prompt.
    assert any("Rewrite this draft to sound more natural" in p for p in fake_llm["prompts"])
    assert all("faithful translator" not in p for p in fake_llm["prompts"])
    _doc_id, fields = patches[0]
    patched_md = "\n\n".join(
        ("## " if b["style"] == "h2" else "") + b["children"][0]["text"]
        for b in fields["body"]
    )
    assert not tc.is_mostly_cyrillic(patched_md)
    assert "repricing" in patched_md.lower()


async def test_regenerate_non_en_without_en_source_raises_clear_error(
    fresh_db, fake_llm, monkeypatch
):
    """No EN sibling → RegenerateError (clear message), nothing patched, no LLM call."""
    ru_draft = {"title": "x", "body": [], "language": "uk", "topicId": "t-orphan"}
    patches = _mock_sanity(monkeypatch, draft_doc=ru_draft, en_rows=[])

    with pytest.raises(RegenerateError, match="no English source"):
        await regenerate_draft_text("drafts.post-uk1", fresh_db)

    assert patches == []
    assert fake_llm["prompts"] == []  # bailed before any translate call


async def test_regenerate_translation_with_invented_numbers_is_blocked(
    fresh_db, fake_llm, monkeypatch
):
    """A translation that fabricates a figure fails the hard gate → error, no patch."""
    fake_llm["response"] = json.dumps(RU_FABRICATED)
    ru_draft = {"title": "x", "body": [], "language": "ru", "topicId": "t-1"}
    patches = _mock_sanity(monkeypatch, draft_doc=ru_draft, en_rows=[_en_sibling()])

    with pytest.raises(RegenerateError, match="introduced numbers"):
        await regenerate_draft_text("drafts.post-ru1", fresh_db)

    assert patches == []  # bad translation never written
