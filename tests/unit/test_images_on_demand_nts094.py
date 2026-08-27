"""IT_PROJ_NTS_094 — cover images on manager demand.

The flag ``pipeline_config.images_on_demand`` decides whether a pipeline run
pays Replicate for a cover per topic (OFF — how it has always worked) or skips
image generation entirely and leaves the cover to the manager who actually
picks a draft (ON).

What is asserted here, in the order the acceptance criteria state it:

* **Flag OFF ⇒ zero behavioural change.** Proved, not asserted: the run still
  calls the image seam once per topic, still attaches the asset to every
  language sibling, and reports ``images_skipped=0``.
* **Flag ON ⇒ zero Replicate calls.** Proved by booby-trapping every layer
  the generation path would touch (``generate_image_for_topic``,
  ``build_scene_prompt``, ``ImageGenerator``) so entering any of them fails
  the test. Drafts are still produced, with ``cover_image_asset_id=None``.
* The skip is logged as ``image.skipped_on_demand`` — its OWN event, never
  ``image.failed``, so a deliberate skip and a broken generation stay
  distinguishable in the logs and in any alerting built on them.
* The skip count reaches run stats and the Telegram run summary.
* The value written through the Settings API is the value the next run reads
  (this project has twice shipped a config surface nothing read).
"""

# ruff: noqa: F811 — importing a pytest fixture by name IS the redefinition
# ruff sees; the fixture is shared with test_run_pipeline_fanout on purpose.

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.config_client import AdminConfigClient
from pipeline.admin.models import PipelineConfig, Run
from pipeline.common import config as config_module
from pipeline.monitoring.alerts import format_run_finished
from tests.unit.test_run_pipeline_fanout import (  # noqa: F401 — fixture import
    _mock_externals,
    _set_brand_languages,
    fresh_admin_db_with_source,
)

ADMIN_TOKEN = "tok-nts094"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


# --- helpers --------------------------------------------------------------


class _LogRecorder:
    """Stand-in for ``run.log`` that records ``(event, kwargs)`` per level.

    Asserting on the module logger rather than on structlog's global config
    keeps the test honest: ``run_pipeline`` calls ``configure_logging()``,
    which re-``structlog.configure``s and would wipe a global capture.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    def _record(self, level: str):
        def emit(event: str, **kw) -> None:
            self.entries.append((level, event, kw))

        return emit

    def __getattr__(self, level: str):
        return self._record(level)

    def events(self, level: str | None = None) -> list[str]:
        return [e for lv, e, _ in self.entries if level in (None, lv)]

    def kwargs_for(self, event: str) -> list[dict]:
        return [kw for _, e, kw in self.entries if e == event]


@pytest.fixture
def admin_client(fresh_admin_db_with_source, monkeypatch):
    """Authenticated TestClient over the same admin.db the pipeline reads."""
    from fastapi.testclient import TestClient

    from pipeline.admin.server import create_app

    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv(
        "ADMIN_LOG_PATH", str(fresh_admin_db_with_source["path"].parent / "missing.log")
    )
    monkeypatch.setattr(config_module, "_settings", None)
    return TestClient(create_app())


def _set_images_on_demand(brand_id: int, value: bool) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        cfg = session.get(PipelineConfig, brand_id)
        assert cfg is not None
        cfg.images_on_demand = value
        session.commit()


def _booby_trap_image_generation(monkeypatch) -> None:
    """Make ANY step of cover generation an immediate test failure.

    Covers all three layers so the proof does not rest on a single seam:
    the per-topic helper, the gpt-4o-mini scene brief, and the Flux client.
    """
    from pipeline import run as pipe

    async def _no_images(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError(
            "cover generation was entered while images_on_demand is ON"
        )

    def _no_generator(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError(
            "ImageGenerator was constructed while images_on_demand is ON"
        )

    monkeypatch.setattr(pipe, "generate_image_for_topic", _no_images)
    monkeypatch.setattr(pipe, "build_scene_prompt", _no_images)
    monkeypatch.setattr(pipe, "ImageGenerator", _no_generator)


def _run_stats(session) -> dict:
    runs = list(session.scalars(select(Run)))
    assert len(runs) == 1
    return json.loads(runs[0].stats)


# --- migration 017 --------------------------------------------------------


def test_migration_017_images_on_demand_round_trip(tmp_path: Path) -> None:
    """upgrade head → column present, default 0 → downgrade → re-upgrade.

    The default matters as much as the column: applying 017 must leave every
    existing brand generating covers exactly as before.
    """
    project_root = Path(__file__).resolve().parents[2]
    test_db = tmp_path / "alembic-017.db"
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
                for r in conn.execute(
                    "PRAGMA table_info(pipeline_config)"
                ).fetchall()
            }

    alembic("upgrade", "head")
    col = cols().get("images_on_demand")
    assert col is not None, "017 did not add images_on_demand"
    assert col[3] == 1, "images_on_demand must be NOT NULL"
    assert "0" in str(col[4]), f"default must be false, got {col[4]!r}"

    # Re-running the upgrade is a no-op (idempotent), not a duplicate-column
    # error — a partially applied deploy can be re-run.
    alembic("upgrade", "head")
    assert "images_on_demand" in cols()

    alembic("downgrade", "016_draft_scores")
    assert "images_on_demand" not in cols()

    alembic("upgrade", "head")
    assert "images_on_demand" in cols()


def test_new_config_row_defaults_to_off(fresh_admin_db_with_source) -> None:
    """The ORM default matches the migration's: covers are generated."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        cfg = session.get(PipelineConfig, icon_id)
        assert cfg is not None
        assert cfg.images_on_demand is False


# --- flag OFF: prove nothing changed --------------------------------------


def test_flag_off_still_generates_one_cover_per_topic(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """Default state = today's behaviour, unchanged.

    One image per topic (NTS_069), shared across all four language siblings,
    and nothing reported as skipped.
    """
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    # 2 topics → 2 image calls, one per topic, NOT one per (topic, language).
    assert len(fake_sanity.image_call_log) == 2
    assert len(set(fake_sanity.image_call_log)) == 2

    # 8 drafts, every one carrying its topic's shared asset (NTS_069).
    assert len(fake_sanity.created) == 8
    by_topic: dict[str, set[str]] = {}
    for post in fake_sanity.created:
        assert post.cover_image_asset_id is not None
        by_topic.setdefault(post.topic_id, set()).add(post.cover_image_asset_id)
    assert all(len(assets) == 1 for assets in by_topic.values())

    factory = admin_db.get_session_factory()
    with factory() as session:
        assert _run_stats(session).get("images_skipped", 0) == 0


# --- flag ON: prove zero spend --------------------------------------------


def test_flag_on_makes_zero_replicate_calls_and_still_drafts(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The whole point: drafts, no images, no spend."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)
    _set_images_on_demand(icon_id, True)
    # Applied AFTER _mock_externals so the trap replaces its fake seam.
    _booby_trap_image_generation(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    # Drafts are unaffected — 2 topics × 4 languages.
    assert len(fake_sanity.created) == 8
    # …and every one is written with the cover deliberately absent.
    assert all(post.cover_image_asset_id is None for post in fake_sanity.created)
    # The seam that costs money was never entered.
    assert fake_sanity.image_call_log == []


def test_flag_on_counts_skips_in_run_stats(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """One skip per TOPIC (not per language) lands in ``runs.stats``."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    _mock_externals(monkeypatch)
    _set_images_on_demand(icon_id, True)
    _booby_trap_image_generation(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    factory = admin_db.get_session_factory()
    with factory() as session:
        stats = _run_stats(session)
        assert stats["images_skipped"] == 2
        assert stats["drafted"] == 8


def test_skip_is_its_own_log_event_not_image_failed(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """A deliberate skip must never masquerade as a broken generation.

    Alerting is built on these event names — conflating them would either
    page someone every run or hide a real image outage.
    """
    from pipeline import run as pipe

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    _mock_externals(monkeypatch)
    _set_images_on_demand(icon_id, True)
    _booby_trap_image_generation(monkeypatch)

    recorder = _LogRecorder()
    monkeypatch.setattr(pipe, "log", recorder)

    asyncio.run(pipe.run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    events = recorder.events()
    assert events.count("image.skipped_on_demand") == 2, events
    # info, not warning/error — a skip is a normal step, not an incident.
    assert recorder.events("info").count("image.skipped_on_demand") == 2
    assert all(kw.get("topic") for kw in recorder.kwargs_for("image.skipped_on_demand"))
    # The two failure events must NOT appear: alerting distinguishes them.
    assert "image.failed" not in events
    assert "image.unexpected_failure" not in events


def test_flag_off_emits_no_skip_event(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The mirror image: nothing claims a skip when nothing was skipped."""
    from pipeline import run as pipe

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    _mock_externals(monkeypatch)

    recorder = _LogRecorder()
    monkeypatch.setattr(pipe, "log", recorder)

    asyncio.run(pipe.run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    assert "image.skipped_on_demand" not in recorder.events()


# --- the config surface actually reaches the runtime ----------------------


def test_config_client_reads_the_column(fresh_admin_db_with_source) -> None:
    icon_id = fresh_admin_db_with_source["icon_id"]
    assert AdminConfigClient(brand_slug="icon").get_config().images_on_demand is False
    _set_images_on_demand(icon_id, True)
    assert AdminConfigClient(brand_slug="icon").get_config().images_on_demand is True


def test_settings_api_write_is_what_the_next_run_reads(
    fresh_admin_db_with_source, admin_client, monkeypatch
) -> None:
    """NTS_094 Task B acceptance — the UI edit reaches generation.

    Twice now this project has shipped a config surface that wrote to a
    column nothing read. This walks the whole chain the Settings toggle
    walks: PUT /api/v1/config → pipeline_config row → ConfigRecord →
    ``run_pipeline`` → no image call.
    """
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    client = admin_client

    before = client.get(f"/api/v1/config?brand_id={icon_id}", headers=AUTH)
    assert before.status_code == 200, before.text
    assert before.json()["images_on_demand"] is False

    # 1. The write the Settings form performs.
    resp = client.put(
        f"/api/v1/config?brand_id={icon_id}",
        json={"images_on_demand": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["images_on_demand"] is True

    # 2. The read the pipeline performs.
    assert AdminConfigClient(brand_slug="icon").get_config().images_on_demand is True

    # 3. What the next run actually does with it.
    fake_sanity = _mock_externals(monkeypatch)
    _booby_trap_image_generation(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=1, dry_run=False))
    assert fake_sanity.image_call_log == []
    assert fake_sanity.created, "drafts must still be produced"

    # 4. …and toggling it back restores generation, same chain.
    assert (
        client.put(
            f"/api/v1/config?brand_id={icon_id}",
            json={"images_on_demand": False},
            headers=AUTH,
        ).json()["images_on_demand"]
        is False
    )
    assert AdminConfigClient(brand_slug="icon").get_config().images_on_demand is False


def test_config_put_leaves_other_tunables_alone(
    fresh_admin_db_with_source, admin_client
) -> None:
    """A partial PUT of the new field must not reset its neighbours."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    client = admin_client
    original = client.get(f"/api/v1/config?brand_id={icon_id}", headers=AUTH).json()

    updated = client.put(
        f"/api/v1/config?brand_id={icon_id}",
        json={"images_on_demand": True},
        headers=AUTH,
    ).json()

    for key in (
        "scoring_threshold",
        "topics_per_run",
        "stale_draft_days",
        "dedup_enabled",
        "eval_enabled",
        "eval_threshold",
    ):
        assert updated[key] == original[key], key


def test_settings_form_payload_shape_is_accepted(
    fresh_admin_db_with_source, admin_client
) -> None:
    """The Settings form PUTs its WHOLE value set, not just the changed field.

    ``PipelineConfigUpdate`` is ``extra="forbid"``, so a key the form sends
    that the model does not know is a 422 on save — the realistic way this
    toggle would ship broken. Mirrors the zod schema in
    ``app/(admin)/settings/settings-client.tsx``; add a field there, add it
    here.
    """
    icon_id = fresh_admin_db_with_source["icon_id"]
    payload = {
        "scoring_threshold": 7,
        "topics_per_run": 3,
        "stale_draft_days": 3,
        "dedup_enabled": True,
        "dedup_threshold": 0.85,
        "dedup_window_days": 7,
        "eval_enabled": True,
        "eval_threshold": 7.0,
        "images_on_demand": True,
        "voice_profile": "mission: edited via settings\n",
    }
    resp = admin_client.put(
        f"/api/v1/config?brand_id={icon_id}", json=payload, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["images_on_demand"] is True
    # …and the pipeline reads back exactly what the form wrote.
    assert AdminConfigClient(brand_slug="icon").get_config().images_on_demand is True


# --- Telegram run summary -------------------------------------------------


def _finished(**kw) -> str:
    base = {
        "run_id": 7,
        "status": "success",
        "fetched": 10,
        "relevant": 4,
        "drafted": 8,
        "finished_at": datetime(2026, 8, 27, 9, 30, tzinfo=timezone.utc),
    }
    base.update(kw)
    return format_run_finished(**base)


def test_summary_reports_skipped_covers() -> None:
    assert "covers skipped: 2" in _finished(images_skipped=2)


def test_summary_stays_silent_when_nothing_was_skipped() -> None:
    """An ordinary run's summary must not gain a noise line."""
    assert "covers skipped" not in _finished()
    assert "covers skipped" not in _finished(images_skipped=0)


def test_skip_count_travels_from_the_run_all_the_way_to_telegram(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The whole reporting chain, not just its ends.

    ``_process_source`` stats → ``runs.stats`` JSON → the alert poller →
    the run-finished pulse. Asserting the two halves separately would let a
    renamed stats key slip through the join.
    """
    from pipeline.monitoring.alerts import _gather_run_events

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    _mock_externals(monkeypatch)
    _set_images_on_demand(icon_id, True)
    _booby_trap_image_generation(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    pulses = [msg for key, msg in _gather_run_events(set()) if "завершён" in msg]
    assert len(pulses) == 1, pulses
    assert "covers skipped: 2" in pulses[0]
