"""Tests for embedding-based news dedup (IT_PROJ_NTS_090 / spec NTS_079).

Covers: L1 normalization + Jaccard, L2 cosine + threshold/yellow logic,
first-seen policy, window load + cleanup, the ``_apply_dedup`` selection stage
(skip duplicate, keep distinct-story-same-company, longest-summary canonical,
FAIL-OPEN), and embedding cost recording (C1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from pipeline.admin import db as admin_db
from pipeline.admin.models import DedupLog, TopicEmbedding
from pipeline.common import config as config_module
from pipeline.common.models import RawItem, Topic
from pipeline.selector.dedup import cosine, jaccard, normalize_title
from pipeline.selector.dedup_service import (
    DedupEngine,
    cleanup_old_embeddings,
)
from tests.unit.conftest import seed_icon_brand

# --- L1: normalization + Jaccard (pure) ------------------------------------


def test_normalize_strips_case_punct_stopwords() -> None:
    assert normalize_title("The ECB Raises Rates!") == frozenset({"ecb", "raises", "rates"})


def test_normalize_drops_single_char_and_stopwords() -> None:
    # "A"/"B" (1-char) and "story"(kept) — the degenerate-title trap.
    assert normalize_title("A wealth story") == frozenset({"wealth", "story"})


def test_jaccard_identical_and_disjoint() -> None:
    assert jaccard(normalize_title("ECB raises rates"), normalize_title("ECB Raises Rates!")) == 1.0
    a = normalize_title("Visa launches new card in Europe")
    b = normalize_title("Mastercard acquires fintech startup")
    assert jaccard(a, b) < 0.7


# --- L2 / engine (DB-backed) -----------------------------------------------


@pytest.fixture
def brand_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        bid = seed_icon_brand(session)
        session.commit()
    yield bid
    admin_db.reset_for_tests()


def _vec(*xs: float) -> np.ndarray:
    return np.array(xs, dtype=np.float32)


def test_cosine_thresholds() -> None:
    a = _vec(1.0, 0.0)
    assert cosine(a, _vec(1.0, 0.05)) > 0.85  # duplicate zone
    assert 0.75 <= cosine(a, _vec(0.8, 0.6)) < 0.85  # yellow zone (=0.8)
    assert cosine(a, _vec(0.5, 0.8660254)) < 0.75  # distinct (=0.5)


def test_engine_first_seen_wins_duplicate(brand_id) -> None:
    eng = DedupEngine(brand_id_fk=brand_id, threshold=0.85, window_days=7)
    eng.remember("t-canon", "ECB raises interest rates sharply", _vec(1.0, 0.0))
    d = eng.check("t-dup", "Totally different headline tokens here", _vec(1.0, 0.05))
    assert d.is_duplicate and d.level == 2 and d.matched_topic_id == "t-canon"
    assert d.action == "skipped"


def test_engine_yellow_zone_not_skipped(brand_id) -> None:
    eng = DedupEngine(brand_id_fk=brand_id, threshold=0.85, window_days=7)
    eng.remember("t-canon", "Alpha beta gamma delta", _vec(1.0, 0.0))
    d = eng.check("t-y", "Zeta eta theta iota", _vec(0.8, 0.6))
    assert not d.is_duplicate
    assert d.action == "yellow" and d.level == 2


def test_engine_l1_title_match_even_with_orthogonal_vectors(brand_id) -> None:
    eng = DedupEngine(brand_id_fk=brand_id, threshold=0.85, window_days=7)
    eng.remember("t-canon", "ECB raises interest rates", _vec(1.0, 0.0))
    # Orthogonal embedding (cosine 0) but identical title tokens → L1 catches.
    d = eng.check("t-dup", "ECB raises interest rates", _vec(0.0, 1.0))
    assert d.is_duplicate and d.level == 1 and d.action == "skipped"


def test_engine_distinct_story_same_vector_space_not_duplicate(brand_id) -> None:
    eng = DedupEngine(brand_id_fk=brand_id, threshold=0.85, window_days=7)
    eng.remember("t-visa", "Visa launches new premium card", _vec(1.0, 0.0))
    d = eng.check("t-mc", "Mastercard acquires a fintech startup", _vec(0.5, 0.8660254))
    assert not d.is_duplicate and d.action is None


def test_engine_persists_embedding_and_logs(brand_id) -> None:
    eng = DedupEngine(brand_id_fk=brand_id, threshold=0.85, window_days=7, run_id=None)
    eng.remember("t-1", "Some headline about markets", _vec(1.0, 0.0))
    d = eng.check("t-2", "Another different headline entirely", _vec(1.0, 0.02))
    eng.record("t-2", d)
    factory = admin_db.get_session_factory()
    with factory() as s:
        embs = s.query(TopicEmbedding).all()
        logs = s.query(DedupLog).all()
    assert len(embs) == 1 and embs[0].topic_id == "t-1"
    assert len(logs) == 1 and logs[0].action == "skipped" and logs[0].matched_topic_id == "t-1"


def test_window_excludes_old_embeddings(brand_id) -> None:
    factory = admin_db.get_session_factory()
    old = datetime.now(tz=timezone.utc) - timedelta(days=10)
    with factory() as s:
        s.add(
            TopicEmbedding(
                topic_id="t-old",
                brand_id_fk=brand_id,
                embedding=_vec(1.0, 0.0).tobytes(),
                model="text-embedding-3-small",
                title_norm="old headline",
                created_at=old,
            )
        )
        s.commit()
    # 7-day window must not see the 10-day-old row → no match.
    eng = DedupEngine(brand_id_fk=brand_id, threshold=0.85, window_days=7)
    d = eng.check("t-new", "brand new headline", _vec(1.0, 0.0))
    assert not d.is_duplicate


def test_cleanup_old_embeddings_removes_only_expired(brand_id) -> None:
    factory = admin_db.get_session_factory()
    now = datetime.now(tz=timezone.utc)
    with factory() as s:
        for tid, age in (("t-old", 10), ("t-fresh", 1)):
            s.add(
                TopicEmbedding(
                    topic_id=tid,
                    brand_id_fk=brand_id,
                    embedding=_vec(1.0, 0.0).tobytes(),
                    model="m",
                    title_norm="x",
                    created_at=now - timedelta(days=age),
                )
            )
        s.commit()
    removed = cleanup_old_embeddings(brand_id, window_days=7)
    assert removed == 1
    with factory() as s:
        remaining = [e.topic_id for e in s.query(TopicEmbedding).all()]
    assert remaining == ["t-fresh"]


# --- _apply_dedup selection stage ------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.topic_calls: list[dict] = []

    def record_topic_result(self, **kw) -> None:
        self.topic_calls.append(kw)


def _topic(tid: str, title: str, summary: str, vec: np.ndarray, score: int = 8) -> Topic:
    raw = RawItem(
        source_id="s1",
        source_name="Src",
        url=f"https://example.com/{tid}",
        title=title,
        summary=summary,
    )
    return Topic(
        id=tid, brand_id="icon", raw=raw, relevance_score=float(score),
        embedding=vec.astype(float).tolist(), entities=[],
    )


def test_apply_dedup_skips_duplicate(brand_id) -> None:
    from pipeline.run import _apply_dedup

    fake = _FakeClient()
    topics = [
        _topic("t1", "ECB raises interest rates sharply today", "Long summary about the ECB decision " * 5, _vec(1.0, 0.0)),
        _topic("t2", "Completely unrelated wording in this one", "Short.", _vec(1.0, 0.03)),
    ]
    kept, skipped = _apply_dedup(
        topics, brand_id_fk=brand_id, source_id=1, run_id=None, client=fake,
        dedup_enabled=True, dedup_threshold=0.85, dedup_window_days=7,
    )
    assert skipped == 1 and len(kept) == 1
    # The longer-summary topic (t1) is canonical and survives.
    assert kept[0].id == "t1"
    assert any(c["status"] == "filtered_dup" and "duplicate_of:" in c["filter_reason"] for c in fake.topic_calls)


def test_apply_dedup_keeps_distinct_stories(brand_id) -> None:
    from pipeline.run import _apply_dedup

    fake = _FakeClient()
    topics = [
        _topic("t1", "Visa launches premium metal card", "Visa summary.", _vec(1.0, 0.0)),
        _topic("t2", "Mastercard buys a payments startup", "Mastercard summary.", _vec(0.5, 0.8660254)),
    ]
    kept, skipped = _apply_dedup(
        topics, brand_id_fk=brand_id, source_id=1, run_id=None, client=fake,
        dedup_enabled=True, dedup_threshold=0.85, dedup_window_days=7,
    )
    assert skipped == 0 and len(kept) == 2


def test_apply_dedup_disabled_is_noop(brand_id) -> None:
    from pipeline.run import _apply_dedup

    fake = _FakeClient()
    topics = [
        _topic("t1", "same title tokens here now", "s", _vec(1.0, 0.0)),
        _topic("t2", "same title tokens here now", "s", _vec(1.0, 0.0)),
    ]
    kept, skipped = _apply_dedup(
        topics, brand_id_fk=brand_id, source_id=1, run_id=None, client=fake,
        dedup_enabled=False, dedup_threshold=0.85, dedup_window_days=7,
    )
    assert skipped == 0 and len(kept) == 2


def test_apply_dedup_fails_open_on_engine_error(brand_id, monkeypatch) -> None:
    """HARD STOP: any dedup error → all topics kept, 0 skipped, run continues."""
    import pipeline.run as pipe

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(pipe, "DedupEngine", boom)
    fake = _FakeClient()
    topics = [
        _topic("t1", "headline one distinct", "s", _vec(1.0, 0.0)),
        _topic("t2", "headline two distinct", "s", _vec(1.0, 0.0)),
    ]
    kept, skipped = pipe._apply_dedup(
        topics, brand_id_fk=brand_id, source_id=1, run_id=None, client=fake,
        dedup_enabled=True, dedup_threshold=0.85, dedup_window_days=7,
    )
    assert skipped == 0 and len(kept) == 2


# --- embedding cost recording (C1) -----------------------------------------


def test_embed_records_cost(monkeypatch) -> None:
    import pipeline.run as pipe

    captured: dict = {}

    def fake_record_cost(**kw):  # noqa: ANN003
        captured.update(kw)

    monkeypatch.setattr("pipeline.admin.cost_recorder.record_cost", fake_record_cost)

    class _Usage:
        total_tokens = 50

    class _Resp:
        usage = _Usage()
        data = [type("D", (), {"embedding": [0.1, 0.2, 0.3]})()]

    class _Embeddings:
        async def create(self, **kw):  # noqa: ANN003
            return _Resp()

    class _FakeOpenAI:
        def __init__(self, **kw):  # noqa: ANN003
            self.embeddings = _Embeddings()

    monkeypatch.setattr(pipe.openai, "AsyncOpenAI", _FakeOpenAI)

    import asyncio

    vec = asyncio.run(pipe._embed("some text"))
    assert vec.shape == (3,)
    assert captured["operation"] == "embedding"
    assert captured["provider"] == "openai"
    assert captured["tokens_in"] == 50
    # 50 tokens * $0.02 / 1M = 1e-6
    assert captured["cost_usd"] == pytest.approx(50 / 1_000_000 * 0.02)


# --- config schema ---------------------------------------------------------


def test_config_update_validates_dedup_threshold_range() -> None:
    from pydantic import ValidationError

    from pipeline.admin.schemas import PipelineConfigUpdate

    assert PipelineConfigUpdate(dedup_threshold=0.9).dedup_threshold == 0.9
    assert PipelineConfigUpdate(dedup_enabled=False).dedup_enabled is False
    with pytest.raises(ValidationError):
        PipelineConfigUpdate(dedup_threshold=1.5)
    with pytest.raises(ValidationError):
        PipelineConfigUpdate(dedup_window_days=0)
