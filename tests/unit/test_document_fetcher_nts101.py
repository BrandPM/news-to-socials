"""IT_PROJ_NTS_101 — the primary document: find it, read it, prove it, cut it (S5).

The DoD lines this covers, each with the failure it exists to catch:

* ``doc_match`` returns an enum, and the ``mismatch`` case is real — the spec's
  own example is a FINMA circular hint against a FINMA document about a
  different circular. Nothing else in the pipeline can tell those apart.
* Targeted extraction: a 200-page document comes back inside the token budget
  **with the section labels recorded**. A selector that silently returned the
  first N characters would pass a length check and lose annex II.
* The cache does not pay twice inside the TTL, and a changed document creates a
  new version rather than overwriting the one an article already cited.
* A document from a domain outside ``sources`` is refused (NTS_101 §2).
* A non-English document goes through unchanged (NTS_101 §6 — the norm, not a
  branch).
* The 48-hour retry, its ceiling, and ``expired`` with ``no_document`` after it.

No network: the fetcher's HTTP is stubbed at ``httpx.AsyncClient`` and the two
model calls take injected clients.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline.admin import db as admin_db
from pipeline.admin.models import Candidate, DocumentVersion, PipelineConfig, Source
from pipeline.common import config as config_module
from pipeline.selector import portfolio_sweep
from pipeline.sources import document_fetcher as df
from tests.unit.conftest import seed_icon_brand

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        brand_id = seed_icon_brand(session)
        session.add(
            PipelineConfig(
                brand_id_fk=brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases=json.dumps([]),
                voice_profile="mission: x\n",
            )
        )
        session.commit()
    yield brand_id
    admin_db.reset_for_tests()


def _source(brand_id: int, **overrides) -> Source:
    fields = {
        "brand_id_fk": brand_id,
        "name": "FINMA News",
        "source_type": "rss",
        "url": "https://www.finma.ch/en/rss/news/",
        "primary_category": "structuring",
        "polling_minutes": 720,
        "active": True,
        "source_role": "primary_feed",
        "source_class": "regulator",
        "license_class": "public_official",
        "cache_ttl_days": 14,
        "created_at": NOW.replace(tzinfo=None),
        "updated_at": NOW.replace(tzinfo=None),
    }
    fields.update(overrides)
    with admin_db.get_session_factory()() as session:
        row = Source(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


class _Response:
    def __init__(self, content: bytes, *, content_type: str, status: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Stands in for ``httpx.AsyncClient`` inside the fetcher."""

    def __init__(self, responses: dict[str, _Response]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        self.calls.append(url)
        if url not in self.responses:
            raise RuntimeError(f"unexpected fetch of {url}")
        return self.responses[url]


class _MatchClient:
    """A stub OpenAI client for :func:`judge_doc_match`."""

    def __init__(self, verdict: str, reason: str = "because"):
        self.payload = json.dumps({"verdict": verdict, "reason": reason})
        self.chat = self
        self.completions = self
        self.seen: list[str] = []

    async def create(self, **kwargs):
        self.seen.append(kwargs["messages"][1]["content"])

        class _Msg:
            content = self.payload

        class _Choice:
            message = _Msg()

        class _Resp:
            usage = None

            def __init__(self) -> None:
                self.choices = [_Choice()]

        return _Resp()


# --------------------------------------------------------------------------
# the registry gate (NTS_101 §2)
# --------------------------------------------------------------------------


def test_only_registered_primary_domains_are_accepted(db):
    rows = [
        _source(db),
        _source(db, name="Reuters", url="https://reuters.com/feed", source_role="news"),
    ]
    domains = df.registered_domains(rows)
    assert domains == {"finma.ch"}
    assert df.is_registered("https://www.finma.ch/en/doc.pdf", domains)
    # A subdomain of a registered host is in; a news outlet is not, however
    # plausible the document looks.
    assert df.is_registered("https://media.finma.ch/x", domains)
    assert not df.is_registered("https://reuters.com/article", domains)
    assert not df.is_registered("https://finma.ch.evil.test/x", domains)


async def test_a_document_from_an_unregistered_domain_is_refused(db):
    """NTS_101 §2 — otherwise the registry stops registering anything."""
    _source(db)
    candidate = _Snapshot(
        input_kind="news",
        primary_doc_url="https://some-blog.test/leak.pdf",
        doc_match="exact",
    )
    outcome = await df.resolve_document(
        candidate=candidate,
        sources=[_source(db, name="dup", url="https://www.finma.ch/x")],
        budget=df.FetchBudget(),
        now=NOW,
    )
    assert outcome.status == "doc_missing"
    assert "not a registered primary domain" in outcome.reason


class _Snapshot:
    """A stand-in for the candidate snapshot the fetcher reads."""

    def __init__(self, **kw):
        defaults = {
            "id": 1,
            "input_kind": "news",
            "source_title": "FINMA tightens circular 2008/21",
            "source_summary": "The regulator revised the circular.",
            "source_url": "https://www.finma.ch/en/news/item",
            "source_published_at": NOW,
            "source_id_fk": None,
            "primary_doc_url": None,
            "primary_doc_hint": "FINMA circular 2008/21",
            "doc_match": None,
        }
        defaults.update(kw)
        for key, value in defaults.items():
            setattr(self, key, value)


# --------------------------------------------------------------------------
# extraction (NTS_101 §4)
# --------------------------------------------------------------------------


def test_html_extraction_keeps_headings_on_their_own_lines():
    html = b"""<html><head><title>Circular 2008/21</title></head><body>
    <nav>menu</nav><script>x()</script>
    <h1>Circular 2008/21</h1><p>The threshold rises to CHF 5m.</p>
    <h2>Entry into force</h2><p>Applies from 1 January 2027.</p>
    </body></html>"""
    text, title = df.extract_text(html, content_type="text/html")
    assert title == "Circular 2008/21"
    assert "menu" not in text and "x()" not in text
    # Headings on their own lines is what makes the section splitter work at
    # all; a run-together get_text() turns an act into one paragraph.
    assert "Entry into force" in text.splitlines()


def test_a_scan_is_unreadable_rather_than_three_characters_of_text():
    """NTS_101 §4 rules OCR out of the first iteration. The failure to avoid is
    a PDF that "extracted fine" into whitespace and produced an empty article."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(df.DocumentUnreadable):
        df.extract_text(buffer.getvalue(), content_type="application/pdf")


def test_language_is_guessed_but_never_load_bearing():
    german = "Der Bundesrat hat die Verordnung gemäß Artikel 4 nicht geändert. " * 5
    assert df.guess_language(german) == "de"
    assert df.guess_language("the shall of the with the and ") == "en"
    assert df.guess_language("") is None


# --------------------------------------------------------------------------
# doc_match (NTS_101 §3)
# --------------------------------------------------------------------------


def _doc(text: str = "Circular 2008/21 on liquidity", **kw) -> df.ExtractedDocument:
    fields = {
        "url": "https://www.finma.ch/en/doc.pdf",
        "text": text,
        "content_hash": "h",
        "content_type": "application/pdf",
        "byte_size": 10,
        "fetched_at": NOW,
    }
    fields.update(kw)
    return df.ExtractedDocument(**fields)  # type: ignore[arg-type]


async def test_doc_match_returns_the_enum_in_both_vocabularies():
    for verdict, column in (
        ("match", "exact"),
        ("partial", "probable"),
        ("mismatch", "none"),
    ):
        result = await df.judge_doc_match(
            title="t",
            summary="s",
            hint="FINMA circular 2008/21",
            item_date=NOW,
            doc=_doc(),
            client=_MatchClient(verdict),
        )
        assert result.verdict == verdict
        assert result.column_value == column
        assert result.usable is (verdict != "mismatch")


async def test_the_spec_mismatch_case(db):
    """The NTS_101 §3 test, verbatim: a hint about one FINMA circular against a
    FINMA document about a different one must come back ``mismatch``."""
    client = _MatchClient("mismatch", "the document is circular 2016/07")
    result = await df.judge_doc_match(
        title="FINMA revises circular 2008/21",
        summary="Capital adequacy for banks.",
        hint="FINMA circular 2008/21",
        item_date=NOW,
        doc=_doc("FINMA circular 2016/07 on corporate governance"),
        client=client,
    )
    assert result.verdict == "mismatch"
    assert result.column_value == "none"
    # The check saw the head of the document, not just the title.
    assert "2016/07" in client.seen[0]


async def test_a_broken_match_check_fails_closed():
    """An article written from the wrong document is worse than no article, so
    a check that cannot answer must not answer "yes"."""

    class _Broken:
        chat = property(lambda self: self)

        def __getattr__(self, name):
            raise RuntimeError("API down")

    result = await df.judge_doc_match(
        title="t", summary="s", hint=None, item_date=None, doc=_doc(), client=_Broken()
    )
    assert result.verdict == "mismatch"


# --------------------------------------------------------------------------
# targeted extraction (NTS_101 §4)
# --------------------------------------------------------------------------


def _long_document() -> str:
    parts = ["REGULATION (EU) 2026/1 ON REPORTING", "Adopted on 12 August 2026."]
    for n in range(1, 120):
        parts.append(
            f"Article {n}\nProvision {n} concerning administrative arrangements "
            "and procedural matters of no particular interest. " * 8
        )
    parts.append(
        "Article 200\nENTRY INTO FORCE\nThis Regulation applies from "
        "1 January 2028."
    )
    parts.append(
        "Annex II\nThe reporting threshold is set at EUR 5 000 000 for holding "
        "structures."
    )
    return "\n\n".join(parts)


def test_a_long_document_is_cut_to_budget_and_says_which_sections_it_kept():
    text = _long_document()
    selection = df.select_sections(
        text,
        hint="reporting threshold for holding structures",
        headline="EU sets EUR 5m reporting threshold",
        max_tokens=1200,
    )
    assert len(selection.text) <= 1200 * df.CHARS_PER_TOKEN
    assert selection.sections_total > 100
    assert selection.truncated
    # The point of recording labels: the editor can see the annex made it in.
    assert any("Annex II" in label for label in selection.sections_used)


def test_entry_into_force_survives_a_ranking_that_does_not_like_it():
    """NTS_101 §4 makes it mandatory because it is short, and BM25 is worst at
    short sections — it is also the first thing an editor asks about."""
    selection = df.select_sections(
        _long_document(),
        hint="administrative arrangements",
        headline="Procedural changes",
        max_tokens=800,
    )
    assert any("ENTRY INTO FORCE" in s for s in selection.sections_used) or (
        "ENTRY INTO FORCE" in selection.text
    )


def test_an_unsplittable_document_degrades_to_its_head_not_to_silence():
    wall = "no headings here at all. " * 4000
    selection = df.select_sections(
        wall, hint=None, headline="anything", max_tokens=300
    )
    assert selection.text
    assert selection.truncated
    assert selection.sections_used == ["document"]


# --------------------------------------------------------------------------
# the cache (NTS_101 §5)
# --------------------------------------------------------------------------


async def test_a_second_read_inside_the_ttl_costs_no_fetch(db, monkeypatch):
    body = b"<html><body><h1>Doc</h1><p>The threshold is EUR 5m.</p></body></html>"
    client = _Client({"https://www.finma.ch/d": _Response(body, content_type="text/html")})
    monkeypatch.setattr("httpx.AsyncClient", client)

    first = await df.fetch_document(
        "https://www.finma.ch/d",
        budget=df.FetchBudget(),
        cache_ttl_days=14,
        now=NOW,
    )
    assert not first.from_cache
    second = await df.fetch_document(
        "https://www.finma.ch/d",
        budget=df.FetchBudget(),
        cache_ttl_days=14,
        now=NOW + timedelta(days=3),
    )
    assert second.from_cache
    assert len(client.calls) == 1


async def test_a_changed_document_becomes_a_new_version_and_keeps_the_old(
    db, monkeypatch
):
    """An article cites the version it was written from (NTS_108 §3). A cache
    that updated in place would silently re-date every article quoting it."""
    url = "https://www.finma.ch/d"
    first_body = b"<html><body>threshold EUR 5m</body></html>"
    second_body = b"<html><body>threshold EUR 8m</body></html>"

    monkeypatch.setattr(
        "httpx.AsyncClient", _Client({url: _Response(first_body, content_type="text/html")})
    )
    await df.fetch_document(url, budget=df.FetchBudget(), cache_ttl_days=14, now=NOW)

    monkeypatch.setattr(
        "httpx.AsyncClient",
        _Client({url: _Response(second_body, content_type="text/html")}),
    )
    # Past the TTL, so it refetches.
    fresh = await df.fetch_document(
        url, budget=df.FetchBudget(), cache_ttl_days=14, now=NOW + timedelta(days=30)
    )
    assert not fresh.from_cache
    with admin_db.get_session_factory()() as session:
        versions = session.query(DocumentVersion).order_by(DocumentVersion.id).all()
        assert len(versions) == 2
        assert versions[0].extracted_text != versions[1].extracted_text
        assert versions[0].content_hash != versions[1].content_hash


async def test_re_reading_an_unchanged_document_does_not_create_a_version(
    db, monkeypatch
):
    url = "https://www.finma.ch/d"
    body = b"<html><body>same bytes every time</body></html>"
    monkeypatch.setattr(
        "httpx.AsyncClient", _Client({url: _Response(body, content_type="text/html")})
    )
    await df.fetch_document(url, budget=df.FetchBudget(), cache_ttl_days=0, now=NOW)
    await df.fetch_document(
        url, budget=df.FetchBudget(), cache_ttl_days=0, now=NOW + timedelta(days=1)
    )
    with admin_db.get_session_factory()() as session:
        assert session.query(DocumentVersion).count() == 1


async def test_a_document_over_the_size_budget_is_refused(db, monkeypatch):
    url = "https://www.finma.ch/huge"
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _Client({url: _Response(b"x" * (2 * 1024 * 1024), content_type="text/html")}),
    )
    with pytest.raises(df.DocumentUnreadable):
        await df.fetch_document(
            url, budget=df.FetchBudget(max_mb=1), cache_ttl_days=None, now=NOW
        )


# --------------------------------------------------------------------------
# non-English is the norm, not a branch (NTS_101 §6)
# --------------------------------------------------------------------------


async def test_a_german_document_goes_through_unchanged(db, monkeypatch):
    url = "https://www.bafin.de/rundschreiben"
    body = (
        "<html><body><h1>Rundschreiben 05/2026</h1>"
        "<p>Der Schwellenwert wird gemäß Artikel 4 auf 5 Mio. EUR angehoben "
        "und ist nicht rückwirkend anzuwenden.</p></body></html>"
    ).encode()
    monkeypatch.setattr(
        "httpx.AsyncClient", _Client({url: _Response(body, content_type="text/html")})
    )
    doc = await df.fetch_document(
        url, budget=df.FetchBudget(), cache_ttl_days=None, now=NOW
    )
    assert doc.doc_language == "de"
    assert "Schwellenwert" in doc.text
    # And the match check reads it in the original: no translation step exists,
    # and inserting one would put a paraphrase between the article and its
    # source.
    result = await df.judge_doc_match(
        title="BaFin raises the threshold",
        summary="",
        hint="Rundschreiben",
        item_date=NOW,
        doc=doc,
        client=_MatchClient("match"),
    )
    assert result.usable


# --------------------------------------------------------------------------
# reliability (NTS_101 §1)
# --------------------------------------------------------------------------


def test_reliability_is_maintained_by_the_fetcher(db):
    source = _source(db, reliability=None)
    df.record_extraction_outcome(source.id, success=True)
    with admin_db.get_session_factory()() as session:
        assert session.get(Source, source.id).reliability == 1.0
    for _ in range(5):
        df.record_extraction_outcome(source.id, success=False)
    with admin_db.get_session_factory()() as session:
        # Rolling, not cumulative: the Sources screen asks "is this working
        # lately", not "has it ever worked".
        assert session.get(Source, source.id).reliability < 0.4


# --------------------------------------------------------------------------
# the whole stage, and the document-kind shortcut (NTS_101 §2)
# --------------------------------------------------------------------------


async def test_a_document_kind_candidate_skips_the_search_and_the_match(
    db, monkeypatch
):
    url = "https://www.finma.ch/en/news/item"
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _Client(
            {
                url: _Response(
                    b"<html><body><h1>Circular</h1><p>EUR 5m.</p></body></html>",
                    content_type="text/html",
                )
            }
        ),
    )
    source = _source(db)
    outcome = await df.resolve_document(
        candidate=_Snapshot(
            input_kind="document", primary_doc_url=url, source_id_fk=source.id
        ),
        sources=[source],
        budget=df.FetchBudget(),
        now=NOW,
        # Deliberately no match client: reaching one would be the bug.
        match_client=None,
    )
    assert outcome.usable
    assert outcome.match is not None and outcome.match.column_value == "exact"


async def test_a_manual_link_is_taken_at_its_word(db, monkeypatch):
    """``doc_match='manual'`` is a human's decision. Re-asking a cheap model
    whether they were right would be both rude and less reliable."""
    url = "https://unlisted.test/act.html"
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _Client({url: _Response(b"<html><body>act text</body></html>", content_type="text/html")}),
    )
    source = _source(db)
    outcome = await df.resolve_document(
        candidate=_Snapshot(
            input_kind="news",
            primary_doc_url=url,
            doc_match="manual",
            source_id_fk=source.id,
        ),
        sources=[source],
        budget=df.FetchBudget(),
        now=NOW,
    )
    assert outcome.usable
    assert outcome.match is not None and outcome.match.column_value == "manual"


async def test_a_link_in_the_item_is_preferred_over_a_search(db):
    source = _source(db)
    found, how = await df.find_document_url(
        title="FINMA revises the circular",
        summary=(
            'Read the <a href="https://www.finma.ch/en/circ-2008-21.pdf">'
            "circular</a> in full."
        ),
        hint="circular",
        item_url="https://news.test/story",
        domains=df.registered_domains([source]),
        budget=df.FetchBudget(),
    )
    assert how == "item_link"
    assert found == "https://www.finma.ch/en/circ-2008-21.pdf"


# --------------------------------------------------------------------------
# doc_missing: the 48-hour retry and its ceiling (NTS_101 §7)
# --------------------------------------------------------------------------


def _candidate(db_id: int, **kw) -> int:
    fields = {
        "brand_id_fk": db_id,
        "input_kind": "news",
        "source_title": "A regulator announced something",
        "verdict": "accept",
        "reason_code": "ok",
        "reason": "announced",
        "status": "doc_missing",
        "created_at": NOW.replace(tzinfo=None),
        "expires_at": (NOW + timedelta(days=14)).replace(tzinfo=None),
    }
    fields.update(kw)
    with admin_db.get_session_factory()() as session:
        row = Candidate(**fields)
        session.add(row)
        session.commit()
        return int(row.id)


def test_a_missed_search_is_retried_after_48_hours_and_not_before(db):
    from pipeline.production import _document_retry_is_due

    row = type(
        "R",
        (),
        {"doc_attempts": 1, "doc_last_search_at": (NOW - timedelta(hours=20))},
    )()
    assert not _document_retry_is_due(row, now=NOW, max_retries=2, hours=48)
    row.doc_last_search_at = NOW - timedelta(hours=50)
    assert _document_retry_is_due(row, now=NOW, max_retries=2, hours=48)
    row.doc_attempts = 2
    assert not _document_retry_is_due(row, now=NOW, max_retries=2, hours=48)


def test_out_of_retries_expires_with_no_document(db):
    """"По пересказу не пишем" — the candidate ends, the reason is legible."""
    exhausted = _candidate(db, doc_attempts=2)
    still_trying = _candidate(db, doc_attempts=1)
    count = portfolio_sweep.expire_exhausted_doc_searches(
        brand_id_fk=db, doc_retries=2, now=NOW
    )
    assert count == 1
    with admin_db.get_session_factory()() as session:
        assert session.get(Candidate, exhausted).status == "expired"
        assert session.get(Candidate, exhausted).reason_code == "no_document"
        assert session.get(Candidate, still_trying).status == "doc_missing"


def test_parking_a_candidate_costs_no_production_attempt(db):
    """A regulator being slow is not the pipeline failing (NTS_101 §7)."""
    cid = _candidate(db, status="in_production", attempts=0)
    portfolio_sweep.park_document_missing(
        candidate_id=cid, reason="nothing on a registered domain"
    )
    with admin_db.get_session_factory()() as session:
        row = session.get(Candidate, cid)
        assert row.status == "doc_missing"
        assert row.attempts == 0
        assert "registered domain" in (row.last_error or "")
