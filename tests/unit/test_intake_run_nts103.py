"""The intake run end to end (NTS_098 §5, NTS_099 DoD 6, NTS_103 шаг 1).

What this file is for, in order of how badly each failure would hurt:

1. **The run spends nothing on generation.** The whole justification for the
   shadow week is that contour 1 is free enough to run alongside v2 for a week.
   A stray import that pulls a draft call into the intake would turn a $0.05/day
   run into a $10/day one and nobody would notice until the invoice.
2. **`v2_generation_enabled` off actually stops the money** — not just the
   Sanity write. It is a spend switch.
3. **NTS_099 DoD 6** — every reject carries a `reason_code` and a `reason`, and
   the daily cap stores `cap_overflow=1` instead of discarding the item.
4. **NTS_098 DoD 4** — the candidate survives the feed item vanishing, which it
   does routinely within hours while a candidate lives for weeks.
5. **The heartbeat carries absolute numbers** (NTS_106 §2): "the rubric is
   strict" and "the parser died" produce the same empty portfolio and only the
   counts tell them apart.

The guard call and the embedding are stubbed. Everything else — config, dedup,
prefilter, the DB writes, the run row — is real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from pipeline.admin import db as admin_db
from pipeline.admin.models import (
    BrandTaxonomy,
    Candidate,
    PipelineConfig,
    Prompt,
    Run,
    Source,
    SourceHealthRecord,
)
from pipeline.common import config as config_module
from pipeline.common.models import RawItem
from pipeline.intake import (
    IntakeDisabled,
    IntakeStats,
    input_kind_for,
    run_intake,
)
from pipeline.monitoring.alerts import format_intake_heartbeat
from pipeline.selector.editorial_guard import _GUARD_PROMPT, GuardDeferred
from tests.unit.conftest import seed_icon_brand

# Relative to the real clock on purpose. The prefilter drops items older than
# ``prefilter_max_age_hours_news`` (72h) measured against ``datetime.now``, so a
# frozen literal here turns every intake test into a time bomb: it passes for
# three days, then fails for reasons that have nothing to do with intake.
NOW = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)

_TAXONOMY = (
    ("structuring", "Structuring & Tax", "Residence, CRS/DAC/CARF, UBO", "/t"),
    ("wealth", "Wealth Management", "Private banking, trustee regulation", "/w"),
)


@pytest.fixture
def brand(tmp_path, monkeypatch):
    """A brand with a config row, a rubric, a taxonomy and two feeds."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
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
                intake_enabled=True,
                v2_generation_enabled=False,
                portfolio_daily_cap_document=2,
                portfolio_daily_cap_news=1,
            )
        )
        for key, label, description, path in _TAXONOMY:
            session.add(
                BrandTaxonomy(
                    brand_id_fk=brand_id,
                    key=key,
                    label=label,
                    description_for_guard=description,
                    service_url_path=path,
                )
            )
        session.add(
            Prompt(
                brand_id_fk=brand_id,
                prompt_type="editorial_guard",
                version_name="v1",
                content=_GUARD_PROMPT,
                is_active=True,
                created_by="test",
            )
        )
        session.add(
            Source(
                brand_id_fk=brand_id,
                name="ESMA News",
                source_type="rss",
                url="https://esma.test/rss",
                primary_category="structuring",
                active=True,
                source_role="primary_feed",
                source_class="regulator",
                license_class="public_official",
                doc_language="en",
                fetch_method="rss",
            )
        )
        session.add(
            Source(
                brand_id_fk=brand_id,
                name="Wire News",
                source_type="rss",
                url="https://wire.test/rss",
                primary_category="wealth",
                active=True,
                source_role="news",
                source_class="news",
                license_class="news_paywalled",
                doc_language="en",
                fetch_method="rss",
            )
        )
        session.commit()
    yield brand_id
    admin_db.reset_for_tests()


# Distinct stories, not "story 1/2/3": the in-run L1 check normalises a title
# to its token set, so numbered variants of one sentence really are duplicates
# and a fixture built that way would test the dedup instead of the funnel.
_TITLES = (
    "Council adopts DAC8 reporting rules for crypto asset providers",
    "FINMA revises the circular on trustee onboarding requirements",
    "HMRC publishes transitional rules for former non-domiciled residents",
    "Cyprus tax department clarifies the sixty-day residence test",
    "FATF removes two jurisdictions from its increased-monitoring list",
    "Malta closes the residence-by-investment route to new applicants",
)


def _item(n: int, *, title: str | None = None, hours_old: int = 2) -> RawItem:
    return RawItem(
        source_id="1",
        source_name="feed",
        url=f"https://x.test/{n}",
        title=title or _TITLES[(n - 1) % len(_TITLES)],
        summary=(
            f"The measure was adopted on 12 August 2026 (ref {n}), with the "
            "first reporting period starting in January 2027 and thresholds "
            "revised for holding structures."
        ),
        published_at=NOW - timedelta(hours=hours_old),
    )


def _verdict(**overrides):
    payload = {
        "verdict": "accept",
        "reason_code": "ok",
        "reason": "Adopted instrument with a 2027 reporting obligation.",
        "service_category": "structuring",
        "jurisdictions": ["EU"],
        "event_stage": "adopted",
        "depth_prior": "article",
        "primary_doc_hint": "Council directive",
        "doc_language_expected": "en",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


class _Harness:
    """Stubs the two paid calls and records what the guard was asked."""

    def __init__(self, *, verdicts=None) -> None:
        self.prompts: list[str] = []
        self.verdicts = verdicts
        self.calls = 0
        self._axes: dict[str, int] = {}

    async def embed(self, text: str, *, model: str = "text-embedding-3-small"):
        """One orthogonal basis vector per distinct text.

        Distinct texts get cosine 0.0 and identical texts get 1.0, so the dedup
        decisions in these tests are unambiguous. A hash-to-angle scheme drifts
        two unrelated items to 0.97 now and then, which reads as a broken funnel
        rather than as a fixture artefact.
        """
        axis = self._axes.setdefault(text, len(self._axes))
        v = np.zeros(32, dtype=np.float32)
        v[axis % 32] = 1.0
        return v

    async def guard(self, prompt, *, model):
        self.prompts.append(prompt)
        self.calls += 1
        if self.verdicts is None:
            return _verdict(), 100, 20
        outcome = self.verdicts[min(self.calls - 1, len(self.verdicts) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, 100, 20


def _install(monkeypatch, harness: _Harness, feeds: dict[str, list[RawItem]]):
    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", harness.guard
    )

    async def fake_fetch(source_record, limit):
        return feeds.get(source_record.name, [])[:limit]

    monkeypatch.setattr("pipeline.intake._fetch_items", fake_fetch)
    return harness


# --- 1. the defining property: no generation spend ------------------------


def test_the_intake_module_does_not_reach_the_generator() -> None:
    """NTS_103 шаг 1. Checked on the import graph rather than on a mock, because
    the failure is somebody adding a convenient import six months from now —
    and a mock only proves the call was not made along the path the test walks.

    In a subprocess, because the check needs a clean ``sys.modules`` and
    evicting modules from the shared one breaks whatever else in the suite is
    holding a reference to them.
    """
    import subprocess
    import sys

    probe = (
        "import sys, pipeline.intake; "
        "leaked = sorted(m for m in sys.modules "
        "if m.startswith('pipeline.generator')); "
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", (
        f"intake pulled in the generator: {result.stdout.strip()}"
    )


async def test_a_full_intake_records_only_guard_and_embedding_costs(
    brand, monkeypatch
) -> None:
    """Every paid call writes ``cost_records`` (NTS_025 C1), so the row set IS
    the audit: two operations, both cheap, and nothing named draft/polish/
    translate/image/research."""
    harness = _install(
        monkeypatch,
        _Harness(),
        {"ESMA News": [_item(1)], "Wire News": [_item(2)]},
    )
    # The embedding stub bypasses embed_text, so record a cost row the way the
    # real one does — otherwise this test would pass by making no calls at all.
    recorded: list[dict] = []
    monkeypatch.setattr(
        "pipeline.admin.cost_recorder.record_cost",
        lambda **kw: recorded.append(kw),
    )

    await run_intake(brand_slug="icon", embed=harness.embed)

    operations = sorted({r["operation"] for r in recorded})
    assert operations == ["guard:document", "guard:news"]
    assert all(r["cost_usd"] < 0.01 for r in recorded)


# --- 2. the v2 kill switch is a SPEND switch ------------------------------


async def test_v2_generation_off_returns_before_fetching_anything(
    brand, monkeypatch
) -> None:
    """The gate sits above the source loop, so "off" costs nothing at all
    rather than costing a fetch and an embedding."""
    from pipeline.run import run_pipeline

    calls = {"n": 0}

    async def exploding_fetch(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("v2 generation must not fetch while the flag is off")

    monkeypatch.setattr("pipeline.run._build_topics_for_source", exploding_fetch)

    results = await run_pipeline(brand_slug="icon", dry_run=True)
    assert results == []
    assert calls["n"] == 0


async def test_v2_generation_off_finishes_an_operator_run_as_cancelled(
    brand, monkeypatch
) -> None:
    """A row stuck at 'running' forever is how a disabled flag becomes a
    support question. The reason goes in ``log_excerpt``, where the operator
    reads it."""
    from pipeline.run import run_pipeline

    with admin_db.get_session_factory()() as session:
        run = Run(
            brand_id_fk=brand,
            triggered_by="manual",
            source_ids="[]",
            started_at=NOW,
            status="running",
            run_type="production",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    await run_pipeline(brand_slug="icon", existing_run_id=run_id, dry_run=True)

    with admin_db.get_session_factory()() as session:
        row = session.get(Run, run_id)
        assert row.status == "cancelled"
        assert "v2_generation_enabled is OFF" in row.log_excerpt


async def test_intake_refuses_to_run_while_its_own_flag_is_off(
    brand, monkeypatch
) -> None:
    """A new mode ships off (master-prompt rule). ``force`` exists so the flag
    can be exercised before it is switched on — refusing a hand-triggered run
    would make the flag untestable."""
    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, brand).intake_enabled = False
        session.commit()

    harness = _install(monkeypatch, _Harness(), {"ESMA News": [_item(1)]})
    with pytest.raises(IntakeDisabled):
        await run_intake(brand_slug="icon", embed=harness.embed)
    assert harness.calls == 0

    stats = await run_intake(brand_slug="icon", force=True, embed=harness.embed)
    assert stats.total("accepted") == 1


async def test_the_disabled_flag_closes_the_run_as_cancelled_not_a_traceback(
    brand, monkeypatch
) -> None:
    """Shadow-week finding 3 (log 14:03:41). With ``intake_enabled=false`` the
    unit died with a traceback and exit 1, and systemd recorded ``Failed``.

    A flag that is off is the state NTS_103 шаг 1 ships in — the *expected*
    outcome for every day before Andriy switches the shadow week on — so it
    has to look like the v2 gate already does: a terminal ``cancelled`` row
    tagged ``run_type='intake'``, the reason where the operator reads it, and
    nothing in the failure channel. A daily red unit teaches the operator to
    ignore the failure channel, which is the one thing monitoring cannot
    survive (NTS_106).
    """
    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, brand).intake_enabled = False
        session.commit()

    harness = _install(monkeypatch, _Harness(), {"ESMA News": [_item(1)]})
    with pytest.raises(IntakeDisabled):
        await run_intake(brand_slug="icon", embed=harness.embed)
    assert harness.calls == 0

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        run = session.scalars(select(Run).order_by(Run.id.desc())).first()
    assert run is not None
    assert run.status == "cancelled"
    assert run.run_type == "intake"
    assert run.finished_at is not None
    assert "intake_enabled" in (run.log_excerpt or "")
    payload = json.loads(run.stats)
    assert payload["run_type"] == "intake"
    assert payload["cancelled_reason"] == "intake_enabled is off"
    assert payload["funnel"]["fetched"] == 0


def test_the_cli_exits_zero_when_the_flag_is_off(brand, monkeypatch) -> None:
    """The other half of finding 3: ``run_intake`` still refuses in the type
    system — a caller must not read "nothing ran" as "nothing matched" — but
    the CLI, which is what systemd runs, turns that refusal into exit 0 and a
    sentence. The exception is the API; the exit code is the operator's."""
    from typer.testing import CliRunner

    from pipeline.intake import app

    with admin_db.get_session_factory()() as session:
        session.get(PipelineConfig, brand).intake_enabled = False
        session.commit()

    async def exploding_fetch(*args, **kwargs):
        raise AssertionError("a disabled intake must not fetch")

    monkeypatch.setattr("pipeline.intake._fetch_items", exploding_fetch)

    result = CliRunner().invoke(app, ["--brand-slug", "icon"])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "intake_enabled" in result.output


# --- the shadow-week findings, at the funnel level ------------------------


async def test_a_document_item_judged_no_document_is_a_guard_error(
    brand, monkeypatch
) -> None:
    """Finding 1 end to end: 21 primary-feed items were filed as rejects that
    the rubric had no right to make. After the fix the same response is a
    ``guard_error`` — no candidate row, counted in the summary — so it shows up
    as a guard problem to fix rather than as an editorial decision to trust."""
    harness = _install(
        monkeypatch,
        _Harness(
            verdicts=[
                _verdict(
                    verdict="reject",
                    reason_code="no_document",
                    reason="No marker that a document exists.",
                )
            ]
        ),
        {"ESMA News": [_item(1)]},
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.by_kind["document"]["guard_errors"] == 1
    assert stats.total("rejected") == 0
    assert stats.reason_codes == {"guard_error": 1}

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        assert session.scalars(select(Candidate)).all() == []


async def test_a_bare_primary_feed_annotation_reaches_the_guard(
    brand, monkeypatch
) -> None:
    """Finding 2 end to end: BaFin-shaped items (13-60 char annotations) are
    judged, and the identically-short news item is still dropped free."""
    short = "BaFin: Hinweis"
    assert len(short) == 14

    bafin = RawItem(
        source_id="1",
        source_name="feed",
        url="https://bafin.test/1",
        title="BaFin publishes consultation on outsourcing requirements",
        summary=short,
        published_at=NOW - timedelta(hours=2),
    )
    wire = RawItem(
        source_id="2",
        source_name="feed",
        url="https://wire.test/1",
        title="FINMA revises the circular on trustee onboarding requirements",
        summary=short,
        published_at=NOW - timedelta(hours=2),
    )
    harness = _install(
        monkeypatch, _Harness(), {"ESMA News": [bafin], "Wire News": [wire]}
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.by_kind["document"]["after_prefilter"] == 1
    assert stats.by_kind["document"]["guarded"] == 1
    assert stats.by_kind["news"].get("after_prefilter", 0) == 0
    assert stats.prefilter_drops == {"summary_too_short": 1}
    assert harness.calls == 1


# --- the funnel ----------------------------------------------------------


async def test_the_funnel_counts_by_input_kind_and_writes_the_run_row(
    brand, monkeypatch
) -> None:
    """NTS_106 §2/§5: absolute numbers, per input_kind, on a run tagged
    ``run_type='intake'`` so the Monitoring screen can tell the contours
    apart."""
    harness = _install(
        monkeypatch,
        _Harness(),
        {
            "ESMA News": [_item(1), _item(2)],
            "Wire News": [_item(3)],
        },
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.by_kind["document"]["fetched"] == 2
    assert stats.by_kind["news"]["fetched"] == 1
    assert stats.total("guarded") == 3
    # news cap is 1, document cap is 2 — so all three are accepted.
    assert stats.total("accepted") == 3

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        run = session.scalars(select(Run).order_by(Run.id.desc())).first()
    assert run.run_type == "intake"
    assert run.status == "success"
    payload = json.loads(run.stats)
    assert payload["run_type"] == "intake"
    assert payload["funnel"]["fetched"] == 3
    assert payload["by_input_kind"]["document"]["accepted"] == 2


async def test_the_prefilter_runs_inside_the_intake_and_is_counted(
    brand, monkeypatch
) -> None:
    """DoD 1 asks for ``prefilter_drop_rate`` in the run summary — measured on
    the real run,
    not just on the pure function."""
    harness = _install(
        monkeypatch,
        _Harness(),
        {
            "Wire News": [
                _item(1),
                _item(2, title="Bank appoints new chief executive"),
                _item(3, title="Analysts expect rates to fall"),
            ]
        },
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.total("after_dedup") == 3
    assert stats.total("after_prefilter") == 1
    assert stats.prefilter_drops == {"deny_title": 2}
    assert stats.prefilter_drop_rate == pytest.approx(2 / 3)
    # And the guard was only asked about the survivor — the prefilter's job is
    # to not spend money.
    assert harness.calls == 1


async def test_a_deny_pattern_does_not_drop_the_same_title_from_a_primary_feed(
    brand, monkeypatch
) -> None:
    harness = _install(
        monkeypatch,
        _Harness(),
        {"ESMA News": [_item(1, title="ESMA appoints new board of supervisors")]},
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)
    assert stats.prefilter_drops == {}
    assert stats.total("guarded") == 1


async def test_two_feeds_carrying_one_story_buy_only_one_guard_call(
    brand, monkeypatch
) -> None:
    """The in-run L1 title check. Without it each feed pays for the same wire
    story, which at 28 news feeds is most of the guard bill."""
    same_title = "Council adopts DAC8 reporting rules for crypto providers"
    harness = _install(
        monkeypatch,
        _Harness(),
        {
            "ESMA News": [_item(1, title=same_title)],
            "Wire News": [_item(2, title=same_title)],
        },
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)
    assert harness.calls == 1
    assert stats.dedup_windows.get("run_title") == 1


# --- 3. NTS_099 DoD 6: rejects, reason codes, the daily cap ---------------


async def test_a_reject_is_stored_with_its_code_and_sentence(
    brand, monkeypatch
) -> None:
    """NTS_099 DoD 6: a reject carries both ``reason_code`` and ``reason``.
    Stored, not counted and dropped: the
    reject distribution is the only evidence the rubric is right, and it is
    what the 50-verdict review reads."""
    harness = _install(
        monkeypatch,
        _Harness(
            verdicts=[
                _verdict(
                    verdict="reject",
                    reason_code="personnel",
                    reason="Appointment with no policy change for clients.",
                    service_category=None,
                )
            ]
        ),
        {"Wire News": [_item(1)]},
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.total("rejected") == 1
    assert stats.reason_codes == {"personnel": 1}
    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        row = session.scalars(select(Candidate)).one()
    assert row.status == "rejected"
    assert row.verdict == "reject"
    assert row.reason_code == "personnel"
    assert row.reason == "Appointment with no policy change for clients."
    assert row.cap_overflow is False


async def test_over_cap_accepts_are_stored_as_promotable_daily_cap_rejects(
    brand, monkeypatch
) -> None:
    """NTS_099 §5 keeps them with ``cap_overflow=1`` so a manager can promote
    one by hand the same day. Discarding them would hide the days when
    the cap, not the rubric, is what is limiting the portfolio."""
    harness = _install(
        monkeypatch,
        _Harness(),
        {"Wire News": [_item(1), _item(2), _item(3)]},  # news cap is 1
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.total("accepted") == 1
    assert stats.cap_overflow == 2
    assert stats.reason_codes["daily_cap"] == 2

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        rows = session.scalars(select(Candidate)).all()
    overflow = [r for r in rows if r.cap_overflow]
    assert len(overflow) == 2
    for row in overflow:
        assert row.verdict == "reject"
        assert row.reason_code == "daily_cap"
        assert row.status == "rejected"
        # The guard's own sentence survives inside the cap reason — otherwise a
        # promoted candidate arrives with no editorial justification at all.
        assert "Adopted instrument" in row.reason
        # The metadata the manager needs in order to promote it is kept.
        assert row.service_category == "structuring"
        assert row.event_stage == "adopted"


async def test_the_cap_is_per_input_kind(brand, monkeypatch) -> None:
    """document 2 / news 1 (NTS_115 artefact 3 item 10). A shared counter would
    let a busy news day eat the document allowance, which is backwards: the
    document feeds are the ones v3 exists to read."""
    harness = _install(
        monkeypatch,
        _Harness(),
        {
            "ESMA News": [_item(1), _item(2), _item(3)],
            "Wire News": [_item(4), _item(5)],
        },
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)
    assert stats.by_kind["document"]["accepted"] == 2
    assert stats.by_kind["news"]["accepted"] == 1
    assert stats.cap_overflow == 2


async def test_a_schema_violation_creates_no_candidate_row(brand, monkeypatch) -> None:
    """NTS_099 §3, the load-bearing half of DoD 2: ``guard_error`` means no
    row. Not an accept (money on garbage), not a reject (a real story thrown
    away with a verdict nobody made)."""
    harness = _install(
        monkeypatch,
        _Harness(verdicts=[_verdict(event_stage="whenever")]),
        {"Wire News": [_item(1)]},
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        assert session.scalars(select(Candidate)).all() == []
    assert stats.total("guard_errors") == 1
    assert stats.reason_codes == {"guard_error": 1}
    assert stats.guard_error_rate == 1.0


async def test_a_deferred_item_creates_no_row_and_is_counted_separately(
    brand, monkeypatch
) -> None:
    """NTS_106 §1. A transport failure is not an editorial statement, and the
    two must be countable apart: 20% guard_errors is a rubric problem, 20%
    deferred is OpenAI having a bad afternoon."""
    harness = _install(
        monkeypatch,
        _Harness(verdicts=[GuardDeferred("unreachable")]),
        {"Wire News": [_item(1)]},
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        assert session.scalars(select(Candidate)).all() == []
    assert stats.total("deferred") == 1
    assert stats.total("guard_errors") == 0
    assert stats.guard_error_rate == 0.0


async def test_an_embedding_failure_skips_the_item_without_guarding_it(
    brand, monkeypatch
) -> None:
    """Fail-open would guard without dedup and risk a duplicate article; fail
    silently would drop the item with no trace. Counted and skipped: the next
    intake sees it again."""

    async def broken_embed(text, *, model="text-embedding-3-small"):
        raise RuntimeError("embeddings endpoint down")

    harness = _install(monkeypatch, _Harness(), {"Wire News": [_item(1)]})
    stats = await run_intake(brand_slug="icon", embed=broken_embed)
    assert stats.embed_failures == 1
    assert harness.calls == 0


# --- 4. NTS_098 DoD 4: the candidate outlives the feed item ---------------


async def test_a_candidate_survives_the_feed_item_disappearing(
    brand, monkeypatch
) -> None:
    """NTS_098 DoD 4. An RSS item falls off the end of a feed within hours; a
    candidate lives for up to three weeks. The snapshot is why the portfolio
    does not rot — and re-running the intake against an empty feed must not
    touch, expire or duplicate what is already there.
    """
    harness = _install(monkeypatch, _Harness(), {"Wire News": [_item(1)]})
    await run_intake(brand_slug="icon", embed=harness.embed)

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        before = session.scalars(select(Candidate)).one()
        snapshot = (
            before.id,
            before.source_title,
            before.source_summary,
            before.source_url,
            before.source_published_at,
            before.status,
            before.reason,
        )

    # The feed is now empty — the item is gone from the source entirely.
    _install(monkeypatch, _Harness(), {"Wire News": []})
    stats = await run_intake(brand_slug="icon", embed=harness.embed)
    assert stats.total("fetched") == 0

    with admin_db.get_session_factory()() as session:
        rows = session.scalars(select(Candidate)).all()
    assert len(rows) == 1
    after = rows[0]
    assert (
        after.id,
        after.source_title,
        after.source_summary,
        after.source_url,
        after.source_published_at,
        after.status,
        after.reason,
    ) == snapshot
    # Everything needed to work the candidate is on the row itself.
    assert after.source_title and after.source_summary and after.source_url


async def test_an_empty_feed_is_recorded_as_a_failed_fetch(brand, monkeypatch) -> None:
    """NTS_106 §1 counts "0 элементов" alongside a timeout. taxathand.com
    answers HTTP 200 with an error document, which parses to zero entries and
    would otherwise read as healthy forever."""
    _install(monkeypatch, _Harness(), {"Wire News": []})
    await run_intake(brand_slug="icon", embed=_Harness().embed)

    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        rows = session.scalars(select(SourceHealthRecord)).all()
    assert rows, "an intake must always leave a health record per source"
    assert all(r.success is False for r in rows)
    assert all("0 items" in (r.error_msg or "") for r in rows)


async def test_a_source_with_no_fetcher_fails_that_source_only(
    brand, monkeypatch
) -> None:
    """S5 lands ``html_list``/``edgar_fts``. Until then the run records a health
    failure and keeps going: one unimplemented fetcher must not cost the day's
    whole funnel."""
    with admin_db.get_session_factory()() as session:
        from sqlalchemy import select

        source = session.scalars(
            select(Source).where(Source.name == "ESMA News")
        ).one()
        source.fetch_method = "html_list"
        session.commit()

    harness = _Harness()
    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", harness.guard
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)

    assert stats.source_errors == 1
    from sqlalchemy import select

    with admin_db.get_session_factory()() as session:
        health = session.scalars(select(SourceHealthRecord)).all()
        run = session.scalars(select(Run).order_by(Run.id.desc())).first()
    assert any("html_list" in (r.error_msg or "") for r in health)
    # A run with a dead source is not a clean success.
    assert run.status == "failed"


# --- input kind mapping --------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("primary_feed", "document"),
        ("primary_site", "document"),
        ("news", "news"),
        ("", "news"),
    ],
)
def test_the_source_role_decides_the_input_kind(role: str, expected: str) -> None:
    """NTS_099 §2. Getting this backwards routes regulator documents through
    the news rules — the deny-list and the 72 h age limit — and drops them."""
    assert input_kind_for(role) == expected


async def test_the_rubric_sees_the_right_input_kind_per_source(
    brand, monkeypatch
) -> None:
    harness = _install(
        monkeypatch,
        _Harness(),
        {"ESMA News": [_item(1)], "Wire News": [_item(2)]},
    )
    await run_intake(brand_slug="icon", embed=harness.embed)
    kinds = [
        "document" if "input_kind: document" in p else "news"
        for p in harness.prompts
    ]
    assert kinds == ["document", "news"]
    # And the per-brand blocks reached the prompt from the tables.
    assert "structuring: Structuring & Tax" in harness.prompts[0]
    assert "tier1:" in harness.prompts[0]


# --- 5. the heartbeat ----------------------------------------------------


async def test_the_heartbeat_reports_absolute_numbers_per_input_kind(
    brand, monkeypatch
) -> None:
    """NTS_106 §2. The rendered message is what Andriy reads at 09:00; a rate
    alone cannot distinguish a strict rubric from a dead parser."""
    harness = _install(
        monkeypatch,
        _Harness(),
        {
            "ESMA News": [_item(1), _item(2, title="Bank hires three partners")],
            "Wire News": [_item(3)],
        },
    )
    stats = await run_intake(brand_slug="icon", embed=harness.embed)
    message = format_intake_heartbeat(
        run_id=7, finished_at=NOW, stats=stats.as_dict()
    )

    assert "fetched 3" in message
    assert "· document:" in message and "· news:" in message
    assert "prefilter_drop_rate:" in message
    for stage in ("after_dedup", "after_prefilter", "guarded", "accepted"):
        assert stats.as_dict()["funnel"][stage] is not None
    assert "accepted" in message


def test_the_heartbeat_survives_a_stats_payload_with_nothing_in_it() -> None:
    """The heartbeat is a dead-man switch: its absence is itself the alert
    (NTS_106 §2), so the one thing it must never do is fail to render."""
    message = format_intake_heartbeat(run_id=None, finished_at=NOW, stats={})
    assert "Интейк" in message
    assert "fetched 0" in message


def test_an_alarming_drop_rate_is_flagged_in_the_message() -> None:
    quiet = format_intake_heartbeat(
        run_id=1,
        finished_at=NOW,
        stats={
            "funnel": {"fetched": 100, "after_dedup": 100, "after_prefilter": 95},
            "prefilter_drop_rate": 0.05,
        },
    )
    assert "🟡" in quiet
    healthy = format_intake_heartbeat(
        run_id=1,
        finished_at=NOW,
        stats={
            "funnel": {"fetched": 100, "after_dedup": 100, "after_prefilter": 40},
            "prefilter_drop_rate": 0.60,
        },
    )
    assert "🟡" not in healthy


def test_a_high_guard_error_rate_is_flagged() -> None:
    """NTS_106 §1 alerts above 20% for a run."""
    message = format_intake_heartbeat(
        run_id=1,
        finished_at=NOW,
        stats={
            "funnel": {"fetched": 50, "after_dedup": 50, "after_prefilter": 30},
            "prefilter_drop_rate": 0.4,
            "guard_error_rate": 0.35,
        },
    )
    assert "guard_error_rate: 0.35 🟡" in message


def test_an_intake_run_gets_the_funnel_pulse_not_the_generation_pulse(
    brand, monkeypatch
) -> None:
    """An intake run through ``format_run_finished`` would say "релевантных
    0/340, черновиков 0" — true of every intake run and informative about
    none."""
    from pipeline.monitoring.alerts import _gather_run_events

    with admin_db.get_session_factory()() as session:
        session.add(
            Run(
                brand_id_fk=brand,
                triggered_by="cron",
                source_ids="[1]",
                started_at=NOW,
                finished_at=datetime.now(tz=UTC),
                status="success",
                run_type="intake",
                stats=json.dumps(
                    IntakeStats(
                        by_kind={
                            "document": {
                                "fetched": 12,
                                "after_dedup": 11,
                                "after_prefilter": 6,
                                "guarded": 6,
                                "accepted": 2,
                                "rejected": 4,
                                "guard_errors": 0,
                                "deferred": 0,
                            }
                        }
                    ).as_dict()
                ),
            )
        )
        session.commit()

    events = dict(_gather_run_events(set()))
    heartbeats = [v for k, v in events.items() if k.startswith("intake_heartbeat:")]
    assert len(heartbeats) == 1
    assert "Интейк" in heartbeats[0]
    assert not any(k.startswith("run_finished:") for k in events)
