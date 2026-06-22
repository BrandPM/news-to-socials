"""Add manager-feedback banned phrases + a bad style example to a brand's
voice profile (IT_PROJ_NTS_070).

Generation reads ``brand.voice_profile_yaml`` from admin.db. This script edits
it YAML-aware (load → mutate → dump; the profile has no comments, so the
round-trip is content-safe) and idempotently:

* appends the EN manager-feedback phrases to ``voice.en.banned_phrases``;
* appends one bad example to ``voice.en.style_examples.bad``;
* records a top-level ``nts070_pending_banned_translations`` marker listing the
  EN phrases that still need NATIVE RU/UK/PL equivalents — we deliberately do
  NOT inject the English phrases or machine calques into the non-EN banned
  lists (bad calques would be worse than nothing). Fill them by hand later.

Default is DRY RUN. ``--apply`` backs up admin.db first, then writes.

Usage (on the VPS):
    .venv/bin/python -m scripts.nts070_update_voice --brand-slug icon
    .venv/bin/python -m scripts.nts070_update_voice --brand-slug icon --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import yaml

from pipeline.admin import db as admin_db
from pipeline.admin.models import Brand
from pipeline.common.config import get_settings

NEW_EN_BANNED = [
    "growing uncertainty",
    "rising uncertainty",
    "significant impact",
    "immediate action required",
    "potential conflict",
    "each case is different",
    "it is important to",
    "plays a crucial role",
    "when it comes to",
]
BAD_EXAMPLE = (
    "Growing uncertainty creates significant challenges that require "
    "immediate action."
)
NON_EN_LANGS = ("ru", "uk", "pl")


def _ensure_list_has(container: dict, key: str, items: list[str]) -> int:
    cur = container.get(key)
    if not isinstance(cur, list):
        cur = []
    added = 0
    for it in items:
        if it not in cur:
            cur.append(it)
            added += 1
    container[key] = cur
    return added


def _mutate(data: dict) -> dict[str, int]:
    stats = {"banned_added": 0, "bad_added": 0}
    voice = data.get("voice")
    if not isinstance(voice, dict):
        raise SystemExit("voice_profile has no `voice:` map — unexpected structure")
    en = voice.get("en")
    if not isinstance(en, dict):
        raise SystemExit("voice.en missing — unexpected structure")

    stats["banned_added"] = _ensure_list_has(en, "banned_phrases", NEW_EN_BANNED)

    se = en.get("style_examples")
    if not isinstance(se, dict):
        se = {}
        en["style_examples"] = se
    stats["bad_added"] = _ensure_list_has(se, "bad", [BAD_EXAMPLE])

    # TODO marker — native equivalents pending for RU/UK/PL (do NOT calque).
    data["nts070_pending_banned_translations"] = {
        "note": "Fill native RU/UK/PL equivalents into voice.<lang>.banned_phrases.",
        "languages": list(NON_EN_LANGS),
        "en_phrases": NEW_EN_BANNED,
    }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument("--apply", action="store_true", help="write (default dry-run)")
    args = parser.parse_args()

    factory = admin_db.get_session_factory()
    with factory() as session:
        brand = session.query(Brand).filter(Brand.slug == args.brand_slug).one_or_none()
        if brand is None:
            raise SystemExit(f"brand {args.brand_slug!r} not found")
        original = brand.voice_profile_yaml or ""

    data = yaml.safe_load(original) or {}
    before_banned = len((data.get("voice", {}).get("en", {}) or {}).get("banned_phrases") or [])
    stats = _mutate(data)
    new_yaml = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    # Re-parse to confirm the generation path still reads it.
    from pipeline.common.models import Language  # noqa: PLC0415
    from pipeline.generator.comment_writer import (  # noqa: PLC0415
        parse_topics_relevant,
        parse_voice_guardrails,
        parse_voice_principles,
    )

    banned, good = parse_voice_guardrails(new_yaml, Language.en)
    principles = parse_voice_principles(new_yaml, Language.en)
    topics = parse_topics_relevant(new_yaml, Language.en)
    print(f"EN banned: {before_banned} -> {len(banned)} (+{stats['banned_added']} new)")
    print(f"bad examples added: {stats['bad_added']}")
    print(f"re-parse OK: principles={len(principles)} topics_relevant={len(topics)} good={len(good)}")
    missing = [p for p in NEW_EN_BANNED if p not in banned]
    print("all NEW_EN_BANNED present after parse:", not missing, missing or "")
    print(
        "\n⚠️ RU/UK/PL native equivalents NOT added (no calques). "
        "Fill voice.<ru|uk|pl>.banned_phrases by hand — see "
        "top-level nts070_pending_banned_translations marker."
    )

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to persist.")
        return 0

    src = Path(get_settings().admin_db_path).expanduser()
    backup = f"{src}.bak-{int(time.time())}"
    shutil.copy2(src, backup)
    for suffix in ("-wal", "-shm"):
        side = Path(f"{src}{suffix}")
        if side.exists():
            shutil.copy2(side, f"{backup}{suffix}")
    print(f"\nBacked up admin.db -> {backup}")

    with factory() as session:
        brand = session.query(Brand).filter(Brand.slug == args.brand_slug).one()
        brand.voice_profile_yaml = new_yaml
        session.commit()
    print("voice_profile_yaml updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
