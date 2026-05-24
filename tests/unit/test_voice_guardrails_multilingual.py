"""S6.2 — language-aware voice profile parser.

``parse_voice_guardrails`` now accepts a language argument and reads
per-language sections out of the YAML if they exist. Flat (legacy)
shape still works so EN-only deployments are unaffected.
"""

from __future__ import annotations

import textwrap

import pytest

from pipeline.common.models import Language
from pipeline.generator.comment_writer import parse_voice_guardrails


# --- legacy / flat shape --------------------------------------------------


def test_flat_yaml_returns_top_level_keys():
    yaml_str = textwrap.dedent(
        """
        banned_phrases:
          - "delve into"
          - "in today's fast-paced world"
        style_examples:
          good:
            - "Crisp, factual, second-person."
            - "Lead with the so-what."
        """
    )
    banned, good = parse_voice_guardrails(yaml_str)
    assert "delve into" in banned
    assert good == ["Crisp, factual, second-person.", "Lead with the so-what."]


def test_flat_yaml_same_data_for_every_language():
    """The legacy shape doesn't differentiate languages — every caller gets
    the same guardrails regardless of which language they ask for. That
    preserves behaviour for EN-only deployments that don't fan out yet."""
    yaml_str = textwrap.dedent(
        """
        banned_phrases: ["foo"]
        style_examples:
          good: ["bar"]
        """
    )
    for lang in (Language.en, Language.ru, Language.uk, Language.pl):
        banned, good = parse_voice_guardrails(yaml_str, lang)
        assert banned == ["foo"]
        assert good == ["bar"]


def test_style_examples_as_list_still_works():
    """The earliest shape was a flat list under style_examples; still honoured
    so legacy voice profiles in admin.db don't need a migration."""
    yaml_str = textwrap.dedent(
        """
        style_examples:
          - "Plain example one."
          - "Plain example two."
        """
    )
    banned, good = parse_voice_guardrails(yaml_str)
    assert banned == []
    assert good == ["Plain example one.", "Plain example two."]


# --- per-language shape ---------------------------------------------------


PER_LANGUAGE_YAML = textwrap.dedent(
    """
    voice:
      en:
        banned_phrases: ["delve into"]
        style_examples:
          good: ["English good example."]
      ru:
        banned_phrases: ["погружаться"]
        style_examples:
          good: ["Русский пример."]
      uk:
        banned_phrases: ["заглиблюватися"]
        style_examples:
          good: ["Український приклад."]
      pl:
        banned_phrases: ["zagłębiać się"]
        style_examples:
          good: ["Polski przykład."]
    """
)


@pytest.mark.parametrize(
    "language,expected_banned,expected_good",
    [
        (Language.en, "delve into", "English good example."),
        (Language.ru, "погружаться", "Русский пример."),
        (Language.uk, "заглиблюватися", "Український приклад."),
        (Language.pl, "zagłębiać się", "Polski przykład."),
    ],
)
def test_per_language_yaml_returns_correct_section(
    language, expected_banned, expected_good
):
    banned, good = parse_voice_guardrails(PER_LANGUAGE_YAML, language)
    assert banned == [expected_banned]
    assert good == [expected_good]


def test_per_language_yaml_accepts_str_language_too():
    """Internal callers pass ``Language`` enum; route handlers may pass raw
    strings. Both work."""
    banned, good = parse_voice_guardrails(PER_LANGUAGE_YAML, "ru")
    assert banned == ["погружаться"]
    assert good == ["Русский пример."]


def test_unknown_language_falls_back_to_en():
    """If the brand's YAML hasn't been filled in for a language yet, fall
    back to the EN section so the pipeline keeps running. This makes
    S6.5's "auto-generate placeholder ru/uk/pl" step optional rather
    than a hard prerequisite."""
    yaml_str = textwrap.dedent(
        """
        voice:
          en:
            banned_phrases: ["en-banned"]
            style_examples:
              good: ["en-good"]
        """
    )
    banned, good = parse_voice_guardrails(yaml_str, Language.ru)
    assert banned == ["en-banned"]
    assert good == ["en-good"]


def test_per_language_with_no_en_falls_back_to_top_level():
    """Pathological: voice key exists but neither requested-lang nor EN is
    present. Fall back to flat top-level keys as a last resort so a
    misconfigured profile still emits *something*."""
    yaml_str = textwrap.dedent(
        """
        banned_phrases: ["top-level"]
        style_examples:
          good: ["top-level-good"]
        voice:
          pl:
            banned_phrases: ["pl-only"]
            style_examples:
              good: ["pl-only-good"]
        """
    )
    banned, good = parse_voice_guardrails(yaml_str, Language.ru)
    assert banned == ["top-level"]
    assert good == ["top-level-good"]


# --- robustness -----------------------------------------------------------


def test_malformed_yaml_returns_empty_lists():
    """Broken YAML must not crash the pipeline — we log and treat the brand
    as having no guardrails, then move on."""
    banned, good = parse_voice_guardrails("voice: {ru: }\n  - bad indent")
    assert banned == []
    assert good == []


def test_empty_string_returns_empty_lists():
    assert parse_voice_guardrails("") == ([], [])
    assert parse_voice_guardrails("") == ([], [])


def test_scalar_yaml_returns_empty_lists():
    """If someone puts a literal string in the voice profile column, we
    handle it gracefully instead of erroring."""
    banned, good = parse_voice_guardrails("just-a-string")
    assert banned == []
    assert good == []
