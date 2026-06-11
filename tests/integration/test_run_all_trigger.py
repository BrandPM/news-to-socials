"""NTS_056 Task 1 — X-Triggered-By audit trail on the run endpoints.

The cron systemd unit POSTs to ``/sources/run-all`` with
``X-Triggered-By: cron``; the admin UI omits the header (→ "manual"). The
endpoint must record that provenance in ``runs.triggered_by`` and reject
any value outside ``{cron, manual, cli}`` with a 400.

The fanout itself (brand.languages → run.languages_completed) is exercised
in ``test_multilang_fanout.py`` and ``tests/unit/test_run_pipeline_fanout.py``;
here we stub ``execute_pipeline_run`` with a faithful stand-in that copies
the brand's language roster into the run so the audit assertion mirrors
production without paying for real LLM calls.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import Brand, Run, Source
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "test-token-123"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}
ICON_LANGS = ["en", "ru", "uk", "pl"]


@pytest.fixture
def client_and_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)

    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        brand = session.get(Brand, icon_id)
        brand.status = "active"
        brand.active = True
        brand.languages = json.dumps(ICON_LANGS)
        session.add(
            Source(
                brand_id_fk=icon_id,
                name="Private Banker International",
                source_type="rss",
                url="https://www.privatebankerinternational.com/feed/",
                primary_category="wealth",
                active=True,
                paywall=False,
                polling_minutes=720,
            )
        )
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


def _stub_fanout(monkeypatch):
    """Stub execute_pipeline_run to simulate a completed multilingual run."""

    async def fake_execute(run_id: int) -> None:
        factory = admin_db.get_session_factory()
        with factory() as session:
            run = session.get(Run, run_id)
            brand = session.get(Brand, run.brand_id_fk)
            run.languages_completed = brand.languages  # JSON-as-TEXT, mirrors prod
            run.status = "success"
            session.commit()

    monkeypatch.setattr(admin_jobs, "execute_pipeline_run", fake_execute)


def test_run_all_cron_header_records_provenance_and_languages(
    monkeypatch, client_and_icon
) -> None:
    client, icon_id = client_and_icon
    _stub_fanout(monkeypatch)

    resp = client.post(
        "/api/v1/sources/run-all",
        headers={**AUTH, "X-Triggered-By": "cron"},
        json={"brand_id": icon_id},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]

    factory = admin_db.get_session_factory()
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.triggered_by == "cron"
        assert json.loads(run.languages_completed) == ICON_LANGS


def test_run_all_defaults_to_manual_without_header(monkeypatch, client_and_icon) -> None:
    client, icon_id = client_and_icon
    _stub_fanout(monkeypatch)

    resp = client.post(
        "/api/v1/sources/run-all", headers=AUTH, json={"brand_id": icon_id}
    )
    assert resp.status_code == 202, resp.text

    factory = admin_db.get_session_factory()
    with factory() as session:
        run = session.get(Run, resp.json()["run_id"])
        assert run.triggered_by == "manual"


def test_run_all_accepts_cli_trigger(monkeypatch, client_and_icon) -> None:
    client, icon_id = client_and_icon
    _stub_fanout(monkeypatch)

    resp = client.post(
        "/api/v1/sources/run-all",
        headers={**AUTH, "X-Triggered-By": "cli"},
        json={"brand_id": icon_id},
    )
    assert resp.status_code == 202, resp.text
    factory = admin_db.get_session_factory()
    with factory() as session:
        assert session.get(Run, resp.json()["run_id"]).triggered_by == "cli"


def test_run_all_rejects_junk_trigger(client_and_icon) -> None:
    client, icon_id = client_and_icon
    resp = client.post(
        "/api/v1/sources/run-all",
        headers={**AUTH, "X-Triggered-By": "junk"},
        json={"brand_id": icon_id},
    )
    assert resp.status_code == 400, resp.text
    assert "X-Triggered-By" in resp.json()["detail"]


def test_single_source_run_honours_trigger_header(monkeypatch, client_and_icon) -> None:
    client, icon_id = client_and_icon
    _stub_fanout(monkeypatch)

    factory = admin_db.get_session_factory()
    with factory() as session:
        source_id = session.scalars(select_first_source(icon_id)).first().id

    resp = client.post(
        f"/api/v1/sources/{source_id}/run",
        headers={**AUTH, "X-Triggered-By": "cron"},
    )
    assert resp.status_code == 202, resp.text
    with factory() as session:
        assert session.get(Run, resp.json()["run_id"]).triggered_by == "cron"


def select_first_source(brand_id: int):
    from sqlalchemy import select

    return select(Source).where(Source.brand_id_fk == brand_id)
