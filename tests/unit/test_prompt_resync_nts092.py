"""IT_PROJ_NTS_092 — migration 019 keeps the DB prompt rows and the code in sync.

This is the part that breaks quietly. Since NTS_067 generation reads the
brand's ACTIVE ``prompts`` row and falls back to the in-code constant when the
row's placeholders don't validate. Adding ``{fact_pack}`` to writer_draft's
required set means every live writer_draft row fails that check the moment the
code lands — and the failure mode is not an error, it is the manager's
admin-UI edits silently ceasing to reach production.

So the assertions here are deliberately about the SYMPTOM, not the mechanism:
after the migration, is the active row the thing generation actually renders,
and does an edit to it still reach the model?
"""

from __future__ import annotations

import json
import os
import sqlite3
import string
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin import seed_data
from pipeline.admin.models import Prompt
from pipeline.common import config as config_module
from pipeline.common.models import Language, RawItem, Topic
from pipeline.generator.comment_writer import (
    _DRAFT_PROMPT,
    _POLISH_PROMPT,
    _REQUIRED_PLACEHOLDERS,
    _TRANSLATE_PROMPT,
    CommentWriter,
)
from tests.unit.conftest import seed_icon_brand

_STALE_DRAFT = (
    "STALE pre-NTS_092 draft for {language_name}. Voice:{voice_profile_yaml} "
    "Title:{title} Summary:{summary} Banned:{banned_phrases}\n"
    "Length: 250-400 words."
)
_STALE_POLISH = (
    "STALE pre-NTS_092 polish for {language_name}. Tells:{ai_tells} "
    "Banned:{banned_phrases} Good:{good_examples} "
    "Principles:{voice_principles} Topics:{topics_relevant} "
    "Draft:{draft_json}\nThen 2-3 H2 sections."
)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def alembic_db(tmp_path):
    """A DB driven by real ``alembic`` subprocesses, like the deploy does."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "alembic-019.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(db_path),
    }

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    return db_path, alembic


def _seed_stale_rows(db_path: Path) -> None:
    """A stale ACTIVE writer_draft / writer_polish pair on the migration-seeded
    ``icon`` brand — what production looks like the moment before 019 runs."""
    with sqlite3.connect(db_path) as conn:
        brand_id = conn.execute("SELECT id FROM brands WHERE slug='icon'").fetchone()[0]
        for prompt_type, content in (
            ("writer_draft", _STALE_DRAFT),
            ("writer_polish", _STALE_POLISH),
            ("writer_translate", _TRANSLATE_PROMPT),
        ):
            conn.execute(
                "INSERT INTO prompts (brand_id_fk, prompt_type, version_name, "
                "content, is_active, created_by, created_at) "
                "VALUES (?, ?, 'v1.3 stale', ?, 1, 'test', '2026-01-01 00:00:00')",
                (brand_id, prompt_type, content),
            )
        conn.commit()


def _active(db_path: Path, prompt_type: str) -> tuple[str, str, str]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT content, version_name, notes FROM prompts "
            "WHERE prompt_type = ? AND is_active = 1",
            (prompt_type,),
        ).fetchone()
    assert row is not None, f"no active {prompt_type} row"
    return row


# --- the migration ---------------------------------------------------------


def test_019_reseeds_the_active_rows_with_the_current_constants(alembic_db):
    db_path, alembic = alembic_db
    alembic("upgrade", "018_research_budgets")
    _seed_stale_rows(db_path)
    alembic("upgrade", "head")

    draft_content, draft_version, draft_notes = _active(db_path, "writer_draft")
    assert draft_content == _DRAFT_PROMPT
    assert "{fact_pack}" in draft_content
    # 019's own length wording was superseded in S6: the target moved out of
    # the prompt into {depth_guidance}, computed from the material (NTS_102).
    # What 019 is being tested for here is the RESYNC — that the stale row was
    # replaced by the live constant — which the equality above already states.
    assert "{depth_guidance}" in draft_content
    assert "250-400" not in draft_content
    assert "NTS_092" in draft_version
    assert "{fact_pack}" in draft_notes  # the manual for whoever edits it next

    polish_content, _v, _n = _active(db_path, "writer_polish")
    assert polish_content == _POLISH_PROMPT
    # 019's own length wording was superseded twice: S6 moved the draft's
    # target into {depth_guidance}, and migration 031 did the same for polish
    # after the first real run showed the two stages enforcing different
    # numbers. What 019 is tested for here is the RESYNC — that the stale row
    # was replaced by the live constant — which the equality above states.
    assert "{depth_guidance}" in polish_content
    assert "the H2 sections (`## Heading`) the draft already has" in polish_content


# writer_polish's required set as of migration 019 — before S6/031 added the
# computed length target. Frozen here so the test below keeps asserting what it
# was written to assert.
_REQUIRED_PLACEHOLDERS_AT_019 = {
    "ai_tells",
    "banned_phrases",
    "good_examples",
    "voice_principles",
    "topics_relevant",
    "draft_json",
    "language_name",
}


def test_019_reseeds_polish_even_though_its_placeholders_did_not_change(alembic_db):
    """A stale polish row would still VALIDATE — the placeholder safety net
    does not catch it — and would compress the 700-word piece back to 400.
    A valid stale row is the more dangerous of the two."""
    db_path, alembic = alembic_db
    alembic("upgrade", "018_research_budgets")
    _seed_stale_rows(db_path)

    stale_polish, _v, _n = _active(db_path, "writer_polish")
    fields = {n for _, n, _, _ in string.Formatter().parse(stale_polish) if n}
    # The premise of this test at the time 019 was written: the stale row was
    # VALID, so the placeholder safety net could not catch it, and only the
    # reseed could. Migration 031 has since added {depth_guidance} to the
    # required set, so today the row would also fail validation — the belt now
    # has braces. The reseed is still what this test checks, and the original
    # premise is recorded rather than deleted: it is why 019 exists at all.
    assert _REQUIRED_PLACEHOLDERS_AT_019 <= fields, (
        "the stale row must have been valid at 019 for this test to mean anything"
    )
    assert "2-3 H2" in stale_polish

    alembic("upgrade", "head")
    assert _active(db_path, "writer_polish")[0] == _POLISH_PROMPT


def test_019_does_not_touch_writer_translate(alembic_db):
    """NTS_065 faithfulness holds the non-EN length and H2 count."""
    db_path, alembic = alembic_db
    alembic("upgrade", "018_research_budgets")
    _seed_stale_rows(db_path)
    before = _active(db_path, "writer_translate")
    alembic("upgrade", "head")
    assert _active(db_path, "writer_translate") == before


def test_019_is_idempotent_and_does_not_flip_active(alembic_db):
    db_path, alembic = alembic_db
    alembic("upgrade", "018_research_budgets")
    _seed_stale_rows(db_path)
    alembic("upgrade", "head")

    with sqlite3.connect(db_path) as conn:
        first = conn.execute(
            "SELECT id, content, version_name, created_at, is_active FROM prompts "
            "ORDER BY id"
        ).fetchall()
        # Grouped by (brand, type), which is what idx_active_prompt actually
        # enforces. Grouping by type alone started counting 5 the moment
        # migration 023 seeded one rubric per brand — a true fact about a
        # different invariant.
        active_count = conn.execute(
            "SELECT brand_id_fk, prompt_type, COUNT(*) FROM prompts "
            "WHERE is_active = 1 GROUP BY brand_id_fk, prompt_type"
        ).fetchall()

    # Re-running is a no-op, not a duplicate row and not a new timestamp.
    alembic("upgrade", "head")
    with sqlite3.connect(db_path) as conn:
        second = conn.execute(
            "SELECT id, content, version_name, created_at, is_active FROM prompts "
            "ORDER BY id"
        ).fetchall()
    assert second == first
    # In place: the same row ids, still exactly one active per type.
    assert all(count == 1 for _b, _t, count in active_count)
    assert all(row[4] == 1 for row in second)


def _head_revision() -> str:
    """The current head, read from the migration scripts themselves."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "pipeline/admin/migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def test_019_downgrade_is_a_clean_no_op_and_re_upgrade_works(alembic_db):
    """The content re-sync is not reversible by design; what matters is that
    stepping back and forward neither errors nor loses the schema."""
    db_path, alembic = alembic_db
    alembic("upgrade", "018_research_budgets")
    _seed_stale_rows(db_path)
    alembic("upgrade", "head")
    assert _active(db_path, "writer_draft")[0] == _DRAFT_PROMPT

    alembic("downgrade", "018_research_budgets")
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == "018_research_budgets"
    # Content stays forward — documented, not accidental.
    assert _active(db_path, "writer_draft")[0] == _DRAFT_PROMPT

    alembic("upgrade", "head")
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    # Whatever head is today — this assertion is about landing back ON head,
    # not about which revision that happens to be. Pinning the literal here
    # made every later migration fail this test for the wrong reason.
    assert version == _head_revision()


def test_a_fresh_seed_needs_no_resync(alembic_db):
    """A brand seeded today from the same constants is already in sync — 019
    is for the rows that predate it."""
    db_path, alembic = alembic_db
    alembic("upgrade", "head")
    with sqlite3.connect(db_path) as conn:
        brand_id = conn.execute(
            "SELECT id FROM brands WHERE slug='neovox'"
        ).fetchone()[0]
        version, content = seed_data.get_active_draft_prompt()
        conn.execute(
            "INSERT INTO prompts (brand_id_fk, prompt_type, version_name, content, "
            "is_active, created_by, created_at) VALUES (?, 'writer_draft', ?, ?, 1, "
            "'seed', '2026-01-01 00:00:00')",
            (brand_id, version, content),
        )
        conn.commit()
    assert content == _DRAFT_PROMPT
    assert "NTS_092" in version

    alembic("upgrade", "head")
    with sqlite3.connect(db_path) as conn:
        # Scoped to writer_draft: migration 023 also seeds an editorial_guard
        # row for the brand, and this test is about 019 not touching a fresh
        # writer seed.
        rows = conn.execute(
            "SELECT content, version_name FROM prompts WHERE brand_id_fk = ? "
            "AND prompt_type = 'writer_draft'",
            (brand_id,),
        ).fetchall()
    assert rows == [(content, version)]


def test_seed_version_names_are_bumped_with_the_constants():
    """A stale version_name on a fresh seed is how the DB and the code end up
    disagreeing about which prompt they are running."""
    for getter in (
        seed_data.get_active_draft_prompt,
        seed_data.get_active_polish_prompt,
    ):
        version, _content = getter()
        assert "NTS_092" in version, f"{getter.__name__} still names an old version"
    translate_version, _ = seed_data.get_active_translate_prompt()
    assert "NTS_065" in translate_version  # untouched


# --- Task D: the symptom, not the mechanism -------------------------------


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """An ORM-created admin.db with a brand, for the generation-path checks."""
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


def _resp(payload: dict[str, Any]) -> Any:
    msg = type("M", (), {"content": json.dumps(payload)})()
    choice = type("C", (), {"message": msg})()
    return type("R", (), {"choices": [choice], "usage": None})()


_DRAFT_OUT = {"title": "T", "body": "## A\n\nAcme raised $5m.", "key_takeaway": "K"}
_POLISH_OUT = {
    "title": "T",
    "body": (
        "Acme raised $5m this quarter.\n\n## The raise\n\n"
        "Acme allocated 5m across three funds.\n\n"
        "Acme's 5m now reprices its mezzanine book."
    ),
    "key_takeaway": "K",
}


def _topic() -> Topic:
    return Topic(
        id="t-1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="s",
            url="https://example.org/x",
            title="Acme raises a fund",
            summary="Acme raised five million.",
        ),
        relevance_score=8.0,
    )


async def _render_draft_prompt(brand_id: int) -> str:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_resp(_DRAFT_OUT), _resp(_POLISH_OUT)]
    )
    writer = CommentWriter(client=client, brand_id_fk=brand_id)
    await writer.write(_topic(), "banned_phrases: []\n", Language.en)
    return client.chat.completions.create.await_args_list[0].kwargs["messages"][0][
        "content"
    ]


async def test_a_pre_nts092_row_is_rejected_which_is_why_019_exists(live_db):
    """The failure this migration prevents, demonstrated: the old row loses
    to the code constant and the manager's edits stop reaching the model."""
    _add_prompt(live_db, "writer_draft", _STALE_DRAFT)
    prompt = await _render_draft_prompt(live_db)
    assert "STALE pre-NTS_092 draft" not in prompt
    assert prompt.startswith("OUTPUT LANGUAGE: English.")  # the code constant


async def test_the_reseeded_row_is_what_generation_renders(live_db):
    """After 019 the active row carries {fact_pack}, so it wins again."""
    _add_prompt(live_db, "writer_draft", _DRAFT_PROMPT)
    writer = CommentWriter(client=AsyncMock(), brand_id_fk=live_db)
    kwargs = {
        "voice_profile_yaml": "x",
        "title": "t",
        "url": "u",
        "summary": "s",
        "language": "en",
        "language_name": "English",
        "banned_phrases": "  (none)",
        "fact_pack": "  (none)",
        # S6 (NTS_102 v2) — rendered for every draft call, so a resolver test
        # that omits them is testing a call site that no longer exists.
        "plan": "  (none)",
        "depth_guidance": "TARGET SHAPE: article.",
        "primary_document": "  (none)",
    }
    assert writer._resolve_template("writer_draft", "FALLBACK", kwargs) == _DRAFT_PROMPT


async def test_sentinel_edit_to_the_active_row_reaches_the_next_generation(live_db):
    """Task D, reproducing the NTS_067 check: edit the active row, confirm the
    next generation picks it up, revert it cleanly."""
    _add_prompt(live_db, "writer_draft", _DRAFT_PROMPT)
    sentinel = "SENTINEL-NTS092-DO-NOT-SHIP"

    before = await _render_draft_prompt(live_db)
    assert sentinel not in before

    # --- edit in place, exactly as the admin UI writes it
    with admin_db.get_session_factory()() as session:
        row = session.scalars(
            select(Prompt).where(
                Prompt.prompt_type == "writer_draft", Prompt.is_active.is_(True)
            )
        ).first()
        assert row is not None
        row_id, original = row.id, row.content
        row.content = f"{sentinel}\n{original}"
        session.commit()

    during = await _render_draft_prompt(live_db)
    assert during.startswith(sentinel), "the admin-UI edit did NOT reach generation"

    # --- revert
    with admin_db.get_session_factory()() as session:
        row = session.get(Prompt, row_id)
        row.content = original
        session.commit()

    after = await _render_draft_prompt(live_db)
    assert sentinel not in after
    assert after == before
