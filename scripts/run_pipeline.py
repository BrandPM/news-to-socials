"""End-to-end pipeline orchestrator.

This is the script that turns the skeleton into a working product. It wires
together the modules under ``pipeline/`` into one runnable pipeline:

    source.fetch
        → topic_picker.score (drop low-relevance)
        → dedup.is_duplicate (drop near-duplicates)
        → comment_writer.write (Haiku draft → Sonnet polish)
        → image.generate + image_resizer (master → per-channel sizes)
        → adapter.format (per-channel content)
        → publisher.dispatch (Directus / TG / Meta)

Each stage logs via structlog and writes intermediate state to Directus so
the approval bot and stale-handler can see what's in flight.

Usage:
    nts run --brand icon --source-id <uuid> --language en --channel blog --limit 5
    nts run ... --dry-run   # no external publish APIs called

This is intentionally a script, not a library. The CLI (``pipeline/cli.py``)
defers to it. When Claude Code reaches Stage 3, this is the first file to
read and adapt.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
import openai
import typer

from pipeline.adapter.blog import format_blog
from pipeline.adapter.facebook import format_facebook
from pipeline.adapter.instagram import format_instagram
from pipeline.adapter.telegram import format_telegram
from pipeline.common.config import get_settings
from pipeline.common.logging import configure_logging, get_logger
from pipeline.common.models import (
    Channel,
    Draft,
    Language,
    Post,
    PostStatus,
    RawItem,
    Topic,
)
from pipeline.generator.comment_writer import CommentWriter
from pipeline.generator.image import BrandVisual, ImageGenerator
from pipeline.generator.image_resizer import resize_for_channel
from pipeline.publisher.directus import DirectusClient, DirectusPublisher
from pipeline.publisher.dispatcher import ChannelRoute, Dispatcher
from pipeline.selector.dedup import DedupConfig, Deduper, extract_entities
from pipeline.selector.topic_picker import BrandContext, TopicPicker
from pipeline.sources import REGISTRY
from pipeline.sources.base import Source

log = get_logger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)


# --------------------------------------------------------------------------
# Loaders — pull config from Directus once at start
# --------------------------------------------------------------------------


@dataclass
class BrandConfig:
    """Everything we need to know about a brand for one pipeline run."""

    id: str
    slug: str
    name: str
    voice_profile_yaml: str
    visual: BrandVisual
    context: BrandContext


async def load_brand(directus: DirectusClient, brand_slug: str) -> BrandConfig:
    """Resolve brand by slug; raise if missing or inactive."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{directus.base_url}/items/brands",
            headers=directus._headers(),
            params={
                "filter[slug][_eq]": brand_slug,
                "filter[active][_eq]": "true",
                "limit": 1,
            },
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    if not rows:
        raise RuntimeError(f"Brand not found or inactive: {brand_slug}")
    row = rows[0]

    visual_config = row.get("visual_config_json") or {}
    image_prompts = visual_config.get("image_style_prompts") or []
    if len(image_prompts) < 3:
        log.warning(
            "brand.few_image_prompts",
            brand=brand_slug,
            count=len(image_prompts),
            hint="Add at least 3 image_style_prompts (W6 mitigation)",
        )

    return BrandConfig(
        id=row["id"],
        slug=row["slug"],
        name=row.get("name", brand_slug),
        voice_profile_yaml=row.get("voice_profile_yaml") or "",
        visual=BrandVisual(brand_id=row["id"], image_style_prompts=image_prompts or [""]),
        context=BrandContext(
            brand_id=row["id"],
            name=row.get("name", brand_slug),
            topics_relevant=row.get("topics_relevant") or [],
            topics_banned=row.get("topics_banned") or [],
        ),
    )


async def load_source(directus: DirectusClient, source_id: str) -> Source:
    """Load a single source row and instantiate its handler class."""
    row = await directus.get_item("sources", source_id)
    if not row:
        raise RuntimeError(f"Source not found: {source_id}")
    if not row.get("active", True):
        raise RuntimeError(f"Source is inactive: {source_id}")

    cls = REGISTRY.get(row["type"])
    if cls is None:
        raise RuntimeError(f"No handler for source type: {row['type']}")

    return cls(
        source_id=row["id"],
        name=row["name"],
        url=row["url"],
        **(row.get("opts") or {}),
    )


async def load_channel_route(
    directus: DirectusClient,
    brand_id: str,
    channel: Channel,
    language: Language,
) -> ChannelRoute | None:
    """Find the matching channels row for (brand, channel, language)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{directus.base_url}/items/channels",
            headers=directus._headers(),
            params={
                "filter[brand_id][_eq]": brand_id,
                "filter[platform][_eq]": channel.value,
                "filter[language][_eq]": language.value,
                "filter[active][_eq]": "true",
                "limit": 1,
            },
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    if not rows:
        log.warning(
            "channel.not_configured",
            brand=brand_id,
            channel=channel.value,
            language=language.value,
        )
        return None

    row = rows[0]
    return ChannelRoute(
        channel=channel,
        target_id=row["account_ref"],
        link=row.get("link_preview_url"),
        hashtags=row.get("hashtags") or [],
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
# Stage helpers — each step of the pipeline as a small async function
# --------------------------------------------------------------------------


async def score_relevant_topics(
    items: list[RawItem],
    brand: BrandConfig,
    min_score: int = 7,
    limit_pool: int = 20,
) -> list[tuple[RawItem, int]]:
    """Score items via Haiku, keep those at or above ``min_score``."""
    picker = TopicPicker()
    pool = items[:limit_pool]
    scored: list[tuple[RawItem, int]] = []

    # Bounded concurrency — don't fan out 50 simultaneous Haiku calls.
    sem = asyncio.Semaphore(5)

    async def _one(item: RawItem) -> tuple[RawItem, int] | None:
        async with sem:
            try:
                score, reason = await picker.score(item, brand.context)
            except Exception as exc:  # noqa: BLE001
                log.warning("score.failed", url=str(item.url), err=str(exc))
                return None
            if score < min_score:
                log.debug("score.below_threshold", url=str(item.url), score=score)
                return None
            return item, score

    results = await asyncio.gather(*(_one(it) for it in pool))
    scored = [r for r in results if r is not None]
    scored.sort(key=lambda r: r[1], reverse=True)
    log.info("score.done", pool=len(pool), passed=len(scored), top_score=scored[0][1] if scored else None)
    return scored


async def dedup_filter(
    candidates: list[tuple[RawItem, int]],
    brand_id: str,
    language: Language,
    deduper: Deduper,
) -> list[Topic]:
    """Compute embeddings and drop duplicates. Returns Topic objects."""
    survivors: list[Topic] = []
    for item, score in candidates:
        text = f"{item.title}\n{item.summary or ''}"
        try:
            embedding = await _embed(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("embed.failed", url=str(item.url), err=str(exc))
            continue

        if deduper.is_duplicate(item, brand_id, language, embedding):
            log.info("dedup.skipped", url=str(item.url))
            continue

        entities = extract_entities(text)
        url_hash = hashlib.sha1(str(item.url).encode("utf-8")).hexdigest()
        deduper.remember(url_hash, brand_id, language, embedding, entities)

        survivors.append(
            Topic(
                id=url_hash[:16],
                brand_id=brand_id,
                raw=item,
                relevance_score=float(score),
                embedding=embedding.tolist(),
                entities=sorted(entities),
            )
        )
    log.info("dedup.done", in_=len(candidates), kept=len(survivors))
    return survivors


async def generate_draft(topic: Topic, brand: BrandConfig, language: Language) -> Draft:
    """Two-stage Claude generation (Haiku → Sonnet)."""
    writer = CommentWriter()
    return await writer.write(topic, brand.voice_profile_yaml, language)


async def attach_image(draft: Draft, topic: Topic, brand: BrandConfig, channel: Channel,
                       directus: DirectusClient) -> Draft:
    """Generate one master image, resize for the channel, upload to Directus."""
    settings = get_settings()
    if settings.dry_run:
        log.info("image.dry_run", topic=topic.id)
        return draft

    generator = ImageGenerator()
    master_url = await generator.generate(topic, brand.visual)

    # Fetch master, resize, upload.
    from pipeline.generator.image_resizer import fetch_master

    master_bytes = await fetch_master(master_url)
    resized = resize_for_channel(master_bytes, channel)

    filename = f"{brand.slug}-{topic.id}-{channel.value}.png"
    file_id = await directus.upload_file(resized, filename, "image/png")
    draft.image_url = f"{directus.base_url}/assets/{file_id}"  # type: ignore[assignment]
    draft.image_alt = topic.raw.title[:120]
    return draft


def format_for_channel(
    draft: Draft, topic: Topic, channel: Channel, route: ChannelRoute | None
) -> Post:
    """Pick the right adapter."""
    source_url = str(topic.raw.url)
    if channel is Channel.blog:
        return format_blog(draft, source_url=source_url)
    if channel is Channel.telegram:
        return format_telegram(draft, source_url=source_url)
    if channel is Channel.facebook:
        return format_facebook(draft, source_url=source_url, hashtags=route.hashtags if route else None)
    if channel is Channel.instagram:
        return format_instagram(draft, hashtags=route.hashtags if route else None)
    raise ValueError(f"Unsupported channel: {channel}")


async def persist_and_publish(
    post: Post,
    topic: Topic,
    brand: BrandConfig,
    channel: Channel,
    directus: DirectusClient,
    dispatcher: Dispatcher,
    route: ChannelRoute | None,
    approval_required: bool,
) -> dict[str, Any]:
    """Write Post into Directus, dispatch (or queue for approval), return record."""
    # 1. Create the posts row in draft / pending_approval state.
    status = PostStatus.pending_approval if approval_required else PostStatus.approved
    row = await directus.create_item(
        "posts",
        {
            "brand_id": brand.id,
            "topic_id": topic.id,
            "language": post.language.value,
            "channel": channel.value,
            "content": post.content,
            "image_url": str(post.image_url) if post.image_url else None,
            "status": status.value,
            "source_url": str(topic.raw.url),
        },
    )
    post_id = row.get("id", topic.id)

    if approval_required:
        log.info("post.queued_for_approval", post_id=post_id, channel=channel.value)
        # TODO(Stage-7): the approval bot picks this up via Directus subscription.
        return {"post_id": post_id, "status": status.value, "external_id": None}

    # 2. Direct publish path (channel.approval_required=false).
    if route is None:
        raise RuntimeError(f"No channel route for {brand.slug}/{channel.value}/{post.language.value}")
    external_id = await dispatcher.dispatch(post, route)
    await directus.update_item(
        "posts",
        post_id,
        {"status": PostStatus.published.value, "external_post_id": external_id},
    )
    return {"post_id": post_id, "status": "published", "external_id": external_id}


# --------------------------------------------------------------------------
# Orchestrator entry point
# --------------------------------------------------------------------------


async def run_pipeline(
    brand_slug: str,
    source_id: str,
    language: Language,
    channel: Channel,
    limit: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """The whole pipeline for one (brand, source, language, channel) run."""
    configure_logging()
    settings = get_settings()
    if dry_run:
        settings.dry_run = True  # type: ignore[misc]
    log.info(
        "pipeline.start",
        brand=brand_slug,
        source=source_id,
        language=language.value,
        channel=channel.value,
        limit=limit,
        dry_run=dry_run,
    )

    directus = DirectusClient()
    dispatcher = Dispatcher(directus_client=directus)
    deduper = Deduper(DedupConfig())

    # 1. Load config
    brand = await load_brand(directus, brand_slug)
    source = await load_source(directus, source_id)
    route = await load_channel_route(directus, brand.id, channel, language)
    approval_required = True
    if route is not None:
        # ChannelRoute doesn't carry approval_required; fetch the row again.
        channel_row = await _find_channel_row(directus, brand.id, channel, language)
        approval_required = bool(channel_row.get("approval_required", True))

    # 2. Fetch source items
    raw_items = list(await source.fetch())
    log.info("source.fetched", count=len(raw_items), source=source.name)
    if not raw_items:
        log.warning("source.empty", source=source.name)
        return []

    # 3. Score relevance
    scored = await score_relevant_topics(raw_items, brand, min_score=7, limit_pool=limit * 4)
    if not scored:
        log.warning("score.none_passed", brand=brand_slug)
        return []

    # 4. Dedup
    topics = await dedup_filter(scored, brand.id, language, deduper)
    if not topics:
        log.warning("dedup.all_filtered", brand=brand_slug)
        return []

    # 5. Generate + image + adapt + publish, one topic at a time
    results: list[dict[str, Any]] = []
    for topic in topics[:limit]:
        try:
            draft = await generate_draft(topic, brand, language)
            draft = await attach_image(draft, topic, brand, channel, directus)
            post = format_for_channel(draft, topic, channel, route)
            res = await persist_and_publish(
                post, topic, brand, channel, directus, dispatcher, route, approval_required
            )
            results.append(res)
            log.info("topic.done", topic=topic.id, result=res)
        except Exception as exc:  # noqa: BLE001
            log.error("topic.failed", topic=topic.id, err=str(exc))
            results.append({"topic_id": topic.id, "status": "failed", "error": str(exc)})

    log.info("pipeline.done", processed=len(results), brand=brand_slug)
    return results


async def _find_channel_row(
    directus: DirectusClient, brand_id: str, channel: Channel, language: Language
) -> dict[str, Any]:
    """Refetch the channel row to read approval_required.

    Kept separate from load_channel_route so we don't bloat ChannelRoute with
    fields it doesn't need at publish time.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{directus.base_url}/items/channels",
            headers=directus._headers(),
            params={
                "filter[brand_id][_eq]": brand_id,
                "filter[platform][_eq]": channel.value,
                "filter[language][_eq]": language.value,
                "limit": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    return data[0] if data else {}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@app.command()
def main(
    brand: str = typer.Option(..., help="Brand slug, e.g. 'icon'"),
    source_id: str = typer.Option(..., "--source-id", help="Source UUID from Directus"),
    language: str = typer.Option("en", help="Language code: ru/uk/en/pl"),
    channel: str = typer.Option("blog", help="Channel: blog/telegram/facebook/instagram"),
    limit: int = typer.Option(5, help="Max topics to process"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip external publish"),
) -> None:
    """Run one pipeline pass."""
    try:
        results = asyncio.run(
            run_pipeline(
                brand_slug=brand,
                source_id=source_id,
                language=Language(language),
                channel=Channel(channel),
                limit=limit,
                dry_run=dry_run,
            )
        )
    except KeyboardInterrupt:
        typer.echo("Interrupted")
        sys.exit(130)

    typer.echo(f"\nProcessed {len(results)} topics:")
    for r in results:
        typer.echo(f"  {r}")


if __name__ == "__main__":
    app()
