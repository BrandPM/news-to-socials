"""The feeds and the prefilter after the NTS_129 P2 audit (migrations 032, 033).

The auto-recall on 1 532 accumulated candidates read 8% against a target of
70%, and the per-source audit said why: the general-business wires supplied 85%
of the funnel and 10% of the accepts, while the regulator feeds had the
opposite ratio and almost no volume. Two migrations answer that — 032 changes
what is fetched, 033 changes what survives the prefilter — and this file
asserts the properties that are easy to break later and expensive to notice.

What is deliberately NOT tested here: that a given URL is reachable. Every URL
in 032 was fetched live before it was written (200 *and* a non-empty parse, in
the session log), but a test that hit the network would fail on a regulator's
maintenance window and teach everyone to ignore it. Source liveness is
``source_health_records``' job, which is checked continuously and alerts.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = PROJECT_ROOT / "pipeline/admin/migrations/versions"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m032 = _load("m032", "032_source_overhaul.py")
m033 = _load("m033", "033_prefilter_from_audit.py")


# --------------------------------------------------------------------------
# 032 — the new feed set
# --------------------------------------------------------------------------


def test_every_new_feed_is_a_primary_source_or_a_named_specialist() -> None:
    """The point of the overhaul: volume from regulators, not from wires.

    A future edit that adds a general news feed here would quietly undo the
    thing the migration exists to do, and the recall number would not move for
    a week.
    """
    for name, _url, _cat, role, source_class, *_rest in m032.NEW_FEEDS:
        assert role in ("primary_feed", "news"), name
        if role == "news":
            # The three exceptions are specialist trade press on Icon's exact
            # subjects, and each is classed as such rather than as ``news``.
            assert source_class == "professional_alert", name


def test_the_new_feeds_cover_the_jurisdictions_recall_reported_missing() -> None:
    """NTS_115's seed topics name jurisdictions; ``MISSING_CHANNEL_JURISDICTIONS``
    is the recall report's list of the ones no source could ever have served.
    Adding feeds without closing those is how a recall number stays at 8%."""
    urls = " ".join(url for _n, url, *_r in m032.NEW_FEEDS).lower()
    names = " ".join(name for name, *_r in m032.NEW_FEEDS).lower()
    haystack = urls + " " + names
    for token in ("eba", "cssf", "mfsa", "sec", "gfsc", "iomfsa", "ecb", "treasury"):
        assert token in haystack, token


def test_deactivations_carry_their_evidence() -> None:
    """A feed switched off without a number next to it is an opinion. Every
    entry here has to say what it produced, because that string is what an
    operator reads on the Sources screen when deciding to switch it back on."""
    for url, reason in m032.NOISE_FEEDS:
        assert url.startswith("http"), url
        assert "items" in reason and "accepted" in reason, url
    for url, reason in m032.UNREACHABLE_FEEDS:
        assert reason.strip(), url


def test_no_url_is_both_added_and_deactivated() -> None:
    added = {url for _n, url, *_r in m032.NEW_FEEDS}
    removed = {url for url, _r in m032.NOISE_FEEDS + m032.UNREACHABLE_FEEDS}
    assert added & removed == set()


# --------------------------------------------------------------------------
# 033 — the deny patterns, against the titles that produced them
# --------------------------------------------------------------------------


def _rules_from(patterns):
    from pipeline.selector.prefilter import PrefilterRules

    class _C:
        prefilter_deny_title_patterns = tuple(patterns)
        prefilter_require_summary = False
        prefilter_max_age_hours_news = 72
        prefilter_max_age_hours_primary = 240
        prefilter_languages = ()
        prefilter_min_summary_chars = 0

    return PrefilterRules.from_config(_C())


# Real titles from the 28.08–06.09 reject sample, on feeds migration 032 keeps.
AUDIT_NOISE = (
    "Edmond de Rothschild names new global CIO and equities co-head",
    "Citizens Snags Northern Trust Duo in Florida",
    "Shook CEO Tells Advisors Rankings Will Resume in 2027",
    "Market Brief: AI Spending Is Becoming a Pillar of the Global Economy",
    "PE-backed Authentic Brands acquires IP of Drake's lifestyle brand OVO",
    "Women in PE 2020: where are they now?",
    "NSP Capital targets $150m for debut fund",
    "Kroll celebrates double milestone",
)

# Icon's actual subject matter. If a deny pattern ever matches one of these,
# the prefilter is silently deleting the material the pipeline exists for —
# and unlike a guard rejection, a prefilter drop never becomes a candidate, so
# nothing in the funnel would show it.
MUST_SURVIVE = (
    "EU adopts DAC8 reporting rules for crypto-asset service providers",
    "Malta updates the residence programme's minimum contribution",
    "FINMA opens enforcement proceedings against a Swiss bank over AML failings",
    "Companies House sets the date for mandatory identity verification",
    "Cyprus raises the corporate income tax rate to 15%",
    "EU-backed fund launches sanctions compliance review",
    "HMRC publishes guidance on the non-dom replacement regime",
    "SEC charges an adviser over undisclosed conflicts",
    "OECD ministers agree Pillar Two implementation timetable at conference",
    "Guernsey consults on changes to its private investment fund rules",
)


@pytest.mark.parametrize("title", AUDIT_NOISE)
def test_the_new_patterns_catch_the_titles_they_were_written_from(title) -> None:
    from pipeline.selector.prefilter import prefilter_item

    decision = prefilter_item(
        title=title,
        summary="x" * 200,
        published_at=None,
        source_role="news",
        source_language="en",
        rules=_rules_from(m033.NEW_DENY_PATTERNS),
    )
    assert decision.keep is False, title
    assert decision.reason == "deny_title"


@pytest.mark.parametrize("title", MUST_SURVIVE)
def test_no_pattern_in_the_shipped_default_eats_icons_subject_matter(title) -> None:
    """The negative half, and the one that matters more.

    Run against the *full* shipped list, not just 033's additions: the risk is
    the interaction, and "EU-backed" / "at conference" are here precisely
    because they are the shapes that argued for the four patterns that were
    left out.
    """
    from pipeline.admin.config_client import ConfigRecord
    from pipeline.selector.prefilter import prefilter_item

    decision = prefilter_item(
        title=title,
        summary="x" * 200,
        published_at=None,
        source_role="news",
        source_language="en",
        rules=_rules_from(ConfigRecord.prefilter_deny_title_patterns),
    )
    assert decision.keep is True, f"{title} dropped on {decision.detail!r}"


def test_the_deny_list_still_never_applies_to_a_primary_feed() -> None:
    """The hotfix of 2026-08-28, re-asserted because 033 triples the list and
    a regulator does "appoint" a board and does publish an "outlook"."""
    from pipeline.admin.config_client import ConfigRecord
    from pipeline.selector.prefilter import prefilter_item

    for title in AUDIT_NOISE:
        decision = prefilter_item(
            title=title,
            summary=None,
            published_at=None,
            source_role="primary_feed",
            source_language="en",
            rules=_rules_from(ConfigRecord.prefilter_deny_title_patterns),
        )
        assert decision.keep is True, title


# --------------------------------------------------------------------------
# both migrations, through the real runner
# --------------------------------------------------------------------------


def _alembic_env(test_db: Path) -> dict:
    return {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(PROJECT_ROOT),
        "ADMIN_DB_PATH": str(test_db),
    }


def _alembic(test_db: Path, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=_alembic_env(test_db),
        capture_output=True,
        text=True,
        check=True,
    )


def _deny(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute(
        "SELECT prefilter_deny_title_patterns FROM pipeline_config LIMIT 1"
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def _set_deny(test_db: Path, deny: list[str]) -> int:
    """Stand in for an operator edit on the Editorial screen.

    The migration chain seeds the ``icon`` brand but not its config row, so the
    row is created here with the edited value already in it — the whole point
    is to observe what the *migration* does to a value that was there before it
    ran, which means writing it through raw SQL on the Alembic-built file
    rather than through the ORM's own defaults.
    """
    with sqlite3.connect(test_db) as conn:
        brand_id = conn.execute("SELECT id FROM brands LIMIT 1").fetchone()
        assert brand_id, "the migration chain no longer seeds a brand"
        conn.execute(
            "INSERT INTO pipeline_config (brand_id_fk, scoring_threshold, "
            "topics_per_run, banned_phrases, voice_profile, "
            "prefilter_deny_title_patterns, updated_at) "
            "VALUES (?, 7, 3, '[]', 'mission: x', ?, CURRENT_TIMESTAMP)",
            (brand_id[0], json.dumps(deny)),
        )
        conn.commit()
        return int(brand_id[0])


def test_033_extends_the_list_and_preserves_an_operator_edit(tmp_path) -> None:
    """The failure this guards against is silent: a migration that replaced the
    key would discard whatever Andriy tuned from the Editorial screen, and the
    only symptom would be noise reappearing in the funnel weeks later."""
    test_db = tmp_path / "alembic-test.db"
    _alembic(test_db, "upgrade", "032_source_overhaul")

    _set_deny(test_db, ["appoints", "andriys own pattern"])

    _alembic(test_db, "upgrade", "head")
    with sqlite3.connect(test_db) as conn:
        after = _deny(conn)
    assert "andriys own pattern" in after, "an operator edit was discarded"
    assert after[:2] == ["appoints", "andriys own pattern"], "order was not preserved"
    assert set(m033.NEW_DENY_PATTERNS) <= set(after)
    # Idempotent: "appoints" was already there and must not be duplicated.
    assert len(after) == len(set(after))

    _alembic(test_db, "downgrade", "032_source_overhaul")
    with sqlite3.connect(test_db) as conn:
        rolled_back = _deny(conn)
    assert rolled_back == ["appoints", "andriys own pattern"]

    # up → down → up, the cycle NTS_116 requires of every migration.
    _alembic(test_db, "upgrade", "head")
    with sqlite3.connect(test_db) as conn:
        assert set(m033.NEW_DENY_PATTERNS) <= set(_deny(conn))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
