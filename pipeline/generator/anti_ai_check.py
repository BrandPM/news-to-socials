"""Heuristics to detect AI-style writing tells (Master Doc §5 W10).

Deliberately simple. The goal isn't to be a perfect classifier — it's to
generate concrete, actionable feedback for the polish prompt. After we have
20+ real Stage-3 posts we will revisit and tune the patterns against actual
data. Until then, do not chase precision.

Returns ``(score, tells)`` where:
* ``score`` is in [0, 1] — higher means more AI-like
* ``tells`` is a human-readable list of what was found, fed back into the
  polish prompt for Stage 2
"""

from __future__ import annotations

import re
import statistics

# Phrases LLMs over-use in 2024-2026 corpora. Extend as we see real data.
_FLAGGED_PHRASES = (
    "moreover",
    "furthermore",
    "it's important to note",
    "it is important to note",
    "in conclusion",
    "in today's fast-paced",
    "ever-evolving",
    "delve into",
    "navigate the landscape",
    "in the realm of",
    "harness the power of",
    "unlock the potential",
    "at the forefront of",
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_LONG_SENT_WORDS = 30


def score_ai_tells(text: str) -> tuple[float, list[str]]:
    """Compute an AI-likeness score and a list of concrete tells."""
    text_lower = text.lower()
    tells: list[str] = []

    # 1. Flagged phrases ------------------------------------------------
    phrase_hits = [p for p in _FLAGGED_PHRASES if p in text_lower]
    if phrase_hits:
        tells.append(f"AI-cliché phrases: {', '.join(phrase_hits[:3])}")

    # 2. Sentence length variance ---------------------------------------
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if sentences:
        lengths = [len(s.split()) for s in sentences]
        long_share = sum(1 for n in lengths if n > _LONG_SENT_WORDS) / len(lengths)
        if long_share > 0.4:
            tells.append("too many long sentences (>30 words)")
        if len(lengths) > 3 and statistics.pstdev(lengths) < 4:
            tells.append("sentence lengths uniform (vary them)")

    # 3. Em-dash abuse --------------------------------------------------
    em_dash_count = text.count("—") + text.count(" - ")
    if len(sentences) > 0 and em_dash_count / len(sentences) > 0.5:
        tells.append("too many em-dashes")

    # 4. Triadic constructions ("X, Y, and Z" lists everywhere) --------
    triadic = len(re.findall(r"\b\w+,\s+\w+,?\s+and\s+\w+", text_lower))
    if triadic > 2:
        tells.append("triadic 'X, Y, and Z' constructions overused")

    # Score: simple weighted sum, capped at 1.0
    score = min(1.0, 0.2 * len(tells))
    return score, tells
