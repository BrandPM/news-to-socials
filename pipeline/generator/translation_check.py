"""Fidelity checks for EN→target translations (IT_PROJ_NTS_065).

Non-EN drafts must be faithful translations of the canonical English draft:
the same H2 structure, the same facts and numbers (nothing invented, nothing
dropped), a comparable length, and the right script for the language. These
pure helpers encode those properties so they can be asserted in tests AND used
as a pre-write gate by the backfill script.

Deliberately language-model-free: every function here is deterministic string
analysis, so it runs in unit tests and offline backfills without an API key.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# A "number token" is any run that contains at least one digit, optionally
# carrying separators / a percent sign / a currency-ish suffix. We compare on
# the DIGIT CORE only ("2.4" / "2,4" → "24", "67%" → "67") so that locale
# decimal/thousand-separator differences in the translation are not flagged as
# invented numbers, while a genuinely new figure still is.
_NUMBER_TOKEN = re.compile(r"\d[\d.,\s]*\d|\d")
_H2_LINE = re.compile(r"^\s*##(?!#)\s+(.+?)\s*$", re.MULTILINE)


def extract_number_cores(text: str) -> Counter[str]:
    """Return a multiset of digit-only cores of every numeric token in ``text``.

    "Icon saw $2.4m flow into 3 funds, up 67%." → {'24': 1, '3': 1, '67': 1}.
    Separators (``.`` ``,`` spaces, NBSP/thin-space) are stripped so a
    localised "2,4" matches the source "2.4".
    """
    cores: Counter[str] = Counter()
    for match in _NUMBER_TOKEN.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if digits:
            cores[digits] += 1
    return cores


def invented_numbers(en_body: str, translated_body: str) -> list[str]:
    """Digit cores present in the translation but NOT in the EN source.

    A non-empty result means the translation introduced a figure the
    canonical English never had — the exact failure mode NTS_065 exists to
    kill (the fabricated "67%" in the RU "Tax Advisory" piece). Counts matter:
    if EN uses "3" once and the translation uses it twice, the extra is
    reported.
    """
    en = extract_number_cores(en_body)
    tr = extract_number_cores(translated_body)
    extra = tr - en  # multiset difference
    out: list[str] = []
    for core, count in extra.items():
        out.extend([core] * count)
    return sorted(out)


def dropped_numbers(en_body: str, translated_body: str) -> list[str]:
    """Digit cores present in EN but missing from the translation."""
    en = extract_number_cores(en_body)
    tr = extract_number_cores(translated_body)
    missing = en - tr
    out: list[str] = []
    for core, count in missing.items():
        out.extend([core] * count)
    return sorted(out)


def extract_h2(markdown: str) -> list[str]:
    """Ordered list of H2 heading texts (``## Heading`` lines, not ``###``)."""
    return [m.strip() for m in _H2_LINE.findall(markdown or "")]


def h2_count(markdown: str) -> int:
    return len(extract_h2(markdown))


def length_ratio(en_body: str, translated_body: str) -> float:
    """``len(translation) / len(en)`` by character count. 1.0 == identical
    length. Returns ``inf`` if the EN body is empty (degenerate input)."""
    en_len = len(en_body or "")
    if en_len == 0:
        return float("inf")
    return len(translated_body or "") / en_len


def length_within(en_body: str, translated_body: str, tol: float = 0.35) -> bool:
    """True if the translation length is within ``±tol`` of the EN length.

    Default ±35% is generous on purpose: RU/UK/PL routinely expand or
    contract relative to English. The prompt asks the model for ±15%; this
    gate is the looser backstop that only trips on real structural drift
    (a translation half or double the source length)."""
    return abs(length_ratio(en_body, translated_body) - 1.0) <= tol


def _script_histogram(text: str) -> Counter[str]:
    """Count alphabetic characters by Unicode script family (LATIN/CYRILLIC)."""
    hist: Counter[str] = Counter()
    for ch in text or "":
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if name.startswith("CYRILLIC"):
            hist["CYRILLIC"] += 1
        elif name.startswith("LATIN"):
            hist["LATIN"] += 1
        else:
            hist["OTHER"] += 1
    return hist


def is_mostly_cyrillic(text: str, threshold: float = 0.6) -> bool:
    """True if ≥ ``threshold`` of alphabetic chars are Cyrillic (RU / UK)."""
    hist = _script_histogram(text)
    total = sum(hist.values())
    if total == 0:
        return False
    return hist["CYRILLIC"] / total >= threshold


_POLISH_DIACRITICS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def is_polish_latin(text: str, threshold: float = 0.6) -> bool:
    """True if the text is Latin-script (PL) AND carries Polish diacritics.

    Polish is Latin, so a plain "mostly Latin" check would also pass English.
    Requiring at least one of ``ąćęłńóśźż`` distinguishes a real Polish body
    from EN that simply wasn't translated."""
    hist = _script_histogram(text)
    total = sum(hist.values())
    if total == 0:
        return False
    mostly_latin = hist["LATIN"] / total >= threshold
    has_diacritic = any(ch in _POLISH_DIACRITICS for ch in (text or ""))
    return mostly_latin and has_diacritic


def has_markdown_in_title(title: str) -> bool:
    """True if ``title`` still carries markdown (heading/list markers, bold,
    backticks) — it should be plain text after ``sanitize_title``."""
    if not title:
        return False
    t = title.strip()
    if re.match(r"^\s*(?:#{1,6}|>|[-*+])\s+", t):
        return True
    if "`" in t:
        return True
    if re.search(r"\*\*.+?\*\*|__.+?__", t):
        return True
    return bool(re.search(r"(?<!\*)\*(?!\*).+?(?<!\*)\*(?!\*)", t))
