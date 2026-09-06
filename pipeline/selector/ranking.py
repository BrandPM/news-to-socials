"""The rank formula and the greedy pick that uses it (NTS_100 §1-§2).

NTS_105 §2 threw out "по совокупности" — a phrase that describes no ordering
and cannot be wrong — and NTS_100 replaced it with seven weighted terms. This
module is that formula and nothing else: pure functions over plain data, no
session, no config lookup, no clock of its own. The reasons are practical.

**Every term is logged, per candidate.** "Порядок объясним по логам" is a DoD
item, not a nicety: the weights were, in the spec's own words, "подобраны на
глаз", and the only way to correct them is to read why candidate B beat
candidate A on a day when the editor disagreed. A single float in a log line
cannot answer that; :class:`RankedCandidate` carries the whole breakdown and
the sum is asserted to equal it.

**Diversity is a penalty, not a rule.** Two adopted directives in the same
service category on the same week is worse than one — but a strong second story
must still win over a weak third one taken purely to make the week look varied.
So the two subtractive terms scale with how many the week already holds, the
selection is greedy, and the penalties are recomputed after every pick rather
than precomputed once. A filter would have made "не добирай портфель до квоты"
impossible to express.

**Manual promotion sits outside the formula entirely** (NTS_100 §1): a manager
who promoted a candidate has already ranked it, and re-ranking their decision
against a freshness curve would mean the button does nothing on a slow news
week — the exact week it gets pressed.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# The seven weights of NTS_100 §2, in the order the spec writes them. Exported
# because the API validator rejects any key outside this set — a misspelled
# weight that saves cleanly and is never read is the silent-config failure the
# sentinel suite exists to prevent.
RANK_WEIGHT_KEYS: tuple[str, ...] = (
    "w_conf",
    "w_depth",
    "w_fresh",
    "w_juris",
    "w_kind",
    "w_div",
    "w_juris_div",
)

# depth_weight[depth_prior] — NTS_100 §2. An unknown depth scores as ``note``:
# the guard's vocabulary is CHECK-constrained, so reaching the default means
# something upstream is broken and the candidate should not be flattered.
DEPTH_WEIGHT: Mapping[str, float] = {"note": 0.3, "article": 0.6, "deep": 1.0}
_DEPTH_DEFAULT = 0.3

# half_life[event_stage] in days — NTS_100 §2 names four; the remaining stages
# in ``CANDIDATE_EVENT_STAGES`` take the default. A deal is stale in a week, a
# consultation is still live a month later, and one half-life for both would
# either bury consultations or keep dead deals at the top of the board.
HALF_LIFE_DAYS: Mapping[str, float] = {
    "deal_announced": 3.0,
    "deal_closed": 3.0,
    "ruling": 10.0,
    "in_force": 20.0,
    "consultation": 30.0,
}
_HALF_LIFE_DEFAULT = 14.0

# tier_weight[max tier of jurisdictions] — NTS_100 §2, tiers from NTS_115.
TIER_WEIGHT: Mapping[str, float] = {"tier1": 1.0, "tier2": 0.6, "tier3": 0.2}

# input_kind term: a primary document beats a news lead about one, because the
# document is the thing v3 is built to write from (NTS_101).
KIND_WEIGHT: Mapping[str, float] = {"document": 1.0, "news": 0.7}
_KIND_DEFAULT = 0.7


def default_weights() -> dict[str, float]:
    """The NTS_100 §2 starting weights, as a fresh mutable dict."""
    return {
        "w_conf": 0.30,
        "w_depth": 0.25,
        "w_fresh": 0.15,
        "w_juris": 0.15,
        "w_kind": 0.05,
        "w_div": 0.20,
        "w_juris_div": 0.10,
    }


@dataclass(frozen=True)
class RankWeights:
    """The seven weights, resolved once per run from the brand config."""

    w_conf: float = 0.30
    w_depth: float = 0.25
    w_fresh: float = 0.15
    w_juris: float = 0.15
    w_kind: float = 0.05
    w_div: float = 0.20
    w_juris_div: float = 0.10

    @classmethod
    def from_config(cls, config: Any) -> RankWeights:
        """Read ``rank_weights`` off a ``ConfigRecord`` or a raw JSON string.

        Tolerates both because the selector is called from the production run
        (which holds a parsed ``ConfigRecord``) and from tests and the e2e
        walkthrough (which hold whatever the row contains). A missing or
        unreadable value falls back to the spec's starting weights rather than
        to zeros: a rank of 0.0 for every candidate is an ordering too, and a
        silent one.
        """
        raw: Any = getattr(config, "rank_weights", config)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                log.warning("ranking.bad_weights_json")
                raw = {}
        try:
            mapping = dict(raw or {})
        except (TypeError, ValueError):
            mapping = {}
        merged = default_weights()
        for key in RANK_WEIGHT_KEYS:
            if key in mapping:
                try:
                    merged[key] = float(mapping[key])
                except (TypeError, ValueError):
                    log.warning("ranking.bad_weight", key=key)
        return cls(**merged)


@dataclass(frozen=True)
class CandidateFacts:
    """What ranking needs off a candidate row — and nothing more.

    A plain snapshot rather than the ORM object so the formula can be tested,
    and reasoned about, without a database.
    """

    candidate_id: int
    confidence: float | None
    depth_prior: str | None
    event_stage: str | None
    jurisdictions: tuple[str, ...]
    input_kind: str
    service_category: str | None
    created_at: datetime | None
    source_published_at: datetime | None = None
    manual_action: str | None = None

    @property
    def primary_jurisdiction(self) -> str | None:
        """The first listed jurisdiction — the guard writes them most-relevant
        first, and the diversity penalty needs one name, not a set."""
        return self.jurisdictions[0] if self.jurisdictions else None

    def age_days(self, now: datetime) -> float:
        """Age of the *event*, not of the row.

        ``source_published_at`` when the feed gave one, else ``created_at``:
        a candidate created today off a directive published three weeks ago is
        three weeks old, and freshness that measured our own intake date would
        reward a slow parser.
        """
        stamp = self.source_published_at or self.created_at
        if stamp is None:
            return 0.0
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return max(0.0, (now - stamp).total_seconds() / 86400.0)


@dataclass
class RankedCandidate:
    """One candidate's rank with the arithmetic that produced it."""

    candidate_id: int
    rank: float
    terms: dict[str, float] = field(default_factory=dict)

    def as_log(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "rank": round(self.rank, 4),
            **{k: round(v, 4) for k, v in self.terms.items()},
        }


def tier_of(jurisdictions: Sequence[str], tiers: Mapping[str, Any]) -> str:
    """The best (lowest-numbered) tier any of ``jurisdictions`` falls in.

    Unlisted is tier3 by definition (NTS_115 artefact 4), which is also the
    answer for a candidate the guard gave no jurisdiction at all — scoring an
    unknown as tier1 would put every mis-parsed verdict at the top of the
    board.
    """
    codes = {str(code).strip().upper() for code in jurisdictions if code}
    if not codes:
        return "tier3"
    for tier in ("tier1", "tier2"):
        listed = {str(c).strip().upper() for c in (tiers.get(tier) or ())}
        if codes & listed:
            return tier
    return "tier3"


def score_candidate(
    facts: CandidateFacts,
    *,
    weights: RankWeights,
    tiers: Mapping[str, Any],
    now: datetime,
    same_category_taken: int = 0,
    same_jurisdiction_taken: int = 0,
) -> RankedCandidate:
    """The NTS_100 §2 formula, term by term.

    ``same_category_taken`` / ``same_jurisdiction_taken`` are how many the
    *week* already holds — including the picks made earlier in this same run,
    which is why the caller recomputes them between picks.
    """
    confidence = float(facts.confidence or 0.0)
    depth = DEPTH_WEIGHT.get(facts.depth_prior or "", _DEPTH_DEFAULT)
    half_life = HALF_LIFE_DAYS.get(facts.event_stage or "", _HALF_LIFE_DEFAULT)
    freshness = math.exp(-facts.age_days(now) / half_life)
    tier = tier_of(facts.jurisdictions, tiers)
    kind = KIND_WEIGHT.get(facts.input_kind, _KIND_DEFAULT)

    terms = {
        "conf": weights.w_conf * confidence,
        "depth": weights.w_depth * depth,
        "fresh": weights.w_fresh * freshness,
        "juris": weights.w_juris * TIER_WEIGHT[tier],
        "kind": weights.w_kind * kind,
        "div_penalty": -(weights.w_div * float(same_category_taken)),
        "juris_div_penalty": -(
            weights.w_juris_div * float(same_jurisdiction_taken)
        ),
    }
    ranked = RankedCandidate(
        candidate_id=facts.candidate_id,
        rank=sum(terms.values()),
        terms=terms,
    )
    # The inputs, not just the products: reading a log line where ``fresh`` is
    # 0.02 is only useful next to the age that produced it.
    ranked.terms["_age_days"] = round(facts.age_days(now), 2)
    ranked.terms["_tier"] = {"tier1": 1.0, "tier2": 2.0, "tier3": 3.0}[tier]
    return ranked


def rank_all(
    candidates: Sequence[CandidateFacts],
    *,
    weights: RankWeights,
    tiers: Mapping[str, Any],
    now: datetime,
    category_counts: Mapping[str, int] | None = None,
    jurisdiction_counts: Mapping[str, int] | None = None,
) -> list[RankedCandidate]:
    """Score every candidate against a fixed set of week counters."""
    cats = dict(category_counts or {})
    juris = dict(jurisdiction_counts or {})
    return [
        score_candidate(
            facts,
            weights=weights,
            tiers=tiers,
            now=now,
            same_category_taken=cats.get(facts.service_category or "", 0),
            same_jurisdiction_taken=juris.get(facts.primary_jurisdiction or "", 0),
        )
        for facts in candidates
    ]


def select_batch(
    candidates: Sequence[CandidateFacts],
    *,
    weights: RankWeights,
    tiers: Mapping[str, Any],
    now: datetime,
    limit: int,
    category_counts: Mapping[str, int] | None = None,
    jurisdiction_counts: Mapping[str, int] | None = None,
) -> list[RankedCandidate]:
    """Greedy pick of at most ``limit`` candidates, penalties recomputed.

    NTS_100 §2: "Отбор жадный: берём максимум, пересчитываем штрафы, повторяем."
    Promoted candidates (``manual_action='promoted'``) are taken first and
    out of order, and they *do* count towards the diversity penalties of the
    picks that follow them — the manager's choice bypasses the ranking, not the
    week's shape.

    Ties break on the lower candidate id: an arbitrary but stable order beats
    whatever the query happened to return, so the same portfolio ranks the same
    way twice.
    """
    if limit <= 0:
        return []
    cats = dict(category_counts or {})
    juris = dict(jurisdiction_counts or {})
    remaining = list(candidates)
    picked: list[RankedCandidate] = []

    def _take(facts: CandidateFacts, ranked: RankedCandidate) -> None:
        picked.append(ranked)
        remaining.remove(facts)
        if facts.service_category:
            cats[facts.service_category] = cats.get(facts.service_category, 0) + 1
        if facts.primary_jurisdiction:
            juris[facts.primary_jurisdiction] = (
                juris.get(facts.primary_jurisdiction, 0) + 1
            )

    promoted = [c for c in remaining if c.manual_action == "promoted"]
    for facts in sorted(promoted, key=lambda c: c.candidate_id):
        if len(picked) >= limit:
            break
        ranked = score_candidate(
            facts,
            weights=weights,
            tiers=tiers,
            now=now,
            same_category_taken=cats.get(facts.service_category or "", 0),
            same_jurisdiction_taken=juris.get(facts.primary_jurisdiction or "", 0),
        )
        ranked.terms["_promoted"] = 1.0
        log.info("ranking.promoted", **ranked.as_log())
        _take(facts, ranked)

    while remaining and len(picked) < limit:
        scored = [
            (
                score_candidate(
                    facts,
                    weights=weights,
                    tiers=tiers,
                    now=now,
                    same_category_taken=cats.get(facts.service_category or "", 0),
                    same_jurisdiction_taken=juris.get(
                        facts.primary_jurisdiction or "", 0
                    ),
                ),
                facts,
            )
            for facts in remaining
        ]
        # Logged for every candidate on every pass, not only for the winner:
        # the question the log has to answer is why the one that lost, lost.
        for ranked, _facts in scored:
            log.info("ranking.scored", pick=len(picked) + 1, **ranked.as_log())
        best_ranked, best_facts = max(
            scored, key=lambda pair: (pair[0].rank, -pair[0].candidate_id)
        )
        _take(best_facts, best_ranked)
        log.info("ranking.picked", **best_ranked.as_log())

    return picked
