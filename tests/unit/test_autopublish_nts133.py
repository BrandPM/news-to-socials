"""NTS_133 P5 Ф2.3 — autopublish: the rule, the slot, the journal."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from pipeline import autopublish as ap
from pipeline.admin import db as admin_db
from pipeline.admin.models import Brand, Candidate, PipelineConfig, Run
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

# Thursday 2026-10-15, 10:00 in Madrid — Icon's Thursday slot (capacity 2).
THURSDAY = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
TUESDAY = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)

CLEAN = {
    "status": "drafted",
    "attribution": {"confirmed": 14, "distorted": 0, "uncovered": 0},
    "languages": ["en", "de", "ru", "uk"],
    "words_en": 812,
    "needs_attention": False,
}


# --- the rule ---------------------------------------------------------------


def test_a_clean_draft_passes():
    decision = ap.decide(CLEAN, candidate_id=1)
    assert decision.publish and decision.reasons == ()


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"attribution": {"distorted": 1, "uncovered": 0}}, "distorted: 1"),
        ({"attribution": {"distorted": 0, "uncovered": 2}}, "uncovered: 2"),
        ({"attribution": None}, "attribution not checked"),
        ({"languages": ["en", "de", "ru"]}, "missing languages: uk"),
        ({"words_en": 599}, "words: 599 < 600"),
        ({"needs_attention": True}, "needs_attention"),
    ],
)
def test_any_one_defect_holds_the_draft(patch, reason):
    decision = ap.decide({**CLEAN, **patch}, candidate_id=1)
    assert not decision.publish
    assert reason in decision.reasons


def test_every_reason_is_reported_not_only_the_first():
    entry = {**CLEAN, "words_en": 300, "languages": ["en"]}
    reasons = ap.decide(entry, candidate_id=1).reasons
    assert len(reasons) == 2


def test_no_production_record_is_a_no():
    assert not ap.decide(None, candidate_id=1).publish


def test_slot_capacity_reads_the_brand_calendar():
    slots = [{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]
    assert ap.slot_capacity_today(slots, THURSDAY.date()) == 2
    assert ap.slot_capacity_today(json.dumps(slots), TUESDAY.date()) == 0


# --- the pass ---------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        brand_id = seed_icon_brand(session, with_sanity_creds=True)
        session.get(Brand, brand_id).languages = json.dumps(["en", "de", "ru", "uk"])
        session.add(
            PipelineConfig(
                brand_id_fk=brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases="[]",
                voice_profile="mission: x\n",
                autopublish_enabled=True,
            )
        )
        session.commit()
    yield brand_id
    admin_db.reset_for_tests()


def _drafted(brand_id: int, cid: int) -> None:
    with admin_db.get_session_factory()() as session:
        session.add(
            Candidate(
                id=cid,
                brand_id_fk=brand_id,
                input_kind="document",
                source_title=f"Candidate {cid}",
                verdict="accept",
                reason_code="ok",
                reason="in scope",
                status="drafted",
                sanity_draft_id=f"drafts.c{cid}",
                created_at=THURSDAY.replace(tzinfo=None),
                drafted_at=datetime(2026, 10, 13, 10, cid % 60),
            )
        )
        session.commit()


def _production_run(brand_id: int, entries: list[dict]) -> None:
    with admin_db.get_session_factory()() as session:
        session.add(
            Run(
                brand_id_fk=brand_id,
                triggered_by="cron",
                source_ids="[]",
                started_at=TUESDAY,
                finished_at=TUESDAY,
                status="success",
                run_type="production",
                stats=json.dumps({"candidates": entries}),
            )
        )
        session.commit()


async def test_the_pass_publishes_clean_drafts_up_to_the_slot_and_journals_all(db):
    for cid in (11, 12, 13, 14):
        _drafted(db, cid)
    _production_run(
        db,
        [
            {**CLEAN, "candidate_id": 11},
            {**CLEAN, "candidate_id": 12, "words_en": 410},
            {**CLEAN, "candidate_id": 13},
            {**CLEAN, "candidate_id": 14},
        ],
    )
    published: list[int] = []

    async def publish(brand_id, candidate_id, draft_id):
        assert draft_id == f"drafts.c{candidate_id}"
        published.append(candidate_id)

    result = await ap.run_autopublish("icon", now=THURSDAY, publish=publish)
    # Capacity 2 on Thursday: 11 and 13 go, 12 is held for its length, 14
    # passed the rule but the slot is full.
    assert published == [11, 13]
    assert result.capacity == 2
    held = {d.candidate_id: d for d in result.decisions}
    assert not held[12].publish and "words: 410 < 600" in held[12].reasons
    with admin_db.get_session_factory()() as session:
        run = session.query(Run).filter(Run.run_type == "publish").one()
        assert run.status == "success"
        assert "#12: held — words: 410 < 600" in run.log_excerpt
        assert "#11: published" in run.log_excerpt


async def test_no_slot_today_means_nothing_happens(db):
    _drafted(db, 21)
    _production_run(db, [{**CLEAN, "candidate_id": 21}])

    async def publish(*_a):
        raise AssertionError("must not publish on a day without a slot")

    result = await ap.run_autopublish("icon", now=TUESDAY, publish=publish)
    assert result.skipped == "no slot today"


async def test_a_failed_publish_does_not_stop_the_pass(db):
    for cid in (31, 32):
        _drafted(db, cid)
    _production_run(db, [{**CLEAN, "candidate_id": 31}, {**CLEAN, "candidate_id": 32}])
    published: list[int] = []

    async def publish(brand_id, candidate_id, draft_id):
        if candidate_id == 31:
            raise RuntimeError("Sanity 502")
        published.append(candidate_id)

    result = await ap.run_autopublish("icon", now=THURSDAY, publish=publish)
    assert published == [32]
    assert "Sanity 502" in result.failed[31]


async def test_publish_goes_through_the_editor_path_as_system(db, monkeypatch):
    """The journal row says ``system``: an article the system published must
    never read as one an editor approved."""
    from pipeline.admin.routes import drafts as drafts_route

    seen: dict = {}

    async def fake_topic(brand_id, draft_id):
        return "topic-1"

    async def fake_siblings(topic_id, brand_id, *, reviewer, note):
        seen.update(topic_id=topic_id, reviewer=reviewer, note=note)
        return drafts_route.BatchApprovalOut(
            topic_id=topic_id, ok_count=4, fail_count=0, results=[]
        )

    monkeypatch.setattr(ap, "_topic_of", fake_topic)
    monkeypatch.setattr(drafts_route, "publish_topic_siblings", fake_siblings)
    await ap._publish(db, 41, "drafts.c41")
    assert seen["reviewer"] == "system"
    assert seen["topic_id"] == "topic-1"
