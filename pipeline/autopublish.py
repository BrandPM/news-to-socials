"""Publish a clean draft on its slot without a human (NTS_133 P5 Ф2.3).

From NTS_131 Ф4 through NTS_132 Ф4 to here. The rule is the directive's, and
it is deliberately all-or-nothing — a draft is published by the system only
when there is nothing for an editor to look at:

    ноль искажённых  И  ноль непокрытых  И  все 4 языка  И  600+ слов

Everything else waits in Ревью exactly as before. The inputs are the numbers
the production run already recorded for the candidate (``runs.stats``,
NTS_131 Ф5): the attribution counts, the languages written, the canon's word
count and ``needs_attention``. Nothing here makes a model call.

**Off by default** (``autopublish_enabled``, migration 041). Andriy switches
it on after reading a couple of issues; with it off the pass writes nothing,
not even a run row, because a timer that logs «disabled» four times a week is
noise in the one list that has to stay readable.

**On the slot, not on the draft.** The pass runs on weekday mornings and does
anything only on a day ``publication_slots`` names, up to that day's capacity
less what is already published today. A Tuesday draft goes out on Thursday's
slot, which is what a human approving it on Tuesday would have got too.

**Every decision goes into the journal.** A publish writes the ordinary
``review_decisions`` approve row with ``reviewer='system'`` (through the same
path an editor's batch approve takes), and the pass writes one ``runs`` row
(``run_type='publish'``) listing each candidate it considered and why it did
or did not publish it — so «why did this one go out and that one not» has an
answer in the Runs screen.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from pipeline.common.logging import configure_logging, get_logger
from pipeline.selector.candidate_store import resolve_timezone

log = get_logger(__name__)

SYSTEM_ACTOR = "system"
# The directive's floor for a full article. Below it the piece is a brief,
# and a brief is a format an editor chooses, not one the system ships.
AUTOPUBLISH_MIN_WORDS = 600
# «все 4 языка» — the brand's languages when it has them, these otherwise.
DEFAULT_LANGUAGES = ("en", "de", "ru", "uk")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Decision:
    """Publish or not, and the reasons — every reason, not the first."""

    candidate_id: int
    publish: bool
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "publish": self.publish,
            "reasons": list(self.reasons),
        }


def decide(
    entry: dict[str, Any] | None,
    *,
    candidate_id: int,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
    min_words: int = AUTOPUBLISH_MIN_WORDS,
) -> Decision:
    """The rule. Pure, so every branch is tested without a database.

    ``entry`` is what the production run recorded for the candidate. No entry
    means no evidence, and no evidence is a «no» — the system publishes what it
    can show is clean, not what it cannot show is dirty.
    """
    if not entry:
        return Decision(candidate_id, False, ("no production record",))
    reasons: list[str] = []
    counts = entry.get("attribution")
    if not isinstance(counts, dict):
        reasons.append("attribution not checked")
    else:
        distorted = int(counts.get("distorted", 0) or 0)
        uncovered = int(counts.get("uncovered", 0) or 0)
        if distorted:
            reasons.append(f"distorted: {distorted}")
        if uncovered:
            reasons.append(f"uncovered: {uncovered}")
    written = {str(lang).lower() for lang in entry.get("languages") or ()}
    missing = [lang for lang in languages if lang.lower() not in written]
    if missing:
        reasons.append("missing languages: " + ", ".join(missing))
    words = int(entry.get("words_en", 0) or 0)
    if words < min_words:
        reasons.append(f"words: {words} < {min_words}")
    if entry.get("needs_attention"):
        reasons.append("needs_attention")
    return Decision(candidate_id, not reasons, tuple(reasons))


def slot_capacity_today(slots: Any, today: date) -> int:
    """Today's capacity in ``publication_slots``, 0 on a day with no slot."""
    if isinstance(slots, str):
        try:
            slots = json.loads(slots)
        except (TypeError, ValueError):
            return 0
    day = _WEEKDAYS[today.weekday()]
    total = 0
    for slot in slots or ():
        if isinstance(slot, dict) and str(slot.get("day", "")).lower() == day:
            total += int(slot.get("capacity", 0) or 0)
    return total


def latest_production_entries(brand_id_fk: int) -> dict[int, dict[str, Any]]:
    """candidate id → the newest production record for it.

    Newest wins: a candidate returned and redrafted is judged on the draft
    that exists, not on the one the editor sent back.
    """
    from sqlalchemy import select

    from pipeline.admin.db import session_scope
    from pipeline.admin.models import Run

    out: dict[int, dict[str, Any]] = {}
    with session_scope() as session:
        rows = session.execute(
            select(Run.stats)
            .where(
                Run.brand_id_fk == brand_id_fk,
                Run.run_type == "production",
                Run.stats.is_not(None),
            )
            .order_by(Run.id.desc())
            .limit(60)
        ).scalars()
        for raw in rows:
            try:
                stats = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            for item in stats.get("candidates", []) or []:
                if not isinstance(item, dict) or item.get("status") != "drafted":
                    continue
                cid = item.get("candidate_id")
                if cid is not None and int(cid) not in out:
                    out[int(cid)] = item
    return out


@dataclass
class AutopublishResult:
    skipped: str | None = None
    capacity: int = 0
    decisions: list[Decision] = field(default_factory=list)
    published: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "capacity": self.capacity,
            "decisions": [d.as_dict() for d in self.decisions],
            "published": self.published,
            "failed": {str(k): v for k, v in self.failed.items()},
        }


async def _topic_of(brand_id: int, sanity_draft_id: str) -> str | None:
    """The Sanity ``topicId`` the language siblings share."""
    from pipeline.admin.routes.drafts import _build_sanity_client_for_brand

    client, _slug = _build_sanity_client_for_brand(brand_id)
    draft_id = (
        sanity_draft_id
        if sanity_draft_id.startswith("drafts.")
        else f"drafts.{sanity_draft_id}"
    )
    topic = await client.query(  # type: ignore[attr-defined]
        "*[_id == $id][0].topicId", {"id": draft_id}
    )
    return str(topic) if topic else None


async def _publish(brand_id: int, candidate_id: int, sanity_draft_id: str) -> None:
    """Publish every language sibling through the editor's batch path."""
    from pipeline.admin.routes.drafts import publish_topic_siblings

    topic_id = await _topic_of(brand_id, sanity_draft_id)
    if not topic_id:
        raise RuntimeError(f"draft {sanity_draft_id} has no topicId")
    result = await publish_topic_siblings(
        topic_id,
        brand_id,
        reviewer=SYSTEM_ACTOR,
        note="autopublish: 0 distorted, 0 uncovered, 4 languages, 600+ words",
    )
    if result.fail_count:
        raise RuntimeError(f"{result.fail_count} sibling(s) did not publish")


async def run_autopublish(
    brand_slug: str = "icon",
    *,
    now: datetime | None = None,
    publish: Any = None,
) -> AutopublishResult:
    """One pass for one brand. ``publish`` is injectable for tests."""
    from sqlalchemy import func, select

    from pipeline.admin.config_client import AdminConfigClient, get_brand
    from pipeline.admin.db import session_scope
    from pipeline.admin.models import Candidate

    configure_logging()
    now = now or datetime.now(tz=UTC)
    publish = publish or _publish
    result = AutopublishResult()

    brand = get_brand(brand_slug)
    client = AdminConfigClient(brand_slug=brand.slug)
    config = client.get_config()
    if not getattr(config, "autopublish_enabled", False):
        result.skipped = "autopublish_enabled is off"
        log.info("autopublish.disabled", brand=brand.slug)
        return result

    tz = resolve_timezone(getattr(config, "brand_timezone", None))
    today = now.astimezone(tz).date()
    capacity = slot_capacity_today(getattr(config, "publication_slots", ()), today)
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    with session_scope() as session:
        published_today = int(
            session.execute(
                select(func.count())
                .select_from(Candidate)
                .where(
                    Candidate.brand_id_fk == brand.id,
                    Candidate.status == "published",
                    Candidate.published_at >= day_start.replace(tzinfo=None),
                )
            ).scalar()
            or 0
        )
        waiting = [
            (int(cid), str(draft_id))
            for cid, draft_id in session.execute(
                select(Candidate.id, Candidate.sanity_draft_id)
                .where(
                    Candidate.brand_id_fk == brand.id,
                    Candidate.status == "drafted",
                    Candidate.sanity_draft_id.is_not(None),
                )
                .order_by(Candidate.drafted_at.asc(), Candidate.id.asc())
            ).all()
        ]
    result.capacity = max(0, capacity - published_today)
    if result.capacity == 0:
        result.skipped = "no slot today" if capacity == 0 else "today's slot is full"
        log.info("autopublish.no_capacity", brand=brand.slug, reason=result.skipped)
        return result

    from pipeline.run import _languages_for_brand

    # «все 4 языка» is Icon's four; the rule asks for every language the brand
    # publishes in, whatever that list says.
    languages = tuple(lang.value for lang in _languages_for_brand(brand))
    entries = latest_production_entries(brand.id)
    run_id = client.record_run_start(
        source_ids=[], triggered_by="cron", run_type="publish"
    )
    for candidate_id, draft_id in waiting:
        decision = decide(
            entries.get(candidate_id), candidate_id=candidate_id, languages=languages
        )
        result.decisions.append(decision)
        log.info("autopublish.decision", **decision.as_dict())
        if not decision.publish or len(result.published) >= result.capacity:
            continue
        try:
            await publish(brand.id, candidate_id, draft_id)
        except Exception as exc:  # one failed publish must not stop the pass
            result.failed[candidate_id] = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("autopublish.publish_failed", candidate_id=candidate_id)
            continue
        result.published.append(candidate_id)

    client.record_run_finish(
        run_id,
        status="failed" if result.failed else "success",
        stats=result.as_dict(),
        log_excerpt="\n".join(
            f"#{d.candidate_id}: "
            + ("published" if d.candidate_id in result.published else "held")
            + (f" — {'; '.join(d.reasons)}" if d.reasons else "")
            for d in result.decisions
        )[-4000:],
    )
    return result


def main(brand: str = "icon") -> None:
    outcome = asyncio.run(run_autopublish(brand))
    print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys

    main(*(sys.argv[1:2] or ["icon"]))
