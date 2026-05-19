"""Unit tests for selector/dedup.py.

These don't need real embeddings — synthetic vectors are enough to verify
the cosine + entity-overlap logic.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import HttpUrl

from pipeline.common.models import Language, RawItem
from pipeline.selector.dedup import (
    DedupConfig,
    Deduper,
    cosine,
    extract_entities,
)


def _item(title: str, url: str = "https://example.com/x") -> RawItem:
    return RawItem(
        source_id="s1",
        source_name="test",
        url=HttpUrl(url),
        title=title,
        summary="",
    )


def _vec(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float32)


# --- helpers --------------------------------------------------------------


def test_cosine_identity() -> None:
    v = _vec(0.5, 0.5, 0.5, 0.5)
    assert cosine(v, v) == pytest.approx(1.0, abs=1e-6)


def test_cosine_orthogonal() -> None:
    a = _vec(1.0, 0.0)
    b = _vec(0.0, 1.0)
    assert cosine(a, b) == pytest.approx(0.0, abs=1e-6)


def test_cosine_handles_zero_vector() -> None:
    a = _vec(0.0, 0.0)
    b = _vec(1.0, 0.0)
    assert cosine(a, b) == 0.0  # no NaN


def test_cosine_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        cosine(_vec(1.0, 0.0), _vec(1.0, 0.0, 0.0))


# --- entity extraction ---------------------------------------------------


def test_extract_entities_finds_canonical_names() -> None:
    found = extract_entities("Visa and Mastercard partner on SEPA pilot")
    assert {"Visa", "Mastercard", "SEPA"} <= found


def test_extract_entities_empty_text() -> None:
    assert extract_entities("") == set()


# --- deduper -------------------------------------------------------------


def test_deduper_first_item_never_duplicate() -> None:
    d = Deduper()
    item = _item("Visa launches new payment rail")
    assert not d.is_duplicate(item, "icon", Language.en, _vec(1.0, 0.0))


def test_deduper_near_duplicate_blocked() -> None:
    cfg = DedupConfig(cosine_threshold=0.85, entity_overlap_threshold=0.5)
    d = Deduper(cfg)
    e1 = _vec(1.0, 0.0)
    d.remember("h1", "icon", Language.en, e1, {"Visa"})

    # Same vector, same entity → must be flagged as duplicate.
    near = _item("Visa launches new payment rail (mirror)")
    assert d.is_duplicate(near, "icon", Language.en, e1)


def test_deduper_high_cosine_but_different_entities_kept() -> None:
    """Two stories that look similar in embedding space but mention
    different companies should both pass. This is the W4 mitigation."""
    cfg = DedupConfig(cosine_threshold=0.85, entity_overlap_threshold=0.7)
    d = Deduper(cfg)
    near = _vec(1.0, 0.01)

    d.remember("h1", "icon", Language.en, _vec(1.0, 0.0), {"Visa"})
    # Different brand mentioned, similar vector
    new_item = _item("Mastercard launches new payment rail")
    assert not d.is_duplicate(new_item, "icon", Language.en, near)


def test_deduper_isolates_brands() -> None:
    d = Deduper()
    v = _vec(1.0, 0.0)
    d.remember("h1", "icon", Language.en, v, {"Visa"})

    same = _item("Visa launches new payment rail")
    # Different brand_id → not a duplicate even though the content is the same.
    assert not d.is_duplicate(same, "neovox", Language.en, v)


def test_deduper_isolates_languages() -> None:
    d = Deduper()
    v = _vec(1.0, 0.0)
    d.remember("h1", "icon", Language.en, v, {"Visa"})

    same = _item("Visa launches new payment rail")
    assert not d.is_duplicate(same, "icon", Language.ru, v)
