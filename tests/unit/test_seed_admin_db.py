"""Tests for the seed_admin_db idempotent seed script."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.models import Brand, PipelineConfig, Prompt, Source
from pipeline.common import config as config_module
from scripts.seed_admin_db import seed


@pytest.fixture
def tmp_admin_db(tmp_path, monkeypatch):
    """Bind the admin engine to a fresh tmp DB for the duration of a test."""
    db_path = tmp_path / "admin.db"
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(db_path))
    # Provide a known encryption key so Icon's sanity token (if any) can
    # be encrypted at seed time. Tests don't rely on a specific value.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    from pipeline.admin import encryption as enc_mod

    enc_mod.reset_for_tests()

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=db_path)
    admin_db.Base.metadata.create_all(engine)
    yield db_path
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def _all(model):
    factory = admin_db.get_session_factory()
    with factory() as s:
        return s.scalars(select(model)).all()


def test_seed_inserts_brands_then_sources(tmp_admin_db) -> None:
    report = seed(brand_slug="icon")
    # All 5 brands inserted (Icon + 4 placeholders).
    brands = _all(Brand)
    assert {b.slug for b in brands} == {"icon", "neovox", "creolix", "vilatrix", "nexora"}
    # Three Icon sources.
    sources = _all(Source)
    assert len(sources) == 3
    urls = {s.url for s in sources}
    assert "https://www.privatebankerinternational.com/feed/" in urls


def test_seed_is_idempotent(tmp_admin_db) -> None:
    first = seed(brand_slug="icon")
    assert first.skipped == []  # first run inserts everything
    second = seed(brand_slug="icon")
    assert second.inserted == []
    assert len(second.skipped) == len(first.inserted)
    assert len(_all(Brand)) == 5
    assert len(_all(Source)) == 3
    # writer_polish + writer_draft + writer_translate (NTS_065).
    assert len(_all(Prompt)) == 3
    assert len(_all(PipelineConfig)) == 1


def test_seed_marks_one_writer_polish_prompt_active(tmp_admin_db) -> None:
    seed(brand_slug="icon")
    prompts = _all(Prompt)
    actives = [p for p in prompts if p.is_active]
    # One active per seeded type: draft + polish + translate (NTS_065).
    assert len(actives) == 3
    types_active = sorted(p.prompt_type for p in actives)
    assert types_active == ["writer_draft", "writer_polish", "writer_translate"]


def test_seed_writes_pipeline_config_with_yaml_and_banned_phrases(
    tmp_admin_db,
) -> None:
    seed(brand_slug="icon")
    configs = _all(PipelineConfig)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.scoring_threshold == 7
    assert cfg.topics_per_run == 3
    banned = json.loads(cfg.banned_phrases)
    assert isinstance(banned, list)
    assert "moreover" in banned or "ever-evolving" in banned
    assert "mission" in cfg.voice_profile.lower()


def test_seed_dry_run_changes_nothing(tmp_admin_db) -> None:
    report = seed(brand_slug="icon", dry_run=True)
    assert report.inserted
    assert _all(Brand) == []
    assert _all(Source) == []
    assert _all(Prompt) == []
    assert _all(PipelineConfig) == []
