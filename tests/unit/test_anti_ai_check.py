"""Unit tests for generator/anti_ai_check.py."""

from __future__ import annotations

from pipeline.generator.anti_ai_check import score_ai_tells


def test_clean_text_low_score() -> None:
    text = (
        "The bank rolled out instant transfers last Tuesday. "
        "Customers in three countries got access first. "
        "Adoption was steady through the quarter."
    )
    score, tells = score_ai_tells(text)
    assert score <= 0.2
    assert tells == []


def test_filler_phrases_caught() -> None:
    text = (
        "Moreover, the bank rolled out instant transfers. "
        "Furthermore, customers gained access immediately. "
        "It's important to note that adoption was strong."
    )
    score, tells = score_ai_tells(text)
    assert score > 0
    assert any("cliché" in t.lower() for t in tells)


def test_uniform_sentences_caught() -> None:
    # All sentences have the same length to within noise.
    text = " ".join(
        [
            "The bank rolled out instant transfers last Tuesday morning.",
            "The bank gave customers access through their mobile app.",
            "The bank reported strong adoption in the first quarter.",
            "The bank plans to expand the feature across all regions.",
        ]
    )
    score, tells = score_ai_tells(text)
    assert any("uniform" in t.lower() for t in tells)


def test_empty_input_safe() -> None:
    score, tells = score_ai_tells("")
    assert 0.0 <= score <= 1.0
    assert isinstance(tells, list)
