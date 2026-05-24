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
appears at icon.finance/:lang/insights/:slug.

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
from typing import Any

import httpx
import numpy as np
import openai
import typer

from pipeline.common.config import get_settings
from pipeline.common.logging import configure_logging, get_logger
from pipeline.common.models import (
    Channel,
    Draft,
    Language,
    RawItem,
    Topic,
)
from pipeline.generator.comment_writer import CommentWriter
from pipeline.generator.image import BrandVisual, ImageGenerator
from pipeline.generator.image_resizer import fetch_master, resize_for_channel
from pipeline.publisher.sanity import (
    SanityCategoryMapping,
    SanityClient,
    SanityPostInput,
    SanityPublisher,
)
from pipeline.selector.dedup import DedupConfig, Deduper, extract_entities
from pipeline.selector.topic_picker import BrandContext, TopicPicker
from pipeline.sources import REGISTRY
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
        ),
        visual=BrandVisual(
            brand_id="icon",
            image_style_prompts=[
                "minimalist editorial illustration, muted earth tones, abstract financial geometry",
                "subtle marble texture, classical proportions, dim warm lighting",
                "documentary photography, soft natural light, neutral palette, depth of field",
            ],
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


# --------------------------------------------------------------------------
# Embedding helper — used by dedup
# --------------------------------------------------------------------------


async def _embed(text: str, *, model: str = "text-embedding-3-small") -> np.ndarray:
    """Get an embedding from OpenAI. Cheap, ~$0.00002/topic."""
    settings = get_settings()
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.embeddings.create(model=model, input=text)
    return np.array(resp.data[0].embedding, dtype=np.float32)


# --------------------------------------------------------------------------
# Source loading — Wave 1 loads from a simple in-memory dict.
# When Stage 1 Day 3 produces sources-v0.yaml, this reads that file.
# --------------------------------------------------------------------------


def load_source_by_id(source_id: str, source_url: str | None = None) -> Source:
    """Wave 1 stub: treat source_id as the URL for ad-hoc runs.

    Later we'll load from sources-v0.yaml.
    """
    from pipeline.common.models import SourceType
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


async def dedup_filter(
    candidates: list[tuple[RawItem, int]],
    language: Language,
    deduper: Deduper,
    sanity_publisher: SanityPublisher,
    brand: BrandConfig,
) -> list[Topic]:
    """Two-tier dedup: in-memory + Sanity GROQ query for previously posted."""
    survivors: list[Topic] = []
    for item, score in candidates:
        text = f"{item.title}\n{item.summary or ''}"
        url_hash = hashlib.sha1(str(item.url).encode("utf-8")).hexdigest()
        topic_id = url_hash[:16]

        # Tier 1: in-memory + entity overlap
        try:
            embedding = await _embed(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("embed.failed", url=str(item.url), err=str(exc))
            continue
        if deduper.is_duplicate(item, brand.slug, language, embedding):
            log.info("dedup.local_hit", url=str(item.url))
            continue

        # Tier 2: ask Sanity — was this topic_id already published in this language?
        if await sanity_publisher.is_topic_already_posted(topic_id, language):
            log.info("dedup.sanity_hit", topic_id=topic_id, lang=language.value)
            continue

        entities = extract_entities(text)
        deduper.remember(url_hash, brand.slug, language, embedding, entities)

        survivors.append(
            Topic(
                id=topic_id,
                brand_id=brand.slug,
                raw=item,
                relevance_score=float(score),
                embedding=embedding.tolist(),
                entities=sorted(entities),
            )
        )
    log.info("dedup.done", in_=len(candidates), kept=len(survivors))
    return survivors


async def generate_with_image(
    topic: Topic,
    brand: BrandConfig,
    language: Language,
    sanity_publisher: SanityPublisher,
) -> tuple[Draft, str | None]:
    """Generate the post text + master image + upload image to Sanity assets."""
    settings = get_settings()

    # Text generation
    writer = CommentWriter()
    draft = await writer.write(topic, brand.voice_profile_yaml, language)

    # Image generation + upload (skip on dry-run)
    if settings.dry_run:
        log.info("image.dry_run", topic=topic.id)
        return draft, None

    try:
        gen = ImageGenerator()
        master_url = await gen.generate(topic, brand.visual)
        master_bytes = await fetch_master(master_url)
        # We resize for the blog channel (1792x1008) — the cover used on /insights.
        resized = resize_for_channel(master_bytes, Channel.blog)
        asset_id = await sanity_publisher.upload_cover_image(
            resized, filename=f"{brand.slug}-{topic.id}.png"
        )
        return draft, asset_id
    except Exception:  # noqa: BLE001
        # log.exception captures the traceback so silent image failures
        # surface in /var/log/news-to-socials/run.log (IT_PROJ_NTS_013 Defect 1).
        log.exception("image.failed", topic=topic.id)
        return draft, None  # Continue without image; Andriy can add manually.


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


async def _process_source(
    *,
    source_record,  # config_client.SourceRecord
    brand: BrandConfig,
    brand_id_fk: int,
    language: Language,
    limit: int,
    dry_run: bool,
    sanity_publisher,
    client,  # AdminConfigClient
    run_id: int | None,
    min_score: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run the source → score → dedup → generate stages for ONE source and
    ONE language.

    Returns ``(results, stats)``. Per-topic outcomes are also written to
    the ``topics`` table when ``run_id`` is set. S6 fanout calls this
    once per language; failure inside the call is isolated to that one
    (source × language) branch via the orchestrator's try/except.
    """
    from pipeline.sources.rss import RssSource  # noqa: PLC0415

    stats = {"fetched": 0, "scored": 0, "drafted": 0, "errors": 0}
    if source_record.source_type != "rss":
        # web / telegram sources not implemented yet (S3+).
        log.warning("source.unsupported_type", type=source_record.source_type)
        return [], stats

    source = RssSource(
        source_id=str(source_record.id) if source_record.id is not None else source_record.name,
        name=source_record.name,
        url=source_record.url,
    )

    try:
        raw_items = list(await source.fetch())
        stats["fetched"] = len(raw_items)
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
    if not raw_items:
        log.warning("source.empty", source=source.name)
        return [], stats

    scored = await score_relevant_topics(
        raw_items, brand, min_score=min_score, limit_pool=limit * 4
    )
    stats["scored"] = len(scored)
    if not scored:
        return [], stats

    deduper = Deduper(DedupConfig())
    topics = await dedup_filter(scored, language, deduper, sanity_publisher, brand)
    if not topics:
        return [], stats

    results: list[dict[str, Any]] = []
    for topic in topics[:limit]:
        try:
            draft, asset_id = await generate_with_image(
                topic, brand, language, sanity_publisher
            )
            category = await assign_category(topic.raw, brand)

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
            )

            if dry_run:
                log.info("dry_run.would_create", title=post.title, category=category)
                results.append({"topic_id": topic.id, "status": "dry_run", "category": category, "title": post.title})
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
                results.append({"topic_id": topic.id, "draft_id": draft_id, "category": category, "title": post.title, "language": language.value})
                stats["drafted"] += 1
                log.info("topic.published_as_draft", topic=topic.id, draft_id=draft_id, language=language.value)
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
        except Exception as exc:  # noqa: BLE001
            log.error("topic.failed", topic=topic.id, err=str(exc))
            stats["errors"] += 1
            results.append({"topic_id": topic.id, "status": "failed", "error": str(exc), "language": language.value})
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
    return results, stats


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
    brand = BrandConfig(
        slug=brand_row.slug,
        name=brand_row.name,
        voice_profile_yaml=voice_yaml,
        visual=brand.visual,
        context=brand.context,
        categories=brand.categories,
    )

    brand_id_fk = brand_row.id
    languages = _languages_for_brand(brand_row, override=language)

    # 4. Resolve source list.
    if source_id is not None and source_url is not None:
        sources = [
            SourceRecord(
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
    aggregate_stats = {"fetched": 0, "scored": 0, "drafted": 0, "errors": 0}
    log_lines: list[str] = []

    # Cost-recording context spans the whole run; topic_id / draft_id
    # are layered in by ``_process_source`` for finer attribution.
    #
    # S6.4 fanout: outer loop over languages, inner loop over sources.
    # Failure in one (language, source) branch is isolated via the
    # try/except below so the remaining branches still run. Each
    # finished language is appended to runs.languages_completed so the
    # admin UI can show fanout progress as it happens.
    with cost_context(CostContext(brand_id_fk=brand_id_fk, run_id=run_id)):
        for lang in languages:
            lang_errors = 0
            for src in sources:
                try:
                    results, stats = await _process_source(
                        source_record=src,
                        brand=brand,
                        brand_id_fk=brand_id_fk,
                        language=lang,
                        limit=limit,
                        dry_run=dry_run,
                        sanity_publisher=sanity_publisher,
                        client=client,
                        run_id=run_id,
                        min_score=config.scoring_threshold,
                    )
                    aggregate_results.extend(results)
                    for k, v in stats.items():
                        aggregate_stats[k] = aggregate_stats.get(k, 0) + v
                    log_lines.append(
                        f"[{lang.value}] source {src.name}: fetched={stats['fetched']} "
                        f"scored={stats['scored']} drafted={stats['drafted']} "
                        f"errors={stats['errors']}"
                    )
                    lang_errors += stats.get("errors", 0)
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "source.failed", source=src.name, language=lang.value
                    )
                    aggregate_stats["errors"] += 1
                    lang_errors += 1
                    log_lines.append(
                        f"[{lang.value}] source {src.name}: FAILED {exc!r}"
                    )
            # Mark the language as completed regardless of per-source
            # outcomes — the UI cares about "did fanout reach this lang"
            # rather than "was every source successful for it".
            client.mark_language_completed(run_id, lang.value)
            log.info(
                "pipeline.language_done",
                language=lang.value,
                errors=lang_errors,
            )

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

    Used by ``pipeline.admin.jobs.execute_pipeline_run`` (i.e. the
    ``POST /sources/{id}/run`` endpoint). Looks up the source list from
    the run row, then delegates to :func:`run_pipeline` with
    ``existing_run_id`` set so we don't double-record.
    """
    from pipeline.admin.config_client import AdminConfigClient  # noqa: PLC0415

    # Resolve brand slug from the existing run row so the client uses the
    # right brand context. Step 4 switches AdminConfigClient to take the
    # brand_id_fk directly.
    from pipeline.admin import db as admin_db_mod  # noqa: PLC0415
    from pipeline.admin.models import Brand as BrandModel, Run as RunModel  # noqa: PLC0415

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
        client.record_run_finish(
            run_id, status="failed", log_excerpt="no sources resolved for this run"
        )
        return

    # Re-enter the orchestrator with the existing run_id so it appends to
    # the same row instead of creating a sibling. All rows share the same
    # brand_slug (M1 invariant — sources are brand-scoped).
    for source in rows:
        await run_pipeline(
            brand_slug=brand_slug,
            source_id=str(source.id),
            source_url=source.url,
            # language=None → fan out to every language the brand publishes.
            limit=3,
            dry_run=False,
            existing_run_id=run_id,
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


if __name__ == "__main__":
    app()
