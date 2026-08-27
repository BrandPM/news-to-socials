"""IT_PROJ_NTS_092 — the research stage and its URL discipline.

``build_fact_pack`` is the only place in the pipeline allowed to introduce
facts the RSS item did not carry, so the tests here are mostly about what it
REFUSES: a fact without a resolvable URL, a malformed payload, a timeout, a
call that errors. Every one of those must come back as ``None`` — the thin
path — and never as an exception, because a broken research stage must not
drop a topic.

The OpenAI round-trip is mocked throughout; no test here makes a network call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.cost_recorder import CostContext, cost_context
from pipeline.admin.models import CostRecord
from pipeline.common import config as config_module
from pipeline.common.models import RawItem, Topic
from pipeline.generator.research import (
    NO_FACT_PACK,
    Fact,
    FactPack,
    ResearchBudget,
    build_fact_pack,
    is_resolvable_url,
    parse_fact_pack,
    render_fact_pack,
)


def _topic(topic_id: str = "t-1") -> Topic:
    return Topic(
        id=topic_id,
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="Reuters",
            url="https://www.reuters.com/markets/story",
            title="Regulator lifts the reporting threshold to EUR 2m",
            summary="The threshold rises from EUR 1m on 1 January 2027.",
        ),
        relevance_score=8.0,
    )


def _resp(text: str, *, searches: int = 1, tokens: tuple[int, int] = (900, 400)) -> Any:
    """Mimic a Responses API result: ``output_text``, ``output``, ``usage``."""
    usage = type("U", (), {"input_tokens": tokens[0], "output_tokens": tokens[1]})()
    calls = [type("I", (), {"type": "web_search_call"})() for _ in range(searches)]
    message = type("I", (), {"type": "message"})()
    return type(
        "R", (), {"output_text": text, "output": [*calls, message], "usage": usage}
    )()


_GOOD_PAYLOAD = {
    "source_facts": [
        {
            "text": "The reporting threshold rises to EUR 2m on 1 January 2027.",
            "url": "https://www.reuters.com/markets/story",
            "publisher": "Reuters",
            "date": "2026-08-26",
        },
        {
            "text": "The prior threshold was EUR 1m, set in 2019.",
            "url": "https://www.esma.europa.eu/press/2026-08-26",
            "publisher": "ESMA",
            "date": "2026-08-26",
        },
    ],
    "context": [
        {
            "text": "Germany applied a EUR 1.5m threshold from 2024.",
            "url": "https://www.ft.com/content/abc",
            "publisher": "Financial Times",
            "date": "2024-03-02",
        }
    ],
    "angle_hints": ["Family offices below EUR 2m fall out of scope entirely."],
    "citations": [
        "https://www.reuters.com/markets/story",
        "https://www.bafin.de/dok/123",
    ],
}


def _client(*responses: Any) -> AsyncMock:
    client = AsyncMock()
    client.responses.create = AsyncMock(side_effect=list(responses))
    return client


# --- URL discipline --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reuters.com/markets/story",
        "http://esma.europa.eu/x",
        "https://sub.domain.co.uk/a/b?c=1#d",
    ],
)
def test_resolvable_urls_are_accepted(url):
    assert is_resolvable_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        123,
        "reuters.com/story",  # no scheme
        "ftp://files.example.org/x",  # wrong scheme
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://example.com/story",  # RFC 2606 reserved
        "https://www.example.com/story",
        "https://acme.invalid/x",
        "https://nodot/x",
        "[not a url]",
    ],
)
def test_unresolvable_urls_are_rejected(url):
    assert is_resolvable_url(url) is False


# --- parse_fact_pack -------------------------------------------------------


def test_parse_keeps_facts_and_rebuilds_citations():
    pack = parse_fact_pack(json.dumps(_GOOD_PAYLOAD), budget=ResearchBudget())
    assert pack is not None
    assert len(pack.source_facts) == 2
    assert len(pack.context) == 1
    assert pack.fact_count == 3
    assert pack.angle_hints == [
        "Family offices below EUR 2m fall out of scope entirely."
    ]
    # Citations lead with the URLs actually behind retained facts, then pick up
    # the extra resolvable URL the model listed.
    assert pack.citations[:3] == [
        "https://www.reuters.com/markets/story",
        "https://www.esma.europa.eu/press/2026-08-26",
        "https://www.ft.com/content/abc",
    ]
    assert "https://www.bafin.de/dok/123" in pack.citations
    assert pack.is_empty() is False


def test_parse_drops_the_fact_without_a_url_and_keeps_the_rest():
    payload = {
        "source_facts": [
            {"text": "Threshold rises to EUR 2m.", "url": "https://reuters.com/a"},
            {"text": "Analysts expect EUR 4bn of outflows.", "url": ""},
            {"text": "A third claim.", "url": "https://example.com/made-up"},
            {"text": "", "url": "https://reuters.com/b"},
        ],
        "context": [],
    }
    pack = parse_fact_pack(json.dumps(payload), budget=ResearchBudget())
    assert pack is not None
    assert [f.text for f in pack.source_facts] == ["Threshold rises to EUR 2m."]
    # The dropped facts leave no trace in citations either.
    assert pack.citations == ["https://reuters.com/a"]


def test_parse_returns_none_when_every_fact_is_dropped():
    payload = {
        "source_facts": [
            {"text": "Something happened.", "url": "not-a-url"},
            {"text": "Something else.", "url": "https://example.com/x"},
        ],
        "context": [{"text": "Background.", "url": ""}],
        "citations": ["https://www.reuters.com/real"],
    }
    assert parse_fact_pack(json.dumps(payload), budget=ResearchBudget()) is None


def test_parse_returns_none_on_malformed_json():
    assert parse_fact_pack("{not json at all", budget=ResearchBudget()) is None


def test_parse_returns_none_on_empty_text():
    assert parse_fact_pack("", budget=ResearchBudget()) is None
    assert parse_fact_pack("   \n ", budget=ResearchBudget()) is None


def test_parse_returns_none_when_json_is_not_an_object():
    assert parse_fact_pack("[1, 2, 3]", budget=ResearchBudget()) is None


def test_parse_tolerates_a_code_fence():
    fenced = "```json\n" + json.dumps(_GOOD_PAYLOAD) + "\n```"
    pack = parse_fact_pack(fenced, budget=ResearchBudget())
    assert pack is not None and pack.fact_count == 3


def test_parse_dedupes_identical_facts():
    entry = {"text": "Threshold is EUR 2m.", "url": "https://reuters.com/a"}
    payload = {"source_facts": [entry, dict(entry)], "context": []}
    pack = parse_fact_pack(json.dumps(payload), budget=ResearchBudget())
    assert pack is not None and len(pack.source_facts) == 1


def test_context_is_capped_by_max_sources():
    payload = {
        "source_facts": [{"text": "A.", "url": "https://reuters.com/a"}],
        "context": [
            {"text": f"Ctx {i}.", "url": f"https://outlet{i}.com/x"} for i in range(9)
        ],
    }
    pack = parse_fact_pack(
        json.dumps(payload), budget=ResearchBudget(max_sources=2)
    )
    assert pack is not None and len(pack.context) == 2


# --- render ----------------------------------------------------------------


def test_render_carries_every_url_next_to_its_fact():
    pack = parse_fact_pack(json.dumps(_GOOD_PAYLOAD), budget=ResearchBudget())
    block = render_fact_pack(pack)
    assert "SOURCE FACTS" in block and "CONTEXT" in block
    for fact in [*pack.source_facts, *pack.context]:
        assert fact.url in block
        assert fact.text in block
    assert "ANGLE HINTS" in block
    assert "CITATIONS" in block


def test_render_thin_path_tells_the_drafter_to_write_shorter():
    for pack in (None, FactPack()):
        block = render_fact_pack(pack)
        assert block == NO_FACT_PACK
        assert "NO RESEARCH AVAILABLE" in block
        assert "SHORTER" in block


def test_fact_render_includes_publisher_and_date():
    rendered = Fact(
        text="X happened.",
        url="https://reuters.com/a",
        publisher="Reuters",
        date="2026-08-26",
    ).render()
    assert rendered == "X happened. (https://reuters.com/a — Reuters — 2026-08-26)"


# --- build_fact_pack: the happy path --------------------------------------


async def test_build_fact_pack_success():
    client = _client(_resp(json.dumps(_GOOD_PAYLOAD), searches=2))
    pack = await build_fact_pack(_topic(), client=client)

    assert pack is not None
    assert pack.fact_count == 3
    assert pack.searches == 2
    kwargs = client.responses.create.await_args.kwargs
    assert kwargs["tools"] == [{"type": "web_search"}]
    assert kwargs["model"] == "gpt-4o"
    # The story's own title / URL / summary reach the researcher.
    assert "Regulator lifts the reporting threshold" in kwargs["input"]
    assert "https://www.reuters.com/markets/story" in kwargs["input"]
    assert "1 January 2027" in kwargs["input"]


async def test_budget_reaches_the_call_and_the_prompt():
    client = _client(_resp(json.dumps(_GOOD_PAYLOAD)))
    budget = ResearchBudget(max_sources=3, max_tokens=1234, timeout_seconds=45)
    await build_fact_pack(_topic(), budget=budget, client=client)

    kwargs = client.responses.create.await_args.kwargs
    assert kwargs["max_output_tokens"] == 1234
    assert kwargs["timeout"] == 45.0
    assert "at most 3 distinct outlets" in kwargs["instructions"]


def test_budget_from_config_reads_the_pipeline_config_row():
    class Row:
        research_max_sources = 7
        research_max_tokens = 3000
        research_timeout_seconds = 90

    budget = ResearchBudget.from_config(Row())
    assert (budget.max_sources, budget.max_tokens, budget.timeout_seconds) == (
        7,
        3000,
        90,
    )


def test_budget_from_config_falls_back_for_a_row_predating_the_columns():
    budget = ResearchBudget.from_config(object())
    assert (budget.max_sources, budget.max_tokens, budget.timeout_seconds) == (
        5,
        2000,
        60,
    )


# --- build_fact_pack: every failure is the thin path -----------------------


async def test_empty_response_returns_none():
    client = _client(_resp(""))
    assert await build_fact_pack(_topic(), client=client) is None


async def test_malformed_json_returns_none():
    client = _client(_resp("here you go: {oops"))
    assert await build_fact_pack(_topic(), client=client) is None


async def test_all_facts_dropped_returns_none():
    payload = {"source_facts": [{"text": "X.", "url": "made up"}], "context": []}
    client = _client(_resp(json.dumps(payload)))
    assert await build_fact_pack(_topic(), client=client) is None


async def test_timeout_returns_none_and_does_not_raise():
    client = AsyncMock()

    async def _never(**_kwargs):
        await asyncio.sleep(5)

    client.responses.create = _never
    pack = await build_fact_pack(
        _topic(), budget=ResearchBudget(timeout_seconds=0), client=client
    )
    assert pack is None


async def test_api_error_returns_none_and_does_not_raise():
    client = AsyncMock()
    client.responses.create = AsyncMock(side_effect=RuntimeError("upstream is down"))
    assert await build_fact_pack(_topic(), client=client) is None


async def test_missing_api_key_returns_none_without_calling_openai(monkeypatch):
    from pipeline.common import config as config_module

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert await build_fact_pack(_topic()) is None
    monkeypatch.setattr(config_module, "_settings", None)


async def test_preview_tool_name_is_retried_once():
    """An older snapshot that only knows ``web_search_preview`` costs a retry,
    not the topic's fact pack."""
    client = AsyncMock()
    client.responses.create = AsyncMock(
        side_effect=[
            RuntimeError("Error code: 400 - unsupported tool 'web_search'"),
            _resp(json.dumps(_GOOD_PAYLOAD)),
        ]
    )
    pack = await build_fact_pack(_topic(), client=client)
    assert pack is not None and pack.fact_count == 3
    assert client.responses.create.await_count == 2
    assert client.responses.create.await_args_list[0].kwargs["tools"] == [
        {"type": "web_search"}
    ]
    assert client.responses.create.await_args_list[1].kwargs["tools"] == [
        {"type": "web_search_preview"}
    ]


async def test_a_non_tool_400_is_not_retried():
    client = AsyncMock()
    client.responses.create = AsyncMock(
        side_effect=RuntimeError("Error code: 401 - invalid api key")
    )
    assert await build_fact_pack(_topic(), client=client) is None
    assert client.responses.create.await_count == 1


# --- cost accounting -------------------------------------------------------


@pytest.fixture
def fresh_admin_db(tmp_path, monkeypatch):
    from tests.unit.conftest import seed_icon_brand

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session)
        session.commit()
    yield icon_id
    admin_db.reset_for_tests()


def _cost_rows() -> list[CostRecord]:
    with admin_db.get_session_factory()() as session:
        return list(session.scalars(select(CostRecord).order_by(CostRecord.id)).all())


async def test_research_writes_one_cost_row_including_the_search_surcharge(
    fresh_admin_db,
):
    client = _client(_resp(json.dumps(_GOOD_PAYLOAD), searches=3, tokens=(1000, 500)))
    with cost_context(CostContext(brand_id_fk=fresh_admin_db, run_id=None)):
        pack = await build_fact_pack(_topic(), client=client)

    assert pack is not None
    rows = _cost_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operation == "research"
    assert row.provider == "openai"
    assert row.model == "gpt-4o"
    assert row.tokens_in == 1000
    assert row.tokens_out == 500
    # gpt-4o: 1000 in @ $2.50/1M + 500 out @ $10.00/1M = $0.0075, plus three
    # web-search tool calls at $0.01 each — the surcharge tokens cannot show.
    assert row.cost_usd == pytest.approx(0.0075 + 0.03)
    assert row.cost_usd > 0


async def test_research_records_no_cost_row_when_the_call_fails(fresh_admin_db):
    client = AsyncMock()
    client.responses.create = AsyncMock(side_effect=RuntimeError("boom"))
    with cost_context(CostContext(brand_id_fk=fresh_admin_db)):
        assert await build_fact_pack(_topic(), client=client) is None
    assert _cost_rows() == []


async def test_research_still_records_cost_when_the_payload_is_unusable(
    fresh_admin_db,
):
    """A call that came back and billed us is recorded even though nothing
    survived the parse — otherwise a run of thin articles reads as free."""
    client = _client(_resp("{broken", searches=1, tokens=(800, 20)))
    with cost_context(CostContext(brand_id_fk=fresh_admin_db)):
        assert await build_fact_pack(_topic(), client=client) is None
    rows = _cost_rows()
    assert len(rows) == 1 and rows[0].operation == "research"
