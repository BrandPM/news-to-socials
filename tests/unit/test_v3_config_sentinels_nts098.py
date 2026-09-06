"""IT_PROJ_NTS_098 DoD 6 — every v3 config key reaches the runtime.

"Sentinel на каждый ключ, не выборочно." This project has twice shipped a
config surface that nothing read: a value saved in Settings that the next run
ignored, with no error anywhere. The failure is silent by nature — the run
succeeds, it just uses a number nobody chose.

So each of the 40 v3 keys — 25 from migration 020, two mode flags from 022,
``production_enabled`` + ``rank_weights`` from 026, the five document
budgets from 027 the four composition keys from 028 and
``cover_mode`` from 030 — is walked end to end:

    migration default → ORM column → ConfigRecord → GET /config
                     → PUT /config → ConfigRecord again

driven off ONE table. :func:`test_every_v3_column_has_a_sentinel` fails if a
column exists on the model without an entry, so the next person to add a key
cannot forget to prove it arrives.

The five JSON-as-TEXT keys get extra attention: they are the ones where a
plausible-looking implementation stores a Python ``repr`` and the reader gets
``"['a', 'b']"`` back.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pipeline.admin import db as admin_db
from pipeline.admin.config_client import AdminConfigClient
from pipeline.admin.models import PipelineConfig
from pipeline.common import config as config_module
from tests.unit.conftest import seed_icon_brand

ADMIN_TOKEN = "tok-nts098"
AUTH = {"X-Admin-Token": ADMIN_TOKEN}


# --- the sentinel table ---------------------------------------------------
# (key, migration default as ConfigRecord exposes it, a DIFFERENT value to
# write through the API). Every "edited" value is deliberately unlike the
# default, so a read path that ignores the DB and returns the constant fails.

_SENTINELS: tuple[tuple[str, Any, Any], ...] = (
    # --- rhythm (NTS_098 §4/§5)
    (
        "publication_slots",
        ({"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}),
        [{"day": "tue", "capacity": 3}, {"day": "fri", "capacity": 1}],
    ),
    ("weekly_draft_budget", 6, 9),
    ("portfolio_daily_cap_document", 2, 4),
    ("portfolio_daily_cap_news", 1, 3),
    (
        "candidate_ttl_days",
        {
            "deal_announced": 7,
            "deal_closed": 7,
            "consultation": 21,
            "default": 14,
        },
        {"deal_announced": 5, "consultation": 30, "default": 10},
    ),
    ("production_timeout_min", 60, 90),
    ("max_attempts", 2, 3),
    ("brand_timezone", "Europe/Madrid", "Europe/Zurich"),
    ("retention_days_rejected", 30, 45),
    # --- dedup windows (NTS_098 §3)
    ("dedup_threshold_live", 0.90, 0.93),
    ("dedup_threshold_rejected", 0.92, 0.95),
    ("dedup_window_rejected_days", 14, 21),
    ("dedup_threshold_published", 0.88, 0.85),
    ("dedup_window_published_days", 60, 90),
    # --- guard axes (NTS_099 §2, values from NTS_115)
    (
        "jurisdiction_tiers",
        {
            "tier1": ("CH", "CY", "MT", "AE", "UK", "PL", "UA", "LI", "EU"),
            "tier2": (
                "US", "SG", "HK", "LU", "MC", "PT", "ES", "IT",
                "DE", "AT", "IL", "KZ", "TR",
            ),
        },
        {"tier1": ["CH", "SG"], "tier2": ["US"]},
    ),
    # --- depth (NTS_102 v2)
    ("depth_article_min_facts", 4, 6),
    ("depth_deep_min_facts", 10, 14),
    # --- spend kill-switch (NTS_106 §3)
    ("monthly_spend_cap_usd", 150.0, 220.0),
    ("max_cost_per_candidate_usd", 5.0, 8.5),
    # --- prefilter (NTS_099 §1)
    (
        "prefilter_deny_title_patterns",
        (
            "appoints", "hires", "joins", "named as", "wins award", "ranked",
            "opens office", "rebrand", "outlook", "forecast", "analysts expect",
        ),
        ["appoints", "sponsors"],
    ),
    ("prefilter_require_summary", True, False),
    ("prefilter_max_age_hours_news", 72, 48),
    ("prefilter_max_age_hours_primary", 240, 336),
    (
        "prefilter_languages",
        ("en", "de", "fr", "it", "pl", "uk", "ru", "el"),
        ["en", "de"],
    ),
    ("prefilter_min_summary_chars", 80, 120),
    # --- mode flags (NTS_103 шаг 1/3) — migration 022. Both default OFF, and
    # that is the assertion that matters: the cutover directive is that the
    # deploy which lands these keys generates nothing until a human says so.
    ("intake_enabled", False, True),
    ("v2_generation_enabled", False, True),
    # migration 026 — the third mode flag, and the rank weights it needs.
    # ``production_enabled`` defaults OFF for the same reason the two above do:
    # the deploy that lands S4 must not start spending because it landed.
    ("production_enabled", False, True),
    (
        "rank_weights",
        {
            "w_conf": 0.30,
            "w_depth": 0.25,
            "w_fresh": 0.15,
            "w_juris": 0.15,
            "w_kind": 0.05,
            "w_div": 0.20,
            "w_juris_div": 0.10,
        },
        {
            "w_conf": 0.5,
            "w_depth": 0.1,
            "w_fresh": 0.1,
            "w_juris": 0.1,
            "w_kind": 0.1,
            "w_div": 0.3,
            "w_juris_div": 0.2,
        },
    ),
    # migration 027 — the primary-document fetch budgets (NTS_101 §4).
    ("doc_timeout_s", 60, 120),
    ("doc_max_mb", 25, 60),
    ("doc_max_tokens_for_composition", 12000, 20000),
    ("doc_retries", 2, 3),
    ("doc_match_model", "gpt-4o-mini", "gpt-4o"),
    # migration 028 — composition (NTS_102 v2, NTS_095, NTS_108 §1).
    # ``data_blocks_enabled`` defaults OFF and stays off until the Sanity
    # schema PR of S8 is merged: the order is schema → render → pipeline.
    ("data_blocks_enabled", False, True),
    (
        "depth_length_targets",
        {"note": (300, 450), "article": (600, 900), "deep": (1200, None)},
        {"note": [200, 300], "article": [700, 1000], "deep": [1500, None]},
    ),
    (
        "max_quote_words",
        {"professional_commentary": 15, "corporate_pr": 25, "news_paywalled": 0},
        {"professional_commentary": 10, "corporate_pr": 30},
    ),
    ("attribution_model", "gpt-4o-mini", "gpt-4o"),
    # migration 030 — NTS_112. Defaults to the CURRENT behaviour (flux), for
    # the same reason every other v3 flag does: the deploy that lands a new
    # mode must not switch modes.
    ("cover_mode", "flux", "data"),
    ("guard_model", "gpt-4o-mini", "gpt-4o"),
)

# JSON-as-TEXT columns: ConfigRecord hands back immutable Python objects, the
# API hands back plain lists/dicts. Normalised before comparison.
_JSON_KEYS = {
    "publication_slots",
    "candidate_ttl_days",
    "jurisdiction_tiers",
    "prefilter_deny_title_patterns",
    "prefilter_languages",
    "rank_weights",
    "depth_length_targets",
    "max_quote_words",
}


def _plain(value: Any) -> Any:
    """Tuples/MappingProxy → lists/dicts, recursively."""
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if hasattr(value, "items"):
        return {k: _plain(v) for k, v in value.items()}
    return value


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
                banned_phrases=json.dumps(["delve into"]),
                voice_profile="mission: x\n",
            )
        )
        session.commit()

    from pipeline.admin.server import create_app

    yield TestClient(create_app()), icon_id
    admin_db.reset_for_tests()


# --- completeness ---------------------------------------------------------


def test_every_v3_column_has_a_sentinel() -> None:
    """The guard that makes this file "не выборочно".

    Adding a v3 config column without adding it to ``_SENTINELS`` fails here,
    so a key can never ship unproven.
    """
    v2_columns = {
        "brand_id_fk",
        "scoring_threshold",
        "topics_per_run",
        "banned_phrases",
        "voice_profile",
        "updated_at",
        "stale_draft_days",
        "dedup_enabled",
        "dedup_threshold",
        "dedup_window_days",
        "eval_enabled",
        "eval_threshold",
        "images_on_demand",
        "research_enabled",
        "research_max_sources",
        "research_max_tokens",
        "research_timeout_seconds",
    }
    all_columns = {c.name for c in PipelineConfig.__table__.columns}
    v3_columns = all_columns - v2_columns
    covered = {name for name, _default, _edited in _SENTINELS}

    assert v3_columns == covered, (
        f"unsentinelled column(s): {sorted(v3_columns - covered)}; "
        f"sentinel for a column that no longer exists: {sorted(covered - v3_columns)}"
    )
    # 25 from 020, three mode flags (022 + 026), rank weights (026), the five
    # document budgets (027) the four composition
    # keys (028) and the cover mode (030).
    assert len(_SENTINELS) == 40


# --- default reaches the runtime -----------------------------------------


@pytest.mark.parametrize(
    ("key", "default"), [(k, d) for k, d, _e in _SENTINELS], ids=[s[0] for s in _SENTINELS]
)
def test_migration_default_reaches_the_config_record(
    client_and_brand, key: str, default: Any
) -> None:
    """A row created without touching the v3 keys still answers every read
    with the spec's Icon starting value."""
    record = AdminConfigClient(brand_slug="icon").get_config()
    assert _plain(getattr(record, key)) == _plain(default)


@pytest.mark.parametrize(
    ("key", "default"), [(k, d) for k, d, _e in _SENTINELS], ids=[s[0] for s in _SENTINELS]
)
def test_default_is_visible_over_the_api(
    client_and_brand, key: str, default: Any
) -> None:
    client, icon_id = client_and_brand
    response = client.get(f"/api/v1/config?brand_id={icon_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    assert _plain(response.json()[key]) == _plain(default)


# --- an edit reaches the runtime -----------------------------------------


@pytest.mark.parametrize(
    ("key", "edited"), [(k, e) for k, _d, e in _SENTINELS], ids=[s[0] for s in _SENTINELS]
)
def test_edited_value_reaches_the_config_record(
    client_and_brand, key: str, edited: Any
) -> None:
    """The whole point: what Settings saves is what the next run reads."""
    client, icon_id = client_and_brand

    response = client.put(
        f"/api/v1/config?brand_id={icon_id}", json={key: edited}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert _plain(response.json()[key]) == _plain(edited)

    record = AdminConfigClient(brand_slug="icon").get_config()
    assert _plain(getattr(record, key)) == _plain(edited)


@pytest.mark.parametrize("key", sorted(_JSON_KEYS))
def test_json_keys_are_stored_as_json_not_python_repr(
    client_and_brand, key: str
) -> None:
    """The specific way this goes wrong: ``str(value)`` instead of
    ``json.dumps(value)`` stores ``"['en', 'de']"``, which reads back as a
    string, not a list — and only fails much later, in the run."""
    client, icon_id = client_and_brand
    edited = {k: e for k, _d, e in _SENTINELS}[key]

    client.put(
        f"/api/v1/config?brand_id={icon_id}", json={key: edited}, headers=AUTH
    ).raise_for_status()

    with admin_db.get_session_factory()() as session:
        raw = getattr(session.get(PipelineConfig, icon_id), key)
    assert isinstance(raw, str)
    assert json.loads(raw) == _plain(edited)  # parses, and round-trips


def test_a_partial_put_does_not_reset_the_other_v3_keys(
    client_and_brand,
) -> None:
    """``exclude_unset`` semantics, asserted because a config surface that
    silently resets twenty-four keys when you edit one is worse than none."""
    client, icon_id = client_and_brand

    client.put(
        f"/api/v1/config?brand_id={icon_id}",
        json={"weekly_draft_budget": 11},
        headers=AUTH,
    ).raise_for_status()

    record = AdminConfigClient(brand_slug="icon").get_config()
    assert record.weekly_draft_budget == 11
    for key, default, _edited in _SENTINELS:
        if key == "weekly_draft_budget":
            continue
        assert _plain(getattr(record, key)) == _plain(default), f"{key} was reset"


def test_v2_keys_are_untouched_by_the_v3_additions(client_and_brand) -> None:
    """The NTS_090/091/092/094 keys keep working: v2 generation runs until S2
    switches it off, and it reads these."""
    record = AdminConfigClient(brand_slug="icon").get_config()
    assert record.research_enabled is True
    assert record.research_max_sources == 5
    assert record.dedup_threshold == 0.85
    assert record.dedup_window_days == 7
    assert record.eval_threshold == 7.0
    assert record.images_on_demand is False
    assert record.banned_phrases == ["delve into"]


# --- edits that must be refused ------------------------------------------


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"weekly_draft_budget": -1}, "negative budget"),
        ({"max_attempts": 0}, "zero attempts means nothing is ever produced"),
        ({"dedup_threshold_live": 1.5}, "cosine above 1"),
        ({"monthly_spend_cap_usd": -10}, "negative cap"),
        ({"production_timeout_min": 0}, "zero timeout"),
        ({"brand_timezone": "Mars/Olympus_Mons"}, "unknown timezone"),
        ({"publication_slots": [{"day": "funday", "capacity": 2}]}, "bad weekday"),
        ({"publication_slots": [{"day": "mon"}]}, "slot without capacity"),
        ({"publication_slots": [{"day": "mon", "capacity": -1}]}, "negative capacity"),
        ({"jurisdiction_tiers": {"tier3": ["XX"]}}, "tier3 is implicit, not stored"),
    ],
)
def test_bad_edits_are_refused(client_and_brand, payload: dict, why: str) -> None:
    client, icon_id = client_and_brand
    response = client.put(
        f"/api/v1/config?brand_id={icon_id}", json=payload, headers=AUTH
    )
    assert response.status_code == 422, f"{why}: accepted {payload}"


def test_a_refused_edit_changes_nothing(client_and_brand) -> None:
    client, icon_id = client_and_brand
    client.put(
        f"/api/v1/config?brand_id={icon_id}",
        json={"brand_timezone": "Mars/Olympus_Mons"},
        headers=AUTH,
    )
    assert AdminConfigClient(brand_slug="icon").get_config().brand_timezone == (
        "Europe/Madrid"
    )


# --- degraded rows --------------------------------------------------------


def test_malformed_json_in_a_column_falls_back_instead_of_exploding(
    client_and_brand,
) -> None:
    """The config surface is hand-editable and these columns hold JSON. A
    stray comma is an operator typo, not an outage — the run must keep going
    on the documented default."""
    _client, icon_id = client_and_brand
    with admin_db.get_session_factory()() as session:
        row = session.get(PipelineConfig, icon_id)
        row.prefilter_languages = "{not json,"
        row.jurisdiction_tiers = ""
        session.commit()

    record = AdminConfigClient(brand_slug="icon").get_config()
    assert record.prefilter_languages == (
        "en", "de", "fr", "it", "pl", "uk", "ru", "el",
    )
    assert record.jurisdiction_tiers["tier1"][0] == "CH"


def test_the_hardcoded_fallback_path_still_answers_every_v3_key(
    tmp_path, monkeypatch
) -> None:
    """When admin.db is missing entirely, ``get_config`` falls back to the
    seed constants. Every v3 key must still have its documented value there —
    a None would surface as a TypeError deep in a run."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "does-not-exist.db"))
    admin_db.reset_for_tests()

    record = AdminConfigClient(brand_slug="icon").get_config()
    for key, default, _edited in _SENTINELS:
        assert _plain(getattr(record, key)) == _plain(default), key
    admin_db.reset_for_tests()
