"""Re-translate a topic's non-EN drafts from the canonical EN draft (NTS_065).

The S6 pipeline generated every language NATIVELY from the news peg, so the
RU/UK/PL versions of a topic drifted in structure and length and invented
facts — the canonical example is "Tax Advisory", whose RU body carried a
"67% of clients" stat the English never had.

This script fixes an already-published topic the way the reworked pipeline
now does it: it reads the canonical ENGLISH draft from Sanity and replaces
each non-EN sibling (RU/UK/PL) with a faithful TRANSLATION of it — same H2
set, same facts/numbers, comparable length — via
``CommentWriter.translate`` (gpt-4o), the SAME code path the live pipeline
uses, so backfill and pipeline can never diverge. The English document is
NEVER touched.

Every proposed translation is run through the fidelity gates in
``pipeline.generator.translation_check`` before it is allowed to be written:

* invented numbers (figures not in EN)  -> HARD gate: skip the write.
* wrong script for the language          -> HARD gate: skip the write.
* dropped numbers / H2-count / length    -> soft: warn but allow.

Default mode is **DRY RUN** — prints the EN source, the proposed translation,
and the gate report, and writes nothing. Pass ``--apply`` to patch Sanity. On
``--apply`` the script first copies ``admin.db`` to a timestamped ``.bak``
(HARD CONSTRAINT NTS_065) before any write; it never logs credentials.

Usage (run on the VPS, where .env + Sanity creds + the topic live):
    .venv/bin/python -m scripts.backfill_translate_topic --brand-slug icon \
        --topic-id 0f7c49edcb
    .venv/bin/python -m scripts.backfill_translate_topic --brand-slug icon \
        --match-title "Tax Advisory"
    # write it for real:
    .venv/bin/python -m scripts.backfill_translate_topic --brand-slug icon \
        --topic-id 0f7c49edcb --apply
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.admin import db as admin_db
from pipeline.admin.encryption import get_encryption
from pipeline.admin.models import Brand
from pipeline.common.config import get_settings
from pipeline.common.models import Draft, Language
from pipeline.generator import translation_check as tc
from pipeline.generator.comment_writer import CommentWriter
from pipeline.publisher.sanity import SanityClient, markdown_to_portable_text

# Languages we re-translate by default. EN is the canonical source and is
# excluded on principle — it is never an output of this script.
_DEFAULT_TARGETS = ("ru", "uk", "pl")


@dataclass
class BrandCtx:
    id_fk: int
    voice_profile_yaml: str
    client: SanityClient


@dataclass
class PlannedTranslation:
    doc_id: str  # Sanity _id of the non-EN sibling to patch
    language: str
    en_title: str
    new_title: str
    new_body_md: str
    new_key_takeaway: str
    # gate results
    invented: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    en_h2: int = 0
    tr_h2: int = 0
    length_ratio: float = 0.0
    script_ok: bool = True
    title_clean: bool = True

    @property
    def hard_ok(self) -> bool:
        """A write is allowed only if no invented numbers and the script
        matches the target language."""
        return not self.invented and self.script_ok

    @property
    def soft_warnings(self) -> list[str]:
        w: list[str] = []
        if self.dropped:
            w.append(f"dropped numbers {self.dropped}")
        if self.en_h2 != self.tr_h2:
            w.append(f"H2 count {self.tr_h2} != EN {self.en_h2}")
        if not 0.65 <= self.length_ratio <= 1.35:
            w.append(f"length ratio {self.length_ratio:.2f} outside ±35%")
        if not self.title_clean:
            w.append("title still has markdown")
        return w


def _build_brand_ctx(brand_slug: str) -> BrandCtx:
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
        return BrandCtx(
            id_fk=brand.id,
            voice_profile_yaml=brand.voice_profile_yaml or "",
            client=client,
        )


def _portable_text_to_markdown(body: object) -> str:
    """Reduce Sanity Portable Text back to the markdown the writer emits.

    Same reduction as ``pipeline.admin.text_regenerate`` — H2/H3 prefixes,
    paragraphs separated by blank lines. Exotic blocks fall through as plain
    text, which is fine: we only need a faithful textual source to translate.
    """
    parts: list[str] = []
    if isinstance(body, list):
        for block in body:
            if not isinstance(block, dict) or block.get("_type") != "block":
                continue
            style = block.get("style", "normal")
            text = "".join(
                c.get("text", "")
                for c in block.get("children", [])
                if isinstance(c, dict)
            )
            if style == "h2":
                parts.append(f"## {text}")
            elif style == "h3":
                parts.append(f"### {text}")
            else:
                parts.append(text)
    elif isinstance(body, str):
        parts.append(body)
    return "\n\n".join(p for p in parts if p)


async def _resolve_topic_id(client: SanityClient, match_title: str) -> str | None:
    """Find a topicId by case-insensitive title substring (any language)."""
    groq = (
        '*[_type == "post" && lower(title) match $needle]'
        "{topicId, title, language} | order(language asc)"
    )
    rows = await client.query(groq, {"needle": f"*{match_title.lower()}*"})
    if not isinstance(rows, list) or not rows:
        return None
    tids = {r.get("topicId") for r in rows if isinstance(r, dict) and r.get("topicId")}
    print(f"Title match {match_title!r} → {len(rows)} docs, topicIds: {sorted(tids)}")
    for r in rows:
        if isinstance(r, dict):
            print(f"    [{(r.get('language') or '??'):2}] {r.get('title')!r}")
    if len(tids) != 1:
        print(
            "Refusing to guess: title matched 0 or >1 topicId. Re-run with "
            "--topic-id <id>.",
            file=sys.stderr,
        )
        return None
    return str(next(iter(tids)))


async def _load_en_canonical(
    client: SanityClient, topic_id: str
) -> tuple[Draft, str]:
    """Return (EN draft built from Sanity, en_doc_id). Prefers the PUBLISHED
    EN doc over its draft when both exist (the live canonical)."""
    groq = (
        '*[_type == "post" && topicId == $tid && language == "en"]'
        "{_id, title, body, keyTakeaway}"
    )
    rows = await client.query(groq, {"tid": topic_id})
    if not isinstance(rows, list) or not rows:
        raise SystemExit(
            f"no EN document found for topicId {topic_id!r} — cannot translate "
            "without the canonical English source."
        )
    # Prefer the published id (no 'drafts.' prefix) as the canonical.
    rows.sort(key=lambda r: str(r.get("_id", "")).startswith("drafts."))
    en = rows[0]
    body_md = _portable_text_to_markdown(en.get("body"))
    draft = Draft(
        topic_id=topic_id,
        brand_id="icon",
        language=Language.en,
        title=en.get("title") or "",
        body=body_md,
        key_takeaway=en.get("keyTakeaway") or "",
    )
    return draft, str(en["_id"])


async def _find_non_en_docs(
    client: SanityClient, topic_id: str, targets: tuple[str, ...]
) -> list[dict]:
    groq = (
        '*[_type == "post" && topicId == $tid && language in $langs]'
        "{_id, title, language}"
    )
    rows = await client.query(groq, {"tid": topic_id, "langs": list(targets)})
    return [r for r in rows if isinstance(r, dict) and r.get("_id")] if isinstance(
        rows, list
    ) else []


async def _plan_for_doc(
    writer: CommentWriter,
    voice_yaml: str,
    en_draft: Draft,
    doc: dict,
    cache: dict[str, Draft],
) -> PlannedTranslation:
    lang_code = str(doc.get("language"))
    language = Language(lang_code)
    if lang_code not in cache:
        cache[lang_code] = await writer.translate(en_draft, language, voice_yaml)
    translated = cache[lang_code]

    script_ok = (
        tc.is_mostly_cyrillic(translated.body)
        if lang_code in ("ru", "uk")
        else tc.is_polish_latin(translated.body)
        if lang_code == "pl"
        else True
    )
    return PlannedTranslation(
        doc_id=str(doc["_id"]),
        language=lang_code,
        en_title=en_draft.title,
        new_title=translated.title,
        new_body_md=translated.body,
        new_key_takeaway=translated.key_takeaway,
        invented=tc.invented_numbers(en_draft.body, translated.body),
        dropped=tc.dropped_numbers(en_draft.body, translated.body),
        en_h2=tc.h2_count(en_draft.body),
        tr_h2=tc.h2_count(translated.body),
        length_ratio=tc.length_ratio(en_draft.body, translated.body),
        script_ok=script_ok,
        title_clean=not tc.has_markdown_in_title(translated.title),
    )


def _print_plan(en_draft: Draft, plans: list[PlannedTranslation]) -> None:
    print("\n" + "=" * 72)
    print(f"EN canonical (topic {en_draft.topic_id}) — UNTOUCHED")
    print(f"  title: {en_draft.title!r}")
    print(f"  H2: {tc.h2_count(en_draft.body)}  len: {len(en_draft.body)} chars")
    print(f"  numbers: {sorted(tc.extract_number_cores(en_draft.body).elements())}")
    print("=" * 72)
    for p in plans:
        verdict = "OK" if p.hard_ok else "BLOCKED"
        print(f"\n[{p.language.upper()}] {p.doc_id}   gate: {verdict}")
        print(f"  new title: {p.new_title!r}")
        print(f"  H2: {p.tr_h2} (EN {p.en_h2})   length ratio: {p.length_ratio:.2f}")
        if p.invented:
            print(f"  HARD FAIL invented numbers (not in EN): {p.invented}")
        if not p.script_ok:
            print("  HARD FAIL wrong script for language")
        for w in p.soft_warnings:
            print(f"  warn: {w}")


def _backup_admin_db() -> str:
    src = Path(get_settings().admin_db_path).expanduser()
    stamp = int(time.time())
    dst = f"{src}.bak-{stamp}"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(f"{src}{suffix}")
        if side.exists():
            shutil.copy2(side, f"{dst}{suffix}")
    return dst


async def _apply(client: SanityClient, plans: list[PlannedTranslation]) -> tuple[int, int]:
    patched = skipped = 0
    for p in plans:
        if not p.hard_ok:
            print(f"  SKIP [{p.language}] {p.doc_id} — failed a hard gate.")
            skipped += 1
            continue
        try:
            await client.patch(
                p.doc_id,
                {
                    "title": p.new_title,
                    "body": markdown_to_portable_text(p.new_body_md),
                    "keyTakeaway": p.new_key_takeaway[:280],
                },
            )
            patched += 1
            print(f"  patched [{p.language}] {p.doc_id}")
        except Exception as exc:  # noqa: BLE001
            print(
                f"  FAIL [{p.language}] {p.doc_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            skipped += 1
    return patched, skipped


async def _run(
    brand_slug: str,
    topic_id: str | None,
    match_title: str | None,
    targets: tuple[str, ...],
    apply: bool,
) -> int:
    if "en" in targets:
        raise SystemExit("refusing to translate into EN — it is the canonical source.")
    ctx = _build_brand_ctx(brand_slug)

    if topic_id is None:
        if not match_title:
            raise SystemExit("provide either --topic-id or --match-title.")
        topic_id = await _resolve_topic_id(ctx.client, match_title)
        if topic_id is None:
            return 2

    en_draft, en_doc_id = await _load_en_canonical(ctx.client, topic_id)
    print(f"EN canonical doc: {en_doc_id} (never patched)")

    docs = await _find_non_en_docs(ctx.client, topic_id, targets)
    if not docs:
        print(f"No non-EN docs ({targets}) found for topicId {topic_id!r}.")
        return 0

    # Record translation cost against the brand (mirrors the pipeline).
    from pipeline.admin.cost_recorder import CostContext, cost_context  # noqa: PLC0415

    writer = CommentWriter()
    cache: dict[str, Draft] = {}
    plans: list[PlannedTranslation] = []
    with cost_context(CostContext(brand_id_fk=ctx.id_fk)):
        for doc in docs:
            plans.append(
                await _plan_for_doc(writer, ctx.voice_profile_yaml, en_draft, doc, cache)
            )

    _print_plan(en_draft, plans)

    blocked = [p for p in plans if not p.hard_ok]
    if not apply:
        print(
            f"\nDRY RUN — {len(plans)} doc(s) planned, {len(blocked)} blocked by a "
            "hard gate. EN is untouched. Re-run with --apply to write."
        )
        return 0

    backup = _backup_admin_db()
    print(f"\nBacked up admin.db -> {backup}")
    print("Patching non-EN Sanity docs (EN left untouched)...")
    patched, skipped = await _apply(ctx.client, plans)
    print(f"\nDone: patched {patched}, skipped {skipped}.")
    return 1 if skipped else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand-slug", default="icon")
    parser.add_argument("--topic-id", default=None, help="topicId to re-translate")
    parser.add_argument(
        "--match-title",
        default=None,
        help="resolve topicId by title substring (e.g. 'Tax Advisory')",
    )
    parser.add_argument(
        "--languages",
        default=",".join(_DEFAULT_TARGETS),
        help="comma-separated non-EN target codes (default ru,uk,pl)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write changes (default is dry-run)"
    )
    args = parser.parse_args()
    targets = tuple(c.strip() for c in args.languages.split(",") if c.strip())
    return asyncio.run(
        _run(
            args.brand_slug,
            args.topic_id,
            args.match_title,
            targets,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
