"""The production run: portfolio → drafts (NTS_100, NTS_098 §5, NTS_103 шаг 3).

    sweep → batch claim → weekly budget → rank → produce → drafted

This is the run that was missing. Contour 1 (``pipeline.intake``) has been
filling ``candidates`` since 2026-08-28 and nothing has ever taken one out:
NTS_121 §2 found ``weekly_draft_budget``, ``production_timeout_min``,
``max_cost_per_candidate_usd`` and the TTL pass with **no reader in the code at
all**, and NTS_122 stage 6 printed the selection step as "gap (S4)". Nine days
of intake produced zero drafts, which is the whole reason for this session.

What is deliberately *not* here:

* **A new writer.** Generation still goes through the v2 seams —
  ``comment_writer`` for the EN canon and the translations, ``research`` for
  the fact pack, ``image`` for the cover. NTS_114 S4 says so in as many words
  ("Производство пока по старому ``writer_draft`` + NTS_092 — обкатка ритма");
  the real composition — plan, ``depth_final``, attribution before translation
  — is S6, and building the rhythm and the writer in one session would have
  left neither testable.
* **Publication.** The run ends at ``drafted``. The slot is assigned when the
  editor approves (NTS_098 §2/§5, ``candidate_lifecycle.assign_publication_slot``)
  because publication is a manual Approve and always has been; what this run
  owes the calendar is the *thin portfolio* alert three days before a slot
  (NTS_100 §3.5), which it raises.
* **``v2_generation_enabled``.** That flag gates ``pipeline.run``, the old
  daily path. This run has its own flag, ``production_enabled``, and the two
  are independent on purpose: the cutover (NTS_103) is a sequence of switches,
  and a shared one would make "v3 on, v2 off" unexpressible.

Two properties the tests pin, because both cost real money when wrong:

**One batch per brand per day.** ``production_batches`` has a UNIQUE constraint
on ``(brand_id_fk, batch_date)`` and this run *inserts* to claim the day rather
than querying first (NTS_100 §3.3). A cron firing while the operator presses
"Run now" is the ordinary case, not the exotic one, and a SELECT-then-INSERT
check loses exactly that race.

**A failure never loses what was already paid for.** An exception anywhere in
production rolls the candidate back to ``pending`` with ``attempts+1`` and the
reason on the row (NTS_100 §4) — and leaves the fact pack in place, so the
retry reuses research instead of buying it again. Research is 59% of the cost
of an article (NTS_122).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import typer

from pipeline.common.logging import configure_logging, get_logger
from pipeline.common.models import Draft, Language, RawItem, Topic
from pipeline.selector.candidate_store import (
    claim_pending,
    resolve_timezone,
)
from pipeline.selector.portfolio_sweep import (
    expire_exhausted_doc_searches,
    expire_stale_candidates,
    park_document_missing,
    prune_old_candidates,
    release_to_pending,
    sweep_production_timeouts,
)
from pipeline.selector.ranking import (
    CandidateFacts,
    RankWeights,
    select_batch,
)

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)

# NTS_100 §1 — a news candidate is only eligible once a document has been found
# for it. The spec writes the vocabulary as ``{match, partial}``; NTS_101 §2-7
# and the ``doc_match`` column settled on ``exact``/``probable``/``manual``/
# ``none``, so those are the values checked here (``manual`` is the link a
# manager pasted in the Portfolio, which NTS_123 admits explicitly).
#
# Before S5 nothing writes ``doc_match`` at all, so this constant is what keeps
# the run honest in the meantime: ``document`` candidates — where the document
# *is* the feed item — are produced, and ``news`` leads wait for the fetcher
# rather than being written up from a headline — the standing rule of NTS_123:
# no article from a retelling, only from a document.
ELIGIBLE_DOC_MATCH: tuple[str, ...] = ("exact", "probable", "manual")

# Where the run stops looking for a slot to warn about (NTS_100 §3.5).
THIN_PORTFOLIO_LEAD_DAYS = 3


class ProductionDisabled(RuntimeError):  # noqa: N818 — a refusal, not a failure
    """``production_enabled`` is off. Same contract as ``IntakeDisabled``."""


@dataclass
class ProductionStats:
    """What one production run did, in absolute numbers (NTS_106 §2)."""

    eligible: int = 0
    selected: int = 0
    drafted: int = 0
    failed: int = 0
    reused_fact_packs: int = 0
    doc_missing: int = 0
    doc_found: int = 0
    weekly_budget: int = 0
    taken_this_week: int = 0
    expired: int = 0
    timed_out: int = 0
    pruned: int = 0
    spend_usd: float = 0.0
    batch_date: str | None = None
    stopped_reason: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "selected": self.selected,
            "drafted": self.drafted,
            "failed": self.failed,
            "reused_fact_packs": self.reused_fact_packs,
            "doc_missing": self.doc_missing,
            "doc_found": self.doc_found,
            "weekly_budget": self.weekly_budget,
            "taken_this_week": self.taken_this_week,
            "expired": self.expired,
            "timed_out": self.timed_out,
            "pruned": self.pruned,
            "spend_usd": round(self.spend_usd, 4),
            "batch_date": self.batch_date,
            "stopped_reason": self.stopped_reason,
            "candidates": self.candidates,
        }


# --------------------------------------------------------------------------
# 1. the daily batch — a claim, not a query
# --------------------------------------------------------------------------


def batch_key(brand_slug: str, batch_date: date) -> str:
    """``(brand, date)`` as the string stored on the candidate row."""
    return f"{brand_slug}:{batch_date.isoformat()}"


def claim_batch(
    *,
    brand_id_fk: int,
    batch_date: date,
    run_id: int | None,
    now: datetime | None = None,
) -> bool:
    """Claim today for this brand. ``False`` if a run already has it.

    NTS_100 §3.3. The INSERT *is* the check: the UNIQUE constraint refuses the
    second one, and the loser reads its own IntegrityError rather than a stale
    SELECT.
    """
    from sqlalchemy.exc import IntegrityError

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import ProductionBatch

    try:
        with get_session_factory()() as session:
            session.add(
                ProductionBatch(
                    brand_id_fk=brand_id_fk,
                    batch_date=batch_date,
                    run_id=run_id,
                    selected_count=0,
                    created_at=now or datetime.now(tz=UTC),
                )
            )
            session.commit()
        return True
    except IntegrityError:
        log.info(
            "production.batch_already_claimed",
            brand_id=brand_id_fk,
            batch_date=batch_date.isoformat(),
        )
        return False


def _record_batch_size(
    *, brand_id_fk: int, batch_date: date, selected: int
) -> None:
    from sqlalchemy import update

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import ProductionBatch

    with get_session_factory()() as session:
        session.execute(
            update(ProductionBatch)
            .where(
                ProductionBatch.brand_id_fk == brand_id_fk,
                ProductionBatch.batch_date == batch_date,
            )
            .values(selected_count=selected)
        )
        session.commit()


# --------------------------------------------------------------------------
# 2. who is eligible, and how much of the week is left
# --------------------------------------------------------------------------


def _jurisdictions(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if isinstance(parsed, list):
        return tuple(str(x) for x in parsed if x)
    return ()


def _document_retry_is_due(
    row: Any, *, now: datetime, max_retries: int, hours: int
) -> bool:
    """Is a candidate due for (another) document search?

    NTS_101 §7: retry after 48 hours, at most ``doc_retries`` times. Out of
    retries means the candidate waits for its TTL — or for a manual link from
    the Portfolio — rather than being searched for on every run forever.
    """
    if int(getattr(row, "doc_attempts", 0) or 0) >= max_retries:
        return False
    last = getattr(row, "doc_last_search_at", None)
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return bool((now - last) >= timedelta(hours=hours))


def eligible_candidates(
    *,
    brand_id_fk: int,
    now: datetime | None = None,
    doc_retries: int = 2,
) -> list[CandidateFacts]:
    """The rows NTS_100 §1 admits to the formula.

    Not expired, not held by a manager, and — for ``news`` — either carrying a
    usable document already or still entitled to a document search
    (NTS_101 §7). ``held`` is excluded rather than penalised: a hold is a
    decision, and a decision the ranker could outvote is not a decision.
    """
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate
    from pipeline.sources.document_fetcher import DOC_RETRY_AFTER_HOURS

    now = now or datetime.now(tz=UTC)
    with get_session_factory()() as session:
        rows = (
            session.execute(
                select(Candidate).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    # ``doc_missing`` is not a dead end (NTS_101 §7): a
                    # regulator publishing the act two days after the
                    # announcement is the ordinary case, not the exception.
                    Candidate.status.in_(("pending", "doc_missing")),
                )
            )
            .scalars()
            .all()
        )
        out: list[CandidateFacts] = []
        for row in rows:
            if row.manual_action == "held":
                continue
            expires = row.expires_at
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires < now:
                    continue
            if row.input_kind != "document":
                has_document = bool(row.primary_doc_url) and (
                    row.doc_match or ""
                ) in ELIGIBLE_DOC_MATCH
                # No document yet is fine as long as the document stage is
                # still allowed to look for one. What is not fine is producing
                # an article from a headline, and that is what the stage
                # itself refuses.
                if not has_document and not _document_retry_is_due(
                    row,
                    now=now,
                    max_retries=doc_retries,
                    hours=DOC_RETRY_AFTER_HOURS,
                ):
                    continue
            out.append(
                CandidateFacts(
                    candidate_id=int(row.id),
                    confidence=row.confidence,
                    depth_prior=row.depth_prior,
                    event_stage=row.event_stage,
                    jurisdictions=_jurisdictions(row.jurisdictions),
                    input_kind=row.input_kind,
                    service_category=row.service_category,
                    created_at=row.created_at,
                    source_published_at=row.source_published_at,
                    manual_action=row.manual_action,
                )
            )
    return out


def _iso_week_start(now: datetime, timezone_name: str | None) -> datetime:
    """Monday 00:00 of the brand's ISO week, as UTC.

    NTS_100 §3.1 counts the budget over "ISO-неделю бренда" — a run at 01:00
    Monday in Madrid is a new week even though it is still Sunday in UTC.
    """
    tz = resolve_timezone(timezone_name)
    local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=local.weekday())
    return monday.astimezone(UTC)


def taken_this_week(
    *,
    brand_id_fk: int,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """How much of the weekly budget is spent, and on what.

    Returns ``(count, per service_category, per primary jurisdiction)``. The
    two maps feed the diversity penalties, which is why this is one query
    rather than three: "already taken this week" has to mean the same thing to
    the budget and to the formula, or a category could be penalised for a pick
    the budget does not count.

    Counted by ``selected_at``, over every status a candidate that entered
    production can be in — including ``failed`` and ``published``. A draft that
    was produced and then failed still consumed the week's capacity and, more
    to the point, still cost money.
    """
    from sqlalchemy import select

    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    now = now or datetime.now(tz=UTC)
    week_start = _iso_week_start(now, timezone_name)
    categories: dict[str, int] = {}
    jurisdictions: dict[str, int] = {}
    with get_session_factory()() as session:
        rows = (
            session.execute(
                select(
                    Candidate.service_category,
                    Candidate.jurisdictions,
                ).where(
                    Candidate.brand_id_fk == brand_id_fk,
                    Candidate.selected_at.is_not(None),
                    Candidate.selected_at >= week_start.replace(tzinfo=None),
                    Candidate.status.in_(
                        (
                            "in_production",
                            "drafted",
                            "returned",
                            "ready",
                            "published",
                            "failed",
                        )
                    ),
                )
            )
        ).all()
    for category, raw_jurisdictions in rows:
        if category:
            categories[category] = categories.get(category, 0) + 1
        codes = _jurisdictions(raw_jurisdictions)
        if codes:
            jurisdictions[codes[0]] = jurisdictions.get(codes[0], 0) + 1
    return len(rows), categories, jurisdictions


# --------------------------------------------------------------------------
# 3. producing one candidate on the v2 writer path
# --------------------------------------------------------------------------


def _topic_from_candidate(row: Any, brand_slug: str, *, tag: str | None) -> Topic:
    """A ``Topic`` the v2 generation seams accept, carrying the candidate id.

    ``candidate_id`` is the seam NTS_121 §3 names: it is what lets
    ``link_candidate_to_draft`` fire at the one moment both ids exist. ``tag``
    prefixes the title for an operator-marked run (the e2e proof), and it goes
    on the *title* rather than a side channel so the mark reaches the slug and
    is visible in the Studio.
    """
    from pipeline.intake import _topic_id_for

    title = row.source_title or "(untitled)"
    if tag:
        title = f"[{tag}] {title}"
    url: str = str(row.primary_doc_url or row.source_url or "https://example.invalid/")
    return Topic(
        id=_topic_id_for(row.source_url or row.primary_doc_url, row.source_title or ""),
        brand_id=brand_slug,
        raw=RawItem(
            source_id=str(row.source_id_fk or ""),
            source_name=row.source_name or "portfolio",
            # pydantic coerces the string to HttpUrl on construction; mypy
            # reads the declared field type and cannot see that.
            url=url,  # type: ignore[arg-type]
            title=title,
            summary=row.source_summary or "",
            published_at=row.source_published_at,
        ),
        # The guard's confidence is 0..1 and ``relevance_score`` is 0..10. The
        # number is unused on this path (scoring was demoted to the prefilter
        # in NTS_099) but the field is required and must stay in range.
        relevance_score=min(10.0, max(0.0, float(row.confidence or 0.5) * 10.0)),
        candidate_id=int(row.id),
    )


@dataclass(frozen=True)
class _CandidateSnapshot:
    """The candidate fields the document stage reads, detached from the session.

    A plain snapshot because ``resolve_document`` does network work that can
    take a minute, and holding an ORM row open across it would keep a SQLite
    write transaction alive for the whole fetch.
    """

    id: int
    input_kind: str
    source_title: str
    source_summary: str
    source_url: str | None
    source_published_at: datetime | None
    source_id_fk: int | None
    primary_doc_url: str | None
    primary_doc_hint: str | None
    doc_match: str | None
    # The guard's guess, kept only so the run can log where it disagreed with
    # the material (NTS_102 v2 §1 / NTS_099 v2 metric).
    depth_prior: str | None = None
    # NTS_108 §1 — set from the candidate's source row; decides the quote
    # ceiling the attribution check enforces.
    license_class: str | None = None

    @classmethod
    def of(cls, row: Any) -> _CandidateSnapshot:
        return cls(
            id=int(row.id),
            input_kind=row.input_kind,
            source_title=row.source_title or "",
            source_summary=row.source_summary or "",
            source_url=row.source_url,
            source_published_at=row.source_published_at,
            source_id_fk=row.source_id_fk,
            primary_doc_url=row.primary_doc_url,
            primary_doc_hint=row.primary_doc_hint,
            doc_match=row.doc_match,
            depth_prior=row.depth_prior,
        )

    def with_license(self, license_class: str | None) -> _CandidateSnapshot:
        from dataclasses import replace

        return replace(self, license_class=license_class)


def _record_document_outcome(
    *, candidate_id: int, outcome: Any, now: datetime
) -> None:
    """Count the document search on the candidate, whatever it found.

    Written even on success: ``doc_attempts`` is how many times we went
    looking, and the retry window (NTS_101 §7) is measured from the last look,
    not from the last failure.
    """
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    with get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is None:
            return
        row.doc_attempts = int(row.doc_attempts or 0) + 1
        row.doc_last_search_at = now.replace(tzinfo=None)
        if getattr(outcome, "match", None) is not None and not outcome.usable:
            row.doc_match = outcome.match.column_value
        session.commit()


def _store_document_link(
    *,
    candidate_id: int,
    version_id: int | None,
    doc_match: str | None,
    url: str,
    sections: Sequence[str],
) -> None:
    """The last link of the traceability chain (NTS_121 §3, migration 025).

    ``doc_sections_used`` is the one that matters for the editor: it says which
    parts of a 200-page act the writer actually read, so "the number is not in
    the document" and "the number is in a section we did not send" stop looking
    the same.
    """
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    with get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is None:
            return
        row.primary_doc_url = url
        if version_id is not None:
            row.doc_version_id = str(version_id)
        if doc_match:
            row.doc_match = doc_match
        row.doc_sections_used = json.dumps(list(sections), ensure_ascii=False)
        session.commit()


def _store_depth(*, candidate_id: int, depth: str) -> None:
    """``candidates.depth_final`` — the depth the material supported."""
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    with get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is not None:
            row.depth_final = depth
            session.commit()


def _store_needs_attention(*, candidate_id: int, value: bool) -> None:
    """NTS_102 v2 §2 — the flag the review queue sorts on."""
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate

    with get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is not None:
            row.needs_attention = bool(value)
            session.commit()


def _store_plan(fact_pack_id: int, plan: dict[str, Any]) -> None:
    """The plan, on the pack it was planned from (NTS_102 v2 §3).

    Best-effort like every other traceability write: losing it costs the
    editor a ``scope=plan`` return, raising would cost the article.
    """
    _patch_fact_pack(fact_pack_id, plan=json.dumps(plan, ensure_ascii=False))


def _store_attribution(fact_pack_id: int, report: dict[str, Any]) -> None:
    """The per-claim verdicts (NTS_096 §C)."""
    _patch_fact_pack(
        fact_pack_id, attribution=json.dumps(report, ensure_ascii=False)
    )


def _patch_fact_pack(fact_pack_id: int, **values: Any) -> None:
    try:
        from sqlalchemy import update

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import FactPack

        with get_session_factory()() as session:
            session.execute(
                update(FactPack)
                .where(FactPack.id == fact_pack_id)
                .values(**values)
            )
            session.commit()
    except Exception as exc:
        log.warning(
            "production.fact_pack_patch_failed",
            fact_pack_id=fact_pack_id,
            fields=sorted(values),
            err=str(exc)[:200],
        )


async def _upload_data_cover(
    *,
    candidate_id: int,
    topic_id: str,
    fact_pack: Any,
    document_sections: int,
    sanity_publisher: Any,
) -> str | None:
    """Draw the cover from data and upload it as one asset (NTS_112, NTS_069).

    One asset for all four siblings, as before: the cover carries currencies,
    percentages, ISO dates and act names, and none of those need translating —
    which is exactly why the data cover can be shared where a captioned image
    could not.
    """
    from pipeline.admin.cost_recorder import record_cost
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate
    from pipeline.generator.cover_svg import (
        build_svg,
        cover_from_candidate,
        render_png,
    )

    with get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is None:
            return None
        data = cover_from_candidate(
            candidate=row,
            fact_pack=fact_pack,
            document_sections=document_sections,
        )
    png = render_png(build_svg(data))
    # Recorded at zero rather than not recorded: NTS_112's DoD asks for the
    # operation in the ledger, and an operation missing from it is
    # indistinguishable from one that never ran.
    record_cost(
        provider="local", operation="cover_data", model="cover_svg/1", cost_usd=0.0
    )
    asset_id = await sanity_publisher.upload_cover_image(
        png, filename=f"icon-{topic_id}-cover.png"
    )
    log.info(
        "production.data_cover",
        candidate_id=candidate_id,
        stamp=data.stamp,
        service=data.service,
        depth=data.depth,
    )
    return str(asset_id)


def _writer_for(brand: Any) -> Any:
    """A ``CommentWriter`` bound to the brand, for the repair pass."""
    from pipeline.generator.comment_writer import CommentWriter

    return CommentWriter(brand_id_fk=getattr(brand, "id_fk", None))


async def _finish_sibling(
    *,
    draft: Any,
    language: Language,
    brand: Any,
    brand_id_fk: int,
    topic: Any,
    category: str | None,
    config: Any,
) -> Any:
    """Everything that happens to one language sibling after translation.

    Two things, in this order (NTS_093, NTS_123 S6):

    1. **Dash typography**, deterministically. The model emits the English
       tight em-dash in every language; RU/UK want a non-breaking space before
       it and PL wants a half-dash. A rule the model follows nine times in ten
       is a rule the editor has to check ten times in ten.
    2. **Internal links**, per sibling, because the language prefix and the
       article slug are both per-language and a translation pass must not be
       trusted to rewrite a URL (NTS_093 §Архитектурное решение).

    Banned phrases are reported rather than rewritten: deleting a phrase
    mechanically leaves a broken sentence, and the count is what tells the
    operator a per-language list needs work (NTS_072).
    """
    from pipeline.generator.comment_writer import parse_voice_guardrails
    from pipeline.generator.composition import normalise_dashes, strip_banned_phrases
    from pipeline.generator.internal_links import link_draft
    from pipeline.selector.editorial_guard import load_brand_taxonomy

    body = normalise_dashes(draft.body, language.value)
    banned, _examples = parse_voice_guardrails(
        getattr(brand, "voice_profile_yaml", "") or "", language
    )
    survivors = strip_banned_phrases(body, banned)
    if survivors:
        log.info(
            "production.banned_phrases_survived",
            language=language.value,
            phrases=survivors[:6],
        )
    try:
        body, _placed = await link_draft(
            body=body,
            language=language.value,
            category=category,
            brand_id_fk=brand_id_fk,
            topic_id=topic.id,
            taxonomy=load_brand_taxonomy(brand_id_fk),
            anchor_pool=(),
        )
    except Exception as exc:
        log.warning(
            "production.linking_failed",
            language=language.value,
            err=str(exc)[:200],
        )
    draft.body = body
    return draft


def _stages_to_run(return_scope: str | None) -> dict[str, bool]:
    """Which stages a regeneration actually re-runs (NTS_100 §5).

    "Регенерация перезапускает **только** указанный этап и всё после него;
    документ и fact pack не перечитываются." So ``translation:uk`` re-runs the
    UK translation and the Sanity write, and buys neither research nor a new
    English canon — one return, one paid stage.

    A ``None`` scope (a fresh candidate, or a return with no scope recorded)
    runs everything, which is the safe direction: producing too much costs
    money, producing too little ships a half-built article.
    """
    scope = (return_scope or "").strip().lower()
    if not scope:
        return {"research": True, "canon": True, "translations": True, "cover": True}
    if scope.startswith("translation:"):
        return {
            "research": False,
            "canon": False,
            "translations": True,
            "cover": False,
        }
    if scope in ("cover", "image"):
        return {
            "research": False,
            "canon": False,
            "translations": False,
            "cover": True,
        }
    if scope in ("sources", "plan"):
        return {"research": True, "canon": True, "translations": True, "cover": False}
    # "text", "blocks", anything else: rewrite from the canon down, keep the
    # research that was already paid for.
    return {"research": False, "canon": True, "translations": True, "cover": False}


def _languages_from_scope(
    languages: list[Language], return_scope: str | None
) -> list[Language]:
    """``translation:uk`` narrows the fanout to UK; anything else keeps it."""
    scope = (return_scope or "").strip().lower()
    if not scope.startswith("translation:"):
        return languages
    wanted = scope.split(":", 1)[1].strip()
    picked = [lang for lang in languages if lang.value == wanted]
    if not picked:
        log.warning("production.unknown_return_language", scope=return_scope)
        return languages
    return picked


class DocumentMissing(RuntimeError):  # noqa: N818 — a verdict, not a crash
    """No usable primary document. The candidate waits; it does not fail.

    NTS_101 §7 / NTS_123 S5: without a document the article is not written.
    Raised out of :func:`produce_candidate` and caught by the run, which parks
    the candidate in ``doc_missing`` rather than counting an attempt against
    ``max_attempts`` — a regulator being slow is not the pipeline failing.
    """


async def produce_candidate(
    *,
    candidate_id: int,
    brand: Any,  # run.BrandConfig
    brand_id_fk: int,
    brand_slug: str,
    languages: list[Language],
    sanity_publisher: Any,
    config: Any,
    run_id: int | None,
    dry_run: bool,
    tag: str | None,
    stats: ProductionStats,
    sources: Sequence[Any] = (),
) -> dict[str, Any]:
    """One candidate, from ``in_production`` to ``drafted``.

    Raises on any failure — the caller owns the rollback, so that the "back to
    pending, attempts+1, reason on the row" rule (NTS_100 §4) has exactly one
    implementation instead of one per except-branch.
    """
    from pipeline.admin.cost_recorder import CostContext, cost_context
    from pipeline.admin.db import get_session_factory
    from pipeline.admin.models import Candidate
    from pipeline.common.display_date import compute_display_date
    from pipeline.generator.composition import (
        Plan,
        build_data_blocks,
        build_plan,
        check_attribution,
        compute_depth_final,
        depth_guidance,
    )
    from pipeline.generator.fact_pack_store import (
        load_latest_fact_pack,
        persist_fact_pack,
    )
    from pipeline.generator.research import PrimaryDocument, ResearchBudget, fact_pack_from_dict
    from pipeline.publisher.sanity import SanityPostInput
    from pipeline.run import (
        _attach_fact_pack_to_draft,
        _fact_pack_as_dict,
        _order_languages_en_first,
        assign_category,
        build_fact_pack_for_topic,
        generate_draft_for_language,
        generate_image_for_topic,
        translate_draft_for_language,
    )
    from pipeline.selector.candidate_lifecycle import (
        exceeds_cost_cap,
        link_candidate_to_draft,
    )
    from pipeline.sources.document_fetcher import (
        FetchBudget,
        resolve_document,
        select_sections,
    )

    with get_session_factory()() as session:
        row = session.get(Candidate, candidate_id)
        if row is None:
            raise LookupError(f"candidate {candidate_id} vanished mid-run")
        topic = _topic_from_candidate(row, brand_slug, tag=tag)
        return_scope = row.return_scope
        primary_doc_url = row.primary_doc_url
        snapshot = _CandidateSnapshot.of(row)
    # The licence class lives on the source, not on the candidate: it is a
    # property of who published the document, and it decides how much of that
    # document may be quoted (NTS_108 §1).
    snapshot = snapshot.with_license(
        next(
            (
                getattr(src, "license_class", None)
                for src in sources
                if getattr(src, "id", None) == snapshot.source_id_fk
            ),
            None,
        )
    )

    stages = _stages_to_run(return_scope)
    fanout = _languages_from_scope(languages, return_scope)
    doc_budget = FetchBudget.from_config(config)
    primary_document: PrimaryDocument | None = None
    doc_sections: list[str] = []

    # Every paid call from here down is charged to THIS candidate — the
    # production path is the one that knows the id before it spends anything,
    # which is what ``CostContext.candidate_id`` exists for (NTS_121 §6).
    with cost_context(
        CostContext(
            brand_id_fk=brand_id_fk, run_id=run_id, candidate_id=candidate_id
        )
    ):
        # --- the primary document, BEFORE research (NTS_101 §2-7) ---------
        # Ordered first on purpose. NTS_123 S5 names this as the main cause of
        # the invented specifics: research asked for figures and dates with
        # nothing authoritative in front of it, so the only place to get them
        # was the model's memory. A regeneration keeps the document it already
        # has — the scope rules say the document is not re-read.
        if stages["research"]:
            outcome = await resolve_document(
                candidate=snapshot,
                sources=sources,
                budget=doc_budget,
                now=datetime.now(tz=UTC),
            )
            _record_document_outcome(
                candidate_id=candidate_id, outcome=outcome, now=datetime.now(tz=UTC)
            )
            if not outcome.usable:
                stats.doc_missing += 1
                raise DocumentMissing(
                    f"{outcome.status}: {outcome.reason or 'no document'}"
                )
            document = outcome.document
            assert document is not None  # guarded by outcome.usable
            selection = select_sections(
                document.text,
                hint=snapshot.primary_doc_hint,
                headline=snapshot.source_title,
                max_tokens=doc_budget.max_tokens_for_composition,
            )
            doc_sections = selection.sections_used
            primary_document = PrimaryDocument(
                url=document.url,
                text=selection.text,
                as_of=document.as_of.date().isoformat(),
                sections_used=tuple(doc_sections),
            )
            primary_doc_url = document.url
            _store_document_link(
                candidate_id=candidate_id,
                version_id=document.version_id,
                doc_match=(outcome.match.column_value if outcome.match else None),
                url=document.url,
                sections=doc_sections,
            )
            log.info(
                "production.document",
                candidate_id=candidate_id,
                url=document.url,
                how=outcome.how,
                match=outcome.match.verdict if outcome.match else None,
                sections=f"{len(doc_sections)}/{selection.sections_total}",
                cached=document.from_cache,
            )

        # --- research: reuse before buying (NTS_100 §4) -------------------
        stored = load_latest_fact_pack(candidate_id)
        fact_pack = None
        fact_pack_id: int | None = None
        if stored is not None:
            fact_pack_id, stored_pack = stored
            fact_pack = fact_pack_from_dict(stored_pack)
            if fact_pack is not None:
                stats.reused_fact_packs += 1
                log.info(
                    "production.fact_pack_reused",
                    candidate_id=candidate_id,
                    fact_pack_id=fact_pack_id,
                    facts=fact_pack.fact_count,
                )
        if fact_pack is None and stages["research"]:
            fact_pack = await build_fact_pack_for_topic(
                topic,
                research_enabled=bool(getattr(config, "research_enabled", True)),
                budget=ResearchBudget.from_config(config),
                document=primary_document,
            )
            fact_pack_id = persist_fact_pack(
                brand_id_fk=brand_id_fk,
                candidate_id=candidate_id,
                topic_id=topic.id,
                pack=_fact_pack_as_dict(fact_pack),
                sources=tuple(fact_pack.citations) if fact_pack else (),
                primary_doc_url=primary_doc_url,
                doc_sections_used=doc_sections or None,
                doc_text=primary_document.text if primary_document else None,
                model=fact_pack.model if fact_pack else None,
            )
        if fact_pack is None:
            # Not a failure — the article ships thin and says so, exactly as on
            # the v2 path (NTS_092). The count is what makes a broken research
            # stage loud instead of quietly worse.
            log.warning(
                "production.thin", candidate_id=candidate_id, reason="no_fact_pack"
            )

        # --- depth_final and the plan (NTS_102 v2 §1, §3) -----------------
        # Depth comes from the material, never from the guard's guess: the
        # guard read an abstract. Twelve facts with no comparable pair is an
        # ``article``, because ``deep`` promises a table and a table needs
        # pairs.
        decision = compute_depth_final(
            fact_pack,
            article_min_facts=int(getattr(config, "depth_article_min_facts", 4)),
            deep_min_facts=int(getattr(config, "depth_deep_min_facts", 10)),
        )
        targets = dict(getattr(config, "depth_length_targets", {}) or {})
        guidance = depth_guidance(decision.depth, targets, decision)
        _store_depth(candidate_id=candidate_id, depth=decision.depth)
        log.info(
            "production.depth",
            candidate_id=candidate_id,
            depth_prior=snapshot.depth_prior,
            **decision.as_log(),
        )

        plan = Plan()
        if stages["canon"]:
            plan = await build_plan(
                title=topic.raw.title,
                summary=topic.raw.summary,
                fact_pack=fact_pack,
                document_text=primary_document.text if primary_document else "",
                document_url=primary_document.url if primary_document else "",
                depth=decision.depth,
                targets=targets,
                model=str(getattr(config, "attribution_model", "gpt-4o-mini")),
            )
            if fact_pack_id is not None:
                _store_plan(fact_pack_id, plan.as_dict())

        # --- cover (NTS_112) ---------------------------------------------
        # ``data`` draws it from the article's own figures: free, deterministic
        # on the candidate id, and different for two articles in a way the
        # diffusion path never was. The Sanity write happens with the drafts,
        # so here we only build the bytes and upload the asset.
        asset_id = None
        images_on_demand = bool(getattr(config, "images_on_demand", False))
        if stages["cover"] and not images_on_demand and not dry_run:
            cover_mode = str(getattr(config, "cover_mode", "flux") or "flux")
            try:
                if cover_mode == "data":
                    asset_id = await _upload_data_cover(
                        candidate_id=candidate_id,
                        topic_id=topic.id,
                        fact_pack=fact_pack,
                        document_sections=len(doc_sections),
                        sanity_publisher=sanity_publisher,
                    )
                else:
                    asset_id = await generate_image_for_topic(
                        topic, brand, sanity_publisher
                    )
            except Exception:
                # A missing cover is not a lost article — the manager can
                # generate one from the card (NTS_091/094).
                log.exception("production.cover_failed", candidate_id=candidate_id)
                asset_id = None

        category = await assign_category(topic.raw, brand)
        display_date_val, _src = compute_display_date(
            topic.raw.published_at, datetime.now(tz=UTC)
        )
        display_date_iso = display_date_val.isoformat()

        # --- the EN canon, from the plan (NTS_102 v2) ---------------------
        en_draft = await generate_draft_for_language(
            topic,
            brand,
            Language.en,
            fact_pack=fact_pack,
            plan=plan.render(),
            depth_guidance=guidance,
            primary_document=(
                f"URL: {primary_document.url}\nRead on: {primary_document.as_of}\n"
                f"Sections included: {', '.join(primary_document.sections_used) or 'all'}\n\n"
                f"{primary_document.text}"
                if primary_document
                else ""
            ),
        )

        # --- ATTRIBUTION, before a single translation (NTS_096 §C) --------
        # The whole reason this stage sits here: NTS_096's reference case is a
        # right number attached to a wrong claim, which every other defence
        # passes. Checked after translation it would be bought four times.
        report = await check_attribution(
            body=f"{en_draft.title}\n\n{en_draft.body}",
            fact_pack=fact_pack,
            document_text=primary_document.text if primary_document else "",
            license_class=snapshot.license_class,
            max_quote_words=getattr(config, "max_quote_words", {}),
            model=str(getattr(config, "attribution_model", "gpt-4o-mini")),
        )
        if report.needs_fix:
            # Exactly one cycle (NTS_102 v2 §2). A second would be a loop with
            # a model on both ends of it.
            log.info(
                "production.attribution_repair",
                candidate_id=candidate_id,
                **report.counts(),
            )
            writer = _writer_for(brand)
            en_draft = await writer.repair_attribution(
                en_draft, report.fix_instructions(), Language.en
            )
            report = await check_attribution(
                body=f"{en_draft.title}\n\n{en_draft.body}",
                fact_pack=fact_pack,
                document_text=primary_document.text if primary_document else "",
                license_class=snapshot.license_class,
                max_quote_words=getattr(config, "max_quote_words", {}),
                model=str(getattr(config, "attribution_model", "gpt-4o-mini")),
            )
        needs_attention = bool(report.distorted or report.flagged)
        if fact_pack_id is not None:
            _store_attribution(fact_pack_id, report.as_dict())
        if needs_attention:
            # The draft is still created — the check advises, it does not block
            # (NTS_096 §C) — but the review card opens on these claims.
            log.warning(
                "production.needs_attention",
                candidate_id=candidate_id,
                **report.counts(),
            )
        _store_needs_attention(candidate_id=candidate_id, value=needs_attention)

        # --- data blocks, from the pack only (NTS_095, NTS_102 v2 §1b) ----
        blocks = build_data_blocks(
            fact_pack,
            depth=decision.depth,
            enabled=bool(getattr(config, "data_blocks_enabled", False)),
        )
        if decision.depth == "deep" and not blocks:
            # NTS_102 v2 §1b asks for this to be a metric, not a shrug: more
            # than 30% of deep articles without a block means the thresholds
            # are set too low.
            log.info("production.deep_without_blocks", candidate_id=candidate_id)

        # --- translations, only now (NTS_102 v2 §2) -----------------------
        drafts: list[tuple[Language, Draft]] = []
        for language in _order_languages_en_first(fanout):
            if language == Language.en:
                drafts.append((Language.en, en_draft))
                continue
            drafts.append(
                (
                    language,
                    await translate_draft_for_language(
                        topic, brand, language, en_draft
                    ),
                )
            )

        # --- deterministic post-process + links, per sibling (NTS_093) ----
        drafts = [
            (language, await _finish_sibling(
                draft=draft,
                language=language,
                brand=brand,
                brand_id_fk=brand_id_fk,
                topic=topic,
                category=category,
                config=config,
            ))
            for language, draft in drafts
        ]

        posts = [
            SanityPostInput(
                title=draft.title,
                body_markdown=draft.body,
                language=language,
                category=category,
                source_url=str(topic.raw.url),
                topic_id=topic.id,
                key_takeaway=draft.key_takeaway,
                cover_image_asset_id=asset_id,
                cover_image_alt=draft.title[:120],
                display_date=display_date_iso,
            )
            for language, draft in drafts
        ]

        if dry_run:
            log.info(
                "production.dry_run",
                candidate_id=candidate_id,
                languages=[lang.value for lang, _ in drafts],
                title=posts[0].title if posts else None,
            )
            return {
                "candidate_id": candidate_id,
                "status": "dry_run",
                "languages": [lang.value for lang, _ in drafts],
                "title": posts[0].title if posts else None,
                "thin": fact_pack is None,
            }

        # One transaction for the whole set (NTS_100 §4): a half-written
        # article in the Studio is worse than none, because nothing downstream
        # can tell it is half-written.
        draft_ids = await sanity_publisher.publish_draft_batch(posts)
        en_index = next(
            (i for i, (lang, _) in enumerate(drafts) if lang == Language.en), None
        )
        canonical_id = draft_ids[en_index] if en_index is not None else draft_ids[0]

        linked = link_candidate_to_draft(
            candidate_id=candidate_id,
            sanity_draft_id=canonical_id,
            brand_id_fk=brand_id_fk,
        )
        if not linked:
            # The draft exists; the candidate refused the link. Loud, because
            # this is precisely the half-state NTS_122 §1 was about.
            log.error(
                "production.link_refused",
                candidate_id=candidate_id,
                draft_id=canonical_id,
            )
            raise RuntimeError(
                f"candidate {candidate_id} would not accept draft {canonical_id}"
            )
        if fact_pack_id is not None:
            _attach_fact_pack_to_draft(fact_pack_id, canonical_id)

        # --- the judge, in the real loop at last (NTS_080, S10) -----------
        # Advisory and after the draft exists, so a dead judge cannot block a
        # publication. It reads the fact pack as well as the body: NTS_092
        # established that withholding the pack makes every researched figure
        # read as invented, and the judge would then flag the pipeline for
        # doing exactly what it was asked.
        try:
            from pipeline.admin.judge import score_draft

            source_text = f"{topic.raw.title}\n{topic.raw.summary or ''}"
            if fact_pack is not None and not fact_pack.is_empty():
                source_text = (
                    f"{source_text}\n\n--- WEB RESEARCH FACT PACK "
                    f"(also authoritative) ---\n{fact_pack.render()}"
                )
            await score_draft(
                draft_id=canonical_id,
                lang=Language.en.value,
                draft_text=f"{en_draft.title}\n\n{en_draft.body}",
                eval_enabled=bool(getattr(config, "eval_enabled", True)),
                eval_threshold=float(getattr(config, "eval_threshold", 7.0)),
                source_text=source_text,
                voice_profile_yaml=getattr(brand, "voice_profile_yaml", "") or "",
                brand_id_fk=brand_id_fk,
                run_id=run_id,
            )
        except Exception:
            log.exception("production.judge_failed", candidate_id=candidate_id)

        cap = float(getattr(config, "max_cost_per_candidate_usd", 0.0) or 0.0)
        if exceeds_cost_cap(candidate_id, cap):
            # After the fact by construction — the spend is only knowable once
            # it happened. The ceiling's job here is to stop the *next* attempt
            # on this candidate and to be visible in the run summary.
            log.warning(
                "production.cost_cap_exceeded",
                candidate_id=candidate_id,
                cap_usd=cap,
            )

    return {
        "candidate_id": candidate_id,
        "status": "drafted",
        "draft_id": canonical_id,
        "doc_url": primary_doc_url,
        "doc_sections": doc_sections,
        "languages": [lang.value for lang, _ in drafts],
        "title": posts[0].title if posts else None,
        "thin": fact_pack is None,
    }


# --------------------------------------------------------------------------
# 4. the run
# --------------------------------------------------------------------------


async def run_production(
    brand_slug: str = "icon",
    *,
    brand_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    cost_cap_usd: float | None = None,
    tag: str | None = None,
    triggered_by: str = "cron",
) -> ProductionStats:
    """Run contour 2 once for one brand (NTS_100 §3).

    ``force`` bypasses both ``production_enabled`` and the one-batch-per-day
    claim, for the same reason ``run_intake`` has it: a flag that cannot be
    tested before it is switched on is a flag nobody switches on. ``limit``
    caps the batch below ``weekly_draft_budget``; ``cost_cap_usd`` stops the
    loop between candidates once this run has spent that much, which is what
    makes a supervised first run on real keys safe.
    """
    from pipeline.admin.config_client import (
        AdminConfigClient,
        BrandNotReadyError,
        get_brand,
    )
    from pipeline.run import (
        BrandConfig,
        _languages_for_brand,
        _resolve_brand_image_styles,
        icon_brand_config,
    )
    from pipeline.selector.candidate_lifecycle import (
        begin_production,
        exceeds_cost_cap,
        monthly_spend_usd,
        run_spend_usd,
    )

    configure_logging()
    stats = ProductionStats()

    try:
        brand_row = get_brand(brand_id if brand_id is not None else brand_slug)
    except Exception as exc:
        raise BrandNotReadyError(
            f"brand {(brand_id or brand_slug)!r} not reachable in admin.db: {exc!s}"
        ) from exc
    if brand_row.status != "active":
        raise BrandNotReadyError(
            f"brand {brand_row.slug!r} status is {brand_row.status!r}; expected 'active'"
        )
    # Unlike intake, this run writes to Sanity, so it needs the credentials up
    # front rather than at the last stage of the most expensive candidate.
    if not dry_run and (
        not brand_row.has_sanity_token or not brand_row.sanity_project_id
    ):
        raise BrandNotReadyError(
            f"brand {brand_row.slug!r} has no Sanity credentials configured"
        )

    brand_id_fk = brand_row.id
    client = AdminConfigClient(brand_slug=brand_row.slug)
    config = client.get_config()
    now = datetime.now(tz=UTC)
    tz_name = getattr(config, "brand_timezone", None)
    today = now.astimezone(resolve_timezone(tz_name)).date()
    stats.batch_date = today.isoformat()

    if not getattr(config, "production_enabled", False) and not force:
        # A flag that is off is the shipped state (NTS_103 шаг 3), so it is a
        # normal outcome with a terminal run row — the same treatment intake
        # got in the 2026-08-28 hotfix, and for the same reason: a traceback
        # every morning trains the operator to ignore the one channel that has
        # to stay meaningful.
        cancelled_id = client.record_run_start(
            source_ids=[], triggered_by=triggered_by, run_type="production"
        )
        stats.stopped_reason = "production_enabled is off"
        client.record_run_finish(
            cancelled_id,
            status="cancelled",
            stats=stats.as_dict(),
            log_excerpt=(
                f"production_enabled is OFF for brand {brand_row.slug!r} "
                "(NTS_103 шаг 3). No candidate was selected and nothing was "
                "generated. Switch it on in Settings, or run with --force for "
                "a one-off."
            ),
        )
        raise ProductionDisabled(
            f"production_enabled is off for brand {brand_row.slug!r} — "
            "switch it on in Settings, or pass force=True for a one-off run"
        )

    run_id = client.record_run_start(
        source_ids=[], triggered_by=triggered_by, run_type="production"
    )
    log_lines: list[str] = []

    # --- the daily passes, before anything is selected -------------------
    # Ordered deliberately: a candidate whose TTL ran out overnight must not be
    # ranked this morning, and one stuck in ``in_production`` from a crashed
    # run must be back in ``pending`` before the budget is counted, or the week
    # would be charged for work that never happened.
    max_attempts = int(getattr(config, "max_attempts", 2))
    stats.expired = expire_stale_candidates(brand_id_fk=brand_id_fk, now=now)
    stats.expired += expire_exhausted_doc_searches(
        brand_id_fk=brand_id_fk,
        doc_retries=int(getattr(config, "doc_retries", 2)),
        now=now,
    )
    swept = sweep_production_timeouts(
        brand_id_fk=brand_id_fk,
        timeout_minutes=int(getattr(config, "production_timeout_min", 60)),
        max_attempts=max_attempts,
        now=now,
    )
    stats.timed_out = swept["released"] + swept["failed"]
    stats.failed += swept["failed"]
    pruned = prune_old_candidates(
        brand_id_fk=brand_id_fk,
        retention_days_rejected=int(
            getattr(config, "retention_days_rejected", 30)
        ),
        now=now,
    )
    stats.pruned = pruned["rejected"] + pruned["terminal"]
    log_lines.append(
        f"sweep: expired={stats.expired} released={swept['released']} "
        f"failed={swept['failed']} pruned={stats.pruned} "
        f"kept_with_decisions={pruned['kept_with_decisions']}"
    )

    # --- the spend kill-switch (NTS_106 §3) ------------------------------
    monthly_cap = float(getattr(config, "monthly_spend_cap_usd", 0.0) or 0.0)
    spent_this_month = monthly_spend_usd(brand_id_fk, now=now)
    if monthly_cap > 0 and spent_this_month >= monthly_cap:
        # "При 100% production не стартует" — intake keeps running, which is
        # the point of stopping the expensive contour and not the cheap one.
        stats.stopped_reason = "monthly_spend_cap"
        log.error(
            "production.monthly_cap_reached",
            brand=brand_row.slug,
            spent_usd=round(spent_this_month, 2),
            cap_usd=monthly_cap,
        )
        client.record_run_finish(
            run_id,
            status="cancelled",
            stats=stats.as_dict(),
            log_excerpt=(
                f"monthly_spend_cap_usd reached: ${spent_this_month:.2f} of "
                f"${monthly_cap:.2f} (NTS_106 §3). Production does not start; "
                "intake is unaffected. Raise the cap in Settings to resume."
            ),
        )
        return stats

    # --- one batch per brand per day (NTS_100 §3.3) ----------------------
    if not force and not claim_batch(
        brand_id_fk=brand_id_fk, batch_date=today, run_id=run_id, now=now
    ):
        stats.stopped_reason = "batch_already_run"
        client.record_run_finish(
            run_id,
            status="success",
            stats=stats.as_dict(),
            log_excerpt=(
                f"production batch for {today.isoformat()} was already claimed "
                "by an earlier run today (NTS_100 §3.3). Nothing selected — "
                "this is the designed no-op, not a failure."
            ),
        )
        log.info("production.noop_batch_taken", brand=brand_row.slug)
        return stats

    # --- the weekly budget (NTS_100 §3.1-3.2) ----------------------------
    budget = int(getattr(config, "weekly_draft_budget", 6))
    taken, category_counts, jurisdiction_counts = taken_this_week(
        brand_id_fk=brand_id_fk, now=now, timezone_name=tz_name
    )
    stats.weekly_budget = budget
    stats.taken_this_week = taken
    slots_left = budget - taken
    if limit is not None:
        slots_left = min(slots_left, int(limit))
    log_lines.append(
        f"budget: weekly={budget} taken={taken} slots={max(0, slots_left)}"
    )
    if slots_left <= 0:
        stats.stopped_reason = "weekly_budget_exhausted"
        client.record_run_finish(
            run_id,
            status="success",
            stats=stats.as_dict(),
            log_excerpt="\n".join(log_lines),
        )
        log.info(
            "production.budget_exhausted",
            brand=brand_row.slug,
            budget=budget,
            taken=taken,
        )
        return stats

    # --- rank and select -------------------------------------------------
    facts = eligible_candidates(
        brand_id_fk=brand_id_fk,
        now=now,
        doc_retries=int(getattr(config, "doc_retries", 2)),
    )
    stats.eligible = len(facts)
    picks = select_batch(
        facts,
        weights=RankWeights.from_config(config),
        tiers=getattr(config, "jurisdiction_tiers", {}) or {},
        now=now,
        limit=slots_left,
        category_counts=category_counts,
        jurisdiction_counts=jurisdiction_counts,
    )
    log_lines.append(f"eligible={stats.eligible} picked={len(picks)}")
    if not picks:
        # NTS_100 §3.5 — an empty portfolio is a valid outcome, not a failure.
        # The alert for it is the thin-portfolio one, three days before a slot,
        # and it is raised by the monitoring pass rather than here.
        stats.stopped_reason = "empty_portfolio"
        _record_batch_size(brand_id_fk=brand_id_fk, batch_date=today, selected=0)
        client.record_run_finish(
            run_id,
            status="success",
            stats=stats.as_dict(),
            log_excerpt="\n".join(log_lines),
        )
        log.info("production.empty_portfolio", brand=brand_row.slug)
        return stats

    # --- brand config for the generation seams ---------------------------
    voice_yaml = (
        brand_row.voice_profile_yaml
        or config.voice_profile
        or icon_brand_config().voice_profile_yaml
    )
    from pipeline.generator.image import BrandVisual

    base = icon_brand_config()
    brand = BrandConfig(
        slug=brand_row.slug,
        name=brand_row.name,
        voice_profile_yaml=voice_yaml,
        visual=BrandVisual(
            brand_id=brand_row.slug,
            image_style_prompts=_resolve_brand_image_styles(voice_yaml),
        ),
        context=base.context,
        categories=base.categories,
        id_fk=brand_id_fk,
    )
    languages = _languages_for_brand(brand_row)
    # The registry, read once: the document stage refuses any URL whose domain
    # is not in it (NTS_101 §2), so it needs the whole source list, not just
    # the candidate's own row.
    source_rows = client.get_active_sources()

    if dry_run:
        sanity_publisher: Any = _DryRunPublisher()
    else:
        from pipeline.publisher.sanity import SanityClient, SanityPublisher

        sanity_publisher = SanityPublisher(
            client=SanityClient(
                project_id=brand_row.sanity_project_id or "",
                dataset=brand_row.sanity_dataset or "production",
                api_version=brand_row.sanity_api_version or "2024-01-01",
                token=brand_row.decrypted_sanity_token() or "",
            )
        )

    candidate_cap = float(getattr(config, "max_cost_per_candidate_usd", 0.0) or 0.0)
    key = batch_key(brand_row.slug, today)

    for pick in picks:
        candidate_id = pick.candidate_id
        if cost_cap_usd is not None:
            spent = run_spend_usd(run_id)
            stats.spend_usd = spent
            if spent >= float(cost_cap_usd):
                stats.stopped_reason = "run_cost_cap"
                log.warning(
                    "production.run_cap_reached",
                    spent_usd=round(spent, 4),
                    cap_usd=cost_cap_usd,
                )
                break
        if candidate_cap > 0 and exceeds_cost_cap(candidate_id, candidate_cap):
            # A retry of a candidate that already burned its budget. Skipping
            # is the point of the ceiling: the previous attempt's spend is
            # exactly the evidence that a second one is not free.
            log.warning(
                "production.skipped_over_cap",
                candidate_id=candidate_id,
                cap_usd=candidate_cap,
            )
            stats.candidates.append(
                {"candidate_id": candidate_id, "status": "skipped_over_cap"}
            )
            continue
        if not claim_pending(candidate_id, now=datetime.now(tz=UTC)):
            # Someone else took it between ranking and here — the exact race
            # ``claim_pending`` exists for. Not an error.
            log.info("production.claim_lost", candidate_id=candidate_id)
            continue
        if not begin_production(
            candidate_id=candidate_id, brand_id_fk=brand_id_fk, batch_key=key
        ):
            log.warning("production.begin_refused", candidate_id=candidate_id)
            continue
        stats.selected += 1
        log.info("production.candidate_start", **pick.as_log())
        try:
            result = await produce_candidate(
                candidate_id=candidate_id,
                brand=brand,
                brand_id_fk=brand_id_fk,
                brand_slug=brand_row.slug,
                languages=languages,
                sanity_publisher=sanity_publisher,
                config=config,
                run_id=run_id,
                dry_run=dry_run,
                tag=tag,
                stats=stats,
                sources=source_rows,
            )
        except DocumentMissing as exc:
            # NTS_101 §7 — not a failure and not an attempt. The candidate goes
            # to ``doc_missing`` and comes back in 48 hours; charging this to
            # ``max_attempts`` would retire candidates for a regulator's
            # publishing schedule.
            park_document_missing(candidate_id=candidate_id, reason=str(exc))
            stats.candidates.append(
                {
                    "candidate_id": candidate_id,
                    "status": "doc_missing",
                    "reason": str(exc)[:300],
                }
            )
            log_lines.append(f"candidate {candidate_id}: doc_missing — {exc}")
            continue
        except Exception as exc:
            status = release_to_pending(
                candidate_id=candidate_id,
                error=f"{type(exc).__name__}: {exc}",
                max_attempts=max_attempts,
            )
            stats.failed += 1
            stats.candidates.append(
                {
                    "candidate_id": candidate_id,
                    "status": status,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
            log.exception("production.candidate_failed", candidate_id=candidate_id)
            log_lines.append(f"candidate {candidate_id}: FAILED → {status}")
            continue
        stats.drafted += 1
        if result.get("doc_url"):
            stats.doc_found += 1
        stats.candidates.append(result)
        log_lines.append(
            f"candidate {candidate_id}: {result['status']} "
            f"{result.get('draft_id', '')} langs={result.get('languages')}"
        )

    _record_batch_size(
        brand_id_fk=brand_id_fk, batch_date=today, selected=stats.selected
    )
    stats.spend_usd = run_spend_usd(run_id)
    status = "dry_run" if dry_run else ("success" if not stats.failed else "failed")
    client.record_run_finish(
        run_id,
        status=status,
        stats=stats.as_dict(),
        log_excerpt="\n".join(log_lines)[-4000:],
    )
    log.info(
        "production.done",
        brand=brand_row.slug,
        run_id=run_id,
        **{
            k: v
            for k, v in stats.as_dict().items()
            if k != "candidates"
        },
    )
    return stats


class _DryRunPublisher:
    """Stands in for ``SanityPublisher`` when ``--dry-run`` is set.

    Only the two methods production calls. A dry run still pays for research,
    drafting and translation — it is a *publish* switch, not a spend switch,
    and the CLI help says so.
    """

    async def publish_draft_batch(self, posts) -> list[str]:
        return [f"drafts.dry-run-{i}" for i, _ in enumerate(posts)]

    async def upload_cover_image(self, image_bytes: bytes, filename: str) -> str:
        return "image-dryrun"


# --- CLI ------------------------------------------------------------------


@app.command()
def main(
    brand: str = typer.Option("icon", "--brand", "--brand-slug"),
    brand_id: int | None = typer.Option(None, "--brand-id"),
    limit: int | None = typer.Option(
        None, "--limit", help="cap the batch below weekly_draft_budget"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="skip the Sanity write. Still pays for research and drafting.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="run even when production_enabled is off, and ignore today's batch",
    ),
    db: str | None = typer.Option(
        None, "--db", help="admin.db to use (sets ADMIN_DB_PATH for this run)"
    ),
    cost_cap_usd: float | None = typer.Option(
        None, "--cost-cap-usd", help="stop between candidates above this spend"
    ),
    tag: str | None = typer.Option(
        None, "--tag", help="mark the drafts' titles/slugs, e.g. for an e2e proof"
    ),
    triggered_by: str = typer.Option("cron", "--triggered-by"),
) -> None:
    """Run contour 2 (selection + production) once."""
    import asyncio
    import os

    if db:
        # Set before any admin.db import resolves the path — this is the flag
        # that keeps an e2e proof off the production database.
        os.environ["ADMIN_DB_PATH"] = db
        from pipeline.admin import db as admin_db
        from pipeline.common import config as config_module

        config_module._settings = None
        admin_db.reset_for_tests()

    try:
        stats = asyncio.run(
            run_production(
                brand_slug=brand,
                brand_id=brand_id,
                limit=limit,
                dry_run=dry_run,
                force=force,
                cost_cap_usd=cost_cap_usd,
                tag=tag,
                triggered_by=triggered_by,
            )
        )
    except ProductionDisabled as exc:
        # The expected daily outcome while the flag is off; the run row is
        # already written as ``cancelled``. systemd reads the exit code.
        typer.echo(str(exc))
        return
    typer.echo(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":  # pragma: no cover
    app()
