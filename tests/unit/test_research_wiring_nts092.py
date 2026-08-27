"""IT_PROJ_NTS_092 — the fact pack reaches the prompt, and its absence is loud.

Three things are asserted here, in the order the acceptance criteria state
them:

* **The pack reaches the drafter.** Every retained fact and its URL is in the
  prompt actually sent to the model, and a missing pack renders the explicit
  "NO RESEARCH AVAILABLE, write shorter" block rather than an empty string
  that reads as "nothing to see here".
* **Failure isolation (Task B).** Research returning ``None`` — switched off,
  timed out, garbled, every fact dropped — must NOT drop the topic. The
  article still ships, ``research.failed`` is logged, the thin counter
  increments, and the run does not abort.
* **The prompt changes are real (Task C).** 600-800 words in BOTH writer_draft
  and writer_polish (polish compresses back otherwise), the H2 count
  recalibrated for the new length, ``{fact_pack}`` required, and
  ``writer_translate`` untouched.

No test in this module makes a network call.
"""

# ruff: noqa: F811 — importing a pytest fixture by name IS the redefinition
# ruff sees; the fixture is shared with test_run_pipeline_fanout on purpose.

from __future__ import annotations

import asyncio
import json
import re
import string
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.models import PipelineConfig, Run
from pipeline.common.models import Language, RawItem, Topic
from pipeline.generator.comment_writer import (
    _DRAFT_MAX_TOKENS,
    _DRAFT_PROMPT,
    _POLISH_MAX_TOKENS,
    _POLISH_PROMPT,
    _REQUIRED_PLACEHOLDERS,
    _TRANSLATE_MAX_TOKENS,
    _TRANSLATE_PROMPT,
    CommentWriter,
)
from pipeline.generator.research import NO_FACT_PACK, Fact, FactPack
from pipeline.monitoring.alerts import format_run_finished
from tests.unit.test_run_pipeline_fanout import (  # noqa: F401 — fixture import
    _mock_externals,
    _set_brand_languages,
    fresh_admin_db_with_source,
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# --- helpers --------------------------------------------------------------


class _LogRecorder:
    """Stand-in for ``run.log`` recording ``(level, event, kwargs)``.

    Asserting on the module logger rather than structlog's global config
    keeps the test honest: ``run_pipeline`` calls ``configure_logging()``,
    which would wipe a global capture.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    def _record(self, level: str):
        def emit(event: str, **kw) -> None:
            self.entries.append((level, event, kw))

        return emit

    def __getattr__(self, level: str):
        return self._record(level)

    def events(self, level: str | None = None) -> list[str]:
        return [e for lv, e, _ in self.entries if level in (None, lv)]

    def kwargs_for(self, event: str) -> list[dict]:
        return [kw for _, e, kw in self.entries if e == event]


def _topic() -> Topic:
    return Topic(
        id="t-1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="Reuters",
            url="https://www.reuters.com/x",
            title="Acme raises a fund",
            summary="Acme raised five million.",
        ),
        relevance_score=8.0,
    )


def _pack() -> FactPack:
    return FactPack(
        source_facts=[
            Fact(
                text="Acme closed a $5m Series B on 12 August 2026.",
                url="https://www.reuters.com/acme-series-b",
                publisher="Reuters",
                date="2026-08-12",
            ),
            Fact(
                text="The fund's hurdle rate is 8%.",
                url="https://www.ft.com/content/acme",
                publisher="Financial Times",
            ),
        ],
        context=[
            Fact(
                text="Comparable 2025 vehicles closed at a 7% hurdle.",
                url="https://www.preqin.com/insights/2025",
                publisher="Preqin",
            )
        ],
        angle_hints=["An 8% hurdle reprices the mezzanine tranche."],
        citations=[
            "https://www.reuters.com/acme-series-b",
            "https://www.ft.com/content/acme",
            "https://www.preqin.com/insights/2025",
        ],
    )


def _resp(payload: dict[str, Any]) -> Any:
    msg = type("M", (), {"content": json.dumps(payload)})()
    choice = type("C", (), {"message": msg})()
    return type("R", (), {"choices": [choice], "usage": None})()


_DRAFT_OUT = {"title": "T", "body": "## A\n\nAcme raised $5m.", "key_takeaway": "K"}
_POLISH_OUT = {
    "title": "T",
    "body": (
        "Acme raised $5m this quarter.\n\n"
        "## The raise\n\n"
        "Acme allocated 5m across three funds.\n\n"
        "Acme's 5m now reprices its mezzanine book."
    ),
    "key_takeaway": "K",
}


def _writer() -> tuple[CommentWriter, AsyncMock]:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_resp(_DRAFT_OUT), _resp(_POLISH_OUT)]
    )
    return CommentWriter(client=client), client


def _draft_prompt_sent(client: AsyncMock) -> str:
    return client.chat.completions.create.await_args_list[0].kwargs["messages"][0][
        "content"
    ]


# --- the pack reaches the drafter -----------------------------------------


async def test_every_fact_and_url_reaches_the_draft_prompt():
    writer, client = _writer()
    pack = _pack()
    await writer.write(_topic(), "banned_phrases: []\n", Language.en, fact_pack=pack)

    prompt = _draft_prompt_sent(client)
    for fact in [*pack.source_facts, *pack.context]:
        assert fact.text in prompt
        assert fact.url in prompt
    assert pack.angle_hints[0] in prompt
    assert "RESEARCH FACT PACK" in prompt
    # The thin-path block is NOT rendered. (The GROUNDING rules legitimately
    # quote its wording, so match the block itself, not the phrase.)
    assert NO_FACT_PACK not in prompt


async def test_missing_pack_renders_the_explicit_thin_block():
    writer, client = _writer()
    await writer.write(_topic(), "banned_phrases: []\n", Language.en)

    prompt = _draft_prompt_sent(client)
    assert NO_FACT_PACK in prompt
    assert "write a SHORTER" in prompt or "SHORTER" in prompt


async def test_an_empty_pack_is_treated_exactly_like_a_missing_one():
    writer, client = _writer()
    await writer.write(
        _topic(), "banned_phrases: []\n", Language.en, fact_pack=FactPack()
    )
    assert NO_FACT_PACK in _draft_prompt_sent(client)


async def test_write_does_not_research_on_its_own():
    """The pack is built once per TOPIC, upstream. ``write`` runs once per
    LANGUAGE, so researching here would pay four times for one article."""
    import pipeline.generator.research as research_mod

    calls: list[str] = []

    async def _trap(*args, **kwargs):  # pragma: no cover — must not run
        calls.append("called")
        raise AssertionError("CommentWriter.write built its own fact pack")

    original = research_mod.build_fact_pack
    research_mod.build_fact_pack = _trap
    try:
        writer, _client = _writer()
        await writer.write(_topic(), "banned_phrases: []\n", Language.en)
    finally:
        research_mod.build_fact_pack = original
    assert calls == []


# --- Task C: the prompt constants -----------------------------------------


@pytest.mark.parametrize(
    ("name", "prompt"), [("writer_draft", _DRAFT_PROMPT), ("writer_polish", _POLISH_PROMPT)]
)
def test_both_en_prompts_carry_the_new_length(name, prompt):
    """Both, not one: polish compresses the draft back otherwise."""
    assert "600-800 words" in prompt, f"{name} missing the new length"
    assert "250-400" not in prompt, f"{name} still carries the old length"


@pytest.mark.parametrize(
    ("name", "prompt"), [("writer_draft", _DRAFT_PROMPT), ("writer_polish", _POLISH_PROMPT)]
)
def test_h2_count_was_recalibrated_for_the_longer_piece(name, prompt):
    """2-3 headings was calibrated for 350 words and is too sparse for 700."""
    assert "3-5 H2 sections" in prompt, f"{name} H2 count not recalibrated"
    assert "2-3 H2" not in prompt, f"{name} still asks for 2-3 H2"


def test_draft_prompt_has_an_unambiguous_grounding_block():
    flat = _flat(_DRAFT_PROMPT)
    assert "GROUNDING (mandatory" in _DRAFT_PROMPT
    # …and it outranks everything, length included.
    assert "outranks EVERY other rule below, including length" in flat
    # the three named prohibitions
    assert "Never invent one." in _DRAFT_PROMPT
    assert "Never extrapolate one" in _DRAFT_PROMPT
    assert '"sensibly round"' in _DRAFT_PROMPT
    # only two permitted sources, and memory is not one of them
    assert "RESEARCH FACT PACK or in the news peg" in flat
    assert "There is no third source." in _DRAFT_PROMPT
    assert "Your own knowledge is NOT a source." in _DRAFT_PROMPT


def test_thin_pack_means_a_shorter_article_not_a_padded_one():
    """The one instruction that keeps 600-800 from becoming a filler licence."""
    flat = _flat(_DRAFT_PROMPT)
    assert "write a SHORTER article" in flat
    assert "A padded 700-word piece is a failure" in flat
    assert "never an instruction to keep writing" in flat
    # polish must not undo it by padding back up to range
    assert "STAYS short" in _POLISH_PROMPT
    assert "in order to reach a word count" in _flat(_POLISH_PROMPT)


def test_polish_may_not_add_facts_to_fill_the_extra_words():
    flat = _flat(_POLISH_PROMPT)
    assert "GROUNDING (mandatory" in _POLISH_PROMPT
    assert "You may not ADD facts." in _POLISH_PROMPT
    assert "The draft below is your ONLY source." in flat


def test_fact_pack_is_a_required_placeholder_of_writer_draft():
    assert _REQUIRED_PLACEHOLDERS["writer_draft"] == {
        "voice_profile_yaml",
        "title",
        "summary",
        "language_name",
        "banned_phrases",
        "fact_pack",
    }
    fields = {n for _, n, _, _ in string.Formatter().parse(_DRAFT_PROMPT) if n}
    assert _REQUIRED_PLACEHOLDERS["writer_draft"] <= fields


def test_writer_translate_is_untouched():
    """NTS_065's faithfulness holds the non-EN length and H2 count. Weakening
    it is how RU/UK/PL drift and invent."""
    assert _REQUIRED_PLACEHOLDERS["writer_translate"] == {
        "draft_json",
        "language_name",
        "banned_phrases",
        "good_examples",
    }
    assert "{fact_pack}" not in _TRANSLATE_PROMPT
    assert "600-800" not in _TRANSLATE_PROMPT
    flat = _flat(_TRANSLATE_PROMPT)
    assert "ABSOLUTE FIDELITY RULES" in _TRANSLATE_PROMPT
    assert "within roughly ±15% of the source" in flat
    assert "the SAME number of H2 sections in the SAME order" in flat
    assert "the SAME numeric value" in flat


def test_output_ceilings_were_raised_with_the_word_target():
    """1,500 tokens was sized for a 400-word piece; an 800-word one truncates
    mid-body and lands in the "(parse failed)" path."""
    assert _DRAFT_MAX_TOKENS >= 2400
    assert _POLISH_MAX_TOKENS >= 2400
    # Slavic targets run longer in words AND cost more tokens per word.
    assert _TRANSLATE_MAX_TOKENS > _POLISH_MAX_TOKENS


async def test_the_raised_ceilings_are_what_the_api_is_called_with():
    writer, client = _writer()
    await writer.write(_topic(), "banned_phrases: []\n", Language.en, fact_pack=_pack())
    calls = client.chat.completions.create.await_args_list
    assert calls[0].kwargs["max_tokens"] == _DRAFT_MAX_TOKENS
    assert calls[1].kwargs["max_tokens"] == _POLISH_MAX_TOKENS


# --- Task B: failure isolation, end to end --------------------------------


def _set_research(brand_id: int, *, enabled: bool) -> None:
    factory = admin_db.get_session_factory()
    with factory() as session:
        cfg = session.get(PipelineConfig, brand_id)
        assert cfg is not None
        cfg.research_enabled = enabled
        session.commit()


def _run_stats(session) -> dict:
    runs = list(session.scalars(select(Run)))
    assert len(runs) == 1
    return json.loads(runs[0].stats)


def _break_research(monkeypatch) -> None:
    """Force ``build_fact_pack_for_topic`` to return None, the way a timeout
    or a garbled payload does."""
    from pipeline import run as pipe

    async def _no_pack(topic, *, research_enabled=True, budget=None):
        return None

    monkeypatch.setattr(pipe, "build_fact_pack_for_topic", _no_pack)


def test_research_runs_once_per_topic_not_once_per_language(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """Same economics as the shared cover: four language siblings, one pack."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    assert len(fake_sanity.created) == 8
    assert fake_sanity.research_call_log == ["t-0", "t-1"] or len(
        fake_sanity.research_call_log
    ) == 2, fake_sanity.research_call_log


def test_broken_research_still_produces_every_article(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The hard rule: research failing must not cost us a topic."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    fake_sanity = _mock_externals(monkeypatch)
    _break_research(monkeypatch)

    from pipeline.run import run_pipeline

    results = asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    # 2 topics × 4 languages, all published, none failed.
    assert len(fake_sanity.created) == 8
    assert [r for r in results if r.get("status") == "failed"] == []
    with admin_db.get_session_factory()() as session:
        stats = _run_stats(session)
        assert stats["drafted"] == 8
        assert stats["errors"] == 0
        assert stats["thin"] == 2  # per topic, not per language
        runs = list(session.scalars(select(Run)))
        assert runs[0].status == "success"  # the run does NOT abort


def test_broken_research_logs_research_failed_with_a_reason(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    from pipeline import run as pipe

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    _mock_externals(monkeypatch)
    _break_research(monkeypatch)

    recorder = _LogRecorder()
    monkeypatch.setattr(pipe, "log", recorder)
    asyncio.run(pipe.run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    assert recorder.events("warning").count("research.failed") == 2
    for kw in recorder.kwargs_for("research.failed"):
        assert kw.get("reason")
        assert kw.get("topic")
    # A thin article is not an error — it must not masquerade as one.
    assert "topic.failed" not in recorder.events()


def test_an_exception_escaping_the_research_seam_is_still_only_thin(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """build_fact_pack swallows its own failures, so reaching the paranoia net
    means something unexpected exploded. Still not a reason to lose the topic."""
    from pipeline import run as pipe

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    fake_sanity = _mock_externals(monkeypatch)

    async def _explode(topic, *, research_enabled=True, budget=None):
        raise RuntimeError("openai client blew up in a new and exciting way")

    monkeypatch.setattr(pipe, "build_fact_pack_for_topic", _explode)
    recorder = _LogRecorder()
    monkeypatch.setattr(pipe, "log", recorder)

    asyncio.run(pipe.run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    assert len(fake_sanity.created) == 2
    reasons = {kw.get("reason") for kw in recorder.kwargs_for("research.failed")}
    assert "unexpected_error" in reasons


def test_research_disabled_skips_the_call_and_still_counts_thin(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """Switched off is a deliberate state, but the articles are still thin and
    must still say so — a quiet downgrade is the failure mode being avoided."""
    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    fake_sanity = _mock_externals(monkeypatch)
    _set_research(icon_id, enabled=False)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    assert len(fake_sanity.created) == 2
    with admin_db.get_session_factory()() as session:
        assert _run_stats(session)["thin"] == 2


def test_a_working_pack_counts_no_thin_articles(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The mirror image: nothing claims thinness when research worked."""
    from pipeline import run as pipe

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    _mock_externals(monkeypatch)
    recorder = _LogRecorder()
    monkeypatch.setattr(pipe, "log", recorder)

    asyncio.run(pipe.run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    with admin_db.get_session_factory()() as session:
        assert _run_stats(session)["thin"] == 0
    assert "research.failed" not in recorder.events()
    assert recorder.events("info").count("research.fact_pack") == 2


def test_the_pack_reaches_the_en_draft_seam_and_only_it(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """The canonical EN draft gets the pack; the translations get the EN draft
    (NTS_065), never a second research call."""
    from pipeline import run as pipe

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en", "ru", "uk", "pl"])
    _mock_externals(monkeypatch)

    seen: list[Any] = []
    # The fixture already replaced the seam with a fake; wrap THAT, so the
    # spy observes what the orchestrator actually passes.
    faked = pipe.generate_draft_for_language

    async def spy(topic, brand, language, fact_pack=None):
        seen.append((language.value, fact_pack))
        return await faked(topic, brand, language, fact_pack=fact_pack)

    monkeypatch.setattr(pipe, "generate_draft_for_language", spy)
    asyncio.run(pipe.run_pipeline(brand_slug="icon", limit=1, dry_run=False))

    assert [lang for lang, _ in seen] == ["en"]
    assert seen[0][1] is not None
    assert seen[0][1].fact_count == 1


# --- the counter travels to the operator ----------------------------------


def test_thin_count_travels_from_the_run_all_the_way_to_telegram(
    fresh_admin_db_with_source, monkeypatch
) -> None:
    """A run where research is broken has to be loudly visible, not quietly
    worse — which means the number has to survive the whole chain."""
    from pipeline.monitoring.alerts import _gather_run_events

    icon_id = fresh_admin_db_with_source["icon_id"]
    _set_brand_languages(icon_id, ["en"])
    _mock_externals(monkeypatch)
    _break_research(monkeypatch)

    from pipeline.run import run_pipeline

    asyncio.run(run_pipeline(brand_slug="icon", limit=2, dry_run=False))

    with admin_db.get_session_factory()() as session:
        assert _run_stats(session)["thin"] == 2

    messages = [msg for _key, msg in _gather_run_events(set())]
    finished = [m for m in messages if "Прогон завершён" in m]
    assert finished, messages
    assert "thin" in finished[0]
    assert "2" in finished[0]


def test_summary_says_nothing_about_thinness_when_there_is_none():
    text = format_run_finished(
        run_id=1,
        status="success",
        fetched=10,
        relevant=3,
        drafted=3,
        finished_at=__import__("datetime").datetime(2026, 8, 27, 10, 0),
        thin=0,
    )
    assert "thin" not in text


def test_summary_reports_thin_articles_when_there_are_some():
    text = format_run_finished(
        run_id=1,
        status="success",
        fetched=10,
        relevant=3,
        drafted=3,
        finished_at=__import__("datetime").datetime(2026, 8, 27, 10, 0),
        thin=3,
    )
    assert "thin" in text
    assert "3" in text
