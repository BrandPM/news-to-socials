"""IT_PROJ_NTS_098 DoD 3 and 5 — the atomic claim and the three dedup windows.

**DoD 3 — ``pending → selected`` atomic under two parallel processes.** Not two
threads: the spec asks for a test with two processes, and threads in one process
share a connection pool and a GIL, which is exactly the environment where a
read-then-write implementation passes. Two real subprocesses racing on one
SQLite file is the environment where it fails.

**DoD 5 — three windows, thresholds from config, document URL as the key.** The
failure this prevents is not "a duplicate article": it is that each window has
a *different* right answer (drop / copy the verdict for free / supersede), and
an implementation with one window silently gives the wrong one three times out
of four.

The embeddings here are hand-built unit vectors, so a similarity is exact and a
threshold test measures the threshold rather than the embedding model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    CANDIDATE_LIVE_STATUSES,
    BrandTaxonomy,
    Candidate,
    PipelineConfig,
    TopicEmbedding,
)
from pipeline.common import config as config_module
from pipeline.selector.candidate_dedup import (
    CandidateDedupConfig,
    check_post_guard,
    check_pre_guard,
    normalize_doc_url,
)
from pipeline.selector.candidate_store import (
    CandidateInput,
    brand_day_bounds,
    claim_pending,
    count_accepted_today,
    create_candidate,
    mark_superseded,
    recent_accepted_titles,
    ttl_for_stage,
)
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
DIM = 8


def _vec(*, angle: float) -> np.ndarray:
    """A unit vector in the first two dimensions — cosine == cos(angle)."""
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = float(np.cos(angle))
    v[1] = float(np.sin(angle))
    return v


IDENTICAL = _vec(angle=0.0)
NEAR = _vec(angle=0.30)  # cos ≈ 0.955
FARISH = _vec(angle=0.55)  # cos ≈ 0.852
FAR = _vec(angle=1.4)  # cos ≈ 0.170


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
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
                banned_phrases=json.dumps([]),
                voice_profile="mission: x\n",
            )
        )
        session.add(
            BrandTaxonomy(
                brand_id_fk=brand_id,
                key="structuring",
                label="Structuring & Tax",
                description_for_guard="CRS/DAC/CARF",
                service_url_path="/t",
            )
        )
        session.commit()
    yield {"path": tmp_path / "admin.db", "brand_id": brand_id}
    admin_db.reset_for_tests()


def _make_candidate(
    brand_id: int,
    *,
    topic_id: str,
    embedding: np.ndarray | None,
    status: str = "pending",
    verdict: str = "accept",
    reason_code: str = "ok",
    reason: str = "adopted, first reporting period 2027",
    event_stage: str = "adopted",
    primary_doc_url: str | None = None,
    input_kind: str = "news",
    created_at: datetime = NOW,
    published_at: datetime | None = None,
) -> int:
    """Insert a candidate plus (optionally) its embedding row."""
    with admin_db.get_session_factory()() as session:
        row = Candidate(
            brand_id_fk=brand_id,
            input_kind=input_kind,
            source_title=f"item {topic_id}",
            verdict=verdict,
            reason_code=reason_code,
            reason=reason,
            event_stage=event_stage,
            status=status,
            topic_embedding_ref=topic_id,
            primary_doc_url=primary_doc_url,
            created_at=created_at,
            published_at=published_at,
        )
        session.add(row)
        if embedding is not None:
            session.add(
                TopicEmbedding(
                    topic_id=topic_id,
                    brand_id_fk=brand_id,
                    embedding=np.asarray(embedding, dtype=np.float32).tobytes(),
                    model="text-embedding-3-small",
                    title_norm=topic_id,
                    created_at=created_at,
                )
            )
        session.commit()
        return int(row.id)


CONFIG = CandidateDedupConfig()


# --- DoD 5: the live window ----------------------------------------------


def test_live_window_same_stage_is_a_duplicate_and_a_new_stage_supersedes(db) -> None:
    """NTS_098 §3: "дубль: не создаётся; если event_stage новее — superseded"."""
    brand = db["brand_id"]
    existing = _make_candidate(
        brand, topic_id="a", embedding=IDENTICAL, event_stage="consultation"
    )

    same = check_post_guard(
        brand_id_fk=brand,
        embedding=IDENTICAL,
        event_stage="consultation",
        config=CONFIG,
        now=NOW,
    )
    assert same.action == "duplicate"
    assert same.window == "live"
    assert same.matched_candidate_id == existing

    later = check_post_guard(
        brand_id_fk=brand,
        embedding=IDENTICAL,
        event_stage="adopted",
        config=CONFIG,
        now=NOW,
    )
    assert later.action == "supersede"
    assert later.matched_candidate_id == existing


@pytest.mark.parametrize("status", sorted(CANDIDATE_LIVE_STATUSES))
def test_every_live_status_participates_in_the_live_window(db, status: str) -> None:
    """The window is defined by ``CANDIDATE_LIVE_STATUSES``; a status left out
    of it would show up as duplicate articles, not as an error."""
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=IDENTICAL, status=status)
    decision = check_post_guard(
        brand_id_fk=brand,
        embedding=IDENTICAL,
        event_stage="adopted",
        config=CONFIG,
        now=NOW,
    )
    assert decision.action in ("duplicate", "supersede")


@pytest.mark.parametrize("status", ["expired", "failed", "superseded", "rejected"])
def test_terminal_statuses_are_not_in_the_live_window(db, status: str) -> None:
    """An expired or failed candidate must not block the story coming back —
    that is what the TTL was for. ``published`` is terminal too but has its own
    window, threshold and tests below."""
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=IDENTICAL, status=status)
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )


def test_a_published_candidate_with_no_published_at_is_out_of_the_window(db) -> None:
    """The published window is keyed on ``published_at``, not ``created_at``:
    "опубликовано за N дней" is about the publication date. A row marked
    published without one is a data defect and must not silently act as an
    eternal block on the topic."""
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        status="published",
        published_at=None,
    )
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )


def test_the_live_threshold_comes_from_the_config(db) -> None:
    """0.955 is a duplicate at the default 0.90 and a fresh candidate at 0.97 —
    the number has to be the operator's, not the module's."""
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=IDENTICAL)

    default = check_post_guard(
        brand_id_fk=brand,
        embedding=NEAR,
        event_stage="adopted",
        config=CandidateDedupConfig(threshold_live=0.90),
        now=NOW,
    )
    assert default.action == "duplicate"
    assert default.similarity == pytest.approx(0.955, abs=0.01)

    strict = check_post_guard(
        brand_id_fk=brand,
        embedding=NEAR,
        event_stage="adopted",
        config=CandidateDedupConfig(threshold_live=0.97),
        now=NOW,
    )
    assert strict.action == "create"


def test_an_unrelated_item_is_not_a_duplicate(db) -> None:
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=IDENTICAL)
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=FAR,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )


def test_an_unknown_stage_resolves_to_duplicate_not_supersede(db) -> None:
    """The conservative side: writing the same story twice is a visible
    editorial failure, a missed follow-up is a candidate the next intake sees
    again."""
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=IDENTICAL, event_stage=None)
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "duplicate"
    )


# --- DoD 5: the rejected window ------------------------------------------


def test_a_match_in_the_rejected_window_copies_the_verdict_for_free(db) -> None:
    """NTS_098 §3: "не платим стражу повторно, reason_code копируется". This is
    the one window whose whole purpose is not spending money, so it must decide
    BEFORE the guard call."""
    brand = db["brand_id"]
    rejected = _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        status="rejected",
        verdict="reject",
        reason_code="personnel",
        reason="Appointment with no policy change.",
    )
    decision = check_pre_guard(
        brand_id_fk=brand,
        embedding=IDENTICAL,
        input_kind="news",
        primary_doc_url=None,
        config=CONFIG,
        now=NOW,
    )
    assert decision.action == "copy_rejected"
    assert decision.matched_candidate_id == rejected
    assert decision.reason_code == "personnel"
    assert decision.reason == "Appointment with no policy change."


def test_the_rejected_window_expires(db) -> None:
    """14 days by default — past that the same headline gets a fresh look,
    because the rubric may have changed underneath it."""
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        status="rejected",
        verdict="reject",
        reason_code="personnel",
        created_at=NOW - timedelta(days=20),
    )
    assert (
        check_pre_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            input_kind="news",
            primary_doc_url=None,
            config=CONFIG,
            now=NOW,
        ).action
        == "guard"
    )


def test_the_rejected_threshold_is_stricter_than_live_and_configurable(db) -> None:
    """0.92 vs 0.90 in the spec: reusing a *rejection* needs more confidence
    than declining to duplicate, because the cost of being wrong is a story
    silently never judged."""
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        status="rejected",
        verdict="reject",
        reason_code="forecast",
        created_at=NOW,
    )
    # 0.955 clears 0.92 …
    assert (
        check_pre_guard(
            brand_id_fk=brand,
            embedding=NEAR,
            input_kind="news",
            primary_doc_url=None,
            config=CandidateDedupConfig(threshold_rejected=0.92),
            now=NOW,
        ).action
        == "copy_rejected"
    )
    # … and 0.852 does not.
    assert (
        check_pre_guard(
            brand_id_fk=brand,
            embedding=FARISH,
            input_kind="news",
            primary_doc_url=None,
            config=CandidateDedupConfig(threshold_rejected=0.92),
            now=NOW,
        ).action
        == "guard"
    )


# --- DoD 5: the published window -----------------------------------------


def test_the_published_window_uses_its_own_threshold_and_window(db) -> None:
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        status="published",
        event_stage="adopted",
        published_at=NOW - timedelta(days=30),
    )
    # 0.852 clears the looser published threshold of 0.88? No — it does not,
    # which is the point of pinning the number rather than the direction.
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=FARISH,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=NEAR,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "duplicate"
    )
    # A later stage of a published story is a follow-up, not a duplicate.
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=NEAR,
            event_stage="in_force",
            config=CONFIG,
            now=NOW,
        ).action
        == "supersede"
    )


def test_a_publication_older_than_the_window_stops_blocking(db) -> None:
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        status="published",
        published_at=NOW - timedelta(days=90),
    )
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )


# --- DoD 5: the document URL key -----------------------------------------


def test_one_document_one_candidate(db) -> None:
    """NTS_098 §3, and decided without an embedding: for ``input_kind=document``
    the normalised ``primary_doc_url`` is the key."""
    brand = db["brand_id"]
    existing = _make_candidate(
        brand,
        topic_id="a",
        embedding=IDENTICAL,
        input_kind="document",
        primary_doc_url="https://www.finma.ch/en/docs/Circular-2026-1.pdf",
    )
    decision = check_pre_guard(
        brand_id_fk=brand,
        embedding=FAR,  # deliberately dissimilar — the URL alone decides
        input_kind="document",
        primary_doc_url="https://WWW.FINMA.CH/en/docs/Circular-2026-1.pdf?utm=rss",
        config=CONFIG,
        now=NOW,
    )
    assert decision.action == "skip_doc_url"
    assert decision.matched_candidate_id == existing
    assert decision.window == "doc_url"


def test_the_doc_url_key_does_not_apply_to_news_input(db) -> None:
    """A news item's URL is the article, not the document. Keying on it would
    make two outlets covering one story indistinguishable from one outlet
    covering it twice."""
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=FAR,
        input_kind="document",
        primary_doc_url="https://x.test/doc.pdf",
    )
    assert (
        check_pre_guard(
            brand_id_fk=brand,
            embedding=FAR,
            input_kind="news",
            primary_doc_url="https://x.test/doc.pdf",
            config=CONFIG,
            now=NOW,
        ).action
        == "guard"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://A.test/Doc.PDF", "https://a.test/Doc.PDF"),
        ("https://a.test/doc.pdf?utm=x&s=1", "https://a.test/doc.pdf"),
        ("https://a.test/doc.pdf#page=3", "https://a.test/doc.pdf"),
        ("https://a.test/docs/", "https://a.test/docs"),
        ("", None),
        (None, None),
    ],
)
def test_doc_url_normalisation(raw, expected) -> None:
    """Host case and query folded, path case preserved: plenty of document
    stores are case-sensitive, and folding the path would collide two real
    documents."""
    assert normalize_doc_url(raw) == expected


# --- fail-open ------------------------------------------------------------


def test_a_candidate_with_no_stored_embedding_does_not_match_everything(db) -> None:
    """A missing embedding must exclude the row from similarity dedup, not be
    compared as a zero vector (which matches everything equally badly)."""
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=None)
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )


def test_an_embedding_of_a_different_dimension_is_skipped_not_fatal(db) -> None:
    """A model swap leaves old vectors of another width in the table. Comparing
    across models is noise, and treating noise as a duplicate drops real
    stories."""
    brand = db["brand_id"]
    _make_candidate(
        brand, topic_id="a", embedding=np.ones(16, dtype=np.float32)
    )
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )


def test_dedup_fails_open_when_the_database_is_unreachable(db, monkeypatch) -> None:
    """NTS_079's contract, carried over: a DB hiccup must resolve to "not a
    duplicate". A duplicate costs one article a human can spot; a real story
    dropped by an infrastructure error is invisible."""
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=IDENTICAL)

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("pipeline.admin.db.get_session_factory", boom)
    assert (
        check_post_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            event_stage="adopted",
            config=CONFIG,
            now=NOW,
        ).action
        == "create"
    )
    assert (
        check_pre_guard(
            brand_id_fk=brand,
            embedding=IDENTICAL,
            input_kind="document",
            primary_doc_url="https://x.test/d.pdf",
            config=CONFIG,
            now=NOW,
        ).action
        == "guard"
    )


# --- DoD 3: the atomic claim ---------------------------------------------


def test_claim_pending_moves_the_row_and_stamps_selected_at(db) -> None:
    brand = db["brand_id"]
    candidate_id = _make_candidate(brand, topic_id="a", embedding=IDENTICAL)
    assert claim_pending(candidate_id, now=NOW) is True
    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        assert row.status == "selected"
        assert row.selected_at is not None
    # A second claim of the same row loses.
    assert claim_pending(candidate_id, now=NOW) is False


@pytest.mark.parametrize("status", ["selected", "in_production", "drafted", "rejected"])
def test_claim_pending_refuses_anything_but_pending(db, status: str) -> None:
    brand = db["brand_id"]
    candidate_id = _make_candidate(
        brand, topic_id="a", embedding=IDENTICAL, status=status
    )
    assert claim_pending(candidate_id, now=NOW) is False


_RACE_CHILD = textwrap.dedent(
    """
    import os, sys
    os.environ["ADMIN_DB_PATH"] = sys.argv[1]
    from pipeline.admin import db as admin_db
    from pipeline.common import config as config_module
    config_module._settings = None
    admin_db.reset_for_tests()
    from pipeline.selector.candidate_store import claim_pending
    # Both children block on the same row id; SQLite serialises the writes.
    won = claim_pending(int(sys.argv[2]))
    print("WON" if won else "LOST")
    """
)


def test_pending_to_selected_is_atomic_across_two_processes(db, tmp_path) -> None:
    """DoD 3, with the spec's «два процесса».

    Two production runs overlapping is not exotic — a cron firing while the
    operator presses "Run now" does it. The failure mode is two runs producing
    the same article and paying twice. A read-then-write claim passes every
    single-threaded test there is, so the race has to be run for real.
    """
    brand = db["brand_id"]
    candidate_id = _make_candidate(brand, topic_id="a", embedding=IDENTICAL)

    script = tmp_path / "race_child.py"
    script.write_text(_RACE_CHILD)
    env = {**os.environ, "ADMIN_DB_PATH": str(db["path"])}
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(db["path"]), str(candidate_id)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(script.parent.parents[0]),
        )
        for _ in range(2)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        outputs.append(out.strip().splitlines()[-1])

    assert sorted(outputs) == ["LOST", "WON"], outputs
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, candidate_id).status == "selected"


# --- creation, TTL, caps, supersede -------------------------------------


def test_an_accept_lands_pending_and_a_reject_lands_rejected(db) -> None:
    brand = db["brand_id"]
    accept_id = create_candidate(
        CandidateInput(
            brand_id_fk=brand,
            input_kind="news",
            source_id_fk=None,
            source_title="Council adopts DAC8",
            source_summary="s",
            source_url="https://x.test/1",
            source_published_at=NOW,
            source_language="en",
            source_name="ESMA",
            source_class="regulator",
            topic_embedding_ref="a",
            verdict="accept",
            reason_code="ok",
            reason="adopted",
            jurisdictions=("EU", "PL"),
            event_stage="adopted",
            depth_prior="deep",
        ),
        now=NOW,
    )
    reject_id = create_candidate(
        CandidateInput(
            brand_id_fk=brand,
            input_kind="news",
            source_id_fk=None,
            source_title="Bank appoints CEO",
            source_summary="s",
            source_url="https://x.test/2",
            source_published_at=NOW,
            source_language="en",
            source_name="Wire",
            source_class="news",
            topic_embedding_ref="b",
            verdict="reject",
            reason_code="personnel",
            reason="appointment, no policy change",
        ),
        now=NOW,
    )
    with admin_db.get_session_factory()() as session:
        accept = session.get(Candidate, accept_id)
        reject = session.get(Candidate, reject_id)
        assert accept.status == "pending"
        assert json.loads(accept.jurisdictions) == ["EU", "PL"]
        # Rejects are STORED (NTS_098 §2, retention_days_rejected): the reject
        # distribution is the only evidence the rubric is right.
        assert reject.status == "rejected"
        assert reject.reason_code == "personnel"


def test_expires_at_is_set_from_the_ttl_config_by_event_stage(db) -> None:
    brand = db["brand_id"]
    ttl = {"deal_announced": 7, "consultation": 21, "default": 14}
    for stage, days in (("deal_announced", 7), ("consultation", 21), ("ruling", 14)):
        candidate_id = create_candidate(
            CandidateInput(
                brand_id_fk=brand,
                input_kind="news",
                source_id_fk=None,
                source_title=f"t {stage}",
                source_summary=None,
                source_url=None,
                source_published_at=None,
                source_language="en",
                source_name="n",
                source_class="news",
                topic_embedding_ref=None,
                verdict="accept",
                reason_code="ok",
                reason="r",
                jurisdictions=("EU",),
                event_stage=stage,
            ),
            ttl_config=ttl,
            now=NOW,
        )
        with admin_db.get_session_factory()() as session:
            row = session.get(Candidate, candidate_id)
        assert row.expires_at == (NOW + timedelta(days=days)).replace(tzinfo=None)


def test_ttl_falls_back_to_14_days_when_the_config_is_unusable() -> None:
    """A candidate with no expiry lives forever if the TTL pass is ever off."""
    assert ttl_for_stage(None, "ruling") == 14
    assert ttl_for_stage("not a dict", "ruling") == 14
    assert ttl_for_stage({"default": 9}, "ruling") == 9


def test_the_daily_cap_is_counted_in_the_brands_day_not_utc(db) -> None:
    """NTS_098 §5 / NTS_099 §5. Counting in UTC moves the boundary twice a year
    and makes the cap silently wrong in exactly the DST weeks."""
    brand = db["brand_id"]
    # 22:30 UTC on the 27th is 00:30 on the 28th in Madrid (CEST, +2).
    late_utc = datetime(2026, 8, 27, 22, 30, tzinfo=UTC)
    _make_candidate(
        brand, topic_id="a", embedding=None, created_at=late_utc, input_kind="news"
    )
    assert (
        count_accepted_today(
            brand_id_fk=brand,
            input_kind="news",
            now=datetime(2026, 8, 28, 8, 0, tzinfo=UTC),
            timezone_name="Europe/Madrid",
        )
        == 1
    )
    # In UTC the same row belongs to the previous day.
    assert (
        count_accepted_today(
            brand_id_fk=brand,
            input_kind="news",
            now=datetime(2026, 8, 28, 8, 0, tzinfo=UTC),
            timezone_name="UTC",
        )
        == 0
    )


def test_the_cap_counts_each_input_kind_separately(db) -> None:
    brand = db["brand_id"]
    _make_candidate(brand, topic_id="a", embedding=None, input_kind="document")
    _make_candidate(brand, topic_id="b", embedding=None, input_kind="document")
    assert (
        count_accepted_today(
            brand_id_fk=brand,
            input_kind="document",
            now=NOW,
            timezone_name="Europe/Madrid",
        )
        == 2
    )
    assert (
        count_accepted_today(
            brand_id_fk=brand,
            input_kind="news",
            now=NOW,
            timezone_name="Europe/Madrid",
        )
        == 0
    )


def test_rejects_do_not_consume_the_daily_cap(db) -> None:
    brand = db["brand_id"]
    _make_candidate(
        brand,
        topic_id="a",
        embedding=None,
        verdict="reject",
        reason_code="forecast",
        status="rejected",
    )
    assert (
        count_accepted_today(
            brand_id_fk=brand, input_kind="news", now=NOW, timezone_name="UTC"
        )
        == 0
    )


def test_an_unknown_timezone_degrades_to_utc_instead_of_raising(db) -> None:
    """The cap boundary being wrong by an hour is smaller than not judging the
    feed at all."""
    start, end = brand_day_bounds(now=NOW, timezone_name="Mars/Olympus")
    assert (start, end) == (
        datetime(2026, 8, 28, tzinfo=UTC),
        datetime(2026, 8, 29, tzinfo=UTC),
    )


def test_mark_superseded_only_retires_a_candidate_with_no_work_attached(db) -> None:
    """NTS_098 §2 allows it from pending/doc_missing only: past that there is a
    draft attached, and silently retiring it orphans the draft."""
    brand = db["brand_id"]
    for status, expected in (
        ("pending", True),
        ("doc_missing", True),
        ("in_production", False),
        ("drafted", False),
        ("ready", False),
    ):
        candidate_id = _make_candidate(
            brand, topic_id=f"t{status}", embedding=None, status=status
        )
        assert mark_superseded(candidate_id) is expected, status


def test_recent_accepted_titles_are_newest_first_and_accepts_only(db) -> None:
    """This is what lets the guard answer ``duplicate_stage`` on its own."""
    brand = db["brand_id"]
    _make_candidate(
        brand, topic_id="old", embedding=None, created_at=NOW - timedelta(days=2)
    )
    _make_candidate(brand, topic_id="new", embedding=None, created_at=NOW)
    _make_candidate(
        brand,
        topic_id="rej",
        embedding=None,
        verdict="reject",
        reason_code="forecast",
        status="rejected",
    )
    titles = recent_accepted_titles(brand_id_fk=brand, limit=10)
    assert titles == ("item new", "item old")
