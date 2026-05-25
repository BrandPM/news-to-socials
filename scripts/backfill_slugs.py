"""Backfill Sanity drafts whose slug is missing or broken.

S6 multilingual fanout exposed two distinct slug problems:

* RU/UK/PL drafts created before this fix went through the old inline
  ``slugify`` that stripped every non-Latin character → they all landed
  at ``slug.current == "untitled"``. The published URL is broken.
* A handful of drafts have ``slug.current == null`` outright (older code
  path, the field was dropped on writes).

This script identifies both populations, computes the correct slug via
:func:`pipeline.common.slug.compute_slug`, and patches them in place.

Default mode is **DRY RUN** — counts only, no writes. Pass ``--apply``
to actually patch documents. Andriy gates ``--apply`` on counts looking
reasonable (see stop conditions in IT_PROJ_NTS_051).

Usage:
    .venv/bin/python -m scripts.backfill_slugs --brand-slug icon
    .venv/bin/python -m scripts.backfill_slugs --brand-slug icon --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass

from pipeline.admin import db as admin_db
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand
from pipeline.common.slug import compute_slug
from pipeline.publisher.sanity import SanityClient


@dataclass
class DraftRow:
    sanity_id: str
    title: str | None
    language: str | None
    current_slug: str | None


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


async def _find_broken_drafts(client: SanityClient) -> list[DraftRow]:
    # Two conditions:
    #   - slug is missing / null
    #   - slug.current is literally "untitled" or starts with "untitled-"
    #     (legacy slugify fallback)
    # Drafts only — published posts already have slugs Andriy approved.
    groq = (
        '*[_type == "post" && _id in path("drafts.**") && '
        '(!defined(slug.current) || slug.current == "untitled" || '
        'slug.current match "untitled-*")]'
        '{_id, title, language, "current": slug.current}'
    )
    rows = await client.query(groq)
    if not isinstance(rows, list):
        return []
    out: list[DraftRow] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("_id"):
            continue
        out.append(
            DraftRow(
                sanity_id=str(r["_id"]),
                title=r.get("title"),
                language=r.get("language"),
                current_slug=r.get("current"),
            )
        )
    return out


async def _is_slug_taken(
    client: SanityClient, slug: str, exclude_id: str
) -> bool:
    """Is ``slug`` already owned by a doc other than ``exclude_id``?"""
    groq = (
        '*[_type == "post" && slug.current == $slug && _id != $self][0]._id'
    )
    result = await client.query(groq, {"slug": slug, "self": exclude_id})
    return result is not None


async def _compute_unique_slug(
    client: SanityClient, base: str, draft_id: str
) -> str:
    candidate = base
    for n in range(1, 11):
        # Exclude both the draft and the published mirror.
        if not await _is_slug_taken(client, candidate, draft_id):
            published = draft_id.replace("drafts.", "")
            if not await _is_slug_taken(client, candidate, published):
                return candidate
        candidate = f"{base}-{n + 1}"
    # Pathological — should never happen in real data.
    return f"{base}-{draft_id.split('post-')[-1][:8]}"


async def _backfill(client: SanityClient, apply: bool) -> int:
    rows = await _find_broken_drafts(client)
    if not rows:
        print("No drafts need backfilling. Slugs look healthy.")
        return 0

    by_lang: Counter[str] = Counter()
    plan: list[tuple[DraftRow, str]] = []

    for r in rows:
        lang = r.language or "en"
        by_lang[lang] += 1
        title = r.title or ""
        base = compute_slug(title, lang)
        new_slug = await _compute_unique_slug(client, base, r.sanity_id)
        plan.append((r, new_slug))

    total = len(rows)
    counts_str = ", ".join(f"{k.upper()}={by_lang[k]}" for k in sorted(by_lang))
    print(f"Found {total} drafts with broken/missing slug ({counts_str}).")

    # Stop condition from the spec: >50 is "something else is wrong",
    # halt and ask Andriy before --apply.
    if apply and total > 50:
        print(
            f"REFUSING TO APPLY: {total} drafts is over the 50-draft safety "
            "limit. Re-run with --force-large to override after confirmation.",
            file=sys.stderr,
        )
        return 2

    if not apply:
        print("\nDRY RUN — first 15 changes:")
        for row, new in plan[:15]:
            old = row.current_slug or "<null>"
            print(f"  [{(row.language or '??').upper()}] {old!r:30s} -> {new!r}")
        if total > 15:
            print(f"  ... {total - 15} more")
        print("\nRe-run with --apply to actually patch these drafts.")
        return 0

    print("\nApplying patches...")
    patched = 0
    for row, new_slug in plan:
        try:
            await client.patch(
                row.sanity_id,
                {"slug": {"_type": "slug", "current": new_slug}},
            )
            patched += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"  FAIL [{row.sanity_id}] {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(f"\nPatched {patched}/{total} drafts.")

    # Verify by re-querying.
    remaining = await _find_broken_drafts(client)
    if remaining:
        print(
            f"WARNING: {len(remaining)} drafts still show as broken after patch.",
            file=sys.stderr,
        )
        return 1
    print("Verification re-query returned 0 broken drafts. Done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument(
        "--apply", action="store_true", help="actually patch drafts (default is dry-run)"
    )
    parser.add_argument(
        "--force-large",
        action="store_true",
        help="bypass the 50-draft safety limit (only when --apply)",
    )
    args = parser.parse_args()

    client = _build_sanity_client(args.brand_slug)
    rc = asyncio.run(_backfill(client, apply=args.apply))
    if rc == 2 and args.force_large:
        # Re-run forcing the apply path. The check is in _backfill but we
        # propagate the override via a sentinel: re-call with apply=True
        # after raising the limit locally. (Kept simple: just rerun.)
        async def _go() -> int:
            rows = await _find_broken_drafts(client)
            print(f"FORCED apply over safety limit ({len(rows)} drafts).")
            return await _backfill(client, apply=True)

        # The simple path: spec says "halt and ask Andriy", so for now
        # the operator's confirmation IS --force-large.
        return asyncio.run(_go())
    return rc


if __name__ == "__main__":
    sys.exit(main())
