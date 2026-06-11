"""NTS_056 Task 5 — multilingual fanout regression smoke (HTTP path).

Guards the NTS_055 regression at the API boundary: a cron-triggered
``POST /sources/run-all`` for the Icon brand (languages = en/ru/uk/pl) must
produce a run whose ``triggered_by='cron'`` and whose ``languages_completed``
covers all four languages — not EN-only.

The pipeline's internal fanout (RSS → OpenAI → Sanity) is mocked in
``tests/unit/test_run_pipeline_fanout.py``; here we stub the worker but drive
the language roster through the *real* ``_languages_for_brand`` resolver so
the assertion reflects production logic rather than a hardcoded list.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import Brand, Run, Source
from pipeline.common import config as config_module
from pipeline.run import _languages_for_brand
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-fanout"
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
            )
        )
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


def _stub_fanout_via_real_resolver(monkeypatch):
    """Stub the worker: resolve languages with prod logic, record completion."""

    async def fake_execute(run_id: int) -> None:
        factory = admin_db.get_session_factory()
        with factory() as session:
            run = session.get(Run, run_id)
            brand = session.get(Brand, run.brand_id_fk)
            langs = [lang.value for lang in _languages_for_brand(brand)]
            run.languages_completed = json.dumps(langs)
            run.status = "success"
            session.commit()

    monkeypatch.setattr(admin_jobs, "execute_pipeline_run", fake_execute)


def test_cron_run_all_fans_out_to_all_four_languages(
    monkeypatch, client_and_icon
) -> None:
    client, icon_id = client_and_icon
    _stub_fanout_via_real_resolver(monkeypatch)

    resp = client.post(
        "/api/v1/sources/run-all",
        headers={**AUTH, "X-Triggered-By": "cron"},
        json={"brand_id": icon_id},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]

    # Read back through the public runs API (the same shape the UI consumes).
    detail = client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
    assert detail.status_code == 200, detail.text
    run = detail.json()["run"]
    assert run["triggered_by"] == "cron"
    assert run["languages_completed"] == ICON_LANGS


def test_single_language_brand_does_not_fan_out(monkeypatch, client_and_icon) -> None:
    client, icon_id = client_and_icon
    # Narrow the brand to EN-only and confirm the resolver respects it.
    factory = admin_db.get_session_factory()
    with factory() as session:
        session.get(Brand, icon_id).languages = json.dumps(["en"])
        session.commit()
    _stub_fanout_via_real_resolver(monkeypatch)

    resp = client.post(
        "/api/v1/sources/run-all",
        headers={**AUTH, "X-Triggered-By": "cron"},
        json={"brand_id": icon_id},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    with factory() as session:
        assert json.loads(session.get(Run, run_id).languages_completed) == ["en"]
