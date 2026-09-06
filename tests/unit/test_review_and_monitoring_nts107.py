"""S7 — the review surface, the recall test and alert delivery (NTS_107, NTS_106).

Four things, each with the failure it exists to catch:

* **Traceability without a paid call** (NTS_096 DoD). The endpoint assembles the
  document, the pack, the plan and the verdicts out of what the run already
  stored. A version that re-derived any of it would pass a "does it return
  data" test and cost money per open.
* **A scoped return** (NTS_100 §5). ``translation:uk`` must reach the candidate
  row, because that is what makes the next run re-do one language; a return
  that only wrote a comment would look identical in the UI and cost a whole
  article.
* **The recall test's two ratios** (NTS_099 §7). They have separate
  denominators on purpose: a subject the feeds never carried says nothing about
  the rubric, and folding it in lets a sourcing gap read as an editorial one.
* **Alert delivery** (NTS_106 §1). The intent is recorded *before* the send,
  so an alert raised while Telegram is down leaves a row to retry rather than
  nothing at all — the state NTS_122 §8 found.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    AlertSent,
    Candidate,
    FactPack,
    PipelineConfig,
    SeedTopic,
)
from pipeline.common import config as config_module
from pipeline.monitoring import alerts as alerts_mod
from pipeline.selector.recall import compute_recall, matches
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-nts107"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        brand_id = seed_icon_brand(session)
        session.add(
            PipelineConfig(
                brand_id_fk=brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases="[]",
                voice_profile="mission: x\n",
            )
        )
        session.commit()
    yield brand_id
    admin_db.reset_for_tests()


@pytest.fixture
def api(db):
    from pipeline.admin.server import create_app

    return TestClient(create_app())


def _candidate(brand_id: int, **kw) -> int:
    fields = {
        "brand_id_fk": brand_id,
        "input_kind": "document",
        "source_title": "ESMA shortens the CASP authorisation clock",
        "source_summary": "The assessment clock falls to 25 working days.",
        "verdict": "accept",
        "reason_code": "ok",
        "reason": "direct consequence for applicants",
        "status": "drafted",
        "created_at": NOW.replace(tzinfo=None),
    }
    fields.update(kw)
    with admin_db.get_session_factory()() as session:
        row = Candidate(**fields)
        session.add(row)
        session.commit()
        return int(row.id)


# --------------------------------------------------------------------------
# traceability (NTS_096 part B)
# --------------------------------------------------------------------------


def test_traceability_assembles_the_whole_chain_from_stored_rows(db, api):
    cid = _candidate(
        db,
        primary_doc_url="https://www.esma.europa.eu/doc",
        doc_match="exact",
        doc_sections_used=json.dumps(["Article 3", "ENTRY INTO FORCE"]),
        depth_prior="deep",
        depth_final="article",
        needs_attention=True,
    )
    with admin_db.get_session_factory()() as session:
        session.add(
            FactPack(
                brand_id_fk=db,
                candidate_id_fk=cid,
                pack=json.dumps(
                    {
                        "empty": False,
                        "source_facts": [
                            {"text": "25 working days", "url": "https://esma.test/d"}
                        ],
                    }
                ),
                sources=json.dumps(["https://esma.test/d"]),
                plan=json.dumps({"sections": [{"heading": "The clock"}], "lede": "L"}),
                attribution=json.dumps(
                    {
                        "checked": True,
                        "error": "",
                        "counts": {
                            "confirmed": 2,
                            "distorted": 1,
                            "uncovered": 0,
                            "person_detail": 1,
                            "quote_too_long": 0,
                        },
                        "claims": [
                            {
                                "claim": "an 18-year tenure at CS and UBS",
                                "verdict": "distorted",
                                "why": "the source says experience, not tenure",
                                "flags": ["person_detail"],
                            }
                        ],
                    }
                ),
                created_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()

    resp = api.get(
        f"/api/v1/candidates/{cid}/traceability?brand_id={db}", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_attention"] is True
    assert body["depth_prior"] == "deep" and body["depth_final"] == "article"
    assert body["document"]["doc_match"] == "exact"
    # The section list is the last link of the chain: what the writer read, and
    # by omission what it did not (NTS_101 §4).
    assert body["document"]["sections_used"] == ["Article 3", "ENTRY INTO FORCE"]
    assert body["plan"]["sections"][0]["heading"] == "The clock"
    assert body["attribution"]["counts"]["distorted"] == 1
    assert body["fact_pack"]["source_facts"][0]["url"] == "https://esma.test/d"


def test_traceability_of_a_v2_article_is_empty_rather_than_an_error(db, api):
    """A pre-v3 draft has no pack, no plan and no document. The card must open
    and say so — an error here would make every legacy article look broken."""
    cid = _candidate(db, primary_doc_url=None)
    body = api.get(
        f"/api/v1/candidates/{cid}/traceability?brand_id={db}", headers=AUTH
    ).json()
    assert body["document"] is None
    assert body["fact_pack"] is None
    assert body["plan"] is None
    assert body["history"] == []


# --------------------------------------------------------------------------
# the scoped return (NTS_100 §5, NTS_107)
# --------------------------------------------------------------------------


def test_a_return_writes_the_scope_to_the_candidate_and_the_log(db, api):
    cid = _candidate(db, status="drafted", publication_slot=NOW.date())
    resp = api.post(
        f"/api/v1/candidates/{cid}/return?brand_id={db}",
        headers=AUTH,
        json={
            "scope": "translation:uk",
            "comment": "Украинский перевод потерял число.",
            "time_spent_s": 480,
        },
    )
    assert resp.status_code == 200, resp.text
    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, cid)
        assert row.status == "returned"
        # The scope on the row is what makes the next run cheap.
        assert row.return_scope == "translation:uk"
        # A slot held by something nobody approved makes the calendar lie.
        assert row.publication_slot is None

    decisions = api.get(
        f"/api/v1/candidates/review-decisions/list?brand_id={db}", headers=AUTH
    ).json()
    assert decisions[0]["action"] == "return"
    assert decisions[0]["scope"] == "translation:uk"
    assert decisions[0]["time_spent_s"] == 480


@pytest.mark.parametrize("scope", ["translation:de", "everything", ""])
def test_an_unknown_scope_is_refused(db, api, scope):
    """A scope the pipeline cannot act on would be saved, displayed and
    silently ignored — the exact shape of failure the config sentinels exist
    to prevent, one layer up."""
    cid = _candidate(db)
    resp = api.post(
        f"/api/v1/candidates/{cid}/return?brand_id={db}",
        headers=AUTH,
        json={"scope": scope, "comment": "no"},
    )
    assert resp.status_code == 422


def test_a_return_needs_a_comment(db, api):
    cid = _candidate(db)
    resp = api.post(
        f"/api/v1/candidates/{cid}/return?brand_id={db}",
        headers=AUTH,
        json={"scope": "text", "comment": ""},
    )
    assert resp.status_code == 422


def test_a_pending_candidate_cannot_be_returned(db, api):
    """Nothing has been written yet, so there is nothing to send back."""
    cid = _candidate(db, status="pending")
    resp = api.post(
        f"/api/v1/candidates/{cid}/return?brand_id={db}",
        headers=AUTH,
        json={"scope": "text", "comment": "no"},
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# the recall test (NTS_099 §7, NTS_114 §Приёмка)
# --------------------------------------------------------------------------


def _seed(brand_id: int, topic: str, keywords: list[str], jurisdiction=None) -> None:
    with admin_db.get_session_factory()() as session:
        session.add(
            SeedTopic(
                brand_id_fk=brand_id,
                topic=topic,
                keywords=json.dumps(keywords),
                jurisdiction=jurisdiction,
                created_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()


def test_the_two_ratios_have_different_denominators(db):
    """The whole point of two numbers: a subject the feeds never carried says
    nothing about the rubric (NTS_099 §7)."""
    _seed(db, "MiCA", ["mica"])
    _seed(db, "CRS", ["crs amendment"])
    _seed(db, "Never in the feed", ["quantum tax"])
    _seed(db, "Also never", ["martian residence"])

    _candidate(db, source_title="ESMA opens MiCA consultation", verdict="accept")
    _candidate(
        db,
        source_title="A CRS amendment lands",
        source_summary="crs amendment detail",
        verdict="reject",
        reason_code="out_of_scope",
        status="rejected",
    )

    report = compute_recall(brand_id_fk=db, now=NOW)
    # Two of four subjects reached the funnel.
    assert report.in_feed_rate == 0.5
    # Of those two, one was accepted — the denominator is *in feed*, not all.
    assert report.accepted_rate == 0.5


def test_a_topic_with_no_channel_is_excluded_from_both_denominators(db):
    """NTS_119 left the EUR-Lex saved searches unbuilt. Counting a subject the
    feed cannot carry as a recall failure blames the guard for a feed nobody
    connected."""
    _seed(db, "DAC8", ["dac8"], jurisdiction="EU")
    _seed(db, "MiCA", ["mica"])
    _candidate(db, source_title="ESMA opens MiCA consultation")

    report = compute_recall(brand_id_fk=db, now=NOW)
    assert len(report.measurable) == 1
    assert report.in_feed_rate == 1.0
    assert [t.channel_missing for t in report.topics if t.topic == "DAC8"] == [True]


def test_a_keyword_matches_on_a_word_boundary(db):
    """Without the boundary "crs" matches "concerns" and every topic reports
    itself in the feed."""
    assert matches("A CRS amendment is due", ["crs"])
    assert not matches("It concerns everyone", ["crs"])
    assert matches("MiCA consultation", ["mica"])


def test_the_report_is_reachable_from_the_api(db, api):
    _seed(db, "MiCA", ["mica"])
    body = api.get(
        f"/api/v1/candidates/recall/report?brand_id={db}&window_days=30",
        headers=AUTH,
    ).json()
    assert body["target_in_feed"] == 0.7
    assert body["target_accepted"] == 0.8
    assert body["topics"][0]["topic"] == "MiCA"


def test_candidates_outside_the_window_do_not_count(db):
    _seed(db, "MiCA", ["mica"])
    _candidate(
        db,
        source_title="ESMA opens MiCA consultation",
        created_at=(NOW - timedelta(days=60)).replace(tzinfo=None),
    )
    report = compute_recall(brand_id_fk=db, window_days=30, now=NOW)
    assert report.in_feed_rate == 0.0


# --------------------------------------------------------------------------
# alert delivery (NTS_106 §1, NTS_122 §8)
# --------------------------------------------------------------------------


def test_the_intent_is_recorded_before_the_send(db):
    """The ordering IS the fix. An alert raised while Telegram is unreachable
    must leave a row saying it needs saying — the state NTS_122 §8 found was
    one where it left nothing."""
    alerts_mod.record_intent("run-42", "🔴 run 42 failed")
    with admin_db.get_session_factory()() as session:
        row = session.get(AlertSent, "run-42")
        assert row is not None
        assert row.delivered is False
        assert row.message == "🔴 run 42 failed"
        assert row.attempts == 0


def test_a_failed_delivery_is_retried_after_ten_minutes_and_not_before(db):
    alerts_mod.record_intent("run-42", "🔴 run 42 failed")
    alerts_mod.mark_delivery("run-42", delivered=False)

    now = datetime.now(tz=UTC)
    assert alerts_mod.pending_deliveries(now=now) == []
    due = alerts_mod.pending_deliveries(now=now + timedelta(minutes=11))
    assert due == [("run-42", "🔴 run 42 failed")]


def test_a_delivered_alert_is_never_retried(db):
    alerts_mod.record_intent("run-42", "message")
    alerts_mod.mark_delivery("run-42", delivered=True)
    assert (
        alerts_mod.pending_deliveries(now=datetime.now(tz=UTC) + timedelta(hours=2))
        == []
    )


def test_retries_stop_at_the_ceiling(db):
    """Past five attempts the channel is down, and retrying is not the problem
    to solve — the dead-man switch covers a channel that stays down."""
    alerts_mod.record_intent("run-42", "message")
    for _ in range(alerts_mod.ALERT_MAX_ATTEMPTS):
        alerts_mod.mark_delivery("run-42", delivered=False)
    assert (
        alerts_mod.pending_deliveries(now=datetime.now(tz=UTC) + timedelta(days=1))
        == []
    )


def test_the_dead_man_switch_fires_on_silence_not_on_failure(db):
    """NTS_106 §5 — a monitoring channel that has gone quiet looks exactly like
    a quiet morning, and telling those apart is the only thing this does."""
    morning = datetime(2026, 9, 7, 8, 30, tzinfo=UTC)  # 10:30 in Madrid
    pulse = alerts_mod.check_dead_man(
        brand_id_fk=db, timezone_name="Europe/Madrid", now=morning
    )
    assert pulse is not None
    assert pulse[0].startswith("dead_man:")
    assert "Сводки сегодня не было" in pulse[1]

    # One delivered message today, and the switch stays quiet.
    alerts_mod.record_intent("intake_heartbeat:1", "утренняя сводка")
    alerts_mod.mark_delivery("intake_heartbeat:1", delivered=True)
    with admin_db.get_session_factory()() as session:
        row = session.get(AlertSent, "intake_heartbeat:1")
        row.sent_at = morning.replace(tzinfo=None)
        session.commit()
    assert (
        alerts_mod.check_dead_man(
            brand_id_fk=db, timezone_name="Europe/Madrid", now=morning
        )
        is None
    )


def test_the_dead_man_switch_stays_quiet_before_nine(db):
    """Before 09:00 the silence is not yet evidence of anything."""
    early = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)  # 07:00 in Madrid
    assert (
        alerts_mod.check_dead_man(
            brand_id_fk=db, timezone_name="Europe/Madrid", now=early
        )
        is None
    )
