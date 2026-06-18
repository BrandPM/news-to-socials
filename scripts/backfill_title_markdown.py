"""Backfill draft titles that leaked markdown (IT_PROJ_NTS_060).

The translation/polish pass occasionally returned a *title* with leading
heading markers ("## ", "# "), bold (``**...**``), or backticks — the
markdown belongs in the body, not the title. The English source title was
clean; only the translated drafts (RU/UK/PL) broke. Example from prod
(topic 0f7c49edcb):

    EN: "The Shifting Landscape of Tax Advisory Services"        (clean)
    PL: "## Klienci stawiają na strategię w doradztwie podatkowym"

This script cleans titles already persisted, in two stores:

* **Sanity** draft documents (``drafts.**``) — what the admin UI shows.
* **admin.db** ``topics.title`` rows — the run-history / audit copy.

It reuses :func:`pipeline.generator.comment_writer.sanitize_title`, the
SAME function now applied on the live write path, so the backfill and the
fix can never diverge. Only the ``title`` is touched — the body is left
exactly as-is.

Default mode is **DRY RUN** — it prints every change it *would* make and
writes nothing. Pass ``--apply`` to actually patch. On ``--apply`` the
script first copies ``admin.db`` to a timestamped ``.bak`` before any DB
write (it never touches credentials in logs).

Usage:
    .venv/bin/python -m scripts.backfill_title_markdown --brand-slug icon
    .venv/bin/python -m scripts.backfill_title_markdown --brand-slug icon --apply
    # focus a single topic (e.g. the verify topic):
    .venv/bin/python -m scripts.backfill_title_markdown --brand-slug icon --topic-id 0f7c49edcb
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from dataclasses import dataclass

from pipeline.admin import db as admin_db
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand, Topic
from pipeline.common.config import get_settings
from pipeline.generator.comment_writer import sanitize_title
from pipeline.publisher.sanity import SanityClient

# Safety valve: a markdown-in-title bug should affect a handful of drafts,
# not the whole dataset. If we somehow match more than this, halt and ask
# before writing — same posture as backfill_slugs.
_SAFETY_LIMIT = 200


@dataclass
class TitleChange:
    store: str  # "sanity" | "admin.db"
    ref: str  # sanity _id, or admin.db row id
    language: str
    old: str
    new: str


def _build_sanity_client(brand_slug: str) -> SanityClient:
    """Build a Sanity client from the brand's encrypted creds in admin.db."""
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


async def _find_sanity_changes(
    client: SanityClient, topic_id: str | None
) -> list[TitleChange]:
    """Fetch draft titles and keep the ones the sanitizer would change.

    We pull every draft title and filter in Python with ``sanitize_title``
    rather than encoding the markdown rule in GROQ — that guarantees the
    backfill matches the live fix exactly.
    """
    groq = '*[_type == "post" && _id in path("drafts.**")'
    params: dict[str, object] = {}
    if topic_id:
        groq += " && topicId == $tid"
        params["tid"] = topic_id
    groq += "]{_id, title, language}"

    rows = await client.query(groq, params or None)
    if not isinstance(rows, list):
        return []
    changes: list[TitleChange] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("_id"):
            continue
        old = r.get("title") or ""
        new = sanitize_title(old)
        if new != old:
            changes.append(
                TitleChange(
                    store="sanity",
                    ref=str(r["_id"]),
                    language=str(r.get("language") or "??"),
                    old=old,
                    new=new,
                )
            )
    return changes


def _find_db_changes(topic_id: str | None) -> list[TitleChange]:
    """Find admin.db topics rows whose title the sanitizer would change."""
    factory = admin_db.get_session_factory()
    changes: list[TitleChange] = []
    with factory() as session:
        q = session.query(Topic)
        if topic_id:
            q = q.filter(Topic.topic_id == topic_id)
        for row in q.all():
            old = row.title or ""
            new = sanitize_title(old)
            if new != old:
                changes.append(
                    TitleChange(
                        store="admin.db",
                        ref=str(row.id),
                        language=row.language or "??",
                        old=old,
                        new=new,
                    )
                )
    return changes


def _print_plan(changes: list[TitleChange]) -> None:
    for c in changes:
        print(f"  [{c.store:8s}][{c.language.upper():2s}] {c.ref}")
        print(f"      - {c.old!r}")
        print(f"      + {c.new!r}")


def _backup_admin_db() -> str:
    """Copy admin.db -> admin.db.bak-<epoch> before any DB write.

    The DB runs in WAL mode, so we copy the ``-wal``/``-shm`` sidecars too
    when present — a bare copy of the main file could miss not-yet-checkpointed
    commits. ``time.time()`` is used here (a one-shot operator script, not a
    resumable workflow).
    """
    from pathlib import Path

    src = Path(get_settings().admin_db_path).expanduser()
    stamp = int(time.time())
    dst = f"{src}.bak-{stamp}"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(f"{src}{suffix}")
        if side.exists():
            shutil.copy2(side, f"{dst}{suffix}")
    return dst


async def _apply_sanity(client: SanityClient, changes: list[TitleChange]) -> int:
    patched = 0
    for c in changes:
        try:
            await client.patch(c.ref, {"title": c.new})
            patched += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL [sanity {c.ref}] {type(exc).__name__}: {exc}", file=sys.stderr)
    return patched


def _apply_db(changes: list[TitleChange]) -> int:
    factory = admin_db.get_session_factory()
    updated = 0
    with factory() as session:
        for c in changes:
            row = session.get(Topic, int(c.ref))
            if row is None:
                continue
            row.title = c.new
            updated += 1
        session.commit()
    return updated


async def _run(brand_slug: str, topic_id: str | None, apply: bool, force_large: bool) -> int:
    client = _build_sanity_client(brand_slug)

    sanity_changes = await _find_sanity_changes(client, topic_id)
    db_changes = _find_db_changes(topic_id)
    total = len(sanity_changes) + len(db_changes)

    if total == 0:
        print("No titles with markdown found. Nothing to backfill.")
        return 0

    print(
        f"Found {total} title(s) with markdown: "
        f"{len(sanity_changes)} in Sanity, {len(db_changes)} in admin.db."
    )

    if apply and total > _SAFETY_LIMIT and not force_large:
        print(
            f"REFUSING TO APPLY: {total} titles is over the {_SAFETY_LIMIT}-title "
            "safety limit — something may be wrong. Re-run with --force-large to "
            "override after eyeballing the dry run.",
            file=sys.stderr,
        )
        return 2

    if not apply:
        print("\nDRY RUN — changes that WOULD be made (title only; body untouched):\n")
        _print_plan(sanity_changes + db_changes)
        print("\nRe-run with --apply to write these changes.")
        return 0

    backup = _backup_admin_db()
    print(f"\nBacked up admin.db -> {backup}")

    print("Patching Sanity drafts...")
    patched = await _apply_sanity(client, sanity_changes)
    print(f"  Sanity: patched {patched}/{len(sanity_changes)}.")

    print("Updating admin.db topics...")
    updated = _apply_db(db_changes)
    print(f"  admin.db: updated {updated}/{len(db_changes)}.")

    # Verify by re-querying both stores.
    remaining = await _find_sanity_changes(client, topic_id)
    remaining += _find_db_changes(topic_id)
    if remaining:
        print(
            f"WARNING: {len(remaining)} title(s) still show markdown after backfill.",
            file=sys.stderr,
        )
        return 1
    print("Verification re-scan found 0 markdown titles. Done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument(
        "--topic-id", default=None, help="limit to a single topicId (e.g. the verify topic)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually write changes (default is dry-run)"
    )
    parser.add_argument(
        "--force-large",
        action="store_true",
        help=f"bypass the {_SAFETY_LIMIT}-title safety limit (only with --apply)",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(args.brand_slug, args.topic_id, apply=args.apply, force_large=args.force_large)
    )


if __name__ == "__main__":
    sys.exit(main())
