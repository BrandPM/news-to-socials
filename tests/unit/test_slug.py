"""Unit tests for ``pipeline.common.slug.compute_slug``.

The S6 multilingual fanout exposed that the publisher's inline ``slugify``
stripped every non-Latin character, so Cyrillic and Polish titles
collapsed to ``"untitled"``. These tests pin the new behaviour:
transliteration via ``python-slugify`` (which uses ``unidecode``), plus a
language suffix on non-EN posts.
"""

from __future__ import annotations

import pytest

from pipeline.common.slug import LANG_SUFFIX, compute_slug


def test_english_no_suffix() -> None:
    # English is the primary language — leave the URL clean. Andriy's
    # call (2026-05-25) was "no -en suffix; ru/uk/pl get tagged".
    out = compute_slug("India 360 ONE Asset 500M fund", "en")
    assert out == "india-360-one-asset-500m-fund"
    assert not out.endswith("-en")


def test_russian_cyrillic_transliterated_and_suffixed() -> None:
    out = compute_slug("Индия: новый кредитный фонд 500 млн", "ru")
    # python-slugify (via unidecode) produces "indiia"/"novyi"; both are
    # valid romanisations. The important invariants are: ASCII output,
    # digits preserved, -ru suffix present.
    assert out.endswith("-ru")
    assert "500" in out
    assert out.replace("-", "").isascii()


def test_ukrainian_cyrillic_transliterated_and_suffixed() -> None:
    out = compute_slug("Індія залучає 500 млн для кредитного фонду", "uk")
    assert out.endswith("-uk")
    assert "500" in out
    assert out.replace("-", "").isascii()


def test_polish_diacritics_normalised_and_suffixed() -> None:
    out = compute_slug("Indie: nowy fundusz kredytowy o wartości 500 mln", "pl")
    # ś → s, ą → a, ł → l, etc.
    assert out == "indie-nowy-fundusz-kredytowy-o-wartosci-500-mln-pl"


def test_empty_title_falls_back() -> None:
    # An empty slug would make Sanity reject the document. Fallback to a
    # human-recognisable placeholder rather than 400ing the publish call.
    assert compute_slug("", "en") == "untitled"
    assert compute_slug("", "ru") == "untitled-ru"
    assert compute_slug("???", "pl") == "untitled-pl"


def test_long_title_truncated_before_suffix() -> None:
    # Sanity tolerates ~96 char slug.current; we cap the base at 80 so
    # base + "-uk" stays well under that ceiling.
    long_title = "word " * 50
    out = compute_slug(long_title, "uk")
    assert out.endswith("-uk")
    base = out[: -len("-uk")]
    assert len(base) <= 80


def test_unknown_language_falls_back_to_iso_suffix() -> None:
    # The pipeline currently fans into en/ru/uk/pl, but a future brand
    # could add another locale. Don't silently drop the language tag —
    # use the language code as the suffix verbatim.
    assert compute_slug("Test article", "de").endswith("-de")
    assert compute_slug("Test article", "es") == "test-article-es"


def test_lang_suffix_table_is_explicit_about_en() -> None:
    # Guardrail: if someone changes the EN suffix to "-en" the URL of
    # every English post changes. Pin the contract.
    assert LANG_SUFFIX["en"] == ""
    assert LANG_SUFFIX["ru"] == "-ru"
    assert LANG_SUFFIX["uk"] == "-uk"
    assert LANG_SUFFIX["pl"] == "-pl"


@pytest.mark.parametrize(
    "title,language",
    [
        ("Some title with !@#$%", "en"),
        ("Title with multiple    spaces", "ru"),
        ("UPPERCASE TITLE", "pl"),
    ],
)
def test_output_is_url_safe(title: str, language: str) -> None:
    out = compute_slug(title, language)
    import re

    # Lowercase ASCII alphanumerics + hyphens only.
    assert re.fullmatch(r"[a-z0-9-]+", out), f"unsafe slug: {out!r}"
