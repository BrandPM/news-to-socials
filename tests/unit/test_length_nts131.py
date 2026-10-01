"""NTS_131 P3 Ф4 — where the length collapsed, and the fix at each stage.

Measured on the first live cluster (FINMA / Julius Baer, 38 facts): the plan
assigned 6 facts; with 18 assigned the chat-model draft stopped near 400
words; with a reasoning-model draft at ~1 300 words, the polish cut it to 771.
Each stage is pinned here without a model call.
"""

from __future__ import annotations

import json

import pytest

from pipeline.common.models import Draft, Language, RawItem, Topic
from pipeline.generator import comment_writer as cw
from pipeline.generator import composition as comp
from pipeline.generator.research import Fact, FactPack

# --------------------------------------------------------------------------
# the plan: a fact budget, and one re-ask when it under-spends
# --------------------------------------------------------------------------


def test_the_fact_budget_follows_depth_and_never_exceeds_the_pack():
    assert comp.plan_fact_budget("deep", 38) == (16, 22)
    assert comp.plan_fact_budget("article", 38) == (12, 16)
    assert comp.plan_fact_budget("article", 9) == (9, 9)
    assert comp.plan_fact_budget("note", 38) == (0, 0)


def _plan_json(n_facts: int) -> str:
    return json.dumps(
        {
            "sections": [
                {
                    "heading": f"Section {i}",
                    "move": "changes",
                    "facts": [f"fact {i}-{j}" for j in range(n_facts // 4)],
                }
                for i in range(4)
            ]
        }
    )


class _Planner:
    def __init__(self, answers: list[str]):
        self.answers = answers
        self.calls: list[list[dict]] = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        content = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]

        class _R:
            usage = None

            class _C:
                class message:  # noqa: N801
                    pass

            choices = [_C()]

        _R.choices[0].message.content = content
        return _R()


def _pack(n: int) -> FactPack:
    return FactPack(
        source_facts=[Fact(text=f"Fact {i} is EUR {i}m", url="https://reg.eu/d") for i in range(n)]
    )


async def test_a_plan_that_underspends_the_pack_is_re_asked_once():
    planner = _Planner([_plan_json(4), _plan_json(20)])
    plan = await comp.build_plan(
        title="t", summary="s", fact_pack=_pack(38), depth="deep", client=planner
    )
    assert len(planner.calls) == 2
    assert "assigns 4 facts" in planner.calls[1][-1]["content"]
    assert plan.assigned_facts() == 20


async def test_a_plan_inside_its_budget_costs_one_call():
    planner = _Planner([_plan_json(16)])
    plan = await comp.build_plan(
        title="t", summary="s", fact_pack=_pack(38), depth="deep", client=planner
    )
    assert len(planner.calls) == 1
    assert plan.assigned_facts() == 16
    assert "assign 16-22 distinct facts" in planner.calls[0][0]["content"]


def test_article_and_deep_guidance_specify_the_section():
    text = comp.depth_guidance("deep", {"deep": (900, 1300)})
    assert "SECTION SHAPE" in text and "2-3 paragraphs" in text
    assert "SECTION SHAPE" not in comp.depth_guidance("brief", {"brief": (300, 400)})


# --------------------------------------------------------------------------
# the draft model and the polish
# --------------------------------------------------------------------------


def test_reasoning_models_get_a_completion_budget_not_max_tokens():
    assert cw._limit_kwargs("gpt-5.5", 8000) == {
        "max_completion_tokens": 16000,
        "reasoning_effort": "medium",
    }
    assert cw._limit_kwargs("gpt-4o", 8000) == {"max_tokens": 8000}
    assert cw.CommentWriter.__init__.__defaults__[1] == cw.DRAFT_MODEL


def test_the_polish_is_told_the_length_it_must_keep():
    lock = cw._length_lock("word " * 1000)
    assert "1000 words" in lock and "900-1050" in lock
    assert cw._length_lock("short text") == ""


class _Writer(cw.CommentWriter):
    """Draft and polish stubbed; everything between them is the real code."""

    def __init__(self, draft_words: int, polish_words: int):
        self.client = None
        self.draft_model = "gpt-5.5"
        self.polish_model = "gpt-4o"
        self.brand_id_fk = None
        self._d, self._p = draft_words, polish_words

    async def _draft(self, *a, **k):
        return cw._DraftJSON(title="T", body="## H\n" + "draft " * self._d, key_takeaway="k")

    async def _polish(self, *a, **k):
        return cw._DraftJSON(title="T", body="## H\n" + "polished " * self._p, key_takeaway="k")


def _topic() -> Topic:
    return Topic(
        id="t1",
        brand_id="icon",
        raw=RawItem(source_id="1", source_name="FINMA", url="https://www.finma.ch/x", title="t", summary="s"),
        relevance_score=5.0,
    )


@pytest.mark.parametrize(
    ("polish_words", "kept"), [(1100, "polished"), (700, "draft")]
)
async def test_a_polish_that_summarises_is_not_used(monkeypatch, polish_words, kept):
    monkeypatch.setattr(cw, "close_lacks_anchor", lambda body: False)
    writer = _Writer(draft_words=1300, polish_words=polish_words)
    out: Draft = await writer.write(_topic(), "", Language.en)
    assert out.body.split()[2] == kept


# --------------------------------------------------------------------------
# what the first cluster run surfaced on the way to the Studio
# --------------------------------------------------------------------------


def test_a_markdown_table_becomes_a_list_with_the_same_cells():
    md = "Lede.\n\n| Item | Figure | Why |\n|---|---:|---|\n| Cap | 100% | more |\n| Floor | GBP 1m | |\n\nAfter."
    out = comp.tables_to_lists(md)
    assert "|" not in out
    assert "- **Cap** — figure: 100%; why: more" in out
    assert "- **Floor** — figure: GBP 1m" in out
    assert out.startswith("Lede.") and out.endswith("After.")


def test_the_check_reads_the_article_a_few_sections_at_a_time():
    sections = "".join(f"## S{i}\n\n" + "word " * 200 + "\n\n" for i in range(5))
    chunks = comp.split_for_attribution("Title\n\nLede.\n\n" + sections)
    assert len(chunks) == 5 // 2 + 1
    assert chunks[0].startswith("Title")
    assert all(c.count("## ") <= 2 for c in chunks)


def test_a_takeaway_is_never_cut_mid_word():
    from pipeline.publisher.sanity import _clip

    text = "The ruling reprices PEP files. " + "Clients must map assets outside appetite " * 10
    clipped = _clip(text, 280)
    assert len(clipped) <= 281
    assert clipped.endswith((".", "…"))
    assert _clip("short", 280) == "short"
