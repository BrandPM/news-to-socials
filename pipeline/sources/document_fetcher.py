"""Contour 2, stage one: get the primary document (NTS_101 §2-7).

This is the link the whole of v3 is built around. Without it "каждое число
прослеживается до документа" is undeliverable and the ``news`` branch can only
produce retellings — which is exactly what NTS_105 called the failure of v1.
Until this module existed, ``candidates.doc_match``, ``doc_version_id`` and
``doc_sections_used`` were columns nothing wrote (NTS_121 §3).

Four things happen here, in this order:

**1. Find it.** For ``input_kind=document`` there is nothing to find — the feed
item *is* the document, and NTS_101 §2 says ``doc_match`` is not needed. For
``news``, three paths in order of confidence: a link in the news item that
already points at a registered primary domain; a site search inside the
``primary_site`` rows of the class the guard guessed; a general web search
restricted to registered primary domains. **A document from a domain that is
not in ``sources`` is refused** (NTS_101 §2) — the alternative is a registry
that does not register anything.

**2. Fetch and extract it.** HTML through the configured render service when
there is one and plain HTTP otherwise; PDF through ``pypdf``. A scan (a PDF
with no text layer) is ``doc_unreadable`` and counts as missing — NTS_101 §4
rules OCR out of the first iteration, and half-read text is worse than none.

**3. Check it is the right one.** ``doc_match`` is a cheap model call over the
title, the summary, the guard's hint and the first 3 000 characters
(NTS_101 §3). ``mismatch`` sends the candidate to ``doc_missing``; ``partial``
is allowed through with a flag for the editor. The spec's vocabulary
(``match``/``partial``/``mismatch``) is mapped onto the column's
(``exact``/``probable``/``none``), which is what ``_MATCH_TO_COLUMN`` is for.

**4. Cut it down.** A directive runs to hundreds of pages and the composition
budget is ``doc_max_tokens_for_composition``. Sections are ranked by BM25
against the hint and the headline — no model, because ranking sections with a
model costs a call per document to answer a question a term-frequency score
answers well — and the ones that always go in regardless of score are the
title, the date, the table of contents and anything about entry into force.
The labels of what was taken are recorded in ``doc_sections_used`` so the
editor can see what the writer read and, more importantly, what it did not.

The cache (§5) is versioned, never overwritten, and keyed by URL: an article
cites the version it was written from, so ``as_of`` has to keep meaning the
same thing a year later.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from ..common.logging import get_logger

log = get_logger(__name__)

# The extractor identity stored on every cached version. Bump when the
# extraction changes shape, not when a dependency patch-bumps: the point is to
# be able to tell "this text came from a different reader" apart from "this
# document changed".
TOOL_VERSION = "nts-extract/1 pypdf+bs4"

# NTS_101 §3 — how much of the document the match check sees.
DOC_MATCH_HEAD_CHARS = 3000

# NTS_101 §7 — regulators publish the act a day or two after the announcement.
DOC_RETRY_AFTER_HOURS = 48

# Rough characters-per-token. Only used to turn the token budget into a slicing
# limit; being 20% wrong here costs a paragraph, not a correctness property.
CHARS_PER_TOKEN = 4

# NTS_101 §3 vocabulary → the ``candidates.doc_match`` column vocabulary. The
# two specs disagree (NTS_100 §1 says {match, partial}); the column and
# NTS_101 §2-7 say {exact, probable, manual, none}, and the column wins because
# it is what the selector reads.
_MATCH_TO_COLUMN = {
    "match": "exact",
    "partial": "probable",
    "mismatch": "none",
}

# Sections that go into the composition whatever BM25 thinks of them
# (NTS_101 §4). "Entry into force" is the one an editor asks about first and
# the one keyword ranking is worst at, because it is short.
_ALWAYS_KEEP = re.compile(
    r"entry into force|entering into force|application date|applies from|"
    r"commencement|in ?kraft|inkrafttreten|entrée en vigueur|"
    r"table of contents|contents|summary|executive summary",
    re.IGNORECASE,
)

# The keyword branch carries its own ``(?i:…)`` rather than the whole pattern
# taking re.IGNORECASE: case-insensitive would make the ALL-CAPS branch match
# every ordinary line, and the first version of this regex did exactly that —
# a 120-article regulation came back as two sections.
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,4}\s+.+"                       # markdown-ish
    r"|(?i:article|art\.|section|chapter|annex|schedule|part)\s+[\dIVXivx]+.*"
    r"|[A-Z][A-Z \d.,'\-()]{6,80}"       # ALL-CAPS heading line
    r"|\d+(?:\.\d+)*\.?\s+[A-Z].{3,80}"  # 1.2.3 Numbered heading
    r")$",
    re.MULTILINE,
)

_WORD_RE = re.compile(r"[a-zà-öø-ÿ0-9]{3,}", re.IGNORECASE)


class DocumentUnreadable(RuntimeError):  # noqa: N818 — a condition, not a crash
    """A PDF with no text layer, or a body no extractor could turn into text."""


@dataclass(frozen=True)
class FetchBudget:
    """The four numbers NTS_101 §4 puts in the brand config."""

    timeout_s: int = 60
    max_mb: int = 25
    max_tokens_for_composition: int = 12000
    retries: int = 2
    match_model: str = "gpt-4o-mini"

    @classmethod
    def from_config(cls, config: Any) -> FetchBudget:
        def _int(name: str, default: int) -> int:
            try:
                return int(getattr(config, name, default) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            timeout_s=_int("doc_timeout_s", 60),
            max_mb=_int("doc_max_mb", 25),
            max_tokens_for_composition=_int("doc_max_tokens_for_composition", 12000),
            retries=_int("doc_retries", 2),
            match_model=str(getattr(config, "doc_match_model", None) or "gpt-4o-mini"),
        )


@dataclass
class ExtractedDocument:
    """A document as the pipeline uses it: text, provenance, and nothing else."""

    url: str
    text: str
    content_hash: str
    content_type: str
    byte_size: int
    fetched_at: datetime
    tool_version: str = TOOL_VERSION
    title: str | None = None
    doc_language: str | None = None
    http_status: int | None = None
    version_id: int | None = None
    section_count: int = 0
    from_cache: bool = False

    @property
    def as_of(self) -> datetime:
        """The stamp the article carries (NTS_108 §2): when this version was read."""
        return self.fetched_at


@dataclass
class DocumentSelection:
    """What of a document actually reaches composition."""

    text: str
    sections_used: list[str] = field(default_factory=list)
    sections_total: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class DocMatch:
    """The §3 verdict, in both vocabularies."""

    verdict: str  # match | partial | mismatch
    reason: str
    column_value: str  # exact | probable | none

    @property
    def usable(self) -> bool:
        return self.verdict in ("match", "partial")


# --------------------------------------------------------------------------
# 1. the registry gate
# --------------------------------------------------------------------------


def registered_domains(source_rows: Iterable[Any]) -> set[str]:
    """Hostnames of the ``primary_feed`` / ``primary_site`` rows in ``sources``.

    NTS_101 §2: a document from a domain that is not in the registry is not
    accepted until someone adds the domain in the Sources screen. Without this
    the registry stops registering anything and the "primary source" guarantee
    becomes "whatever the search engine returned".
    """
    out: set[str] = set()
    for row in source_rows:
        if str(getattr(row, "source_role", "")) not in (
            "primary_feed",
            "primary_site",
        ):
            continue
        host = _host(str(getattr(row, "url", "") or ""))
        if host:
            out.add(host)
    return out


def _host(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def is_registered(url: str, domains: set[str]) -> bool:
    """``True`` when ``url``'s host, or its parent domain, is registered.

    Subdomains count: ``eur-lex.europa.eu`` registered admits
    ``publications.europa.eu`` only if that host is itself registered, but
    ``www.finma.ch`` registered admits ``finma.ch/en/…``. Matching on the
    registrable parent instead would let one registered regulator vouch for
    every site under a national domain.
    """
    host = _host(url)
    if not host:
        return False
    return any(host == d or host.endswith(f".{d}") for d in domains)


# --------------------------------------------------------------------------
# 2. extraction
# --------------------------------------------------------------------------


def extract_text(
    body: bytes, *, content_type: str, url: str = ""
) -> tuple[str, str | None]:
    """``(text, title)`` from a PDF or an HTML body.

    Raises :class:`DocumentUnreadable` for a PDF with no text layer — that is a
    scan, and NTS_101 §4 rules OCR out of the first iteration rather than
    letting a document silently arrive as three characters of whitespace.
    """
    ctype = (content_type or "").lower()
    looks_pdf = "pdf" in ctype or body[:5] == b"%PDF-" or url.lower().endswith(".pdf")
    if looks_pdf:
        return _extract_pdf(body), None
    return _extract_html(body)


def _extract_pdf(body: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    pages: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # a single broken page must not lose the rest
            log.warning("document.pdf_page_failed", page=index, err=str(exc))
            continue
        if text.strip():
            # The page marker is a section anchor for the selector below and a
            # citation aid for the editor.
            pages.append(f"\n[page {index}]\n{text}")
    joined = "".join(pages).strip()
    if len(joined) < 200:
        raise DocumentUnreadable(
            f"pdf yielded {len(joined)} characters over {len(reader.pages)} pages "
            "— almost certainly a scan with no text layer"
        )
    return joined


def _extract_html(body: bytes) -> tuple[str, str | None]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    # Headings keep their own lines so the section splitter can see them; a
    # naive get_text() run together turns a structured act into one paragraph.
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "li", "p", "tr"]):
        tag.append("\n")
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned, title


def guess_language(text: str) -> str | None:
    """A cheap script/stopword guess, good enough for ``doc_language``.

    Deliberately not a dependency: the value is metadata for the editor and an
    input to nothing. NTS_101 §6 makes non-English documents the norm rather
    than a branch, so the pipeline must not care what this returns.
    """
    sample = text[:4000].lower()
    if not sample.strip():
        return None
    markers = {
        "de": (" der ", " und ", " gemäß", " werden ", " nicht "),
        "fr": (" les ", " des ", " est ", " selon ", " doit "),
        "it": (" della ", " degli ", " sono ", " deve "),
        "es": (" los ", " las ", " debe ", " según "),
        "pl": (" oraz ", " przez ", " który ", " nie "),
        "en": (" the ", " and ", " shall ", " of the ", " with "),
    }
    scores = {
        lang: sum(sample.count(m) for m in words) for lang, words in markers.items()
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 3 else None


# --------------------------------------------------------------------------
# 3. the cache (NTS_101 §5)
# --------------------------------------------------------------------------


def cached_version(
    url: str, *, ttl_days: int | None, now: datetime | None = None
) -> ExtractedDocument | None:
    """The newest cached version of ``url``, if it is still inside its TTL.

    ``ttl_days`` of 0 or ``None`` means "always refetch" rather than "never
    expire": a source whose class carries no TTL is one nobody has classified,
    and the safe reading of an unclassified source is that its content moves.
    """
    if not ttl_days:
        return None
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import DocumentVersion

    now = now or datetime.now(tz=UTC)
    cutoff = now - timedelta(days=int(ttl_days))
    with get_session_factory()() as session:
        row = (
            session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.url == url)
                .order_by(DocumentVersion.fetched_at.desc(), DocumentVersion.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        fetched = row.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        if fetched < cutoff:
            return None
        return ExtractedDocument(
            url=row.url,
            text=row.extracted_text,
            content_hash=row.content_hash,
            content_type=row.content_type or "",
            byte_size=int(row.byte_size or 0),
            fetched_at=fetched,
            tool_version=row.tool_version,
            title=row.title,
            doc_language=row.doc_language,
            http_status=row.http_status,
            version_id=int(row.id),
            section_count=int(row.section_count or 0),
            from_cache=True,
        )


def store_version(
    doc: ExtractedDocument, *, source_class: str | None = None
) -> int | None:
    """Persist a version, or return the id of the identical one already stored.

    Same URL and same ``content_hash`` is the same version — re-reading a
    document that has not changed must not create a second row, or "the article
    cites version N" stops being a statement about content.
    """
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import DocumentVersion

    try:
        with get_session_factory()() as session:
            existing = session.execute(
                select(DocumentVersion.id).where(
                    DocumentVersion.url == doc.url,
                    DocumentVersion.content_hash == doc.content_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            row = DocumentVersion(
                url=doc.url,
                content_hash=doc.content_hash,
                fetched_at=doc.fetched_at.replace(tzinfo=None),
                extracted_text=doc.text,
                doc_language=doc.doc_language,
                byte_size=doc.byte_size,
                content_type=doc.content_type,
                tool_version=doc.tool_version,
                title=doc.title,
                source_class=source_class,
                section_count=doc.section_count,
                http_status=doc.http_status,
                created_at=datetime.now(tz=UTC).replace(tzinfo=None),
            )
            session.add(row)
            session.commit()
            log.info(
                "document.version_stored",
                url=doc.url,
                version_id=row.id,
                chars=len(doc.text),
                hash=doc.content_hash[:12],
            )
            return int(row.id)
    except Exception as exc:
        # A document that could not be cached is still a document. Losing the
        # cache row costs a refetch; raising would cost the article.
        log.warning("document.version_store_failed", url=doc.url, err=str(exc))
        return None


# --------------------------------------------------------------------------
# 4. fetching
# --------------------------------------------------------------------------


async def fetch_document(
    url: str,
    *,
    budget: FetchBudget,
    source_class: str | None = None,
    cache_ttl_days: int | None = None,
    now: datetime | None = None,
) -> ExtractedDocument:
    """Fetch, extract and cache one document. Raises on anything unusable.

    Returns the cached version when one is inside its TTL — the whole point of
    §5 is that reading the same directive for the second article this week is
    free. On a refetch whose ``content_hash`` differs, a **new** version is
    written and the old one is kept.
    """
    now = now or datetime.now(tz=UTC)
    hit = cached_version(url, ttl_days=cache_ttl_days, now=now)
    if hit is not None:
        log.info("document.cache_hit", url=url, version_id=hit.version_id)
        return hit

    import httpx

    from ..common.config import get_settings

    max_bytes = max(1, int(budget.max_mb)) * 1024 * 1024
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=float(budget.timeout_s),
        headers={"User-Agent": get_settings().outbound_user_agent},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.content
        content_type = resp.headers.get("content-type", "")
        status = resp.status_code

    if len(body) > max_bytes:
        raise DocumentUnreadable(
            f"document is {len(body) / 1048576:.1f} MB, over the "
            f"{budget.max_mb} MB budget"
        )

    text, title = extract_text(body, content_type=content_type, url=url)
    if not text.strip():
        raise DocumentUnreadable("extraction produced no text")

    doc = ExtractedDocument(
        url=url,
        text=text,
        content_hash=hashlib.sha256(body).hexdigest(),
        content_type=content_type,
        byte_size=len(body),
        fetched_at=now,
        title=title,
        doc_language=guess_language(text),
        http_status=status,
        section_count=len(split_sections(text)),
    )
    doc.version_id = store_version(doc, source_class=source_class)
    return doc


# --------------------------------------------------------------------------
# 5. doc_match (NTS_101 §3)
# --------------------------------------------------------------------------


_DOC_MATCH_INSTRUCTIONS = """\
You decide whether a document is the one a news item is about. You are not
summarising and not judging quality.

Answer with JSON only:
{"verdict": "match" | "partial" | "mismatch", "reason": "<one short sentence>"}

* "match" — the document IS the instrument/decision/filing the item reports.
* "partial" — same body and same subject area, but a different instrument, an
  earlier or later version, or a related annex.
* "mismatch" — a different subject, a different institution, or an unrelated
  document. When in doubt between partial and mismatch, answer mismatch: an
  article written from the wrong document is worse than no article.
"""

_DOC_MATCH_INPUT = """\
NEWS ITEM
Title: {title}
Summary: {summary}
Published: {item_date}
What the editorial guard expected the document to be: {hint}

DOCUMENT
URL: {url}
Title: {doc_title}
First {head_chars} characters:
{head}
"""


async def judge_doc_match(
    *,
    title: str,
    summary: str,
    hint: str | None,
    item_date: datetime | None,
    doc: ExtractedDocument,
    model: str = "gpt-4o-mini",
    client: Any = None,
) -> DocMatch:
    """Ask a cheap model whether this document is the right one.

    Fails **closed**: an API error or an unparseable answer returns
    ``mismatch``, because the failure mode this guards against is writing an
    article from a document about something else, and a broken check that
    defaults to "yes" would not guard against anything.

    Charged as its own operation in ``cost_records`` (NTS_101 §Мерить), so the
    document stage's cost is separable from research.
    """
    from ..admin.cost_recorder import record_cost
    from ..common.pricing import openai_cost

    if client is None:
        from openai import AsyncOpenAI

        from ..common.config import get_settings

        api_key = get_settings().openai_api_key
        if not api_key:
            return DocMatch("mismatch", "no OPENAI_API_KEY for the match check", "none")
        client = AsyncOpenAI(api_key=api_key)

    payload = _DOC_MATCH_INPUT.format(
        title=title,
        summary=(summary or "")[:800],
        item_date=item_date.date().isoformat() if item_date else "unknown",
        hint=hint or "(no hint)",
        url=doc.url,
        doc_title=doc.title or "(none)",
        head_chars=DOC_MATCH_HEAD_CHARS,
        head=doc.text[:DOC_MATCH_HEAD_CHARS],
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _DOC_MATCH_INSTRUCTIONS},
                {"role": "user", "content": payload},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        log.warning("document.match_failed", url=doc.url, err=str(exc)[:200])
        return DocMatch("mismatch", f"match check failed: {type(exc).__name__}", "none")

    usage = getattr(resp, "usage", None)
    if usage is not None:
        record_cost(
            provider="openai",
            operation="doc_match",
            model=model,
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            cost_usd=openai_cost(
                model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            ),
        )

    raw = ""
    try:
        raw = resp.choices[0].message.content or ""
        parsed = json.loads(raw)
        verdict = str(parsed.get("verdict", "")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()[:200]
    except Exception:
        log.warning("document.match_unparseable", url=doc.url, raw=raw[:200])
        return DocMatch("mismatch", "match check returned an unreadable answer", "none")
    if verdict not in _MATCH_TO_COLUMN:
        return DocMatch("mismatch", f"unknown verdict {verdict!r}", "none")
    return DocMatch(verdict, reason, _MATCH_TO_COLUMN[verdict])


# --------------------------------------------------------------------------
# 6. targeted extraction (NTS_101 §4)
# --------------------------------------------------------------------------


def split_sections(text: str) -> list[tuple[str, str]]:
    """``[(label, body)]`` split on headings, page markers and blank runs.

    A single unsplittable wall of text comes back as one section labelled
    ``document`` rather than as nothing — the selector must degrade to "the
    first N characters", not to silence.
    """
    if not text.strip():
        return []
    marks: list[tuple[int, str]] = []
    for match in _HEADING_RE.finditer(text):
        label = match.group(0).strip().lstrip("#").strip()
        if label:
            marks.append((match.start(), label[:120]))
    for match in re.finditer(r"^\[page \d+\]$", text, re.MULTILINE):
        marks.append((match.start(), match.group(0).strip("[]")))
    if not marks:
        return [("document", text)]
    marks.sort()
    sections: list[tuple[str, str]] = []
    if marks[0][0] > 0:
        sections.append(("preamble", text[: marks[0][0]]))
    for index, (start, label) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((label, body))
    return sections


def _terms(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _bm25_scores(
    sections: Sequence[tuple[str, str]], query: Sequence[str]
) -> list[float]:
    """Plain BM25 over the sections. No model, deliberately (NTS_101 §4).

    Ranking sections with an LLM would cost one call per document to answer a
    question term frequency answers well, and would make "why was annex II
    dropped" unanswerable.
    """
    k1, b = 1.5, 0.75
    docs = [_terms(f"{label}\n{body}") for label, body in sections]
    lengths = [len(d) or 1 for d in docs]
    avg_len = sum(lengths) / len(lengths)
    n = len(docs)
    scores = [0.0] * n
    for term in set(query):
        containing = sum(1 for d in docs for _ in (1,) if term in d)
        if not containing:
            continue
        idf = math.log(1 + (n - containing + 0.5) / (containing + 0.5))
        for i, doc in enumerate(docs):
            freq = doc.count(term)
            if not freq:
                continue
            scores[i] += idf * (
                freq * (k1 + 1) / (freq + k1 * (1 - b + b * lengths[i] / avg_len))
            )
    return scores


def select_sections(
    text: str,
    *,
    hint: str | None,
    headline: str,
    max_tokens: int,
) -> DocumentSelection:
    """The part of a document that reaches composition, and its section labels.

    Mandatory sections first (NTS_101 §4: title, date, contents, entry into
    force), then the BM25 ranking against the hint and the headline, until the
    budget runs out. Sections are re-ordered back into document order before
    joining, so the writer reads an act in the sequence it was written in.
    """
    sections = split_sections(text)
    budget_chars = max(1000, int(max_tokens) * CHARS_PER_TOKEN)
    if not sections:
        return DocumentSelection(text="", sections_used=[], sections_total=0)

    query = _terms(f"{hint or ''} {headline}")
    scores = _bm25_scores(sections, query) if query else [0.0] * len(sections)

    mandatory: set[int] = {0}  # the head of a document is its title and date
    for index, (label, body) in enumerate(sections):
        if _ALWAYS_KEEP.search(label) or _ALWAYS_KEEP.search(body[:400]):
            mandatory.add(index)

    chosen: list[int] = []
    used = 0
    for index in sorted(mandatory):
        chunk = len(sections[index][1])
        if used + chunk > budget_chars and chosen:
            break
        chosen.append(index)
        used += chunk
    for index in sorted(
        range(len(sections)), key=lambda i: (-scores[i], i)
    ):
        if index in chosen:
            continue
        chunk = len(sections[index][1])
        if used + chunk > budget_chars:
            continue
        chosen.append(index)
        used += chunk

    chosen.sort()
    if not chosen:
        chosen = [0]
    body = "\n\n".join(sections[i][1] for i in chosen)
    # A mandatory section can be larger than the whole budget on its own — a
    # document with no headings is one section of 100 000 characters. The
    # budget is a budget, so the head is taken and the truncation is declared;
    # silently handing composition ten times its context window would fail
    # later, further away, and more expensively.
    truncated = len(chosen) < len(sections) or len(body) > budget_chars
    return DocumentSelection(
        text=body[:budget_chars],
        sections_used=[sections[i][0] for i in chosen],
        sections_total=len(sections),
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# 7. finding the document for a news lead (NTS_101 §2)
# --------------------------------------------------------------------------


_LINK_RE = re.compile(r'https?://[^\s"\'<>)\]]+', re.IGNORECASE)


def links_in(html_or_text: str) -> list[str]:
    """Every absolute URL in an RSS summary or a page body, in order.

    Order matters: the first registered link in a regulator's announcement is
    almost always the act it announces, and taking the first hit is both
    cheaper and more accurate than ranking them.
    """
    if not html_or_text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_or_text, "lxml")
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if href.lower().startswith("http") and href not in seen:
                seen.add(href)
                out.append(href)
    except Exception:  # a summary that is plain text, not HTML
        pass
    for match in _LINK_RE.finditer(html_or_text):
        url = match.group(0).rstrip(".,;")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


async def find_document_url(
    *,
    title: str,
    summary: str,
    hint: str | None,
    item_url: str | None,
    domains: set[str],
    class_domains: set[str] | None = None,
    budget: FetchBudget,
    client: Any = None,
) -> tuple[str | None, str]:
    """The three paths of NTS_101 §2, in order. Returns ``(url, how)``.

    1. a link already in the item that points at a registered primary domain;
    2. a search restricted to the ``primary_site`` domains of the class the
       guard guessed;
    3. the same search across every registered primary domain.

    ``how`` names the path, so the funnel can report which one is carrying the
    finds — the difference between "our registry works" and "we are relying on
    a search engine" is exactly this number.
    """
    for candidate_url in links_in(summary):
        if is_registered(candidate_url, domains):
            return candidate_url, "item_link"

    if item_url and is_registered(item_url, domains):
        # A primary feed's own item: the announcement page is on a registered
        # domain, so it is itself an acceptable document (this is the ordinary
        # case for ``input_kind=document``).
        return item_url, "item_url"

    for scope, scoped in (("class_site", class_domains or set()), ("registry", domains)):
        if not scoped:
            continue
        found = await _search_for_document(
            title=title,
            hint=hint,
            domains=scoped,
            budget=budget,
            client=client,
        )
        if found and is_registered(found, domains):
            return found, scope
        if found:
            # Refused rather than accepted: NTS_101 §2 is explicit that a
            # document from an unregistered domain waits for a human to add
            # the domain in Sources.
            log.info("document.search_hit_unregistered", url=found, scope=scope)
    return None, "none"


_SEARCH_INSTRUCTIONS = """\
Find the single official document a news item is about.

Search ONLY these domains: {domains}

Return JSON only: {{"url": "<direct link to the document, or empty string>"}}

Return the document itself — the act, decision, consultation paper, circular or
filing — not a news page about it and not a listing page. If you cannot find it
on those domains, return an empty string. An empty answer is correct and
useful; a plausible wrong link is not.
"""


async def _search_for_document(
    *,
    title: str,
    hint: str | None,
    domains: set[str],
    budget: FetchBudget,
    client: Any = None,
) -> str | None:
    """One domain-restricted web search. ``None`` on anything unusable."""
    from ..admin.cost_recorder import record_cost
    from ..common.pricing import web_search_cost

    if client is None:
        from openai import AsyncOpenAI

        from ..common.config import get_settings

        api_key = get_settings().openai_api_key
        if not api_key:
            return None
        client = AsyncOpenAI(api_key=api_key)

    instructions = _SEARCH_INSTRUCTIONS.format(
        domains=", ".join(sorted(domains)[:40])
    )
    payload = f"News item: {title}\nExpected document: {hint or '(not specified)'}"
    for tool_type in ("web_search", "web_search_preview"):
        try:
            resp = await client.responses.create(
                model=budget.match_model,
                instructions=instructions,
                input=payload,
                tools=[{"type": tool_type}],
                max_output_tokens=500,
                timeout=float(budget.timeout_s),
            )
        except Exception as exc:
            message = str(exc).lower()
            if "400" in message and ("web_search" in message or "tool" in message):
                continue
            log.warning("document.search_failed", err=str(exc)[:200])
            return None
        # Charged as its own operation so the document stage is separable from
        # research on the cost screen (NTS_101 §Мерить).
        record_cost(
            provider="openai",
            operation="doc_search",
            model=budget.match_model,
            cost_usd=web_search_cost(1),
        )
        raw = getattr(resp, "output_text", "") or ""
        try:
            url = str(json.loads(raw).get("url", "")).strip()
        except Exception:
            found = _LINK_RE.search(raw)
            url = found.group(0) if found else ""
        return url or None
    return None


# --------------------------------------------------------------------------
# 8. source reliability (NTS_101 §1) — "maintained by the fetcher"
# --------------------------------------------------------------------------


def record_extraction_outcome(source_id: int | None, *, success: bool) -> None:
    """Roll ``sources.reliability`` towards the latest outcome.

    An exponential moving average with alpha 0.2, because the number the
    Sources screen needs is "is this feed working lately", not "has it ever
    worked". Until this session the column was described as "maintained by the
    fetcher" and written only by hand (NTS_121 §3).
    """
    if source_id is None:
        return
    try:
        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import Source

        alpha = 0.2
        with get_session_factory()() as session:
            row = session.get(Source, source_id)
            if row is None:
                return
            current = row.reliability
            observation = 1.0 if success else 0.0
            row.reliability = (
                observation
                if current is None
                else round((1 - alpha) * float(current) + alpha * observation, 4)
            )
            session.commit()
    except Exception as exc:
        log.warning("document.reliability_update_failed", err=str(exc))


# --------------------------------------------------------------------------
# 9. the orchestration one candidate sees
# --------------------------------------------------------------------------


@dataclass
class DocumentOutcome:
    """What the document stage decided about one candidate."""

    status: str  # ok | doc_missing | unreadable
    document: ExtractedDocument | None = None
    match: DocMatch | None = None
    how: str = ""
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.document is not None


async def resolve_document(
    *,
    candidate: Any,
    sources: Sequence[Any],
    budget: FetchBudget,
    now: datetime | None = None,
    match_client: Any = None,
    search_client: Any = None,
) -> DocumentOutcome:
    """Get the document for one candidate, or say why there isn't one.

    ``input_kind=document`` skips the search and the match check entirely
    (NTS_101 §2: "primary_doc_url = url элемента ленты; doc_match не нужен") —
    and the ``doc_match`` written for it is ``exact``, because the feed item
    being the document is not a guess.

    A manager's manual link (``doc_match='manual'``) is honoured the same way:
    a human said this is the document, and re-asking a cheap model whether they
    were right would be both rude and unreliable.
    """
    now = now or datetime.now(tz=UTC)
    domains = registered_domains(sources)
    input_kind = getattr(candidate, "input_kind", "news")
    existing_match = getattr(candidate, "doc_match", None)
    url = getattr(candidate, "primary_doc_url", None)
    source_id = getattr(candidate, "source_id_fk", None)
    by_id = {getattr(s, "id", None): s for s in sources}
    source_row = by_id.get(source_id)
    source_class = getattr(source_row, "source_class", None)
    ttl = getattr(source_row, "cache_ttl_days", None)

    how = "existing"
    if not url or (input_kind == "news" and existing_match not in ("exact", "probable", "manual")):
        if input_kind == "document" and url:
            how = "item_url"
        else:
            class_domains = {
                _host(str(getattr(s, "url", "")))
                for s in sources
                if getattr(s, "source_class", None) == source_class
                and str(getattr(s, "source_role", "")) in ("primary_feed", "primary_site")
            } - {""}
            found, how = await find_document_url(
                title=getattr(candidate, "source_title", "") or "",
                summary=getattr(candidate, "source_summary", "") or "",
                hint=getattr(candidate, "primary_doc_hint", None),
                item_url=getattr(candidate, "source_url", None),
                domains=domains,
                class_domains=class_domains,
                budget=budget,
                client=search_client,
            )
            url = found

    if not url:
        return DocumentOutcome(
            status="doc_missing", how=how, reason="no document found on a registered domain"
        )
    if not is_registered(url, domains) and existing_match != "manual":
        # The one exception is the manual link: a manager pasting a URL is the
        # documented way to admit a document the registry does not know yet.
        return DocumentOutcome(
            status="doc_missing",
            how=how,
            reason=f"{_host(url)} is not a registered primary domain",
        )

    try:
        doc = await fetch_document(
            url,
            budget=budget,
            source_class=source_class,
            cache_ttl_days=ttl,
            now=now,
        )
    except DocumentUnreadable as exc:
        record_extraction_outcome(source_id, success=False)
        return DocumentOutcome(status="unreadable", how=how, reason=str(exc)[:200])
    except Exception as exc:
        record_extraction_outcome(source_id, success=False)
        return DocumentOutcome(
            status="doc_missing", how=how, reason=f"{type(exc).__name__}: {exc}"[:200]
        )
    record_extraction_outcome(source_id, success=True)

    if input_kind == "document":
        return DocumentOutcome(
            status="ok",
            document=doc,
            match=DocMatch("match", "the feed item is the document", "exact"),
            how=how,
        )
    if existing_match == "manual":
        return DocumentOutcome(
            status="ok",
            document=doc,
            match=DocMatch("match", "linked by hand from the Portfolio", "manual"),
            how="manual",
        )

    verdict = await judge_doc_match(
        title=getattr(candidate, "source_title", "") or "",
        summary=getattr(candidate, "source_summary", "") or "",
        hint=getattr(candidate, "primary_doc_hint", None),
        item_date=getattr(candidate, "source_published_at", None),
        doc=doc,
        model=budget.match_model,
        client=match_client,
    )
    if not verdict.usable:
        return DocumentOutcome(
            status="doc_missing", match=verdict, how=how, reason=verdict.reason
        )
    return DocumentOutcome(status="ok", document=doc, match=verdict, how=how)
