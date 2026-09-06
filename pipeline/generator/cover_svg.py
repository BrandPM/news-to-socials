"""Covers drawn from the article's own data, not from diffusion (NTS_112).

NTS_112 opens with the diagnosis: diffusion paints "financial atmosphere" —
glass, towers, handshakes — and forty articles get the same atmosphere. The
cover carries no information, is not recognisable as Icon, and does not tell a
``deep`` piece on CRS from a ``note`` about an appointment. It also costs $0.04
plus a gpt-4o-mini call to write the scene prompt.

So the cover is generated from the candidate and the fact pack: SVG rendered to
PNG through ``resvg``, deterministic (the seed is the candidate id), free, and
about fifty milliseconds. Three layers, all language-independent:

1. **The service field** — the service's colour with a coarse paper grain
   (``feTurbulence``, seeded), which kills the flatness and makes the series
   recognisable.
2. **The service motif** — thin paper-coloured lines whose *geometry is the
   data*: arcs counted by facts, a tree branched by jurisdictions, rectangles
   nested by thresholds, two shapes overlapping in the ratio of the deal's own
   figures, a polygon with a vertex per document section. Two articles in the
   same service are never identical, and the series is still one series.
3. **The stamp** — the key figure with no words: ``CHF 5 000 000``, ``12.5 %``,
   ``2027-01-01``, ``FATF``. Currencies, percentages, ISO dates and the short
   names of acts are the only text on the cover, and none of it needs
   translating — which is what lets one asset serve all four language siblings
   (NTS_069).

``note`` gets the field and the stamp and no motif: a short note should not
dress up as an analysis.

Nothing here draws a word from the headline, a person, a building or a flag —
NTS_112's three prohibitions, and the reason the cover can be produced without
a model reading anything.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)

# The five service colours of NTS_110/112. Kept here as the default rather than
# in a config column because they are the brand's identity, not a tunable; a
# second brand overrides the whole map (NTS_109).
SERVICE_COLOURS: Mapping[str, str] = {
    "wealth": "#1F3A5F",
    "family": "#2E4A3C",
    "structuring": "#3B2F55",
    "ma": "#5A3222",
    "special": "#33383D",
}
DEFAULT_COLOUR = "#33383D"

# "paper" — the single foreground colour. Contrast against every one of the
# five fields is above 4.5:1, which is the NTS_112 requirement and the reason
# the stamp never changes colour by service.
PAPER = "#F3EFE7"

COVER_SIZES: Mapping[str, tuple[int, int]] = {
    "wide": (1200, 630),
    "square": (1200, 1200),
}

MOTIFS = ("wealth", "family", "structuring", "ma", "special")


def _seeded(seed: int) -> _Rng:
    return _Rng(seed)


@dataclass
class _Rng:
    """A tiny deterministic PRNG.

    ``random`` seeded globally would make two covers depend on the order they
    were drawn in; this one is per-cover and its state is the candidate id, so
    the same candidate always produces the same file — which is what makes the
    cover cacheable and a regression visible as a diff.
    """

    state: int

    def next(self) -> float:
        # xorshift32, adequate for jitter and reproducible across platforms in
        # a way that ``hash()`` is not.
        x = self.state or 0x9E3779B9
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x & 0xFFFFFFFF
        return self.state / 0xFFFFFFFF

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * self.next()


@dataclass
class CoverData:
    """What a cover is drawn from. All of it already exists on the candidate."""

    candidate_id: int
    service: str | None = None
    depth: str = "article"
    jurisdictions: Sequence[str] = ()
    fact_count: int = 0
    figures: Sequence[str] = ()
    sections: int = 0
    stamp: str = ""
    colours: Mapping[str, str] = field(default_factory=lambda: SERVICE_COLOURS)

    @property
    def colour(self) -> str:
        return self.colours.get(self.service or "", DEFAULT_COLOUR)


# --------------------------------------------------------------------------
# the stamp (NTS_112 §Грамматика 3)
# --------------------------------------------------------------------------

_AMOUNT_RE = re.compile(
    r"\b(?:EUR|USD|CHF|GBP|PLN|UAH)\s?[\d][\d\s,.]*(?:\s?(?:m|bn|million|billion))?",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s?%")
_ISO_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
# Short act names: FATF, DAC8, CRS, MiCA, CARF, AMLA — an uppercase run,
# optionally with a digit.
_ACT_RE = re.compile(r"\b[A-Z]{3,6}\s?\d?\b")


def pick_stamp(*, facts: Sequence[str], fallback: Sequence[str] = ()) -> str:
    """The one value the cover states, in the order NTS_112 prefers them.

    Amounts before percentages before dates before act names — most concrete
    first, because the stamp is the whole informational content of the image
    and a date is a weaker answer than a threshold.

    Returns ``""`` when the material has no figure at all; the caller then
    stamps the jurisdiction codes, which is what NTS_112 says a ``note``
    should carry.
    """
    haystack = " ".join(facts)
    for pattern in (_AMOUNT_RE, _PERCENT_RE, _ISO_DATE_RE):
        found = pattern.search(haystack)
        if found:
            return _tidy(found.group(0))
    for pattern in (_ACT_RE,):
        for match in pattern.finditer(haystack):
            token = match.group(0).strip()
            # Guard against ISO country codes and the brand's own initials
            # reading as an act.
            if token not in ("EUR", "USD", "CHF", "GBP", "PLN", "UAH", "THE"):
                return token
    return " · ".join(fallback[:3])


# U+2009 THIN SPACE — the typographic digit separator. Written as an escape
# because it is indistinguishable from an ordinary space in a diff.
_THIN_SPACE = "\u2009"


def _tidy(raw: str) -> str:
    """Group digits with thin spaces; ``5000000`` is not a readable number."""
    text = " ".join(raw.split())
    def _group(match: re.Match[str]) -> str:
        digits = match.group(0).replace(",", "").replace(" ", "")
        if len(digits) <= 4 or "." in digits:
            return digits
        out = ""
        for index, char in enumerate(reversed(digits)):
            if index and index % 3 == 0:
                out = _THIN_SPACE + out
            out = char + out
        return out

    return re.sub(r"[\d][\d\s,]*", _group, text).strip()


# --------------------------------------------------------------------------
# the motifs (NTS_112 §Грамматика 2)
# --------------------------------------------------------------------------


def _arcs(data: CoverData, rng: _Rng, width: int, height: int) -> str:
    """``wealth`` — concentric arcs, one per fact."""
    count = max(2, min(9, data.fact_count or 3))
    cx, cy = width * 0.72, height * 0.5
    parts = []
    for index in range(count):
        radius = height * (0.16 + 0.075 * index) * rng.between(0.96, 1.04)
        parts.append(
            f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{radius:.0f}" fill="none" '
            f'stroke="{PAPER}" stroke-opacity="{0.5 + 0.03 * index:.2f}" '
            f'stroke-width="1.5"/>'
        )
    return "".join(parts)


def _tree(data: CoverData, rng: _Rng, width: int, height: int) -> str:
    """``family`` — a branching tree, generations by jurisdiction count."""
    branches = max(2, min(5, len(data.jurisdictions) or 2))
    parts = [
        f'<line x1="{width * 0.72:.0f}" y1="{height * 0.86:.0f}" '
        f'x2="{width * 0.72:.0f}" y2="{height * 0.52:.0f}" stroke="{PAPER}" '
        f'stroke-opacity="0.7" stroke-width="1.5"/>'
    ]
    for index in range(branches):
        angle = math.pi * (0.25 + 0.5 * (index + 1) / (branches + 1))
        length = height * rng.between(0.2, 0.3)
        x1, y1 = width * 0.72, height * 0.52
        x2 = x1 + math.cos(angle) * length * -1
        y2 = y1 - math.sin(angle) * length
        parts.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke="{PAPER}" stroke-opacity="0.6" stroke-width="1.5"/>'
        )
        parts.append(
            f'<circle cx="{x2:.0f}" cy="{y2:.0f}" r="4" fill="{PAPER}" '
            'fill-opacity="0.6"/>'
        )
    return "".join(parts)


def _nested(data: CoverData, rng: _Rng, width: int, height: int) -> str:
    """``structuring`` — nested rectangles, depth by thresholds."""
    levels = max(2, min(6, len(data.figures) or data.fact_count or 3))
    parts = []
    for index in range(levels):
        inset = height * 0.07 * index
        x = width * 0.55 + inset
        y = height * 0.16 + inset
        w = width * 0.36 - inset * 2
        h = height * 0.68 - inset * 2
        if w <= 8 or h <= 8:
            break
        parts.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
            f'fill="none" stroke="{PAPER}" '
            f'stroke-opacity="{0.5 + 0.05 * index:.2f}" stroke-width="1.5"/>'
        )
    return "".join(parts)


def _merging(data: CoverData, rng: _Rng, width: int, height: int) -> str:
    """``ma`` — two shapes entering one another, areas in the deal's ratio."""
    ratio = 0.6
    numbers: list[float] = [
        parsed
        for parsed in (_as_float(value) for value in data.figures)
        if parsed
    ]
    if len(numbers) >= 2:
        larger, smaller = max(numbers[:2]), min(numbers[:2])
        if larger:
            ratio = max(0.25, min(0.95, smaller / larger))
    big = height * 0.3
    small = big * math.sqrt(ratio)
    cx, cy = width * 0.7, height * 0.5
    return (
        f'<circle cx="{cx - big * 0.35:.0f}" cy="{cy:.0f}" r="{big:.0f}" '
        f'fill="none" stroke="{PAPER}" stroke-opacity="0.65" stroke-width="1.5"/>'
        f'<circle cx="{cx + small * 0.35:.0f}" cy="{cy:.0f}" r="{small:.0f}" '
        f'fill="none" stroke="{PAPER}" stroke-opacity="0.8" stroke-width="1.5"/>'
    )


def _polygon(data: CoverData, rng: _Rng, width: int, height: int) -> str:
    """``special`` — an irregular polygon, one vertex per document section."""
    vertices = max(3, min(11, data.sections or data.fact_count or 5))
    cx, cy = width * 0.72, height * 0.5
    radius = height * 0.3
    points = []
    for index in range(vertices):
        angle = 2 * math.pi * index / vertices
        jitter = rng.between(0.78, 1.12)
        points.append(
            f"{cx + math.cos(angle) * radius * jitter:.0f},"
            f"{cy + math.sin(angle) * radius * jitter:.0f}"
        )
    return (
        f'<polygon points="{" ".join(points)}" fill="none" stroke="{PAPER}" '
        'stroke-opacity="0.7" stroke-width="1.5"/>'
    )


_MOTIF_FUNCS = {
    "wealth": _arcs,
    "family": _tree,
    "structuring": _nested,
    "ma": _merging,
    "special": _polygon,
}


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace(" ", "").rstrip("%"))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# the cover
# --------------------------------------------------------------------------


def build_svg(data: CoverData, *, size: str = "wide") -> str:
    """The whole cover as SVG. Deterministic for a given candidate id."""
    width, height = COVER_SIZES.get(size, COVER_SIZES["wide"])
    rng = _seeded(data.candidate_id or 1)
    colour = data.colour
    codes = " · ".join(str(code).upper() for code in list(data.jurisdictions)[:3])
    stamp = data.stamp or codes

    # A `note` gets no motif: a short note must not dress up as an analysis
    # (NTS_112 §Правила).
    motif = ""
    if data.depth != "note":
        draw = _MOTIF_FUNCS.get(data.service or "", _polygon)
        motif = draw(data, rng, width, height)

    grain_seed = (data.candidate_id or 1) % 9973
    stamp_size = 64 if size == "wide" else 84
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        "<defs>"
        f'<filter id="grain" x="0" y="0" width="100%" height="100%">'
        f'<feTurbulence type="fractalNoise" baseFrequency="0.9" '
        f'numOctaves="3" seed="{grain_seed}" result="noise"/>'
        '<feColorMatrix in="noise" type="saturate" values="0"/>'
        '<feComponentTransfer><feFuncA type="linear" slope="0.16"/>'
        "</feComponentTransfer></filter>"
        "</defs>"
        f'<rect width="{width}" height="{height}" fill="{colour}"/>'
        f'<rect width="{width}" height="{height}" filter="url(#grain)" '
        'fill="#FFFFFF" opacity="0.55"/>'
        f"{motif}"
        f'<text x="{int(width * 0.06)}" y="{int(height * 0.84)}" '
        f'font-family="IBM Plex Sans, Helvetica, Arial, sans-serif" '
        f'font-size="{stamp_size}" font-weight="600" fill="{PAPER}" '
        f'letter-spacing="-1">{_escape(stamp)}</text>'
        + (
            f'<text x="{int(width * 0.06)}" y="{int(height * 0.90)}" '
            f'font-family="IBM Plex Sans, Helvetica, Arial, sans-serif" '
            f'font-size="{int(stamp_size * 0.32)}" fill="{PAPER}" '
            f'fill-opacity="0.75" letter-spacing="2">{_escape(codes)}</text>'
            if codes and stamp != codes
            else ""
        )
        + "</svg>"
    )


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_png(svg: str) -> bytes:
    """SVG → PNG through ``resvg``. Raises if the renderer is unavailable.

    Deliberately not falling back to a blank image: a cover that silently
    became a coloured rectangle would pass every check the pipeline makes and
    ship as a finished article.
    """
    import resvg_py

    return bytes(resvg_py.svg_to_bytes(svg_string=svg))


def cover_from_candidate(
    *,
    candidate: Any,
    fact_pack: Any = None,
    document_sections: int = 0,
    colours: Mapping[str, str] | None = None,
) -> CoverData:
    """Assemble the drawing data from rows the run already wrote."""
    import json

    facts = []
    figures: list[str] = []
    if fact_pack is not None:
        for fact in list(getattr(fact_pack, "source_facts", []) or []) + list(
            getattr(fact_pack, "context", []) or []
        ):
            facts.append(getattr(fact, "text", "") or "")
            value = (getattr(fact, "value", "") or "").strip()
            if value:
                figures.append(value)

    raw_jurisdictions = getattr(candidate, "jurisdictions", None)
    if isinstance(raw_jurisdictions, str):
        try:
            jurisdictions = list(json.loads(raw_jurisdictions) or [])
        except (TypeError, ValueError):
            jurisdictions = []
    else:
        jurisdictions = list(raw_jurisdictions or [])

    return CoverData(
        candidate_id=int(getattr(candidate, "id", 0) or 0),
        service=getattr(candidate, "service_category", None),
        depth=getattr(candidate, "depth_final", None)
        or getattr(candidate, "depth_prior", None)
        or "article",
        jurisdictions=jurisdictions,
        fact_count=len(facts),
        figures=figures,
        sections=document_sections,
        stamp=pick_stamp(
            facts=facts,
            fallback=[str(code).upper() for code in jurisdictions],
        ),
        colours=colours or SERVICE_COLOURS,
    )
