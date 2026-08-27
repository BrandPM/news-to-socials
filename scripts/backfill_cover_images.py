"""Backfill ``coverImage`` on Sanity posts — published and/or pending drafts.

The incident behind IT_PROJ_NTS_090: articles went live with
``coverImage: null``. NTS_090 Task A stops any new one from slipping through;
this script repairs the ones already written.

Two sets need repairing, selected with ``--target``:

* ``published`` (default) — articles already on the site. Nothing but this
  script can fix them; the guard came too late.
* ``drafts`` (NTS_091) — PENDING drafts the new publish guard now blocks
  because their only missing component is the cover. Sweeping them clears the
  editorial queue in one command instead of one Regenerate click per draft.
  They stay drafts: only ``coverImage`` is set, so each one re-enters the
  normal review flow (a manager still approves, and now sees the new cover).
* ``all`` — both passes, published first.

Work is grouped by TOPIC, never by document — one cover per topic shared
across its EN/RU/UK/PL siblings (NTS_069). Two shapes of repair:

* **reuse** — the topic already has a cover on at least one language (a
  partial fanout). The existing asset is patched onto the siblings that lack
  it. Costs nothing and, importantly, does not replace a cover a human may
  have already looked at. In the ``drafts`` pass the donor may be a PUBLISHED
  sibling of the same topic — paying for a second image there would give one
  story two different covers.
* **generate** — no language of the topic has a cover. One image is generated
  from the EN-canonical title via the same path Regenerate uses
  (``build_scene_prompt`` → Flux → resize ``Channel.blog`` → upload) and
  patched onto every sibling in ONE Sanity transaction.

Only ``coverImage`` is written. ``publishedAt`` is NTS_089's separate
backfill and ``_updatedAt`` / ``dateModified`` belong to Sanity. Nothing here
approves or publishes anything.

NTS_094 guard rail: when the brand has ``images_on_demand`` ON, the
``drafts`` pass would mass-generate precisely the covers the pipeline just
decided NOT to generate — one command undoing the whole cost change, at
~$0.04 a topic. That pass warns loudly in dry-run and refuses ``--apply``
without ``--override-images-on-demand``. The ``published`` pass is unaffected:
a live article with no cover is damage regardless of how covers are made.

Default mode is **DRY RUN**: it prints the candidate table and writes
nothing. ``--apply`` executes. A single topic's failure is isolated — the
rest still run, and the summary names what failed.

Usage:
    .venv/bin/python -m scripts.backfill_cover_images --brand-slug icon
    .venv/bin/python -m scripts.backfill_cover_images --brand-slug icon --apply
    .venv/bin/python -m scripts.backfill_cover_images --brand-slug icon \\
        --target drafts
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
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
    # False for a sibling that is only here to *donate* a cover — e.g. the
    # published EN of a topic whose RU draft is being swept. Its own missing
    # cover (if any) belongs to the other target's pass, not this one.
    patchable: bool = True

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
        """Siblings this pass will patch: cover-less AND in the target's scope."""
        return [s for s in self._ordered() if not s.has_cover and s.patchable]

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
    def cost_doc(self) -> Sibling:
        """Which document the generation cost is attributed to.

        A document this pass actually patches, so a drafts sweep bills the
        draft (``drafts.post-…``) rather than a published sibling that only
        came along to donate a title.
        """
        patched = self.missing
        return patched[0] if patched else self.canonical

    @property
    def languages(self) -> str:
        """``EN,RU*,PL!`` — ``*`` will be patched, ``!`` is out of scope."""
        out = []
        for s in self._ordered():
            code = (s.language or "??").upper()
            if s.has_cover:
                out.append(code)
            elif s.patchable:
                out.append(f"{code}*")
            else:
                out.append(f"{code}!")
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


def _images_on_demand(brand_slug: str) -> bool:
    """Is this brand's pipeline leaving covers to the manager (NTS_094)?

    Read defensively: a DB shape that predates the column, or an unreadable
    admin.db, must not stop a legitimate ``published`` repair. False here only
    ever means "do not add the guard rail", never "generate more images".
    """
    from pipeline.admin.models import PipelineConfig  # noqa: PLC0415

    try:
        factory = admin_db.get_session_factory()
        with factory() as session:
            brand = (
                session.query(Brand).filter(Brand.slug == brand_slug).one_or_none()
            )
            if brand is None:
                return False
            cfg = session.get(PipelineConfig, brand.id)
            return bool(getattr(cfg, "images_on_demand", False)) if cfg else False
    except Exception:  # noqa: BLE001
        return False


def _warn_on_demand(brand_slug: str, *, apply: bool, override: bool) -> None:
    """Print the guard-rail banner for a drafts pass under cover-on-demand."""
    print(
        f"\n{'!' * 72}\n"
        f"!! {brand_slug!r} has images_on_demand ON — the pipeline is deliberately\n"
        "!! NOT generating covers, and the manager makes one for the draft they\n"
        "!! actually pick. Every 'generate' row below is a cover this change\n"
        "!! decided not to buy. Sweeping them all is the cost change undone.\n"
        "!!\n"
        "!! Legitimate use: a batch of drafts stranded from BEFORE the flag was\n"
        "!! turned on. If that is not what you are looking at, stop.\n"
        f"{'!' * 72}"
    )
    if apply and not override:
        print(
            "\nREFUSING to --apply the drafts pass while images_on_demand is ON.\n"
            "Re-run with --override-images-on-demand if you mean it.\n"
            "(The published pass is unaffected and still runs.)"
        )


def _sibling_from_row(row: dict, *, patchable: bool = True) -> Sibling:
    return Sibling(
        sanity_id=str(row.get("_id")),
        language=row.get("language"),
        slug=row.get("slug"),
        title=row.get("title"),
        summary=row.get("excerpt") or row.get("keyTakeaway"),
        source_url=row.get("sourceUrl"),
        cover_ref=row.get("coverImageRef"),
        patchable=patchable,
    )


_POST = '_type == "post"'
_IS_DRAFT = '_id in path("drafts.**")'
# Published posts: a ``drafts.`` document is excluded here and owned by the
# ``drafts`` target below.
_PUBLISHED = f"{_POST} && !({_IS_DRAFT})"
# Pending drafts, exactly as the Content Hub's "pending" tab defines them
# (``_status_filter_clause`` in routes/drafts.py): a draft with no ``status``
# field (every pre-NTS_052 draft) or an explicit pending one. A REJECTED draft
# is deliberately out — nobody is going to publish it, so a cover would be
# money spent on a document heading for deletion.
_PENDING_DRAFT = f'{_POST} && {_IS_DRAFT} && (!defined(status) || status == "pending")'
# Reuse donors for the drafts pass: the pending drafts themselves plus the
# topic's published siblings, whose cover we adopt for free.
_PENDING_OR_PUBLISHED = (
    f'{_POST} && (!({_IS_DRAFT}) || !defined(status) || status == "pending")'
)
_NO_COVER = "(!defined(coverImage) || !defined(coverImage.asset._ref))"
_PROJECTION = (
    "{_id, topicId, language, title, excerpt, keyTakeaway, sourceUrl, status, "
    '"isDraft": _id in path("drafts.**"), '
    '"slug": slug.current, "coverImageRef": coverImage.asset._ref}'
)


def _row_is_pending_draft(row: dict) -> bool:
    return bool(row.get("isDraft")) and row.get("status") in (None, "", "pending")


@dataclass(frozen=True)
class Target:
    """One set of documents to repair.

    ``holes`` selects the documents that MUST carry a cover (and are patched);
    ``donors`` is the superset queried for siblings, so a cover already on the
    topic can be adopted for free. ``in_scope`` is the Python-side twin of
    ``holes`` — the donors query returns both kinds of row, and only the
    in-scope ones are patch targets.
    """

    name: str
    plural: str
    holes: str
    donors: str
    in_scope: Callable[[dict], bool]


TARGETS: dict[str, Target] = {
    "published": Target(
        name="published",
        plural="published posts",
        holes=_PUBLISHED,
        donors=_PUBLISHED,
        in_scope=lambda row: not row.get("isDraft"),
    ),
    "drafts": Target(
        name="drafts",
        plural="pending drafts",
        holes=_PENDING_DRAFT,
        donors=_PENDING_OR_PUBLISHED,
        in_scope=_row_is_pending_draft,
    ),
}
TARGET_CHOICES = ("published", "drafts", "all")


async def find_candidates(
    client: SanityClient, target: Target | None = None
) -> list[TopicGroup]:
    """Topics with at least one in-scope document missing its cover.

    Two queries: the cover-less documents, then every sibling of the topics
    they belong to. The second query is what makes ``reuse`` possible — a topic
    where only RU lost its cover should not pay for a new image.
    """
    target = target or TARGETS["published"]
    holes = await client.query(f"*[{target.holes} && {_NO_COVER}]{_PROJECTION}")
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
            f"*[{target.donors} && topicId in $topics]{_PROJECTION}",
            {"topics": sorted(set(topic_ids))},
        )
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not r.get("_id"):
                continue
            tid = str(r.get("topicId"))
            groups.setdefault(tid, TopicGroup(topic_id=tid)).siblings.append(
                _sibling_from_row(r, patchable=target.in_scope(r))
            )

    out = [g for g in groups.values() if g.missing]
    # A post with no topicId has no siblings we can identify — repair it alone.
    out.extend(
        TopicGroup(topic_id=None, siblings=[_sibling_from_row(r)]) for r in orphans
    )
    # Most-incomplete first, then stable by key.
    out.sort(key=lambda g: (-len(g.missing), g.key))
    return out


def print_table(groups: list[TopicGroup], target: Target | None = None) -> None:
    target = target or TARGETS["published"]
    if not groups:
        print(f"No {target.plural} are missing a cover image. Nothing to do.")
        return

    to_generate = [g for g in groups if g.action == "generate"]
    to_reuse = [g for g in groups if g.action == "reuse"]
    docs = sum(len(g.missing) for g in groups)
    est = len(to_generate) * COST_PER_TOPIC_USD

    print(
        f"\n[{target.name}] {len(groups)} topic(s) / {docs} document(s) "
        "missing a cover image."
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
        f"{'ACTION':9}  {'LANGS (*=patch, !=other target)':32}  "
        f"{'SLUG':44.44}  TOPIC"
    )
    print(header)
    print("-" * len(header))
    for g in groups:
        print(
            f"{g.action:9}  "
            f"{g.languages:32}  "
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
        # Cost attribution: a document this pass actually patches. Published
        # sweep → the ``post-…`` id (no draft exists for those any more);
        # drafts sweep → the ``drafts.post-…`` id, so the spend shows up on
        # the draft the manager is about to review.
        cost_doc_id=group.cost_doc.sanity_id,
        summary=canon.summary,
        filename_suffix="backfill",
    )
    return f"generated {asset_id} for {len(targets)} doc(s)"


async def _run_target(
    client: SanityClient,
    target: Target,
    apply: bool,
    *,
    blocked: bool = False,
) -> tuple[int, int]:
    """One pass over one target. Returns ``(repaired, total)`` topic counts.

    ``blocked`` (NTS_094) still lists the candidates — seeing what WOULD be
    generated is the point of the warning — but writes nothing.
    """
    groups = await find_candidates(client, target)
    print_table(groups, target)
    if not groups or not apply or blocked:
        return 0, len(groups)

    print(f"\nApplying cover-image backfill [{target.name}]...")
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
    return ok, len(groups)


def _resolve_targets(name: str) -> list[Target]:
    if name == "all":
        # Published first: the site is the visible damage, and a cover it
        # gains becomes a free donor for the drafts pass that follows.
        return [TARGETS["published"], TARGETS["drafts"]]
    return [TARGETS[name]]


async def _run(
    client: SanityClient,
    apply: bool,
    target: str = "published",
    *,
    brand_slug: str = "icon",
    images_on_demand: bool = False,
    override_on_demand: bool = False,
) -> int:
    ok = total = 0
    refused = False
    for t in _resolve_targets(target):
        # NTS_094 — only the drafts pass is guarded. A PUBLISHED article with
        # no cover is damage on the live site whatever the pipeline does.
        guarded = images_on_demand and t.name == "drafts"
        if guarded:
            _warn_on_demand(brand_slug, apply=apply, override=override_on_demand)
        blocked = guarded and apply and not override_on_demand
        refused = refused or blocked
        t_ok, t_total = await _run_target(client, t, apply, blocked=blocked)
        ok += t_ok
        total += t_total

    if refused:
        # Non-zero: a scheduled/scripted caller must not read "refused" as
        # "nothing needed doing".
        return 2
    if total == 0:
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
    return 0 if ok == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument(
        "--target",
        choices=TARGET_CHOICES,
        default="published",
        help=(
            "which documents to repair: published articles (default), pending "
            "drafts blocked by the publish guard, or both"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually generate + patch coverImage (default is dry-run)",
    )
    parser.add_argument(
        "--override-images-on-demand",
        action="store_true",
        help=(
            "NTS_094: allow --apply on the drafts pass even though the brand "
            "generates covers on demand. Only for drafts stranded from before "
            "the flag was turned on — otherwise this undoes the cost change"
        ),
    )
    args = parser.parse_args()
    client = _build_sanity_client(args.brand_slug)
    return asyncio.run(
        _run(
            client,
            apply=args.apply,
            target=args.target,
            brand_slug=args.brand_slug,
            images_on_demand=_images_on_demand(args.brand_slug),
            override_on_demand=args.override_images_on_demand,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
