"""IT_PROJ_NTS_100 — the rank formula, the batch, the budget, the failures (S4).

Eight DoD lines, each with a test that fails for the reason the line exists:

1. rank is the formula, weights come from config, every term is in the log
2. two topics in one category — the second is penalised; a strong one still wins
3. ``production_batch`` is unique per (brand, day); a second run is a no-op
4. a failure on translation returns the candidate to ``pending`` and the retry
   reuses the fact pack (no second ``research`` row in ``cost_records``)
5. ``attempts >= max_attempts`` → ``failed``, and the alert gatherer sees it
6. a ``translation:uk`` return re-runs only the UK translation
7. an empty portfolio is ``success``; the thin-slot alert fires three days out
8. promote / hold / reject beat the formula

Nothing here touches a network. The generation seams are monkeypatched at the
same boundary ``run_pipeline``'s own tests use (``generate_draft_for_language``,
``translate_draft_for_language``, ``build_fact_pack_for_topic``), because the
subject under test is the *rhythm* — who is chosen, how often, and what happens
when a stage throws — not the writer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    Brand,
    BrandTaxonomy,
    Candidate,
    CostRecord,
    FactPack,
    PipelineConfig,
    ProductionBatch,
    ReviewDecision,
)
from pipeline.common import config as config_module
from pipeline.common.models import Draft, Language
from pipeline.selector import portfolio_sweep
from pipeline.selector.ranking import (
    CandidateFacts,
    RankWeights,
    default_weights,
    score_candidate,
    select_batch,
    tier_of,
)
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
TIERS = {"tier1": ["EU", "CH"], "tier2": ["US"]}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        brand_id = seed_icon_brand(session, with_sanity_creds=True)
        # The four-language fanout is the shape production actually runs in;
        # a single-language brand would let the translation tests pass without
        # a translation ever happening.
        session.get(Brand, brand_id).languages = json.dumps(
            ["en", "ru", "uk", "pl"]
        )
        session.add(
            PipelineConfig(
                brand_id_fk=brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps([]),
                voice_profile="mission: x\n",
                production_enabled=True,
                weekly_draft_budget=6,
                images_on_demand=True,
                research_enabled=True,
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
    yield brand_id
    admin_db.reset_for_tests()


def _candidate(
    brand_id: int,
    *,
    title: str = "A directive was adopted",
    status: str = "pending",
    input_kind: str = "document",
    confidence: float = 0.8,
    depth_prior: str = "article",
    event_stage: str = "adopted",
    jurisdictions: tuple[str, ...] = ("EU",),
    service_category: str = "structuring",
    manual_action: str | None = None,
    primary_doc_url: str | None = "https://reg.test/doc",
    doc_match: str | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    selected_at: datetime | None = None,
    attempts: int = 0,
    doc_attempts: int = 0,
    return_scope: str | None = None,
) -> int:
    with admin_db.get_session_factory()() as session:
        row = Candidate(
            brand_id_fk=brand_id,
            input_kind=input_kind,
            source_title=title,
            source_summary="The measure was adopted with a 2027 reporting period.",
            source_url="https://feed.test/item",
            source_published_at=(created_at or NOW) - timedelta(hours=6),
            verdict="accept",
            reason_code="ok",
            reason="adopted instrument with a 2027 obligation",
            confidence=confidence,
            service_category=service_category,
            jurisdictions=json.dumps(list(jurisdictions)),
            event_stage=event_stage,
            depth_prior=depth_prior,
            primary_doc_url=primary_doc_url,
            doc_match=doc_match,
            status=status,
            manual_action=manual_action,
            attempts=attempts,
            doc_attempts=doc_attempts,
            return_scope=return_scope,
            created_at=(created_at or NOW).replace(tzinfo=None),
            expires_at=(expires_at or NOW + timedelta(days=14)).replace(tzinfo=None),
            selected_at=selected_at.replace(tzinfo=None) if selected_at else None,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def _facts(candidate_id: int, **overrides) -> CandidateFacts:
    base = {
        "candidate_id": candidate_id,
        "confidence": 0.8,
        "depth_prior": "article",
        "event_stage": "adopted",
        "jurisdictions": ("EU",),
        "input_kind": "document",
        "service_category": "structuring",
        "created_at": NOW,
        "source_published_at": NOW,
        "manual_action": None,
    }
    base.update(overrides)
    return CandidateFacts(**base)  # type: ignore[arg-type]


class _Writer:
    """Stands in for the whole generation stack. Records what it was asked."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.drafted: list[str] = []
        self.translated: list[str] = []
        self.research_calls = 0
        self.documents_seen: list[Any] = []

    async def fact_pack(self, topic, *, research_enabled=True, budget=None, document=None):
        from pipeline.generator.research import Fact
        from pipeline.generator.research import FactPack as Pack

        self.research_calls += 1
        self.documents_seen.append(document)
        return Pack(
            source_facts=[
                Fact(text="The threshold rises to EUR 5m", url="https://reg.test/doc")
            ],
            citations=["https://reg.test/doc"],
            searches=1,
        )

    async def draft(self, topic, brand, language, fact_pack=None):
        if self.fail_on == "canon":
            raise RuntimeError("the writer blew up")
        self.drafted.append(language.value)
        return Draft(
            topic_id=topic.id,
            brand_id=brand.slug,
            language=language,
            title=f"EN {topic.raw.title}",
            body="## Heading\n\nBody text that is long enough to be a body.",
            key_takeaway="A takeaway.",
        )

    async def translate(self, topic, brand, language, en_draft):
        if self.fail_on == "translate":
            raise RuntimeError("translation exploded")
        self.translated.append(language.value)
        return Draft(
            topic_id=topic.id,
            brand_id=brand.slug,
            language=language,
            title=f"{language.value.upper()} {topic.raw.title}",
            body="## Heading\n\nTranslated body.",
            key_takeaway="Takeaway.",
        )


class _Publisher:
    def __init__(self) -> None:
        self.batches: list[list] = []

    async def publish_draft_batch(self, posts):
        self.batches.append(list(posts))
        return [f"drafts.{p.language.value}-{len(self.batches)}" for p in posts]

    async def upload_cover_image(self, image_bytes, filename):  # pragma: no cover
        return "image-test"


class _Documents:
    """Stands in for the S5 document stage. Records what it was asked for."""

    def __init__(self, *, usable: bool = True) -> None:
        self.usable = usable
        self.calls: list[int] = []

    async def resolve(self, *, candidate, sources, budget, now=None, **kw):
        from pipeline.sources.document_fetcher import (
            DocMatch,
            DocumentOutcome,
            ExtractedDocument,
        )

        self.calls.append(int(candidate.id))
        if not self.usable:
            return DocumentOutcome(
                status="doc_missing", how="registry", reason="nothing on file"
            )
        return DocumentOutcome(
            status="ok",
            document=ExtractedDocument(
                url="https://reg.test/doc",
                text="ARTICLE 1\nThe threshold rises to EUR 5m.\n\n"
                "ENTRY INTO FORCE\nApplies from 1 January 2027.",
                content_hash="abc",
                content_type="text/html",
                byte_size=100,
                fetched_at=NOW,
                version_id=7,
            ),
            match=DocMatch("match", "the feed item is the document", "exact"),
            how="item_url",
        )


@pytest.fixture
def wired(monkeypatch):
    """Patch the generation seams, the document stage and the Sanity publisher."""
    writer = _Writer()
    publisher = _Publisher()
    documents = _Documents()
    import pipeline.production as production
    import pipeline.run as run_mod
    import pipeline.sources.document_fetcher as doc_mod

    # S5: production fetches the primary document before research. Stubbed at
    # the module boundary, because what these tests are about is the rhythm —
    # the fetcher has its own suite.
    monkeypatch.setattr(doc_mod, "resolve_document", documents.resolve)

    monkeypatch.setattr(run_mod, "build_fact_pack_for_topic", writer.fact_pack)
    monkeypatch.setattr(run_mod, "generate_draft_for_language", writer.draft)
    monkeypatch.setattr(run_mod, "translate_draft_for_language", writer.translate)

    async def _category(raw, brand):
        return "structuring"

    monkeypatch.setattr(run_mod, "assign_category", _category)
    monkeypatch.setattr(
        production, "_DryRunPublisher", lambda: publisher, raising=True
    )

    import pipeline.publisher.sanity as sanity_mod

    monkeypatch.setattr(
        sanity_mod, "SanityPublisher", lambda client=None: publisher
    )
    monkeypatch.setattr(sanity_mod, "SanityClient", lambda **kw: object())
    return writer, publisher, documents


# --------------------------------------------------------------------------
# DoD 1 — the formula, its weights and its log
# --------------------------------------------------------------------------


def test_rank_is_the_sum_of_the_seven_terms_and_every_term_is_reported():
    """"Слагаемые в логе на каждого кандидата" — the breakdown IS the answer.

    The weights were picked by eye (NTS_100 §Риски); the only way to correct
    them is to read why one candidate beat another, and a bare float cannot
    say that.
    """
    ranked = score_candidate(
        _facts(1),
        weights=RankWeights(),
        tiers=TIERS,
        now=NOW,
        same_category_taken=1,
        same_jurisdiction_taken=2,
    )
    contributions = {
        k: v for k, v in ranked.terms.items() if not k.startswith("_")
    }
    assert set(contributions) == {
        "conf",
        "depth",
        "fresh",
        "juris",
        "kind",
        "div_penalty",
        "juris_div_penalty",
    }
    assert ranked.rank == pytest.approx(sum(contributions.values()))
    # The two penalties are subtractive and scale with the count.
    assert contributions["div_penalty"] == pytest.approx(-0.20)
    assert contributions["juris_div_penalty"] == pytest.approx(-0.20)
    # And the inputs behind the derived terms are logged too.
    assert ranked.terms["_tier"] == 1.0
    assert "_age_days" in ranked.terms


def test_weights_come_from_the_config_row_not_from_code(db):
    """A weight that needs a deploy to change is a weight nobody changes."""
    with admin_db.get_session_factory()() as session:
        row = session.get(PipelineConfig, db)
        row.rank_weights = json.dumps({"w_conf": 1.0, "w_depth": 0.0})
        session.commit()
    from pipeline.admin.config_client import AdminConfigClient

    config = AdminConfigClient(brand_slug="icon").get_config()
    weights = RankWeights.from_config(config)
    assert weights.w_conf == 1.0
    assert weights.w_depth == 0.0
    # Unmentioned keys keep the spec's starting values rather than becoming 0.
    assert weights.w_fresh == default_weights()["w_fresh"]


def test_unreadable_weights_fall_back_to_the_spec_rather_than_to_zero():
    """Zero for every weight is an ordering too — a silent one."""
    assert RankWeights.from_config("{not json") == RankWeights()


def test_freshness_uses_the_event_date_not_the_intake_date():
    """A directive published three weeks ago is three weeks old even if the
    parser only noticed it today; otherwise a slow parser is rewarded."""
    old_event = _facts(
        1, source_published_at=NOW - timedelta(days=30), created_at=NOW
    )
    fresh_event = _facts(2, source_published_at=NOW, created_at=NOW)
    assert (
        score_candidate(
            old_event, weights=RankWeights(), tiers=TIERS, now=NOW
        ).terms["fresh"]
        < score_candidate(
            fresh_event, weights=RankWeights(), tiers=TIERS, now=NOW
        ).terms["fresh"]
    )


def test_an_unknown_jurisdiction_is_tier3_not_tier1():
    """An unparsed verdict must not float to the top of the board."""
    assert tier_of((), TIERS) == "tier3"
    assert tier_of(("ZZ",), TIERS) == "tier3"
    assert tier_of(("US", "EU"), TIERS) == "tier1"


# --------------------------------------------------------------------------
# DoD 2 — diversity is a penalty, not a rule
# --------------------------------------------------------------------------


def test_the_second_topic_in_a_category_is_penalised():
    picked = select_batch(
        [_facts(1), _facts(2)],
        weights=RankWeights(),
        tiers=TIERS,
        now=NOW,
        limit=2,
    )
    assert [p.candidate_id for p in picked] == [1, 2]
    assert picked[0].terms["div_penalty"] == 0.0
    assert picked[1].terms["div_penalty"] == pytest.approx(-0.20)


def test_a_strong_topic_beats_the_diversity_penalty():
    """NTS_100 §2: a strong topic whose rank clears the penalty is taken; a
    weak one taken only to make the week look varied is not. A filter could not
    express that difference; a penalty can."""
    strong_same_category = _facts(2, confidence=1.0, depth_prior="deep")
    weak_other_category = _facts(
        3, confidence=0.2, depth_prior="note", service_category="wealth"
    )
    picked = select_batch(
        [_facts(1), strong_same_category, weak_other_category],
        weights=RankWeights(),
        tiers=TIERS,
        now=NOW,
        limit=2,
    )
    # The strong one leads; the second pick is its *same-category* neighbour
    # taking the full penalty, not the weak topic from another category that a
    # diversity *rule* would have promoted.
    assert [p.candidate_id for p in picked] == [2, 1]
    assert picked[1].terms["div_penalty"] == pytest.approx(-0.20)
    assert 3 not in [p.candidate_id for p in picked]


def test_the_week_already_taken_counts_towards_the_penalty():
    """The penalty is about the week, not about this run."""
    picked = select_batch(
        [_facts(1)],
        weights=RankWeights(),
        tiers=TIERS,
        now=NOW,
        limit=1,
        category_counts={"structuring": 3},
    )
    assert picked[0].terms["div_penalty"] == pytest.approx(-0.60)


# --------------------------------------------------------------------------
# DoD 8 — a manager's decision is not an input to the formula
# --------------------------------------------------------------------------


def test_a_promoted_candidate_is_taken_first_even_when_it_ranks_last():
    weak_but_promoted = _facts(
        9, confidence=0.05, depth_prior="note", manual_action="promoted"
    )
    picked = select_batch(
        [_facts(1), _facts(2), weak_but_promoted],
        weights=RankWeights(),
        tiers=TIERS,
        now=NOW,
        limit=2,
    )
    assert picked[0].candidate_id == 9
    assert picked[0].terms["_promoted"] == 1.0


def test_a_held_candidate_never_reaches_the_formula(db):
    from pipeline.production import eligible_candidates

    _candidate(db, manual_action="held")
    open_one = _candidate(db)
    ids = [c.candidate_id for c in eligible_candidates(brand_id_fk=db, now=NOW)]
    assert ids == [open_one]


def test_a_news_lead_out_of_document_retries_is_not_eligible(db):
    """The standing rule of NTS_123: no article from a retelling.

    S5 makes a news lead *without* a document eligible — for the document
    search, which is the stage that refuses to go on when it finds nothing
    (NTS_101 §2, §7). What stops being eligible is a lead that has used its
    retry budget: searching for it on every run forever would be spend with a
    known answer.
    """
    from pipeline.production import eligible_candidates

    exhausted = _candidate(db, input_kind="news", doc_match=None, doc_attempts=2)
    searchable = _candidate(db, input_kind="news", primary_doc_url=None)
    with_document = _candidate(db, input_kind="news", doc_match="exact")
    always_eligible = _candidate(db, input_kind="document")
    ids = sorted(
        c.candidate_id
        for c in eligible_candidates(brand_id_fk=db, now=NOW, doc_retries=2)
    )
    assert ids == sorted([searchable, with_document, always_eligible])
    assert exhausted not in ids


def test_a_document_kind_candidate_never_waits_on_the_search(db):
    """NTS_101 §2 — the feed item IS the document, so there is nothing to find
    and nothing to check; a retry budget must not gate it."""
    from pipeline.production import eligible_candidates

    cid = _candidate(db, input_kind="document", doc_attempts=9)
    ids = [
        c.candidate_id
        for c in eligible_candidates(brand_id_fk=db, now=NOW, doc_retries=2)
    ]
    assert ids == [cid]


def test_an_expired_candidate_is_not_eligible(db):
    from pipeline.production import eligible_candidates

    _candidate(db, expires_at=NOW - timedelta(days=1))
    assert eligible_candidates(brand_id_fk=db, now=NOW) == []


# --------------------------------------------------------------------------
# DoD 3 — one batch per brand per day
# --------------------------------------------------------------------------


def test_the_batch_claim_is_the_unique_constraint_not_a_query(db):
    from datetime import date

    from pipeline.production import claim_batch

    assert claim_batch(
        brand_id_fk=db, batch_date=date(2026, 9, 6), run_id=1, now=NOW
    )
    assert not claim_batch(
        brand_id_fk=db, batch_date=date(2026, 9, 6), run_id=2, now=NOW
    )
    # Tomorrow is a different day, and a different brand is a different claim.
    assert claim_batch(
        brand_id_fk=db, batch_date=date(2026, 9, 7), run_id=3, now=NOW
    )


async def test_a_second_run_the_same_day_takes_nothing(db, wired):
    from pipeline.production import run_production

    _candidate(db)
    first = await run_production(brand_slug="icon", dry_run=True)
    assert first.selected == 1

    _candidate(db, title="Another adopted directive")
    second = await run_production(brand_slug="icon", dry_run=True)
    assert second.selected == 0
    assert second.stopped_reason == "batch_already_run"
    # And the no-op is a SUCCESS, not a failure: nothing went wrong.
    with admin_db.get_session_factory()() as session:
        batches = session.query(ProductionBatch).all()
        assert len(batches) == 1
        assert batches[0].selected_count == 1


# --------------------------------------------------------------------------
# the weekly budget
# --------------------------------------------------------------------------


async def test_the_weekly_budget_is_not_exceeded_by_repeated_runs(db, wired):
    from pipeline.production import run_production

    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, db).weekly_draft_budget = 2
        session.commit()
    for i in range(4):
        _candidate(db, title=f"Directive {i}")

    first = await run_production(brand_slug="icon", dry_run=True, force=True)
    assert first.selected == 2
    # ``force`` bypasses the daily batch, so this is purely the budget talking.
    second = await run_production(brand_slug="icon", dry_run=True, force=True)
    assert second.selected == 0
    assert second.stopped_reason == "weekly_budget_exhausted"
    assert second.taken_this_week == 2


async def test_limit_caps_the_batch_below_the_weekly_budget(db, wired):
    from pipeline.production import run_production

    for i in range(3):
        _candidate(db, title=f"Directive {i}")
    stats = await run_production(brand_slug="icon", dry_run=True, limit=1)
    assert stats.selected == 1


# --------------------------------------------------------------------------
# DoD 7 — an empty portfolio is a valid outcome
# --------------------------------------------------------------------------


async def test_an_empty_portfolio_is_a_successful_run(db, wired):
    from pipeline.admin.models import Run
    from pipeline.production import run_production

    stats = await run_production(brand_slug="icon", dry_run=True)
    assert stats.selected == 0
    assert stats.stopped_reason == "empty_portfolio"
    with admin_db.get_session_factory()() as session:
        run = session.query(Run).order_by(Run.id.desc()).first()
        assert run.status == "success"
        assert run.run_type == "production"


def test_the_thin_portfolio_alert_fires_three_days_before_the_slot(db):
    """NTS_100 §3.5 — three days out, while there is still time to act."""
    from pipeline.monitoring.alerts import check_thin_portfolio

    slots = json.dumps([{"day": "wed", "capacity": 2}])
    # 2026-09-06 is a Sunday; three days later is Wednesday the 9th.
    pulse = check_thin_portfolio(
        brand_id_fk=db,
        slots=slots,
        timezone_name="Europe/Madrid",
        now=NOW,
        brand_name=None,
    )
    assert pulse is not None
    key, message = pulse
    assert key.endswith("2026-09-09")
    assert "2026-09-09" in message

    # Two candidates on their way to it — nothing to warn about.
    _candidate(db, status="drafted")
    _candidate(db, status="ready")
    assert (
        check_thin_portfolio(
            brand_id_fk=db,
            slots=slots,
            timezone_name="Europe/Madrid",
            now=NOW,
        )
        is None
    )


def test_no_alert_for_a_day_that_is_not_a_slot(db):
    from pipeline.monitoring.alerts import check_thin_portfolio

    assert (
        check_thin_portfolio(
            brand_id_fk=db,
            slots=json.dumps([{"day": "mon", "capacity": 2}]),
            timezone_name="Europe/Madrid",
            now=NOW,
        )
        is None
    )


# --------------------------------------------------------------------------
# DoD 4 and 5 — failure, rollback, reuse, and the terminal state
# --------------------------------------------------------------------------


async def test_a_failure_on_translation_returns_the_candidate_and_keeps_the_pack(
    db, monkeypatch, wired
):
    """DoD 4 — the retry must not buy the same research twice."""
    import pipeline.run as run_mod
    from pipeline.production import run_production

    writer, _publisher, _documents = wired
    cid = _candidate(db)
    monkeypatch.setattr(run_mod, "translate_draft_for_language", _boom)

    stats = await run_production(brand_slug="icon", dry_run=True, force=True)
    assert stats.failed == 1
    assert stats.drafted == 0
    assert writer.research_calls == 1
    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, cid)
        assert row.status == "pending"
        assert row.attempts == 1
        assert "translation exploded" in (row.last_error or "")
        # The pack survived the failure — that is the point.
        assert session.query(FactPack).count() == 1

    # Retry: the pack is reused, so research is not called a second time.
    monkeypatch.setattr(run_mod, "translate_draft_for_language", writer.translate)
    stats = await run_production(brand_slug="icon", dry_run=True, force=True)
    assert stats.drafted == 1
    assert stats.reused_fact_packs == 1
    assert writer.research_calls == 1


async def _boom(*args, **kwargs):
    raise RuntimeError("translation exploded")


async def test_the_second_failure_is_terminal_and_alertable(db, monkeypatch, wired):
    """DoD 5 — ``attempts >= max_attempts`` → ``failed`` + alert."""
    import pipeline.run as run_mod
    from pipeline.monitoring.alerts import _gather_production_events
    from pipeline.production import run_production

    cid = _candidate(db)
    monkeypatch.setattr(run_mod, "generate_draft_for_language", _boom)

    for _ in range(2):
        await run_production(brand_slug="icon", dry_run=True, force=True)

    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, cid)
        assert row.status == "failed"
        assert row.attempts == 2
        assert row.failed_at is not None

    events = _gather_production_events(set(), now=NOW)
    keys = [k for k, _ in events]
    assert f"candidate_failed:{cid}" in keys
    message = next(m for k, m in events if k == f"candidate_failed:{cid}")
    assert "translation exploded" in message


async def test_a_failed_candidate_is_not_picked_up_again(db, monkeypatch, wired):
    from pipeline.production import eligible_candidates

    cid = _candidate(db, status="failed", attempts=2)
    assert [c.candidate_id for c in eligible_candidates(brand_id_fk=db, now=NOW)] == []
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, cid).status == "failed"


# --------------------------------------------------------------------------
# DoD 6 — a scoped return re-runs one stage
# --------------------------------------------------------------------------


async def test_a_translation_return_reruns_only_that_language(db, wired):
    from pipeline.production import (  # noqa: F401
        _languages_from_scope,
        _stages_to_run,
        produce_candidate,
        run_production,
    )

    stages = _stages_to_run("translation:uk")
    assert stages == {
        "research": False,
        "canon": False,
        "translations": True,
        "cover": False,
    }
    languages = [Language.en, Language.ru, Language.uk, Language.pl]
    assert _languages_from_scope(languages, "translation:uk") == [Language.uk]
    # Anything else keeps the whole fanout.
    assert _languages_from_scope(languages, "text") == languages


async def test_a_returned_candidate_regenerates_only_the_returned_language(
    db, wired
):
    from pipeline.production import produce_candidate

    writer, publisher, _documents = wired
    cid = _candidate(db, status="returned", return_scope="translation:uk")
    with admin_db.get_session_factory()() as session:
        # A pack from the first pass, so the regeneration has one to reuse.
        session.add(
            FactPack(
                brand_id_fk=db,
                candidate_id_fk=cid,
                pack=json.dumps(
                    {
                        "empty": False,
                        "source_facts": [
                            {"text": "EUR 5m", "url": "https://reg.test/doc"}
                        ],
                        "citations": ["https://reg.test/doc"],
                    }
                ),
                sources="[]",
                created_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()

    from pipeline.production import ProductionStats
    from pipeline.run import icon_brand_config

    stats = ProductionStats()
    result = await produce_candidate(
        candidate_id=cid,
        brand=icon_brand_config(),
        brand_id_fk=db,
        brand_slug="icon",
        languages=[Language.en, Language.ru, Language.uk, Language.pl],
        sanity_publisher=publisher,
        config=_config(db),
        run_id=None,
        dry_run=True,
        tag=None,
        stats=stats,
    )
    assert result["languages"] == ["uk"]
    assert writer.research_calls == 0
    assert stats.reused_fact_packs == 1


def _config(brand_id: int):
    from pipeline.admin.config_client import AdminConfigClient

    return AdminConfigClient(brand_slug="icon").get_config()


# --------------------------------------------------------------------------
# the flag and the caps
# --------------------------------------------------------------------------


async def test_production_enabled_off_is_a_cancelled_run_not_a_traceback(db, wired):
    from pipeline.admin.models import Run
    from pipeline.production import ProductionDisabled, run_production

    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, db).production_enabled = False
        session.commit()
    _candidate(db)

    with pytest.raises(ProductionDisabled):
        await run_production(brand_slug="icon", dry_run=True)
    with admin_db.get_session_factory()() as session:
        run = session.query(Run).order_by(Run.id.desc()).first()
        assert run.status == "cancelled"
        assert run.run_type == "production"
        assert session.query(Candidate).one().status == "pending"


async def test_force_runs_through_a_switched_off_flag(db, wired):
    from pipeline.production import run_production

    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, db).production_enabled = False
        session.commit()
    _candidate(db)
    stats = await run_production(brand_slug="icon", dry_run=True, force=True)
    assert stats.selected == 1


async def test_the_monthly_cap_stops_production_and_says_so(db, wired):
    """NTS_106 §3 — at 100% production does not start; intake is untouched."""
    from pipeline.admin.models import Run
    from pipeline.production import run_production

    _candidate(db)
    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, db).monthly_spend_cap_usd = 10.0
        session.add(
            CostRecord(
                brand_id_fk=db,
                provider="openai",
                operation="draft",
                cost_usd=11.0,
                created_at=datetime.now(tz=UTC).replace(tzinfo=None),
            )
        )
        session.commit()

    stats = await run_production(brand_slug="icon", dry_run=True)
    assert stats.stopped_reason == "monthly_spend_cap"
    assert stats.selected == 0
    with admin_db.get_session_factory()() as session:
        assert session.query(Run).order_by(Run.id.desc()).first().status == "cancelled"


async def test_a_candidate_over_its_own_cap_is_skipped(db, wired):
    from pipeline.production import run_production

    cid = _candidate(db)
    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, db).max_cost_per_candidate_usd = 1.0
        session.add(
            CostRecord(
                brand_id_fk=db,
                candidate_id_fk=cid,
                provider="openai",
                operation="research",
                cost_usd=2.5,
                created_at=datetime.now(tz=UTC).replace(tzinfo=None),
            )
        )
        session.commit()

    stats = await run_production(brand_slug="icon", dry_run=True)
    assert stats.drafted == 0
    assert stats.candidates == [
        {"candidate_id": cid, "status": "skipped_over_cap"}
    ]


# --------------------------------------------------------------------------
# the daily passes (NTS_098 §2, NTS_100 §6)
# --------------------------------------------------------------------------


def test_ttl_expires_only_the_three_statuses_the_spec_names(db):
    past = NOW - timedelta(days=1)
    expirable = [
        _candidate(db, status=status, expires_at=past)
        for status in ("pending", "doc_missing", "selected")
    ]
    editors = [
        _candidate(db, status=status, expires_at=past)
        for status in ("drafted", "returned", "ready")
    ]
    assert portfolio_sweep.expire_stale_candidates(brand_id_fk=db, now=NOW) == 3
    with admin_db.get_session_factory()() as session:
        assert all(session.get(Candidate, i).status == "expired" for i in expirable)
        assert all(session.get(Candidate, i).status != "expired" for i in editors)


def test_a_stuck_production_returns_to_pending_then_fails(db):
    cid = _candidate(
        db, status="in_production", selected_at=NOW - timedelta(hours=3)
    )
    first = portfolio_sweep.sweep_production_timeouts(
        brand_id_fk=db, timeout_minutes=60, max_attempts=2, now=NOW
    )
    assert first == {"released": 1, "failed": 0}
    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, cid)
        assert row.status == "pending"
        assert row.attempts == 1
        row.status = "in_production"
        row.selected_at = (NOW - timedelta(hours=3)).replace(tzinfo=None)
        session.commit()

    second = portfolio_sweep.sweep_production_timeouts(
        brand_id_fk=db, timeout_minutes=60, max_attempts=2, now=NOW
    )
    assert second == {"released": 0, "failed": 1}
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, cid).status == "failed"


def test_a_fresh_production_is_left_alone(db):
    cid = _candidate(
        db, status="in_production", selected_at=NOW - timedelta(minutes=5)
    )
    assert portfolio_sweep.sweep_production_timeouts(
        brand_id_fk=db, timeout_minutes=60, max_attempts=2, now=NOW
    ) == {"released": 0, "failed": 0}
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, cid).status == "in_production"


def test_retention_prunes_rejects_but_never_a_decided_candidate(db):
    old = NOW - timedelta(days=40)
    plain = _candidate(db, status="rejected", created_at=old)
    decided = _candidate(db, status="rejected", created_at=old)
    recent = _candidate(db, status="rejected", created_at=NOW - timedelta(days=5))
    published = _candidate(db, status="published", created_at=old)
    with admin_db.get_session_factory()() as session:
        session.add(
            ReviewDecision(
                brand_id_fk=db,
                candidate_id_fk=decided,
                action="disagree_guard",
                at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()

    counts = portfolio_sweep.prune_old_candidates(
        brand_id_fk=db, retention_days_rejected=30, now=NOW
    )
    assert counts["rejected"] == 1
    assert counts["kept_with_decisions"] == 1
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, plain) is None
        assert session.get(Candidate, decided) is not None
        assert session.get(Candidate, recent) is not None
        assert session.get(Candidate, published) is not None


def test_the_sweep_writes_a_ttl_run_row(db):
    from pipeline.admin.models import Run

    _candidate(db, status="pending", expires_at=NOW - timedelta(days=1))
    stats = portfolio_sweep.run_sweep("icon", now=NOW)
    assert stats["expired"] == 1
    with admin_db.get_session_factory()() as session:
        run = session.query(Run).order_by(Run.id.desc()).first()
        assert run.run_type == "ttl"
        assert run.status == "success"


# --------------------------------------------------------------------------
# the happy path, end to end on stubs
# --------------------------------------------------------------------------


async def test_a_production_run_drafts_four_siblings_in_one_transaction(db, wired):
    from pipeline.production import run_production

    writer, publisher, documents = wired
    cid = _candidate(db)
    stats = await run_production(brand_slug="icon", tag="e2e-test")

    # S5: the document is fetched before research, and research sees it.
    assert documents.calls == [cid]
    assert writer.documents_seen and writer.documents_seen[0] is not None

    assert stats.drafted == 1
    # One transaction, every language in it (NTS_100 §4).
    assert len(publisher.batches) == 1
    languages = [p.language.value for p in publisher.batches[0]]
    assert languages[0] == "en", "the canon must be built before its translations"
    assert set(languages) == {"en", "ru", "uk", "pl"}
    # The tag reaches the title, so the draft is identifiable in the Studio.
    assert all("[e2e-test]" in p.title for p in publisher.batches[0])

    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, cid)
        assert row.status == "drafted"
        assert row.sanity_draft_id == "drafts.en-1"
        assert row.production_batch is not None
        assert row.production_batch.startswith("icon:")
        assert session.query(FactPack).one().candidate_id_fk == cid


async def test_every_paid_row_is_charged_to_the_candidate(db, wired, monkeypatch):
    """The production path knows the candidate before it spends (NTS_121 §6)."""
    import pipeline.run as run_mod
    from pipeline.admin.cost_recorder import record_cost
    from pipeline.production import run_production

    cid = _candidate(db)

    async def _drafting_costs_money(topic, brand, language, fact_pack=None):
        record_cost(
            provider="openai", operation="draft", model="gpt-4o", cost_usd=0.01
        )
        return Draft(
            topic_id=topic.id,
            brand_id=brand.slug,
            language=language,
            title="T",
            body="B",
            key_takeaway="K",
        )

    monkeypatch.setattr(
        run_mod, "generate_draft_for_language", _drafting_costs_money
    )
    await run_production(brand_slug="icon", dry_run=True)

    with admin_db.get_session_factory()() as session:
        rows = session.query(CostRecord).all()
        assert rows, "the drafting call recorded nothing"
        assert all(r.candidate_id_fk == cid for r in rows)
