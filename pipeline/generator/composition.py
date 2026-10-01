"""Composition: depth from the material, a plan, blocks, attribution (NTS_102 v2).

The stage NTS_102 opens by saying does not exist. In its words: the decisions
about an article's shape — how many sections, whether a table belongs, what
length the material carries — are taken nowhere; they happen as a side effect
of the prompt. This module is where they are taken, and the order it enforces is the whole point:

    document → fact pack → depth_final → plan → text → polish
        → ATTRIBUTION (one fix cycle) → data blocks → translations

Four things worth stating, because each is a decision that could have gone the
other way and gone quietly wrong:

**Depth is computed from the material, never from the guard's guess.** The
guard read an abstract; ``depth_prior`` ranks candidates and stops there
(NTS_102 v2 §1). ``depth_final`` counts facts that carry a URL *and* a figure
or a date, and pairs of comparable ones. Twelve facts with no comparable pair
is an ``article``, not a ``deep``: without pairs a table is not merely
unnecessary, it is impossible, and a "deep" article with no table is how a
depth threshold quietly proves itself too low.

**The length band is a floor and a hint, never a quota.** NTS_092's rule — not
enough material, write shorter — is older than any target and is restated in
the prompt as outranking it. The ``deep`` band has no ceiling at all: text ends
where the material ends.

**Attribution runs before translation.** NTS_096's reference case is "18 years
of experience, most recently at CS and UBS" becoming "18-year tenure at CS and
UBS": right number, right source, false claim, and every existing defence
passes it. Checked after translation, that error is bought four times. One
automatic fix cycle, and what survives it opens the editor's card rather than
blocking the draft (NTS_096 §C — the check advises until its false-positive
rate is known).

**A block is built or it is not; it is never approximated.** ``keyFigures``,
``statTable`` and ``chart`` come strictly out of the fact pack's own numbers
with their own sources. No comparable group of the required size means no
block, and an article without a table is a normal outcome (NTS_095).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# NTS_102 v2 §1 — a fact only counts towards depth if it carries something
# concrete. "Analysts are concerned" has a URL and says nothing measurable.
_FIGURE_RE = re.compile(r"\d")
_DATE_HINT_RE = re.compile(
    r"\b(20\d{2}|19\d{2})\b|\bQ[1-4]\b|\b(january|february|march|april|may|june|"
    r"july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
# The words that make a date an *effective* date, not just a date.
_EFFECTIVE_RE = re.compile(
    r"entry into force|enters? into force|applies from|effective from|"
    r"in force from|deadline|by \d{1,2} \w+ 20\d{2}|from 1 january",
    re.IGNORECASE,
)

# ``brief`` sits between ``note`` and ``article`` (NTS_129 P2 Ф2): the band
# for material that is real but thin. Ordered shallowest first.
DEPTHS = ("note", "brief", "article", "deep")

# NTS_102 v2 §1b — a chart needs a real series, not two points and optimism.
CHART_MIN_POINTS = 4
# NTS_095 — a block needs at least two comparable numbers with sources.
BLOCK_MIN_FACTS = 2
# NTS_095 — "один-два блока на статью, не больше".
MAX_BLOCKS = 2

# NTS_102 v2 §1b — pie charts are banned: shares read badly on a phone, and
# every Icon story that would want one is better as a bar.
CHART_TYPES = ("line", "bar")


# --------------------------------------------------------------------------
# 1. depth_final (NTS_102 v2 §1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DepthDecision:
    depth: str
    n_facts: int
    n_pairs: int
    has_dates: bool
    reason: str
    # NTS_131 P3 Ф2 — distinct documents the facts rest on.
    n_sources: int = 1

    def as_log(self) -> dict[str, Any]:
        return {
            "depth_final": self.depth,
            "n_facts": self.n_facts,
            "n_pairs": self.n_pairs,
            "n_sources": self.n_sources,
            "has_dates": self.has_dates,
            "reason": self.reason,
        }


def _countable(fact: Any) -> bool:
    """Does this fact carry a figure or a date? (NTS_102 v2 §1)"""
    text = f"{getattr(fact, 'text', '')} {getattr(fact, 'value', '')}"
    return bool(_FIGURE_RE.search(text) or _DATE_HINT_RE.search(text))


def comparable_groups(facts: Sequence[Any]) -> dict[str, list[Any]]:
    """Facts bucketed by ``comparable_group``, dropping the unlabelled.

    A group is only usable if its members share a unit as well as a label —
    the research model labels the group, but "threshold" covering both a
    percentage and an amount would build a table comparing 12.5 with 5 000 000.
    """
    buckets: dict[str, list[Any]] = {}
    for fact in facts:
        group = (getattr(fact, "comparable_group", "") or "").strip().lower()
        if not group:
            continue
        buckets.setdefault(group, []).append(fact)
    usable: dict[str, list[Any]] = {}
    for group, members in buckets.items():
        by_unit: dict[str, list[Any]] = {}
        for fact in members:
            by_unit.setdefault((getattr(fact, "unit", "") or "").strip().lower(), []).append(fact)
        best = max(by_unit.values(), key=len)
        if len(best) >= BLOCK_MIN_FACTS:
            usable[group] = best
    return usable


def _has_value(fact: Any) -> bool:
    return bool((getattr(fact, "value", "") or "").strip())


def _distinct_sources(facts: Sequence[Any]) -> int:
    urls: set[str] = set()
    for fact in facts:
        url = (getattr(fact, "url", "") or "").strip()
        if url:
            urls.add(url)
        urls.update(u for u in getattr(fact, "corroborated_by", ()) or () if u)
    return len(urls)


def compute_depth_final(
    pack: Any,
    *,
    brief_min_facts: int = 4,
    article_min_facts: int = 10,
    deep_min_facts: int = 11,
    deep_min_pairs: int = 1,
    deep_min_sources: int = 2,
) -> DepthDecision:
    """The depth the *material* supports (NTS_102 v2 §1, NTS_129 P2 Ф2).

    ``deep`` additionally requires two comparable pairs, because ``deep`` is
    the only band that asks for a data block and a block without comparable
    numbers cannot be built. Demoting here rather than discovering it later is
    what stops a "deep" article being padded to 1 200 words with nothing to
    put in the table it promised.

    ``brief`` exists for the same reason one band down. The article floor was
    four facts, and four facts do not support 600-900 words: the observed
    result was a 600-900 target answered with ~300 words that read as if the
    piece had been cut off. The model was choosing between padding and
    stopping and choosing correctly — the target was simply wrong for the
    material. So the article floor is now ``deep_min_facts``-scale (ten), and
    300-400 words is a *shape* the writer aims at rather than a length it
    falls short of.
    """
    facts = list(getattr(pack, "source_facts", []) or []) + list(
        getattr(pack, "context", []) or []
    )
    countable = [f for f in facts if _countable(f)]
    n_facts = len(countable)
    # Pairs are counted on exactly the facts a block can be built from — the
    # ones with a ``value`` (NTS_131 P3 Ф2 §5). Counting them on "a figure or
    # a date" while ``build_data_blocks`` groups only filled values meant
    # ``deep`` could be granted for pairs no block could use.
    groups = comparable_groups([f for f in countable if _has_value(f)])
    n_pairs = sum(len(members) // 2 for members in groups.values())
    n_sources = _distinct_sources(countable)
    has_dates = any(
        _EFFECTIVE_RE.search(getattr(f, "text", "") or "") for f in facts
    )

    def _decide(depth: str, reason: str) -> DepthDecision:
        return DepthDecision(depth, n_facts, n_pairs, has_dates, reason, n_sources)

    if n_facts >= deep_min_facts:
        missing = []
        if n_pairs < deep_min_pairs:
            missing.append(f"{n_pairs} comparable pair(s) with values")
        if n_sources < deep_min_sources:
            missing.append(f"{n_sources} source document(s)")
        if not missing:
            return _decide(
                "deep",
                f"{n_facts} facts from {n_sources} documents, {n_pairs} pair(s)",
            )
        return _decide(
            "article",
            f"{n_facts} facts reach deep but only " + " and ".join(missing),
        )
    if n_facts >= article_min_facts:
        return _decide("article", "enough for an article")
    if n_facts >= brief_min_facts:
        return _decide(
            "brief",
            f"{n_facts} fact(s) — real material, but not {article_min_facts} "
            "for an article",
        )
    return _decide("note", f"only {n_facts} countable fact(s)")


def depth_guidance(
    depth: str, targets: Mapping[str, Any], decision: DepthDecision | None = None
) -> str:
    """The length paragraph the draft prompt is rendered with.

    Written as a floor and an explicit anti-quota sentence rather than as a
    range, because NTS_102 §Риски names the failure precisely: "модель
    воспримет 1200+ как обязательство и начнёт добирать", and the priority of
    NTS_092's shorter-is-correct rule has to be stated, not implied.
    """
    band = targets.get(depth) or targets.get("article") or (600, 900)
    low = int(band[0])
    high = band[1] if len(band) > 1 else None
    structure = {
        "note": "no subheadings, or one",
        # A brief still carries the moves; it carries them in one or two
        # sections instead of four, and drops the ones the pack cannot fill.
        "brief": "1-2 H2 sections",
        "article": "3-4 H2 sections",
        "deep": "5-7 H2 sections; a data block is appropriate if the pack has "
        "comparable numbers",
    }[depth if depth in DEPTHS else "article"]
    if high:
        length = f"around {low}-{int(high)} words"
    else:
        length = (
            f"{low} words and up — there is no upper limit; the piece ends "
            "where the grounded material ends"
        )
    lines = [
        f"TARGET SHAPE: {depth} — {length}, {structure}.",
        "This target is a guide to what the material can support, NOT a quota. "
        "The GROUNDING rule above outranks it: padding to reach a word count is "
        "a failure, and inventing a figure to fill space is the worst outcome "
        "available.",
        # The other half, added in NTS_129 P2 Ф2. Without it this paragraph
        # said "stop early" three ways and "reach the target" none, and it sits
        # exactly where the model reads its length — which is how a 600-900
        # band was answered with 300 words.
        "It is not a licence to stop early either. Before you finish, check "
        "that the five moves are answered: what happened, what it changes, who "
        "is affected and by what threshold, what to do and by when, and what "
        "we still don't know. Coming in far under this target almost always "
        "means one of those is still missing from the text, not from the pack.",
    ]
    if depth in ("article", "deep"):
        # NTS_131 P3 Ф4. Measured on the first live cluster: 38 facts, 18 of
        # them assigned by the plan, and every section came back as ONE
        # paragraph of ~60 words — 344 words against 900-1300. No move was
        # missing; every move was a summary of itself. So the unit that is
        # specified is the section, and what fills it is named: the facts, the
        # mechanism, the consequence — none of which needs a figure the pack
        # does not hold.
        n_sections = 4 if depth == "article" else 6
        per_section = max(120, low // n_sections)
        lines.append(
            f"SECTION SHAPE: each H2 section runs 2-3 paragraphs, roughly "
            f"{per_section}-{per_section + 80} words. In each: (a) every fact the "
            "plan assigns to it, each in its own sentence with its figure or date; "
            "(b) the mechanism — who must do what, from when, under which rule or "
            "article; (c) the consequence for a reader who holds assets, accounts, "
            "structures or residence in that jurisdiction. (b) and (c) are built "
            "from the facts already in the section — they explain them, they do "
            "not add new figures. A section that is one paragraph long on this "
            "material is an unfinished section."
        )
    if depth == "brief":
        lines.append(
            "This is a BRIEF: the material is thin, and this length is the "
            "honest shape for it rather than a short article. Write all five "
            "moves compactly — a brief that drops the gaps and the deadline is "
            "just a summary."
        )
    if decision is not None:
        lines.append(
            f"(Depth was computed from the material: {decision.n_facts} facts "
            f"carrying a figure or date, from {decision.n_sources} source "
            f"document(s), {decision.n_pairs} comparable pair(s).)"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 2. the plan (NTS_102 §"План перед текстом", v2 §3)
# --------------------------------------------------------------------------


_PLAN_INSTRUCTIONS = """\
You are planning an expert commentary for readers who hold structures,
residencies, accounts and deals in the jurisdictions this story touches. You
are NOT writing it.

Return JSON only:
{{"sections": [{{"heading": "<substantive H2, describing actual content>",
                "move": "<happened|changes|affected|todo|unknown>",
                "purpose": "<what this section establishes, one line>",
                "facts": ["<fact text you will use here, copied from the pack>"],
                "document_sections": ["<label of the document section it rests on>"],
                "block": "<keyFigures|statTable|chart|none>"}}],
 "lede": "<the specific consequence the opening two sentences state>",
 "close": "<the concrete shift the final paragraph names, anchored to a fact>",
 "unknowns": ["<a question this material leaves open, named specifically>"],
 "omitted": ["<material you are deliberately leaving out, and why>"]}}

THE FIVE MOVES — plan the article as these, in this order:
1. happened  — the act/decision/filing itself: issuer, instrument, date.
2. changes   — the delta against the position before it: old rule vs new,
               old threshold vs new.
3. affected  — WHO, by the actual test: asset value, turnover, ownership
               percentage, residency days, entity type, jurisdiction list.
               "Large holders" is not a threshold; "above EUR 5 million" is.
4. todo      — the concrete next step and the DATE that binds it: filing
               date, entry into force, end of transition, consultation close.
5. unknown   — what the material leaves open. Goes in ``unknowns``, not in a
               section, unless the material is rich enough to earn its own.

FACT BUDGET — {fact_budget}
The writer writes ONLY the facts this plan assigns, so a plan that assigns
six facts produces a 300-word article however rich the pack is. Assign every
concrete fact the reader needs — thresholds, amounts, dates, deadlines, named
instruments, who must do what — copying each fact's text from the pack.
Facts corroborated by several documents and facts behind a DISCREPANCY are
the first to assign; a discrepancy is planned as a sentence that names both
sides.

RULES
* Plan only what the material supports. {shape}
* Every section must be able to name at least one concrete fact from the pack
  or the document. A section you cannot fill is a section you must not plan —
  put the question it raises in ``unknowns`` instead.
* Moves 3 and 4 are the ones the reader came for. If the pack holds a
  threshold or a deadline, there MUST be a section carrying it. Leaving a
  threshold unassigned is the most expensive mistake available here: the
  writer only writes what this plan assigns.
* Two moves may share one section, and a move with nothing behind it is
  dropped — but say so in ``unknowns`` rather than silently omitting it.
* ``block`` is "none" unless the pack holds at least two comparable numbers
  that belong in that section — a table of one number is not a table.
* Headings describe content ("The repricing of mezzanine credit"), never
  position ("What this means", "Key takeaways").
* ``omitted`` is not optional: naming what you are leaving out is how the
  editor sees the article was chosen rather than exhausted.
"""

_PLAN_INPUT = """\
STORY
Title: {title}
Summary: {summary}

{document}

FACT PACK
{fact_pack}
"""


@dataclass
class Plan:
    """The article's shape, before a word of it is written."""

    sections: list[dict[str, Any]] = field(default_factory=list)
    lede: str = ""
    close: str = ""
    # NTS_129 P2 F2 — move 5. Carried on the plan rather than left to the
    # writer to invent, because a named gap has to come from the material: a
    # model asked at drafting time to say what it does not know will produce
    # something plausible-sounding instead of something true.
    unknowns: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    raw: str = ""

    def is_empty(self) -> bool:
        return not self.sections

    def assigned_facts(self) -> int:
        """Distinct facts the plan hands the writer (NTS_131 P3 Ф4)."""
        seen: set[str] = set()
        for section in self.sections:
            for fact in section.get("facts", []) or []:
                key = str(fact).strip().lower()
                if key:
                    seen.add(key)
        return len(seen)

    def render(self) -> str:
        """The plan as the draft prompt sees it."""
        if self.is_empty():
            return (
                "  (NO PLAN — write from the fact pack directly, keeping every "
                "grounding rule above.)"
            )
        lines: list[str] = []
        if self.lede:
            lines.append(f"LEDE: {self.lede}")
        for index, section in enumerate(self.sections, start=1):
            move = section.get("move")
            suffix = f"   [move: {move}]" if move else ""
            lines.append(f"{index}. ## {section.get('heading', '')}{suffix}")
            if section.get("purpose"):
                lines.append(f"   purpose: {section['purpose']}")
            for fact in section.get("facts", [])[:8]:
                lines.append(f"   - {fact}")
            if section.get("document_sections"):
                lines.append(
                    f"   from document: {', '.join(section['document_sections'][:4])}"
                )
            if section.get("block") and section["block"] != "none":
                lines.append(f"   data block: {section['block']}")
        if self.close:
            lines.append(f"CLOSE: {self.close}")
        if self.unknowns:
            lines.append(
                "WHAT WE DON'T KNOW (move 5 — write these as the closing "
                "paragraphs): " + "; ".join(self.unknowns[:4])
            )
        if self.omitted:
            lines.append("DELIBERATELY OMITTED: " + "; ".join(self.omitted[:4]))
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sections": self.sections,
            "lede": self.lede,
            "close": self.close,
            "unknowns": self.unknowns,
            "omitted": self.omitted,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Plan:
        if not raw:
            return cls()
        sections = [s for s in list(raw.get("sections") or []) if isinstance(s, dict)]
        return cls(
            sections=sections,
            lede=str(raw.get("lede") or ""),
            close=str(raw.get("close") or ""),
            unknowns=[str(x) for x in list(raw.get("unknowns") or [])],
            omitted=[str(x) for x in list(raw.get("omitted") or [])],
        )


async def build_plan(
    *,
    title: str,
    summary: str,
    fact_pack: Any,
    document_text: str = "",
    document_url: str = "",
    depth: str = "article",
    targets: Mapping[str, Any] | None = None,
    model: str = "gpt-4o-mini",
    client: Any = None,
) -> Plan:
    """Plan the article. Never raises — a missing plan is a thinner article.

    NTS_102 §Риски worries the plan is "лишний вызов модели, если он не
    улучшает текст". It is a cheap-model call and its output is stored, so the
    question is answerable from data rather than from opinion: the plan sits in
    ``fact_packs.plan`` next to the text it produced.
    """
    from ..admin.cost_recorder import record_cost
    from ..common.pricing import openai_cost

    if client is None:
        from openai import AsyncOpenAI

        from ..common.config import get_settings

        api_key = get_settings().openai_api_key
        if not api_key:
            log.warning("composition.plan_skipped", reason="no_api_key")
            return Plan()
        client = AsyncOpenAI(api_key=api_key)

    document_block = (
        f"PRIMARY DOCUMENT ({document_url})\n{document_text[:20000]}"
        if document_text
        else "PRIMARY DOCUMENT: none available."
    )
    shape = depth_guidance(depth, dict(targets or {}))
    n_pack = int(getattr(fact_pack, "fact_count", 0) or 0)
    low, high = plan_fact_budget(depth, n_pack)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": _PLAN_INSTRUCTIONS.format(
                shape=shape,
                fact_budget=(
                    f"assign {low}-{high} distinct facts across the sections, "
                    "3-6 per section"
                    if low
                    else "assign every concrete fact the pack holds"
                ),
            ),
        },
        {
            "role": "user",
            "content": _PLAN_INPUT.format(
                title=title,
                summary=(summary or "")[:1000],
                document=document_block,
                fact_pack=(fact_pack.render() if fact_pack is not None else "(none)"),
            ),
        },
    ]
    plan = await _plan_call(client, model=model, messages=messages)
    if plan is None:
        return Plan()
    # NTS_131 P3 Ф4 — the collapse was here: the first live cluster had 38
    # facts and the plan assigned 6. One re-ask, with the count, when the
    # plan under-spends a pack that could fill it.
    assigned = plan.assigned_facts()
    if low and assigned < low:
        log.info("composition.plan_thin", assigned=assigned, budget=low, pack=n_pack)
        messages += [
            {"role": "assistant", "content": plan.raw},
            {
                "role": "user",
                "content": (
                    f"This plan assigns {assigned} facts; the pack holds {n_pack} "
                    f"and the budget is {low}-{high}. Re-plan with at least {low} "
                    "distinct facts copied from the pack, 3-6 per section, "
                    "covering the thresholds, dates and obligations first. Same "
                    "JSON shape."
                ),
            },
        ]
        replanned = await _plan_call(client, model=model, messages=messages)
        if replanned is not None and replanned.assigned_facts() > assigned:
            plan = replanned
    log.info(
        "composition.plan",
        sections=len(plan.sections),
        assigned_facts=plan.assigned_facts(),
        blocks=[s.get("block") for s in plan.sections if s.get("block", "none") != "none"],
        omitted=len(plan.omitted),
    )
    return plan


def plan_fact_budget(depth: str, n_pack: int) -> tuple[int, int]:
    """How many facts the plan should assign, capped by what the pack holds."""
    band = {"note": (0, 0), "brief": (5, 8), "article": (12, 16), "deep": (16, 22)}
    low, high = band.get(depth, (12, 16))
    if not low:
        return 0, 0
    return min(low, n_pack), min(high, n_pack)


async def _plan_call(
    client: Any, *, model: str, messages: list[dict[str, str]]
) -> Plan | None:
    from ..admin.cost_recorder import record_cost
    from ..common.pricing import openai_cost

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        log.warning("composition.plan_failed", err=str(exc)[:200])
        return None

    usage = getattr(resp, "usage", None)
    if usage is not None:
        record_cost(
            provider="openai",
            operation="plan",
            model=model,
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            cost_usd=openai_cost(
                model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            ),
        )
    raw = ""
    try:
        raw = resp.choices[0].message.content or ""
        plan = Plan.from_dict(json.loads(raw))
    except Exception:
        log.warning("composition.plan_unparseable", raw=raw[:200])
        return None
    plan.raw = raw
    return plan


# --------------------------------------------------------------------------
# 3. attribution (NTS_096 part C, NTS_108 §1, NTS_102 v2 §2)
# --------------------------------------------------------------------------


_ATTRIBUTION_INSTRUCTIONS = """\
You check whether an article's factual claims are what its sources actually
say. You are not judging style, structure or interest.

Go sentence by sentence. EVERY sentence that states a fact — a number, date,
threshold, named entity, quotation, a statement about who did what, or what
someone must or may do — gets a verdict; expect about one claim per sentence,
and a sentence with two facts is two claims. Check the RELATION as well as the
figure: which party a transaction involved, against whom a proceeding runs,
whether something happened or must happen. Return one verdict per claim:

* "confirmed" — the source says this, including the SUBJECT and the RELATION,
  not merely the number.
* "distorted" — the number or name is right but the claim is not what the
  source says. THIS IS THE ONE THAT MATTERS. Worked example: the source says
  "18 years of private banking experience, most recently at Credit Suisse and
  UBS"; the article says "18-year tenure at Credit Suisse and UBS". The figure
  matches, the source matches, and the claim is false. Comparing digits would
  call that confirmed. Read the subject-predicate-object, not the digits.
* "uncovered" — the claim is not in any source given to you. Not necessarily
  wrong; not supported.

Two kinds of sentence need care (NTS_131 P3):
* A NAMED GAP — "the notice does not say whether…", "FINMA has not yet
  published…", "it is not known when…" — is a claim ABOUT the sources. It is
  "confirmed" when the sources indeed do not state that thing, and
  "distorted" when they do state it. It is never "uncovered" merely for
  expressing uncertainty.
* A DATE or TENSE is part of the claim. "Must write down by the end of 2023"
  when the source says the loans "were fully written down by the end of 2023"
  is distorted: an obligation where the source reports a completed fact.
Interpretation that asserts no fact of its own ("this raises the cost of the
route") is not a claim; skip it. Anything stated as fact — a consequence, an
obligation, who did what — is a claim and gets a verdict.

Also flag, on the claims where they apply:
* "person_detail": the sentence gives a named individual's tenure, past
  employers or personal history. Permitted only for the author or signatory of
  an official document, a party to a published ruling, or an executive
  announcing a policy (NTS_108 §5).
* "quote_too_long": a verbatim run of more than {max_quote_words} words from
  the professional-commentary or press-release material below.

Return JSON only:
{{"claims": [{{"claim": "<the sentence or clause, quoted from the article>",
              "verdict": "confirmed|distorted|uncovered",
              "why": "<one line; for distorted, what the source actually says>",
              "flags": ["person_detail"|"quote_too_long"]}}]}}

Return every claim you checked, confirmed ones included: the proportions are
the measurement this check exists to produce.
"""

_ATTRIBUTION_INPUT = """\
ARTICLE (English canon)
{body}

PRIMARY DOCUMENT
{document}

FACT PACK
{fact_pack}
"""


@dataclass
class Claim:
    claim: str
    verdict: str
    why: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class AttributionReport:
    """Per-claim verdicts, and what the pipeline does about them."""

    claims: list[Claim] = field(default_factory=list)
    checked: bool = False
    error: str = ""

    @property
    def distorted(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict == "distorted"]

    @property
    def uncovered(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict == "uncovered"]

    @property
    def flagged(self) -> list[Claim]:
        return [c for c in self.claims if c.flags]

    @property
    def needs_fix(self) -> bool:
        """A repair cycle is warranted (NTS_102 v2 §2, NTS_131 P3 Ф1).

        Uncovered claims count now. Before NTS_131 they were only reported,
        and "reported" meant an unsupported sentence reached the editor with a
        badge on it. The rule is that an unsupported claim does not reach the
        text at all: it is rewritten onto a fact the sources hold, or it moves
        to what we don't know.
        """
        return bool(self.distorted or self.uncovered or self.flagged)

    @property
    def clean(self) -> bool:
        """Checked, and nothing left to repair."""
        return self.checked and not self.needs_fix

    def counts(self) -> dict[str, int]:
        out = {"confirmed": 0, "distorted": 0, "uncovered": 0}
        for claim in self.claims:
            if claim.verdict in out:
                out[claim.verdict] += 1
        out["person_detail"] = sum(1 for c in self.claims if "person_detail" in c.flags)
        out["quote_too_long"] = sum(
            1 for c in self.claims if "quote_too_long" in c.flags
        )
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "error": self.error,
            "counts": self.counts(),
            "claims": [
                {
                    "claim": c.claim,
                    "verdict": c.verdict,
                    "why": c.why,
                    "flags": c.flags,
                }
                for c in self.claims
            ],
        }

    def fix_instructions(self) -> str:
        """What the repair pass is told to fix, claim by claim."""
        lines = [
            "ATTRIBUTION REPAIR (mandatory, highest priority). The following "
            "statements are not what the sources say, or are not in the "
            "sources at all. Do NOT add facts from memory, do NOT reach for "
            "other numbers, and do not shorten the rest of the piece to "
            "compensate.",
        ]
        for claim in self.distorted:
            lines.append(
                f'* DISTORTED: "{claim.claim}" — {claim.why or "not what the source says"}. '
                "Rewrite it to say exactly what the source says."
            )
        for claim in self.uncovered:
            lines.append(
                f'* UNCOVERED: "{claim.claim}" — {claim.why or "no source holds this"}. '
                "Either rewrite it onto a fact the SOURCE MATERIAL below "
                "actually holds (same point, sourced wording), or, if no source "
                "holds anything like it, turn it into an open question in the "
                "closing what-we-don't-know paragraphs (\"the notice does not "
                "say whether …\"). It may not stay as an assertion."
            )
        for claim in self.flagged:
            if "person_detail" in claim.flags:
                lines.append(
                    f'* "{claim.claim}" — remove the personal detail about a named '
                    "individual (tenure, previous employers, biography). Their "
                    "role in the document may stay."
                )
            if "quote_too_long" in claim.flags:
                lines.append(
                    f'* "{claim.claim}" — the verbatim quote is too long for this '
                    "source's licence. Paraphrase it, keeping the attribution."
                )
        return "\n".join(lines)


def quote_ceiling(
    license_class: str | None, limits: Mapping[str, int] | None
) -> int:
    """``max_quote_words`` for a licence class (NTS_108 §1).

    Zero means "do not reproduce the text at all" (``news_paywalled``: the
    headline is a lead, the body is not ours to use). A class the config does
    not mention has no ceiling of its own — an official act may be quoted at
    length with attribution, which is the legal argument the whole v3 sourcing
    model rests on.
    """
    if not license_class:
        return 0
    mapping = dict(limits or {})
    if license_class not in mapping:
        return 10_000
    return int(mapping[license_class])


# ~60 000 characters is ~15 000 tokens of document, which with the pack and
# the article keeps one check under a 30 000 TPM ceiling (NTS_131 P3: the
# Greek cluster asked for 36 000 and was refused).
ATTRIBUTION_DOC_CHARS = 45000


async def _attribution_call(
    client: Any,
    model: str,
    ceiling: int,
    body: str,
    fact_pack: Any,
    document_text: str,
    *,
    doc_chars: int,
) -> Any:
    return await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": _ATTRIBUTION_INSTRUCTIONS.format(max_quote_words=ceiling),
            },
            {
                "role": "user",
                "content": _ATTRIBUTION_INPUT.format(
                    body=body[:24000],
                    # A whole event cluster, not one press release (NTS_131
                    # P3 Ф2): a claim from the fourth document must be
                    # checkable against it.
                    document=(document_text or "(no primary document)")[:doc_chars],
                    fact_pack=(fact_pack.render() if fact_pack is not None else "(none)"),
                ),
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )


ATTRIBUTION_CHUNK_WORDS = 450


def split_for_attribution(body: str, max_words: int = ATTRIBUTION_CHUNK_WORDS) -> list[str]:
    """The article in pieces of whole H2 sections, about ``max_words`` each.

    The title and lede ride with the first piece. A single section longer than
    the limit stays whole: splitting inside a section would separate a claim
    from the sentence that qualifies it.
    """
    parts = re.split(r"(?m)^(?=## )", body or "")
    chunks: list[str] = []
    current = ""
    for part in parts:
        if current and len((current + part).split()) > max_words:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    return chunks or [body]


async def check_attribution(
    *,
    body: str,
    fact_pack: Any,
    document_text: str = "",
    license_class: str | None = None,
    max_quote_words: Mapping[str, int] | None = None,
    model: str = "gpt-4o-mini",
    client: Any = None,
) -> AttributionReport:
    """Verdicts for every factual claim in ``body`` (NTS_096 §C).

    Never raises and never blocks: an unavailable check yields an unchecked
    report, and the article ships as it would have before this stage existed.
    NTS_096 is explicit that the check advises until its false-positive rate is
    known — a check that stopped the pipeline for its own bugs would be
    switched off within a week and then never switched back on.
    """
    from ..admin.cost_recorder import record_cost
    from ..common.pricing import openai_cost

    if client is None:
        from openai import AsyncOpenAI

        from ..common.config import get_settings

        api_key = get_settings().openai_api_key
        if not api_key:
            return AttributionReport(error="no OPENAI_API_KEY")
        client = AsyncOpenAI(api_key=api_key)

    ceiling = quote_ceiling(license_class, max_quote_words)
    chunks = split_for_attribution(body)
    if len(chunks) > 1:
        # NTS_131 P3 — one call over a 1 100-word article returned ~18
        # verdicts for ~60 factual sentences, and two distortions were among
        # the unchecked. A section at a time, every sentence is looked at.
        reports = [
            await check_attribution(
                body=chunk,
                fact_pack=fact_pack,
                document_text=document_text,
                license_class=license_class,
                max_quote_words=max_quote_words,
                model=model,
                client=client,
            )
            for chunk in chunks
        ]
        failed = [r for r in reports if not r.checked]
        merged = AttributionReport(
            claims=[c for r in reports for c in r.claims],
            checked=not failed,
            error="; ".join(r.error for r in failed)[:200],
        )
        log.info("composition.attribution_total", chunks=len(chunks), **merged.counts())
        return merged
    resp = None
    for attempt in range(3):
        try:
            resp = await _attribution_call(
                client, model, ceiling, body, fact_pack, document_text,
                doc_chars=ATTRIBUTION_DOC_CHARS // (attempt + 1),
            )
            break
        except Exception as exc:
            # A rate limit is the one failure worth waiting out: the cluster
            # is large, the per-minute token ceiling is shared with the plan
            # and the draft, and a minute later the same call fits. Each retry
            # also halves the document share, so a request that is simply too
            # large for the ceiling shrinks until it is not.
            if "429" in str(exc) and attempt < 2:
                log.info("composition.attribution_rate_limited", attempt=attempt)
                await asyncio.sleep(65)
                continue
            log.warning("composition.attribution_failed", err=str(exc)[:200])
            return AttributionReport(error=f"{type(exc).__name__}: {exc}"[:200])
    assert resp is not None

    usage = getattr(resp, "usage", None)
    if usage is not None:
        record_cost(
            provider="openai",
            operation="attribution",
            model=model,
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            cost_usd=openai_cost(
                model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            ),
        )

    raw = ""
    try:
        raw = resp.choices[0].message.content or ""
        parsed = json.loads(raw)
    except Exception:
        log.warning("composition.attribution_unparseable", raw=raw[:200])
        return AttributionReport(error="unparseable answer")

    claims: list[Claim] = []
    for entry in list(parsed.get("claims") or []):
        if not isinstance(entry, dict):
            continue
        verdict = str(entry.get("verdict", "")).strip().lower()
        if verdict not in ("confirmed", "distorted", "uncovered"):
            continue
        claims.append(
            Claim(
                claim=str(entry.get("claim", ""))[:400],
                verdict=verdict,
                why=str(entry.get("why", ""))[:300],
                flags=[
                    str(f)
                    for f in list(entry.get("flags") or [])
                    if str(f) in ("person_detail", "quote_too_long")
                ],
            )
        )
    report = AttributionReport(claims=claims, checked=True)
    log.info("composition.attribution", **report.counts())
    return report


# --------------------------------------------------------------------------
# 4. data blocks (NTS_095 + the chart type of NTS_102 v2 §1b)
# --------------------------------------------------------------------------


@dataclass
class DataBlock:
    """One structured block, ready for the Sanity body once S8 lands."""

    type: str  # keyFigures | statTable | chart
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"_type": self.type, **self.payload}


def _label_of(fact: Any) -> str:
    """A short caption for a figure (NTS_131 P3 Ф2 §6).

    Research's 2-5 word ``label`` when it wrote one. Otherwise the fact's own
    words, cut at the first clause and to five words — a whole sentence under
    a 34px number is what made the cards unreadable, and a trimmed clause of
    the source's wording is still the source's wording.
    """
    label = (getattr(fact, "label", "") or "").strip()
    if not label:
        text = (getattr(fact, "text", "") or "").strip()
        clause = re.split(r"[,;:(—–]| - ", text, maxsplit=1)[0]
        label = " ".join(clause.split()[:5]).rstrip(".")
    return _capitalised(label)


def _capitalised(text: str) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:] if text else text


def _series_points(members: Sequence[Any]) -> list[dict[str, Any]]:
    """``[{x, y}]`` from facts that carry a value and something to key it on."""
    points: list[dict[str, Any]] = []
    for fact in members:
        value = (getattr(fact, "value", "") or "").strip()
        if not value:
            continue
        cleaned = value.replace(",", "").replace(" ", "")
        try:
            y: float | int = float(cleaned)
        except ValueError:
            continue
        if y.is_integer():
            y = int(y)
        date = (getattr(fact, "date", "") or "").strip()
        points.append({"x": date or _label_of(fact)[:40], "y": y})
    return points


def build_data_blocks(
    fact_pack: Any,
    *,
    depth: str,
    enabled: bool,
    max_blocks: int = MAX_BLOCKS,
) -> list[DataBlock]:
    """Blocks the fact pack actually supports. Empty is a normal answer.

    NTS_095 is categorical: no two comparable sourced numbers, no block. An
    article without blocks is a normal result; an article with an invented
    table is a defect. So every number here is copied from a fact that carries
    its own URL, and nothing is computed, converted or rounded.

    ``enabled`` is ``data_blocks_enabled`` (migration 028): the generator runs
    and is tested while the flag is off, and only stops returning an empty list
    once the Sanity schema PR of S8 is merged.
    """
    if not enabled or depth != "deep" or fact_pack is None:
        return []
    facts = list(getattr(fact_pack, "source_facts", []) or []) + list(
        getattr(fact_pack, "context", []) or []
    )
    groups = comparable_groups([f for f in facts if (getattr(f, "value", "") or "")])
    if not groups:
        return []

    blocks: list[DataBlock] = []
    for group, members in sorted(
        groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        if len(blocks) >= max_blocks:
            break
        points = _series_points(members)
        unit = (getattr(members[0], "unit", "") or "").strip()
        sources = [getattr(f, "url", "") for f in members if getattr(f, "url", "")]

        if len(points) >= CHART_MIN_POINTS:
            # Mechanical choice, per NTS_102 v2 §1b: a series keyed on dates is
            # a line, anything else is a bar. No pies, at any point count.
            dated = all(_DATE_HINT_RE.search(str(p["x"])) for p in points)
            blocks.append(
                DataBlock(
                    "chart",
                    {
                        "chartType": "line" if dated else "bar",
                        "series": [{"name": group, "points": points}],
                        "unit": unit,
                        "sourceRefs": sources,
                        "caption": _capitalised(
                            f"{group} ({unit})" if unit else group
                        ),
                    },
                )
            )
            continue
        if len(members) >= BLOCK_MIN_FACTS:
            blocks.append(
                DataBlock(
                    "statTable",
                    {
                        "caption": _capitalised(group),
                        "columns": ["", unit or "value"],
                        "rows": [
                            {
                                "cells": [_label_of(f), getattr(f, "value", "")],
                                "sourceRef": getattr(f, "url", ""),
                            }
                            for f in members
                        ],
                    },
                )
            )
    # keyFigures is the cheap fallback when there is exactly one group of two:
    # two cards read better than a two-row table.
    if len(blocks) == 1 and blocks[0].type == "statTable" and len(
        blocks[0].payload.get("rows", [])
    ) == 2:
        rows = blocks[0].payload["rows"]
        blocks = [
            DataBlock(
                "keyFigures",
                {
                    "figures": [
                        {
                            "value": row["cells"][1],
                            "label": row["cells"][0],
                            "sourceRef": row["sourceRef"],
                        }
                        for row in rows
                    ],
                    "caption": blocks[0].payload.get("caption", ""),
                },
            )
        ]
    log.info(
        "composition.data_blocks",
        depth=depth,
        blocks=[b.type for b in blocks],
        groups=len(groups),
    )
    return blocks


# --------------------------------------------------------------------------
# 5. deterministic post-process (NTS_123 S6)
# --------------------------------------------------------------------------

# Russian, Ukrainian and Polish typography does not use the em-dash the way
# English does. NTS_123 asks for this to be deterministic rather than another
# instruction in a prompt, because a rule the model follows 90% of the time is
# a rule the editor has to check 100% of the time.
# Written as escapes, not as literal characters: a non-breaking space is
# invisible in a diff, and the next person to touch this file would have no way
# of telling it from an ordinary one.
_NBSP = "\u00a0"
_EM_DASH = "\u2014"
_EN_DASH = "\u2013"

_EM_DASH_RULES: dict[str, str] = {
    # RU/UK: the em-dash IS the correct mark between clauses, but it takes a
    # non-breaking space before it, and the model emits the English tight form.
    "ru": f"{_NBSP}{_EM_DASH} ",
    "uk": f"{_NBSP}{_EM_DASH} ",
    # Polish uses the shorter half-dash (polpauza) with ordinary spaces.
    "pl": f" {_EN_DASH} ",
}


def normalise_dashes(text: str, language: str) -> str:
    """Language-correct dash typography, applied after translation.

    English is left alone. The replacement only fires on an em-dash that
    follows a letter, so a numeric range and a leading list dash are left
    exactly as they are.
    """
    rule = _EM_DASH_RULES.get(language)
    if not rule or not text:
        return text
    # A dash between two digits is a numeric range, not a clause break.
    return re.sub(
        rf"(?<=[^\W\d_])[ {_NBSP}]*{_EM_DASH}[ {_NBSP}]*(?=\S)", rule, text
    )


def strip_banned_phrases(text: str, banned: Sequence[str]) -> list[str]:
    """Which banned phrases survived, for the per-language check (NTS_072).

    Reporting rather than rewriting: deleting a phrase mechanically leaves a
    broken sentence, and the caller decides whether to spend a repair pass.
    """
    lowered = (text or "").lower()
    return [phrase for phrase in banned if phrase and phrase.lower() in lowered]


# NTS_131 P3 — the Sanity body is Portable Text built from markdown, and the
# converter has no table: a markdown table reached the Studio as one line of
# pipes. The data blocks of NTS_095 are the structured way to carry a table
# and they are behind their flag, so until then a table the writer produces is
# rewritten, deterministically, as a list with the same cells.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")


def tables_to_lists(markdown: str) -> str:
    out: list[str] = []
    lines = (markdown or "").split("\n")
    i = 0
    while i < len(lines):
        if (
            _TABLE_ROW.match(lines[i])
            and i + 1 < len(lines)
            and _TABLE_RULE.match(lines[i + 1])
        ):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            i += 2
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                head, rest = cells[0], cells[1:]
                tail = "; ".join(
                    f"{header[j + 1].lower()}: {c}" if j + 1 < len(header) and header[j + 1] else c
                    for j, c in enumerate(rest)
                    if c
                )
                out.append(f"- **{head}** — {tail}" if tail else f"- **{head}**")
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)
