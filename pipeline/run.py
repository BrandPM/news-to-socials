"""End-to-end pipeline orchestrator — Sanity edition (ADR-018).

This is the script that turns the skeleton into a working product. It wires
together the modules under ``pipeline/`` into one runnable pipeline:

    source.fetch
        → topic_picker.score (drop low-relevance, assign category)
        → dedup.is_duplicate (drop near-duplicates, two-tier: local + Sanity)
        → comment_writer.write (gpt-4o-mini draft → gpt-4o polish)
        → image.generate (Flux Pro master)
        → upload to Sanity assets
        → SanityPublisher.publish_draft (creates draft in Sanity Studio)

Andriy approves through /studio (drafts → publish). The published document
appears at www.iconfinance.io/:lang/insights/:slug.

Usage:
    python -m scripts.run_pipeline                              # all active sources from admin.db
    python -m scripts.run_pipeline --source-id x --source-url y # override one source
    python -m scripts.run_pipeline --dry-run                    # no Sanity write, no image gen

Sources / scoring threshold / active prompts / voice profile are read from
``admin.db`` via :class:`pipeline.admin.config_client.AdminConfigClient`.
When admin.db is missing or empty we fall back to the in-repo hardcoded
seeds — see Admin-UI-Specific Invariant B in IT_PROJ_NTS_014.

For Wave 1 we only publish to Sanity (blog channel). Wave 2 adds Meta;
Wave 3 adds Telegram.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import openai
import typer

from pipeline.common.config import get_settings
from pipeline.common.display_date import compute_display_date
from pipeline.common.logging import configure_logging, get_logger
from pipeline.common.models import (
    Channel,
    Draft,
    Language,
    RawItem,
    Topic,
)
from pipeline.generator.comment_writer import (
    CommentWriter,
    parse_image_style_prompts,
)
from pipeline.generator.image import (
    DEFAULT_ICON_IMAGE_STYLES,
    BrandVisual,
    ImageGenerator,
)
from pipeline.generator.image_prompt import build_scene_prompt
from pipeline.generator.image_resizer import fetch_master, resize_for_channel
from pipeline.publisher.sanity import (
    SanityCategoryMapping,
    SanityPostInput,
    SanityPublisher,
)
from pipeline.selector.dedup import extract_entities
from pipeline.admin.judge import score_draft
from pipeline.selector.dedup_service import DedupEngine, cleanup_old_embeddings
from pipeline.selector.topic_picker import BrandContext, TopicPicker
from pipeline.sources.base import Source

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)


# --------------------------------------------------------------------------
# Brand config — for Wave 1 (Icon only) we hard-code from .env / constants.
# In Stage 5 we'll fetch from a Sanity `brand` collection.
# --------------------------------------------------------------------------


@dataclass
class BrandConfig:
    """Everything we need to know about a brand for one pipeline run.

    For Wave 1 Icon: hard-coded sensible defaults. For Stage 5 multibrand,
    this will be loaded from a Sanity `brand` document.
    """

    slug: str
    name: str
    voice_profile_yaml: str
    visual: BrandVisual
    context: BrandContext
    categories: SanityCategoryMapping
    # NTS_067: brand PK, so CommentWriter can source the ACTIVE prompt row
    # from the prompts table for this brand. None → use in-code constants.
    id_fk: int | None = None


def icon_brand_config() -> BrandConfig:
    """Minimal Icon config for Wave 1 — enough to start generating.

    Andriy refines voice_profile_yaml in Sanity at Stage 2 Day 5.
    """
    return BrandConfig(
        slug="icon",
        name="Icon",
        voice_profile_yaml=(
            "# Icon Finance voice profile (Wave 1, hardened IT_PROJ_NTS_013)\n"
            "mission: Wealth-management partner for international families and entrepreneurs.\n"
            "audience: HNWI, family office principals, founders post-exit.\n"
            "tone:\n"
            "  formality: high-but-warm\n"
            "  first_person: brand_name # 'Icon believes ...' not 'we'\n"
            "  emoji_allowed: false\n"
            "voice_principles:\n"
            "  - Lead with a specific consequence, not a general observation.\n"
            "  - Name the mechanism: who is repriced, who is exposed, who absorbs the cost.\n"
            "  - One concrete number or named entity per paragraph, not vague intensifiers.\n"
            "  - Address the reader as someone already inside the conversation, not someone being briefed.\n"
            "  - End on what changes for the reader's next decision, not a restatement.\n"
            "  - Short sentences. Vary length. Do not chain three clauses with em-dashes.\n"
            "topics_relevant:\n"
            "  - cross-border tax structuring\n"
            "  - family office operations\n"
            "  - international wealth transfer\n"
            "  - investment-grade product launches\n"
            "  - M&A relevant to private capital\n"
            "topics_banned:\n"
            "  - crypto speculation\n"
            "  - retail trading\n"
            "  - day-trading systems\n"
            "banned_phrases:\n"
            "  - in today's fast-paced\n"
            "  - ever-evolving\n"
            "  - ever-changing\n"
            "  - navigate the landscape\n"
            "  - navigate the complexities\n"
            "  - in the realm of\n"
            "  - in the world of\n"
            "  - harness the power of\n"
            "  - unlock the potential\n"
            "  - at the forefront of\n"
            "  - delve into\n"
            "  - moreover\n"
            "  - furthermore\n"
            "  - it's important to note\n"
            "  - it is important to note\n"
            "  - in conclusion\n"
            "  - in summary\n"
            "  - strategic perspectives on\n"
            "  - enhancing wealth management\n"
            "  - robust framework\n"
            "  - comprehensive approach\n"
            "  - tailored solutions\n"
            "  - bespoke solutions\n"
            "  - cutting-edge\n"
            "  - in an increasingly\n"
            "  - paradigm shift\n"
            # NTS_070 — manager-feedback additions (EN).
            "  - growing uncertainty\n"
            "  - rising uncertainty\n"
            "  - significant impact\n"
            "  - immediate action required\n"
            "  - potential conflict\n"
            "  - each case is different\n"
            "  - it is important to\n"
            "  - plays a crucial role\n"
            "  - when it comes to\n"
            "style_examples:\n"
            "  good:\n"
            "    - \"The proposal moves the discussion, not the timeline.\"\n"
            "    - \"Trust planning rarely fails on tax. It fails on family.\"\n"
            "    - \"A 50bp move in base rates is not the story. The story is who can refinance and who cannot.\"\n"
            "    - \"For a family with operating assets in three jurisdictions, the question is not whether to restructure, but when the cost of not restructuring exceeds the cost of doing it.\"\n"
            "    - \"India's new credit-fund regime will reprice mezzanine paper before it reprices senior. Allocators who set their yield assumptions last quarter should revisit them this one.\"\n"
            "  bad:\n"
            "    - \"In today's fast-paced world of wealth management, it is important to note that the landscape is ever-evolving.\"\n"
            "    - \"Icon believes in harnessing the power of strategic perspectives to navigate the complexities of cross-border structuring.\"\n"
            "    - \"This article will delve into the comprehensive framework that enables families to unlock the potential of their wealth.\"\n"
            "    - \"Moreover, the proposal represents a paradigm shift. Furthermore, it is at the forefront of innovation. In conclusion, allocators should take note.\"\n"
            "    - \"Growing uncertainty creates significant challenges that require immediate action.\"\n"
        ),
        # NTS_075 L1/L3: styles now live in the brand voice profile
        # (``image.style_prompts``); this hardcoded config is only a fallback
        # for brands with no profile yet, so it carries the rich default set.
        visual=BrandVisual(
            brand_id="icon",
            image_style_prompts=list(DEFAULT_ICON_IMAGE_STYLES),
        ),
        context=BrandContext(
            brand_id="icon",
            name="Icon",
            topics_relevant=[
                "cross-border tax structuring",
                "family office operations",
                "international wealth transfer",
                "M&A for private capital",
            ],
            topics_banned=[
                "crypto speculation",
                "retail trading",
                "day-trading systems",
            ],
        ),
        categories=SanityCategoryMapping.icon_default(),
    )


def _resolve_brand_image_styles(voice_profile_yaml: str | None) -> list[str]:
    """Cover-image styles for a brand: from its voice profile, else default.

    NTS_075 L3 — ``image.style_prompts`` is read from the brand's
    ``voice_profile_yaml`` (same place voice/banned_phrases live and are
    edited), with a back-compat fall back to the built-in
    :data:`DEFAULT_ICON_IMAGE_STYLES` when the profile carries none, so an
    empty/legacy profile never leaves a brand with zero styles.
    """
    styles = parse_image_style_prompts(voice_profile_yaml or "")
    return styles or list(DEFAULT_ICON_IMAGE_STYLES)


# --------------------------------------------------------------------------
# Embedding helper — used by dedup
# --------------------------------------------------------------------------


# USD per 1M tokens for the dedup embedding model (OpenAI, 2026-07 pricing).
_EMBED_USD_PER_1M = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13}


async def _embed(text: str, *, model: str = "text-embedding-3-small") -> np.ndarray:
    """Get an embedding from OpenAI. Cheap, ~$0.00002/topic.

    NTS_090 (C1): every paid call records a ``cost_records`` row. Cost is
    computed from the API's reported token usage × the model's per-1M price.
    Recording is best-effort (``record_cost`` no-ops without a cost context).
    """
    from pipeline.admin.cost_recorder import record_cost  # noqa: PLC0415

    settings = get_settings()
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.embeddings.create(model=model, input=text)
    tokens = int(getattr(resp.usage, "total_tokens", 0) or 0)
    record_cost(
        provider="openai",
        operation="embedding",
        model=model,
        tokens_in=tokens,
        cost_usd=tokens / 1_000_000 * _EMBED_USD_PER_1M.get(model, 0.02),
    )
    return np.array(resp.data[0].embedding, dtype=np.float32)


# --------------------------------------------------------------------------
# Source loading — Wave 1 loads from a simple in-memory dict.
# When Stage 1 Day 3 produces sources-v0.yaml, this reads that file.
# --------------------------------------------------------------------------


def load_source_by_id(source_id: str, source_url: str | None = None) -> Source:
    """Wave 1 stub: treat source_id as the URL for ad-hoc runs.

    Later we'll load from sources-v0.yaml.
    """
    from pipeline.sources.rss import RssSource

    # For now, force RSS; the dispatch table is in REGISTRY for real Stage 6 use.
    return RssSource(
        source_id=source_id,
        name=source_id,
        url=source_url or source_id,
    )


# --------------------------------------------------------------------------
# Pipeline stages
# --------------------------------------------------------------------------


async def score_relevant_topics(
    items: list[RawItem],
    brand: BrandConfig,
    min_score: int = 7,
    limit_pool: int = 20,
) -> list[tuple[RawItem, int]]:
    """Score items via gpt-4o-mini, keep those at or above ``min_score``."""
    picker = TopicPicker()
    pool = items[:limit_pool]

    sem = asyncio.Semaphore(5)

    async def _one(item: RawItem) -> tuple[RawItem, int] | None:
        async with sem:
            try:
                score, _reason = await picker.score(item, brand.context)
            except Exception as exc:  # noqa: BLE001
                log.warning("score.failed", url=str(item.url), err=str(exc))
                return None
            if score < min_score:
                return None
            return item, score

    results = await asyncio.gather(*(_one(it) for it in pool))
    scored = [r for r in results if r is not None]
    scored.sort(key=lambda r: r[1], reverse=True)
    log.info(
        "score.done",
        pool=len(pool),
        passed=len(scored),
        top_score=scored[0][1] if scored else None,
    )
    return scored


async def assign_category(
    item: RawItem, brand: BrandConfig
) -> str:
    """Ask gpt-4o-mini which of brand.categories this item belongs to."""
    settings = get_settings()
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    cat_list = "\n".join(
        f"- {v}: {brand.categories.titles[v]}" for v in brand.categories.values
    )
    prompt = (
        f"Classify this news item into ONE of the following categories for {brand.name}:\n\n"
        f"{cat_list}\n\n"
        f"News:\n  Title: {item.title}\n  Summary: {(item.summary or '')[:600]}\n\n"
        'Return ONLY a JSON object: {"category": "<one of: '
        f"{', '.join(brand.categories.values)}"
        '>"}'
    )
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=40,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        cat = json.loads(raw).get("category", "special")
    except json.JSONDecodeError:
        cat = "special"
    if cat not in brand.categories.values:
        log.warning("category.invalid", got=cat, fallback="special")
        cat = "special"
    return cat


async def generate_image_for_topic(
    topic: Topic,
    brand: BrandConfig,
    sanity_publisher: SanityPublisher,
) -> str | None:
    """Generate the master image for ``topic`` and upload to Sanity assets.

    S6 cost fix: image generation is language-independent, so we run it
    ONCE per topic and reuse the resulting asset id across every language
    draft for that topic. Previously this was called per (topic, language)
    pair, producing 4× duplicate Replicate calls and 4× duplicate Sanity
    asset uploads. Now: ``topic.id`` (URL hash) is the natural cache key
    shared across languages.

    Returns the Sanity asset ``_id`` on success, ``None`` on dry-run or
    image failure. Image failures are isolated — the caller still produces
    the language drafts, just without a cover (operator can add one
    manually in Studio).
    """
    settings = get_settings()
    if settings.dry_run:
        log.info("image.dry_run", topic=topic.id)
        return None

    try:
        gen = ImageGenerator()
        # NTS_075 L2: derive a topic-specific visual scene ONCE per topic
        # (this helper is the single per-topic seam, NTS_069) from the
        # EN-canonical headline + summary. Falls back to the headline inside
        # ImageGenerator if the LLM is off/fails. Runs inside the run's cost
        # context, so the image_prompt cost is recorded against the run.
        scene = await build_scene_prompt(
            topic.raw.title, topic.raw.summary, brand_id_fk=brand.id_fk
        )
        master_url = await gen.generate(topic, brand.visual, scene=scene)
        master_bytes = await fetch_master(master_url)
        # Blog channel cover (1792x1008) — used on /insights.
        resized = resize_for_channel(master_bytes, Channel.blog)
        return await sanity_publisher.upload_cover_image(
            resized, filename=f"{brand.slug}-{topic.id}.png"
        )
    except Exception:  # noqa: BLE001
        # log.exception captures the traceback so silent image failures
        # surface in /var/log/news-to-socials/run.log (IT_PROJ_NTS_013 Defect 1).
        log.exception("image.failed", topic=topic.id)
        return None  # Continue without image; Andriy can add manually.


async def generate_draft_for_language(
    topic: Topic,
    brand: BrandConfig,
    language: Language,
) -> Draft:
    """Generate the CANONICAL draft for ``language`` natively from the topic.

    Post-NTS_065 this is used for the canonical (English) draft only — see
    :func:`translate_draft_for_language` for the non-EN path. Kept on this
    name + signature because it's the seam the fanout tests monkeypatch.
    No image work — that's hoisted above this call in the orchestrator
    (see :func:`generate_image_for_topic`)."""
    writer = CommentWriter(brand_id_fk=brand.id_fk)
    return await writer.write(topic, brand.voice_profile_yaml, language)


async def translate_draft_for_language(
    topic: Topic,  # kept for a uniform seam with generate_draft_for_language
    brand: BrandConfig,
    language: Language,
    en_draft: Draft,
) -> Draft:
    """Produce the non-EN draft as a faithful TRANSLATION of ``en_draft``.

    NTS_065 rework: non-EN languages are no longer generated natively from
    the topic (which drifted in structure/length and invented facts). They
    are an exact translation of the canonical English draft — same H2 set,
    same facts/numbers, comparable length — with the target language's
    voice profile applied only as phrasing localisation. Separate seam from
    :func:`generate_draft_for_language` so tests can stub each independently."""
    writer = CommentWriter(brand_id_fk=brand.id_fk)
    return await writer.translate(en_draft, language, brand.voice_profile_yaml)


def _order_languages_en_first(languages: list[Language]) -> list[Language]:
    """Return ``languages`` with English first so the canonical EN draft is
    produced before any translation that depends on it. Order of the
    remaining languages is preserved."""
    rest: list[Language] = [lang for lang in languages if lang != Language.en]
    if Language.en in languages:
        return [Language.en, *rest]
    return rest


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


async def _build_topics_for_source(
    *,
    source_record,
    brand: BrandConfig,
    brand_id_fk: int,
    client,
    limit: int,
    min_score: int,
) -> tuple[list[Topic], int]:
    """Fetch + score + embed a single RSS source.

    Returns ``(topics, fetched_count)``. The Topic objects carry embeddings
    + entities but have NOT been dedup-filtered — that's the caller's
    job because the dedup set is per-language. ``source.fetch()`` runs
    ONCE per source now (S6 fix); previously the per-language outer loop
    re-fetched the same RSS 4×.
    """
    from pipeline.sources.rss import RssSource  # noqa: PLC0415

    if source_record.source_type != "rss":
        log.warning("source.unsupported_type", type=source_record.source_type)
        # Must match the declared (topics, fetched_count) shape — the caller
        # unpacks the tuple, so a bare ``[]`` would crash on a non-rss source
        # (NTS_076 audit fix).
        return [], 0

    source = RssSource(
        source_id=(
            str(source_record.id)
            if source_record.id is not None
            else source_record.name
        ),
        name=source_record.name,
        url=source_record.url,
    )

    try:
        raw_items = list(await source.fetch())
        log.info("source.fetched", count=len(raw_items), source=source.name)
        if source_record.id is not None:
            client.record_source_health(
                source_id=source_record.id,
                brand_id_fk=brand_id_fk,
                success=True,
                articles_count=len(raw_items),
            )
    except Exception as exc:  # noqa: BLE001 — record then re-raise
        if source_record.id is not None:
            client.record_source_health(
                source_id=source_record.id,
                brand_id_fk=brand_id_fk,
                success=False,
                articles_count=0,
                error_msg=f"{type(exc).__name__}: {exc}",
            )
        raise
    fetched_count = len(raw_items)
    if not raw_items:
        log.warning("source.empty", source=source.name)
        return [], 0

    scored = await score_relevant_topics(
        raw_items, brand, min_score=min_score, limit_pool=limit * 4
    )
    if not scored:
        return [], fetched_count

    topics: list[Topic] = []
    for item, score in scored:
        # NTS_090 — embed the SOURCE EN text (title + first 500 chars of
        # summary), never the generated article: dedup must happen before
        # generation spend.
        text = f"{item.title}\n{(item.summary or '')[:500]}"
        url_hash = hashlib.sha1(str(item.url).encode("utf-8")).hexdigest()
        topic_id = url_hash[:16]
        try:
            embedding = await _embed(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("embed.failed", url=str(item.url), err=str(exc))
            continue
        topics.append(
            Topic(
                id=topic_id,
                brand_id=brand.slug,
                raw=item,
                relevance_score=float(score),
                embedding=embedding.tolist(),
                entities=sorted(extract_entities(text)),
            )
        )
    return topics, fetched_count


def _apply_dedup(
    topics: list[Topic],
    *,
    brand_id_fk: int,
    source_id: int | None,
    run_id: int | None,
    client,
    dedup_enabled: bool,
    dedup_threshold: float,
    dedup_window_days: int,
) -> tuple[list[Topic], int]:
    """Two-level dedup over a source's scored topics (NTS_090). FAILS OPEN.

    Returns ``(kept_topics_in_original_order, skipped_count)``. Skipped topics
    are recorded as ``filtered_dup`` rows (so the admin Last-Run view shows the
    reason) and logged to ``dedup_log``. Canonical selection uses first-seen
    (window) + longest-summary tiebreak within the batch. Any error → all
    topics kept, 0 skipped (dedup must never block a run).
    """
    if not dedup_enabled or len(topics) < 1:
        return topics, 0
    try:
        engine = DedupEngine(
            brand_id_fk=brand_id_fk,
            threshold=dedup_threshold,
            window_days=dedup_window_days,
            run_id=run_id,
        )
        # Longest summary first → the richest input wins an intra-batch tie
        # (becomes canonical); later shorter duplicates match + skip.
        ordered = sorted(
            topics, key=lambda t: len(t.raw.summary or ""), reverse=True
        )
        survivors: set[str] = set()
        skipped = 0
        for t in ordered:
            emb = np.asarray(t.embedding, dtype=np.float32)
            decision = engine.check(t.id, t.raw.title, emb)
            if decision.action == "skipped":
                skipped += 1
                engine.record(t.id, decision)
                client.record_topic_result(
                    run_id=run_id,
                    topic_id=t.id,
                    source_id=source_id,
                    title=t.raw.title,
                    url=str(t.raw.url),
                    score=int(t.relevance_score),
                    status="filtered_dup",
                    filter_reason=(
                        f"duplicate_of:{decision.matched_topic_id} "
                        f"sim={decision.similarity:.3f} level={decision.level}"
                    ),
                    language="en",
                )
                log.info(
                    "dedup.skipped",
                    topic=t.id,
                    matched=decision.matched_topic_id,
                    sim=round(decision.similarity, 3),
                    level=decision.level,
                )
                continue
            if decision.action == "yellow":
                engine.record(t.id, decision)
                log.info(
                    "dedup.yellow",
                    topic=t.id,
                    matched=decision.matched_topic_id,
                    sim=round(decision.similarity, 3),
                )
            engine.remember(t.id, t.raw.title, emb)
            survivors.add(t.id)
        # Preserve original (score) order among survivors.
        kept = [t for t in topics if t.id in survivors]
        return kept, skipped
    except Exception as exc:  # noqa: BLE001 — HARD STOP: dedup fails OPEN
        log.warning("dedup.pass_failed_fail_open", err=str(exc))
        return topics, 0


async def _process_source(
    *,
    source_record,  # config_client.SourceRecord
    brand: BrandConfig,
    brand_id_fk: int,
    languages: list[Language],
    limit: int,
    dry_run: bool,
    sanity_publisher,
    client,  # AdminConfigClient
    run_id: int | None,
    min_score: int,
    dedup_enabled: bool = True,
    dedup_threshold: float = 0.85,
    dedup_window_days: int = 7,
    eval_enabled: bool = True,
    eval_threshold: float = 7.0,
    images_on_demand: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int], set[str]]:
    """Process ONE source for ALL ``languages`` in a single pass.

    S6 multilingual + cost fix (IT_PROJ_NTS_051):

    * fetch + score happen ONCE per source (was 4× — outer loop was on
      languages, so each language re-fetched the same RSS).
    * Image generation happens ONCE per topic (was 4× — same article
      yielded a fresh Replicate image per language). The asset id is
      shared across every language draft for that topic.
    * Per-language dedup still runs per topic+language, against the
      brand-wide ``Deduper`` carried across all sources in the run.
    * Failure isolation: a per-(topic, language) error is logged into
      ``stats['errors']`` but does NOT abort siblings.

    Returns ``(results, stats, languages_attempted)``. The third value
    lets the orchestrator know which languages this source contributed
    to so ``runs.languages_completed`` can be aggregated centrally.
    """
    stats = {
        "fetched": 0,
        "scored": 0,
        "drafted": 0,
        "errors": 0,
        "deduped": 0,
        # NTS_094 — topics whose cover was deliberately NOT generated because
        # the brand runs images on demand. Distinct from an image failure.
        "images_skipped": 0,
    }
    topics, fetched_count = await _build_topics_for_source(
        source_record=source_record,
        brand=brand,
        brand_id_fk=brand_id_fk,
        client=client,
        limit=limit,
        min_score=min_score,
    )
    stats["fetched"] = fetched_count
    stats["scored"] = len(topics)
    if not topics:
        return [], stats, set()

    # ---- DEDUP (NTS_090): drop near-duplicate news BEFORE any generation
    # spend. Runs over the full scored pool, then the cap is applied to the
    # survivors so image-gen budget tracks unique topics. Fails OPEN.
    topics, deduped_count = _apply_dedup(
        topics,
        brand_id_fk=brand_id_fk,
        source_id=source_record.id,
        run_id=run_id,
        client=client,
        dedup_enabled=dedup_enabled,
        dedup_threshold=dedup_threshold,
        dedup_window_days=dedup_window_days,
    )
    stats["deduped"] = deduped_count

    # ``limit`` caps the per-source pre-publish pool (applied AFTER dedup so
    # the cap counts unique topics, not near-duplicates we're about to drop).
    topics = topics[:limit]

    results: list[dict[str, Any]] = []
    # Track which languages got at least one publish attempt so we can
    # tell ``runs.languages_completed`` what the fanout reached for THIS
    # source. The orchestrator unions these across sources.
    languages_attempted: set[str] = set()

    for topic in topics:
        # ---- IMAGE: once per topic (the whole point of this refactor).
        # NTS_094: with ``images_on_demand`` the run generates NOTHING here —
        # no scene prompt, no Flux call, no asset upload. The draft is written
        # with ``coverImage: null`` on purpose and the manager generates the
        # cover for the one draft they pick (publish-guard button, NTS_091).
        # The event gets its OWN name: a deliberate skip and a broken
        # generation must stay distinguishable in the log and in alerting.
        if images_on_demand:
            log.info(
                "image.skipped_on_demand",
                topic=topic.id,
                brand=brand.slug,
            )
            stats["images_skipped"] += 1
            asset_id = None
        else:
            try:
                asset_id = await generate_image_for_topic(
                    topic, brand, sanity_publisher
                )
            except Exception:  # noqa: BLE001
                # generate_image_for_topic already swallows + logs internally,
                # so this is a paranoia net for unexpected explosions.
                log.exception("image.unexpected_failure", topic=topic.id)
                asset_id = None

        # ---- CATEGORY: language-agnostic; once per topic.
        try:
            category = await assign_category(topic.raw, brand)
        except Exception as exc:  # noqa: BLE001
            log.error("category.failed", topic=topic.id, err=str(exc))
            category = "special"

        # ---- DISPLAY DATE: news date, not approval date (NTS_089).
        # Computed once per topic from the source RSS pubDate (with clamps)
        # and shared by every language sibling — one date per topic, mirroring
        # the shared cover image. On approve it becomes the site publishedAt.
        display_date_val, display_date_src = compute_display_date(
            topic.raw.published_at, datetime.now(tz=timezone.utc)
        )
        display_date_iso = display_date_val.isoformat()
        log.info(
            "draft.display_date",
            topic=topic.id,
            display_date=display_date_iso,
            source=display_date_src,
            pub_date=(
                topic.raw.published_at.isoformat()
                if topic.raw.published_at
                else None
            ),
        )

        # ---- DRAFTS: once per (topic, language).
        # NTS_065: English is canonical and generated FIRST; every non-EN
        # language is a faithful translation of that EN draft, not a fresh
        # native generation. ``en_draft`` caches the canonical text for the
        # topic so the translate branches reuse it.
        en_draft: Draft | None = None
        for language in _order_languages_en_first(languages):
            languages_attempted.add(language.value)
            try:
                # Cross-source/-run near-duplicate dedup now runs at topic
                # SELECTION (NTS_090, above) before any generation. Here we
                # keep only the Sanity "already published this topic+language"
                # guard — cheap idempotency against re-publishing.
                if await sanity_publisher.is_topic_already_posted(
                    topic.id, language
                ):
                    log.info(
                        "dedup.sanity_hit",
                        topic_id=topic.id,
                        lang=language.value,
                    )
                    continue

                if language == Language.en:
                    draft = await generate_draft_for_language(
                        topic, brand, Language.en
                    )
                    en_draft = draft
                else:
                    # Non-EN = translation of the canonical EN draft. If EN
                    # wasn't produced yet (e.g. EN was a dedup hit, or this
                    # run only targets a non-EN language), generate the
                    # canonical EN now as the translation source — it is NOT
                    # published, it only feeds the translation.
                    if en_draft is None:
                        en_draft = await generate_draft_for_language(
                            topic, brand, Language.en
                        )
                    draft = await translate_draft_for_language(
                        topic, brand, language, en_draft
                    )

                post = SanityPostInput(
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

                if dry_run:
                    log.info(
                        "dry_run.would_create",
                        title=post.title,
                        category=category,
                        language=language.value,
                    )
                    results.append(
                        {
                            "topic_id": topic.id,
                            "status": "dry_run",
                            "category": category,
                            "title": post.title,
                            "language": language.value,
                        }
                    )
                    client.record_topic_result(
                        run_id=run_id,
                        topic_id=topic.id,
                        source_id=source_record.id,
                        title=post.title,
                        url=str(topic.raw.url),
                        score=int(topic.relevance_score),
                        status="passed",
                        draft_id=None,
                        language=language.value,
                    )
                else:
                    draft_id = await sanity_publisher.publish_draft(post)
                    results.append(
                        {
                            "topic_id": topic.id,
                            "draft_id": draft_id,
                            "category": category,
                            "title": post.title,
                            "language": language.value,
                        }
                    )
                    stats["drafted"] += 1
                    log.info(
                        "topic.published_as_draft",
                        topic=topic.id,
                        draft_id=draft_id,
                        language=language.value,
                    )
                    client.record_topic_result(
                        run_id=run_id,
                        topic_id=topic.id,
                        source_id=source_record.id,
                        title=post.title,
                        url=str(topic.raw.url),
                        score=int(topic.relevance_score),
                        status="passed",
                        draft_id=draft_id,
                        language=language.value,
                    )
                    # ---- LLM-JUDGE EVAL (NTS_091): score the draft before the
                    # manager sees it. EN gets the full rubric (vs the source);
                    # non-EN gets the reduced rubric (vs the EN canon). FAILS
                    # OPEN — score_draft never raises, so a dead judge cannot
                    # block creation/publishing.
                    if language == Language.en:
                        _src = f"{topic.raw.title}\n{topic.raw.summary or ''}"
                        _en_text = ""
                    else:
                        _src = ""
                        _en_text = en_draft.body if en_draft else ""
                    await score_draft(
                        draft_id=draft_id,
                        lang=language.value,
                        draft_text=f"{draft.title}\n\n{draft.body}",
                        eval_enabled=eval_enabled,
                        eval_threshold=eval_threshold,
                        source_text=_src,
                        en_text=_en_text,
                        voice_profile_yaml=brand.voice_profile_yaml or "",
                        brand_id_fk=brand_id_fk,
                        run_id=run_id,
                    )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "topic.failed",
                    topic=topic.id,
                    language=language.value,
                    err=str(exc),
                )
                stats["errors"] += 1
                results.append(
                    {
                        "topic_id": topic.id,
                        "status": "failed",
                        "error": str(exc),
                        "language": language.value,
                    }
                )
                client.record_topic_result(
                    run_id=run_id,
                    topic_id=topic.id,
                    source_id=source_record.id,
                    title=topic.raw.title,
                    url=str(topic.raw.url),
                    score=int(topic.relevance_score),
                    status="failed",
                    filter_reason=str(exc),
                    language=language.value,
                )
    return results, stats, languages_attempted


def _languages_for_brand(
    brand_row, override: Language | None = None
) -> list[Language]:
    """Resolve the list of target languages for one pipeline run.

    Precedence:
      1. ``override`` (the legacy single-``language`` argument on
         ``run_pipeline``). Honoured so existing CLI invocations still
         work — a caller that asks for a specific language gets only that
         language, even if the brand publishes more.
      2. ``brand_row.languages`` (JSON column added in migration 006).
      3. ``[Language.en]`` as a final safety net.
    """
    if override is not None:
        return [override]
    raw = getattr(brand_row, "languages", None) or '["en"]'
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        decoded = ["en"]
    if not isinstance(decoded, list) or not decoded:
        decoded = ["en"]
    out: list[Language] = []
    seen: set[str] = set()
    for code in decoded:
        if not isinstance(code, str) or code in seen:
            continue
        try:
            out.append(Language(code))
            seen.add(code)
        except ValueError:
            log.warning("pipeline.unknown_brand_language", code=code)
    return out or [Language.en]


async def run_pipeline(
    brand_slug: str = "icon",
    source_id: str | None = None,
    source_url: str | None = None,
    language: Language | None = None,
    limit: int = 3,
    dry_run: bool = False,
    *,
    triggered_by: str = "cron",
    existing_run_id: int | None = None,
    brand_id: int | None = None,
) -> list[dict[str, Any]]:
    """Run the pipeline for a brand. After NTS_025 Step 4:

    * ``brand_id`` (int) is the canonical selector; ``brand_slug``
      stays as an alternative for CLI ergonomics.
    * Brand row MUST exist + status='active' + sanity_api_token_enc
      present, else ``BrandNotReadyError``. **No fallback** to
      ``icon_brand_config()`` — that was the S1 transition mechanism.
    * Sanity credentials are decrypted into local variables for the
      duration of this single run only (M3 carve-out); never stored on
      long-lived instance attributes.
    * ``--source-id``/``--source-url`` overrides remain for ad-hoc runs
      but the brand resolution still goes through admin.db.
    """
    from pipeline.admin.config_client import (  # noqa: PLC0415
        AdminConfigClient,
        BrandNotReadyError,
        SourceRecord,
        get_brand,
    )
    from pipeline.admin.cost_recorder import CostContext, cost_context  # noqa: PLC0415

    configure_logging()
    settings = get_settings()
    if dry_run:
        settings.dry_run = True  # type: ignore[misc]

    # 1. Resolve brand row from DB. Either id or slug works.
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    try:
        if brand_id is not None:
            brand_row = get_brand(brand_id)
        else:
            brand_row = get_brand(brand_slug)
    except (LookupError, SQLAlchemyError) as exc:
        raise BrandNotReadyError(
            f"brand {(brand_id or brand_slug)!r} not reachable in admin.db: {exc!s}"
        ) from exc

    # M4: must be active AND have Sanity creds for a real run. dry-run
    # may proceed in draft state for smoke tests (no Sanity write happens).
    if not dry_run:
        if brand_row.status != "active":
            raise BrandNotReadyError(
                f"brand {brand_row.slug!r} status is {brand_row.status!r}; "
                "expected 'active'. Configure Sanity creds + activate via /brands."
            )
        if not brand_row.has_sanity_token or not brand_row.sanity_project_id:
            raise BrandNotReadyError(
                f"brand {brand_row.slug!r} has no Sanity credentials configured"
            )

    # 2. Decrypt creds into LOCAL variables for the duration of this run
    #    only. References are released when this function returns (M3
    #    carve-out: GC clears plaintext from memory).
    sanity_token: str | None = None
    if brand_row.has_sanity_token:
        sanity_token = brand_row.decrypted_sanity_token()

    # 3. AdminConfigClient bound to this brand's slug for source/prompt/
    #    config lookups + run history writes.
    client = AdminConfigClient(brand_slug=brand_row.slug)
    config = client.get_config()
    brand = icon_brand_config()
    # Use the brand row's voice profile when present, otherwise fall back
    # to the hardcoded icon config (placeholder for the four brands that
    # have no voice profile yet).
    voice_yaml = (
        brand_row.voice_profile_yaml
        or config.voice_profile
        or brand.voice_profile_yaml
    )
    # NTS_075 L3: cover-image styles come from the brand voice profile
    # (``image.style_prompts``), not hardcode — default set as fallback.
    brand = BrandConfig(
        slug=brand_row.slug,
        name=brand_row.name,
        voice_profile_yaml=voice_yaml,
        visual=BrandVisual(
            brand_id=brand_row.slug,
            image_style_prompts=_resolve_brand_image_styles(voice_yaml),
        ),
        context=brand.context,
        categories=brand.categories,
        id_fk=brand_row.id,
    )

    brand_id_fk = brand_row.id
    languages = _languages_for_brand(brand_row, override=language)

    # 4. Resolve source list.
    if source_id is not None and source_url is not None:
        # When ``source_id`` is a numeric string, treat it as a DB id and
        # look up the real row so topic writes carry a valid FK. The
        # override path stays useful for ad-hoc CLI runs whose source
        # isn't in admin.db — for those we keep id=None and accept that
        # per-topic rows won't be recorded.
        resolved_source_record: SourceRecord | None = None
        try:
            db_source_id = int(source_id)
        except (TypeError, ValueError):
            db_source_id = None
        if db_source_id is not None:
            from sqlalchemy import select  # noqa: PLC0415

            from pipeline.admin import db as admin_db  # noqa: PLC0415
            from pipeline.admin.models import Source as SourceModel  # noqa: PLC0415

            with admin_db.get_session_factory()() as session:
                row = session.execute(
                    select(SourceModel).where(SourceModel.id == db_source_id)
                ).scalar_one_or_none()
            if row is not None:
                resolved_source_record = SourceRecord(
                    id=row.id,
                    name=row.name,
                    source_type=row.source_type,
                    url=row.url,
                    primary_category=row.primary_category,
                    polling_minutes=row.polling_minutes,
                )
        sources = [
            resolved_source_record
            or SourceRecord(
                id=None,
                name=source_id,
                source_type="rss",
                url=source_url,
                primary_category="wealth",
                polling_minutes=720,
            )
        ]
    else:
        sources = client.get_active_sources()

    log.info(
        "pipeline.start",
        brand=brand_row.slug,
        brand_id=brand_id_fk,
        sources=[s.name for s in sources],
        languages=[l.value for l in languages],
        limit=limit,
        dry_run=dry_run,
        threshold=config.scoring_threshold,
    )

    # 5. Record run start. For dry_run we still write a runs row (with
    #    status='dry_run') so the smoke test can see it. For real runs,
    #    status starts as 'running' and gets flipped on completion.
    run_id = existing_run_id
    if run_id is None:
        run_id = client.record_run_start(
            source_ids=[s.id for s in sources if s.id is not None],
            triggered_by=triggered_by,
        )

    # 6. Construct Sanity publisher with this brand's decrypted creds.
    #    SanityPublisher does NOT retain credentials past the run — only
    #    the local SanityClient holds them, and it's GC'd when this
    #    function returns. (M3 carve-out.)
    if dry_run:
        sanity_publisher = _DryRunSanityPublisher()
    else:
        from pipeline.publisher.sanity import SanityClient  # noqa: PLC0415

        sanity_client = SanityClient(
            project_id=brand_row.sanity_project_id or "",
            dataset=brand_row.sanity_dataset or "production",
            api_version=brand_row.sanity_api_version or "2024-01-01",
            token=sanity_token or "",
        )
        sanity_publisher = SanityPublisher(client=sanity_client)
    # Release the plaintext-token reference; the publisher holds it for
    # the lifetime of the run only.
    sanity_token = None  # noqa: F841

    aggregate_results: list[dict[str, Any]] = []
    aggregate_stats = {
        "fetched": 0,
        "scored": 0,
        "drafted": 0,
        "errors": 0,
        "deduped": 0,
        "images_skipped": 0,
    }
    log_lines: list[str] = []

    # Cost-recording context spans the whole run; topic_id / draft_id
    # are layered in by ``_process_source`` for finer attribution.
    #
    # IT_PROJ_NTS_051 refactor: outer loop is now over sources (was
    # languages); for each source we process ALL configured languages in
    # a single pass so image generation can be hoisted above the
    # language loop. Failure in one (source, topic, language) branch is
    # isolated below so the remaining branches still run.
    #
    # ``mark_language_completed`` used to fire as each language branch
    # finished sequentially; now it fires for every language the run
    # touched at the end (one batch). The UI loses incremental progress
    # for the duration of the fanout but the schema stays compatible.
    # NTS_090 — dedup config (fail-open defaults if the row predates the cols).
    dedup_enabled = getattr(config, "dedup_enabled", True)
    dedup_threshold = getattr(config, "dedup_threshold", 0.85)
    dedup_window_days = getattr(config, "dedup_window_days", 7)
    # NTS_091 — LLM-judge eval config (fail-open defaults if row predates cols).
    eval_enabled = getattr(config, "eval_enabled", True)
    eval_threshold = getattr(config, "eval_threshold", 7.0)
    # NTS_094 — cover-on-demand. Fail-safe default False: an unknown/legacy
    # config keeps generating covers, so the flag can only ever REMOVE spend
    # deliberately, never silently.
    images_on_demand = bool(getattr(config, "images_on_demand", False))
    if images_on_demand:
        log.info("image.on_demand_mode", brand=brand_row.slug)
    # Cleanup old embeddings on pipeline start (best-effort, brand-scoped).
    if dedup_enabled:
        cleanup_old_embeddings(brand_id_fk, dedup_window_days)
    languages_seen: set[str] = set()
    with cost_context(CostContext(brand_id_fk=brand_id_fk, run_id=run_id)):
        for src in sources:
            try:
                results, stats, langs_attempted = await _process_source(
                    source_record=src,
                    brand=brand,
                    brand_id_fk=brand_id_fk,
                    languages=languages,
                    limit=limit,
                    dry_run=dry_run,
                    sanity_publisher=sanity_publisher,
                    client=client,
                    run_id=run_id,
                    min_score=config.scoring_threshold,
                    dedup_enabled=dedup_enabled,
                    dedup_threshold=dedup_threshold,
                    dedup_window_days=dedup_window_days,
                    eval_enabled=eval_enabled,
                    eval_threshold=eval_threshold,
                    images_on_demand=images_on_demand,
                )
                aggregate_results.extend(results)
                for k, v in stats.items():
                    aggregate_stats[k] = aggregate_stats.get(k, 0) + v
                languages_seen.update(langs_attempted)
                log_lines.append(
                    f"source {src.name}: fetched={stats['fetched']} "
                    f"scored={stats['scored']} drafted={stats['drafted']} "
                    f"errors={stats['errors']} "
                    f"covers_skipped={stats['images_skipped']} "
                    f"langs={sorted(langs_attempted)}"
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("source.failed", source=src.name)
                aggregate_stats["errors"] += 1
                log_lines.append(f"source {src.name}: FAILED {exc!r}")

    # Mark every configured language as completed (fanout reached it,
    # even if per-topic publishes within it failed — same semantics as
    # before, just batched at the end).
    for lang in languages:
        if lang.value in languages_seen or not sources:
            client.mark_language_completed(run_id, lang.value)
            log.info("pipeline.language_done", language=lang.value)

    if dry_run:
        overall_status = "dry_run"
    elif aggregate_stats["errors"] == 0:
        overall_status = "success"
    else:
        overall_status = "failed"
    client.record_run_finish(
        run_id,
        status=overall_status,
        stats=aggregate_stats,
        log_excerpt="\n".join(log_lines)[-4000:],
    ) if run_id is not None else None

    log.info("pipeline.done", processed=len(aggregate_results), brand=brand_row.slug,
             status=overall_status, stats=aggregate_stats)
    return aggregate_results


async def run_pipeline_for_run(run_id: int) -> None:
    """Execute the pipeline for an already-recorded ``runs`` row.

    Entry point of the detached run-worker (NTS_074): the admin API spawns
    ``python -m pipeline.run for-run --run-id N`` (see
    :func:`pipeline.admin.jobs.spawn_pipeline_run`), which calls this. Looks up
    the source list from the run row, then delegates to :func:`run_pipeline`
    with ``existing_run_id`` set so we don't double-record.
    """
    # Resolve brand slug from the existing run row so the client uses the
    # right brand context. Step 4 switches AdminConfigClient to take the
    # brand_id_fk directly.
    from pipeline.admin import db as admin_db_mod  # noqa: PLC0415
    from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415
    from pipeline.admin.models import Brand as BrandModel  # noqa: PLC0415
    from pipeline.admin.models import Run as RunModel

    factory = admin_db_mod.get_session_factory()
    with factory() as session:
        run_row = session.get(RunModel, run_id)
        if run_row is None:
            raise LookupError(f"run {run_id} not found")
        brand_row = session.get(BrandModel, run_row.brand_id_fk)
        brand_slug = brand_row.slug if brand_row is not None else "icon"

    client = AdminConfigClient(brand_slug=brand_slug)
    source_ids = client.get_run_source_ids(run_id)

    # Pull the SourceRecords for those ids out of admin.db. We bypass
    # get_active_sources() because the operator may have run a source that
    # has since been deactivated — the run was already accepted.
    from sqlalchemy import select  # noqa: PLC0415

    from pipeline.admin import db as admin_db  # noqa: PLC0415
    from pipeline.admin.models import Source  # noqa: PLC0415

    factory = admin_db.get_session_factory()
    with factory() as session:
        rows = session.scalars(
            select(Source).where(Source.id.in_(source_ids))
        ).all()
    if not rows:
        client.update_run_progress(
            run_id, sources_total=0, sources_done=0, stage="done"
        )
        client.record_run_finish(
            run_id, status="failed", log_excerpt="no sources resolved for this run"
        )
        return

    # NTS_068: keep the run visibly RUNNING with live X/N progress across the
    # whole source list. Each per-source run_pipeline finalises the shared run
    # row (writing that source's stats + a terminal status), so we read +
    # accumulate those stats, re-assert 'running' between sources, and write
    # ONE authoritative finish at the end. Generation logic is untouched — this
    # is pure run-lifecycle orchestration + the additive progress field.
    total = len(rows)
    agg = {"fetched": 0, "scored": 0, "drafted": 0, "errors": 0, "deduped": 0}
    client.update_run_progress(
        run_id,
        sources_total=total,
        sources_done=0,
        drafts=0,
        errors=0,
        current_source=rows[0].name,
        stage="starting",
    )

    # Re-enter the orchestrator with the existing run_id so it appends to
    # the same row instead of creating a sibling. All rows share the same
    # brand_slug (M1 invariant — sources are brand-scoped).
    for i, source in enumerate(rows):
        client.update_run_progress(
            run_id, current_source=source.name, stage="processing"
        )
        await run_pipeline(
            brand_slug=brand_slug,
            source_id=str(source.id),
            source_url=source.url,
            # language=None → fan out to every language the brand publishes.
            limit=3,
            dry_run=False,
            existing_run_id=run_id,
        )
        source_stats = client.get_run_stats(run_id)
        for key in agg:
            agg[key] += int(source_stats.get(key, 0) or 0)
        # Re-assert running for every source but the last, so the indicator
        # doesn't read "completed" mid-fanout. The final status is written
        # once, below.
        if i < total - 1:
            client.set_run_running(run_id, stats=agg)
        client.update_run_progress(
            run_id,
            sources_done=i + 1,
            drafts=agg["drafted"],
            errors=agg["errors"],
        )

    final_status = "success" if agg["errors"] == 0 else "failed"
    client.record_run_finish(run_id, status=final_status, stats=agg)
    client.update_run_progress(
        run_id,
        sources_done=total,
        drafts=agg["drafted"],
        errors=agg["errors"],
        stage="done",
    )


class _DryRunSanityPublisher:
    """Stub publisher used when ``--dry-run`` is on — no network calls."""

    async def is_topic_already_posted(self, topic_id: str, language: Language) -> bool:  # noqa: ARG002
        return False

    async def upload_cover_image(self, image_bytes: bytes, filename: str) -> str:  # noqa: ARG002
        return "dryrun-asset-id"

    async def publish_draft(self, post: SanityPostInput) -> str:  # noqa: ARG002
        return f"dryrun-{post.topic_id}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@app.command()
def main(
    brand_id: int | None = typer.Option(
        None, "--brand-id", help="Brand id (int) — mutually exclusive with --brand-slug"
    ),
    brand_slug: str | None = typer.Option(
        None, "--brand-slug", help="Brand slug (e.g. 'icon')"
    ),
    source_id: str | None = typer.Option(
        None, "--source-id", help="Override: run for this source only"
    ),
    source_url: str | None = typer.Option(
        None, "--source-url", help="Override: feed URL (required with --source-id)"
    ),
    language: str = typer.Option("en", help="Language: ru/uk/en/pl"),
    limit: int = typer.Option(3, help="Max topics per source"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Skip Sanity + image generation"
    ),
) -> None:
    """Run one pipeline pass for a single brand.

    Specify exactly one of ``--brand-id`` or ``--brand-slug`` (M2). With
    ``--source-id`` + ``--source-url``: runs only that source.
    """
    if (brand_id is None) == (brand_slug is None):
        typer.echo(
            "exactly one of --brand-id or --brand-slug must be specified", err=True
        )
        sys.exit(2)
    if (source_id is None) != (source_url is None):
        typer.echo(
            "--source-id and --source-url must be supplied together", err=True
        )
        sys.exit(2)

    try:
        results = asyncio.run(
            run_pipeline(
                brand_slug=brand_slug or "icon",
                brand_id=brand_id,
                source_id=source_id,
                source_url=source_url,
                language=Language(language),
                limit=limit,
                dry_run=dry_run,
            )
        )
    except KeyboardInterrupt:
        typer.echo("Interrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        # Brand not ready / encryption errors should fail the CLI cleanly.
        typer.echo(f"ERROR: {type(exc).__name__}: {exc}", err=True)
        sys.exit(1)

    typer.echo(f"\nProcessed {len(results)} topics:")
    for r in results:
        typer.echo(f"  {r}")


@app.command("for-run")
def for_run(
    run_id: int = typer.Option(..., "--run-id", help="Existing runs.id to execute"),
) -> None:
    """Detached run-worker entry point (NTS_074).

    Executes the pipeline for an already-recorded ``runs`` row in THIS process
    — the admin API spawns ``python -m pipeline.run for-run --run-id N`` as a
    detached subprocess so the run never shares the API event loop. The run
    row's terminal status (success/failed) is written by
    :func:`run_pipeline_for_run`; an operator cancel or a worker crash is
    reconciled by ``pipeline.admin.jobs`` (cancel_run / sweep_orphaned_runs).
    """
    try:
        asyncio.run(run_pipeline_for_run(run_id))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        # run_pipeline_for_run records its own terminal status on the row;
        # surface a non-zero exit for journald/systemd visibility.
        typer.echo(f"ERROR: {type(exc).__name__}: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    app()
