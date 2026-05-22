"""Tests for cost_records — schema + every paid call site.

Covers NTS_025 C1 ("every paid call writes one row"). Each call site
test mocks the underlying provider so we don't hit OpenAI / Replicate
in CI.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, select

from pipeline.admin import db as admin_db
from pipeline.admin.config_client import AdminConfigClient
from pipeline.admin.cost_recorder import CostContext, cost_context, record_cost
from pipeline.admin.models import Brand, CostRecord
from pipeline.common import config as config_module
from pipeline.common.pricing import openai_cost, replicate_image_cost
from tests.unit.conftest import seed_icon_brand


# ----- fixtures ----------------------------------------------------------


@pytest.fixture
def fresh_admin_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    factory = admin_db.get_session_factory()
    with factory() as session:
        icon_id = seed_icon_brand(session)
        session.commit()
    yield {"icon_id": icon_id}
    admin_db.reset_for_tests()


def _all_costs() -> list[CostRecord]:
    factory = admin_db.get_session_factory()
    with factory() as s:
        return s.scalars(select(CostRecord).order_by(CostRecord.id)).all()


# ----- schema ------------------------------------------------------------


def test_cost_records_table_exists(fresh_admin_db) -> None:
    factory = admin_db.get_session_factory()
    with factory() as s:
        names = set(inspect(s.bind).get_table_names())
    assert "cost_records" in names


def test_cost_records_indexes_exist(fresh_admin_db) -> None:
    factory = admin_db.get_session_factory()
    with factory() as s:
        idxs = {idx["name"] for idx in inspect(s.bind).get_indexes("cost_records")}
    assert "ix_cost_records_brand_created" in idxs
    assert "ix_cost_records_run_id" in idxs
    assert "ix_cost_records_topic_id" in idxs
    assert "ix_cost_records_draft_id" in idxs


# ----- AdminConfigClient.record_cost (the bottom-half) -------------------


def test_record_cost_inserts_row(fresh_admin_db) -> None:
    icon_id = fresh_admin_db["icon_id"]
    AdminConfigClient.record_cost(
        brand_id_fk=icon_id,
        provider="openai",
        operation="topic_scoring",
        model="gpt-4o-mini",
        tokens_in=120,
        tokens_out=15,
        cost_usd=0.000027,
    )
    rows = _all_costs()
    assert len(rows) == 1
    row = rows[0]
    assert row.brand_id_fk == icon_id
    assert row.provider == "openai"
    assert row.operation == "topic_scoring"
    assert row.tokens_in == 120
    assert row.tokens_out == 15
    assert row.cost_usd == pytest.approx(0.000027)


def test_record_cost_noop_when_db_missing(tmp_path, monkeypatch) -> None:
    """If admin.db is unreachable, record_cost returns None and does NOT raise."""
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "absent.db"))
    admin_db.reset_for_tests()
    result = AdminConfigClient.record_cost(
        brand_id_fk=1, provider="openai", operation="topic_scoring", cost_usd=0.01
    )
    assert result is None


# ----- cost_recorder (the context-var wrapper) ---------------------------


def test_record_cost_via_context_writes_row(fresh_admin_db) -> None:
    icon_id = fresh_admin_db["icon_id"]
    with cost_context(CostContext(brand_id_fk=icon_id)):
        record_cost(
            provider="openai",
            operation="draft",
            model="gpt-4o-mini",
            tokens_in=200,
            tokens_out=350,
            cost_usd=0.00024,
        )
    rows = _all_costs()
    assert len(rows) == 1
    assert rows[0].brand_id_fk == icon_id
    assert rows[0].cost_usd == pytest.approx(0.00024)


def test_record_cost_without_context_is_noop(fresh_admin_db) -> None:
    """When no context is set, record_cost silently skips the write."""
    record_cost(
        provider="openai",
        operation="draft",
        cost_usd=0.0,
    )
    assert _all_costs() == []


# ----- call-site coverage (one test per LLM call site) -------------------


def _chat_resp_with_usage(content: str, prompt_tokens=100, completion_tokens=50) -> Any:
    class _Usage:
        def __init__(self, p: int, c: int) -> None:
            self.prompt_tokens = p
            self.completion_tokens = c

    class _Msg:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Choice:
        def __init__(self, content: str) -> None:
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content: str, p: int, c: int) -> None:
            self.choices = [_Choice(content)]
            self.usage = _Usage(p, c)

    return _Resp(content, prompt_tokens, completion_tokens)


def test_topic_picker_records_cost(fresh_admin_db) -> None:
    from pipeline.selector.topic_picker import BrandContext, TopicPicker
    from pipeline.common.models import RawItem

    icon_id = fresh_admin_db["icon_id"]
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_chat_resp_with_usage('{"score": 8, "reason": "ok"}', 60, 8)
    )
    picker = TopicPicker(client=fake_client, model="gpt-4o-mini")

    async def go() -> None:
        with cost_context(CostContext(brand_id_fk=icon_id)):
            await picker.score(
                RawItem(
                    source_id="s",
                    source_name="s",
                    url="https://example.com/x",
                    title="t",
                    summary="s",
                ),
                BrandContext(
                    brand_id="icon",
                    name="Icon",
                    topics_relevant=["x"],
                    topics_banned=["y"],
                ),
            )

    asyncio.run(go())

    rows = _all_costs()
    assert len(rows) == 1
    assert rows[0].operation == "topic_scoring"
    assert rows[0].provider == "openai"
    assert rows[0].tokens_in == 60
    assert rows[0].tokens_out == 8
    assert rows[0].cost_usd == pytest.approx(openai_cost("gpt-4o-mini", 60, 8))


def test_comment_writer_draft_and_polish_record_two_costs(fresh_admin_db) -> None:
    from pipeline.generator.comment_writer import CommentWriter
    from pipeline.common.models import Language, RawItem, Topic

    icon_id = fresh_admin_db["icon_id"]
    draft_payload = '{"title": "T", "body": "## H\\n\\nbody.", "key_takeaway": "k"}'
    polish_payload = (
        '{"title": "P", "body": "## H\\n\\nclean body.", "key_takeaway": "k"}'
    )
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=[
            _chat_resp_with_usage(draft_payload, 300, 200),
            _chat_resp_with_usage(polish_payload, 350, 200),
        ]
    )
    writer = CommentWriter(client=fake_client)

    async def go() -> None:
        with cost_context(CostContext(brand_id_fk=icon_id)):
            await writer.write(
                Topic(
                    id="t",
                    brand_id="icon",
                    raw=RawItem(
                        source_id="s",
                        source_name="s",
                        url="https://example.com/x",
                        title="t",
                    ),
                    relevance_score=8.0,
                ),
                voice_profile_yaml="banned_phrases:\n  - never\n",
                language=Language.en,
            )

    asyncio.run(go())

    rows = _all_costs()
    operations = [r.operation for r in rows]
    assert "draft" in operations
    assert "polish" in operations
    assert all(r.provider == "openai" for r in rows)


def test_comment_writer_records_anti_check_retry_when_fired(fresh_admin_db) -> None:
    from pipeline.generator.comment_writer import CommentWriter
    from pipeline.common.models import Language, RawItem, Topic

    icon_id = fresh_admin_db["icon_id"]
    draft_payload = '{"title": "T", "body": "## H\\n\\nbody.", "key_takeaway": "k"}'
    dirty_polish = (
        '{"title": "P", "body": '
        '"Moreover, A. Furthermore, B. In conclusion, C.", "key_takeaway": "k"}'
    )
    clean_retry = '{"title": "R", "body": "## H\\n\\nClean.", "key_takeaway": "k"}'
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=[
            _chat_resp_with_usage(draft_payload, 300, 200),
            _chat_resp_with_usage(dirty_polish, 350, 200),
            _chat_resp_with_usage(clean_retry, 400, 220),
        ]
    )
    writer = CommentWriter(client=fake_client)

    voice_yaml = (
        "banned_phrases:\n"
        "  - moreover\n"
        "  - furthermore\n"
        "  - in conclusion\n"
    )

    async def go() -> None:
        with cost_context(CostContext(brand_id_fk=icon_id)):
            await writer.write(
                Topic(
                    id="t",
                    brand_id="icon",
                    raw=RawItem(
                        source_id="s",
                        source_name="s",
                        url="https://example.com/x",
                        title="t",
                    ),
                    relevance_score=8.0,
                ),
                voice_profile_yaml=voice_yaml,
                language=Language.en,
            )

    asyncio.run(go())

    operations = sorted({r.operation for r in _all_costs()})
    assert "anti_check_retry" in operations  # retry path recorded


def test_image_generator_records_replicate_cost(fresh_admin_db, monkeypatch) -> None:
    from pipeline.generator import image as image_mod
    from pipeline.generator.image import BrandVisual, ImageGenerator
    from pipeline.common.models import RawItem, Topic

    icon_id = fresh_admin_db["icon_id"]

    class FakeReplicate:
        async def async_run(self, model, input):  # noqa: ANN001
            return "https://cdn.replicate.com/fake.png"

    # Skip the constructor token check.
    gen = ImageGenerator.__new__(ImageGenerator)
    gen.model = "black-forest-labs/flux-1.1-pro"
    gen._client = FakeReplicate()  # noqa: SLF001

    async def go() -> None:
        with cost_context(CostContext(brand_id_fk=icon_id)):
            await gen.generate(
                Topic(
                    id="t",
                    brand_id="icon",
                    raw=RawItem(
                        source_id="s",
                        source_name="s",
                        url="https://x",
                        title="title",
                    ),
                    relevance_score=10.0,
                ),
                BrandVisual(brand_id="icon", image_style_prompts=["minimal style"]),
            )

    asyncio.run(go())

    rows = _all_costs()
    assert len(rows) == 1
    assert rows[0].provider == "replicate"
    assert rows[0].operation == "image_master"
    assert rows[0].cost_usd == pytest.approx(
        replicate_image_cost("black-forest-labs/flux-1.1-pro")
    )
    assert rows[0].duration_seconds is not None and rows[0].duration_seconds >= 0


def test_image_generate_with_image_regenerate_operation(fresh_admin_db) -> None:
    """ImageGenerator.generate(operation='image_regenerate') records under
    that op so /drafts/{id}/regenerate-image costs are distinguishable."""
    from pipeline.generator.image import BrandVisual, ImageGenerator
    from pipeline.common.models import RawItem, Topic

    icon_id = fresh_admin_db["icon_id"]

    class FakeReplicate:
        async def async_run(self, model, input):  # noqa: ANN001
            return "https://cdn.replicate.com/fake.png"

    gen = ImageGenerator.__new__(ImageGenerator)
    gen.model = "black-forest-labs/flux-1.1-pro"
    gen._client = FakeReplicate()  # noqa: SLF001

    async def go() -> None:
        with cost_context(CostContext(brand_id_fk=icon_id, draft_id="drafts.post-abc")):
            await gen.generate(
                Topic(
                    id="t",
                    brand_id="icon",
                    raw=RawItem(
                        source_id="s", source_name="s", url="https://x", title="t"
                    ),
                    relevance_score=10.0,
                ),
                BrandVisual(brand_id="icon", image_style_prompts=["x"]),
                operation="image_regenerate",
            )

    asyncio.run(go())
    rows = _all_costs()
    assert len(rows) == 1
    assert rows[0].operation == "image_regenerate"
    assert rows[0].draft_id == "drafts.post-abc"


def test_prompt_test_records_cost_with_brand_context(fresh_admin_db, monkeypatch) -> None:
    """``llm.run_prompt_test`` writes a cost row when brand_id_fk is passed."""
    from pipeline.admin import llm as llm_mod

    icon_id = fresh_admin_db["icon_id"]

    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_chat_resp_with_usage("OK", 80, 30)
    )
    # Patch AsyncOpenAI so run_prompt_test uses our fake client.
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: fake_client)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(config_module, "_settings", None)

    async def go() -> None:
        await llm_mod.run_prompt_test(
            prompt_type="writer_polish",
            prompt_content="Polish: {title}",
            sample_topic={
                "title": "India credit funds",
                "summary": "summary",
                "url": "https://example.com",
            },
            brand_id_fk=icon_id,
        )

    asyncio.run(go())

    rows = _all_costs()
    assert len(rows) == 1
    assert rows[0].operation == "prompt_test"
    assert rows[0].brand_id_fk == icon_id


def test_prompt_test_without_brand_context_does_not_record(fresh_admin_db, monkeypatch) -> None:
    from pipeline.admin import llm as llm_mod

    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_chat_resp_with_usage("OK", 80, 30)
    )
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: fake_client)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(config_module, "_settings", None)

    async def go() -> None:
        await llm_mod.run_prompt_test(
            prompt_type="writer_polish",
            prompt_content="Polish",
            sample_topic={
                "title": "x",
                "summary": "y",
                "url": "https://x",
            },
            # brand_id_fk explicitly None
        )

    asyncio.run(go())
    assert _all_costs() == []


# ----- pricing module ----------------------------------------------------


def test_openai_cost_known_model() -> None:
    cost = openai_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    # $0.15 + $0.60 per million.
    assert cost == pytest.approx(0.75)


def test_openai_cost_unknown_model_returns_zero() -> None:
    assert openai_cost("not-a-real-model", 1000, 1000) == 0.0


def test_replicate_image_cost_known_model() -> None:
    assert replicate_image_cost("black-forest-labs/flux-1.1-pro") > 0


def test_replicate_image_cost_unknown_model_returns_zero() -> None:
    assert replicate_image_cost("not-a-real-image-model") == 0.0


def test_cost_record_run_id_set_null_on_run_delete(fresh_admin_db) -> None:
    """When a Run is deleted, cost_records.run_id detaches (SET NULL) so the
    historical cost survives. The cost row itself stays (RESTRICT on brand)."""
    import json as _json
    from datetime import datetime, timezone

    from pipeline.admin.models import Run

    icon_id = fresh_admin_db["icon_id"]
    factory = admin_db.get_session_factory()
    with factory() as session:
        run = Run(
            brand_id_fk=icon_id,
            triggered_by="manual",
            source_ids=_json.dumps([]),
            started_at=datetime.now(tz=timezone.utc),
            status="success",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    AdminConfigClient.record_cost(
        brand_id_fk=icon_id,
        run_id=run_id,
        provider="openai",
        operation="draft",
        cost_usd=0.5,
    )

    # Delete the run.
    with factory() as session:
        session.delete(session.get(Run, run_id))
        session.commit()

    rows = _all_costs()
    assert len(rows) == 1
    assert rows[0].run_id is None  # detached, not cascaded
    assert rows[0].cost_usd == pytest.approx(0.5)
