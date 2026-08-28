"""Contour 1: the intake run (NTS_098 §5, NTS_099, NTS_103 шаг 1).

    fetch → dedup → prefilter → guard → candidates

**Not one paid generation call.** No draft, no polish, no translation, no
cover, no research. The only money this run spends is one embedding and at most
one cheap guard completion per item — cents a day against the tens of dollars
the v2 generation run spent producing articles the rubric itself classifies as
rejects. That property is the entire point of the shadow week (NTS_103 шаг 1)
and it is pinned by a test that fails if this module ever grows an import from
``pipeline.generator``.

Stage order is NTS_098 §5's, not the cheapest one: dedup before prefilter, even
though the prefilter is free and dedup costs an embedding. The funnel names in
NTS_106 §2 — ``fetched → after_dedup → after_prefilter → guarded → accepted`` —
are what the operator reads every morning, and reordering the stages to save
$0.01 a day would make those numbers mean something other than what the spec
says they mean.

Deliberately kept out of here and named in the session log instead:

* **The state machine past ``pending``.** Intake writes ``pending`` (accept)
  and ``rejected`` (reject) and stops. Selection, production and the TTL pass
  are S4's, against the same rows.
* **``html_list`` / ``edgar_fts`` fetching.** Two of the twelve primary feeds
  need a fetcher that arrives in S5 (NTS_101 §2-7); migration 022 inserts them
  inactive, and this module records a health failure rather than pretending.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import typer

from pipeline.common.logging import configure_logging, get_logger
from pipeline.common.models import RawItem
from pipeline.selector.candidate_dedup import (
    CandidateDedupConfig,
    check_post_guard,
    check_pre_guard,
)
from pipeline.selector.candidate_store import (
    CandidateInput,
    count_accepted_today,
    create_candidate,
    mark_superseded,
    recent_accepted_titles,
)
from pipeline.selector.dedup import jaccard, normalize_title
from pipeline.selector.dedup_service import embed_text
from pipeline.selector.editorial_guard import (
    DEFAULT_GUARD_MODEL,
    RECENT_ACCEPTED_LIMIT,
    GuardDeferred,
    GuardSchemaError,
    judge_item,
    load_brand_taxonomy,
    render_jurisdiction_tiers,
    render_services,
    resolve_guard_template,
)
from pipeline.selector.prefilter import (
    PrefilterRules,
    drop_rate,
    prefilter_item,
)

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)

# Feed items an intake reads per source. Higher than v2's ``limit`` of 3
# because intake does not generate: the cost of looking at an item is an
# embedding, and the portfolio is only as good as the funnel feeding it.
INTAKE_ITEMS_PER_SOURCE = 50

# L1 title dedup inside one run (NTS_079 level 1).
_JACCARD_DUP = 0.70

_FUNNEL_STAGES = (
    "fetched",
    "after_dedup",
    "after_prefilter",
    "guarded",
    "accepted",
    "rejected",
    "guard_errors",
    "deferred",
)


class IntakeDisabled(RuntimeError):  # noqa: N818 — a refusal, not a failure
    """``intake_enabled`` is off for this brand (NTS_103 шаг 1)."""


@dataclass
class IntakeStats:
    """The funnel, in absolute numbers, split by ``input_kind`` (NTS_106 §2).

    Absolute, not rates: "the rubric is strict" and "the parser stopped
    returning anything" produce the same empty portfolio, and only the counts
    tell them apart.
    """

    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    prefilter_drops: dict[str, int] = field(default_factory=dict)
    reason_codes: dict[str, int] = field(default_factory=dict)
    dedup_windows: dict[str, int] = field(default_factory=dict)
    cap_overflow: int = 0
    superseded: int = 0
    embed_failures: int = 0
    source_errors: int = 0

    def bump(self, input_kind: str, stage: str, by: int = 1) -> None:
        kind = self.by_kind.setdefault(
            input_kind, dict.fromkeys(_FUNNEL_STAGES, 0)
        )
        kind[stage] = kind.get(stage, 0) + by

    def total(self, stage: str) -> int:
        return sum(k.get(stage, 0) for k in self.by_kind.values())

    @property
    def prefilter_drop_rate(self) -> float:
        """NTS_099 §1 — the metric that says whether the prefilter works."""
        return drop_rate(
            considered=self.total("after_dedup"),
            dropped=self.total("after_dedup") - self.total("after_prefilter"),
        )

    @property
    def guard_error_rate(self) -> float:
        """NTS_106 §1 — alert above 20% for a run."""
        attempted = self.total("guarded") + self.total("guard_errors")
        if attempted <= 0:
            return 0.0
        return self.total("guard_errors") / attempted

    def as_dict(self) -> dict[str, Any]:
        """The ``runs.stats`` payload (NTS_106 §5: stats JSON per mode)."""
        return {
            "run_type": "intake",
            "funnel": {
                stage: self.total(stage) for stage in _FUNNEL_STAGES
            },
            "by_input_kind": self.by_kind,
            "prefilter_drops": self.prefilter_drops,
            "prefilter_drop_rate": round(self.prefilter_drop_rate, 4),
            "guard_error_rate": round(self.guard_error_rate, 4),
            "reason_codes": self.reason_codes,
            "dedup_windows": self.dedup_windows,
            "cap_overflow": self.cap_overflow,
            "superseded": self.superseded,
            "embed_failures": self.embed_failures,
            "source_errors": self.source_errors,
            # ``errors`` is what the admin run views and the Telegram pulse
            # already read off every run row; without it an intake with a dead
            # source would render as a clean success.
            "errors": self.source_errors,
        }


def input_kind_for(source_role: str) -> str:
    """``primary_feed``/``primary_site`` → ``document``, else ``news``.

    NTS_099 §2. The role is the only thing that decides this: for a regulator's
    own feed the annotation is written by the document's author, which is the
    whole reason v3 prefers those feeds (NTS_099 §"Риски").
    """
    return "document" if source_role in ("primary_feed", "primary_site") else "news"


def _topic_id_for(url: str | None, title: str) -> str:
    """Stable 16-hex id from the item URL, falling back to the title.

    Same shape as the v2 topic id so ``topic_embeddings`` rows from both
    contours can coexist in one table without a discriminator.
    """
    basis = (url or title or "").encode("utf-8")
    return hashlib.sha1(basis).hexdigest()[:16]


def _persist_embedding(
    *, topic_id: str, brand_id_fk: int, title: str, embedding: np.ndarray, model: str
) -> None:
    """Store the item's embedding so later runs can dedup against it.

    Best-effort: a candidate without a stored embedding still exists and is
    still workable — it just does not take part in similarity dedup, which is
    the right degradation (the doc-URL key and the guard's own
    ``duplicate_stage`` still apply).
    """
    try:
        from pipeline.admin.db import session_scope
        from pipeline.admin.models import TopicEmbedding

        with session_scope() as session:
            session.add(
                TopicEmbedding(
                    topic_id=topic_id,
                    brand_id_fk=brand_id_fk,
                    embedding=np.asarray(embedding, dtype=np.float32).tobytes(),
                    model=model,
                    title_norm=" ".join(sorted(normalize_title(title))),
                    created_at=datetime.now(tz=UTC),
                )
            )
    # Fail open: a candidate without a stored embedding is still workable.
    except Exception as exc:
        log.warning("intake.embedding_persist_failed", topic=topic_id, err=str(exc))


async def _fetch_items(source_record: Any, limit: int) -> list[RawItem]:
    """Fetch one source. Only feed methods are implemented before S5."""
    fetch_method = getattr(source_record, "fetch_method", None) or "rss"
    if fetch_method not in ("rss", "atom"):
        # NTS_101 §2-7 lands the document fetchers in S5. Raising here (rather
        # than returning []) puts the reason in source_health_records instead
        # of making a missing fetcher look like an empty feed.
        raise NotImplementedError(
            f"fetch_method {fetch_method!r} has no fetcher until S5 (NTS_101)"
        )
    from pipeline.sources.rss import RssSource

    source = RssSource(
        source_id=(
            str(source_record.id)
            if source_record.id is not None
            else source_record.name
        ),
        name=source_record.name,
        url=source_record.url,
    )
    items = list(await source.fetch())
    return items[:limit]


@dataclass
class _SeenItem:
    topic_id: str
    title_norm: frozenset[str]
    embedding: np.ndarray


async def run_intake(
    brand_slug: str = "icon",
    *,
    brand_id: int | None = None,
    triggered_by: str = "cron",
    limit: int = INTAKE_ITEMS_PER_SOURCE,
    force: bool = False,
    embed=None,
) -> IntakeStats:
    """Run contour 1 once for one brand.

    ``force`` bypasses ``intake_enabled`` for an operator-triggered run — the
    flag exists to keep the *cron* quiet, and refusing a hand-triggered run
    would make the flag impossible to test before switching it on.

    ``embed`` is injectable purely so tests can run the whole funnel without a
    network; production always uses :func:`pipeline.run._embed`.
    """
    from pipeline.admin.config_client import (
        AdminConfigClient,
        BrandNotReadyError,
        get_brand,
    )
    from pipeline.admin.cost_recorder import (
        CostContext,
        attach_candidate,
        collect_cost_rows,
        cost_context,
    )

    configure_logging()

    if embed is None:
        embed = embed_text

    try:
        brand_row = get_brand(brand_id if brand_id is not None else brand_slug)
    except Exception as exc:
        raise BrandNotReadyError(
            f"brand {(brand_id or brand_slug)!r} not reachable in admin.db: {exc!s}"
        ) from exc
    # Intake needs no Sanity credentials — it never writes a draft. Requiring
    # them (as run_pipeline does) would block the shadow week on a publishing
    # concern the shadow week does not have.
    if brand_row.status != "active":
        raise BrandNotReadyError(
            f"brand {brand_row.slug!r} status is {brand_row.status!r}; expected 'active'"
        )

    brand_id_fk = brand_row.id
    client = AdminConfigClient(brand_slug=brand_row.slug)
    config = client.get_config()

    if not getattr(config, "intake_enabled", False) and not force:
        log.info("intake.disabled", brand=brand_row.slug)
        # A flag that is off is the shipped state (NTS_103 шаг 1), so it is a
        # normal outcome and gets a terminal row, exactly as the v2 gate does:
        # ``cancelled``, tagged ``run_type='intake'``, with the reason where
        # the operator reads it. Before this, the daily unit exited 1 with a
        # traceback and systemd logged Failed every morning — which trains the
        # operator to ignore the one channel that has to stay meaningful
        # (NTS_106). The run row also answers "did the timer fire at all?",
        # which a missing row cannot.
        cancelled_run_id = client.record_run_start(
            source_ids=[], triggered_by=triggered_by, run_type="intake"
        )
        client.record_run_finish(
            cancelled_run_id,
            status="cancelled",
            stats={
                **IntakeStats().as_dict(),
                "cancelled_reason": "intake_enabled is off",
            },
            log_excerpt=(
                f"intake_enabled is OFF for brand {brand_row.slug!r} "
                "(NTS_103 шаг 1). No source was fetched and no item was "
                "judged. Switch it on in Settings to start the shadow week, "
                "or run with --force for a one-off."
            ),
        )
        raise IntakeDisabled(
            f"intake_enabled is off for brand {brand_row.slug!r} — "
            "switch it on in Settings, or pass force=True for a one-off run"
        )

    rules = PrefilterRules.from_config(config)
    dedup_config = CandidateDedupConfig.from_config(config)
    guard_model = getattr(config, "guard_model", DEFAULT_GUARD_MODEL)
    ttl_config = getattr(config, "candidate_ttl_days", None)
    brand_timezone = getattr(config, "brand_timezone", None)
    caps = {
        "document": int(getattr(config, "portfolio_daily_cap_document", 2)),
        "news": int(getattr(config, "portfolio_daily_cap_news", 1)),
    }

    template, template_source = resolve_guard_template(brand_id_fk)
    taxonomy = load_brand_taxonomy(brand_id_fk)
    services_block = render_services(taxonomy)
    tiers_block = render_jurisdiction_tiers(
        getattr(config, "jurisdiction_tiers", None)
    )
    allowed_service_keys = tuple(row["key"] for row in taxonomy)
    recent_titles = recent_accepted_titles(
        brand_id_fk=brand_id_fk, limit=RECENT_ACCEPTED_LIMIT
    )

    sources = client.get_active_sources()
    run_id = client.record_run_start(
        source_ids=[s.id for s in sources if s.id is not None],
        triggered_by=triggered_by,
        run_type="intake",
    )

    log.info(
        "intake.start",
        brand=brand_row.slug,
        run_id=run_id,
        sources=len(sources),
        rubric_source=template_source,
        services=len(taxonomy),
        guard_model=guard_model,
    )

    stats = IntakeStats()
    seen: list[_SeenItem] = []
    log_lines: list[str] = []
    now = datetime.now(tz=UTC)

    with cost_context(CostContext(brand_id_fk=brand_id_fk, run_id=run_id)):
        for source_record in sources:
            input_kind = input_kind_for(
                getattr(source_record, "source_role", "news")
            )
            try:
                items = await _fetch_items(source_record, limit)
            # One dead feed must not cost the day's whole funnel.
            except Exception as exc:
                stats.source_errors += 1
                log.warning(
                    "intake.source_failed",
                    source=source_record.name,
                    err=f"{type(exc).__name__}: {exc}",
                )
                if source_record.id is not None:
                    client.record_source_health(
                        source_id=source_record.id,
                        brand_id_fk=brand_id_fk,
                        success=False,
                        articles_count=0,
                        error_msg=f"{type(exc).__name__}: {exc}",
                    )
                log_lines.append(f"source {source_record.name}: FAILED {exc!r}")
                continue

            if source_record.id is not None:
                # NTS_106 §1 counts "0 элементов" as a failure alongside a
                # timeout. A feed that answers 200 with an error document —
                # taxathand.com does exactly that — parses to zero entries and
                # would otherwise show as healthy forever.
                client.record_source_health(
                    source_id=source_record.id,
                    brand_id_fk=brand_id_fk,
                    success=bool(items),
                    articles_count=len(items),
                    error_msg=None if items else "feed returned 0 items",
                )
            stats.bump(input_kind, "fetched", len(items))

            for item in items:
                # NTS_106 §3 — the guard completion and the embedding are paid
                # for while deciding *whether* this candidate should exist, so
                # neither call can name it. Collect the rows, then charge them
                # to the row that came out. Without this the per-candidate
                # ceiling is not merely unenforced, it is not computable.
                with collect_cost_rows() as cost_rows:
                    candidate_id = await _process_item(
                        item=item,
                        input_kind=input_kind,
                        source_record=source_record,
                        brand_id_fk=brand_id_fk,
                        stats=stats,
                        seen=seen,
                        rules=rules,
                        dedup_config=dedup_config,
                        caps=caps,
                        template=template,
                        services_block=services_block,
                        tiers_block=tiers_block,
                        allowed_service_keys=allowed_service_keys,
                        recent_titles=recent_titles,
                        guard_model=guard_model,
                        ttl_config=ttl_config,
                        brand_timezone=brand_timezone,
                        now=now,
                        embed=embed,
                    )
                if candidate_id is not None:
                    attach_candidate(cost_rows, candidate_id)

            log_lines.append(
                f"source {source_record.name} [{input_kind}]: "
                f"fetched={len(items)}"
            )

    if run_id is not None:
        client.record_run_finish(
            run_id,
            status="success" if stats.source_errors == 0 else "failed",
            stats=stats.as_dict(),
            log_excerpt="\n".join(log_lines)[-4000:],
        )

    log.info(
        "intake.done",
        brand=brand_row.slug,
        run_id=run_id,
        **{stage: stats.total(stage) for stage in _FUNNEL_STAGES},
        prefilter_drop_rate=round(stats.prefilter_drop_rate, 3),
        guard_error_rate=round(stats.guard_error_rate, 3),
    )
    return stats


async def _process_item(
    *,
    item: RawItem,
    input_kind: str,
    source_record: Any,
    brand_id_fk: int,
    stats: IntakeStats,
    seen: list[_SeenItem],
    rules: PrefilterRules,
    dedup_config: CandidateDedupConfig,
    caps: dict[str, int],
    template: str,
    services_block: str,
    tiers_block: str,
    allowed_service_keys: tuple[str, ...],
    recent_titles: tuple[str, ...],
    guard_model: str,
    ttl_config: Any,
    brand_timezone: str | None,
    now: datetime,
    embed,
) -> int | None:
    """Judge and store one feed item. Returns the candidate id it wrote.

    ``None`` means no row was written — the item was dropped by dedup or the
    prefilter, or the guard deferred. The caller uses the id to charge this
    item's cost rows to the candidate (NTS_106 §3).
    """
    """One feed item through the funnel. Never raises — one bad item is one
    item, and an exception here would take the rest of the feed with it."""
    url = str(item.url) if item.url else None
    topic_id = _topic_id_for(url, item.title)
    # RawItem carries no language, and no detector exists before S5 — the
    # source's ``doc_language`` is the only signal, and "en" is the honest
    # default for a feed nobody has classified yet.
    source_language = getattr(source_record, "doc_language", None) or "en"

    # --- dedup (NTS_098 §3). In-run L1 first: free, and it stops two feeds
    #     carrying the same wire story from each buying a guard call.
    title_norm = normalize_title(item.title)
    for prior in seen:
        if jaccard(title_norm, prior.title_norm) > _JACCARD_DUP:
            stats.dedup_windows["run_title"] = (
                stats.dedup_windows.get("run_title", 0) + 1
            )
            return None

    try:
        embedding = np.asarray(
            await embed(f"{item.title}\n{(item.summary or '')[:500]}"),
            dtype=np.float32,
        )
    except Exception as exc:
        # Fail OPEN would mean guarding without dedup, which risks a duplicate
        # article. Fail CLOSED would drop the item silently. Counted and
        # skipped: the next intake sees the item again, and the counter says
        # why this one did not.
        stats.embed_failures += 1
        log.warning("intake.embed_failed", topic=topic_id, err=str(exc))
        return None

    pre = check_pre_guard(
        brand_id_fk=brand_id_fk,
        embedding=embedding,
        input_kind=input_kind,
        primary_doc_url=url if input_kind == "document" else None,
        config=dedup_config,
        now=now,
    )
    if pre.action != "guard":
        stats.dedup_windows[pre.window or pre.action] = (
            stats.dedup_windows.get(pre.window or pre.action, 0) + 1
        )
        log.info(
            "intake.dedup_skip",
            topic=topic_id,
            action=pre.action,
            window=pre.window,
            matched=pre.matched_candidate_id,
            sim=round(pre.similarity, 3),
        )
        return None
    stats.bump(input_kind, "after_dedup")
    seen.append(_SeenItem(topic_id, title_norm, embedding))

    # --- prefilter (NTS_099 §1)
    decision = prefilter_item(
        title=item.title,
        summary=item.summary,
        published_at=item.published_at,
        source_role=getattr(source_record, "source_role", "news"),
        source_language=source_language,
        rules=rules,
        now=now,
    )
    if not decision.keep:
        reason = decision.reason or "unknown"
        stats.prefilter_drops[reason] = stats.prefilter_drops.get(reason, 0) + 1
        log.info(
            "intake.prefilter_drop",
            topic=topic_id,
            reason=reason,
            detail=decision.detail,
        )
        return None
    stats.bump(input_kind, "after_prefilter")

    # --- guard (NTS_099 §2-§3)
    try:
        verdict = await judge_item(
            input_kind=input_kind,
            title=item.title,
            summary=item.summary,
            source_name=source_record.name,
            source_class=getattr(source_record, "source_class", "news"),
            source_language=source_language,
            published_at=item.published_at,
            recent_accepted_titles=recent_titles,
            template=template,
            services_block=services_block,
            tiers_block=tiers_block,
            allowed_service_keys=allowed_service_keys,
            model=guard_model,
        )
    except GuardSchemaError as exc:
        # NTS_099 §3: no candidate row, counted in the summary. Not coerced
        # into a reject — a reject is an editorial statement, and this is not.
        stats.bump(input_kind, "guard_errors")
        stats.reason_codes["guard_error"] = (
            stats.reason_codes.get("guard_error", 0) + 1
        )
        log.warning("intake.guard_error", topic=topic_id, err=str(exc))
        return None
    except GuardDeferred as exc:
        # NTS_106 §1: not judged, replayed by the next intake.
        stats.bump(input_kind, "deferred")
        log.warning("intake.guard_deferred", topic=topic_id, err=str(exc))
        return None

    stats.bump(input_kind, "guarded")
    stats.reason_codes[verdict.reason_code] = (
        stats.reason_codes.get(verdict.reason_code, 0) + 1
    )

    supersedes_id: int | None = None
    if verdict.accepted:
        post = check_post_guard(
            brand_id_fk=brand_id_fk,
            embedding=embedding,
            event_stage=verdict.event_stage,
            config=dedup_config,
            now=now,
        )
        if post.action == "duplicate":
            stats.dedup_windows[f"{post.window}_same_stage"] = (
                stats.dedup_windows.get(f"{post.window}_same_stage", 0) + 1
            )
            log.info(
                "intake.duplicate_stage",
                topic=topic_id,
                window=post.window,
                matched=post.matched_candidate_id,
                sim=round(post.similarity, 3),
            )
            return None
        if post.action == "supersede":
            supersedes_id = post.matched_candidate_id

    # --- daily cap (NTS_099 §5). Stored, not discarded.
    cap_overflow = False
    reason_code = verdict.reason_code
    reason = verdict.reason
    stored_verdict = verdict.verdict
    if verdict.accepted:
        cap = caps.get(input_kind, 0)
        already = count_accepted_today(
            brand_id_fk=brand_id_fk,
            input_kind=input_kind,
            now=now,
            timezone_name=brand_timezone,
        )
        if cap and already >= cap:
            cap_overflow = True
            stored_verdict = "reject"
            reason_code = "daily_cap"
            reason = (
                f"daily cap for {input_kind} reached ({already}/{cap}) — "
                f"promotable today: {verdict.reason}"
            )[:200]
            stats.cap_overflow += 1
            stats.reason_codes["daily_cap"] = (
                stats.reason_codes.get("daily_cap", 0) + 1
            )
            supersedes_id = None

    _persist_embedding(
        topic_id=topic_id,
        brand_id_fk=brand_id_fk,
        title=item.title,
        embedding=embedding,
        model="text-embedding-3-small",
    )

    candidate_id = create_candidate(
        CandidateInput(
            brand_id_fk=brand_id_fk,
            input_kind=input_kind,
            source_id_fk=source_record.id,
            source_title=item.title,
            source_summary=item.summary or None,
            source_url=url,
            source_published_at=item.published_at,
            source_language=source_language,
            source_name=source_record.name,
            source_class=getattr(source_record, "source_class", "news"),
            topic_embedding_ref=topic_id,
            verdict=stored_verdict,
            reason_code=reason_code,
            reason=reason,
            confidence=verdict.confidence,
            service_category=verdict.service_category,
            jurisdictions=verdict.jurisdictions,
            event_stage=verdict.event_stage,
            depth_prior=verdict.depth_prior,
            primary_doc_hint=verdict.primary_doc_hint,
            primary_doc_url=url if input_kind == "document" else None,
            doc_language_expected=verdict.doc_language_expected,
            cap_overflow=cap_overflow,
            supersedes_id=supersedes_id,
        ),
        ttl_config=ttl_config,
        now=now,
    )

    if supersedes_id is not None and mark_superseded(supersedes_id):
        stats.superseded += 1

    if stored_verdict == "accept":
        stats.bump(input_kind, "accepted")
    else:
        stats.bump(input_kind, "rejected")

    log.info(
        "intake.candidate",
        candidate_id=candidate_id,
        topic=topic_id,
        input_kind=input_kind,
        verdict=stored_verdict,
        reason_code=reason_code,
        service=verdict.service_category,
        stage=verdict.event_stage,
        depth_prior=verdict.depth_prior,
        cap_overflow=cap_overflow,
        supersedes=supersedes_id,
    )
    return candidate_id


# --- CLI ------------------------------------------------------------------


@app.command()
def main(
    brand_slug: str = typer.Option("icon", "--brand-slug"),
    brand_id: int | None = typer.Option(None, "--brand-id"),
    limit: int = typer.Option(INTAKE_ITEMS_PER_SOURCE, "--limit"),
    force: bool = typer.Option(
        False, "--force", help="run even when intake_enabled is off"
    ),
    triggered_by: str = typer.Option("cron", "--triggered-by"),
) -> None:
    """Run contour 1 (intake + guard) once. No generation, ever."""
    import asyncio

    try:
        stats = asyncio.run(
            run_intake(
                brand_slug=brand_slug,
                brand_id=brand_id,
                limit=limit,
                force=force,
                triggered_by=triggered_by,
            )
        )
    # The refusal stays an exception in the API — a caller must not read
    # "nothing ran" as "nothing matched" — but here it is the expected daily
    # outcome while the flag is off, and systemd reads the exit code. The run
    # row was already written as ``cancelled`` by ``run_intake``.
    except IntakeDisabled as exc:
        typer.echo(str(exc))
        return
    typer.echo(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    app()
