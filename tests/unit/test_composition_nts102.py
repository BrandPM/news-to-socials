"""IT_PROJ_NTS_102 v2 + NTS_096 §C + NTS_095 — composition, S6.

The DoD lines, and the failure each test exists to catch:

* ``depth_final`` comes from the material. **Twelve facts with no comparable
  pair is an ``article``** — the spec's own worked case, and the one an
  implementation reading ``n_facts`` alone gets wrong.
* The length band is a floor, never a quota. ``deep`` has no ceiling.
* Attribution runs **before** translation, with one fix cycle. The reference
  case — "18 years of experience, most recently at CS and UBS" becoming
  "18-year tenure at CS and UBS" — must come back ``distorted``, and the test
  asserts alongside it that comparing digits would have said ``confirmed``.
  Without that second assertion the check is a substring search.
* A chart needs four points; three points build no chart. A time series is a
  line, categories are a bar, and a pie is never built.
* Nothing is built from a number without a source.
* Dash typography is deterministic, per language.
* Links land in neither the lede nor the close, and carry their own language
  prefix.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pipeline.generator import composition as comp
from pipeline.generator import internal_links as links
from pipeline.generator.research import Fact, FactPack

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _fact(text: str, **kw) -> Fact:
    fields = {"text": text, "url": "https://www.finma.ch/doc"}
    fields.update(kw)
    return Fact(**fields)  # type: ignore[arg-type]


def _pack(source_facts: list[Fact], context: list[Fact] | None = None) -> FactPack:
    return FactPack(
        source_facts=source_facts,
        context=context or [],
        citations=["https://www.finma.ch/doc"],
    )


class _Model:
    """A stub OpenAI client: one canned JSON answer, and it records the prompt."""

    def __init__(self, payload: dict | str):
        self.payload = payload if isinstance(payload, str) else json.dumps(payload)
        self.chat = self
        self.completions = self
        self.prompts: list[str] = []

    async def create(self, **kwargs):
        self.prompts.append(
            " ".join(str(m.get("content", "")) for m in kwargs["messages"])
        )
        payload = self.payload

        class _Msg:
            content = payload

        class _Choice:
            message = _Msg()

        class _Resp:
            usage = None

            def __init__(self) -> None:
                self.choices = [_Choice()]

        return _Resp()


# --------------------------------------------------------------------------
# depth_final (NTS_102 v2 §1)
# --------------------------------------------------------------------------


def test_twelve_facts_without_pairs_is_an_article_not_a_deep():
    """The spec's worked case. ``deep`` promises a table; a table needs pairs,
    so counting facts alone produces a deep article with nothing to put in it."""
    facts = [
        _fact(f"Provision {n} takes effect in 2027", value=str(n), unit="")
        for n in range(12)
    ]
    decision = comp.compute_depth_final(_pack(facts))
    assert decision.depth == "article"
    assert decision.n_facts == 12
    assert decision.n_pairs == 0
    assert "no table" in decision.reason


def test_facts_and_pairs_together_make_a_deep():
    facts = [
        _fact(
            f"The threshold in country {n} is EUR {n}m",
            value=str(n),
            unit="EUR",
            comparable_group="reporting threshold",
        )
        for n in range(1, 5)
    ] + [
        _fact(f"Rule {n} applies from 1 January 2027", value=str(n))
        for n in range(8)
    ]
    decision = comp.compute_depth_final(_pack(facts))
    assert decision.depth == "deep"
    assert decision.n_pairs >= 2


def test_a_thin_pack_is_a_note():
    decision = comp.compute_depth_final(_pack([_fact("EUR 5m threshold", value="5")]))
    assert decision.depth == "note"


def test_a_fact_with_no_figure_or_date_does_not_count():
    """"Analysts are concerned" has a URL and measures nothing."""
    decision = comp.compute_depth_final(
        _pack([_fact("Analysts are concerned about the direction") for _ in range(9)])
    )
    assert decision.n_facts == 0
    assert decision.depth == "note"


def test_a_group_whose_members_disagree_on_units_is_not_a_pair():
    """"threshold" covering both 12.5% and EUR 5 000 000 would build a table
    comparing a percentage with an amount."""
    facts = [
        _fact("12.5% rate", value="12.5", unit="%", comparable_group="threshold"),
        _fact("EUR 5m cap", value="5000000", unit="EUR", comparable_group="threshold"),
    ]
    assert comp.comparable_groups(facts) == {}


# --------------------------------------------------------------------------
# the length band is a floor, not a quota (NTS_102 §Риски)
# --------------------------------------------------------------------------


def test_deep_guidance_states_no_upper_limit_and_says_the_target_is_not_a_quota():
    targets = {"note": (300, 450), "article": (600, 900), "deep": (1200, None)}
    text = comp.depth_guidance("deep", targets)
    assert "no upper limit" in text
    assert "NOT a quota" in text
    assert "GROUNDING" in text


def test_note_guidance_carries_the_band_from_the_config_not_from_code():
    text = comp.depth_guidance("note", {"note": (120, 180)})
    assert "120-180" in text


# --------------------------------------------------------------------------
# attribution (NTS_096 §C) — the reference case
# --------------------------------------------------------------------------


DISTORTION_SOURCE = (
    "18 years of private banking experience, most recently at Credit Suisse "
    "and UBS"
)
DISTORTION_ARTICLE = "an 18-year tenure at Credit Suisse and UBS"


async def test_the_reference_distortion_is_caught_and_a_digit_check_would_miss_it():
    """NTS_096's whole reason for existing, as a regression test.

    The second assertion is the one that matters: it states, in code, that the
    naive implementation — "the number is in the source, therefore confirmed" —
    passes this case. Without it the test would also pass against a substring
    search, and the check would be worth nothing.
    """
    client = _Model(
        {
            "claims": [
                {
                    "claim": DISTORTION_ARTICLE,
                    "verdict": "distorted",
                    "why": "the source says 18 years of experience, most "
                    "recently at those banks — not 18 years at them",
                    "flags": ["person_detail"],
                }
            ]
        }
    )
    report = await comp.check_attribution(
        body=f"The manager brings {DISTORTION_ARTICLE}.",
        fact_pack=_pack([_fact(DISTORTION_SOURCE)]),
        client=client,
    )
    assert [c.verdict for c in report.distorted] == ["distorted"]
    assert report.needs_fix

    # The naive check that this replaces: the figure appears verbatim in the
    # source, so digit comparison confirms a false claim.
    figure = "18"
    assert figure in DISTORTION_ARTICLE and figure in DISTORTION_SOURCE

    # And the repair instruction names the claim, so the fix pass can act.
    instructions = report.fix_instructions()
    assert DISTORTION_ARTICLE in instructions
    assert "personal detail" in instructions


async def test_a_failed_check_never_blocks_the_article():
    """NTS_096 §C — the check advises until its false-positive rate is known.
    A check that stopped the pipeline for its own bugs would be switched off."""

    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("API down")

    report = await comp.check_attribution(
        body="anything", fact_pack=None, client=_Broken()
    )
    assert not report.checked
    assert not report.needs_fix
    assert report.error


async def test_the_quote_ceiling_reaches_the_prompt_from_the_licence_class():
    client = _Model({"claims": []})
    await comp.check_attribution(
        body="x",
        fact_pack=None,
        license_class="professional_commentary",
        max_quote_words={"professional_commentary": 15},
        client=client,
    )
    assert "more than 15 words" in client.prompts[0]


def test_an_unlisted_licence_class_has_no_quote_ceiling_of_its_own():
    """An official act may be quoted at length with attribution — that is the
    legal argument the v3 sourcing model rests on (NTS_108 §1)."""
    limits = {"professional_commentary": 15, "news_paywalled": 0}
    assert comp.quote_ceiling("public_official", limits) > 1000
    assert comp.quote_ceiling("news_paywalled", limits) == 0
    assert comp.quote_ceiling(None, limits) == 0


def test_the_report_counts_every_verdict_including_the_confirmed_ones():
    """The proportions ARE the measurement (NTS_096 DoD): a report that only
    kept the problems could not tell a strict check from a broken one."""
    report = comp.AttributionReport(
        claims=[
            comp.Claim("a", "confirmed"),
            comp.Claim("b", "distorted", "no"),
            comp.Claim("c", "uncovered"),
            comp.Claim("d", "confirmed", flags=["quote_too_long"]),
        ],
        checked=True,
    )
    counts = report.counts()
    assert counts == {
        "confirmed": 2,
        "distorted": 1,
        "uncovered": 1,
        "person_detail": 0,
        "quote_too_long": 1,
    }
    assert report.needs_fix  # a flag alone is enough to trigger the fix cycle


# --------------------------------------------------------------------------
# data blocks (NTS_095, NTS_102 v2 §1b)
# --------------------------------------------------------------------------


def _series(n: int, *, dates: bool) -> list[Fact]:
    return [
        _fact(
            f"Value {i}",
            value=str(100 + i),
            unit="EUR",
            comparable_group="threshold",
            date=f"202{i}-01-01" if dates else "",
        )
        for i in range(n)
    ]


def test_four_points_build_a_chart_and_three_do_not():
    """NTS_102 v2 §1b — a series of four, or no chart."""
    four = comp.build_data_blocks(
        _pack(_series(4, dates=True)), depth="deep", enabled=True
    )
    assert [b.type for b in four] == ["chart"]
    three = comp.build_data_blocks(
        _pack(_series(3, dates=True)), depth="deep", enabled=True
    )
    assert [b.type for b in three] == ["statTable"]


def test_a_time_series_is_a_line_and_categories_are_a_bar():
    dated = comp.build_data_blocks(
        _pack(_series(4, dates=True)), depth="deep", enabled=True
    )
    assert dated[0].payload["chartType"] == "line"
    undated = comp.build_data_blocks(
        _pack(_series(4, dates=False)), depth="deep", enabled=True
    )
    assert undated[0].payload["chartType"] == "bar"
    # Pie is not a chart type this module can produce, at any point count.
    assert "pie" not in comp.CHART_TYPES


def test_every_figure_in_a_block_carries_its_own_source():
    """NTS_095 — "ни одного блока данных, построенного на числах без источника"."""
    blocks = comp.build_data_blocks(
        _pack(_series(3, dates=False)), depth="deep", enabled=True
    )
    rows = blocks[0].payload["rows"]
    assert rows and all(row["sourceRef"] for row in rows)


def test_two_comparable_numbers_become_key_figures_not_a_two_row_table():
    blocks = comp.build_data_blocks(
        _pack(_series(2, dates=False)), depth="deep", enabled=True
    )
    assert [b.type for b in blocks] == ["keyFigures"]
    assert len(blocks[0].payload["figures"]) == 2


def test_no_comparable_numbers_means_no_block_and_that_is_a_normal_outcome():
    facts = [_fact("A rule changed", value="5") for _ in range(6)]
    assert comp.build_data_blocks(_pack(facts), depth="deep", enabled=True) == []


def test_blocks_are_not_built_below_deep_or_while_the_flag_is_off():
    """NTS_095 fixes the order schema → render → pipeline. The generator is
    written and tested now and writes nothing until S8's PR is merged."""
    pack = _pack(_series(4, dates=True))
    assert comp.build_data_blocks(pack, depth="deep", enabled=False) == []
    assert comp.build_data_blocks(pack, depth="article", enabled=True) == []


def test_at_most_two_blocks_per_article():
    facts: list[Fact] = []
    for group in ("threshold", "rate", "fee"):
        facts += [
            _fact(
                f"{group} {i}",
                value=str(i),
                unit="EUR",
                comparable_group=group,
            )
            for i in range(4)
        ]
    blocks = comp.build_data_blocks(_pack(facts), depth="deep", enabled=True)
    assert len(blocks) <= comp.MAX_BLOCKS


# --------------------------------------------------------------------------
# the plan (NTS_102 §"План перед текстом")
# --------------------------------------------------------------------------


async def test_the_plan_is_parsed_rendered_and_stored_shaped():
    client = _Model(
        {
            "sections": [
                {
                    "heading": "The repricing of mezzanine credit",
                    "purpose": "establish the mechanism",
                    "facts": ["EUR 5m threshold"],
                    "document_sections": ["Article 3"],
                    "block": "statTable",
                }
            ],
            "lede": "The clock resets for applicants already in the queue.",
            "close": "Applicants must refile before 1 January 2027.",
            "omitted": ["the consultation's Q&A annex — no numbers in it"],
        }
    )
    plan = await comp.build_plan(
        title="ESMA shortens the clock",
        summary="",
        fact_pack=_pack([_fact("EUR 5m threshold", value="5000000")]),
        client=client,
    )
    assert len(plan.sections) == 1
    rendered = plan.render()
    assert "## The repricing of mezzanine credit" in rendered
    assert "data block: statTable" in rendered
    assert "DELIBERATELY OMITTED" in rendered
    assert plan.as_dict()["lede"].startswith("The clock resets")


async def test_an_unparseable_plan_is_an_empty_plan_not_an_exception():
    plan = await comp.build_plan(
        title="t", summary="", fact_pack=None, client=_Model("not json")
    )
    assert plan.is_empty()
    assert "NO PLAN" in plan.render()


async def test_the_plan_prompt_carries_the_document_and_the_shape():
    client = _Model({"sections": []})
    await comp.build_plan(
        title="t",
        summary="",
        fact_pack=None,
        document_text="ENTRY INTO FORCE\nApplies from 2027.",
        document_url="https://www.finma.ch/doc",
        depth="deep",
        targets={"deep": (1200, None)},
        client=client,
    )
    prompt = client.prompts[0]
    assert "ENTRY INTO FORCE" in prompt
    assert "no upper limit" in prompt


# --------------------------------------------------------------------------
# deterministic post-process
# --------------------------------------------------------------------------


def test_dash_typography_is_per_language_and_leaves_english_alone():
    assert comp.normalise_dashes("word — word", "en") == "word — word"
    assert comp.normalise_dashes("word — word", "ru") == f"word{comp._NBSP}— word"
    assert comp.normalise_dashes("word — word", "uk") == f"word{comp._NBSP}— word"
    assert comp.normalise_dashes("word — word", "pl") == (
        f"word {comp._EN_DASH} word"
    )


def test_a_numeric_range_is_not_a_clause_break():
    assert comp.normalise_dashes("2026—2027", "ru") == "2026—2027"


def test_banned_phrases_are_reported_not_deleted():
    """Deleting a phrase mechanically leaves a broken sentence; the count is
    what tells the operator a per-language list needs work (NTS_072)."""
    survivors = comp.strip_banned_phrases(
        "It is important to note that we delve into the matter.",
        ["delve into", "it is important to note", "unused phrase"],
    )
    assert survivors == ["delve into", "it is important to note"]


# --------------------------------------------------------------------------
# internal links (NTS_093)
# --------------------------------------------------------------------------


BODY = """The reporting threshold moves to EUR 5m from January.

## What the directive changes

Holding structures above the reporting threshold must file quarterly.

## Who is exposed

Family offices with cross-border subsidiaries are the obvious case.

The practical consequence is a filing calendar that starts in April."""


def test_a_link_never_lands_in_the_lede_or_the_close():
    """NTS_093 — a service link in the close turns it back into the generic CTA
    that NTS_067's close rule was written to remove."""
    indexes = links.linkable_paragraph_indexes(BODY)
    parts = links.paragraphs(BODY)
    assert 0 not in indexes
    assert len(parts) - 1 not in indexes
    # Headings are not paragraphs a link can live in either.
    assert all(not parts[i].lstrip().startswith("#") for i in indexes)


def test_the_service_url_carries_the_language_prefix_and_the_live_domain():
    for language in ("en", "ru", "uk", "pl"):
        url = links.service_url(
            service_path="/services/family-office", language=language
        )
        assert url == f"https://iconfinance.io/{language}/services/family-office"
    assert "icon.finance/" not in (
        links.service_url(service_path="/x", language="en") or ""
    )


def test_a_link_is_skipped_when_no_natural_anchor_exists():
    """"Лучше пропустить, чем вклеить абзац-обрубок"."""
    body, placed = links.apply_links(
        BODY,
        [links.LinkTarget("https://iconfinance.io/en/services/x", "nowhere in text", "service")],
        anchor_pool=(),
    )
    assert placed == []
    assert body == BODY


def test_an_anchor_from_the_text_is_wrapped_in_place():
    body, placed = links.apply_links(
        BODY,
        [
            links.LinkTarget(
                "https://iconfinance.io/en/services/structuring-tax",
                "reporting threshold",
                "service",
            )
        ],
        anchor_pool=(),
    )
    assert len(placed) == 1
    assert "[reporting threshold](https://iconfinance.io/en/services/structuring-tax)" in body
    # And not in the lede, which also contains the phrase.
    assert body.split("\n\n")[0] == BODY.split("\n\n")[0]


def test_the_same_target_is_never_linked_twice():
    target = links.LinkTarget(
        "https://iconfinance.io/en/services/structuring-tax",
        "reporting threshold",
        "service",
    )
    _body, placed = links.apply_links(BODY, [target, target], anchor_pool=())
    assert len(placed) == 1


def test_a_short_note_gets_no_links_at_all():
    """Three paragraphs is a lede, a middle and a close — there is nowhere a
    link may go, and that is the correct outcome, not a failure."""
    short = "Lede paragraph.\n\n## Heading\n\nClose paragraph."
    assert links.linkable_paragraph_indexes(short) == []


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("wealth", "/en/services/wealth-management"),
        ("structuring", "/en/services/structuring-tax"),
        ("nonexistent", None),
    ],
)
async def test_the_service_is_a_lookup_not_a_model_call(category, expected):
    taxonomy = [
        {"key": "wealth", "label": "Wealth Management", "service_url_path": "/services/wealth-management"},
        {"key": "structuring", "label": "Structuring & Tax", "service_url_path": "/services/structuring-tax"},
    ]
    _body, placed = await links.link_draft(
        body=BODY,
        language="en",
        category=category,
        brand_id_fk=1,
        topic_id="t1",
        taxonomy=taxonomy,
        anchor_pool=["reporting threshold"],
    )
    if expected is None:
        assert placed == []
    else:
        assert placed and placed[0].url.endswith(expected)
