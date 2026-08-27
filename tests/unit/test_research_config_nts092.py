"""IT_PROJ_NTS_092 — research budgets live in pipeline_config, not in code.

The three budgets the research call runs under (max sources, token ceiling,
timeout) plus the master switch have to be tunable from Settings without a
deploy, which means the value written through the API must be the value the
next run reads. This project has twice shipped a config surface nothing read,
so the chain is asserted end to end: migration → ORM → ConfigRecord → API.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.config_client import AdminConfigClient
from pipeline.admin.models import PipelineConfig
from pipeline.common import config as config_module
from pipeline.generator.research import ResearchBudget
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-nts092"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}

_DEFAULTS = {
    "research_enabled": True,
    "research_max_sources": 5,
    "research_max_tokens": 2000,
    "research_timeout_seconds": 60,
}


# --- migration 018 --------------------------------------------------------


def test_migration_018_research_budgets_round_trip(tmp_path: Path) -> None:
    """upgrade head → four columns, NOT NULL, with the documented defaults →
    re-upgrade is a no-op → downgrade drops all four → re-upgrade restores."""
    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-018.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(test_db),
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

    def cols() -> dict[str, tuple]:
        with sqlite3.connect(test_db) as conn:
            return {
                r[1]: r
                for r in conn.execute("PRAGMA table_info(pipeline_config)").fetchall()
            }

    expected_defaults = {
        "research_enabled": "1",
        "research_max_sources": "5",
        "research_max_tokens": "2000",
        "research_timeout_seconds": "60",
    }

    alembic("upgrade", "head")
    present = cols()
    for name, default in expected_defaults.items():
        col = present.get(name)
        assert col is not None, f"018 did not add {name}"
        assert col[3] == 1, f"{name} must be NOT NULL"
        assert default in str(col[4]), f"{name} default {col[4]!r} != {default}"

    # Idempotent: a partially applied deploy can be re-run.
    alembic("upgrade", "head")
    assert set(expected_defaults) <= set(cols())

    alembic("downgrade", "017_images_on_demand")
    assert not (set(expected_defaults) & set(cols()))

    alembic("upgrade", "head")
    assert set(expected_defaults) <= set(cols())


def test_migration_018_leaves_the_nts094_column_alone(tmp_path: Path) -> None:
    """Downgrading 018 must not take ``images_on_demand`` with it — batch
    ALTER on SQLite rebuilds the table, which is exactly how a neighbouring
    column goes missing."""
    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-018-neighbour.db"
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "ADMIN_DB_PATH": str(test_db),
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

    def colnames() -> set[str]:
        with sqlite3.connect(test_db) as conn:
            return {
                r[1]
                for r in conn.execute("PRAGMA table_info(pipeline_config)").fetchall()
            }

    alembic("upgrade", "head")
    alembic("downgrade", "017_images_on_demand")
    after = colnames()
    for survivor in (
        "images_on_demand",
        "eval_enabled",
        "dedup_threshold",
        "stale_draft_days",
        "voice_profile",
    ):
        assert survivor in after, f"018 downgrade lost {survivor}"


# --- ORM / ConfigRecord ---------------------------------------------------


@pytest.fixture
def client_and_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("ADMIN_LOG_PATH", str(tmp_path / "missing.log"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session)
        session.add(
            PipelineConfig(
                brand_id_fk=icon_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps(["delve into"]),
                voice_profile="mission: x\n",
            )
        )
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


def test_new_config_row_defaults_to_research_on_with_documented_budgets(
    client_and_brand,
):
    _, icon_id = client_and_brand
    with admin_db.get_session_factory()() as session:
        cfg = session.get(PipelineConfig, icon_id)
        assert cfg is not None
        assert cfg.research_enabled is True
        assert cfg.research_max_sources == 5
        assert cfg.research_max_tokens == 2000
        assert cfg.research_timeout_seconds == 60


def test_config_record_carries_the_budgets(client_and_brand):
    _, icon_id = client_and_brand
    record = AdminConfigClient(brand_slug="icon").get_config()
    assert record.research_enabled is True
    budget = ResearchBudget.from_config(record)
    assert (budget.max_sources, budget.max_tokens, budget.timeout_seconds) == (
        5,
        2000,
        60,
    )


def test_edited_budgets_reach_the_config_record(client_and_brand):
    """The whole point of the columns: change them, the next run obeys."""
    client, icon_id = client_and_brand
    resp = client.put(
        f"/api/v1/config?brand_id={icon_id}",
        headers=AUTH,
        json={
            "research_max_sources": 3,
            "research_max_tokens": 1200,
            "research_timeout_seconds": 25,
        },
    )
    assert resp.status_code == 200, resp.text

    budget = ResearchBudget.from_config(AdminConfigClient(brand_slug="icon").get_config())
    assert (budget.max_sources, budget.max_tokens, budget.timeout_seconds) == (
        3,
        1200,
        25,
    )


def test_research_can_be_switched_off_and_back_on_from_the_api(client_and_brand):
    client, icon_id = client_and_brand
    url = f"/api/v1/config?brand_id={icon_id}"

    assert client.put(url, headers=AUTH, json={"research_enabled": False}).json()[
        "research_enabled"
    ] is False
    assert AdminConfigClient(brand_slug="icon").get_config().research_enabled is False

    assert client.put(url, headers=AUTH, json={"research_enabled": True}).json()[
        "research_enabled"
    ] is True
    assert AdminConfigClient(brand_slug="icon").get_config().research_enabled is True


def test_get_config_exposes_the_budgets(client_and_brand):
    client, icon_id = client_and_brand
    body = client.get(f"/api/v1/config?brand_id={icon_id}", headers=AUTH).json()
    for key, value in _DEFAULTS.items():
        assert body[key] == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("research_max_sources", 0),
        ("research_max_sources", 21),
        ("research_max_tokens", 100),
        ("research_max_tokens", 9000),
        ("research_timeout_seconds", 5),
        ("research_timeout_seconds", 3600),
    ],
)
def test_out_of_range_budgets_are_refused(client_and_brand, field, value):
    """A 6-hour timeout or a 100-token ceiling is a typo, not a policy."""
    client, icon_id = client_and_brand
    resp = client.put(
        f"/api/v1/config?brand_id={icon_id}", headers=AUTH, json={field: value}
    )
    assert resp.status_code == 422


def test_a_partial_put_does_not_reset_the_other_budgets(client_and_brand):
    client, icon_id = client_and_brand
    url = f"/api/v1/config?brand_id={icon_id}"
    client.put(url, headers=AUTH, json={"research_max_sources": 8})
    body = client.put(url, headers=AUTH, json={"research_timeout_seconds": 90}).json()
    assert body["research_max_sources"] == 8
    assert body["research_timeout_seconds"] == 90
    assert body["research_max_tokens"] == 2000
    assert body["scoring_threshold"] == 7
