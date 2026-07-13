"""Tests for the LLM-judge eval harness (IT_PROJ_NTS_091 / spec NTS_080).

Pure: rubric parse, weighted total, banned penalty, threshold/yellow logic,
deterministic banned scan. Integration (monkeypatched LLM): EN full rubric vs
non-EN reduced rubric, yellow-band escalation gpt-4o→gpt-5.5, and score_draft
persistence + threshold flag + FAIL-OPEN.
"""

from __future__ import annotations

import asyncio

import pytest

from pipeline.admin import db as admin_db
from pipeline.admin import judge
from pipeline.admin.judge import (
    EN_WEIGHTS,
    ESCALATION_MODEL,
    NONEN_WEIGHTS,
    STREAM_MODEL,
    JudgeError,
    apply_banned_penalty,
    deterministic_banned_hits,
    is_yellow,
    parse_scores,
    weighted_total,
    weights_for,
    worst_axis,
)
from pipeline.admin.models import DraftScore
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

EN_AXES = list(EN_WEIGHTS)
NONEN_AXES = list(NONEN_WEIGHTS)


def _payload(scores: dict, feedback: str = "ok", paraphrases=None) -> dict:
    return {"scores": scores, "feedback": feedback, "banned_paraphrases": paraphrases or []}


# --- pure helpers ----------------------------------------------------------


def test_parse_scores_valid_and_clamped() -> None:
    got = parse_scores(_payload({a: 12 for a in EN_AXES}), EN_AXES)  # 12 → clamp 10
    assert all(v == 10.0 for v in got.values())
    assert set(got) == set(EN_AXES)


def test_parse_scores_missing_axis_raises() -> None:
    with pytest.raises(JudgeError):
        parse_scores(_payload({"factuality": 8}), EN_AXES)


def test_parse_scores_non_numeric_raises() -> None:
    bad = _payload({a: 7 for a in EN_AXES})
    bad["scores"]["factuality"] = "high"
    with pytest.raises(JudgeError):
        parse_scores(bad, EN_AXES)


def test_weighted_total_en_and_nonen() -> None:
    en = {a: 8.0 for a in EN_AXES}
    assert weighted_total(en, EN_WEIGHTS) == 8.0
    non = {a: 6.0 for a in NONEN_AXES}
    assert weighted_total(non, NONEN_WEIGHTS) == 6.0


def test_weights_for_lang() -> None:
    assert weights_for("en") == EN_WEIGHTS
    assert weights_for("ru") == NONEN_WEIGHTS


def test_apply_banned_penalty_caps_axis() -> None:
    axes = {"translation_fidelity": 9.0, "banned_leakage": 10.0}
    out = apply_banned_penalty(axes, hit_count=2)  # 10 - 3*2 = 4
    assert out["banned_leakage"] == 4.0
    assert apply_banned_penalty(axes, 0)["banned_leakage"] == 10.0


def test_worst_axis() -> None:
    assert worst_axis({"a": 9, "b": 3, "c": 7}) == "b"


def test_is_yellow_band() -> None:
    assert is_yellow(7.0, 7.0)
    assert is_yellow(6.1, 7.0)
    assert is_yellow(7.9, 7.0)
    assert not is_yellow(5.0, 7.0)
    assert not is_yellow(9.0, 7.0)


def test_deterministic_banned_hits() -> None:
    hits = deterministic_banned_hits("Let's delve into the topic", ["delve into"])
    assert hits
    assert deterministic_banned_hits("clean text", ["delve into"]) == []


# --- run_judge (monkeypatched LLM) -----------------------------------------


@pytest.fixture
def no_cost(monkeypatch):
    # run_judge with brand_id_fk=None skips cost; keep tests DB-free here.
    yield


def test_run_judge_en_uses_full_rubric(monkeypatch, no_cost) -> None:
    async def fake_4o(system, payload, schema):
        assert "SOURCE" in payload  # EN payload carries the source
        return _payload({a: 8 for a in EN_AXES}), 100, 40

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    res = asyncio.run(judge.run_judge(draft_text="Body", lang="en", source_text="src", model=STREAM_MODEL))
    assert set(res.axes) == set(EN_AXES)
    assert res.model == STREAM_MODEL
    assert res.total == 8.0


def test_run_judge_nonen_uses_reduced_rubric(monkeypatch, no_cost) -> None:
    async def fake_4o(system, payload, schema):
        assert "EN CANONICAL" in payload  # non-EN payload carries the EN canon
        return _payload({a: 7 for a in NONEN_AXES}), 80, 30

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    res = asyncio.run(judge.run_judge(draft_text="перевод", lang="ru", en_text="EN body", model=STREAM_MODEL))
    assert set(res.axes) == set(NONEN_AXES)
    assert res.total == 7.0


def test_run_judge_deterministic_banned_dominates(monkeypatch, no_cost) -> None:
    async def fake_4o(system, payload, schema):
        # Judge naively thinks banned is clean (10) but the text has a hit.
        return _payload({**{a: 9 for a in EN_AXES}, "banned_leakage": 10}), 50, 20

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    res = asyncio.run(
        judge.run_judge(
            draft_text="We delve into markets", lang="en", source_text="s",
            banned=["delve into"], model=STREAM_MODEL,
        )
    )
    assert res.banned_hits  # deterministic hit recorded
    assert res.axes["banned_leakage"] <= 7.0  # penalised below the judge's 10


def test_run_judge_bad_payload_raises(monkeypatch, no_cost) -> None:
    async def fake_4o(system, payload, schema):
        return {"nope": True}, 10, 10

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    with pytest.raises(JudgeError):
        asyncio.run(judge.run_judge(draft_text="b", lang="en", model=STREAM_MODEL))


# --- evaluate_draft escalation ---------------------------------------------


def test_evaluate_escalates_in_yellow_band(monkeypatch, no_cost) -> None:
    calls = {"4o": 0, "55": 0}

    async def fake_4o(system, payload, schema):
        calls["4o"] += 1
        return _payload({a: 7 for a in EN_AXES}), 100, 40  # total 7.0 == threshold → yellow

    async def fake_55(system, payload, schema):
        calls["55"] += 1
        return _payload({a: 6 for a in EN_AXES}), 200, 300

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    monkeypatch.setattr(judge, "_call_gpt55", fake_55)
    res = asyncio.run(judge.evaluate_draft(draft_text="b", lang="en", eval_threshold=7.0, source_text="s"))
    assert calls["55"] == 1
    assert res.model == ESCALATION_MODEL
    assert res.total == 6.0


def test_evaluate_no_escalation_when_clear(monkeypatch, no_cost) -> None:
    calls = {"55": 0}

    async def fake_4o(system, payload, schema):
        return _payload({a: 9 for a in EN_AXES}), 100, 40  # total 9.0, far above → no escalate

    async def fake_55(system, payload, schema):
        calls["55"] += 1
        return _payload({a: 5 for a in EN_AXES}), 1, 1

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    monkeypatch.setattr(judge, "_call_gpt55", fake_55)
    res = asyncio.run(judge.evaluate_draft(draft_text="b", lang="en", eval_threshold=7.0, source_text="s"))
    assert calls["55"] == 0
    assert res.model == STREAM_MODEL


# --- score_draft persistence + flag + fail-open ----------------------------


@pytest.fixture
def brand_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")  # no alert in tests
    monkeypatch.setenv("TELEGRAM_MONITORING_CHAT_ID", "")
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as s:
        bid = seed_icon_brand(s)
        s.commit()
    yield bid
    admin_db.reset_for_tests()


def test_score_draft_persists_and_flags_low(monkeypatch, brand_db) -> None:
    async def fake_4o(system, payload, schema):
        return _payload({a: 4 for a in EN_AXES}), 100, 40  # total 4.0 < 7.0 → flagged

    monkeypatch.setattr(judge, "_call_gpt4o", fake_4o)
    monkeypatch.setattr(judge, "_call_gpt55", fake_4o)  # escalation also low
    res = asyncio.run(
        judge.score_draft(
            draft_id="drafts.post-x", lang="en", draft_text="body",
            eval_enabled=True, eval_threshold=7.0, source_text="s",
            brand_id_fk=brand_db,
        )
    )
    assert res is not None and res.total == 4.0
    with admin_db.get_session_factory()() as s:
        rows = s.query(DraftScore).all()
    assert len(rows) == 1
    assert rows[0].flagged is True and rows[0].draft_id == "drafts.post-x"
    assert rows[0].judge_prompt_version == judge.JUDGE_PROMPT_VERSION


def test_score_draft_disabled_is_noop(monkeypatch, brand_db) -> None:
    res = asyncio.run(
        judge.score_draft(
            draft_id="d", lang="en", draft_text="b", eval_enabled=False,
            eval_threshold=7.0, brand_id_fk=brand_db,
        )
    )
    assert res is None
    with admin_db.get_session_factory()() as s:
        assert s.query(DraftScore).count() == 0


def test_score_draft_fails_open_on_judge_error(monkeypatch, brand_db) -> None:
    async def boom(system, payload, schema):
        raise JudgeError("model down")

    monkeypatch.setattr(judge, "_call_gpt4o", boom)
    monkeypatch.setattr(judge, "_call_gpt55", boom)
    res = asyncio.run(
        judge.score_draft(
            draft_id="d", lang="en", draft_text="b", eval_enabled=True,
            eval_threshold=7.0, source_text="s", brand_id_fk=brand_db,
        )
    )
    assert res is None  # fail-open: no crash, no score
    with admin_db.get_session_factory()() as s:
        assert s.query(DraftScore).count() == 0


# --- config + measurability endpoint ---------------------------------------


def test_config_update_validates_eval_threshold() -> None:
    from pydantic import ValidationError

    from pipeline.admin.schemas import PipelineConfigUpdate

    assert PipelineConfigUpdate(eval_threshold=7.5).eval_threshold == 7.5
    assert PipelineConfigUpdate(eval_enabled=False).eval_enabled is False
    with pytest.raises(ValidationError):
        PipelineConfigUpdate(eval_threshold=11.0)


def test_eval_summary_endpoint_aggregates(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from pipeline.admin import encryption as enc_mod

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", "tok-eval")
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as s:
        bid = seed_icon_brand(s)
        for i, total in enumerate((8.0, 6.0)):  # one clean, one flagged
            s.add(
                DraftScore(
                    draft_id=f"drafts.post-{i}", brand_id_fk=bid, lang="en",
                    rubric_json="{}", total=total, flagged=(total < 7.0),
                    model="gpt-4o", judge_prompt_version="v1",
                    created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                )
            )
        s.commit()

    from pipeline.admin.server import create_app

    client = TestClient(create_app())
    resp = client.get(
        f"/api/v1/eval/summary?brand_id={bid}", headers={"X-Admin-Token": "tok-eval"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    v1 = next(r for r in body["by_version"] if r["key"] == "v1")
    assert v1["n"] == 2 and v1["flagged"] == 1
    assert v1["avg_total"] == pytest.approx(7.0)
    assert len(body["by_week"]) == 1
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()
