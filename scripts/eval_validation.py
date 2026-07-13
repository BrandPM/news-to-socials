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

from pipeline.admin import db as admin_db
from pipeline.admin.judge import ESCALATION_MODEL, STREAM_MODEL, run_judge
from pipeline.admin.models import Brand
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


def _text_of(doc: dict) -> str:
    parts: list[str] = [str(doc.get("title") or "")]
    for block in doc.get("body") or []:
        for ch in (block or {}).get("children") or []:
            parts.append(str(ch.get("text") or ""))
    return "\n".join(p for p in parts if p).strip()


async def _query_class(client, cls: str, n: int, en_only: bool) -> list[Item]:
    """Pull one class from Sanity. 'approved' = published posts; 'rejected' =
    drafts.* flagged status=='rejected' (NTS_052 — the real rejection source;
    admin.db draft_approvals never stores rejections)."""
    if cls == "approved":
        base = '_type == "post" && !(_id in path("drafts.**"))'
    else:
        base = '_type == "post" && _id in path("drafts.**") && status == "rejected"'
    lang = ' && language == "en"' if en_only else ""
    groq = (
        f"*[{base}{lang} && defined(body)] | order(_createdAt desc) "
        f"[0...{n}]{{_id, title, language, \"body\": body[]{{children[]{{text}}}}}}"
    )
    rows = await client.query(groq)
    rows = rows if isinstance(rows, list) else []
    out: list[Item] = []
    for doc in rows:
        if not isinstance(doc, dict) or not doc.get("_id"):
            continue
        text = _text_of(doc)
        if not text:
            continue
        out.append(
            Item(
                sanity_draft_id=str(doc["_id"]),
                cls=cls,
                source_title=str(doc.get("title") or ""),
                text=text,
            )
        )
    return out


def _degrade(text: str) -> str:
    """Turn a good draft into a KNOWN-BAD one: keep only a short stub, strip
    all H2 structure, and inject a fabricated statistic + generic filler — the
    exact failure modes the rubric is meant to punish (factuality, specificity,
    structure, voice). Used to build a negative class when prod has no natural
    rejections."""
    words = text.replace("##", "").split()
    stub = " ".join(words[:110])
    return (
        stub
        + " In today's fast-paced world, it is important to note that, "
        "according to our internal analysis, 73% of clients unlock synergies "
        "and leverage best-in-class solutions to move the needle going forward. "
        "At the end of the day, this is a game-changer that will delight "
        "stakeholders across the board."
    )


async def _collect(client, brand_id: int, n: int) -> tuple[list[Item], str]:
    """Return (items, negative_class_name).

    Positive class = approved (published). Negative class = real rejected
    drafts (status=='rejected') if any exist; otherwise a synthetic 'degraded'
    class derived from the approved drafts (documented in the report), because
    prod has never recorded a rejection.
    """
    approved = await _query_class(client, "approved", n, en_only=True)
    print(f"  collected {len(approved)} approved (EN) drafts")
    rejected = await _query_class(client, "rejected", n, en_only=True)
    if len(rejected) < 3:
        widened = await _query_class(client, "rejected", n, en_only=False)
        if len(widened) > len(rejected):
            print(f"  (widened rejected to all languages: {len(widened)})")
        rejected = widened

    if rejected:
        print(f"  collected {len(rejected)} rejected drafts")
        return approved + rejected, "rejected"

    # No natural negatives → synthesise a degraded copy of each approved draft.
    degraded = [
        Item(sanity_draft_id=f"{it.sanity_draft_id}#degraded", cls="degraded",
             source_title=it.source_title, text=_degrade(it.text))
        for it in approved
    ]
    print(
        f"  NO rejected drafts exist in prod — built {len(degraded)} SYNTHETIC "
        "degraded negatives from the approved set"
    )
    return approved + degraded, "degraded"


async def _score_all(
    items: list[Item], model: str, brand_id: int, neg: str
) -> tuple[list[float], list[float], float]:
    pos_scores: list[float] = []
    neg_scores: list[float] = []
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
        (pos_scores if it.cls == "approved" else neg_scores).append(res.total)
        costs.append(res.cost_usd)
    return pos_scores, neg_scores, (statistics.mean(costs) if costs else 0.0)


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
        items, neg = await _collect(client, brand_id, args.n)
        if not items:
            print("No EN drafts resolved — nothing to score.")
            return 1
        print(f"\nNegative class: {neg!r}. Scoring {len(items)} drafts with: {', '.join(models)}\n")
        header = f"{'MODEL':26} {'approved':>9} {neg:>10} {'separation':>11} {'cost/draft':>11}"
        print(header)
        print("-" * len(header))
        for model in models:
            pos, negs, cost = await _score_all(items, model, brand_id, neg)
            p, q = _mean(pos), _mean(negs)
            sep = round(p - q, 2)
            verdict = "PASS" if sep >= 1.5 else "FAIL"
            print(
                f"{model:26} {p:>9} {q:>10} {sep:>11} {round(cost, 6):>11}  "
                f"[{verdict} sep>=1.5]"
            )
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
