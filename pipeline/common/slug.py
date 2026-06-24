"""Slug computation for multilingual Sanity posts.

S6 multilingual fanout exposed a long-standing bug in the publisher's
inline ``slugify`` (``pipeline/publisher/sanity.py``): it stripped every
non-Latin character, so Cyrillic/Polish titles collapsed to ``"untitled"``
and every non-EN draft landed at ``iconfinance.io/insights/untitled``.

This module replaces that with the ``python-slugify`` library (already
listed in ``pyproject.toml``), which transliterates non-Latin scripts via
``unidecode``, and adds the language suffix that keeps slugs unique
across languages within Sanity's per-document-type uniqueness rules.

Decision (Andriy, 2026-05-25): EN gets NO suffix — keeps the primary
language URL clean. RU/UK/PL get ``-ru`` / ``-uk`` / ``-pl``.
"""

from __future__ import annotations

from slugify import slugify

# Per-language URL suffix. Empty string for the primary language (English)
# so iconfinance.io/insights/<slug> stays clean. Non-primary languages get
# their ISO code appended; that's what disambiguates the same article
# across the four locales we publish into.
LANG_SUFFIX: dict[str, str] = {
    "en": "",
    "ru": "-ru",
    "uk": "-uk",
    "pl": "-pl",
}

# Cap the base (pre-suffix) length so the final slug + suffix stays under
# Sanity's 96-char practical ceiling for slug.current.
_BASE_MAX_LENGTH = 80


def compute_slug(title: str, language: str) -> str:
    """Compute a deterministic URL slug for ``title`` in ``language``.

    Returns lowercase kebab-case with a language suffix (except EN).
    Falls back to ``untitled-<lang>`` (or just ``untitled`` for EN) when
    the title slugifies to an empty string — the alternative is letting
    Sanity reject the document.
    """
    suffix = LANG_SUFFIX.get(language, f"-{language}")
    base = slugify(title or "", lowercase=True, separator="-", max_length=_BASE_MAX_LENGTH)
    if not base:
        base = "untitled"
    return f"{base}{suffix}"
