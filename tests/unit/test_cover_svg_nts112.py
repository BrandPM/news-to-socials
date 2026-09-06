"""IT_PROJ_NTS_112 — covers drawn from data, not from diffusion (S9).

The DoD lines, and what each test is actually protecting:

* **Five motifs, deterministic on the seed.** Determinism is what makes a cover
  cacheable and a change to the template visible as a diff; without it "the
  cover changed" is unanswerable.
* **PNG at both sizes through resvg.** Asserted by decoding the bytes, not by
  checking that a function returned something.
* **No text but the stamp.** The prohibition is the point of the whole design
  (NTS_112 §Правила): no words from the headline, no people, no buildings, no
  flags — and it is enforceable here precisely because the generator has no
  access to the headline at all.
* **``note`` gets no motif.** A short note must not dress up as an analysis.
* **The cost row is written at zero.** An operation missing from the ledger is
  indistinguishable from one that never ran.
"""

from __future__ import annotations

import io
import re

import pytest
from PIL import Image

from pipeline.generator.cover_svg import (
    COVER_SIZES,
    MOTIFS,
    SERVICE_COLOURS,
    CoverData,
    build_svg,
    cover_from_candidate,
    pick_stamp,
    render_png,
)


def _data(**kw) -> CoverData:
    fields = {
        "candidate_id": 42,
        "service": "structuring",
        "depth": "article",
        "jurisdictions": ("CH", "EU"),
        "fact_count": 5,
        "figures": ["5000000", "1200000"],
        "sections": 7,
        "stamp": "CHF 5 000 000",
    }
    fields.update(kw)
    return CoverData(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("service", MOTIFS)
def test_every_service_has_a_motif_and_its_own_field_colour(service):
    svg = build_svg(_data(service=service, depth="deep"))
    assert SERVICE_COLOURS[service] in svg
    # Some geometry beyond the background and the grain rect.
    assert re.search(r"<(circle|line|rect|polygon)", svg.split("</defs>")[1])


@pytest.mark.parametrize("size", sorted(COVER_SIZES))
def test_it_renders_to_png_at_both_sizes(size):
    png = render_png(build_svg(_data(), size=size))
    assert Image.open(io.BytesIO(png)).size == COVER_SIZES[size]


def test_the_same_candidate_always_draws_the_same_cover():
    """Determinism is what makes the cover cacheable and a template change
    visible as a diff. ``random`` seeded globally would make a cover depend on
    the order it was drawn in."""
    first = build_svg(_data(candidate_id=99))
    second = build_svg(_data(candidate_id=99))
    assert first == second
    assert build_svg(_data(candidate_id=100)) != first


def test_the_data_changes_the_shape():
    """Two articles in one service must not be identical (NTS_112 §Грамматика).
    The count of nested rectangles follows the figures."""
    few = build_svg(_data(service="structuring", figures=["1"], depth="deep"))
    many = build_svg(
        _data(service="structuring", figures=["1", "2", "3", "4", "5"], depth="deep")
    )
    assert few.count("<rect") < many.count("<rect")


def test_a_note_gets_the_field_and_the_stamp_and_no_motif():
    """A short note should not dress up as an analysis (NTS_112 §Правила)."""
    note = build_svg(_data(depth="note"))
    body = note.split("</defs>")[1]
    # Two rects: the field and the grain overlay. Nothing else drawn.
    assert body.count("<circle") == 0
    assert body.count("<polygon") == 0
    assert body.count("<rect") == 2
    assert "CHF" in note


def test_nothing_but_the_stamp_is_text():
    """No words from the headline, no people, no buildings, no flags. The
    generator never sees the headline, which is what makes the rule keepable
    rather than merely stated."""
    svg = build_svg(_data(stamp="EUR 5 000 000"))
    texts = re.findall(r"<text[^>]*>(.*?)</text>", svg)
    assert texts == ["EUR 5 000 000", "CH · EU"]


def test_the_stamp_prefers_the_most_concrete_figure():
    """Amount, then percentage, then ISO date, then act name — a date is a
    weaker answer than a threshold."""
    assert pick_stamp(
        facts=["applies from 2027-01-01 above EUR 5000000"]
    ).startswith("EUR")
    assert pick_stamp(facts=["a rate of 12.5 % applies from 2027-01-01"]) == "12.5%"
    assert pick_stamp(facts=["applies from 2027-01-01"]) == "2027-01-01"
    assert pick_stamp(facts=["FATF added two jurisdictions"]) == "FATF"


def test_with_no_figure_the_stamp_is_the_jurisdiction_codes():
    assert pick_stamp(facts=["a qualitative change"], fallback=["CH", "EU"]) == "CH · EU"


def test_big_numbers_are_grouped_to_be_readable():
    stamp = pick_stamp(facts=["the threshold is EUR 5000000"])
    assert stamp != "EUR 5000000"
    assert stamp.replace(" ", "") == "EUR 5000000"


def test_the_cover_is_assembled_from_rows_the_run_already_wrote():
    """No new call, no headline: everything comes off the candidate and the
    pack (NTS_112 §Решение)."""
    from pipeline.generator.research import Fact, FactPack

    class _Candidate:
        id = 7
        service_category = "ma"
        depth_final = "deep"
        depth_prior = "article"
        jurisdictions = '["US", "UK"]'

    data = cover_from_candidate(
        candidate=_Candidate(),
        fact_pack=FactPack(
            source_facts=[
                Fact(
                    text="the deal is valued at USD 900000000",
                    url="https://x.test/a",
                    value="900000000",
                    unit="USD",
                )
            ],
            citations=["https://x.test/a"],
        ),
        document_sections=4,
    )
    assert data.service == "ma"
    # depth_final wins over the guard's prior — the material decided.
    assert data.depth == "deep"
    assert data.jurisdictions == ["US", "UK"]
    assert data.stamp.startswith("USD")
    assert data.sections == 4


def test_an_unknown_service_still_draws_something():
    """A brand whose taxonomy the cover map does not know must still get a
    cover; a missing key is not a reason to ship an article without one."""
    svg = build_svg(_data(service="unmapped", depth="deep"))
    assert "<polygon" in svg


async def test_the_zero_cost_row_is_still_written(tmp_path, monkeypatch):
    """NTS_112 DoD — "в cost_records для data-обложек стоимость 0, операция
    пишется". An operation absent from the ledger cannot be told from one that
    never ran."""
    from cryptography.fernet import Fernet

    from pipeline.admin import db as admin_db
    from pipeline.admin import image_regenerate
    from pipeline.admin.models import CostRecord
    from pipeline.common import config as config_module
    from tests.unit.conftest import seed_icon_brand

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        seed_icon_brand(session)
        session.commit()

    uploaded: list[bytes] = []

    class _Publisher:
        async def upload_cover_image(self, png, filename):
            uploaded.append(png)
            return "image-data-cover"

    class _Client:
        def __init__(self):
            self.mutations = []

        async def mutate(self, mutations):
            self.mutations.append(mutations)
            return {}

    client = _Client()
    asset = await image_regenerate.generate_and_apply_cover(
        title="ignored — the data cover never sees a headline",
        topic_id="t1",
        source_url="https://x.test/a",
        target_ids=["drafts.a-en", "drafts.a-ru"],
        client=client,  # type: ignore[arg-type]
        publisher=_Publisher(),  # type: ignore[arg-type]
        mode="data",
        cover_data=_data(),
    )
    assert asset == "image-data-cover"
    assert Image.open(io.BytesIO(uploaded[0])).size == (1200, 630)
    # One transaction for every sibling (NTS_069) — never half-covered.
    assert len(client.mutations[0]) == 2

    with admin_db.get_session_factory()() as session:
        rows = session.query(CostRecord).all()
        assert [(r.operation, r.cost_usd) for r in rows] == [("cover_data", 0.0)]
    admin_db.reset_for_tests()


async def test_an_explicit_custom_prompt_still_takes_the_diffusion_path():
    """The Regenerate button with a hand-written prompt is by definition a
    request for the artistic cover — data mode must not swallow it."""
    from pipeline.admin import image_regenerate

    calls: list[str] = []

    async def _fake_flux(*args, **kwargs):
        calls.append("flux")
        raise RuntimeError("stop here — reaching flux is the assertion")

    import pipeline.admin.image_regenerate as mod

    original = mod.ImageGenerator
    class _Gen:
        async def generate(self, *a, **kw):
            return await _fake_flux()

    mod.ImageGenerator = lambda: _Gen()  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await image_regenerate.generate_and_apply_cover(
                title="t",
                topic_id="t1",
                source_url="https://x.test/a",
                target_ids=["drafts.a-en"],
                client=object(),  # type: ignore[arg-type]
                publisher=object(),  # type: ignore[arg-type]
                mode="data",
                cover_data=_data(),
                custom_prompt="a photograph of a lighthouse",
            )
    finally:
        mod.ImageGenerator = original  # type: ignore[assignment]
    assert calls == ["flux"]
