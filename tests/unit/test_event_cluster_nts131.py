"""NTS_131 P3 Ф2 — the event is the unit of work.

Pins the four things the directive names: items about one event join one
cluster instead of being dropped; the pack is built across the cluster with
each fact traced to its document and disagreements kept as disagreements; the
stored pack keeps every field a retry needs; and figure captions are short.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest
from cryptography.fernet import Fernet

from pipeline.admin import db as admin_db
from pipeline.admin.models import Candidate, EventClusterSource, TopicEmbedding
from pipeline.common import config as config_module
from pipeline.generator import composition as comp
from pipeline.generator.research import (
    Fact,
    FactPack,
    ResearchBudget,
    fact_pack_from_dict,
    parse_fact_pack,
)
from pipeline.selector import event_cluster as ec
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        brand_id = seed_icon_brand(session)
        session.commit()
    yield brand_id
    admin_db.reset_for_tests()


def _candidate(brand_id: int, *, title: str, embedding: np.ndarray, status="pending") -> int:
    with admin_db.get_session_factory()() as session:
        ref = f"t-{title[:10]}"
        session.add(
            TopicEmbedding(
                topic_id=ref,
                brand_id_fk=brand_id,
                embedding=embedding.astype(np.float32).tobytes(),
                model="test",
            )
        )
        row = Candidate(
            brand_id_fk=brand_id,
            input_kind="document",
            source_title=title,
            source_url="https://www.finma.ch/en/news/2026/09/notice/",
            primary_doc_url="https://www.finma.ch/en/news/2026/09/notice/",
            topic_embedding_ref=ref,
            verdict="accept",
            reason_code="ok",
            reason="test",
            status=status,
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(row)
        session.commit()
        return int(row.id)


def _unit(*values: float) -> np.ndarray:
    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


# --------------------------------------------------------------------------
# same event = similarity + identifiers
# --------------------------------------------------------------------------


def test_identifiers_are_instruments_and_figures_not_years():
    keys = ec.entity_keys("FINMA fines Julius Bär CHF 586m under DAC8 in 2026 (EU, UK)")
    assert {"FINMA", "DAC8", "#586"} <= keys
    assert "#2026" not in keys, "a year identifies a year, not an event"
    assert "EU" not in keys and "UK" not in keys


def test_near_duplicates_join_whatever_the_identifiers_say():
    assert ec.same_event(0.93, set(), set(), live_threshold=0.90, cluster_threshold=0.80)


def test_a_similar_item_joins_only_with_two_shared_identifiers():
    a = {"FINMA", "#586", "#250"}
    assert ec.same_event(0.84, a, {"FINMA", "#586"}, live_threshold=0.9, cluster_threshold=0.8)
    assert not ec.same_event(
        0.84, a, {"FINMA"}, live_threshold=0.9, cluster_threshold=0.8
    ), "one shared regulator name is what every FINMA notice of the month shares"


def test_below_the_cluster_threshold_it_is_another_story():
    a = {"FINMA", "#586", "#250"}
    assert not ec.same_event(0.70, a, a, live_threshold=0.9, cluster_threshold=0.8)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def test_sources_attach_once_and_never_duplicate_the_anchor(db):
    cid = _candidate(db, title="FINMA Julius Bär enforcement", embedding=_unit(1, 0, 0))
    assert ec.add_source(
        anchor_candidate_id=cid,
        url="https://www.finews.com/news/jb?utm=x",
        publisher="finews",
        origin="sourcing",
    )
    # Same URL modulo the query — the dedup's own normalisation.
    assert not ec.add_source(
        anchor_candidate_id=cid, url="https://www.finews.com/news/jb", origin="sourcing"
    )
    # The anchor's own document is not a second source.
    assert not ec.add_source(
        anchor_candidate_id=cid,
        url="https://www.finma.ch/en/news/2026/09/notice/",
        origin="sourcing",
    )
    sources = ec.load_sources(cid)
    assert [s.publisher for s in sources] == ["finews"]


def test_a_full_cluster_refuses_quietly(db):
    cid = _candidate(db, title="FINMA notice", embedding=_unit(1, 0, 0))
    for n in range(2):
        assert ec.add_source(
            anchor_candidate_id=cid, url=f"https://a.test/{n}", max_sources=2
        )
    assert not ec.add_source(anchor_candidate_id=cid, url="https://a.test/9", max_sources=2)


def test_find_anchor_picks_the_live_candidate_of_the_same_event(db):
    near = _candidate(
        db, title="FINMA fines Julius Bär CHF 586m, orders CHF 250m capital",
        embedding=_unit(1, 0.3, 0),
    )
    _candidate(
        db, title="Drafted FINMA piece", embedding=_unit(1, 0.3, 0), status="drafted"
    )
    found = ec.find_anchor(
        brand_id_fk=db,
        embedding=_unit(1, 0.45, 0),
        text="Julius Baer: FINMA confiscates CHF 10m after CHF 586m Signa write-down",
        live_threshold=0.99,
        cluster_threshold=0.80,
    )
    assert found is not None and found[0] == near, "drafted articles are not reopened"


# --------------------------------------------------------------------------
# the pack across the cluster
# --------------------------------------------------------------------------


CLUSTER_PAYLOAD = {
    "source_facts": [
        {
            "text": "FINMA ordered an additional capital add-on of CHF 250 million.",
            "url": "https://www.finma.ch/en/news/2026/09/notice/",
            "publisher": "FINMA",
            "value": "250000000",
            "unit": "CHF",
            "comparable_group": "capital measure",
            "label": "additional capital add-on for the bank.",
            "corroborated_by": ["https://www.finews.com/news/jb", "not a url"],
        }
    ],
    "context": [],
    "discrepancies": [
        {
            "point": "size of the earlier add-on",
            "claims": [
                {"publisher": "finews", "says": "cut from CHF 500m", "url": "https://www.finews.com/news/jb"},
                {"publisher": "FINMA", "says": "not stated", "url": "https://www.finma.ch/en/news/2026/09/notice/"},
            ],
        },
        {"point": "one-sided", "claims": [{"says": "x", "url": "https://a.test"}]},
    ],
    "citations": [],
}


def test_a_cluster_pack_keeps_corroboration_and_disagreement():
    pack = parse_fact_pack(json.dumps(CLUSTER_PAYLOAD), budget=ResearchBudget(), documents=3)
    assert pack is not None
    fact = pack.source_facts[0]
    assert fact.corroborated_by == ("https://www.finews.com/news/jb",)
    assert fact.label == "Additional capital add-on for the", "5 words at most, capitalised"
    assert len(pack.discrepancies) == 1, "a one-sided disagreement is not one"
    rendered = pack.render()
    assert "DISCREPANCIES" in rendered and "X reports A" in rendered
    assert "also stated by" in rendered
    assert pack.source_urls == [
        "https://www.finma.ch/en/news/2026/09/notice/",
        "https://www.finews.com/news/jb",
    ]


def test_a_stored_pack_comes_back_with_every_field():
    """The retry must be able to reach the same depth and blocks as the first
    attempt — before NTS_131 the values and groups were lost on the way."""
    from pipeline.run import _fact_pack_as_dict

    pack = parse_fact_pack(json.dumps(CLUSTER_PAYLOAD), budget=ResearchBudget(), documents=3)
    restored = fact_pack_from_dict(json.loads(json.dumps(_fact_pack_as_dict(pack))))
    assert restored is not None
    assert restored.source_facts == pack.source_facts
    assert restored.discrepancies == pack.discrepancies


def test_a_cluster_is_allowed_more_facts_than_one_press_release():
    payload = {
        "source_facts": [
            {"text": f"Fact {n} is EUR {n}m", "url": f"https://reg{n % 3}.eu/doc"}
            for n in range(30)
        ]
    }
    single = parse_fact_pack(json.dumps(payload), budget=ResearchBudget(max_sources=5))
    cluster = parse_fact_pack(
        json.dumps(payload), budget=ResearchBudget(max_sources=5), documents=3
    )
    assert single is not None and cluster is not None
    assert len(single.source_facts) == 15
    assert len(cluster.source_facts) == 25


# --------------------------------------------------------------------------
# blocks: short labels, capitalised captions
# --------------------------------------------------------------------------


def _pair_pack() -> FactPack:
    return FactPack(
        source_facts=[
            Fact(
                text="The maximum OFSI penalty rises from 50% of the breach value, per HM Treasury.",
                url="https://gov.uk/a",
                value="50",
                unit="%",
                comparable_group="penalty cap",
            ),
            Fact(
                text="The new cap is 100% of the breach value.",
                url="https://cms.law/b",
                value="100",
                unit="%",
                comparable_group="penalty cap",
                label="New penalty cap",
            ),
        ]
    )


def test_key_figures_carry_short_labels_and_a_capitalised_caption():
    blocks = comp.build_data_blocks(_pair_pack(), depth="deep", enabled=True)
    assert [b.type for b in blocks] == ["keyFigures"]
    payload = blocks[0].payload
    labels = [f["label"] for f in payload["figures"]]
    assert labels[1] == "New penalty cap"
    # No research label: the first clause of the fact, five words at most.
    assert labels[0] == "The maximum OFSI penalty rises"
    assert all(len(label.split()) <= 5 for label in labels)
    assert payload["caption"] == "Penalty cap"


def test_one_value_pair_from_two_documents_reaches_deep():
    facts = list(_pair_pack().source_facts) + [
        Fact(text=f"Measure {n} applies from 1 January 2027", url="https://gov.uk/a")
        for n in range(9)
    ]
    decision = comp.compute_depth_final(FactPack(source_facts=facts))
    assert decision.depth == "deep", decision.reason
    assert decision.n_sources == 2 and decision.n_pairs == 1


def test_pairs_without_values_do_not_count():
    """The §5 gap: pairs counted on figure-or-date while blocks need a value."""
    facts = [
        Fact(text=f"Threshold {n} is EUR {n}m", url=f"https://s{n % 2}.test", comparable_group="thr", unit="EUR")
        for n in range(12)
    ]
    decision = comp.compute_depth_final(FactPack(source_facts=facts))
    assert decision.n_pairs == 0
    assert decision.depth == "article"


def test_cluster_sources_reach_the_sanity_doc():
    from pipeline.production import _article_sources

    class _Doc:
        url = "https://www.finews.com/news/jb"
        title = "JB"
        publisher = "finews"
        role = "secondary"

    out = _article_sources(
        primary_url="https://www.finma.ch/notice",
        primary_title="FINMA",
        cluster_docs=[_Doc()],
        fact_pack=_pair_pack(),
    )
    assert [s["url"] for s in out] == [
        "https://www.finma.ch/notice",
        "https://www.finews.com/news/jb",
        "https://gov.uk/a",
        "https://cms.law/b",
    ]


def test_the_orm_knows_the_cluster_tables():
    assert EventClusterSource.__tablename__ == "event_cluster_sources"
