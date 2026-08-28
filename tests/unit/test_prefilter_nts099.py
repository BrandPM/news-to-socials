"""IT_PROJ_NTS_099 §1 / DoD 1 — the prefilter, and the metric that watches it.

The prefilter is free and therefore easy to get wrong without noticing: it runs
before the guard, drops items silently by design, and a bug in it looks exactly
like a strict rubric. So this file pins the two rules that are actually
directional (deny patterns skip primary feeds; the age limit differs by role),
plus the drop-rate metric and its alarm band.

The config plumbing itself — every key reaching the runtime from Settings — is
proved separately in ``test_v3_config_sentinels_nts098``. Here the rules are
built from a stand-in config object, so a test failure points at the rule and
not at the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.selector.prefilter import (
    PREFILTER_DROP_REASONS,
    PrefilterRules,
    drop_rate,
    is_drop_rate_alarming,
    prefilter_item,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@dataclass
class _Config:
    """The five NTS_099 §1 keys as ``ConfigRecord`` exposes them."""

    prefilter_deny_title_patterns: tuple[str, ...] = (
        "appoints",
        "hires",
        "wins award",
        "outlook",
    )
    prefilter_require_summary: bool = True
    prefilter_max_age_hours_news: int = 72
    prefilter_max_age_hours_primary: int = 240
    prefilter_languages: tuple[str, ...] = ("en", "de", "pl")
    prefilter_min_summary_chars: int = 80


def _rules(**overrides) -> PrefilterRules:
    return PrefilterRules.from_config(_Config(**overrides))


def _decide(
    *,
    title: str = "EU adopts DAC8 reporting rules for crypto providers",
    summary: str | None = "The Council adopted the directive on 12 August 2026, "
    "with the first reporting period starting in January 2027.",
    published_at: datetime | None = NOW - timedelta(hours=2),
    source_role: str = "news",
    source_language: str | None = "en",
    rules: PrefilterRules | None = None,
):
    return prefilter_item(
        title=title,
        summary=summary,
        published_at=published_at,
        source_role=source_role,
        source_language=source_language,
        rules=rules or _rules(),
        now=NOW,
    )


# --- the config is the source of the rules --------------------------------


def test_rules_come_from_the_config_row_not_a_constant() -> None:
    """DoD 1: "Префилтр-списки в конфиге бренда". An edit in Settings has to
    change the decision — that is the whole difference between a config key and
    a hardcoded list that happens to have the same values."""
    default = _rules()
    assert _decide(title="Bank appoints new CEO", rules=default).keep is False

    edited = _rules(prefilter_deny_title_patterns=("sponsors",))
    assert _decide(title="Bank appoints new CEO", rules=edited).keep is True
    assert _decide(title="Bank sponsors regatta", rules=edited).keep is False


def test_an_empty_deny_list_disables_the_pattern_rule() -> None:
    """A deliberate operator choice, not a fallback to the defaults: a config
    surface that silently reinstates a list the operator cleared is worse than
    one that has no list at all."""
    rules = _rules(prefilter_deny_title_patterns=())
    assert _decide(title="Bank appoints new CEO", rules=rules).keep is True


# --- the directional rules ------------------------------------------------


def test_deny_patterns_do_not_apply_to_a_primary_feed() -> None:
    """NTS_099 §1, explicitly. A regulator "appointing" a board is the
    composition of an organ; a tax authority publishing an "outlook" is a
    fiscal projection. Applying the news deny-list to a regulator's own feed
    drops exactly the class of item v3 exists to read."""
    for title in ("FINMA appoints new board of directors", "HMRC tax outlook 2027"):
        assert _decide(title=title, source_role="news").keep is False
        assert _decide(title=title, source_role="primary_feed").keep is True
        assert _decide(title=title, source_role="primary_site").keep is True


def test_matching_is_case_insensitive() -> None:
    assert _decide(title="BANK APPOINTS NEW CEO").keep is False
    assert _decide(title="Bank Appoints New CEO").keep is False


def test_age_limit_differs_by_source_role() -> None:
    """72 h news / 240 h primary (NTS_099 §1). A consultation paper is still
    worth writing about a week later; a news item about it is not."""
    five_days_old = NOW - timedelta(hours=120)
    assert _decide(published_at=five_days_old, source_role="news").keep is False
    assert (
        _decide(published_at=five_days_old, source_role="primary_feed").keep is True
    )

    eleven_days_old = NOW - timedelta(hours=264)
    assert (
        _decide(published_at=eleven_days_old, source_role="primary_feed").keep
        is False
    )


def test_an_item_with_no_publication_date_is_kept() -> None:
    """Plenty of regulator feeds omit pubDate. Dropping those would remove a
    whole source class on a formatting detail."""
    assert _decide(published_at=None, source_role="primary_feed").keep is True
    assert _decide(published_at=None, source_role="news").keep is True


def test_a_naive_timestamp_is_read_as_utc_not_dropped() -> None:
    """feedparser hands back naive datetimes for some feeds. Comparing a naive
    to an aware datetime raises, and a raising prefilter would take the run
    down mid-source."""
    naive_recent = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert _decide(published_at=naive_recent).keep is True
    naive_old = (NOW - timedelta(hours=200)).replace(tzinfo=None)
    assert _decide(published_at=naive_old, source_role="news").keep is False


# --- summary and language -------------------------------------------------


def test_summary_required_and_length_enforced() -> None:
    assert _decide(summary=None).reason == "no_summary"
    assert _decide(summary="").reason == "no_summary"
    assert _decide(summary="Short.").reason == "summary_too_short"
    assert _decide(summary="x" * 80).keep is True


def test_require_summary_off_lets_a_bare_headline_through() -> None:
    rules = _rules(prefilter_require_summary=False)
    assert _decide(summary=None, rules=rules).keep is True
    # But a present-and-too-short summary is still short: the switch governs
    # whether a summary is REQUIRED, not whether the length rule exists.
    assert _decide(summary="Short.", rules=rules).reason == "summary_too_short"


def test_the_summary_thresholds_do_not_apply_to_a_primary_feed() -> None:
    """Shadow-week finding 2 (run #125). BaFin's feed titles its items and
    summarises them in 13-60 characters, so ``prefilter_min_summary_chars=80``
    dropped the whole source before the guard ever saw it —
    ``intake.prefilter_drop / summary_too_short``.

    Same reasoning as the deny-list rule one section up (NTS_099 §1): the
    length threshold is a proxy for "a news item with nothing in it", and on a
    regulator's own feed a bare headline is a published document, not an empty
    story. The guard, not the character count, decides whether it has a
    consequence. Both keys stay in the config and both keep applying to news —
    the exemption is by ``source_role``, not by lowering the bar for everyone.
    """
    bafin = "BaFin: Verbraucherhinweis"  # 25 chars, shorter than the 80 floor
    assert len(bafin) < 80

    assert _decide(summary=bafin, source_role="news").reason == "summary_too_short"
    assert _decide(summary=bafin, source_role="primary_feed").keep is True
    assert _decide(summary=bafin, source_role="primary_site").keep is True

    # And a primary feed with no annotation at all is still judged, not dropped.
    assert _decide(summary=None, source_role="news").reason == "no_summary"
    assert _decide(summary=None, source_role="primary_feed").keep is True


def test_the_news_thresholds_are_untouched_by_the_primary_feed_exemption() -> None:
    """The exemption must not become a global loosening: the numbers Andriy
    set for news feeds are the same numbers after the hotfix."""
    assert _decide(summary="x" * 79, source_role="news").reason == "summary_too_short"
    assert _decide(summary="x" * 80, source_role="news").keep is True
    tightened = _rules(prefilter_min_summary_chars=200)
    assert _decide(summary="x" * 120, source_role="news", rules=tightened).keep is False
    assert _decide(summary="x" * 120, source_role="primary_feed", rules=tightened).keep


def test_language_filter_only_fires_when_the_language_is_known() -> None:
    assert _decide(source_language="fr").reason == "language"
    assert _decide(source_language="de").keep is True
    # An unclassified source must not be filtered on a field nobody filled in.
    assert _decide(source_language=None).keep is True
    assert _decide(source_language="").keep is True


def test_every_drop_carries_a_documented_reason() -> None:
    """A drop with an unnamed reason is invisible in the funnel."""
    cases = [
        {"title": "Bank appoints new CEO"},
        {"summary": None},
        {"summary": "Short."},
        {"published_at": NOW - timedelta(hours=200)},
        {"source_language": "fr"},
    ]
    for kwargs in cases:
        decision = _decide(**kwargs)
        assert decision.keep is False, kwargs
        assert decision.reason in PREFILTER_DROP_REASONS, decision


# --- the metric -----------------------------------------------------------


def test_drop_rate_is_a_ratio_and_zero_on_an_empty_run() -> None:
    assert drop_rate(considered=100, dropped=70) == pytest.approx(0.7)
    assert drop_rate(considered=0, dropped=0) == 0.0
    # Negative denominators cannot happen, but a divide-by-zero in the daily
    # summary would take the heartbeat down, which is the one message that must
    # always send.
    assert drop_rate(considered=-5, dropped=3) == 0.0


def test_alarm_band_is_below_030_or_above_095_and_suppressed_when_tiny() -> None:
    """NTS_099 §1: below 0.3 the prefilter is not filtering, above 0.95 it is
    eating the feed. Both are alarming; a five-item run is neither."""
    assert is_drop_rate_alarming(0.10, considered=100) is True
    assert is_drop_rate_alarming(0.98, considered=100) is True
    assert is_drop_rate_alarming(0.70, considered=100) is False
    assert is_drop_rate_alarming(0.10, considered=5) is False
