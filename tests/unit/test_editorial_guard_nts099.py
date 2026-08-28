"""IT_PROJ_NTS_099 DoD 2-5 — the guard's contract, its placeholders, its seed.

Four DoD items, and each one is a specific silent failure this file exists to
make loud:

* **DoD 2** — both ``input_kind`` values are judged, the output is validated,
  and a violation is a ``guard_error`` with **no candidate row**. The failure
  it prevents: a malformed response coerced into a verdict. An accept spends
  money on garbage; a reject throws away a real story. Neither leaves a trace.
* **DoD 3** — ``{services}`` and ``{jurisdiction_tiers}`` render per brand.
  The failure it prevents: Icon's five services quietly hardcoded in the
  module, so onboarding brand two (NTS_109) silently judges its feed against
  Icon's rubric.
* **DoD 4** — the ``prompts`` migration verified up **and down** on a real
  database file. 021 widened the CHECK, 023 seeds the row.
* **DoD 5** — the sentinel: an edit saved through the API is what the next run
  renders. The failure it prevents is the one NTS_071 §2 is entirely about — a
  broken placeholder set silently reverting to the code constant while the
  operator believes their edit is live.
"""

from __future__ import annotations

import json
import os
import sqlite3
import string
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.models import BrandTaxonomy, PipelineConfig
from pipeline.common import config as config_module
from pipeline.selector.editorial_guard import (
    _GUARD_PROMPT,
    GUARD_REQUIRED_PLACEHOLDERS,
    GuardDeferred,
    GuardSchemaError,
    guard_json_schema,
    guard_template_placeholders,
    judge_item,
    load_brand_taxonomy,
    parse_guard_response,
    render_guard_prompt,
    render_jurisdiction_tiers,
    render_services,
    resolve_guard_template,
)
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-nts099"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}

_ICON_TAXONOMY = (
    ("family", "Family Office", "Family structures: foundations, trusts", "/f"),
    ("ma", "M&A Consulting", "Mid-market deals: SPA, earn-out", "/m"),
    ("special", "Special Solutions", "Sanctions, residence programmes", "/s"),
    ("structuring", "Structuring & Tax", "Residence, CRS/DAC/CARF, UBO", "/t"),
    ("wealth", "Wealth Management", "Private banking, trustee regulation", "/w"),
)
_ALLOWED = tuple(row[0] for row in _ICON_TAXONOMY)


def _accept_payload(**overrides):
    payload = {
        "verdict": "accept",
        "reason_code": "ok",
        "reason": "Council adopted DAC8; first reporting period Jan 2027.",
        "service_category": "structuring",
        "jurisdictions": ["EU", "pl"],
        "event_stage": "adopted",
        "depth_prior": "deep",
        "primary_doc_hint": "Council directive, EUR-Lex, DAC8",
        "doc_language_expected": "EN",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


# --- DoD 2: the output contract -------------------------------------------


def test_a_valid_accept_parses_and_normalises() -> None:
    verdict = parse_guard_response(
        _accept_payload(), input_kind="news", allowed_service_keys=_ALLOWED
    )
    assert verdict.accepted is True
    assert verdict.jurisdictions == ("EU", "PL")  # upper-cased
    assert verdict.doc_language_expected == "en"  # lower-cased
    assert verdict.depth_prior == "deep"


@pytest.mark.parametrize("field", sorted(guard_json_schema()["required"]))
def test_every_required_field_missing_is_a_schema_error(field: str) -> None:
    """A response missing a required field is a guard_error (NTS_099 §3), each field
    parametrised so a new required field cannot be added without a check."""
    payload = _accept_payload()
    payload.pop(field)
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            payload, input_kind="news", allowed_service_keys=_ALLOWED
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verdict", "maybe"),
        ("reason_code", "vibes"),
        ("event_stage", "pending"),
        ("depth_prior", "epic"),
        # daily_cap and guard_error are OURS to assign — a model returning
        # either is a model reporting something it cannot know.
        ("reason_code", "daily_cap"),
        ("reason_code", "guard_error"),
    ],
)
def test_an_unknown_enum_value_is_a_schema_error(field: str, value: str) -> None:
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(**{field: value}),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )


def test_a_blank_reason_is_refused_even_on_a_reject() -> None:
    """NTS_099 §3 makes ``reason`` required on rejects too — it is the sentence
    Andriy reads when proofreading 50 verdicts, and a blank one makes that
    review impossible."""
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(
                verdict="reject", reason_code="personnel", reason="   "
            ),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )


def test_reason_is_truncated_to_200_chars_not_rejected() -> None:
    """A long reason is a formatting miss, not a contract violation: the column
    is 200 chars and dropping a real verdict over prose length would be worse
    than trimming it."""
    verdict = parse_guard_response(
        _accept_payload(reason="x" * 500),
        input_kind="news",
        allowed_service_keys=_ALLOWED,
    )
    assert len(verdict.reason) == 200


def test_verdict_and_reason_code_must_agree() -> None:
    """The portfolio board reads the code and the editor reads the verdict; a
    row where they disagree tells two different people two different things."""
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(reason_code="personnel"),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(verdict="reject", reason_code="ok"),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )


def test_an_empty_jurisdiction_list_is_refused() -> None:
    """NTS_099 §3 requires ≥ 1 — the jurisdiction axis is what the tier rule
    and the ranking are computed from."""
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(jurisdictions=[]),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )


def test_an_accept_must_name_a_service_from_this_brands_taxonomy() -> None:
    """An accept with no service, or one this brand does not sell, cannot be
    ranked (NTS_100) or internally linked (NTS_093)."""
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(service_category=None),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )
    with pytest.raises(GuardSchemaError):
        parse_guard_response(
            _accept_payload(service_category="crypto_desk"),
            input_kind="news",
            allowed_service_keys=_ALLOWED,
        )


def test_a_reject_may_carry_no_service_and_a_wrong_one_is_dropped() -> None:
    reject = parse_guard_response(
        _accept_payload(
            verdict="reject",
            reason_code="personnel",
            service_category=None,
        ),
        input_kind="news",
        allowed_service_keys=_ALLOWED,
    )
    assert reject.service_category is None

    # A wrong service on a reject is dropped rather than stored, so the
    # reject-distribution report stays honest.
    reject2 = parse_guard_response(
        _accept_payload(
            verdict="reject", reason_code="forecast", service_category="nonsense"
        ),
        input_kind="news",
        allowed_service_keys=_ALLOWED,
    )
    assert reject2.service_category is None


def test_confidence_must_be_a_number_in_range() -> None:
    for bad in ("high", None, 1.4, -0.1):
        with pytest.raises(GuardSchemaError):
            parse_guard_response(
                _accept_payload(confidence=bad),
                input_kind="news",
                allowed_service_keys=_ALLOWED,
            )


def test_primary_doc_hint_is_nulled_for_document_input() -> None:
    """NTS_099 §3: null for ``document`` — the document IS the item, so a hint
    is a search instruction for something already in hand."""
    verdict = parse_guard_response(
        _accept_payload(primary_doc_hint="Find the FINMA circular"),
        input_kind="document",
        allowed_service_keys=_ALLOWED,
    )
    assert verdict.primary_doc_hint is None
    news = parse_guard_response(
        _accept_payload(), input_kind="news", allowed_service_keys=_ALLOWED
    )
    assert news.primary_doc_hint == "Council directive, EUR-Lex, DAC8"


def test_a_non_object_response_is_a_schema_error() -> None:
    for payload in ([], "accept", None, 7):
        with pytest.raises(GuardSchemaError):
            parse_guard_response(
                payload, input_kind="news", allowed_service_keys=_ALLOWED
            )


# --- DoD 2: both input kinds, retries, and the deferred/error split -------


async def test_both_input_kinds_are_judged_and_reach_the_prompt(monkeypatch) -> None:
    seen: list[str] = []

    async def fake_call(prompt, *, model):
        seen.append(prompt)
        return _accept_payload(), 100, 20

    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", fake_call
    )
    for kind in ("document", "news"):
        verdict = await judge_item(
            input_kind=kind,
            title="Council adopts DAC8",
            summary="Adopted 12 August 2026.",
            source_name="ESMA News",
            source_class="regulator",
            source_language="en",
            published_at=datetime(2026, 8, 12, tzinfo=UTC),
            recent_accepted_titles=("Earlier accepted item",),
            template=_GUARD_PROMPT,
            services_block=render_services(
                [
                    {"key": k, "label": lab, "description_for_guard": d}
                    for k, lab, d, _u in _ICON_TAXONOMY
                ]
            ),
            tiers_block=render_jurisdiction_tiers({"tier1": ["CH"], "tier2": ["US"]}),
            allowed_service_keys=_ALLOWED,
        )
        assert verdict.accepted
    assert "input_kind: document" in seen[0]
    assert "input_kind: news" in seen[1]
    assert "Earlier accepted item" in seen[0]


async def test_an_unknown_input_kind_is_a_programming_error(monkeypatch) -> None:
    async def fake_call(prompt, *, model):
        raise AssertionError("must not be called")

    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", fake_call
    )
    with pytest.raises(ValueError, match="input_kind"):
        await judge_item(
            input_kind="rumour",
            title="t",
            summary="s",
            source_name="n",
            source_class="news",
            source_language="en",
            published_at=None,
            recent_accepted_titles=(),
            template=_GUARD_PROMPT,
            services_block="-",
            tiers_block="-",
            allowed_service_keys=_ALLOWED,
        )


async def test_a_transport_failure_retries_three_times_then_defers(
    monkeypatch,
) -> None:
    """NTS_106 §1: three attempts with backoff, then the item is deferred —
    not judged, replayed by the next intake. Deferred is a visible
    non-decision, and the sleeps are injected so the test does not wait 6 s."""
    calls = {"n": 0}
    slept: list[float] = []

    async def flaky(prompt, *, model):
        calls["n"] += 1
        raise TimeoutError("upstream 429")

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", flaky
    )
    with pytest.raises(GuardDeferred):
        await judge_item(
            input_kind="news",
            title="t",
            summary="s",
            source_name="n",
            source_class="news",
            source_language="en",
            published_at=None,
            recent_accepted_titles=(),
            template=_GUARD_PROMPT,
            services_block="-",
            tiers_block="-",
            allowed_service_keys=_ALLOWED,
            sleep=fake_sleep,
        )
    assert calls["n"] == 3
    assert slept == [2.0, 4.0]  # backoff between attempts, not after the last


async def test_a_malformed_body_is_not_retried(monkeypatch) -> None:
    """Retrying the same prompt against the same model reproduces a malformed
    body and pays twice for it. A schema violation is a rubric or model
    problem, not weather."""
    calls = {"n": 0}

    async def bad_shape(prompt, *, model):
        calls["n"] += 1
        raise GuardSchemaError("not JSON")

    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", bad_shape
    )
    with pytest.raises(GuardSchemaError):
        await judge_item(
            input_kind="news",
            title="t",
            summary="s",
            source_name="n",
            source_class="news",
            source_language="en",
            published_at=None,
            recent_accepted_titles=(),
            template=_GUARD_PROMPT,
            services_block="-",
            tiers_block="-",
            allowed_service_keys=_ALLOWED,
        )
    assert calls["n"] == 1


async def test_a_guard_call_records_its_cost_split_by_input_kind(
    monkeypatch,
) -> None:
    """NTS_099 §"Мерить" (measure from day one): guard cost, separately per
    input_kind.
    ``cost_records`` has no such column, so the split rides in ``operation``."""
    captured: list[dict] = []

    async def fake_call(prompt, *, model):
        return _accept_payload(), 1000, 100

    monkeypatch.setattr(
        "pipeline.selector.editorial_guard._call_guard_model", fake_call
    )
    monkeypatch.setattr(
        "pipeline.admin.cost_recorder.record_cost",
        lambda **kw: captured.append(kw),
    )
    for kind in ("document", "news"):
        await judge_item(
            input_kind=kind,
            title="t",
            summary="s",
            source_name="n",
            source_class="news",
            source_language="en",
            published_at=None,
            recent_accepted_titles=(),
            template=_GUARD_PROMPT,
            services_block="-",
            tiers_block="-",
            allowed_service_keys=_ALLOWED,
            model="gpt-4o-mini",
        )
    assert [c["operation"] for c in captured] == ["guard:document", "guard:news"]
    # gpt-4o-mini: $0.15/1M in, $0.60/1M out.
    assert captured[0]["cost_usd"] == pytest.approx(
        1000 / 1e6 * 0.15 + 100 / 1e6 * 0.60
    )


# --- DoD 3: services and tiers are per brand ------------------------------


def test_the_rubric_has_no_hardcoded_service_or_jurisdiction() -> None:
    """DoD 3. The fallback constant must carry the *placeholders*, never
    Icon's actual services or tier lists — a constant that names them works
    perfectly for Icon and silently judges brand two by Icon's rubric."""
    rendered_names = ("Wealth Management", "Family Office", "Special Solutions")
    for name in rendered_names:
        assert name not in _GUARD_PROMPT
    assert "{services}" in _GUARD_PROMPT
    assert "{jurisdiction_tiers}" in _GUARD_PROMPT
    # No tier list either: those are NTS_115 artefact 4, config data.
    assert "CH, CY, MT" not in _GUARD_PROMPT


def test_services_render_from_taxonomy_rows() -> None:
    rendered = render_services(
        [
            {"key": k, "label": lab, "description_for_guard": d}
            for k, lab, d, _u in _ICON_TAXONOMY
        ]
    )
    for key, label, description, _url in _ICON_TAXONOMY:
        assert f"- {key}: {label} — {description}" in rendered


def test_no_services_renders_an_instruction_to_reject_not_an_empty_block() -> None:
    """With no services the rubric can only reject, and that has to be said out
    loud: a guard that accepts into a service the brand does not sell is worse
    than one that accepts nothing."""
    rendered = render_services([])
    assert "out_of_scope" in rendered


def test_tiers_render_from_the_config_key_and_tier3_is_implicit() -> None:
    rendered = render_jurisdiction_tiers(
        {"tier1": ["CH", "CY"], "tier2": ["US", "SG"]}
    )
    assert "- tier1: CH, CY" in rendered
    assert "- tier2: US, SG" in rendered
    assert "tier3" not in rendered  # anything unlisted, by definition


def test_the_rendered_prompt_has_no_unsubstituted_placeholder() -> None:
    """A leftover ``{name}`` means the model is being asked to fill in a field
    the pipeline was supposed to supply."""
    rendered = render_guard_prompt(
        _GUARD_PROMPT,
        services="- wealth: W — desc",
        jurisdiction_tiers="- tier1: CH",
        input_kind="news",
        title="T",
        summary=None,
        source_name="S",
        source_class="regulator",
        source_language="en",
        published_at=None,
        recent_accepted_titles=(),
    )
    leftovers = {
        f for _, f, _, _ in string.Formatter().parse(rendered) if f
    }
    assert leftovers == set()
    assert "(no summary)" in rendered
    assert "(nothing accepted yet)" in rendered


# --- the placeholder contract ---------------------------------------------


def test_the_constant_carries_exactly_the_ten_required_placeholders() -> None:
    """NTS_099 §6 lists ten. Fewer and the rubric cannot see its input; more
    and the render raises a KeyError mid-run."""
    assert guard_template_placeholders(_GUARD_PROMPT) == GUARD_REQUIRED_PLACEHOLDERS
    assert len(GUARD_REQUIRED_PLACEHOLDERS) == 10


# --- DoD 5: the sentinel — a UI edit reaches the next run -----------------


@pytest.fixture
def client_and_brand(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_TRIGGER_SECRET", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("ADMIN_LOG_PATH", str(tmp_path / "missing.log"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session)
        session.add(
            PipelineConfig(
                brand_id_fk=icon_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps([]),
                voice_profile="mission: x\n",
            )
        )
        for key, label, description, url_path in _ICON_TAXONOMY:
            session.add(
                BrandTaxonomy(
                    brand_id_fk=icon_id,
                    key=key,
                    label=label,
                    description_for_guard=description,
                    service_url_path=url_path,
                )
            )
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


def _create_and_activate(client, brand_id: int, content: str) -> int:
    created = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json={
            "brand_id": brand_id,
            "prompt_type": "editorial_guard",
            "version_name": "edited in the UI",
            "content": content,
        },
    )
    assert created.status_code in (200, 201), created.text
    prompt_id = created.json()["id"]
    activated = client.post(
        f"/api/v1/prompts/{prompt_id}/activate", headers=AUTH
    )
    assert activated.status_code == 200, activated.text
    return prompt_id


def test_editorial_guard_is_reachable_through_the_prompts_api(
    client_and_brand,
) -> None:
    """Migration 021 widened the DB CHECK; if the API's ``PromptType`` Literal
    had not been widened with it, the rubric would exist in the database and be
    uneditable from the screen that owns it."""
    client, brand_id = client_and_brand
    _create_and_activate(client, brand_id, _GUARD_PROMPT)
    listed = client.get(
        "/api/v1/prompts",
        headers=AUTH,
        params={"brand_id": brand_id, "prompt_type": "editorial_guard"},
    )
    assert listed.status_code == 200
    assert [p["prompt_type"] for p in listed.json()] == ["editorial_guard"]


def test_sentinel_an_edit_saved_in_the_ui_is_what_the_next_run_renders(
    client_and_brand,
) -> None:
    """DoD 5, end to end: save through the API, resolve the way the intake run
    resolves, and find the edited sentence in the rendered prompt."""
    client, brand_id = client_and_brand
    sentinel = "SENTINEL-2026: treat Liechtenstein foundations as tier1."
    edited = _GUARD_PROMPT.replace(
        "=== THE ITEM ===", f"{sentinel}\n\n=== THE ITEM ==="
    )
    _create_and_activate(client, brand_id, edited)

    template, source = resolve_guard_template(brand_id)
    assert source == "db"
    rendered = render_guard_prompt(
        template,
        services=render_services(load_brand_taxonomy(brand_id)),
        jurisdiction_tiers=render_jurisdiction_tiers({"tier1": ["LI"]}),
        input_kind="news",
        title="T",
        summary="S",
        source_name="N",
        source_class="news",
        source_language="en",
        published_at=None,
        recent_accepted_titles=(),
    )
    assert sentinel in rendered
    # And the per-brand blocks really came from the tables, not constants.
    assert "wealth: Wealth Management" in rendered
    assert "- tier1: LI" in rendered


def _activate_directly(brand_id: int, content: str) -> None:
    """Put an active rubric row in place bypassing the API.

    Since S3 the API refuses to *activate* an unrenderable body, so a broken
    active row can now only arrive another way — a migration, a direct SQL
    edit, an older client. The resolver still has to survive it, which is what
    this helper sets up.
    """
    from sqlalchemy import update

    from pipeline.admin.models import Prompt

    with admin_db.get_session_factory()() as session:
        session.execute(
            update(Prompt)
            .where(
                Prompt.brand_id_fk == brand_id,
                Prompt.prompt_type == "editorial_guard",
            )
            .values(is_active=False)
        )
        session.add(
            Prompt(
                brand_id_fk=brand_id,
                prompt_type="editorial_guard",
                version_name="broken, inserted directly",
                content=content,
                is_active=True,
                created_by="test",
            )
        )
        session.commit()


def test_a_broken_placeholder_set_falls_back_to_the_constant(
    client_and_brand,
) -> None:
    """NTS_071 §2's failure, for the rubric: the operator's edit stops reaching
    production and the only evidence is a log line. So the fallback must be
    exercised — and must not raise mid-run."""
    _client, brand_id = client_and_brand

    # (a) a required placeholder deleted
    _activate_directly(
        brand_id, _GUARD_PROMPT.replace("{recent_accepted_titles}", "")
    )
    template, source = resolve_guard_template(brand_id)
    assert source == "code"
    assert template == _GUARD_PROMPT

    # (b) an invented placeholder the pipeline cannot supply
    _activate_directly(brand_id, _GUARD_PROMPT + "\nExtra: {my_new_field}\n")
    template, source = resolve_guard_template(brand_id)
    assert source == "code"


def test_the_api_refuses_to_activate_a_rubric_that_would_not_apply(
    client_and_brand,
) -> None:
    """Defence in depth for the same failure, one layer earlier: since S3 the
    operator is told at activation time instead of discovering it from the next
    run's output (NTS_063 pending, closed in S3)."""
    client, brand_id = client_and_brand
    created = client.post(
        "/api/v1/prompts",
        headers=AUTH,
        json={
            "brand_id": brand_id,
            "prompt_type": "editorial_guard",
            "version_name": "work in progress",
            "content": _GUARD_PROMPT.replace("{services}", ""),
        },
    )
    # Saving a draft is allowed — an unfinished edit is not an error.
    assert created.status_code in (200, 201), created.text
    refused = client.post(
        f"/api/v1/prompts/{created.json()['id']}/activate", headers=AUTH
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["missing"] == ["services"]
    # And nothing became active, so the run still resolves to the constant.
    assert resolve_guard_template(brand_id) == (_GUARD_PROMPT, "code")


def test_no_active_row_and_no_brand_both_resolve_to_the_constant(
    client_and_brand,
) -> None:
    _client, brand_id = client_and_brand
    assert resolve_guard_template(brand_id) == (_GUARD_PROMPT, "code")
    assert resolve_guard_template(None) == (_GUARD_PROMPT, "code")


def test_taxonomy_loads_ordered_and_is_empty_for_an_unknown_brand(
    client_and_brand,
) -> None:
    _client, brand_id = client_and_brand
    rows = load_brand_taxonomy(brand_id)
    assert [r["key"] for r in rows] == sorted(_ALLOWED)
    assert load_brand_taxonomy(99999) == ()
    assert load_brand_taxonomy(None) == ()


# --- DoD 4: the migration, up and down on a real file ---------------------

_020 = "020_v3_portfolio_core"
_021 = "021_editorial_guard_prompt_type"
_022 = "022_intake_flags_and_primary_feeds"


@pytest.fixture
def alembic_db(tmp_path: Path):
    """A real sqlite file driven by the real alembic CLI.

    The suite builds its other databases with ``create_all``; migrations have
    to be tested through alembic or the thing under test is not the thing that
    runs on prod.
    """
    db = tmp_path / "admin.db"
    root = Path(__file__).resolve().parents[2]

    def alembic(*args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "ADMIN_DB_PATH": str(db)}
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    return db, alembic


def test_023_seeds_one_active_rubric_per_brand_and_the_resolver_accepts_it(
    alembic_db, monkeypatch
) -> None:
    """DoD 4 + the point of writing the row from the constant: the shipped row
    must be one the shipped resolver accepts. A row this code cannot render is
    worse than no row — it looks live and is not."""
    db, alembic = alembic_db
    alembic("upgrade", "head")

    with sqlite3.connect(db) as conn:
        brands = conn.execute("SELECT count(*) FROM brands").fetchone()[0]
        rows = conn.execute(
            "SELECT brand_id_fk, content, is_active, created_by FROM prompts "
            "WHERE prompt_type = 'editorial_guard' ORDER BY brand_id_fk"
        ).fetchall()
    assert len(rows) == brands >= 1
    assert all(r[2] == 1 for r in rows)
    assert all(r[3] == "migration_023" for r in rows)
    for _brand, content, _active, _by in rows:
        assert guard_template_placeholders(content) == GUARD_REQUIRED_PLACEHOLDERS

    # The resolver reads it as the live rubric, not as a broken row.
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(db))
    admin_db.reset_for_tests()
    try:
        template, source = resolve_guard_template(rows[0][0])
        assert source == "db"
        assert template == rows[0][1]
    finally:
        admin_db.reset_for_tests()


def test_023_is_idempotent_and_never_overwrites_an_edit(alembic_db) -> None:
    db, alembic = alembic_db
    alembic("upgrade", "head")
    edited = _GUARD_PROMPT + "\nOperator note.\n"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE prompts SET content = ? WHERE prompt_type = 'editorial_guard'",
            (edited,),
        )
        before = conn.execute(
            "SELECT count(*) FROM prompts WHERE prompt_type = 'editorial_guard'"
        ).fetchone()[0]

    alembic("downgrade", _022)  # 023's downgrade keeps an edited row
    alembic("upgrade", "head")  # and re-running does not duplicate it

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT content FROM prompts WHERE prompt_type = 'editorial_guard'"
        ).fetchall()
    assert len(rows) == before
    assert all(r[0] == edited for r in rows)


def test_the_full_v3_stack_walks_down_to_020_and_back_up_cleanly(
    alembic_db,
) -> None:
    """DoD 4 asks for up **and** down. With the rubric untouched, 023 removes
    what it seeded, which lets 021 narrow the CHECK — so the whole S1+S2 stack
    is reversible in one command, on a file, with integrity checked."""
    db, alembic = alembic_db
    alembic("upgrade", "head")
    alembic("downgrade", _020)

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT count(*) FROM prompts WHERE prompt_type='editorial_guard'"
            ).fetchone()[0]
            == 0
        )
        columns = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_config)")}
        assert {"intake_enabled", "v2_generation_enabled", "guard_model"} & columns == set()
        assert (
            conn.execute(
                "SELECT count(*) FROM sources WHERE source_role='primary_feed'"
            ).fetchone()[0]
            == 0
        )

    alembic("upgrade", "head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        columns = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_config)")}
        assert {"intake_enabled", "v2_generation_enabled", "guard_model"} <= columns


def test_022_seeds_the_primary_feeds_with_their_registry_classification(
    alembic_db,
) -> None:
    """NTS_115 artefact 1. The classification is the point: ``source_role``
    decides the item's ``input_kind`` and which prefilter age limit applies, so
    a feed inserted as plain news would be judged by the wrong rules."""
    db, alembic = alembic_db
    alembic("upgrade", "head")

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT name, source_role, source_class, license_class, doc_language, "
            "fetch_method, active FROM sources WHERE source_role = 'primary_feed' "
            "ORDER BY name"
        ).fetchall()
    by_name = {r[0]: r for r in rows}
    assert len(rows) == 13  # twelve listed feeds; EUR-Lex is two saved searches

    assert by_name["FINMA News DE"][2:6] == (
        "regulator",
        "public_official",
        "de",
        "rss",
    )
    assert by_name["HMRC policy papers"][2:6] == (
        "tax_authority",
        "public_official",
        "en",
        "atom",
    )
    assert by_name["GlobeNewswire M&A"][2:4] == ("corporate_pr", "corporate_pr")
    assert by_name["Deloitte tax@hand"][2:4] == (
        "professional_alert",
        "professional_commentary",
    )
    # Everything with a fetcher is active; the four without one are not, so the
    # Sources screen does not fill with daily failures for unimplemented code.
    for name, row in by_name.items():
        has_fetcher = row[5] in ("rss", "atom")
        is_placeholder_url = "REPLACE_ME" in name or name.startswith("EUR-Lex")
        assert bool(row[6]) == (has_fetcher and not is_placeholder_url), name


def test_022_re_applied_keeps_operator_edits_and_inserts_no_duplicates(
    alembic_db,
) -> None:
    """A half-applied deploy has to be re-runnable, and the operator owns the
    feed list from the Sources screen the moment it exists.

    ``stamp`` then ``upgrade`` is the shape of that incident: the version
    marker says 021 while the tables are already at 022. Re-running must add no
    second copy of any feed and must leave a pause and a reclassification
    alone — the insert is keyed on ``(brand, url)`` and skips what is present.
    """
    db, alembic = alembic_db
    alembic("upgrade", "head")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE sources SET active = 0, license_class = 'public_domain' "
            "WHERE name = 'FCA News'"
        )
        before = conn.execute(
            "SELECT count(*) FROM sources WHERE source_role = 'primary_feed'"
        ).fetchone()[0]

    alembic("stamp", _021)
    alembic("upgrade", "head")

    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM sources WHERE source_role = 'primary_feed'"
            ).fetchone()[0]
            == before
        )
        assert conn.execute(
            "SELECT active, license_class FROM sources WHERE name = 'FCA News'"
        ).fetchall() == [(0, "public_domain")]
