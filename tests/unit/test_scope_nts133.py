"""NTS_133 P5 Ф2 — the scope term: private wealth or not."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline.selector.ranking import CandidateFacts, RankWeights, select_batch
from pipeline.selector.scope import off_scope_marker, scope_of

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("category", "title", "expected"),
    [
        ("structuring", "OECD Pillar Two administrative guidance", 1.0),
        ("special", "OFSI imposes GBP 4.73 million penalty on Citibank", 1.0),
        ("ma", "XXXLutz to acquire Porta furniture stores", 0.0),
        ("ma", "SIKA schliesst Übernahme von Akkim ab", 0.0),
        ("ma", "Veritas Capital outbids CVC in £1.65bn Bodycote deal", 0.0),
        ("ma", "Sequoia Financial Buys $2.3 Billion Colorado RIA", 1.0),
        ("ma", "Jyske Bank agrees to buy Danish wealth manager Formuepleje", 1.0),
        ("ma", "Founder sells family-owned machine maker to EQT", 1.0),
        (None, "anything", 0.0),
    ],
)
def test_scope(category, title, expected):
    assert scope_of(category, title=title) == expected


def test_the_marker_names_the_industry_for_the_log():
    assert off_scope_marker("XXXLutz to acquire Porta furniture stores") == "furniture"


def _facts(cid: int, *, scope: float, conf: float = 0.8, promoted: bool = False):
    return CandidateFacts(
        candidate_id=cid,
        confidence=conf,
        depth_prior="article",
        event_stage="adopted",
        jurisdictions=("CH",),
        input_kind="document",
        service_category="ma",
        created_at=NOW,
        manual_action="promoted" if promoted else None,
        scope=scope,
    )


def test_off_scope_is_a_floor_even_on_an_empty_week():
    picks = select_batch(
        [_facts(1, scope=0.0, conf=1.0)],
        weights=RankWeights(),
        tiers={},
        now=NOW,
        limit=2,
    )
    assert picks == []


def test_a_manager_can_still_promote_an_off_scope_story():
    picks = select_batch(
        [_facts(1, scope=0.0, promoted=True), _facts(2, scope=1.0)],
        weights=RankWeights(),
        tiers={},
        now=NOW,
        limit=2,
    )
    assert [p.candidate_id for p in picks] == [1, 2]
