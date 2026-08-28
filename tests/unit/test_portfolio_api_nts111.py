"""The Portfolio / Sources / Editorial Policy API (NTS_111 п. 1-3, NTS_098 §7).

The screens above this are the reason S3 exists: Andriy has to read 50 guard
verdicts during the shadow week, and he reads them here. So the tests are
weighted towards the things that would make that reading wrong rather than
merely ugly:

* **The guard's `reason` and `reason_code` reach the list**, not just the
  detail — a board that hides the reason behind a click is not a rubric review.
* **Manual actions are transitions, not a status PATCH.** Every one is a
  compare-and-set, refuses from the wrong status with a 409, and leaves a
  `review_decisions` row. NTS_113 calls that table the only free signal for
  tuning the rubric; it is only free if writing it is not optional.
* **Promoting a `cap_overflow` reject actually promotes it.** Leaving
  `verdict='reject'` would keep the row out of every accepted-candidate query
  including the guard's own `recent_accepted_titles` — the promotion would look
  applied and not be.
* **Placeholder validation refuses a body that would silently not apply**
  (NTS_071 §2, closing the NTS_063 pending).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin import models
from pipeline.admin.models import (
    BrandTaxonomy,
    Candidate,
    CostRecord,
    PipelineConfig,
    Prompt,
    ReviewDecision,
    Source,
    SourceHealthRecord,
    TopicEmbedding,
)
from pipeline.admin.routes.candidates import next_slots
from pipeline.common import config as config_module
from pipeline.selector.editorial_guard import _GUARD_PROMPT
from tests.unit.conftest import seed_brand, seed_icon_brand

ADMIN_TOKEN = "tok-nts111"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}
NOW = datetime.now(tz=UTC)

_TAXONOMY = (
    ("structuring", "Structuring & Tax", "Residence, CRS/DAC/CARF, UBO", "/t"),
    ("wealth", "Wealth Management", "Private banking, trustee regulation", "/w"),
)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A client, Icon, and a second brand — every read here is brand-scoped and
    a second tenant is the only way to prove it."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("ADMIN_LOG_PATH", str(tmp_path / "missing.log"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session)
        other = seed_brand(session, slug="neovox", name="Neovox")
        for brand_id in (icon_id, other.id):
            session.add(
                PipelineConfig(
                    brand_id_fk=brand_id,
                    scoring_threshold=7,
                    topics_per_run=3,
                    banned_phrases=json.dumps([]),
                    voice_profile="mission: x\n",
                )
            )
        for key, label, description, path in _TAXONOMY:
            session.add(
                BrandTaxonomy(
                    brand_id_fk=icon_id,
                    key=key,
                    label=label,
                    description_for_guard=description,
                    service_url_path=path,
                )
            )
        session.commit()
        other_id = other.id

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id, other_id
    admin_db.reset_for_tests()


def _candidate(
    brand_id: int,
    *,
    title: str = "FINMA revises the trustee circular",
    status: str = "pending",
    verdict: str = "accept",
    reason_code: str = "ok",
    reason: str = "Threshold and effective date bite for structures.",
    input_kind: str = "document",
    service: str | None = "structuring",
    event_stage: str = "adopted",
    cap_overflow: bool = False,
    created_at: datetime | None = None,
    publication_slot: date | None = None,
    source_id: int | None = None,
    embedding: np.ndarray | None = None,
    topic_id: str | None = None,
    primary_doc_url: str | None = None,
    attempts: int = 0,
) -> int:
    with admin_db.get_session_factory()() as session:
        row = Candidate(
            brand_id_fk=brand_id,
            input_kind=input_kind,
            source_id_fk=source_id,
            source_title=title,
            source_summary="The circular takes effect on 1 January 2027.",
            source_url="https://finma.test/circ-2026-3",
            source_published_at=NOW - timedelta(hours=3),
            source_language="de",
            source_name="FINMA News DE",
            source_class="regulator",
            topic_embedding_ref=topic_id,
            verdict=verdict,
            reason_code=reason_code,
            reason=reason,
            confidence=0.82,
            service_category=service,
            jurisdictions=json.dumps(["CH", "EU"]),
            event_stage=event_stage,
            depth_prior="deep",
            status=status,
            cap_overflow=cap_overflow,
            attempts=attempts,
            publication_slot=publication_slot,
            primary_doc_url=primary_doc_url,
            created_at=created_at or NOW,
        )
        session.add(row)
        if embedding is not None and topic_id:
            session.add(
                TopicEmbedding(
                    topic_id=topic_id,
                    brand_id_fk=brand_id,
                    embedding=np.asarray(embedding, dtype=np.float32).tobytes(),
                    model="text-embedding-3-small",
                    title_norm=topic_id,
                    created_at=NOW,
                )
            )
        session.commit()
        return int(row.id)


# --- the vocabulary is one vocabulary ------------------------------------


def test_the_api_literals_match_the_model_constants() -> None:
    """S1 exported the verdict vocabulary as module constants so the guard, the
    API and the UI filters spell it the same way. That only holds if something
    checks — a Literal that drifts turns a legal value into a 422."""
    from typing import get_args

    from pipeline.admin import schemas

    pairs = [
        (schemas.CandidateInputKind, models.CANDIDATE_INPUT_KINDS),
        (schemas.CandidateVerdict, models.CANDIDATE_VERDICTS),
        (schemas.CandidateReasonCode, models.CANDIDATE_REASON_CODES),
        (schemas.CandidateEventStage, models.CANDIDATE_EVENT_STAGES),
        (schemas.CandidateDepth, models.CANDIDATE_DEPTHS),
        (schemas.CandidateStatus, models.CANDIDATE_STATUSES),
        (schemas.CandidateManualAction, models.CANDIDATE_MANUAL_ACTIONS),
        (schemas.ReviewAction, models.REVIEW_ACTIONS),
        (schemas.SourceRole, models.SOURCE_ROLES),
        (schemas.SourceClass, models.SOURCE_CLASSES),
        (schemas.LicenseClass, models.LICENSE_CLASSES),
        (schemas.FetchMethod, models.FETCH_METHODS),
    ]
    for literal, constant in pairs:
        assert set(get_args(literal)) == set(constant), literal


# --- the list ------------------------------------------------------------


def test_the_list_carries_the_guard_reason_on_every_row(api) -> None:
    """NTS_111 §Портфель: «Причина стража — всегда видна, это главный
    инструмент вычитки рубрики»."""
    client, icon, _other = api
    _candidate(icon, reason="Threshold bites for CH structures.")
    _candidate(
        icon,
        title="Bank appoints a new CEO",
        status="rejected",
        verdict="reject",
        reason_code="personnel",
        reason="Appointment with no policy change for clients.",
        service=None,
    )

    got = client.get(
        "/api/v1/candidates", headers=AUTH, params={"brand_id": icon}
    )
    assert got.status_code == 200, got.text
    rows = got.json()
    assert len(rows) == 2
    for row in rows:
        assert row["reason"]
        assert row["reason_code"]
    assert {r["reason_code"] for r in rows} == {"ok", "personnel"}
    # jurisdictions arrive as a list, not as the stored JSON string.
    assert rows[0]["jurisdictions"] == ["CH", "EU"]


@pytest.mark.parametrize(
    ("params", "expected_titles"),
    [
        ({"status": "pending"}, {"Accepted one"}),
        ({"status": "rejected"}, {"Rejected one"}),
        ({"status": "pending,rejected"}, {"Accepted one", "Rejected one"}),
        ({"reason_code": "personnel"}, {"Rejected one"}),
        ({"input_kind": "news"}, {"Rejected one"}),
        ({"service_category": "structuring"}, {"Accepted one"}),
        ({"cap_overflow": True}, set()),
        ({"q": "reject"}, {"Rejected one"}),
    ],
)
def test_list_filters(api, params, expected_titles) -> None:
    client, icon, _other = api
    _candidate(icon, title="Accepted one")
    _candidate(
        icon,
        title="Rejected one",
        status="rejected",
        verdict="reject",
        reason_code="personnel",
        input_kind="news",
        service=None,
    )
    got = client.get(
        "/api/v1/candidates", headers=AUTH, params={"brand_id": icon, **params}
    )
    assert got.status_code == 200, got.text
    assert {r["source_title"] for r in got.json()} == expected_titles


def test_the_list_never_leaks_another_brands_candidates(api) -> None:
    client, icon, other = api
    _candidate(icon, title="Icon's")
    _candidate(other, title="Neovox's")
    got = client.get(
        "/api/v1/candidates", headers=AUTH, params={"brand_id": icon}
    )
    assert {r["source_title"] for r in got.json()} == {"Icon's"}


def test_an_unknown_brand_is_a_404_not_an_empty_list(api) -> None:
    """An empty list would read as "the guard accepted nothing" — the exact
    failure NTS_106 §2 is about, arriving through the API instead."""
    client, _icon, _other = api
    got = client.get(
        "/api/v1/candidates", headers=AUTH, params={"brand_id": 9999}
    )
    assert got.status_code == 404


# --- counters ------------------------------------------------------------


def test_counts_split_todays_rejects_by_reason_code(api) -> None:
    """«Отсеяно 143» is a number; «personnel 61 · forecast 40» is a finding."""
    client, icon, _other = api
    _candidate(icon)
    for _ in range(3):
        _candidate(
            icon,
            status="rejected",
            verdict="reject",
            reason_code="personnel",
            service=None,
        )
    _candidate(
        icon,
        status="rejected",
        verdict="reject",
        reason_code="forecast",
        service=None,
    )
    # Yesterday's reject must not land in today's distribution.
    _candidate(
        icon,
        status="rejected",
        verdict="reject",
        reason_code="award_pr",
        service=None,
        created_at=NOW - timedelta(days=2),
    )

    got = client.get(
        "/api/v1/candidates/counts", headers=AUTH, params={"brand_id": icon}
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["by_reason_code_today"] == {"personnel": 3, "forecast": 1}
    assert body["rejected_today"] == 4
    assert body["accepted_today"] == 1
    assert body["by_status"]["rejected"] == 5  # all time
    assert body["nav_portfolio"] == 1


def test_nav_counters_follow_the_six_section_navigation(api) -> None:
    """NTS_111 §Навигация: Портфель = pending + doc_missing, Ревью = drafted +
    returned, Публикации = ready."""
    client, icon, _other = api
    _candidate(icon, status="pending")
    _candidate(icon, status="doc_missing")
    _candidate(icon, status="drafted")
    _candidate(icon, status="returned")
    _candidate(icon, status="ready")

    body = client.get(
        "/api/v1/candidates/counts", headers=AUTH, params={"brand_id": icon}
    ).json()
    assert body["nav_portfolio"] == 2
    assert body["nav_review"] == 2
    assert body["nav_ready"] == 1


def test_a_source_counts_unhealthy_after_three_consecutive_failures(api) -> None:
    """NTS_106 §1: «источник unhealthy после 3 подряд». Three failures with a
    success in the middle is not three in a row."""
    client, icon, _other = api
    with admin_db.get_session_factory()() as session:
        bad = Source(
            brand_id_fk=icon,
            name="Broken feed",
            source_type="rss",
            url="https://broken.test/rss",
            primary_category="wealth",
        )
        recovering = Source(
            brand_id_fk=icon,
            name="Recovering feed",
            source_type="rss",
            url="https://recovering.test/rss",
            primary_category="wealth",
        )
        session.add_all([bad, recovering])
        session.flush()
        for index in range(3):
            session.add(
                SourceHealthRecord(
                    source_id=bad.id,
                    brand_id_fk=icon,
                    fetched_at=NOW - timedelta(hours=index),
                    success=False,
                    articles_count=0,
                    error_msg="boom",
                )
            )
        # newest success, then two failures → not unhealthy
        for index, ok in enumerate((True, False, False)):
            session.add(
                SourceHealthRecord(
                    source_id=recovering.id,
                    brand_id_fk=icon,
                    fetched_at=NOW - timedelta(hours=index),
                    success=ok,
                    articles_count=1 if ok else 0,
                )
            )
        session.commit()

    body = client.get(
        "/api/v1/candidates/counts", headers=AUTH, params={"brand_id": icon}
    ).json()
    assert body["nav_sources_unhealthy"] == 1


# --- the slot strip ------------------------------------------------------


def test_next_slots_returns_the_configured_days_in_order() -> None:
    slots = next_slots(
        slots_config=[
            {"day": "mon", "capacity": 2},
            {"day": "thu", "capacity": 2},
        ],
        today=date(2026, 8, 28),  # a Friday
        horizon_days=14,
    )
    assert [(d.isoformat(), name, cap) for d, name, cap in slots] == [
        ("2026-08-31", "mon", 2),
        ("2026-09-03", "thu", 2),
        ("2026-09-07", "mon", 2),
        ("2026-09-10", "thu", 2),
    ]


def test_next_slots_includes_today_when_today_is_a_slot_day() -> None:
    """The midnight case: on a Monday morning, Monday's slot is still today's.
    Skipping it would make the strip say the next slot is in a week."""
    slots = next_slots(
        slots_config=[{"day": "mon", "capacity": 2}],
        today=date(2026, 8, 31),  # Monday
        horizon_days=3,
    )
    assert slots[0][0] == date(2026, 8, 31)


def test_next_slots_is_unaffected_by_a_dst_change() -> None:
    """Europe/Madrid moves on 2026-10-25. Slot dates are *calendar* dates, so
    the strip must not shift or duplicate across the boundary — the arithmetic
    is on dates precisely so a 23- or 25-hour day cannot move it."""
    slots = next_slots(
        slots_config=[{"day": "sun", "capacity": 1}],
        today=date(2026, 10, 18),
        horizon_days=21,
    )
    assert [d.isoformat() for d, _n, _c in slots] == [
        "2026-10-18",
        "2026-10-25",  # the DST Sunday itself
        "2026-11-01",
    ]


@pytest.mark.parametrize(
    "bad",
    [None, [], "nonsense", [{"day": "funday", "capacity": 2}], [{"day": "mon"}]],
)
def test_next_slots_survives_a_malformed_config(bad) -> None:
    """The config surface is hand-editable. A stray value must empty the strip,
    not take the board down with it."""
    assert next_slots(slots_config=bad, today=date(2026, 8, 28)) == []


def test_summary_reports_slots_month_spend_and_the_brand_timezone(api) -> None:
    client, icon, _other = api
    with admin_db.get_session_factory()() as session:
        cfg = session.get(PipelineConfig, icon)
        cfg.publication_slots = json.dumps(
            [{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]
        )
        cfg.monthly_spend_cap_usd = 150.0
        cfg.brand_timezone = "Europe/Madrid"
        session.add(
            CostRecord(
                brand_id_fk=icon,
                provider="openai",
                operation="guard:news",
                cost_usd=48.0,
                created_at=NOW,
            )
        )
        session.commit()

    body = client.get(
        "/api/v1/candidates/summary", headers=AUTH, params={"brand_id": icon}
    ).json()
    assert body["month_spend_usd"] == pytest.approx(48.0)
    assert body["month_cap_usd"] == pytest.approx(150.0)
    assert body["month_spend_pct"] == pytest.approx(32.0)
    assert body["brand_timezone"] == "Europe/Madrid"
    assert len(body["slots"]) >= 4
    assert all(s["filled"] == 0 for s in body["slots"])


def test_summary_counts_a_ready_candidate_against_its_slot(api) -> None:
    client, icon, _other = api
    body = client.get(
        "/api/v1/candidates/summary", headers=AUTH, params={"brand_id": icon}
    ).json()
    first_slot = date.fromisoformat(body["slots"][0]["date"])
    _candidate(icon, status="ready", publication_slot=first_slot)

    body = client.get(
        "/api/v1/candidates/summary", headers=AUTH, params={"brand_id": icon}
    ).json()
    assert body["slots"][0]["filled"] == 1


def test_summary_shows_a_zero_cap_without_dividing_by_it(api) -> None:
    client, icon, _other = api
    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, icon).monthly_spend_cap_usd = 0.0
        session.commit()
    body = client.get(
        "/api/v1/candidates/summary", headers=AUTH, params={"brand_id": icon}
    ).json()
    assert body["month_spend_pct"] == 0.0


# --- the side panel -----------------------------------------------------


def test_detail_returns_the_service_label_and_the_decision_log(api) -> None:
    client, icon, _other = api
    candidate_id = _candidate(icon)
    client.post(
        "/api/v1/candidates/review-decisions",
        headers=AUTH,
        params={"brand_id": icon},
        json={
            "candidate_id": candidate_id,
            "action": "disagree_guard",
            "comment": "This is a policy change, not a personnel note.",
        },
    )
    got = client.get(
        f"/api/v1/candidates/{candidate_id}",
        headers=AUTH,
        params={"brand_id": icon},
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["candidate"]["id"] == candidate_id
    assert body["service_label"] == "Structuring & Tax"
    assert [d["action"] for d in body["review_decisions"]] == ["disagree_guard"]


def test_detail_shows_what_this_candidate_resembles(api) -> None:
    """«похоже на кандидата #412, 0.91» (NTS_111 §Портфель). The panel exists
    so a near-miss the pipeline decided to KEEP is visible — that is the
    calibration signal NTS_079 logs and nothing displays."""
    client, icon, _other = api
    vector = np.zeros(8, dtype=np.float32)
    vector[0] = 1.0
    near = np.zeros(8, dtype=np.float32)
    near[0] = float(np.cos(0.3))
    near[1] = float(np.sin(0.3))

    first = _candidate(icon, title="Original", topic_id="a", embedding=vector)
    second = _candidate(icon, title="Near miss", topic_id="b", embedding=near)

    body = client.get(
        f"/api/v1/candidates/{second}", headers=AUTH, params={"brand_id": icon}
    ).json()
    matches = body["dedup_matches"]
    assert matches, "a 0.955 neighbour must be visible in the panel"
    assert matches[0]["candidate_id"] == first
    assert matches[0]["title"] == "Original"
    assert matches[0]["similarity"] == pytest.approx(0.955, abs=0.01)


def test_detail_has_no_dedup_matches_without_an_embedding(api) -> None:
    client, icon, _other = api
    candidate_id = _candidate(icon)
    body = client.get(
        f"/api/v1/candidates/{candidate_id}",
        headers=AUTH,
        params={"brand_id": icon},
    ).json()
    assert body["dedup_matches"] == []


def test_detail_of_another_brands_candidate_is_a_404(api) -> None:
    client, icon, other = api
    foreign = _candidate(other)
    got = client.get(
        f"/api/v1/candidates/{foreign}", headers=AUTH, params={"brand_id": icon}
    )
    assert got.status_code == 404


# --- manual actions (NTS_098 §7) ----------------------------------------


def test_promote_turns_a_cap_overflow_reject_into_a_real_accept(api) -> None:
    """NTS_099 §5's whole promise. Leaving `verdict='reject'` would keep the row
    out of every accepted-candidate query — including the guard's own
    `recent_accepted_titles` — so the promotion would look applied and not be.
    """
    client, icon, _other = api
    candidate_id = _candidate(
        icon,
        status="rejected",
        verdict="reject",
        reason_code="daily_cap",
        reason="daily cap for news reached (1/1) — promotable today: adopted",
        cap_overflow=True,
    )
    got = client.post(
        f"/api/v1/candidates/{candidate_id}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": "promote", "reviewer": "andriy"},
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "pending"
    assert body["verdict"] == "accept"
    assert body["reason_code"] == "ok"
    assert body["cap_overflow"] is False
    assert body["manual_action"] == "promoted"
    assert body["manual_by"] == "andriy"

    from pipeline.selector.candidate_store import recent_accepted_titles

    assert any(
        "FINMA" in title for title in recent_accepted_titles(brand_id_fk=icon)
    )


def test_every_manual_action_writes_a_review_decision(api) -> None:
    """NTS_113 calls `review_decisions` the only free signal for tuning the
    rubric and the rank weights. Free only if writing it is not optional."""
    client, icon, _other = api
    for action, payload in (
        ("promote", {}),
        ("hold", {}),
        ("reject", {"comment": "Not a policy change after all."}),
    ):
        candidate_id = _candidate(icon)
        got = client.post(
            f"/api/v1/candidates/{candidate_id}/action",
            headers=AUTH,
            params={"brand_id": icon},
            json={"action": action, **payload},
        )
        assert got.status_code == 200, got.text
        with admin_db.get_session_factory()() as session:
            decisions = list(
                session.scalars(
                    select(ReviewDecision).where(
                        ReviewDecision.candidate_id_fk == candidate_id
                    )
                )
            )
        assert len(decisions) == 1, action
        assert decisions[0].scope == action


def test_reject_requires_a_comment(api) -> None:
    """A rejection with no sentence is indistinguishable from a mis-click when
    the row is read back a week later."""
    client, icon, _other = api
    candidate_id = _candidate(icon)
    got = client.post(
        f"/api/v1/candidates/{candidate_id}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": "reject"},
    )
    assert got.status_code == 422
    got = client.post(
        f"/api/v1/candidates/{candidate_id}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": "reject", "comment": "Out of scope: retail product."},
    )
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "Out of scope: retail product."
    assert body["reason_code"] == "out_of_scope"


def test_reset_clears_the_attempt_counter(api) -> None:
    """NTS_098 §2 asks for a manual reset to pending from the Portfolio.
    Without clearing
    `attempts` the candidate re-fails on its first try, because `max_attempts`
    is already spent."""
    client, icon, _other = api
    candidate_id = _candidate(icon, status="failed", attempts=2)
    got = client.post(
        f"/api/v1/candidates/{candidate_id}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": "reset"},
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["last_error"] is None


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("hold", "published"),
        ("reject", "published"),
        ("reset", "pending"),
        ("promote", "in_production"),
    ],
)
def test_an_action_from_the_wrong_status_is_a_409(api, action, status) -> None:
    """A stale board must not move a candidate that has since gone into
    production. The 409 names the current status so the browser refetches
    instead of retrying."""
    client, icon, _other = api
    candidate_id = _candidate(icon, status=status)
    got = client.post(
        f"/api/v1/candidates/{candidate_id}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": action, "comment": "x" * 25},
    )
    assert got.status_code == 409, got.text
    assert status in got.json()["detail"]


def test_an_action_on_another_brands_candidate_is_a_404(api) -> None:
    client, icon, other = api
    foreign = _candidate(other)
    got = client.post(
        f"/api/v1/candidates/{foreign}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": "hold"},
    )
    assert got.status_code == 404
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, foreign).status == "pending"


# --- the manual document link -------------------------------------------


def test_a_manual_document_link_moves_doc_missing_back_to_pending(api) -> None:
    """NTS_111 §Портфель. The operator can always beat the fetcher to a
    document, and before S5 there is no fetcher for several source classes."""
    client, icon, _other = api
    candidate_id = _candidate(icon, status="doc_missing")
    got = client.put(
        f"/api/v1/candidates/{candidate_id}/document",
        headers=AUTH,
        params={"brand_id": icon},
        json={
            "primary_doc_url": "https://finma.test/docs/circ-2026-3.pdf",
            "reviewer": "andriy",
        },
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "pending"
    assert body["primary_doc_url"].endswith("circ-2026-3.pdf")
    assert body["manual_by"] == "andriy"

    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        assert row.doc_match == "manual"


def test_a_document_link_is_not_filed_as_an_editorial_decision(api) -> None:
    """`review_decisions` is editorial actions (NTS_107). Pasting a URL is data
    entry — filing it under "hold" would put a non-decision into the one
    dataset NTS_113 reads to tune the rubric."""
    client, icon, _other = api
    candidate_id = _candidate(icon, status="doc_missing")
    client.put(
        f"/api/v1/candidates/{candidate_id}/document",
        headers=AUTH,
        params={"brand_id": icon},
        json={"primary_doc_url": "https://finma.test/d.pdf"},
    )
    with admin_db.get_session_factory()() as session:
        rows = list(
            session.scalars(
                select(ReviewDecision).where(
                    ReviewDecision.candidate_id_fk == candidate_id
                )
            )
        )
    assert rows == []


def test_a_non_url_document_link_is_refused(api) -> None:
    client, icon, _other = api
    candidate_id = _candidate(icon, status="doc_missing")
    got = client.put(
        f"/api/v1/candidates/{candidate_id}/document",
        headers=AUTH,
        params={"brand_id": icon},
        json={"primary_doc_url": "the pdf andriy has on his desktop"},
    )
    assert got.status_code == 422


# --- the disagree-with-the-verdict action -------------------------------


def test_disagree_guard_requires_a_comment(api) -> None:
    """A disagreement with no reason cannot be read back into a rubric edit,
    which is the only reason the button exists."""
    client, icon, _other = api
    candidate_id = _candidate(icon)
    got = client.post(
        "/api/v1/candidates/review-decisions",
        headers=AUTH,
        params={"brand_id": icon},
        json={"candidate_id": candidate_id, "action": "disagree_guard"},
    )
    assert got.status_code == 422
    got = client.post(
        "/api/v1/candidates/review-decisions",
        headers=AUTH,
        params={"brand_id": icon},
        json={
            "candidate_id": candidate_id,
            "action": "disagree_guard",
            "comment": "Personnel note that DOES change client policy.",
            "time_spent_s": 95,
        },
    )
    assert got.status_code == 201, got.text
    assert got.json()["time_spent_s"] == 95


def test_the_disagreement_log_is_filterable(api) -> None:
    """`?action=disagree_guard` is the rubric review's reading list."""
    client, icon, _other = api
    candidate_id = _candidate(icon)
    client.post(
        "/api/v1/candidates/review-decisions",
        headers=AUTH,
        params={"brand_id": icon},
        json={
            "candidate_id": candidate_id,
            "action": "disagree_guard",
            "comment": "Should have been accepted.",
        },
    )
    client.post(
        f"/api/v1/candidates/{candidate_id}/action",
        headers=AUTH,
        params={"brand_id": icon},
        json={"action": "hold"},
    )
    got = client.get(
        "/api/v1/candidates/review-decisions/list",
        headers=AUTH,
        params={"brand_id": icon, "action": "disagree_guard"},
    )
    assert [d["action"] for d in got.json()] == ["disagree_guard"]


# --- the Sources registry (NTS_111 §Источники) --------------------------


def test_the_registry_returns_the_classification_and_three_state_health(
    api,
) -> None:
    """A gap in the health series is `null`, not `false`: a source that stopped
    being polled must not render as a failing one, and neither may read as a
    success."""
    client, icon, _other = api
    with admin_db.get_session_factory()() as session:
        src = Source(
            brand_id_fk=icon,
            name="FINMA News DE",
            source_type="rss",
            url="https://finma.test/de/rss",
            primary_category="structuring",
            source_role="primary_feed",
            source_class="regulator",
            license_class="public_official",
            doc_language="de",
            fetch_method="rss",
        )
        session.add(src)
        session.flush()
        session.add(
            SourceHealthRecord(
                source_id=src.id,
                brand_id_fk=icon,
                fetched_at=NOW,
                success=True,
                articles_count=20,
            )
        )
        session.commit()

    rows = client.get(
        "/api/v1/sources/registry",
        headers=AUTH,
        params={"brand_id": icon, "days": 7},
    ).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["source_role"] == "primary_feed"
    assert row["source_class"] == "regulator"
    assert row["license_class"] == "public_official"
    assert row["fetch_method"] == "rss"
    assert len(row["health"]) == 7
    assert row["health"][-1] is True
    assert row["health"][0] is None  # no fetch that day
    assert row["success_rate_pct"] == 100.0


def test_the_document_find_share_is_null_until_it_is_measured(api) -> None:
    """A hard 0.0 reads as "this source never finds documents"; before S5 the
    honest answer is "not measured". And for a primary feed the share is
    meaningless — the item IS the document."""
    client, icon, _other = api
    with admin_db.get_session_factory()() as session:
        news = Source(
            brand_id_fk=icon,
            name="Wire",
            source_type="rss",
            url="https://wire.test/rss",
            primary_category="wealth",
            source_role="news",
        )
        primary = Source(
            brand_id_fk=icon,
            name="ESMA",
            source_type="rss",
            url="https://esma.test/rss",
            primary_category="structuring",
            source_role="primary_feed",
            source_class="regulator",
        )
        session.add_all([news, primary])
        session.flush()
        news_id, primary_id = news.id, primary.id
        session.commit()

    rows = {
        r["name"]: r
        for r in client.get(
            "/api/v1/sources/registry", headers=AUTH, params={"brand_id": icon}
        ).json()
    }
    assert rows["Wire"]["doc_find_share"] is None
    assert rows["ESMA"]["doc_find_share"] is None

    _candidate(icon, source_id=news_id, primary_doc_url="https://x.test/d.pdf")
    _candidate(icon, source_id=news_id)
    _candidate(icon, source_id=primary_id, primary_doc_url="https://x.test/e.pdf")

    rows = {
        r["name"]: r
        for r in client.get(
            "/api/v1/sources/registry", headers=AUTH, params={"brand_id": icon}
        ).json()
    }
    assert rows["Wire"]["doc_find_share"] == pytest.approx(0.5)
    assert rows["Wire"]["candidates_30d"] == 2
    assert rows["Wire"]["accepted_30d"] == 2
    # Still None for the primary feed, where the number would mean nothing.
    assert rows["ESMA"]["doc_find_share"] is None


def test_reclassifying_a_source_is_its_own_action(api) -> None:
    """NTS_108's DoD wants a licence class on every source, and 020 started
    every pre-v3 feed at the most restrictive one. This is the endpoint that
    moves them up."""
    client, icon, _other = api
    with admin_db.get_session_factory()() as session:
        src = Source(
            brand_id_fk=icon,
            name="Legacy news feed",
            source_type="rss",
            url="https://legacy.test/rss",
            primary_category="wealth",
        )
        session.add(src)
        session.flush()
        source_id = src.id
        session.commit()

    got = client.put(
        f"/api/v1/sources/{source_id}/registry",
        headers=AUTH,
        params={"brand_id": icon},
        json={
            "license_class": "public_official",
            "source_class": "regulator",
            "source_role": "primary_feed",
            "doc_language": "en",
            "fetch_method": "atom",
        },
    )
    assert got.status_code == 200, got.text
    assert got.json()["license_class"] == "public_official"

    bad = client.put(
        f"/api/v1/sources/{source_id}/registry",
        headers=AUTH,
        params={"brand_id": icon},
        json={"license_class": "whatever_we_feel_like"},
    )
    assert bad.status_code == 422


# --- the taxonomy editor (NTS_111 §Редполитика) -------------------------


def test_services_are_listed_in_the_order_the_rubric_renders_them(api) -> None:
    client, icon, _other = api
    rows = client.get(
        "/api/v1/taxonomy", headers=AUTH, params={"brand_id": icon}
    ).json()
    assert [r["key"] for r in rows] == ["structuring", "wealth"]


def test_a_service_can_be_added_and_edited(api) -> None:
    client, icon, _other = api
    created = client.post(
        "/api/v1/taxonomy",
        headers=AUTH,
        json={
            "brand_id": icon,
            "key": "ma",
            "label": "M&A Consulting",
            "description_for_guard": "Mid-market deals: SPA, earn-out, price disclosed",
            "service_url_path": "/services/ma-consulting",
        },
    )
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]

    updated = client.put(
        f"/api/v1/taxonomy/{service_id}",
        headers=AUTH,
        params={"brand_id": icon},
        json={"label": "M&A"},
    )
    assert updated.status_code == 200
    assert updated.json()["label"] == "M&A"


def test_a_service_key_cannot_be_renamed(api) -> None:
    """`candidates.service_category` stores the key with no foreign key, so a
    rename would orphan history silently. The update schema simply has no
    `key`, and `extra="forbid"` turns an attempt into a 422."""
    client, icon, _other = api
    rows = client.get(
        "/api/v1/taxonomy", headers=AUTH, params={"brand_id": icon}
    ).json()
    got = client.put(
        f"/api/v1/taxonomy/{rows[0]['id']}",
        headers=AUTH,
        params={"brand_id": icon},
        json={"key": "renamed"},
    )
    assert got.status_code == 422


def test_a_service_in_use_cannot_be_deleted(api) -> None:
    client, icon, _other = api
    _candidate(icon, service="structuring")
    rows = {
        r["key"]: r
        for r in client.get(
            "/api/v1/taxonomy", headers=AUTH, params={"brand_id": icon}
        ).json()
    }
    blocked = client.delete(
        f"/api/v1/taxonomy/{rows['structuring']['id']}",
        headers=AUTH,
        params={"brand_id": icon},
    )
    assert blocked.status_code == 409
    assert "1 candidate" in blocked.json()["detail"]

    freed = client.delete(
        f"/api/v1/taxonomy/{rows['wealth']['id']}",
        headers=AUTH,
        params={"brand_id": icon},
    )
    assert freed.status_code == 204


def test_a_service_needs_a_description_the_guard_can_render(api) -> None:
    """`{services}` renders `description_for_guard` verbatim; an empty one gives
    the rubric a service key with no meaning attached."""
    client, icon, _other = api
    got = client.post(
        "/api/v1/taxonomy",
        headers=AUTH,
        json={
            "brand_id": icon,
            "key": "misc",
            "label": "Misc",
            "description_for_guard": "n/a",
            "service_url_path": "/misc",
        },
    )
    assert got.status_code == 422


def test_a_duplicate_service_key_is_refused(api) -> None:
    client, icon, _other = api
    got = client.post(
        "/api/v1/taxonomy",
        headers=AUTH,
        json={
            "brand_id": icon,
            "key": "structuring",
            "label": "Duplicate",
            "description_for_guard": "Something long enough to pass",
            "service_url_path": "/dup",
        },
    )
    assert got.status_code == 422


# --- placeholder validation (closes the NTS_063 pending) ----------------


def test_the_validate_endpoint_explains_a_broken_rubric(api) -> None:
    client, icon, _other = api
    got = client.post(
        "/api/v1/prompts/validate",
        headers=AUTH,
        json={
            "brand_id": icon,
            "prompt_type": "editorial_guard",
            "version_name": "draft",
            "content": _GUARD_PROMPT.replace("{recent_accepted_titles}", ""),
        },
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["ok"] is False
    assert body["missing"] == ["recent_accepted_titles"]
    assert "REJECTED at run time" in body["message"]


def test_the_validate_endpoint_passes_the_shipped_rubric(api) -> None:
    client, icon, _other = api
    body = client.post(
        "/api/v1/prompts/validate",
        headers=AUTH,
        json={
            "brand_id": icon,
            "prompt_type": "editorial_guard",
            "version_name": "shipped",
            "content": _GUARD_PROMPT,
        },
    ).json()
    assert body["ok"] is True
    assert body["missing"] == []
    assert body["unknown"] == []


def test_activating_a_prompt_that_would_not_apply_is_refused(api) -> None:
    """The NTS_071 §2 failure, closed at the moment it would take effect.

    A *draft* may be invalid — the editor has to be usable mid-edit, and the
    resolver only ever reads the active row. Activation is where the body would
    start (or silently stop) reaching production, so that is where the refusal
    belongs.
    """
    client, icon, _other = api
    created = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json={
            "brand_id": icon,
            "prompt_type": "editorial_guard",
            "version_name": "broken",
            "content": _GUARD_PROMPT + "\nAlso consider {my_new_idea}.\n",
        },
    )
    assert created.status_code == 201, created.text

    refused = client.post(
        f"/api/v1/prompts/{created.json()['id']}/activate", headers=AUTH
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["unknown"] == ["my_new_idea"]

    with admin_db.get_session_factory()() as session:
        rows = list(
            session.scalars(select(Prompt).where(Prompt.brand_id_fk == icon))
        )
    assert len(rows) == 1
    assert rows[0].is_active is False, "a refused activation must not take effect"


def test_a_valid_rubric_still_saves(api) -> None:
    client, icon, _other = api
    edited = _GUARD_PROMPT.replace(
        "=== THE ITEM ===", "Treat LI foundations as tier1.\n\n=== THE ITEM ==="
    )
    got = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json={
            "brand_id": icon,
            "prompt_type": "editorial_guard",
            "version_name": "edited",
            "content": edited,
        },
    )
    assert got.status_code == 201, got.text
    activated = client.post(
        f"/api/v1/prompts/{got.json()['id']}/activate", headers=AUTH
    )
    assert activated.status_code == 200

    from pipeline.selector.editorial_guard import resolve_guard_template

    template, source = resolve_guard_template(icon)
    assert source == "db"
    assert "tier1" in template


@pytest.mark.parametrize(
    "prompt_type", ["writer_draft", "writer_polish", "writer_translate"]
)
def test_the_writer_contracts_match_what_the_writer_actually_renders(
    prompt_type: str,
) -> None:
    """The allowed-placeholder table the API validates against has to be the
    set the renderer really supplies. A drifted copy would be invisible,
    because the symptom is a prompt that saves fine and never applies."""
    from pipeline.admin.routes.prompts import check_placeholders, placeholder_contract
    from pipeline.generator import comment_writer

    required, allowed = placeholder_contract(prompt_type)
    assert required <= allowed

    constant = {
        "writer_draft": comment_writer._DRAFT_PROMPT,
        "writer_polish": comment_writer._POLISH_PROMPT,
        "writer_translate": comment_writer._TRANSLATE_PROMPT,
    }[prompt_type]
    # The shipped constant is by definition valid: it is what runs when a DB
    # row is rejected, so if the contract calls it invalid the contract is wrong.
    assert check_placeholders(prompt_type, constant).ok is True


async def test_the_allowed_sets_are_the_real_render_kwargs(monkeypatch) -> None:
    """Captured from a real generation pass rather than read off the source, so
    a kwarg added at a call site without updating the table fails here."""
    import json as json_mod
    from unittest.mock import AsyncMock

    from pipeline.common.models import Language, RawItem, Topic
    from pipeline.generator import comment_writer
    from pipeline.generator.comment_writer import CommentWriter

    seen: dict[str, set[str]] = {}
    original = CommentWriter._resolve_template

    def spy(self, prompt_type, fallback, render_kwargs):
        seen[prompt_type] = set(render_kwargs)
        return original(self, prompt_type, fallback, render_kwargs)

    monkeypatch.setattr(CommentWriter, "_resolve_template", spy)

    payload = {"title": "T", "body": "## A\n\nAcme raised $5m.", "key_takeaway": "K"}

    def _resp(data):
        message = type("M", (), {"content": json_mod.dumps(data)})()
        return type(
            "R", (), {"choices": [type("C", (), {"message": message})()], "usage": None}
        )()

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_resp(payload), _resp(payload), _resp(payload)]
    )
    writer = CommentWriter(client=client)
    topic = Topic(
        id="t1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="s",
            url="https://example.test/x",
            title="Acme raises a fund",
            summary="Acme raised five million.",
        ),
        relevance_score=8.0,
    )
    en = await writer.write(topic, "banned_phrases: []\n", Language.en)
    await writer.translate(en, Language.ru, "banned_phrases: []\n")

    for prompt_type, kwargs in seen.items():
        assert kwargs == comment_writer._ALLOWED_PLACEHOLDERS[prompt_type], (
            prompt_type,
            sorted(kwargs ^ comment_writer._ALLOWED_PLACEHOLDERS[prompt_type]),
        )
    assert set(seen) == {"writer_draft", "writer_polish", "writer_translate"}
