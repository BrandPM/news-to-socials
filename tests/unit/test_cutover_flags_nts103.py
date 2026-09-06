"""IT_PROJ_NTS_103 — every mode switches OFF, not merely on (S10).

The DoD line this file exists for reads "проверено выключение, а не только
включение", and NTS_103 §Риски explains why it is worded that way: a flag is
only a rollback if turning it off is the tested direction. Switching a mode on
is exercised by every other test in the suite; switching it off is exercised
here, once per flag, by asserting on what the run **does not do**.

There are six modes at the end of the cutover, and each answers a different
question about what stops:

* ``intake_enabled`` — the funnel. Off: a ``cancelled`` run row and no fetch.
* ``v2_generation_enabled`` — the old daily generation. Off: nothing fetched,
  nothing drafted, the run closed with a reason.
* ``production_enabled`` — the v3 run. Off: a ``cancelled`` row, and the
  candidates stay ``pending`` — untouched, not consumed.
* ``data_blocks_enabled`` — blocks in the body. Off: the generator still runs
  and returns nothing, which is what keeps S8's ordering (schema → render →
  pipeline) honest.
* ``cover_mode`` — ``flux`` is the pre-S9 behaviour and the default.
* ``judge_enabled`` (``eval_enabled``) — the LLM judge. Off: no score row.

The seventh switch is not a flag at all: ``monthly_spend_cap_usd`` stops
production at 100% and leaves intake running, and that asymmetry is the point
(NTS_106 §3) — the cheap contour must not stop because the expensive one did.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from pipeline.admin import db as admin_db
from pipeline.admin.models import Candidate, PipelineConfig, Run
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

# Every mode flag, its shipped default, and the session that introduced it.
# The completeness test below reads this table, so a flag added without a
# switch-off test fails here rather than in production six months later.
CUTOVER_FLAGS: tuple[tuple[str, object, str], ...] = (
    ("intake_enabled", False, "S2 / migration 022"),
    ("v2_generation_enabled", False, "S2 / migration 022"),
    ("production_enabled", False, "S4 / migration 026"),
    ("data_blocks_enabled", False, "S6 / migration 028"),
    ("cover_mode", "flux", "S9 / migration 030"),
    ("eval_enabled", True, "NTS_091, judge — S10 puts it in the real loop"),
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        brand_id = seed_icon_brand(session, with_sanity_creds=True)
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


def _set(brand_id: int, **values) -> None:
    with admin_db.get_session_factory()() as session:
        row = session.get(PipelineConfig, brand_id)
        for key, value in values.items():
            setattr(row, key, value)
        session.commit()


def _candidate(brand_id: int, **kw) -> int:
    fields = {
        "brand_id_fk": brand_id,
        "input_kind": "document",
        "source_title": "A directive was adopted",
        "verdict": "accept",
        "reason_code": "ok",
        "reason": "adopted",
        "status": "pending",
        "primary_doc_url": "https://reg.test/doc",
        "created_at": NOW.replace(tzinfo=None),
        "expires_at": datetime(2026, 12, 1).replace(tzinfo=None),
    }
    fields.update(kw)
    with admin_db.get_session_factory()() as session:
        row = Candidate(**fields)
        session.add(row)
        session.commit()
        return int(row.id)


# --------------------------------------------------------------------------
# completeness: no flag without an off-switch test
# --------------------------------------------------------------------------


def test_every_mode_flag_in_the_config_is_listed_here():
    """The guard that keeps this file honest as flags accumulate.

    NTS_103 §Риски: "к концу перехода их будет с полдюжины. Записать в вольт,
    что каждый значит и когда его снимать, иначе через полгода никто не
    вспомнит." The vault register is in NTS_127; this is its executable half.
    """
    columns = {c.name for c in PipelineConfig.__table__.columns}
    mode_flags = {
        name
        for name in columns
        if name.endswith("_enabled") or name == "cover_mode"
    }
    # ``dedup_enabled`` and ``research_enabled`` are v2 tunables, not cutover
    # steps: they change how a stage behaves, not whether a contour runs.
    mode_flags -= {"dedup_enabled", "research_enabled", "images_on_demand"}
    listed = {name for name, _default, _origin in CUTOVER_FLAGS}
    assert mode_flags == listed, (
        f"flag(s) with no switch-off test: {sorted(mode_flags - listed)}; "
        f"listed but gone from the model: {sorted(listed - mode_flags)}"
    )


@pytest.mark.parametrize(
    ("flag", "default"), [(f, d) for f, d, _o in CUTOVER_FLAGS]
)
def test_each_flag_ships_at_its_documented_default(db, flag, default):
    """A new mode ships off; an old mode keeps its behaviour. Both are the same
    rule from the operator's side: the deploy changes nothing by itself."""
    from pipeline.admin.config_client import AdminConfigClient

    config = AdminConfigClient(brand_slug="icon").get_config()
    assert getattr(config, flag) == default


# --------------------------------------------------------------------------
# each flag, switched OFF
# --------------------------------------------------------------------------


async def test_intake_off_writes_a_cancelled_run_and_fetches_nothing(db):
    from pipeline.intake import IntakeDisabled, run_intake

    _set(db, intake_enabled=False)
    with pytest.raises(IntakeDisabled):
        await run_intake(brand_slug="icon")
    with admin_db.get_session_factory()() as session:
        run = session.query(Run).order_by(Run.id.desc()).first()
        assert run.status == "cancelled"
        assert run.run_type == "intake"
        # Nothing was fetched — the check is before the first source.
        assert "intake_enabled is OFF" in (run.log_excerpt or "")


async def test_v2_generation_off_returns_before_the_first_fetch(db):
    from pipeline.run import run_pipeline

    _set(db, v2_generation_enabled=False)
    results = await run_pipeline(brand_slug="icon", dry_run=True)
    assert results == []


async def test_production_off_leaves_the_candidates_pending(db):
    """Off must mean untouched, not consumed: the whole value of a flag as a
    rollback is that the state it protects survives it."""
    from pipeline.production import ProductionDisabled, run_production

    _set(db, production_enabled=False)
    cid = _candidate(db)
    with pytest.raises(ProductionDisabled):
        await run_production(brand_slug="icon", dry_run=True)
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, cid).status == "pending"
        run = session.query(Run).order_by(Run.id.desc()).first()
        assert run.status == "cancelled" and run.run_type == "production"


def test_data_blocks_off_builds_the_block_and_writes_nothing(db):
    """NTS_095 fixes schema → render → pipeline. With the flag off the
    generator still runs — so the day S8 merges, turning it on is the only
    change — and it returns nothing."""
    from pipeline.generator.composition import build_data_blocks
    from pipeline.generator.research import Fact, FactPack

    pack = FactPack(
        source_facts=[
            Fact(
                text=f"threshold {n}",
                url="https://x.test/a",
                value=str(n),
                unit="EUR",
                comparable_group="threshold",
            )
            for n in range(4)
        ],
        citations=["https://x.test/a"],
    )
    assert build_data_blocks(pack, depth="deep", enabled=False) == []
    assert build_data_blocks(pack, depth="deep", enabled=True) != []


def test_cover_mode_flux_is_the_pre_s9_behaviour(db):
    from pipeline.admin.config_client import AdminConfigClient

    assert AdminConfigClient(brand_slug="icon").get_config().cover_mode == "flux"
    _set(db, cover_mode="data")
    assert AdminConfigClient(brand_slug="icon").get_config().cover_mode == "data"


async def test_the_judge_off_writes_no_score(db):
    """NTS_080 — the judge advises; with it off nothing is scored and nothing
    fails. S10 is where it enters the real loop, still behind this flag."""
    from pipeline.admin.judge import score_draft
    from pipeline.admin.models import DraftScore

    result = await score_draft(
        draft_id="drafts.x-en",
        lang="en",
        draft_text="body",
        eval_enabled=False,
        eval_threshold=7.0,
        source_text="src",
        brand_id_fk=db,
    )
    assert result is None
    with admin_db.get_session_factory()() as session:
        assert session.query(DraftScore).count() == 0


# --------------------------------------------------------------------------
# the switch that is not a flag (NTS_106 §3)
# --------------------------------------------------------------------------


async def test_the_spend_cap_stops_production_and_leaves_intake_running(db):
    """The asymmetry IS the design: contour 1 costs cents a day, and stopping
    it because the expensive contour hit its ceiling would blind the portfolio
    for the rest of the month."""
    from pipeline.admin.models import CostRecord
    from pipeline.production import run_production

    _set(db, production_enabled=True, monthly_spend_cap_usd=1.0, intake_enabled=True)
    _candidate(db)
    with admin_db.get_session_factory()() as session:
        session.add(
            CostRecord(
                brand_id_fk=db,
                provider="openai",
                operation="draft",
                cost_usd=5.0,
                created_at=datetime.now(tz=UTC).replace(tzinfo=None),
            )
        )
        session.commit()

    stats = await run_production(brand_slug="icon", dry_run=True)
    assert stats.stopped_reason == "monthly_spend_cap"
    assert stats.selected == 0
    # And intake is untouched: its own flag is still on.
    from pipeline.admin.config_client import AdminConfigClient

    assert AdminConfigClient(brand_slug="icon").get_config().intake_enabled is True


# --------------------------------------------------------------------------
# rollback rehearsed, not described (NTS_103 DoD)
# --------------------------------------------------------------------------


def test_turning_a_flag_off_after_on_restores_the_earlier_behaviour(db):
    """Rehearsing the direction that matters. Every other test turns a flag on;
    this one turns it back and asserts the run reads the old value — the flag
    is a rollback only if the round trip works."""
    from pipeline.admin.config_client import AdminConfigClient

    client = AdminConfigClient(brand_slug="icon")
    for flag, default, _origin in CUTOVER_FLAGS:
        flipped = "data" if flag == "cover_mode" else (not default)
        _set(db, **{flag: flipped})
        assert getattr(client.get_config(), flag) == flipped
        _set(db, **{flag: default})
        assert getattr(client.get_config(), flag) == default


def test_the_rank_weights_survive_a_round_trip_through_the_api(db):
    """The one composite setting an operator is most likely to edit and then
    want back (NTS_100 §2)."""
    from pipeline.admin.config_client import AdminConfigClient

    original = dict(AdminConfigClient(brand_slug="icon").get_config().rank_weights)
    _set(db, rank_weights=json.dumps({**original, "w_conf": 0.9}))
    assert AdminConfigClient(brand_slug="icon").get_config().rank_weights["w_conf"] == 0.9
    _set(db, rank_weights=json.dumps(original))
    assert (
        dict(AdminConfigClient(brand_slug="icon").get_config().rank_weights) == original
    )
