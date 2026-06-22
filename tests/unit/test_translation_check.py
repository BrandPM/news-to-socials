"""Unit tests for the EN→target translation fidelity checks (NTS_065).

These guard the four properties a faithful translation must hold:
* right script for the language (RU/UK Cyrillic, PL Latin + diacritics),
* fact/number parity (no invented figures — the "67%" regression),
* same H2 set + comparable length,
* title is plain text (no leaked markdown).
"""
# ruff: noqa: RUF001 — fixtures mix Cyrillic with ASCII figures/markdown on
# purpose (real translated bodies); the "ambiguous character" lint is noise here.

from __future__ import annotations

from pipeline.generator.translation_check import (
    dropped_numbers,
    extract_h2,
    extract_number_cores,
    h2_count,
    has_markdown_in_title,
    invented_numbers,
    is_mostly_cyrillic,
    is_polish_latin,
    length_ratio,
    length_within,
)

# --- number parity --------------------------------------------------------

EN_BODY = (
    "Icon sees a shift.\n\n"
    "## The repricing\n\n"
    "A $2.4m allocation moved into 3 funds, up 67% on the quarter.\n\n"
    "## What changes next\n\n"
    "Base rates held at 50bp.\n"
)


def test_extract_number_cores_strips_separators_and_percent():
    cores = extract_number_cores("$2.4m into 3 funds, up 67%, held at 50bp")
    assert cores["24"] == 1
    assert cores["3"] == 1
    assert cores["67"] == 1
    assert cores["50"] == 1


def test_invented_numbers_flags_fabricated_stat():
    """The NTS_065 failure mode: a translation that adds a "85% of clients"
    figure the English never had (the real bug invented a client-share stat
    that was absent from EN)."""
    ru_with_fabrication = (
        "## Переоценка\n\n"
        "85% клиентов перевели $2,4 млн в 3 фонда, рост 67%, ставки 50bp.\n\n"
        "## Что меняется\n\nДалее.\n"
    )
    # EN never mentions 85; the translation conjured it → flagged.
    invented = invented_numbers(EN_BODY, ru_with_fabrication)
    assert "85" in invented
    # And a faithful figure that IS in EN is not falsely flagged.
    assert "67" not in invented


def test_invented_numbers_empty_for_faithful_translation():
    ru_faithful = (
        "## Переоценка\n\n"
        "Аллокация $2,4 млн ушла в 3 фонда, рост на 67% за квартал.\n\n"
        "## Что меняется дальше\n\nБазовые ставки на уровне 50bp.\n"
    )
    # Locale separator "2,4" must NOT be read as a new number vs EN "2.4".
    assert invented_numbers(EN_BODY, ru_faithful) == []


def test_dropped_numbers_flags_missing_figure():
    ru_missing = (
        "## Переоценка\n\nАллокация ушла в 3 фонда.\n\n"
        "## Что меняется\n\nСтавки держатся.\n"
    )
    dropped = dropped_numbers(EN_BODY, ru_missing)
    assert "24" in dropped and "67" in dropped and "50" in dropped


# --- H2 structure ---------------------------------------------------------


def test_extract_h2_ignores_h3_and_returns_order():
    md = "## First\n\ntext\n\n### sub\n\n## Second\n"
    assert extract_h2(md) == ["First", "Second"]


def test_h2_count_parity():
    ru = "## Переоценка\n\nx\n\n## Что меняется дальше\n\ny\n"
    assert h2_count(EN_BODY) == h2_count(ru) == 2


# --- length ---------------------------------------------------------------


def test_length_ratio_and_within():
    en = "x" * 100
    tr = "y" * 120
    assert length_ratio(en, tr) == 1.2
    assert length_within(en, tr, tol=0.35)
    assert not length_within(en, "y" * 200, tol=0.35)


# --- script / charset -----------------------------------------------------


def test_cyrillic_detection_ru_uk():
    assert is_mostly_cyrillic("Базовые ставки держатся на уровне")
    assert is_mostly_cyrillic("Базові ставки тримаються на рівні")  # uk
    assert not is_mostly_cyrillic("Base rates hold steady")


def test_polish_detection_requires_diacritics():
    pl = "Stopy bazowe utrzymują się; przepływ środków wzrósł"
    assert is_polish_latin(pl)
    # Untranslated English is Latin but has no Polish diacritics → not PL.
    assert not is_polish_latin("Base rates hold steady and flows rose")
    # Cyrillic is not Polish.
    assert not is_polish_latin("Базовые ставки")


# --- title markdown -------------------------------------------------------


def test_has_markdown_in_title():
    assert has_markdown_in_title("## Переоценка кредита")
    assert has_markdown_in_title("**Жирный заголовок**")
    assert has_markdown_in_title("`code` title")
    assert not has_markdown_in_title("Переоценка мезонинного кредита")
    assert not has_markdown_in_title("Icon: #1 в сегменте")
