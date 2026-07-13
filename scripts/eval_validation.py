"""Validation experiment for the LLM-judge (IT_PROJ_NTS_091 DoD).

Selects ~N approved + ~N rejected historical EN drafts from admin.db, fetches
their text from Sanity, and runs the judge OFFLINE on each with one or more
models. Reports the mean total per class and the separation (approved −
rejected). Success = separation ≥ 1.5. Also compares gpt-4o vs gpt-5.5 on the
same set to justify the default model, and prints measured cost per draft.

Read-only w.r.t. drafts; it does make real (paid) judge calls, so each writes
a ``cost_records`` row (operation ``draft_eval``, C1). Nothing is written to
``draft_scores`` — this is an offline experiment, not the live stream.

Usage (on the VPS, where admin.db + Sanity creds live):
    .venv/bin/python -m scripts.eval_validation --brand-slug icon --n 10 \
        --models gpt-4o,gpt-5.5-2026-04-23
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from dataclasses import dataclass

from sqlalchemy import select

from pipeline.admin import db as admin_db
from pipeline.admin.judge import ESCALATION_MODEL, STREAM_MODEL, run_judge
from pipeline.admin.models import Brand, DraftApproval, Topic
from scripts.backfill_slugs import _build_sanity_client


@dataclass
class Item:
    sanity_draft_id: str
    cls: str  # "approved" | "rejected"
    source_title: str
    text: str


def _brand_id(brand_slug: str) -> int:
    with admin_db.get_session_factory()() as s:
        brand = s.query(Brand).filter(Brand.slug == brand_slug).one_or_none()
        if brand is None:
            raise SystemExit(f"brand {brand_slug!r} not found")
        return brand.id


def _pick_drafts(brand_id: int, cls: str, n: int) -> list[tuple[str, str]]:
    """Return up to n (sanity_draft_id, source_title) for a decision class."""
    with admin_db.get_session_factory()() as s:
        rows = s.execute(
            select(DraftApproval.sanity_draft_id)
            .where(
                DraftApproval.brand_id_fk == brand_id,
                DraftApproval.status == cls,
            )
            .order_by(DraftApproval.decided_at.desc())
            .limit(n * 3)  # over-fetch; some won't be EN / won't resolve
        ).scalars().all()
        out: list[tuple[str, str]] = []
        for did in rows:
            title = s.execute(
                select(Topic.title).where(Topic.draft_id == did).limit(1)
            ).scalar_one_or_none()
            out.append((did, title or ""))
    return out


async def _fetch_en_text(client, sanity_draft_id: str) -> str | None:
    """Fetch the EN body for a draft id (tries drafts.* then published)."""
    published = sanity_draft_id[len("drafts.") :] if sanity_draft_id.startswith("drafts.") else sanity_draft_id
    groq = (
        "*[_id in [$d, $p]][0]{language, title, "
        '"body": body[]{children[]{text}}}'
    )
    doc = await client.query(groq, {"d": sanity_draft_id, "p": published})
    if not isinstance(doc, dict) or (doc.get("language") or "en") != "en":
        return None
    parts: list[str] = [str(doc.get("title") or "")]
    for block in doc.get("body") or []:
        for ch in (block or {}).get("children") or []:
            parts.append(str(ch.get("text") or ""))
    text = "\n".join(p for p in parts if p).strip()
    return text or None


async def _collect(client, brand_id: int, n: int) -> list[Item]:
    items: list[Item] = []
    for cls in ("approved", "rejected"):
        got = 0
        for did, src_title in _pick_drafts(brand_id, cls, n):
            if got >= n:
                break
            text = await _fetch_en_text(client, did)
            if not text:
                continue
            items.append(Item(sanity_draft_id=did, cls=cls, source_title=src_title, text=text))
            got += 1
        print(f"  collected {got} {cls} EN drafts")
    return items


async def _score_all(items: list[Item], model: str, brand_id: int) -> dict[str, list[float]]:
    by_cls: dict[str, list[float]] = {"approved": [], "rejected": []}
    costs: list[float] = []
    for it in items:
        try:
            res = await run_judge(
                draft_text=it.text,
                lang="en",
                source_text=it.source_title,
                model=model,
                brand_id_fk=brand_id,
                draft_id=it.sanity_draft_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    judge failed for {it.sanity_draft_id[:20]}: {exc}")
            continue
        by_cls[it.cls].append(res.total)
        costs.append(res.cost_usd)
    by_cls["_cost_per_draft"] = [statistics.mean(costs)] if costs else [0.0]  # type: ignore[assignment]
    return by_cls


def _mean(xs: list[float]) -> float:
    return round(statistics.mean(xs), 2) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brand-slug", default="icon")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--models", default=f"{STREAM_MODEL},{ESCALATION_MODEL}")
    args = ap.parse_args()

    brand_id = _brand_id(args.brand_slug)
    client = _build_sanity_client(args.brand_slug)
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    async def go() -> int:
        print("Collecting historical drafts...")
        items = await _collect(client, brand_id, args.n)
        if not items:
            print("No EN drafts resolved — nothing to score.")
            return 1
        print(f"\nScoring {len(items)} drafts with: {', '.join(models)}\n")
        print(f"{'MODEL':26} {'approved':>9} {'rejected':>9} {'separation':>11} {'cost/draft':>11}")
        print("-" * 70)
        results = {}
        for model in models:
            by = await _score_all(items, model, brand_id)
            appr, rej = _mean(by["approved"]), _mean(by["rejected"])
            sep = round(appr - rej, 2)
            cost = round(by["_cost_per_draft"][0], 6)
            results[model] = (appr, rej, sep, cost)
            verdict = "PASS" if sep >= 1.5 else "FAIL"
            print(f"{model:26} {appr:>9} {rej:>9} {sep:>11} {cost:>11}  [{verdict} sep>=1.5]")
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
