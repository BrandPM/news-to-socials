"""Backfill ``coverImage`` on already-published Sanity posts (NTS_090).

The incident behind IT_PROJ_NTS_090: articles went live with
``coverImage: null``. Task A stops any new one from slipping through; this
script repairs the ones already on the site.

Work is grouped by TOPIC, never by document — one cover per topic shared
across its EN/RU/UK/PL siblings (NTS_069). Two shapes of repair:

* **reuse** — the topic already has a cover on at least one language (a
  partial fanout). The existing asset is patched onto the siblings that lack
  it. Costs nothing and, importantly, does not replace a cover a human may
  have already looked at.
* **generate** — no language of the topic has a cover. One image is generated
  from the EN-canonical title via the same path Regenerate uses
  (``build_scene_prompt`` → Flux → resize ``Channel.blog`` → upload) and
  patched onto every sibling in ONE Sanity transaction.

Only ``coverImage`` is written. ``publishedAt`` is NTS_089's separate
backfill and ``_updatedAt`` / ``dateModified`` belong to Sanity.

Default mode is **DRY RUN**: it prints the candidate table and writes
nothing. ``--apply`` executes. A single topic's failure is isolated — the
rest still run, and the summary names what failed.

Usage:
    .venv/bin/python -m scripts.backfill_cover_images --brand-slug icon
    .venv/bin/python -m scripts.backfill_cover_images --brand-slug icon --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from pipeline.admin import db as admin_db
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand
from pipeline.publisher.sanity import SanityClient, SanityPublisher

# Flux 1.1 pro, one 16:9 master per topic. The gpt-4o-mini scene brief adds a
# fraction of a cent on top; rounded into this figure for the estimate.
COST_PER_TOPIC_USD = 0.04

LANGUAGE_ORDER = ("en", "ru", "uk", "pl")


@dataclass
class Sibling:
    sanity_id: str
    language: str | None
    slug: str | None
    title: str | None
    summary: str | None
    source_url: str | None
    cover_ref: str | None

    @property
    def has_cover(self) -> bool:
        return bool(self.cover_ref)


@dataclass
class TopicGroup:
    """One article across its languages. ``topic_id`` is ``None`` for orphan
    documents that carry no ``topicId`` — those are repaired one by one."""

    topic_id: str | None
    siblings: list[Sibling] = field(default_factory=list)

    @property
    def missing(self) -> list[Sibling]:
        return [s for s in self.siblings if not s.has_cover]

    @property
    def existing_cover_ref(self) -> str | None:
        """A cover already present on some language of this topic, if any."""
        for s in self._ordered():
            if s.has_cover:
                return s.cover_ref
        return None

    @property
    def action(self) -> str:
        return "reuse" if self.existing_cover_ref else "generate"

    def _ordered(self) -> list[Sibling]:
        """Siblings in EN-first language order — EN is canonical (NTS_069)."""

        def key(s: Sibling) -> tuple[int, str]:
            lang = (s.language or "").lower()
            idx = (
                LANGUAGE_ORDER.index(lang)
                if lang in LANGUAGE_ORDER
                else len(LANGUAGE_ORDER)
            )
            return idx, s.sanity_id

        return sorted(self.siblings, key=key)

    @property
    def canonical(self) -> Sibling:
        """The sibling whose title drives the image prompt — EN when present."""
        return self._ordered()[0]

    @property
    def languages(self) -> str:
        out = []
        for s in self._ordered():
            code = (s.language or "??").upper()
            out.append(code if s.has_cover else f"{code}*")
        return ",".join(out)

    @property
    def key(self) -> str:
        return self.topic_id or f"(no topicId) {self.canonical.sanity_id}"


def _build_sanity_client(brand_slug: str) -> SanityClient:
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand = session.query(Brand).filter(Brand.slug == brand_slug).one_or_none()
        if brand is None:
            raise SystemExit(f"brand {brand_slug!r} not found in admin.db")
        if not brand.sanity_project_id or not brand.sanity_api_token_enc:
            raise SystemExit(f"brand {brand_slug!r} has no Sanity creds configured")
        token = get_encryption().decrypt(brand.sanity_api_token_enc)
        return SanityClient(
            project_id=brand.sanity_project_id,
            dataset=brand.sanity_dataset or "production",
            api_version=brand.sanity_api_version or "2024-01-01",
            token=token,
        )


def _sibling_from_row(row: dict) -> Sibling:
    return Sibling(
        sanity_id=str(row.get("_id")),
        language=row.get("language"),
        slug=row.get("slug"),
        title=row.get("title"),
        summary=row.get("excerpt") or row.get("keyTakeaway"),
        source_url=row.get("sourceUrl"),
        cover_ref=row.get("coverImageRef"),
    )


# Published posts only: a ``drafts.`` document is the Content Hub's problem
# and Task A's guard now stops it from shipping cover-less.
_PUBLISHED = '_type == "post" && !(_id in path("drafts.**"))'
_NO_COVER = "(!defined(coverImage) || !defined(coverImage.asset._ref))"
_PROJECTION = (
    "{_id, topicId, language, title, excerpt, keyTakeaway, sourceUrl, "
    '"slug": slug.current, "coverImageRef": coverImage.asset._ref}'
)


async def find_candidates(client: SanityClient) -> list[TopicGroup]:
    """Topics with at least one published post missing its cover.

    Two queries: the cover-less posts, then every sibling of the topics they
    belong to. The second query is what makes ``reuse`` possible — a topic
    where only RU lost its cover should not pay for a new image.
    """
    holes = await client.query(f"*[{_PUBLISHED} && {_NO_COVER}]{_PROJECTION}")
    if not isinstance(holes, list) or not holes:
        return []

    orphans: list[dict] = []
    topic_ids: list[str] = []
    for r in holes:
        if not isinstance(r, dict) or not r.get("_id"):
            continue
        tid = r.get("topicId")
        if tid:
            topic_ids.append(str(tid))
        else:
            orphans.append(r)

    groups: dict[str, TopicGroup] = {}
    if topic_ids:
        rows = await client.query(
            f"*[{_PUBLISHED} && topicId in $topics]{_PROJECTION}",
            {"topics": sorted(set(topic_ids))},
        )
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not r.get("_id"):
                continue
            tid = str(r.get("topicId"))
            groups.setdefault(tid, TopicGroup(topic_id=tid)).siblings.append(
                _sibling_from_row(r)
            )

    out = list(groups.values())
    # A post with no topicId has no siblings we can identify — repair it alone.
    out.extend(
        TopicGroup(topic_id=None, siblings=[_sibling_from_row(r)]) for r in orphans
    )
    # Most-incomplete first, then stable by key.
    out.sort(key=lambda g: (-len(g.missing), g.key))
    return out


def print_table(groups: list[TopicGroup]) -> None:
    if not groups:
        print("No published posts are missing a cover image. Nothing to do.")
        return

    to_generate = [g for g in groups if g.action == "generate"]
    to_reuse = [g for g in groups if g.action == "reuse"]
    docs = sum(len(g.missing) for g in groups)
    est = len(to_generate) * COST_PER_TOPIC_USD

    print(
        f"\n{len(groups)} topic(s) / {docs} document(s) missing a cover image."
    )
    print(
        f"  {len(to_generate)} topic(s) need a NEW image "
        f"× ~${COST_PER_TOPIC_USD:.2f} = ~${est:.2f} estimated"
    )
    print(
        f"  {len(to_reuse)} topic(s) can REUSE a cover already on a sibling "
        "language (free)\n"
    )
    header = (
        f"{'ACTION':9}  {'LANGS (*=no cover)':22}  {'SLUG':44.44}  TOPIC"
    )
    print(header)
    print("-" * len(header))
    for g in groups:
        print(
            f"{g.action:9}  "
            f"{g.languages:22}  "
            f"{(g.canonical.slug or '<no-slug>'):44.44}  "
            f"{g.key}"
        )


async def _apply_group(client: SanityClient, group: TopicGroup) -> str:
    """Repair one topic. Returns a short human-readable outcome."""
    targets = [s.sanity_id for s in group.missing]
    if not targets:
        return "nothing to patch"

    existing = group.existing_cover_ref
    if existing:
        # Free path: adopt the cover the topic already has. Only the
        # cover-less siblings are touched — never overwrite a live cover.
        await client.mutate(
            [
                {
                    "patch": {
                        "id": sid,
                        "set": {
                            "coverImage": {
                                "_type": "image",
                                "asset": {"_type": "reference", "_ref": existing},
                            }
                        },
                    }
                }
                for sid in targets
            ]
        )
        return f"reused {existing} on {len(targets)} doc(s)"

    from pipeline.admin.image_regenerate import (  # noqa: PLC0415
        generate_and_apply_cover,
    )

    canon = group.canonical
    asset_id = await generate_and_apply_cover(
        title=canon.title or "Untitled",
        topic_id=group.topic_id or "unknown",
        source_url=canon.source_url or "https://example.com/",
        target_ids=targets,
        client=client,
        publisher=SanityPublisher(client=client),
        # Cost attribution: the published doc id, not a ``drafts.`` id — no
        # draft exists for these any more.
        cost_doc_id=canon.sanity_id,
        summary=canon.summary,
        filename_suffix="backfill",
    )
    return f"generated {asset_id} for {len(targets)} doc(s)"


async def _run(client: SanityClient, apply: bool) -> int:
    groups = await find_candidates(client)
    print_table(groups)
    if not groups:
        return 0

    if not apply:
        print(
            "\nDRY RUN — nothing written. Re-run with --apply to patch coverImage."
        )
        print(
            "NOTE: --apply generates images (real money) and changes what the "
            "public site shows; review the table first."
        )
        return 0

    print("\nApplying cover-image backfill...")
    ok = 0
    for g in groups:
        try:
            outcome = await _apply_group(client, g)
        except Exception as exc:  # noqa: BLE001
            # Isolate per topic, same posture as the pipeline: one bad topic
            # must not cost us the rest of the run.
            print(
                f"  FAIL [{g.key}] {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        ok += 1
        print(f"  OK   [{g.key}] {outcome}")

    print(f"\nRepaired {ok}/{len(groups)} topic(s).")
    return 0 if ok == len(groups) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually generate + patch coverImage (default is dry-run)",
    )
    args = parser.parse_args()
    client = _build_sanity_client(args.brand_slug)
    return asyncio.run(_run(client, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
