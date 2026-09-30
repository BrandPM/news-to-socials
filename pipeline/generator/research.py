"""Web-research stage: assemble a grounded fact pack before drafting (NTS_092).

Why this exists
---------------
``writer_draft`` used to receive an RSS ``{title}`` plus a two-line
``{summary}`` and nothing else — the source article body is never fetched.
Two rounds of prompt hardening (NTS_067 SPECIFICITY + ``close_lacks_anchor``
retry, NTS_070 NO REPETITION & DENSITY) told the model to make every sentence
specific, and the filler survived both, because there was nothing on the input
to be specific *about*. Input starvation is not an instruction-quality problem;
it is fixed with input.

This module runs ONE web-search-backed call per topic (EN canon, not per
language — same placement as the shared cover image, NTS_069) and returns a
:class:`FactPack`: the numbers/dates/entities of the story itself, 2–4
corroborating facts from other outlets, angle hints for an HNWI readership,
and the citations actually used.

The anti-hallucination contract
-------------------------------
The lesson of NTS_065 (native per-language generation invented a "67% of
clients" statistic) applies directly: an article that is longer and invented
is strictly worse than the short one we have today. So:

* **A fact without a resolvable URL is dropped here, at parse time.** The
  model is never trusted to self-police this — :func:`_clean_facts` enforces
  it in code, before the pack can reach a prompt.
* Malformed JSON, an empty response, or a timeout returns ``None`` rather
  than raising. ``None`` is the *thin* path: the caller drafts from
  title+summary as before and counts the article as thin. Research breaking
  must never drop a topic.

"Resolvable" is checked syntactically (http/https scheme + a real public
host), not by fetching. Fetching every candidate URL would add seconds and a
new failure mode per topic to buy very little: the point of the rule is to
stop the model inventing a citation shape out of thin air, and a reserved or
malformed host is exactly what that looks like. A live 404 behind a
well-formed URL is a weaker failure and stays visible in ``citations`` for the
human review that follows.

Budgets (max sources / token ceiling / timeout) come from ``pipeline_config``
so they are tunable from Settings without a deploy.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..common.logging import get_logger
from ..common.models import Topic

log = get_logger(__name__)


# The research call is a single gpt-4o pass with the web_search tool. gpt-4o
# (not gpt-5.5) is deliberate: this now runs for EVERY topic of EVERY run, so
# it sits on the per-topic cost line next to draft/polish rather than next to
# the judge's yellow-band escalation. One constant, one line to change.
RESEARCH_MODEL = "gpt-4o"

# How many times to ask for the pack before giving up and going thin. The
# model intermittently returns a well-formed object with EMPTY arrays — seen
# once in three live calls during NTS_092 acceptance, on a topic that
# researched fine on the retry. Since the first call is paid either way, a
# second attempt buys back most of that loss for a bounded 2x on the minority
# path; a topic that genuinely has nothing to find comes back empty twice and
# goes thin, which is the correct outcome. This does NOT retry API errors or
# timeouts — those are already handled, and retrying a timeout would blow the
# budget the timeout exists to enforce.
_RESEARCH_ATTEMPTS = 2

# Built-in web-search tool variants, in preference order. Older model
# snapshots only accept ``web_search_preview``; a 400 that names the tool
# retries once with the next variant instead of burning the topic's research.
_WEB_SEARCH_TOOL_TYPES: tuple[str, ...] = ("web_search", "web_search_preview")

# Hosts a model reaches for when it is inventing a citation rather than
# reporting one. RFC 2606 / RFC 6761 reserved names plus loopback.
_RESERVED_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "url.com",
    "source.com",
    "news.com",
}
_RESERVED_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local")

# Keep one fact readable inside a prompt without letting the model paste a
# whole article body into the pack.
_MAX_FACT_CHARS = 400
_MAX_HINT_CHARS = 300
_MAX_ANGLE_HINTS = 6

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchBudget:
    """Per-call limits, sourced from ``pipeline_config`` (no deploy to tune).

    ``max_sources`` caps how many ``context`` entries survive the parse (and
    is stated in the prompt so the model does not go wide for its own sake);
    ``max_tokens`` caps the model's output; ``timeout_seconds`` is a hard
    ceiling on the whole call — a slow search turns into the thin path, not
    into a stalled run.
    """

    max_sources: int = 5
    max_tokens: int = 2000
    timeout_seconds: int = 60

    @classmethod
    def from_config(cls, config: Any) -> ResearchBudget:
        """Build from a ``ConfigRecord``-shaped object, tolerating old rows.

        ``getattr`` with the dataclass defaults, the same pattern the dedup /
        eval tunables use, so a config row that predates the columns (or the
        hardcoded fallback path) still produces a usable budget.
        """
        return cls(
            max_sources=int(getattr(config, "research_max_sources", 5) or 5),
            max_tokens=int(getattr(config, "research_max_tokens", 2000) or 2000),
            timeout_seconds=int(
                getattr(config, "research_timeout_seconds", 60) or 60
            ),
        )


@dataclass(frozen=True)
class Fact:
    """One retained claim. ``url`` is non-empty by construction — a fact
    without a resolvable URL never becomes a ``Fact`` (see
    :func:`_clean_facts`)."""

    text: str
    url: str
    publisher: str = ""
    date: str = ""
    # NTS_102 v2 §1 — the label that makes two facts *comparable*: same unit,
    # different jurisdiction / period / threshold. ``depth_final`` counts pairs
    # of these, and a data block is only built from a group with at least two
    # members (NTS_095). Labelled by the research model because it is the only
    # step that sees the facts side by side; empty means "not comparable to
    # anything", which is the honest default.
    comparable_group: str = ""
    # The number and its unit, when the model could isolate them. Used only for
    # building blocks — the prose always quotes ``text``, never a reassembled
    # figure, because reassembly is where a correct number becomes a wrong claim.
    value: str = ""
    unit: str = ""
    # NTS_131 P3 Ф2 §6 — a 2-5 word caption for the figure. ``text`` is a whole
    # sentence, and a whole sentence under a 34px number is what made the
    # keyFigures cards unreadable. Written by research, the only step that
    # knows what the number measures.
    label: str = ""
    # NTS_131 P3 Ф2 §2 — other cluster documents that state the same fact.
    # A fact two sources agree on is confirmed; the writer may say so.
    corroborated_by: tuple[str, ...] = ()

    def render(self) -> str:
        meta = " — ".join(x for x in (self.publisher, self.date) if x)
        tail = f"{self.url} — {meta}" if meta else self.url
        also = (
            f" [also stated by: {', '.join(self.corroborated_by)}]"
            if self.corroborated_by
            else ""
        )
        return f"{self.text} ({tail}){also}"


@dataclass(frozen=True)
class Discrepancy:
    """Two cluster sources that say different things about one point.

    Kept as its own list rather than as two facts, because two contradicting
    facts side by side read to the writer as two facts — and the article then
    states both as true. A discrepancy is written as a discrepancy:
    "X reports A, Y reports B".
    """

    point: str
    claims: tuple[tuple[str, str, str], ...]  # (publisher, says, url)

    def render(self) -> str:
        sides = "; ".join(
            f"{pub or url} says: {says} ({url})" for pub, says, url in self.claims
        )
        return f"{self.point} — {sides}"


@dataclass(frozen=True)
class FactPack:
    """What the drafter is allowed to be specific about.

    ``source_facts`` are the story's own numbers/dates/entities/mechanism;
    ``context`` is corroboration or background from other outlets;
    ``angle_hints`` is what is non-obvious here for an HNWI readership;
    ``citations`` are the URLs actually used.
    """

    source_facts: list[Fact] = field(default_factory=list)
    context: list[Fact] = field(default_factory=list)
    angle_hints: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    model: str = RESEARCH_MODEL
    searches: int = 0
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def source_urls(self) -> list[str]:
        """Distinct documents the facts rest on — the cluster's breadth."""
        seen: list[str] = []
        for fact in [*self.source_facts, *self.context]:
            for url in (fact.url, *fact.corroborated_by):
                if url and url not in seen:
                    seen.append(url)
        return seen

    @property
    def fact_count(self) -> int:
        return len(self.source_facts) + len(self.context)

    def is_empty(self) -> bool:
        """True when nothing survived the URL rule. Callers treat this exactly
        like ``None`` — the article is thin."""
        return self.fact_count == 0

    def render(self) -> str:
        """The ``{fact_pack}`` block as the draft prompt sees it."""
        lines: list[str] = []
        lines.append(
            "SOURCE FACTS (from this story — each is followed by the URL it "
            "came from):"
        )
        if self.source_facts:
            lines.extend(f"  - {f.render()}" for f in self.source_facts)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("CONTEXT (corroborating / background, other outlets):")
        if self.context:
            lines.extend(f"  - {f.render()}" for f in self.context)
        else:
            lines.append("  (none)")
        if self.discrepancies:
            lines.append("")
            lines.append(
                "DISCREPANCIES (the sources disagree — write each one as a "
                "disagreement, \"X reports A, while Y reports B\"; never pick "
                "one side silently and never state both as fact):"
            )
            lines.extend(f"  - {d.render()}" for d in self.discrepancies)
        if self.angle_hints:
            lines.append("")
            lines.append("ANGLE HINTS (non-obvious for this readership):")
            lines.extend(f"  - {h}" for h in self.angle_hints)
        if self.citations:
            lines.append("")
            lines.append("CITATIONS (the only URLs behind the facts above):")
            lines.extend(f"  - {u}" for u in self.citations)
        return "\n".join(lines)


def fact_pack_from_dict(
    stored: Mapping[str, Any] | None,
) -> FactPack | None:
    """Rebuild a pack from the row ``fact_packs`` stored (NTS_100 §4).

    The inverse of ``pipeline.run._fact_pack_as_dict``, and the reason a
    production retry does not pay for research twice: the pack that the first
    attempt bought is on disk, and research is 59% of the cost of an article
    (NTS_122). Returns ``None`` for a stored pack that recorded an empty result
    — that row is a valuable record of *why* an article was thin and a useless
    input to write from.
    """
    if not stored or stored.get("empty", True):
        return None

    def _facts(items: Any) -> list[Fact]:
        out: list[Fact] = []
        for item in list(items or []):
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or "").strip()
            url = str(item.get("url") or "").strip()
            # Same invariant as ``_clean_facts``: no URL, no fact. A restored
            # pack must not be able to reintroduce a claim the live path would
            # have dropped.
            if not text or not url:
                continue
            # Every field the live path keeps, restored. Before NTS_131 only
            # text/url/publisher/date came back, so a reused pack had lost the
            # values and groups — and a retry could never reach ``deep`` or
            # build a block that the first attempt could have.
            out.append(
                Fact(
                    text=text,
                    url=url,
                    publisher=str(item.get("publisher") or ""),
                    date=str(item.get("date") or ""),
                    comparable_group=str(item.get("comparable_group") or ""),
                    value=str(item.get("value") or ""),
                    unit=str(item.get("unit") or ""),
                    label=str(item.get("label") or ""),
                    corroborated_by=tuple(
                        str(u) for u in list(item.get("corroborated_by") or []) if u
                    ),
                )
            )
        return out

    pack = FactPack(
        source_facts=_facts(stored.get("source_facts")),
        context=_facts(stored.get("context")),
        angle_hints=[str(h) for h in list(stored.get("angle_hints") or [])],
        citations=[str(u) for u in list(stored.get("citations") or [])],
        searches=int(stored.get("searches") or 0),
        discrepancies=_discrepancies(stored.get("discrepancies")),
    )
    return None if pack.is_empty() else pack


# The block rendered into ``{fact_pack}`` when research produced nothing. It
# is not a blank string on purpose: the drafter must be told that the absence
# is real and what to do about it, or 600–800 words of padding is exactly what
# comes back.
NO_FACT_PACK = (
    "  (NO RESEARCH AVAILABLE — the web-research stage returned nothing for\n"
    "  this story. Work from the news peg above and NOTHING else. Do not\n"
    "  supply numbers, dates or names from memory to fill the gap. Write a\n"
    "  SHORTER piece — well under the target length — rather than padding it.)"
)


def render_fact_pack(pack: FactPack | None) -> str:
    """Render ``pack`` for the ``{fact_pack}`` placeholder, thin path included.

    The placeholder is *required* in ``writer_draft`` (NTS_092), so every
    draft call renders something here — a populated pack, or the explicit
    "no research available, write shorter" block above.
    """
    if pack is None or pack.is_empty():
        return NO_FACT_PACK
    return pack.render()


# --------------------------------------------------------------------------
# Parsing / URL discipline
# --------------------------------------------------------------------------


def is_resolvable_url(value: Any) -> bool:
    """True for a syntactically real, public http(s) URL.

    Deliberately strict about the shapes a model produces when it is making a
    citation up (bare words, ``example.com``, ``localhost``) and deliberately
    NOT a network call — see the module docstring.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate:
        return False
    try:
        parsed = urlparse(candidate)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    if not host or "." not in host or host.startswith(".") or host.endswith("."):
        return False
    bare = host[4:] if host.startswith("www.") else host
    if bare in _RESERVED_HOSTS or host in _RESERVED_HOSTS:
        return False
    return not host.endswith(_RESERVED_SUFFIXES)


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


_MAX_LABEL_WORDS = 5


def _short_label(value: Any) -> str:
    """The 2-5 word caption, capitalised; longer answers are cut, not kept.

    Capitalised here rather than in the renderer: the model writes captions in
    lower case about half the time, and every consumer of the label would
    otherwise have to remember to fix it.
    """
    text = _clean_text(value, 60).rstrip(".:;,")
    if not text:
        return ""
    words = text.split()[:_MAX_LABEL_WORDS]
    label = " ".join(words)
    return label[:1].upper() + label[1:]


def _discrepancies(raw: Any) -> list[Discrepancy]:
    """``[{point, claims: [{publisher, says, url}]}]`` → discrepancies.

    A side without a resolvable URL is dropped, and a discrepancy left with
    fewer than two sides is not a discrepancy — the same URL rule as facts.
    """
    out: list[Discrepancy] = []
    if not isinstance(raw, list):
        return out
    for entry in raw[:6]:
        if not isinstance(entry, dict):
            continue
        point = _clean_text(entry.get("point"), 200)
        sides: list[tuple[str, str, str]] = []
        for side in list(entry.get("claims") or [])[:4]:
            if not isinstance(side, dict):
                continue
            url = str(side.get("url") or "").strip()
            says = _clean_text(side.get("says"), 300)
            if says and is_resolvable_url(url):
                sides.append((_clean_text(side.get("publisher"), 80), says, url))
        if point and len(sides) >= 2:
            out.append(Discrepancy(point=point, claims=tuple(sides)))
    return out


def _clean_facts(raw: Any, *, limit: int, bucket: str) -> tuple[list[Fact], int]:
    """Coerce a raw ``[{text,url,publisher,date}, ...]`` list into ``Fact``s.

    Returns ``(facts, dropped)``. Anything without text or without a
    resolvable URL is dropped HERE — this is the rule the whole stage exists
    to enforce, so it lives in code and is counted, not delegated to the
    model's own judgement.
    """
    if not isinstance(raw, list):
        return [], 0
    out: list[Fact] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        text = _clean_text(entry.get("text") or entry.get("fact"), _MAX_FACT_CHARS)
        url = entry.get("url") or entry.get("source_url") or ""
        if not text or not is_resolvable_url(url):
            dropped += 1
            log.debug(
                "research.fact_dropped",
                bucket=bucket,
                reason="no_text" if not text else "unresolvable_url",
                url=str(url)[:200],
            )
            continue
        url = str(url).strip()
        key = (text.lower(), url)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Fact(
                text=text,
                url=url,
                publisher=_clean_text(entry.get("publisher"), 80),
                date=_clean_text(entry.get("date"), 40),
                comparable_group=_clean_text(entry.get("comparable_group"), 60),
                value=_clean_text(entry.get("value"), 40),
                unit=_clean_text(entry.get("unit"), 24),
                label=_short_label(entry.get("label")),
                corroborated_by=tuple(
                    str(u).strip()
                    for u in list(entry.get("corroborated_by") or [])[:4]
                    if is_resolvable_url(u) and str(u).strip() != url
                ),
            )
        )
        if len(out) >= limit:
            break
    return out, dropped


def parse_fact_pack(
    text: str,
    *,
    budget: ResearchBudget,
    model: str = RESEARCH_MODEL,
    searches: int = 0,
    documents: int = 1,
) -> FactPack | None:
    """Parse the model's JSON into a :class:`FactPack`, or ``None``.

    ``None`` on: empty text, non-JSON, JSON that is not an object, or a
    payload where nothing survived the URL rule. Never raises — every failure
    shape here is the thin path.
    """
    if not text or not text.strip():
        log.warning("research.parse_failed", reason="empty_response")
        return None
    stripped = _FENCE.sub("", text.strip()).strip()
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        log.warning(
            "research.parse_failed",
            reason="malformed_json",
            err=str(exc),
            raw=stripped[:200],
        )
        return None
    if not isinstance(data, dict):
        log.warning("research.parse_failed", reason="not_an_object")
        return None

    # A cluster of several documents holds more facts than one press release;
    # capping it at the single-document limit would throw the cluster away
    # again one stage later (NTS_131 P3 Ф2).
    clustered = documents > 1
    source_facts, dropped_src = _clean_facts(
        data.get("source_facts"),
        limit=budget.max_sources * (5 if clustered else 3),
        bucket="source_facts",
    )
    context, dropped_ctx = _clean_facts(
        data.get("context"),
        limit=budget.max_sources * (2 if clustered else 1),
        bucket="context",
    )
    dropped = dropped_src + dropped_ctx

    hints_raw = data.get("angle_hints")
    angle_hints = (
        [
            h
            for h in (
                _clean_text(x, _MAX_HINT_CHARS)
                for x in hints_raw[:_MAX_ANGLE_HINTS]
            )
            if h
        ]
        if isinstance(hints_raw, list)
        else []
    )

    if not source_facts and not context:
        # ``dropped`` separates the two very different causes: >0 means WE
        # rejected everything (no resolvable URL), 0 means the model returned
        # empty arrays. The raw snippet settles it either way without a second
        # paid call to reproduce.
        log.warning(
            "research.parse_failed",
            reason="all_facts_dropped",
            dropped=dropped,
            raw=stripped[:200],
        )
        return None

    # Citations are rebuilt from what survived, then extended with any extra
    # resolvable URL the model listed. A citation for a dropped fact is not a
    # citation — it is the shape of the thing we just refused to trust.
    citations: list[str] = []
    for fact in [*source_facts, *context]:
        if fact.url not in citations:
            citations.append(fact.url)
    raw_citations = data.get("citations")
    if isinstance(raw_citations, list):
        for url in raw_citations:
            candidate = str(url).strip() if isinstance(url, str) else ""
            if is_resolvable_url(candidate) and candidate not in citations:
                citations.append(candidate)

    if dropped:
        log.info(
            "research.facts_dropped",
            dropped=dropped,
            kept=len(source_facts) + len(context),
        )
    return FactPack(
        source_facts=source_facts,
        context=context,
        angle_hints=angle_hints,
        citations=citations,
        model=model,
        searches=searches,
        discrepancies=_discrepancies(data.get("discrepancies")),
    )


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------


_RESEARCH_INSTRUCTIONS = """\
You are a research assistant assembling a FACT PACK for a financial-commentary
writer. You are NOT writing the article. You are collecting verifiable
material it can be specific about.

{document_rule}

Use the web_search tool. Search the story itself first, then look for
corroboration and background from OTHER outlets.

HARD RULES:
* EVERY entry in source_facts and context MUST carry the exact URL of the page
  you actually read it on. An entry without a real URL is discarded and is
  worse than no entry at all.
* Never invent, extrapolate, or "sensibly round" a number, date or name. If
  you could not find a figure, leave it out. A short, true fact pack is the
  correct output when the story is thin.
* Prefer primary sources (regulator, exchange, company release, court filing)
  and named outlets over aggregators.
* Facts must be concrete: amounts, percentages, dates, jurisdictions, named
  entities, thresholds, effective dates, and the actual mechanism of the story.
  "Analysts are concerned" is not a fact.
* Use at most {max_sources} distinct outlets for context.

Return ONLY a JSON object, no prose and no code fence:

{{
  "source_facts": [
    {{"text": "<one concrete fact from this story>",
      "url": "<https URL you read it on>",
      "publisher": "<outlet>", "date": "<YYYY-MM-DD if known, else \\"\\">",
      "value": "<the bare figure, e.g. 5000000 or 12.5 — empty if the fact has none>",
      "unit": "<EUR, %, days, … — empty if none>",
      "comparable_group": "<short label shared by facts that measure the SAME
       thing on a different jurisdiction, period or threshold, e.g.
       'reporting threshold' — empty when this fact is comparable to nothing>",
      "label": "<2-5 word caption for the figure, e.g. 'Maximum OFSI penalty'
       — a name for the number, NOT a sentence; empty if the fact has no value>",
      "corroborated_by": ["<URL of another supplied document stating the same
       fact — empty list if only one document states it>"]}}
  ],
  "context": [
    {{"text": "<corroborating or background fact from another outlet>",
      "url": "<https URL>", "publisher": "<outlet>", "date": "<YYYY-MM-DD>",
      "value": "", "unit": "", "comparable_group": ""}}
  ],
  "angle_hints": [
    "<what is non-obvious here for high-net-worth individuals, family offices \
and their advisers — second-order consequences, who is exposed, what changes \
in practice>"
  ],
  "discrepancies": [
    {{"point": "<what the sources disagree about>",
      "claims": [{{"publisher": "<who>", "says": "<what they say>", "url": "<URL>"}},
                 {{"publisher": "<who>", "says": "<what they say>", "url": "<URL>"}}]}}
  ],
  "citations": ["<every URL you used>"]
}}

{volume_rule}

``comparable_group`` is what lets the article build a table or a chart later,
so label honestly: two thresholds in different jurisdictions share a group,
a threshold and a deadline do not. A wrong group produces a table comparing
things that are not comparable, which is worse than no table.
"""


_RESEARCH_INPUT = """\
Story to research.

Title: {title}
Source URL: {url}
Summary: {summary}
{document_block}{cluster_block}"""

_VOLUME_SINGLE = """\
Aim for 4-8 source_facts and 2-4 context entries when the material supports
it. Return fewer rather than padding with vague statements."""

# NTS_131 P3 Ф2 — the pack is built across the whole event cluster. The volume
# rule is what makes that real: with the single-document wording ("aim for
# 4-8") the model stops at eight facts whether it was given one document or
# five, and the cluster is thrown away inside the call.
_VOLUME_CLUSTER = """\
You were given {n_docs} documents about ONE event. Extract from ALL of them:
aim for 12-20 source_facts in total when the documents hold them — every
threshold, amount, percentage, date, deadline, named instrument and named
party, from whichever document states it. Put each fact under the URL of the
document that states it. When two documents state the same fact, list it ONCE
and put the other document's URL in ``corroborated_by``. When two documents
say DIFFERENT things about the same point (a different date, figure, scope or
reading), do not pick one: record it in ``discrepancies``. Return fewer rather
than padding with vague statements."""

_CLUSTER_BLOCK = """\

--- FURTHER DOCUMENTS ABOUT THE SAME EVENT (read all of them) ---
{docs}
--- END OF FURTHER DOCUMENTS ---
"""

_CLUSTER_DOC = """\
[{n}] {role} — {publisher}
URL: {url}
Title: {title}
Read on: {as_of}

{text}
"""

# NTS_101 §2-7 / NTS_123 S5 — when a primary document was fetched it goes in
# FIRST and web_search is demoted to context. The reason is named in the
# directive: research over a headline alone is the main producer of plausible
# invention, because the model has nothing authoritative to be specific about
# and the instructions still ask it for specifics.
_DOCUMENT_RULE_WITH_DOC = """\
A PRIMARY DOCUMENT is supplied below. It is the authority. Take every figure,
date, threshold and definition from it, quoting its own wording, and cite it by
the document URL. Use web_search ONLY to add what the document does not contain
— reaction, context, prior versions, market detail. If the document and a
search result disagree, the document wins and the disagreement is not a fact.\
"""

_DOCUMENT_RULE_CLUSTER = """\
A PRIMARY DOCUMENT and FURTHER DOCUMENTS about the same event are supplied
below. They are your material; read every one of them before you search. The
primary document is the authority on what the act itself says — its figures,
dates, thresholds and definitions, in its own wording. The further documents
add what it does not say: other regulators' positions, practitioners' reading,
the numbers the press release left out. Cite each fact by the URL of the
document it came from. Use web_search ONLY for what none of the documents
contains. Where a further document contradicts the primary one, record a
discrepancy; do not resolve it yourself.\
"""

_DOCUMENT_RULE_NO_DOC = """\
No primary document was available for this story, so every fact must come from
a page you actually read via web_search.\
"""

_DOCUMENT_BLOCK = """\

--- PRIMARY DOCUMENT (authoritative) ---
URL: {doc_url}
Read on (as_of): {as_of}
Sections included: {sections}

{doc_text}
--- END OF PRIMARY DOCUMENT ---
"""

# Appended on the second attempt only. The first attempt sometimes returns a
# well-formed object with empty arrays; this says plainly that empty is not an
# acceptable shortcut, WITHOUT licensing invention to avoid it.
_RETRY_NUDGE = (
    "Your previous attempt returned nothing usable. Search again and return at "
    "least one source_fact with a real URL you actually read it on. Returning "
    "empty arrays is NOT an acceptable answer while any concrete, sourced fact "
    "about this story exists. If — and only if — you genuinely cannot find one, "
    "return empty arrays rather than inventing anything: a fabricated fact is "
    "worse than an empty pack."
)


def _count_searches(resp: Any) -> int:
    """How many web_search calls the model actually made (for cost + logs)."""
    items = getattr(resp, "output", None)
    if not isinstance(items, list):
        return 0
    return sum(
        1
        for item in items
        if str(getattr(item, "type", "")).startswith("web_search_call")
    )


def _record_research_cost(resp: Any, *, model: str, searches: int) -> None:
    """One ``cost_records`` row per research call, ``operation='research'``.

    Mirrors ``comment_writer._record_openai_cost`` (missing usage → token cost
    0, row still written when a brand context is active) and adds the
    web-search tool-call surcharge, which is billed per call and not in the
    token usage.
    """
    from pipeline.admin.cost_recorder import record_cost  # noqa: PLC0415
    from pipeline.common.pricing import openai_cost, web_search_cost  # noqa: PLC0415

    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "input_tokens", None)
    tokens_out = getattr(usage, "output_tokens", None)
    record_cost(
        provider="openai",
        operation="research",
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(
            openai_cost(model, tokens_in, tokens_out) + web_search_cost(searches), 6
        ),
    )


async def _create_response(
    client: Any, *, model: str, instructions: str, payload: str, budget: ResearchBudget
) -> Any:
    """One Responses API call with the web-search tool.

    Retries once against the older ``web_search_preview`` tool name if the API
    rejects ``web_search`` — a model snapshot that only knows the preview name
    should cost us a retry, not the topic's whole fact pack.
    """
    last_exc: Exception | None = None
    for tool_type in _WEB_SEARCH_TOOL_TYPES:
        try:
            return await client.responses.create(
                model=model,
                instructions=instructions,
                input=payload,
                tools=[{"type": tool_type}],
                max_output_tokens=budget.max_tokens,
                timeout=float(budget.timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            message = str(exc).lower()
            unsupported_tool = "400" in message and (
                "web_search" in message or "tool" in message
            )
            if not unsupported_tool:
                raise
            log.warning(
                "research.tool_type_rejected", tool_type=tool_type, err=str(exc)[:300]
            )
    # Every variant was rejected as unsupported — surface the last error to
    # build_fact_pack, which turns it into the thin path.
    raise last_exc if last_exc is not None else RuntimeError("no web-search tool")


@dataclass(frozen=True)
class PrimaryDocument:
    """The document the research call reads before it searches (NTS_101 §2-7)."""

    url: str
    text: str
    as_of: str = ""
    sections_used: tuple[str, ...] = ()


async def build_fact_pack(
    topic: Topic,
    *,
    budget: ResearchBudget | None = None,
    client: Any = None,
    model: str = RESEARCH_MODEL,
    document: PrimaryDocument | None = None,
    cluster: Sequence[Any] = (),
) -> FactPack | None:
    """Research ``topic`` and return a grounded fact pack.

    Placed between dedup and ``writer_draft``, once per topic (EN canon).
    Up to ``_RESEARCH_ATTEMPTS`` calls: an unusable payload — malformed JSON,
    or a well-formed one with nothing that survives the URL rule — is retried
    once with a nudge. A timeout or an API error is NOT retried.

    ``document`` is the S5 change: when a primary document was fetched it is
    put in front of the model and ``web_search`` is demoted to filling gaps
    (NTS_123 S5, "ресёрч получает документ на вход ПЕРЕД web_search"). Research
    over a headline alone is where the invented specifics came from — the
    instructions ask for figures and dates, and without a document the only
    place to get them is memory.

    Returns ``None`` when every attempt failed. Never raises: the caller
    drafts from title+summary and counts the article thin.
    """
    budget = budget or ResearchBudget()
    if client is None:
        from openai import AsyncOpenAI  # noqa: PLC0415

        from ..common.config import get_settings  # noqa: PLC0415

        api_key = get_settings().openai_api_key
        if not api_key:
            log.warning("research.failed", topic=topic.id, reason="no_api_key")
            return None
        client = AsyncOpenAI(api_key=api_key)

    # ``cluster`` is the event's other documents (NTS_131 P3 Ф2) — objects
    # with url/publisher/role/title/text/as_of, i.e. ``ClusterDocument``.
    cluster = [c for c in cluster if getattr(c, "text", "")]
    n_docs = len(cluster) + (1 if document is not None else 0)
    if cluster and document is not None:
        rule = _DOCUMENT_RULE_CLUSTER
    elif document is not None:
        rule = _DOCUMENT_RULE_WITH_DOC
    else:
        rule = _DOCUMENT_RULE_NO_DOC
    instructions = _RESEARCH_INSTRUCTIONS.format(
        max_sources=budget.max_sources,
        document_rule=rule,
        volume_rule=(
            _VOLUME_CLUSTER.format(n_docs=n_docs) if n_docs > 1 else _VOLUME_SINGLE
        ),
    )
    document_block = ""
    if document is not None:
        document_block = _DOCUMENT_BLOCK.format(
            doc_url=document.url,
            as_of=document.as_of or "unknown",
            sections=", ".join(document.sections_used) or "(whole document)",
            doc_text=document.text,
        )
    cluster_block = ""
    if cluster:
        cluster_block = _CLUSTER_BLOCK.format(
            docs="\n".join(
                _CLUSTER_DOC.format(
                    n=i,
                    role=(
                        "OFFICIAL" if getattr(c, "role", "") == "primary" else "COMMENTARY / PRESS"
                    ),
                    publisher=getattr(c, "publisher", "") or "(unknown)",
                    url=getattr(c, "url", ""),
                    title=getattr(c, "title", "") or "",
                    as_of=getattr(c, "as_of", "") or "unknown",
                    text=getattr(c, "text", ""),
                )
                for i, c in enumerate(cluster, start=1)
            )
        )
    payload = _RESEARCH_INPUT.format(
        title=topic.raw.title,
        url=str(topic.raw.url),
        summary=(topic.raw.summary or "")[:1000],
        document_block=document_block,
        cluster_block=cluster_block,
    )

    for attempt in range(1, _RESEARCH_ATTEMPTS + 1):
        attempt_payload = payload if attempt == 1 else f"{payload}\n{_RETRY_NUDGE}"
        try:
            resp = await asyncio.wait_for(
                _create_response(
                    client,
                    model=model,
                    instructions=instructions,
                    payload=attempt_payload,
                    budget=budget,
                ),
                timeout=float(budget.timeout_seconds),
            )
        except TimeoutError:
            log.warning(
                "research.failed",
                topic=topic.id,
                reason="timeout",
                timeout_s=budget.timeout_seconds,
                attempt=attempt,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — research NEVER drops a topic
            log.warning(
                "research.failed",
                topic=topic.id,
                reason="api_error",
                err=str(exc)[:300],
                attempt=attempt,
            )
            return None

        searches = _count_searches(resp)
        # Recorded per attempt: a retry is a real charge, and a run whose
        # research keeps coming back empty must show up on the cost line.
        _record_research_cost(resp, model=model, searches=searches)

        pack = parse_fact_pack(
            getattr(resp, "output_text", "") or "",
            budget=budget,
            model=model,
            searches=searches,
            documents=n_docs,
        )
        if pack is not None:
            break
        log.warning(
            "research.unusable_payload",
            topic=topic.id,
            attempt=attempt,
            retrying=attempt < _RESEARCH_ATTEMPTS,
        )
    if pack is None:
        log.warning(
            "research.failed",
            topic=topic.id,
            reason="unusable_payload",
            attempts=_RESEARCH_ATTEMPTS,
        )
        return None
    log.info(
        "research.ok",
        topic=topic.id,
        source_facts=len(pack.source_facts),
        context=len(pack.context),
        citations=len(pack.citations),
        searches=searches,
        documents=n_docs,
        discrepancies=len(pack.discrepancies),
    )
    return pack


# --------------------------------------------------------------------------
# NTS_131 P3 Ф2 — the pack across an event cluster
# --------------------------------------------------------------------------
#
# One research call over five documents returned ten facts on the first live
# cluster (FINMA / Julius Baer, 30 Sep): the model read the first document,
# skimmed the rest and stopped at the single-document volume it knows. So a
# cluster is read the way an analyst reads it — one document at a time, every
# figure from each — and only then merged. Two consequences that matter more
# than the count:
#
# * **The URL is set by the code, not by the model.** Each extraction call sees
#   exactly one document, so every fact it returns belongs to that document's
#   URL. Traceability stops depending on the model copying a URL correctly.
# * **Agreement and disagreement are decided on facts, not on documents.** The
#   merge call sees numbered facts, answers in ids, and the code assembles
#   ``corroborated_by`` and ``discrepancies`` from those ids — the model never
#   gets to restate a figure on the way.

_EXTRACT_INSTRUCTIONS = """\
You extract facts from ONE document for a financial-commentary writer. You
are not summarising and not writing.

Return every concrete fact the document states about the event: amounts,
percentages, thresholds, dates and deadlines, periods, counts, named
instruments and articles, named parties and what each did or must do, and the
mechanism (who must do what, by when, under which rule). Use the document's
own wording for definitions and thresholds. Never add anything the document
does not say, and never compute, convert or round a figure. Write every fact
in English, whatever the document's language; keep figures, currencies and
names exactly as the document gives them.

Return JSON only:
{"facts": [{"text": "<one self-contained sentence stating the fact>",
            "date": "<YYYY-MM-DD the fact refers to, if any, else \\"\\">",
            "value": "<the bare figure, e.g. 250000000 or 15 — empty if none>",
            "unit": "<CHF, EUR, %, days, … — empty if none>",
            "label": "<2-5 word name for the figure, e.g. 'Capital add-on' — empty if no value>",
            "comparable_group": "<short label shared by figures that measure the
             same thing on a different period, jurisdiction or party, e.g.
             'transfer tax rate' — empty if none>"}]}

Up to {max_facts} facts; fewer when the document holds fewer. A fact without a
figure, a date or a named party/instrument is not a fact — leave it out.
"""

_EXTRACT_INPUT = """\
EVENT: {title}

DOCUMENT ({role}) — {publisher}
URL: {url}

{text}
"""

_MERGE_INSTRUCTIONS = """\
You are given numbered facts extracted from several documents about ONE
event. Decide, by id only:

* "same": groups of ids that state the SAME fact (same figure, same date,
  same subject). The first id in each group is the one kept.
* "conflicts": pairs or groups of ids that state DIFFERENT things about the
  same point — a different date, amount, scope or reading. Name the point.
* "angle_hints": up to 5 non-obvious consequences for high-net-worth
  individuals, family offices and their advisers. These are leads for the
  writer's analysis, not facts; they may not contain a figure or date that
  is not in the facts.

Return JSON only:
{"same": [["F3", "F11"]],
 "conflicts": [{"point": "<what they disagree about>", "ids": ["F2", "F9"]}],
 "angle_hints": ["..."]}
"""


async def _json_call(
    client: Any, *, model: str, system: str, user: str, max_tokens: int, operation: str
) -> dict[str, Any] | None:
    from ..admin.cost_recorder import record_cost  # noqa: PLC0415
    from ..common.pricing import openai_cost  # noqa: PLC0415

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 — one document never sinks the pack
        log.warning("research.cluster_call_failed", op=operation, err=str(exc)[:200])
        return None
    usage = getattr(resp, "usage", None)
    record_cost(
        provider="openai",
        operation=operation,
        model=model,
        tokens_in=getattr(usage, "prompt_tokens", None),
        tokens_out=getattr(usage, "completion_tokens", None),
        cost_usd=openai_cost(
            model,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        ),
    )
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except (ValueError, TypeError, AttributeError, IndexError):
        log.warning("research.cluster_unparseable", op=operation)
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class _Extracted:
    fact: Fact
    official: bool


async def _extract_one(
    client: Any,
    *,
    model: str,
    title: str,
    url: str,
    publisher: str,
    role: str,
    text: str,
    as_of: str,
    max_facts: int,
) -> list[_Extracted]:
    data = await _json_call(
        client,
        model=model,
        system=_EXTRACT_INSTRUCTIONS.replace("{max_facts}", str(max_facts)),
        user=_EXTRACT_INPUT.format(
            title=title, role=role, publisher=publisher or "(unknown)", url=url, text=text
        ),
        max_tokens=3000,
        operation="research_extract",
    )
    out: list[_Extracted] = []
    seen: set[str] = set()
    for entry in list((data or {}).get("facts") or [])[:max_facts]:
        if not isinstance(entry, dict):
            continue
        text_ = _clean_text(entry.get("text"), _MAX_FACT_CHARS)
        if not text_ or text_.lower() in seen:
            continue
        seen.add(text_.lower())
        out.append(
            _Extracted(
                Fact(
                    text=text_,
                    url=url,  # set here, never taken from the model
                    publisher=_clean_text(publisher, 80),
                    date=_clean_text(entry.get("date"), 40) or as_of,
                    comparable_group=_clean_text(entry.get("comparable_group"), 60),
                    value=_clean_text(entry.get("value"), 40),
                    unit=_clean_text(entry.get("unit"), 24),
                    label=_short_label(entry.get("label")),
                ),
                official=role != "secondary",
            )
        )
    return out


def _merge(
    extracted: list[_Extracted], answer: dict[str, Any] | None
) -> tuple[list[Fact], list[Fact], list[Discrepancy], list[str]]:
    """Assemble the pack from the merge call's ids. Unknown ids are ignored."""
    ids = {f"F{i}": item for i, item in enumerate(extracted, start=1)}
    answer = answer or {}
    dropped: set[str] = set()
    corroboration: dict[str, list[str]] = {}
    for group in list(answer.get("same") or []):
        members = [m for m in (group or []) if isinstance(m, str) and m in ids]
        if len(members) < 2:
            continue
        # Prefer an official document's wording as the one kept.
        members.sort(key=lambda m: (not ids[m].official, int(m[1:])))
        keep, rest = members[0], members[1:]
        urls = [ids[m].fact.url for m in rest if ids[m].fact.url != ids[keep].fact.url]
        corroboration.setdefault(keep, []).extend(urls)
        dropped.update(rest)
    discrepancies: list[Discrepancy] = []
    for conflict in list(answer.get("conflicts") or [])[:6]:
        if not isinstance(conflict, dict):
            continue
        members = [m for m in (conflict.get("ids") or []) if isinstance(m, str) and m in ids]
        urls = {ids[m].fact.url for m in members}
        if len(members) < 2 or len(urls) < 2:
            continue
        discrepancies.append(
            Discrepancy(
                point=_clean_text(conflict.get("point"), 200) or "differing accounts",
                claims=tuple(
                    (ids[m].fact.publisher, ids[m].fact.text, ids[m].fact.url)
                    for m in members
                ),
            )
        )
    source_facts: list[Fact] = []
    context: list[Fact] = []
    for key, item in ids.items():
        if key in dropped:
            continue
        fact = item.fact
        extra = tuple(dict.fromkeys(corroboration.get(key, ())))
        if extra:
            from dataclasses import replace  # noqa: PLC0415

            fact = replace(fact, corroborated_by=extra)
        (source_facts if item.official else context).append(fact)
    hints = [
        h
        for h in (_clean_text(x, _MAX_HINT_CHARS) for x in list(answer.get("angle_hints") or [])[:_MAX_ANGLE_HINTS])
        if h
    ]
    return source_facts, context, discrepancies, hints


async def build_cluster_fact_pack(
    topic: Topic,
    *,
    document: PrimaryDocument,
    cluster: Sequence[Any],
    client: Any = None,
    model: str = RESEARCH_MODEL,
    max_facts_per_document: int = 14,
) -> FactPack | None:
    """The fact pack across an event cluster: extract per document, then merge.

    ``None`` only when nothing at all could be extracted — the caller then
    falls back to the single-call research path, which is what ran before.
    """
    if client is None:
        from openai import AsyncOpenAI  # noqa: PLC0415

        from ..common.config import get_settings  # noqa: PLC0415

        api_key = get_settings().openai_api_key
        if not api_key:
            return None
        client = AsyncOpenAI(api_key=api_key)

    docs: list[tuple[str, str, str, str, str]] = [
        (document.url, "", "primary", document.text, document.as_of)
    ]
    docs += [
        (
            getattr(c, "url", ""),
            getattr(c, "publisher", "") or "",
            getattr(c, "role", "secondary") or "secondary",
            getattr(c, "text", "") or "",
            getattr(c, "as_of", "") or "",
        )
        for c in cluster
        if getattr(c, "text", "")
    ]
    batches = await asyncio.gather(
        *(
            _extract_one(
                client,
                model=model,
                title=topic.raw.title,
                url=url,
                publisher=publisher or topic.raw.source_name,
                role=role,
                text=text,
                as_of="",
                max_facts=max_facts_per_document,
            )
            for url, publisher, role, text, _as_of in docs
        )
    )
    extracted = [item for batch in batches for item in batch]
    if not extracted:
        log.warning("research.cluster_empty", topic=topic.id, documents=len(docs))
        return None

    listing = "\n".join(
        f"F{i} [{item.fact.publisher or item.fact.url}] {item.fact.text}"
        for i, item in enumerate(extracted, start=1)
    )
    answer = await _json_call(
        client,
        model=model,
        system=_MERGE_INSTRUCTIONS,
        user=f"EVENT: {topic.raw.title}\n\n{listing}",
        max_tokens=1500,
        operation="research_merge",
    )
    source_facts, context, discrepancies, hints = _merge(extracted, answer)
    citations = list(dict.fromkeys(f.url for f in [*source_facts, *context]))
    pack = FactPack(
        source_facts=source_facts,
        context=context,
        angle_hints=hints,
        citations=citations,
        model=model,
        searches=0,
        discrepancies=discrepancies,
    )
    log.info(
        "research.cluster_ok",
        topic=topic.id,
        documents=len(docs),
        extracted=len(extracted),
        source_facts=len(source_facts),
        context=len(context),
        corroborated=sum(1 for f in [*source_facts, *context] if f.corroborated_by),
        discrepancies=len(discrepancies),
    )
    return pack
