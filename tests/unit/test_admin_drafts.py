"""Integration tests for /api/v1/drafts/{id}/regenerate-image."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import jobs as admin_jobs
from pipeline.common import config as config_module

ADMIN_TOKEN = "tok-drafts"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))

    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    admin_jobs.reset_image_jobs_for_tests()

    from pipeline.admin.server import create_app

    yield TestClient(create_app())
    admin_db.reset_for_tests()
    admin_jobs.reset_image_jobs_for_tests()


def test_regenerate_image_returns_202_and_job_completes(monkeypatch, client) -> None:
    captured: dict = {}

    async def fake_regenerate(draft_id: str, custom_prompt):  # noqa: ANN001
        captured["draft_id"] = draft_id
        captured["custom_prompt"] = custom_prompt
        return "image-asset-xyz"

    # Patch the regenerate implementation, not the dispatcher — that way
    # we still exercise the job state-machine.
    from pipeline.admin import image_regenerate

    monkeypatch.setattr(image_regenerate, "regenerate_cover_image", fake_regenerate)

    resp = client.post(
        "/api/v1/drafts/post-abc123/regenerate-image",
        headers=AUTH,
        json={"custom_prompt": "warm marble texture"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert isinstance(job_id, str) and len(job_id) >= 8

    # TestClient drains BackgroundTasks before returning — by now the
    # job should be 'done' with asset_id set.
    resp = client.get(f"/api/v1/drafts/jobs/{job_id}/status", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "done"
    assert body["asset_id"] == "image-asset-xyz"
    assert body["error"] is None
    assert captured["custom_prompt"] == "warm marble texture"


def test_regenerate_image_error_state(monkeypatch, client) -> None:
    async def boom(draft_id: str, custom_prompt):  # noqa: ANN001
        raise RuntimeError("replicate 500")

    from pipeline.admin import image_regenerate

    monkeypatch.setattr(image_regenerate, "regenerate_cover_image", boom)

    resp = client.post(
        "/api/v1/drafts/post-broken/regenerate-image",
        headers=AUTH,
        json={},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    resp = client.get(f"/api/v1/drafts/jobs/{job_id}/status", headers=AUTH)
    body = resp.json()
    assert body["state"] == "error"
    assert "replicate 500" in body["error"]


def test_status_for_unknown_job_is_404(client) -> None:
    resp = client.get("/api/v1/drafts/jobs/nonexistent/status", headers=AUTH)
    assert resp.status_code == 404
