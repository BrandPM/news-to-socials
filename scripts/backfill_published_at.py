"""Backfill Sanity ``publishedAt`` on already-published posts (NTS_089).

Before IT_PROJ_NTS_089 the site's ``publishedAt`` was stamped at *approval*
time, so a story from the 9th approved on the 12th showed as "12th". This
script proposes correcting each published post's ``publishedAt`` to the real
**news date**, where publish lagged the news by more than a day.

The "correct" news date is chosen by priority:
  1. the post's ``displayDate`` (set by NTS_089 at draft creation), else
  2. the earliest ``topics.created_at`` in admin.db for that ``topicId`` (the
     draft-creation date — the closest proxy for older posts predating
     ``displayDate``), else
  3. the post's ``_createdAt`` (last-ditch fallback).

A post is a candidate only when ``publishedAt`` is MORE THAN ONE DAY AFTER the
correct date (the stale-forward case the feature fixes). The proposed value is
that date at **12:00:00 UTC**. ``_updatedAt``/``dateModified`` are never
touched — Sanity owns those.

Default mode is **DRY RUN**: it prints a review table (slug, current
publishedAt, proposed) and writes nothing. ``--apply`` patches — gate it on the
table looking right (a legitimately held-back article is a valid reason to
skip a row). Per NTS_087 P2, do NOT run ``--apply`` unattended: it reorders the
public feed.

Usage:
    .venv/bin/python -m scripts.backfill_published_at --brand-slug icon
    .venv/bin/python -m scripts.backfill_published_at --brand-slug icon --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select

from pipeline.admin import db as admin_db
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand, Topic
from pipeline.common.display_date import parse_display_date
from pipeline.publisher.sanity import SanityClient

# The lag that makes a post a backfill candidate: published strictly more than
# this many days after the news date.
STALE_LAG_DAYS = 1


@dataclass
class Candidate:
    sanity_id: str
    slug: str | None
    language: str | None
    current_published_at: datetime
    correct_date: date
    source: str  # "displayDate" | "topics.created_at" | "_createdAt"

    @property
    def proposed_published_at(self) -> datetime:
        return datetime(
            self.correct_date.year,
            self.correct_date.month,
            self.correct_date.day,
            12,
            0,
            0,
            tzinfo=timezone.utc,
        )


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


def _topic_created_date(topic_id: str) -> date | None:
    """Earliest ``topics.created_at`` (as a UTC date) for a Sanity ``topicId``."""
    if not topic_id:
        return None
    factory = admin_db.get_session_factory()
    with factory() as session:
        earliest = session.execute(
            select(func.min(Topic.created_at)).where(Topic.topic_id == topic_id)
        ).scalar_one_or_none()
    if earliest is None:
        return None
    if earliest.tzinfo is not None:
        earliest = earliest.astimezone(timezone.utc)
    return earliest.date()


def _parse_iso_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _correct_date(row: dict) -> tuple[date, str] | None:
    """Resolve the news date + which signal produced it (priority order)."""
    dd = parse_display_date(row.get("displayDate"))
    if dd is not None:
        return dd, "displayDate"
    topic_date = _topic_created_date(str(row.get("topicId") or ""))
    if topic_date is not None:
        return topic_date, "topics.created_at"
    created = _parse_iso_dt(row.get("_createdAt"))
    if created is not None:
        return created.date(), "_createdAt"
    return None


async def _find_candidates(client: SanityClient) -> list[Candidate]:
    groq = (
        '*[_type == "post" && !(_id in path("drafts.**")) && defined(publishedAt)]'
        '{_id, publishedAt, displayDate, topicId, language, _createdAt, '
        '"slug": slug.current}'
    )
    rows = await client.query(groq)
    if not isinstance(rows, list):
        return []
    out: list[Candidate] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("_id"):
            continue
        pub = _parse_iso_dt(r.get("publishedAt"))
        if pub is None:
            continue
        resolved = _correct_date(r)
        if resolved is None:
            continue
        correct_date, source = resolved
        # Candidate only if published MORE THAN a day after the news date.
        if (pub.date() - correct_date).days <= STALE_LAG_DAYS:
            continue
        out.append(
            Candidate(
                sanity_id=str(r["_id"]),
                slug=r.get("slug"),
                language=r.get("language"),
                current_published_at=pub,
                correct_date=correct_date,
                source=source,
            )
        )
    # Most-stale first so the review table leads with the worst offenders.
    out.sort(key=lambda c: (c.current_published_at.date() - c.correct_date).days, reverse=True)
    return out


def _print_table(cands: list[Candidate]) -> None:
    if not cands:
        print("No published posts need a publishedAt correction. Feed is accurate.")
        return
    print(f"\n{len(cands)} candidate(s) — published > {STALE_LAG_DAYS}d after the news date:\n")
    header = f"{'LANG':4}  {'SLUG':40.40}  {'CURRENT publishedAt':20}  {'PROPOSED':12}  SRC"
    print(header)
    print("-" * len(header))
    for c in cands:
        lag = (c.current_published_at.date() - c.correct_date).days
        print(
            f"{(c.language or '??').upper():4}  "
            f"{(c.slug or '<no-slug>'):40.40}  "
            f"{c.current_published_at.date().isoformat():20}  "
            f"{c.proposed_published_at.date().isoformat():12}  "
            f"{c.source} (-{lag}d)"
        )


async def _run(client: SanityClient, apply: bool) -> int:
    cands = await _find_candidates(client)
    _print_table(cands)
    if not cands:
        return 0

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to patch publishedAt.")
        print("NOTE: --apply reorders the public feed; review each row first.")
        return 0

    print("\nApplying publishedAt corrections...")
    patched = 0
    for c in cands:
        try:
            await client.patch(
                c.sanity_id,
                {"publishedAt": c.proposed_published_at.isoformat()},
            )
            patched += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL [{c.sanity_id}] {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"\nPatched {patched}/{len(cands)} posts.")
    return 0 if patched == len(cands) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually patch publishedAt (default is dry-run)",
    )
    args = parser.parse_args()
    client = _build_sanity_client(args.brand_slug)
    return asyncio.run(_run(client, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
