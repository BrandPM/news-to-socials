"""Backfill: purge admin.db references to drafts that no longer exist in Sanity.

IT_PROJ_NTS_062. The permanent-delete endpoint historically deleted the
Sanity document but left local pointers dangling. Those orphans are silent
desync — they don't show in the (Sanity-sourced) Content-hub list, but they
keep dead ``draft_id`` links in ``topics`` / ``cost_records`` and stale
``rejected`` rows in ``draft_approvals``. This script finds every draft id
referenced in admin.db for a brand, asks Sanity which of them still exist,
and unlinks the ones that don't via the SAME helper the live delete uses
(:func:`pipeline.admin.routes.drafts.purge_draft_local_refs`).

Safety:
  * **Dead refs only.** A reference is purged only if Sanity confirms the
    doc is GONE. Anything Sanity still has is left untouched — real drafts
    are never modified.
  * **Brand-scoped.** Operates on a single brand (default ``icon``); never
    crosses brands.
  * **Dry-run by default.** Prints the full plan and writes nothing. Pass
    ``--apply`` to act.
  * **Backup before write.** ``--apply`` copies ``admin.db`` (+ ``-wal`` /
    ``-shm`` sidecars) to ``admin.db.bak-<unix-ts>`` before any DELETE/UPDATE.

Usage:
    .venv/bin/python -m scripts.backfill_dead_draft_refs --brand-slug icon
    .venv/bin/python -m scripts.backfill_dead_draft_refs --brand-slug icon --apply
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand, CostRecord, DraftApproval, Run, Topic
from pipeline.admin.routes.drafts import _normalise_draft_id, purge_draft_local_refs
from pipeline.common.config import get_settings
from pipeline.publisher.sanity import SanityClient

# Seed id from the IT_PROJ_NTS_062 report — the one we know is dead.
KNOWN_DEAD_SEED = "post-4670c339e90e"


def _build_sanity_client(brand_slug: str) -> tuple[SanityClient, int]:
    factory = admin_db.get_session_factory()
    with factory() as session:
        brand = session.query(Brand).filter(Brand.slug == brand_slug).one_or_none()
        if brand is None:
            raise SystemExit(f"brand {brand_slug!r} not found in admin.db")
        if not brand.sanity_project_id or not brand.sanity_api_token_enc:
            raise SystemExit(f"brand {brand_slug!r} has no Sanity creds configured")
        token = get_encryption().decrypt(brand.sanity_api_token_enc)
        client = SanityClient(
            project_id=brand.sanity_project_id,
            dataset=brand.sanity_dataset or "production",
            api_version=brand.sanity_api_version or "2024-01-01",
            token=token,
        )
        return client, brand.id


def _collect_referenced_ids(brand_id: int) -> dict[str, set[str]]:
    """Every draft id referenced in admin.db for ``brand_id``, per table.

    Keyed by table name → set of normalised ``drafts.<id>`` strings.
    """
    factory = admin_db.get_session_factory()
    by_table: dict[str, set[str]] = {
        "topics": set(),
        "cost_records": set(),
        "draft_approvals": set(),
    }
    with factory() as session:
        # topics: scoped via its run's brand.
        for (did,) in session.execute(
            select(Topic.draft_id)
            .join(Run, Topic.run_id == Run.id)
            .where(Topic.draft_id.is_not(None), Run.brand_id_fk == brand_id)
        ):
            if did:
                by_table["topics"].add(_normalise_draft_id(did))
        for (did,) in session.execute(
            select(CostRecord.draft_id).where(
                CostRecord.draft_id.is_not(None),
                CostRecord.brand_id_fk == brand_id,
            )
        ):
            if did:
                by_table["cost_records"].add(_normalise_draft_id(did))
        for (did,) in session.execute(
            select(DraftApproval.sanity_draft_id).where(
                DraftApproval.brand_id_fk == brand_id
            )
        ):
            if did:
                by_table["draft_approvals"].add(_normalise_draft_id(did))
    return by_table


async def _existing_in_sanity(client: SanityClient, ids: list[str]) -> set[str]:
    """Return the subset of ``ids`` (normalised) that Sanity still has.

    We ask for both the draft form (``drafts.<id>``) and the published
    mirror (``<id>``): if a doc was published, its draft id maps to a live
    published doc and must NOT be treated as dead.
    """
    if not ids:
        return set()
    bare = [i[len("drafts.") :] for i in ids]
    groq = '*[_id in $ids]._id'
    rows = await client.query(groq, {"ids": ids + bare})
    found_raw = set(rows) if isinstance(rows, list) else set()
    alive: set[str] = set()
    for normalised in ids:
        if normalised in found_raw or normalised[len("drafts.") :] in found_raw:
            alive.add(normalised)
    return alive


def _backup_admin_db() -> list[Path]:
    """Copy admin.db (+ WAL/SHM sidecars) to timestamped .bak files."""
    db_path = Path(get_settings().admin_db_path).expanduser()
    ts = int(time.time())
    made: list[Path] = []
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{db_path}{suffix}")
        if src.exists():
            dst = Path(f"{db_path}.bak-{ts}{suffix}")
            shutil.copy2(src, dst)
            made.append(dst)
    return made


async def _run(brand_slug: str, apply: bool) -> int:
    client, brand_id = _build_sanity_client(brand_slug)

    by_table = _collect_referenced_ids(brand_id)
    all_ids = sorted(set().union(*by_table.values()))
    seed = _normalise_draft_id(KNOWN_DEAD_SEED)
    if seed not in all_ids:
        print(
            f"Note: seed {seed!r} is not referenced in admin.db for "
            f"brand {brand_slug!r} (already clean or never recorded)."
        )

    if not all_ids:
        print(f"No draft refs in admin.db for brand {brand_slug!r}. Nothing to do.")
        return 0

    alive = await _existing_in_sanity(client, all_ids)
    dead = [i for i in all_ids if i not in alive]

    print(
        f"Brand {brand_slug!r}: {len(all_ids)} distinct draft refs in admin.db, "
        f"{len(alive)} still in Sanity, {len(dead)} dead."
    )
    if not dead:
        print("No dead references. admin.db is in sync with Sanity.")
        return 0

    print("\nDead references to purge (NULL topics/cost links, DROP approval row):")
    for did in dead:
        tables = [t for t, ids in by_table.items() if did in ids]
        marker = "  <-- seed" if did == seed else ""
        print(f"  {did:32s} in [{', '.join(sorted(tables))}]{marker}")

    if not apply:
        print(
            "\nDRY RUN — nothing written. Re-run with --apply to purge "
            "(a timestamped admin.db backup is taken first)."
        )
        return 0

    backups = _backup_admin_db()
    print("\nBackups written:")
    for b in backups:
        print(f"  {b}")

    totals = {"topics": 0, "cost_records": 0, "draft_approvals": 0}
    with admin_db.session_scope() as session:
        for did in dead:
            counts = purge_draft_local_refs(session, did, brand_id)
            for k, v in counts.items():
                totals[k] += v
    print(
        f"\nPurged {len(dead)} dead drafts. Rows touched — "
        f"topics(draft_id→NULL)={totals['topics']}, "
        f"cost_records(draft_id→NULL)={totals['cost_records']}, "
        f"draft_approvals(deleted)={totals['draft_approvals']}."
    )

    # Verify: re-collect and confirm none of the dead ids remain referenced.
    remaining = _collect_referenced_ids(brand_id)
    still = [
        i
        for i in dead
        if any(i in remaining[t] for t in remaining)
    ]
    if still:
        print(
            f"WARNING: {len(still)} ids still referenced after purge: {still}",
            file=sys.stderr,
        )
        return 1
    print("Verification re-scan: 0 dead refs remain. Done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually purge dead refs (default is dry-run)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.brand_slug, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
