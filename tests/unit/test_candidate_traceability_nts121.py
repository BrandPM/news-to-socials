"""The candidate↔draft↔article chain (NTS_098 §1-2, NTS_106 §3, NTS_096 A).

Why this file exists: on 2026-08-28 the production database held 337
candidates and 137 approvals, and ``candidates.sanity_draft_id`` was filled on
**zero** rows, ``draft_approvals.candidate_id_fk`` on **zero** rows. The link
was declared in NTS_098 §1 and written by nobody, which makes the status
``published`` unreachable and ends the whole contour at ``drafted``.

Every test here pins one link in that chain, in the order the chain runs:

1. draft creation writes **both** sides or neither (transactional);
2. ``publication_slot`` is assigned on the move to ``ready``, in the brand's
   timezone, respecting slot capacity — including midnight and DST;
3. ``published`` is set **only** against a non-empty
   ``draft_approvals.published_at`` — never on the strength of an approve click;
4. every paid row in ``cost_records`` carries the candidate, so
   ``max_cost_per_candidate_usd`` is computable at all;
5. an editor action on a *draft* writes ``review_decisions``, which until now
   only the *candidate* endpoints did;
6. the fact pack survives the run that produced it (NTS_096 part A).

No paid call anywhere: the guard, the embedding and Sanity are stubbed.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin import encryption as enc_mod
from pipeline.admin import jobs as admin_jobs
from pipeline.admin.models import (
    Candidate,
    CostRecord,
    DraftApproval,
    FactPack,
    PipelineConfig,
    ReviewDecision,
)
from pipeline.common import config as config_module
from pipeline.selector import candidate_lifecycle as lifecycle
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-trace"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}

# 2026-08-28 is a Friday. Slots are Monday and Thursday, so "the next slot"
# from that day is Monday 2026-08-31 — a date the tests can name out loud.
FRIDAY = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SLOTS = '[{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]'


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv(
        "BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    yield admin_db.get_session_factory()
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


@pytest.fixture
def brand_id(db):
    with db() as session:
        bid = seed_icon_brand(session, with_sanity_creds=True)
        session.add(
            PipelineConfig(
                brand_id_fk=bid,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps([]),
                voice_profile="mission: x\n",
                publication_slots=SLOTS,
                brand_timezone="Europe/Madrid",
            )
        )
        session.commit()
    return bid


def _candidate(db, brand_id: int, *, status: str = "in_production") -> int:
    with db() as session:
        row = Candidate(
            brand_id_fk=brand_id,
            input_kind="document",
            source_title="ESMA consults on CASP authorisation",
            verdict="accept",
            reason_code="ok",
            reason="in scope",
            status=status,
            created_at=FRIDAY,
        )
        session.add(row)
        session.commit()
        return int(row.id)


# --------------------------------------------------------------------------
# 1. both sides of the link, or neither
# --------------------------------------------------------------------------


def test_link_writes_candidate_and_approval_sides(db, brand_id):
    cid = _candidate(db, brand_id)

    assert lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )

    with db() as session:
        cand = session.get(Candidate, cid)
        assert cand.sanity_draft_id == "drafts.abc-en"
        assert cand.status == "drafted"
        assert cand.drafted_at is not None
        approval = session.query(DraftApproval).one()
        assert approval.sanity_draft_id == "drafts.abc-en"
        assert approval.candidate_id_fk == cid
        assert approval.status == "draft"


def test_link_attaches_candidate_to_a_pre_existing_approval(db, brand_id):
    """A regenerated draft already has an approval row; the link must adopt it
    rather than violate ``uq_draft_approvals_draft_brand``."""
    cid = _candidate(db, brand_id)
    with db() as session:
        session.add(
            DraftApproval(
                sanity_draft_id="drafts.abc-en",
                brand_id_fk=brand_id,
                status="rejected",
                decided_at=FRIDAY,
            )
        )
        session.commit()

    assert lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )

    with db() as session:
        approval = session.query(DraftApproval).one()
        assert approval.candidate_id_fk == cid
        # The decision itself is untouched — linking is not a re-decision.
        assert approval.status == "rejected"


def test_link_is_idempotent(db, brand_id):
    cid = _candidate(db, brand_id)
    for _ in range(2):
        assert lifecycle.link_candidate_to_draft(
            candidate_id=cid,
            sanity_draft_id="drafts.abc-en",
            brand_id_fk=brand_id,
            now=FRIDAY,
        )
    with db() as session:
        assert session.query(DraftApproval).count() == 1


def test_link_refused_from_pending_leaves_both_sides_untouched(db, brand_id):
    """``pending`` has not been through production; a draft cannot exist for it
    (NTS_098 §2). Refusing is the point — a silent accept would hide a bug in
    the selector."""
    cid = _candidate(db, brand_id, status="pending")

    assert not lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )

    with db() as session:
        assert session.get(Candidate, cid).sanity_draft_id is None
        assert session.query(DraftApproval).count() == 0


def test_link_rolls_back_the_candidate_side_when_the_approval_side_fails(
    db, brand_id, monkeypatch
):
    """The two writes are one transaction. Half a link is worse than none: a
    candidate saying ``drafted`` with no approval row is invisible to the
    review queue and to the publish gate at the same time."""
    cid = _candidate(db, brand_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("approval side down")

    monkeypatch.setattr(lifecycle, "_attach_approval", boom)

    with pytest.raises(RuntimeError):
        lifecycle.link_candidate_to_draft(
            candidate_id=cid,
            sanity_draft_id="drafts.abc-en",
            brand_id_fk=brand_id,
            now=FRIDAY,
        )

    with db() as session:
        cand = session.get(Candidate, cid)
        assert cand.sanity_draft_id is None
        assert cand.status == "in_production"


# --------------------------------------------------------------------------
# 2. publication_slot on the move to ready
# --------------------------------------------------------------------------


def test_next_slot_is_the_nearest_configured_day():
    slots = json.loads(SLOTS)
    # Friday 2026-08-28 → Monday 2026-08-31.
    assert lifecycle.next_publication_slot(
        slots=slots, timezone_name="Europe/Madrid", now=FRIDAY, taken={}
    ) == date(2026, 8, 31)


def test_next_slot_skips_a_full_slot_and_takes_the_following_one():
    slots = json.loads(SLOTS)
    assert lifecycle.next_publication_slot(
        slots=slots,
        timezone_name="Europe/Madrid",
        now=FRIDAY,
        taken={date(2026, 8, 31): 2},
    ) == date(2026, 9, 3)  # Thursday


def test_next_slot_counts_capacity_not_existence():
    slots = json.loads(SLOTS)
    assert lifecycle.next_publication_slot(
        slots=slots,
        timezone_name="Europe/Madrid",
        now=FRIDAY,
        taken={date(2026, 8, 31): 1},
    ) == date(2026, 8, 31)


def test_next_slot_uses_the_brand_day_not_utc():
    """23:30 UTC on Sunday is already Monday in Madrid, so the Monday slot is
    today, not next week. Getting this wrong shifts every displayDate by a day
    twice a year."""
    slots = json.loads(SLOTS)
    late_sunday_utc = datetime(2026, 8, 30, 23, 30, tzinfo=UTC)
    assert lifecycle.next_publication_slot(
        slots=slots,
        timezone_name="Europe/Madrid",
        now=late_sunday_utc,
        taken={},
    ) == date(2026, 8, 31)


def test_next_slot_across_the_dst_end_boundary():
    """EU clocks go back on Sunday 2026-10-25. The Monday slot after that is
    2026-10-26 whether or not the offset changed."""
    slots = json.loads(SLOTS)
    saturday = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert lifecycle.next_publication_slot(
        slots=slots, timezone_name="Europe/Madrid", now=saturday, taken={}
    ) == date(2026, 10, 26)


def test_next_slot_across_the_dst_start_boundary():
    """EU clocks go forward on Sunday 2026-03-29; 23:30 UTC that day is
    01:30 Monday in Madrid (offset already +2)."""
    slots = json.loads(SLOTS)
    late = datetime(2026, 3, 29, 23, 30, tzinfo=UTC)
    assert lifecycle.next_publication_slot(
        slots=slots, timezone_name="Europe/Madrid", now=late, taken={}
    ) == date(2026, 3, 30)


def test_next_slot_returns_none_when_no_slots_are_configured():
    assert (
        lifecycle.next_publication_slot(
            slots=[], timezone_name="Europe/Madrid", now=FRIDAY, taken={}
        )
        is None
    )


def test_assign_slot_moves_drafted_to_ready(db, brand_id):
    cid = _candidate(db, brand_id, status="drafted")
    slot = lifecycle.assign_publication_slot(
        candidate_id=cid, brand_id_fk=brand_id, now=FRIDAY
    )
    assert slot == date(2026, 8, 31)
    with db() as session:
        cand = session.get(Candidate, cid)
        assert cand.status == "ready"
        assert cand.publication_slot == date(2026, 8, 31)


def test_assign_slot_counts_slots_already_taken_by_siblings(db, brand_id):
    first = _candidate(db, brand_id, status="drafted")
    second = _candidate(db, brand_id, status="drafted")
    third = _candidate(db, brand_id, status="drafted")
    assert lifecycle.assign_publication_slot(
        candidate_id=first, brand_id_fk=brand_id, now=FRIDAY
    ) == date(2026, 8, 31)
    assert lifecycle.assign_publication_slot(
        candidate_id=second, brand_id_fk=brand_id, now=FRIDAY
    ) == date(2026, 8, 31)
    # Monday's capacity of 2 is used up; the third lands on Thursday.
    assert lifecycle.assign_publication_slot(
        candidate_id=third, brand_id_fk=brand_id, now=FRIDAY
    ) == date(2026, 9, 3)


def test_assign_slot_refuses_from_pending(db, brand_id):
    cid = _candidate(db, brand_id, status="pending")
    assert (
        lifecycle.assign_publication_slot(
            candidate_id=cid, brand_id_fk=brand_id, now=FRIDAY
        )
        is None
    )
    with db() as session:
        assert session.get(Candidate, cid).status == "pending"


# --------------------------------------------------------------------------
# 3. published only against draft_approvals.published_at
# --------------------------------------------------------------------------


def test_published_is_refused_without_a_published_at(db, brand_id):
    cid = _candidate(db, brand_id)
    lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )
    lifecycle.assign_publication_slot(
        candidate_id=cid, brand_id_fk=brand_id, now=FRIDAY
    )
    # Approved, but Sanity never confirmed the promote.
    with db() as session:
        approval = session.query(DraftApproval).one()
        approval.status = "approved"
        session.commit()

    assert not lifecycle.mark_published_if_approved(candidate_id=cid)
    with db() as session:
        cand = session.get(Candidate, cid)
        assert cand.status == "ready"
        assert cand.published_at is None


def test_published_is_set_from_the_approval_stamp(db, brand_id):
    cid = _candidate(db, brand_id)
    lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )
    lifecycle.assign_publication_slot(
        candidate_id=cid, brand_id_fk=brand_id, now=FRIDAY
    )
    stamp = FRIDAY + timedelta(hours=3)
    with db() as session:
        approval = session.query(DraftApproval).one()
        approval.status = "approved"
        approval.published_at = stamp
        approval.sanity_published_id = "abc-en"
        session.commit()

    assert lifecycle.mark_published_if_approved(candidate_id=cid)
    with db() as session:
        cand = session.get(Candidate, cid)
        assert cand.status == "published"
        assert cand.published_at is not None
        assert cand.published_at.replace(tzinfo=UTC) == stamp


# --------------------------------------------------------------------------
# 4. cost per candidate
# --------------------------------------------------------------------------


def test_cost_rows_written_in_a_collector_get_attached_to_the_candidate(
    db, brand_id
):
    from pipeline.admin.cost_recorder import (
        CostContext,
        attach_candidate,
        collect_cost_rows,
        cost_context,
    )

    cid = _candidate(db, brand_id, status="pending")
    with (
        cost_context(CostContext(brand_id_fk=brand_id)),
        collect_cost_rows() as rows,
    ):
        from pipeline.admin.cost_recorder import record_cost

        record_cost(
            provider="openai",
            operation="guard:document",
            model="gpt-4o-mini",
            cost_usd=0.0004,
        )
        record_cost(
            provider="openai", operation="embedding", cost_usd=0.00002
        )
    assert len(rows) == 2
    attach_candidate(rows, cid)

    with db() as session:
        attached = session.query(CostRecord).all()
        assert {r.candidate_id_fk for r in attached} == {cid}


def test_cost_context_carries_the_candidate_directly(db, brand_id):
    """The production path (S4) knows the candidate before it spends, so it
    sets it on the context instead of back-filling."""
    from pipeline.admin.cost_recorder import (
        CostContext,
        cost_context,
        record_cost,
    )

    cid = _candidate(db, brand_id)
    with cost_context(CostContext(brand_id_fk=brand_id, candidate_id=cid)):
        record_cost(provider="openai", operation="draft", cost_usd=1.25)

    with db() as session:
        assert session.query(CostRecord).one().candidate_id_fk == cid


def test_candidate_spend_and_the_per_candidate_cap(db, brand_id):
    cid = _candidate(db, brand_id)
    with db() as session:
        for amount in (1.5, 2.0, 0.25):
            session.add(
                CostRecord(
                    brand_id_fk=brand_id,
                    candidate_id_fk=cid,
                    provider="openai",
                    operation="draft",
                    cost_usd=amount,
                )
            )
        # Another candidate's spend must not count towards this one.
        other = _candidate(db, brand_id)
        session.add(
            CostRecord(
                brand_id_fk=brand_id,
                candidate_id_fk=other,
                provider="openai",
                operation="draft",
                cost_usd=99.0,
            )
        )
        session.commit()

    assert lifecycle.candidate_spend_usd(cid) == pytest.approx(3.75)
    assert not lifecycle.exceeds_cost_cap(cid, 5.0)
    assert lifecycle.exceeds_cost_cap(cid, 3.0)
    # A cap of 0 means "no cap", not "everything is over budget".
    assert not lifecycle.exceeds_cost_cap(cid, 0.0)


# --------------------------------------------------------------------------
# 5. review_decisions on a draft action
# --------------------------------------------------------------------------


@pytest.fixture
def api(db, monkeypatch):
    from pipeline.admin.routes import drafts as drafts_routes
    from pipeline.admin.server import create_app
    from pipeline.publisher import sanity as sanity_mod

    async def fake_promote(self, draft_id, *, published_at=None):
        return draft_id.replace("drafts.", "")

    async def fake_fetch_for_validation(client, sanity_id):
        return {
            "_id": sanity_id,
            "language": "en",
            "title": "T",
            "displayDate": "2026-08-31",
            "slug": "a-slug",
            "coverImageRef": "image-abc123-1792x1008-png",
            "bodyBlockCount": 6,
            "bodyH2Count": 2,
        }

    monkeypatch.setattr(
        sanity_mod.SanityPublisher, "promote_draft_to_published", fake_promote
    )
    monkeypatch.setattr(
        drafts_routes, "fetch_draft_for_validation", fake_fetch_for_validation
    )
    admin_jobs.reset_image_jobs_for_tests()
    admin_jobs.reset_text_jobs_for_tests()
    yield TestClient(create_app())
    admin_jobs.reset_image_jobs_for_tests()
    admin_jobs.reset_text_jobs_for_tests()


def test_approving_a_linked_draft_publishes_the_candidate_and_logs_the_decision(
    db, brand_id, api
):
    cid = _candidate(db, brand_id)
    lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )

    resp = api.post(
        f"/api/v1/drafts/abc-en/approve?brand_id={brand_id}",
        headers=AUTH,
        json={"note": "ok"},
    )
    assert resp.status_code == 200, resp.text

    with db() as session:
        cand = session.get(Candidate, cid)
        # ready → published, with the slot assigned on the way through.
        assert cand.status == "published"
        # Computed, not named: the approve goes through the API and therefore
        # through the real clock, so a literal date here would only hold for
        # the week it was written in.
        assert cand.publication_slot == lifecycle.next_publication_slot(
            slots=json.loads(SLOTS),
            timezone_name="Europe/Madrid",
            now=datetime.now(tz=UTC),
            taken={},
        )
        assert cand.published_at is not None
        decision = session.query(ReviewDecision).one()
        assert decision.action == "approve"
        assert decision.candidate_id_fk == cid


def test_rejecting_a_linked_draft_rejects_the_candidate_and_logs_it(
    db, brand_id, api
):
    cid = _candidate(db, brand_id)
    lifecycle.link_candidate_to_draft(
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        brand_id_fk=brand_id,
        now=FRIDAY,
    )

    resp = api.post(
        f"/api/v1/drafts/abc-en/reject?brand_id={brand_id}",
        headers=AUTH,
        json={"note": "off scope"},
    )
    assert resp.status_code == 200, resp.text

    with db() as session:
        cand = session.get(Candidate, cid)
        assert cand.status == "rejected"
        assert cand.manual_action == "rejected"
        decision = session.query(ReviewDecision).one()
        assert decision.action == "reject"
        assert decision.comment == "off scope"


def test_an_unlinked_v2_draft_behaves_exactly_as_before(db, brand_id, api):
    """137 approval rows on production have no candidate. The new writes must
    be conditional on the link, or every v2 approve starts erroring."""
    resp = api.post(
        f"/api/v1/drafts/legacy-en/approve?brand_id={brand_id}",
        headers=AUTH,
        json={"note": None},
    )
    assert resp.status_code == 200, resp.text
    with db() as session:
        assert session.query(ReviewDecision).count() == 0
        approval = session.query(DraftApproval).one()
        assert approval.candidate_id_fk is None
        assert approval.status == "approved"


# --------------------------------------------------------------------------
# 6. fact pack survives the run (NTS_096 part A)
# --------------------------------------------------------------------------


def test_fact_pack_is_persisted_with_the_whole_chain_of_ids(db, brand_id):
    from pipeline.generator.fact_pack_store import persist_fact_pack

    cid = _candidate(db, brand_id)
    pack_id = persist_fact_pack(
        brand_id_fk=brand_id,
        candidate_id=cid,
        sanity_draft_id="drafts.abc-en",
        topic_id="t-1",
        pack={"context": [{"claim": "18 years", "url": "https://x/y"}]},
        sources=("https://x/y",),
        primary_doc_url="https://esma.europa.eu/doc.pdf",
        doc_version_id="v1",
        doc_sections_used=("art. 4", "annex II"),
        doc_text="Article 4 …",
        model="gpt-4o-mini",
        cost_usd=0.02,
    )
    assert pack_id

    with db() as session:
        row = session.get(FactPack, pack_id)
        assert row.candidate_id_fk == cid
        assert row.sanity_draft_id == "drafts.abc-en"
        assert row.primary_doc_url.endswith("doc.pdf")
        assert json.loads(row.doc_sections_used) == ["art. 4", "annex II"]
        assert json.loads(row.sources) == ["https://x/y"]
        assert json.loads(row.pack)["context"][0]["claim"] == "18 years"


def test_fact_pack_is_persisted_even_without_a_candidate(db, brand_id):
    """NTS_096: "пишется на каждом ресёрч-вызове, включая темы, которые до
    публикации не дошли". Until S4 there is no candidate on the v2 path at all,
    and a pack that only survives linked topics answers nothing."""
    from pipeline.generator.fact_pack_store import persist_fact_pack

    pack_id = persist_fact_pack(
        brand_id_fk=brand_id,
        candidate_id=None,
        sanity_draft_id=None,
        topic_id="t-2",
        pack={"context": []},
        sources=(),
        model="gpt-4o-mini",
        cost_usd=0.0,
    )
    with db() as session:
        assert session.get(FactPack, pack_id).candidate_id_fk is None


# --------------------------------------------------------------------------
# 7. schema
# --------------------------------------------------------------------------


def test_migration_025_columns_exist(db):
    from sqlalchemy import inspect

    inspector = inspect(admin_db.get_engine())
    cost_cols = {c["name"] for c in inspector.get_columns("cost_records")}
    assert "candidate_id_fk" in cost_cols
    cand_cols = {c["name"] for c in inspector.get_columns("candidates")}
    assert "doc_sections_used" in cand_cols
    assert "fact_packs" in inspector.get_table_names()
