"""Tests for the seed_admin_db idempotent seed script."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.models import PipelineConfig, Prompt, Source
from scripts.seed_admin_db import seed


@pytest.fixture
def tmp_admin_db(tmp_path, monkeypatch):
    """Bind the admin engine to a fresh tmp DB for the duration of a test."""
    db_path = tmp_path / "admin.db"
    monkeypatch.setenv("ADMIN_DB_PATH", str(db_path))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=db_path)
    admin_db.Base.metadata.create_all(engine)
    yield db_path
    admin_db.reset_for_tests()


def _all(model):
    factory = admin_db.get_session_factory()
    with factory() as s:
        return s.scalars(select(model)).all()


def test_seed_inserts_three_sources(tmp_admin_db) -> None:
    report = seed(brand_id="icon")
    assert any("Private Banker" in line for line in report.inserted)
    sources = _all(Source)
    assert len(sources) == 3
    urls = {s.url for s in sources}
    assert "https://www.privatebankerinternational.com/feed/" in urls


def test_seed_is_idempotent(tmp_admin_db) -> None:
    first = seed(brand_id="icon")
    assert first.skipped == []  # first run inserts everything
    second = seed(brand_id="icon")
    assert second.inserted == []
    assert len(second.skipped) == len(first.inserted)
    # Counts are unchanged.
    assert len(_all(Source)) == 3
    assert len(_all(Prompt)) == 2
    assert len(_all(PipelineConfig)) == 1


def test_seed_marks_one_writer_polish_prompt_active(tmp_admin_db) -> None:
    seed(brand_id="icon")
    prompts = _all(Prompt)
    actives = [p for p in prompts if p.is_active]
    # writer_polish + writer_draft each get one active prompt.
    assert len(actives) == 2
    types_active = sorted(p.prompt_type for p in actives)
    assert types_active == ["writer_draft", "writer_polish"]


def test_seed_writes_pipeline_config_with_yaml_and_banned_phrases(
    tmp_admin_db,
) -> None:
    seed(brand_id="icon")
    configs = _all(PipelineConfig)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.scoring_threshold == 7
    assert cfg.topics_per_run == 3
    # banned_phrases is a JSON array sourced from the voice YAML.
    banned = json.loads(cfg.banned_phrases)
    assert isinstance(banned, list)
    assert "moreover" in banned or "ever-evolving" in banned
    # voice_profile is the raw YAML — contains the brand mission line.
    assert "mission" in cfg.voice_profile.lower()


def test_seed_dry_run_changes_nothing(tmp_admin_db) -> None:
    report = seed(brand_id="icon", dry_run=True)
    # Report claims insertions...
    assert report.inserted
    # ...but the DB is empty afterwards.
    assert _all(Source) == []
    assert _all(Prompt) == []
    assert _all(PipelineConfig) == []
