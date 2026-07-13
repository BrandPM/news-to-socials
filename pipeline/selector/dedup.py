"""Hybrid deduplication: embedding cosine + entity overlap.

Two-step gate (mitigates §5 W4 from Master Documentation):

1. Cosine similarity over text embeddings >= ``cosine_threshold``
   (defaults to 0.85). Otherwise → not a duplicate, fast path.
2. If cosine is high, compare extracted entities. Two stories about
   different companies will have high cosine but disjoint entities
   ("Visa launched X" vs "Mastercard launched Y") — keep both.

Entities are kept simple on purpose: a hand-curated regex + ``CompanyVocab``
lookup. We do **not** depend on spaCy for the MVP — adding ~500MB of model
weight to the pipeline image is not worth it before we have real false-
positive data to tune against.

For a future migration to a more sophisticated approach, see meridian's
embeddings + UMAP/HDBSCAN pattern under
/research/meridian/services/meridian-ml-service/. When/if we move that way,
this module becomes a thin client of an HTTP ML service. See
``ml_service_url`` in :class:`~pipeline.common.config.Settings`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from ..common.logging import get_logger
from ..common.models import Language, RawItem

log = get_logger(__name__)


# Tiny hand-curated finance vocabulary. Extend in Stage 3-6 as you see real
# duplicates. Keep keys lower-case; values are the canonical form.
FINANCE_ENTITIES: dict[str, str] = {
    "visa": "Visa",
    "mastercard": "Mastercard",
    "swift": "SWIFT",
    "paypal": "PayPal",
    "stripe": "Stripe",
    "revolut": "Revolut",
    "wise": "Wise",
    "binance": "Binance",
    "coinbase": "Coinbase",
    "bitcoin": "Bitcoin",
    "ethereum": "Ethereum",
    "sepa": "SEPA",
    "iban": "IBAN",
    "fatf": "FATF",
    "fincen": "FinCEN",
    "ecb": "ECB",
    "fed": "Federal Reserve",
    "sec": "SEC",
}

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9&.-]+")


def extract_entities(text: str) -> set[str]:
    """Return canonical finance entities present in ``text``."""
    found: set[str] = set()
    for tok in _WORD_RE.findall(text):
        key = tok.lower()
        if key in FINANCE_ENTITIES:
            found.add(FINANCE_ENTITIES[key])
    return found


# --- Level 1: deterministic title match (NTS_090) --------------------------

# Small English stop-word list — enough to stop "the/a/of" inflating Jaccard
# overlap between unrelated headlines. Kept tiny on purpose (no NLTK dep).
_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
        "with", "at", "by", "from", "as", "is", "are", "was", "were", "be",
        "been", "it", "its", "this", "that", "these", "those", "will", "has",
        "have", "had", "s", "amid", "over", "after", "into", "up", "down",
    }
)
_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_title(title: str) -> frozenset[str]:
    """Lowercase → tokenise (alnum) → drop stop-words + 1-char tokens.

    Returns a token *set* for Jaccard. Punctuation and case are stripped so
    "ECB Raises Rates!" and "ecb raises rates" collapse to the same set.
    O(len(title)).
    """
    toks = _TITLE_TOKEN_RE.findall(title.lower())
    return frozenset(t for t in toks if len(t) > 1 and t not in _TITLE_STOPWORDS)


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    """|A∩B| / |A∪B|. 1.0 iff both empty; 0.0 if exactly one is empty. O(|A|+|B|)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two 1-D unit-ish vectors."""
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


@dataclass(frozen=True)
class SeenItem:
    hash: str
    brand_id: str
    language: Language
    embedding: np.ndarray
    entities: frozenset[str]


@dataclass(frozen=True)
class DedupConfig:
    cosine_threshold: float = 0.85
    entity_overlap_threshold: float = 0.70  # of the smaller entity set


class Deduper:
    """In-memory deduper. Persistence (SQLite ``seen`` table) is layered on
    top by the scheduler; this class is intentionally pure so it can be unit-
    tested with synthetic vectors.
    """

    def __init__(self, config: DedupConfig | None = None) -> None:
        self.config = config or DedupConfig()
        self._seen: list[SeenItem] = []

    def is_duplicate(
        self,
        item: RawItem,
        brand_id: str,
        language: Language,
        embedding: np.ndarray,
    ) -> bool:
        entities = extract_entities(f"{item.title}\n{item.summary}")
        for prev in self._seen:
            if prev.brand_id != brand_id or prev.language != language:
                continue
            sim = cosine(prev.embedding, embedding)
            if sim < self.config.cosine_threshold:
                continue
            overlap = _entity_overlap(prev.entities, entities)
            if overlap >= self.config.entity_overlap_threshold:
                log.info(
                    "dedup.hit",
                    cosine=round(sim, 3),
                    overlap=round(overlap, 3),
                    prev_hash=prev.hash[:8],
                )
                return True
        return False

    def remember(
        self,
        item_hash: str,
        brand_id: str,
        language: Language,
        embedding: np.ndarray,
        entities: set[str],
    ) -> None:
        self._seen.append(
            SeenItem(
                hash=item_hash,
                brand_id=brand_id,
                language=language,
                embedding=embedding,
                entities=frozenset(entities),
            )
        )


def _entity_overlap(a: frozenset[str], b: set[str]) -> float:
    """Overlap ratio relative to the smaller set; returns 1.0 if both empty."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    smaller = min(len(a), len(b))
    return len(a & b) / smaller
