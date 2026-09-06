"""End-to-end walkthrough of the v3 pipeline, stage by stage, on a local copy.

    python -m scripts.e2e_walkthrough                 # temp fixture DB
    python -m scripts.e2e_walkthrough --from-dump admin.db.bak-...
    python -m scripts.e2e_walkthrough --markdown      # the report table

**Nothing here talks to the internet and nothing here costs a cent.** The very
first thing the script does is replace ``httpx``'s request methods with one
that raises, so a paid call is not "avoided by convention" — it is impossible,
and any code path that tried would fail loudly instead of quietly billing.
OpenAI, Firecrawl, Replicate and Sanity are replaced by fakes returning
realistically shaped payloads with plausible token counts, which is what makes
the cost column meaningful: the numbers come from the real accounting code
(``cost_recorder`` → ``cost_records`` → ``pricing``) applied to fake usage.

The point is not "does it work" — the intake demonstrably works on production.
The point is **where the chain stops**, in the order the chain runs, with a
snapshot after each stage. Stages that do not exist yet are printed as
``NOT IMPLEMENTED (Sn)`` with the session that owns them. They are never
simulated: a stage that fakes success is worse than a missing stage, because it
removes the only signal that the work is outstanding.

Written for IT_PROJ_NTS_122; the accompanying report is
``docs/specs/03_session_logs/IT_PROJ_NTS_122_e2e_walkthrough_20260828.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

# --------------------------------------------------------------------------
# 0. Block the network before anything can import a client
# --------------------------------------------------------------------------


class NetworkBlockedError(RuntimeError):
    """A stage tried to reach the network. That is a bug in this script."""


def _block_network() -> None:
    """Make every outbound HTTP call raise.

    Done by patching ``httpx`` rather than by unsetting API keys, because an
    unset key produces a *skip* ("research disabled, no api key") and a skip
    reported as a pass is exactly the lie this walkthrough exists to catch.
    """
    import httpx

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise NetworkBlockedError(
            "outbound HTTP is blocked in the walkthrough — this call should "
            "have been served by a fake"
        )

    for attr in ("request", "send", "get", "post", "patch", "put", "delete"):
        if hasattr(httpx.Client, attr):
            setattr(httpx.Client, attr, _blocked)
        if hasattr(httpx.AsyncClient, attr):
            setattr(httpx.AsyncClient, attr, _blocked)


_block_network()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------
# 1. The stage ledger
# --------------------------------------------------------------------------

OK = "ok"
GAP = "gap"
NOT_IMPLEMENTED = "not_implemented"


@dataclass
class Stage:
    name: str
    inputs: str = ""
    outputs: str = ""
    db_writes: str = ""
    status: str = OK
    owner: str = ""
    note: str = ""
    cost_usd: float = 0.0

    @property
    def badge(self) -> str:
        return {
            OK: "ok",
            GAP: "gap",
            NOT_IMPLEMENTED: "NOT IMPLEMENTED",
        }[self.status]


@dataclass
class Ledger:
    stages: list[Stage] = field(default_factory=list)
    # Running total of ``cost_records`` at the previous stage boundary. All
    # spend between two ``add`` calls belongs to the stage being added, so the
    # per-stage cost needs no bookkeeping at the call sites — and it comes from
    # the real accounting table rather than from a second estimate that could
    # disagree with it.
    _spend_watermark: float = 0.0

    def add(self, stage: Stage) -> Stage:
        total = total_spend()
        stage.cost_usd = round(total - self._spend_watermark, 6)
        self._spend_watermark = total
        self.stages.append(stage)
        marker = {OK: "  ok  ", GAP: " gap  ", NOT_IMPLEMENTED: " ---- "}[
            stage.status
        ]
        owner = f" [{stage.owner}]" if stage.owner else ""
        print(f"[{marker}] {len(self.stages):>2}. {stage.name}{owner}")
        if stage.inputs:
            print(f"           in : {stage.inputs}")
        if stage.outputs:
            print(f"           out: {stage.outputs}")
        if stage.db_writes:
            print(f"           db : {stage.db_writes}")
        if stage.note:
            print(f"           →   {stage.note}")
        return stage


# --------------------------------------------------------------------------
# 2. Fakes
# --------------------------------------------------------------------------


def total_spend() -> float:
    """Everything ``cost_records`` holds, right now.

    Reads the table rather than summing what the fakes claimed: if the
    accounting code drops a row, this walkthrough should report the drop, not
    paper over it with a parallel tally.
    """
    try:
        from sqlalchemy import func, select

        from pipeline.admin import db as admin_db
        from pipeline.admin.models import CostRecord

        with admin_db.get_session_factory()() as session:
            return float(
                session.execute(
                    select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0))
                ).scalar()
                or 0.0
            )
    except Exception:
        return 0.0


def _usage(tokens_in: int, tokens_out: int) -> Any:
    return type(
        "U",
        (),
        {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
        },
    )()


def _completion(text: str, tokens_in: int, tokens_out: int) -> Any:
    message = type("M", (), {"content": text})()
    choice = type("C", (), {"message": message})()
    return type(
        "R", (), {"choices": [choice], "usage": _usage(tokens_in, tokens_out)}
    )()


_GUARD_ACCEPT = {
    "verdict": "accept",
    "reason_code": "ok",
    "reason": (
        "ESMA opens a consultation on CASP authorisation timelines — direct "
        "consequence for licence applicants in EU jurisdictions we serve."
    ),
    "service_category": "structuring",
    "jurisdictions": ["EU"],
    "event_stage": "consultation",
    "depth_prior": "article",
    "primary_doc_hint": "ESMA consultation paper ESMA35-1234",
    "doc_language_expected": "en",
    "confidence": 0.82,
}

_DRAFT_JSON = {
    "title": "ESMA tightens the clock on CASP authorisation",
    "body": (
        "## What changed\n\n"
        "ESMA opened a consultation on the authorisation timeline for "
        "crypto-asset service providers, proposing a 40-working-day "
        "completeness assessment.\n\n"
        "## Why it matters\n\n"
        "Applicants who file an incomplete dossier lose the clock and restart "
        "it, which in practice adds a quarter to a licence project.\n\n"
        "## What to do\n\n"
        "Front-load the governance and outsourcing annexes before filing."
    ),
    "key_takeaway": (
        "An incomplete CASP dossier now costs a quarter, not a fortnight."
    ),
}

_TRANSLATED_JSON = {
    "title": "ESMA ужесточает сроки авторизации CASP",
    "body": (
        "## Что изменилось\n\n"
        "ESMA открыла консультацию по срокам авторизации поставщиков услуг "
        "по криптоактивам: 40 рабочих дней на проверку комплектности.\n\n"
        "## Почему это важно\n\n"
        "Неполный комплект обнуляет отсчёт, и лицензионный проект удлиняется "
        "на квартал.\n\n"
        "## Что делать\n\n"
        "Готовить приложения по управлению и аутсорсингу до подачи."
    ),
    "key_takeaway": (
        "Неполный комплект по CASP теперь стоит квартала, "
        "а не двух недель."  # noqa: RUF001
    ),
}

_RESEARCH_JSON = {
    "source_facts": [
        {
            "text": "ESMA proposes a 40-working-day completeness assessment",
            "url": "https://www.esma.europa.eu/press-news/consultation-casp",
            "publisher": "ESMA",
            "date": "2026-08-27",
        },
        {
            "text": "Consultation closes 2026-11-14",
            "url": "https://www.esma.europa.eu/press-news/consultation-casp",
            "publisher": "ESMA",
            "date": "2026-08-27",
        },
    ],
    "context": [
        {
            "text": "17 CASP licences granted across the EU in H1 2026",
            "url": "https://www.reuters.com/markets/eu-casp-licences",
            "publisher": "Reuters",
            "date": "2026-07-02",
        }
    ],
    "angle_hints": [
        "The clock reset, not the deadline, is the expensive part for "
        "applicants"
    ],
}


class FakeCompletions:
    """``chat.completions.create`` for the guard, the writer and the judge.

    Dispatches on the prompt text because that is what the real API does with
    it — a single fake that answered every prompt identically would let a
    prompt-routing bug pass.
    """

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def create(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages") or []
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        lowered = prompt.lower()
        schema = (kwargs.get("response_format") or {}).get("json_schema") or {}
        schema_name = schema.get("name", "")

        if schema_name == "editorial_verdict":
            self._calls.append("guard")
            return _completion(json.dumps(_GUARD_ACCEPT), 1180, 140)
        # Matched on the prompts' own opening lines rather than on a loose
        # keyword: "editor" and "translate" both appear in more than one of
        # them, and a fake that answers the wrong prompt hides a routing bug.
        if "you are a faithful translator" in lowered:
            self._calls.append("translate")
            return _completion(json.dumps(_TRANSLATED_JSON), 2400, 900)
        if "absolute fidelity rules" in lowered:
            self._calls.append("translate")
            return _completion(json.dumps(_TRANSLATED_JSON), 2400, 900)
        if "polish" in lowered:
            self._calls.append("polish")
            return _completion(json.dumps(_DRAFT_JSON), 2600, 950)
        self._calls.append("draft")
        return _completion(json.dumps(_DRAFT_JSON), 2100, 900)


class FakeResponses:
    """``responses.create`` — the research call with the web-search tool."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def create(self, **kwargs: Any) -> Any:
        self._calls.append("research")
        search_calls = [
            type("S", (), {"type": "web_search_call"})() for _ in range(3)
        ]
        message = type("M", (), {"type": "message"})()
        return type(
            "R",
            (),
            {
                "output_text": json.dumps(_RESEARCH_JSON),
                "output": [*search_calls, message],
                "usage": _usage(9400, 1300),
            },
        )()


class FakeOpenAI:
    """Stands in for ``openai.AsyncOpenAI`` everywhere at once.

    ``calls`` is deliberately class-level: every module under test constructs
    its own client, and the walkthrough needs one record of which prompts were
    actually issued in order to assert at the end that the guard ran at all.
    """

    calls: ClassVar[list[str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.chat = type(
            "Chat", (), {"completions": FakeCompletions(FakeOpenAI.calls)}
        )()
        self.responses = FakeResponses(FakeOpenAI.calls)
        self.embeddings = self


def fake_embed(text: str, *, model: str = "text-embedding-3-small") -> Any:
    """Deterministic unit vector, plus the cost row the real embedder writes.

    Deterministic so the dedup stage is reproducible: the same title always
    produces the same vector, and a near-duplicate title produces a near
    vector, which is what the three windows are actually asked to spot.
    """
    import hashlib

    import numpy as np

    from pipeline.admin.cost_recorder import record_cost
    from pipeline.common.pricing import openai_cost

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
    base = rng.normal(size=1536)
    # Fold the normalised words in so similar titles land near each other.
    for word in sorted({w for w in text.lower().split() if len(w) > 3}):
        wdig = hashlib.sha256(word.encode("utf-8")).digest()
        wrng = np.random.default_rng(int.from_bytes(wdig[:8], "big"))
        base += wrng.normal(size=1536) * 3.0
    vec = (base / np.linalg.norm(base)).astype(np.float32)
    tokens = max(1, len(text) // 4)
    record_cost(
        provider="openai",
        operation="embedding",
        model=model,
        tokens_in=tokens,
        cost_usd=openai_cost(model, tokens, 0),
    )
    return vec


class FakeSanityPublisher:
    """``publish_draft`` / ``promote_draft_to_published`` without Sanity."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.published: list[str] = []

    async def publish_draft(self, post: Any) -> str:
        lang = getattr(post.language, "value", str(post.language))
        return f"drafts.e2e-{post.topic_id[:8]}-{lang}"

    async def promote_draft_to_published(
        self, draft_id: str, *, published_at: Any = None
    ) -> str:
        promoted = draft_id.replace("drafts.", "")
        self.published.append(promoted)
        return promoted

    async def is_topic_already_posted(self, topic_id: str, language: Any) -> bool:
        return False


# --------------------------------------------------------------------------
# 3. The fixture database
# --------------------------------------------------------------------------

_FEED_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "title": (
            "ESMA consults on authorisation timelines for crypto-asset "
            "service providers"
        ),
        "summary": (
            "The European Securities and Markets Authority has opened a "
            "consultation on the completeness assessment period applied to "
            "CASP authorisation applications under MiCA, proposing a "
            "40-working-day window."
        ),
        "url": "https://www.esma.europa.eu/press-news/consultation-casp",
        "age_hours": 6,
    },
    {
        # Near-duplicate of the first: the dedup stage has to catch it, and a
        # walkthrough where every item is unique never exercises dedup at all.
        "title": (
            "ESMA consults on authorisation timelines for crypto asset "
            "service providers"
        ),
        "summary": (
            "ESMA opened a consultation on the completeness assessment period "
            "for CASP authorisation applications under MiCA, proposing a "
            "40 working day window."
        ),
        "url": "https://www.esma.europa.eu/press-news/consultation-casp-2",
        "age_hours": 5,
    },
    {
        # Prefilter bait: a personnel story on a news feed.
        "title": "Boutique firm appoints new head of private clients",
        "summary": (
            "The firm said the appointment strengthens its private client "
            "offering across the region and follows a period of growth."
        ),
        "url": "https://example.com/appoints-head",
        "age_hours": 12,
    },
)


def build_fixture_db(target: Path, *, from_dump: Path | None) -> None:
    """A schema-current admin.db with one brand, one config, two feeds.

    Built with the real migration stack rather than ``create_all``: a
    walkthrough that runs against a schema no deploy ever produced proves
    nothing about the deploy.
    """
    import subprocess

    if from_dump is not None:
        shutil.copy2(from_dump, target)
        print(f"      fixture: copied {from_dump} → {target}")
    env = {**os.environ, "ADMIN_DB_PATH": str(target)}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    print(f"      fixture: migrated to head at {target}")


def seed_fixture(now: datetime) -> tuple[int, list[Any]]:
    """Seed brand + config + rubric + taxonomy + two sources. Returns ids."""
    import json as _json

    from pipeline.admin import db as admin_db
    from pipeline.admin.encryption import get_encryption
    from pipeline.admin.models import (
        Brand,
        BrandTaxonomy,
        PipelineConfig,
        Prompt,
        Source,
    )
    from pipeline.selector.editorial_guard import _GUARD_PROMPT

    with admin_db.get_session_factory()() as session:
        brand = session.query(Brand).filter(Brand.slug == "icon").one_or_none()
        if brand is None:
            brand = Brand(slug="icon", name="Icon Finance")
            session.add(brand)
        brand.status = "active"
        brand.active = True
        brand.language = "en"
        brand.timezone = "Europe/Madrid"
        brand.languages = _json.dumps(["en", "ru"])
        brand.sanity_project_id = "e2e-project"
        brand.sanity_dataset = "production"
        brand.sanity_api_version = "2024-01-01"
        brand.sanity_api_token_enc = get_encryption().encrypt("e2e-token")
        brand.voice_profile_yaml = "mission: clarity for cross-border wealth\n"
        session.flush()
        brand_id = int(brand.id)

        config = session.get(PipelineConfig, brand_id)
        if config is None:
            config = PipelineConfig(
                brand_id_fk=brand_id,
                scoring_threshold=7,
                topics_per_run=3,
                banned_phrases="[]",
                voice_profile="mission: clarity\n",
            )
            session.add(config)
        config.intake_enabled = True
        # Left OFF on purpose: the walkthrough drives the generation stages
        # directly, and flipping this in a fixture would misreport the state
        # the production service is actually in.
        config.v2_generation_enabled = False
        config.publication_slots = _json.dumps(
            [{"day": "mon", "capacity": 2}, {"day": "thu", "capacity": 2}]
        )
        config.brand_timezone = "Europe/Madrid"
        config.research_enabled = True
        session.flush()

        if not session.query(BrandTaxonomy).filter(
            BrandTaxonomy.brand_id_fk == brand_id
        ).count():
            for key, label, desc, path in (
                (
                    "structuring",
                    "Structuring & Tax",
                    "Residence and citizenship, CRS/DAC/CARF reporting, UBO",
                    "/services/structuring",
                ),
                (
                    "wealth",
                    "Wealth Management",
                    "Private banking access, trustee and fund regulation",
                    "/services/wealth",
                ),
            ):
                session.add(
                    BrandTaxonomy(
                        brand_id_fk=brand_id,
                        key=key,
                        label=label,
                        description_for_guard=desc,
                        service_url_path=path,
                    )
                )

        if not session.query(Prompt).filter(
            Prompt.brand_id_fk == brand_id,
            Prompt.prompt_type == "editorial_guard",
            Prompt.is_active.is_(True),
        ).count():
            session.add(
                Prompt(
                    brand_id_fk=brand_id,
                    prompt_type="editorial_guard",
                    version_name="e2e-rubric",
                    content=_GUARD_PROMPT,
                    is_active=True,
                    created_by="e2e_walkthrough",
                )
            )

        sources: list[Source] = []
        for name, url, role, klass, license_class in (
            (
                "E2E ESMA (primary)",
                "https://example.invalid/esma.rss",
                "primary_feed",
                "regulator",
                "public_official",
            ),
            (
                "E2E trade press (news)",
                "https://example.invalid/news.rss",
                "news",
                "news",
                "news_paywalled",
            ),
        ):
            existing = (
                session.query(Source)
                .filter(Source.brand_id_fk == brand_id, Source.url == url)
                .one_or_none()
            )
            if existing is None:
                existing = Source(
                    brand_id_fk=brand_id,
                    name=name,
                    source_type="rss",
                    url=url,
                    primary_category="structuring",
                    active=True,
                )
                session.add(existing)
            existing.source_role = role
            existing.source_class = klass
            existing.license_class = license_class
            existing.doc_language = "en"
            existing.fetch_method = "rss"
            sources.append(existing)
        session.commit()
        return brand_id, [
            {
                "id": int(s.id),
                "name": s.name,
                "role": s.source_role,
                "class": s.source_class,
                "license": s.license_class,
            }
            for s in sources
        ]


def raw_items(source: dict[str, Any], now: datetime) -> list[Any]:
    """The fake feed, as ``RawItem``s — what a real fetch would have returned."""
    from pipeline.common.models import RawItem

    items = _FEED_ITEMS if source["role"] == "primary_feed" else _FEED_ITEMS[2:]
    return [
        RawItem(
            source_id=str(source["id"]),
            source_name=source["name"],
            url=item["url"],
            title=item["title"],
            summary=item["summary"],
            published_at=now - timedelta(hours=item["age_hours"]),
        )
        for item in items
    ]


# --------------------------------------------------------------------------
# 4. Helpers for the snapshots
# --------------------------------------------------------------------------


def table_counts() -> dict[str, int]:
    from sqlalchemy import func, select

    from pipeline.admin import db as admin_db
    from pipeline.admin.models import (
        Candidate,
        CostRecord,
        DraftApproval,
        FactPack,
        ReviewDecision,
        Run,
        SourceHealthRecord,
        TopicEmbedding,
    )

    models = {
        "candidates": Candidate,
        "cost_records": CostRecord,
        "draft_approvals": DraftApproval,
        "fact_packs": FactPack,
        "review_decisions": ReviewDecision,
        "runs": Run,
        "source_health_records": SourceHealthRecord,
        "topic_embeddings": TopicEmbedding,
    }
    with admin_db.get_session_factory()() as session:
        return {
            name: int(
                session.execute(select(func.count(model.id))).scalar() or 0
            )
            for name, model in models.items()
        }


def diff_counts(before: dict[str, int], after: dict[str, int]) -> str:
    parts = [
        f"{name} +{after[name] - before[name]}"
        for name in sorted(after)
        if after[name] > before.get(name, 0)
    ]
    return ", ".join(parts) or "no writes"


def spend_by_operation() -> dict[str, float]:
    from sqlalchemy import func, select

    from pipeline.admin import db as admin_db
    from pipeline.admin.models import CostRecord

    with admin_db.get_session_factory()() as session:
        rows = session.execute(
            select(CostRecord.operation, func.sum(CostRecord.cost_usd)).group_by(
                CostRecord.operation
            )
        ).all()
    return {str(r[0]): round(float(r[1] or 0.0), 6) for r in rows}


# --------------------------------------------------------------------------
# 5. The walkthrough
# --------------------------------------------------------------------------


async def walkthrough(ledger: Ledger, *, now: datetime) -> dict[str, Any]:
    import numpy as np

    from pipeline.admin import db as admin_db
    from pipeline.admin.config_client import AdminConfigClient
    from pipeline.admin.cost_recorder import (
        CostContext,
        attach_candidate,
        collect_cost_rows,
        cost_context,
    )
    from pipeline.common.models import Language, Topic
    from pipeline.production import batch_key
    from pipeline.selector import candidate_lifecycle as lifecycle
    from pipeline.selector.candidate_lifecycle import begin_production
    from pipeline.selector.candidate_dedup import (
        CandidateDedupConfig,
        check_post_guard,
        check_pre_guard,
    )
    from pipeline.selector.candidate_store import (
        CandidateInput,
        claim_pending,
        create_candidate,
        recent_accepted_titles,
    )
    from pipeline.selector.dedup import jaccard, normalize_title
    from pipeline.selector.editorial_guard import (
        judge_item,
        load_brand_taxonomy,
        render_jurisdiction_tiers,
        render_services,
        resolve_guard_template,
    )
    from pipeline.selector.prefilter import PrefilterRules, prefilter_item

    brand_id, sources = seed_fixture(now)
    client = AdminConfigClient(brand_slug="icon")
    config = client.get_config()
    primary = sources[0]
    news = sources[1]

    run_id = client.record_run_start(
        source_ids=[s["id"] for s in sources],
        triggered_by="e2e_walkthrough",
        run_type="intake",
    )
    outcome: dict[str, Any] = {"brand_id": brand_id, "run_id": run_id}

    with cost_context(CostContext(brand_id_fk=brand_id, run_id=run_id)):
        # ---- 1. fetch -------------------------------------------------
        before = table_counts()
        primary_items = raw_items(primary, now)
        news_items = raw_items(news, now)
        client.record_source_health(
            source_id=primary["id"],
            brand_id_fk=brand_id,
            success=True,
            articles_count=len(primary_items),
        )
        client.record_source_health(
            source_id=news["id"],
            brand_id_fk=brand_id,
            success=True,
            articles_count=len(news_items),
        )
        ledger.add(
            Stage(
                name="fetch",
                inputs=(
                    f"2 active sources (1 primary_feed, 1 news), "
                    f"limit={50}"
                ),
                outputs=(
                    f"{len(primary_items)} items from primary_feed, "
                    f"{len(news_items)} from news"
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "feedparser is stubbed; the health row records "
                    "`success=bool(items)` exactly as production does — an "
                    "empty feed is a failed fetch (NTS_106 §1)"
                ),
            )
        )

        # ---- 2. dedup -------------------------------------------------
        # Every paid row for THIS item is collected across stages 2, 4 and 5
        # and charged to the candidate at 5 — the same ordering the real intake
        # has to live with, since the guard is paid for before the row exists.
        item_cost_rows: list[int] = []
        before = table_counts()
        dedup_config = CandidateDedupConfig.from_config(config)
        with collect_cost_rows() as rows:
            embeddings = {
                item.title: np.asarray(
                    fake_embed(f"{item.title}\n{item.summary}")
                )
                for item in primary_items
            }
        item_cost_rows.extend(rows)
        first, near_dup = primary_items[0], primary_items[1]
        in_run_similarity = jaccard(
            normalize_title(first.title), normalize_title(near_dup.title)
        )
        pre = check_pre_guard(
            brand_id_fk=brand_id,
            embedding=embeddings[first.title],
            input_kind="document",
            primary_doc_url=str(first.url),
            config=dedup_config,
        )
        ledger.add(
            Stage(
                name="dedup",
                inputs=(
                    "3 items; three windows "
                    f"(live≥{dedup_config.threshold_live}, "
                    f"rejected≥{dedup_config.threshold_rejected}/"
                    f"{dedup_config.window_rejected_days}d, "
                    f"published≥{dedup_config.threshold_published}/"
                    f"{dedup_config.window_published_days}d)"
                ),
                outputs=(
                    f"in-run L1 title jaccard {in_run_similarity:.2f} on the "
                    f"planted near-duplicate (drops at ≥0.70); "
                    f"pre-guard decision={pre.action}"
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "embeddings are deterministic fakes, so this measures the "
                    "windows and the doc-URL key, not embedding quality"
                ),
            )
        )

        # ---- 3. prefilter ---------------------------------------------
        before = table_counts()
        rules = PrefilterRules.from_config(config)
        decisions = {
            "primary_feed item": prefilter_item(
                title=first.title,
                summary=first.summary,
                published_at=first.published_at,
                source_role="primary_feed",
                source_language="en",
                rules=rules,
                now=now,
            ),
            "news personnel item": prefilter_item(
                title=news_items[0].title,
                summary=news_items[0].summary,
                published_at=news_items[0].published_at,
                source_role="news",
                source_language="en",
                rules=rules,
                now=now,
            ),
        }
        ledger.add(
            Stage(
                name="prefilter",
                inputs=(
                    f"{len(rules.deny_title_patterns)} deny patterns, "
                    f"min_summary={rules.min_summary_chars} chars "
                    "(news only), age caps "
                    f"{rules.max_age_hours_news}h news / "
                    f"{rules.max_age_hours_primary}h primary"
                ),
                outputs="; ".join(
                    f"{label}: "
                    + ("keep" if d.keep else f"drop({d.reason})")
                    for label, d in decisions.items()
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "free, in code. The primary-feed exemption is the "
                    "2026-08-28 hotfix: BaFin annotates in 13-60 chars and "
                    "was being dropped before the guard saw it"
                ),
            )
        )

        # ---- 4. guard -------------------------------------------------
        before = table_counts()
        template, template_source = resolve_guard_template(brand_id)
        taxonomy = load_brand_taxonomy(brand_id)
        with collect_cost_rows() as rows:
            verdict = await judge_item(
                input_kind="document",
                title=first.title,
                summary=first.summary,
                source_name=primary["name"],
                source_class=primary["class"],
                source_language="en",
                published_at=first.published_at,
                recent_accepted_titles=recent_accepted_titles(
                    brand_id_fk=brand_id
                ),
                template=template,
                services_block=render_services(taxonomy),
                tiers_block=render_jurisdiction_tiers(
                    getattr(config, "jurisdiction_tiers", None)
                ),
                allowed_service_keys=tuple(
                    entry["key"] for entry in taxonomy
                ),
                model=getattr(config, "guard_model", "gpt-4o-mini"),
            )
        item_cost_rows.extend(rows)
        ledger.add(
            Stage(
                name="guard",
                inputs=(
                    f"rubric from {template_source}, "
                    f"{len(taxonomy)} services, input_kind=document, "
                    f"model={getattr(config, 'guard_model', 'gpt-4o-mini')}"
                ),
                outputs=(
                    f"{verdict.verdict}/{verdict.reason_code}, "
                    f"service={verdict.service_category}, "
                    f"stage={verdict.event_stage}, "
                    f"depth_prior={verdict.depth_prior}, "
                    f"confidence={verdict.confidence}"
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "the only paid call in contour 1; JSON-schema-constrained, "
                    "so a malformed answer is a guard_error and not a candidate"
                ),
            )
        )

        # ---- 5. candidate ---------------------------------------------
        before = table_counts()
        with collect_cost_rows() as rows:
            post = check_post_guard(
                brand_id_fk=brand_id,
                embedding=embeddings[first.title],
                event_stage=verdict.event_stage,
                config=dedup_config,
            )
            candidate_id = create_candidate(
                CandidateInput(
                    brand_id_fk=brand_id,
                    input_kind="document",
                    source_id_fk=primary["id"],
                    source_title=first.title,
                    source_summary=first.summary,
                    source_url=str(first.url),
                    source_published_at=first.published_at,
                    source_language="en",
                    source_name=primary["name"],
                    source_class=primary["class"],
                    topic_embedding_ref=None,
                    verdict=verdict.verdict,
                    reason_code=verdict.reason_code,
                    reason=verdict.reason,
                    confidence=verdict.confidence,
                    service_category=verdict.service_category,
                    jurisdictions=verdict.jurisdictions,
                    event_stage=verdict.event_stage,
                    depth_prior=verdict.depth_prior,
                    primary_doc_hint=verdict.primary_doc_hint,
                    primary_doc_url=str(first.url),
                    doc_language_expected=verdict.doc_language_expected,
                ),
                ttl_config=getattr(config, "candidate_ttl_days", None),
                now=now,
            )
        item_cost_rows.extend(rows)
        attached = attach_candidate(item_cost_rows, candidate_id)
        outcome["candidate_id"] = candidate_id
        ledger.add(
            Stage(
                name="candidate",
                inputs=f"guard verdict + post-guard decision={post.action}",
                outputs=(
                    f"candidate #{candidate_id} status=pending, "
                    f"expires in "
                    f"{getattr(config, 'candidate_ttl_days', '{}')}"
                ),
                db_writes=diff_counts(before, table_counts())
                + f"; {attached} cost row(s) charged to the candidate",
                note=(
                    "cost→candidate is the NTS_121 fix: the guard is paid for "
                    "before the row exists, so the ids are collected and "
                    "back-filled (NTS_106 §3)"
                ),
            )
        )

        # ---- 6. select ------------------------------------------------
        # S4 closed this. The rank runs for real here — over the one candidate
        # the walkthrough built — so a regression in the formula shows up as a
        # changed stage line rather than as a silently different ordering in
        # production.
        from pipeline.production import eligible_candidates
        from pipeline.selector.ranking import RankWeights, select_batch

        before = table_counts()
        facts = eligible_candidates(brand_id_fk=brand_id, now=now)
        picks = select_batch(
            facts,
            weights=RankWeights.from_config(config),
            tiers=getattr(config, "jurisdiction_tiers", {}) or {},
            now=now,
            limit=int(getattr(config, "weekly_draft_budget", 6)),
        )
        top = picks[0] if picks else None
        won = claim_pending(candidate_id, now=now)
        started = begin_production(
            candidate_id=candidate_id,
            brand_id_fk=brand_id,
            batch_key=batch_key("icon", now.date()),
        )
        ledger.add(
            Stage(
                name="select",
                inputs=(
                    f"{len(facts)} eligible in `pending`; weekly_draft_budget="
                    f"{getattr(config, 'weekly_draft_budget', '?')}; "
                    f"weights={RankWeights.from_config(config)}"
                ),
                outputs=(
                    f"rank={top.rank:.4f} "
                    f"({', '.join(f'{k}={v:+.3f}' for k, v in top.terms.items() if not k.startswith('_'))})"
                    if top
                    else "no eligible candidate"
                )
                + f"; claim_pending → {won}; begin_production → {started}",
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "S4: the formula's terms are logged per candidate, the "
                    "batch is claimed once per (brand, day), and the four keys "
                    "NTS_121 §2 found without a reader — weekly_draft_budget, "
                    "production_timeout_min, candidate_ttl_days, "
                    "retention_days_rejected — all have one now"
                ),
            )
        )

        # ---- 7. doc fetch + match -------------------------------------
        ledger.add(
            Stage(
                name="doc fetch + match",
                status=NOT_IMPLEMENTED,
                owner="S5",
                inputs=(
                    f"candidate #{candidate_id}: "
                    f"primary_doc_url={first.url}, "
                    f"doc_hint={verdict.primary_doc_hint!r}, "
                    f"doc_language_expected="
                    f"{verdict.doc_language_expected!r}"
                ),
                outputs="nothing — no fetcher exists",
                note=(
                    "NTS_101 §2-7: two paths to the document, Firecrawl + PDF "
                    "extraction, doc_match, section extraction into "
                    "doc_sections_used, the cache with as_of, doc_missing with "
                    "retries. The columns are in place (025 added "
                    "doc_sections_used); the only writer today is the manual "
                    "link from the Portfolio screen (doc_match='manual')"
                ),
            )
        )

        # ---- 8. research ----------------------------------------------
        before = table_counts()
        from pipeline.generator.fact_pack_store import persist_fact_pack
        from pipeline.generator.research import ResearchBudget, build_fact_pack

        topic = Topic(
            id=f"e2e-{candidate_id}",
            brand_id="icon",
            raw=first,
            relevance_score=8.0,
            candidate_id=candidate_id,
        )
        fact_pack = await build_fact_pack(
            topic,
            budget=ResearchBudget(
                max_sources=int(getattr(config, "research_max_sources", 5)),
                max_tokens=int(getattr(config, "research_max_tokens", 2000)),
                timeout_seconds=int(
                    getattr(config, "research_timeout_seconds", 60)
                ),
            ),
            client=FakeOpenAI(),
        )
        from pipeline.run import _fact_pack_as_dict

        fact_pack_id = persist_fact_pack(
            brand_id_fk=brand_id,
            candidate_id=candidate_id,
            topic_id=topic.id,
            pack=_fact_pack_as_dict(fact_pack),
            sources=tuple(fact_pack.citations) if fact_pack else (),
            model=fact_pack.model if fact_pack else None,
        )
        outcome["fact_pack_id"] = fact_pack_id
        ledger.add(
            Stage(
                name="research",
                inputs="topic title + summary + url; web_search tool",
                outputs=(
                    f"fact pack: {fact_pack.fact_count} facts, "
                    f"{len(fact_pack.citations)} citations, "
                    f"{fact_pack.searches} searches"
                    if fact_pack
                    else "no fact pack (thin path)"
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "NTS_096 part A now persists the pack on every call, "
                    "candidate id included — before this the pack was "
                    "discarded and reconstructing provenance cost a new paid "
                    "research call. Parts B (traceability block on the card) "
                    "and C (attribution check) are still open"
                ),
            )
        )

        # ---- 9. plan --------------------------------------------------
        ledger.add(
            Stage(
                name="plan",
                status=NOT_IMPLEMENTED,
                owner="S6",
                inputs="fact pack + primary document",
                outputs="nothing — composition still goes prompt→text",
                note=(
                    "NTS_102 v2: a saved plan before the text, depth_final "
                    "from the fact count (depth_article_min_facts / "
                    "depth_deep_min_facts have no reader), length with no "
                    "ceiling. Today depth_prior is written and depth_final "
                    "never is"
                ),
            )
        )

        # ---- 10. compose ----------------------------------------------
        before = table_counts()
        from pipeline.generator.comment_writer import CommentWriter

        writer = CommentWriter(client=FakeOpenAI(), brand_id_fk=brand_id)
        en_draft = await writer.write(
            topic,
            "mission: clarity for cross-border wealth\n",
            Language.en,
            fact_pack=fact_pack,
        )
        ledger.add(
            Stage(
                name="compose",
                inputs=(
                    "topic + voice profile + fact pack; prompts sourced from "
                    "the brand's active `prompts` rows (NTS_067)"
                ),
                outputs=(
                    f"EN draft: {len(en_draft.body)} chars, "
                    f"title={en_draft.title!r}"
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "this is the v2 writer (draft → polish). It works, and it "
                    "is what S4 was told to reuse; the v3 composition — plan, "
                    "no length ceiling, data blocks — is S6"
                ),
            )
        )

        # ---- 11. data blocks ------------------------------------------
        ledger.add(
            Stage(
                name="data blocks",
                status=NOT_IMPLEMENTED,
                owner="S6 + S8",
                inputs="fact pack figures",
                outputs="nothing — the body is markdown prose only",
                note=(
                    "NTS_095/NTS_102: keyFigures / statTable / chart generated "
                    "strictly from the fact pack. Blocked on the Sanity schema "
                    "PR (S8) before the pipeline may write them"
                ),
            )
        )

        # ---- 12. attribution ------------------------------------------
        ledger.add(
            Stage(
                name="attribution check",
                status=NOT_IMPLEMENTED,
                owner="S6",
                inputs="EN body + fact pack + primary document",
                outputs="nothing — no confirmed/distorted/uncovered verdicts",
                note=(
                    "NTS_096 part C, and the reason it exists: '18 years of "
                    "experience, most recently at CS and UBS' became '18-year "
                    "tenure at CS and UBS'. Right number, right source, false "
                    "claim — and every existing check passes it. Must run "
                    "before translation, so the distortion is not multiplied "
                    "by four languages"
                ),
            )
        )

        # ---- 13. translate --------------------------------------------
        before = table_counts()
        ru_draft = await writer.translate(
            en_draft, Language.ru, "mission: clarity\n"
        )
        ledger.add(
            Stage(
                name="translate",
                inputs=f"EN canon ({len(en_draft.body)} chars) → ru",
                outputs=f"RU draft: {len(ru_draft.body)} chars",
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "runs today BEFORE any attribution check, which is the "
                    "ordering NTS_096 part C says is wrong"
                ),
            )
        )

        # ---- 14. internal linking -------------------------------------
        ledger.add(
            Stage(
                name="internal linking",
                status=NOT_IMPLEMENTED,
                owner="S6",
                inputs=(
                    "brand_taxonomy.service_url_path "
                    f"({len(taxonomy)} services), published articles"
                ),
                outputs="nothing — no linker module exists",
                note=(
                    "NTS_093. The data side is ready and in use by the guard; "
                    "there is no code that resolves a link. Must run after "
                    "translation so each language links its own pages"
                ),
            )
        )

        # ---- 15. sanity draft + the link ------------------------------
        before = table_counts()
        publisher = FakeSanityPublisher()
        from pipeline.publisher.sanity import SanityPostInput

        post_input = SanityPostInput(
            title=en_draft.title,
            body_markdown=en_draft.body,
            language=Language.en,
            category="structuring",
            source_url=str(first.url),
            topic_id=topic.id,
            key_takeaway=en_draft.key_takeaway,
            cover_image_asset_id=None,
            cover_image_alt=en_draft.title[:120],
            display_date=(now.date()).isoformat(),
        )
        draft_id = await publisher.publish_draft(post_input)
        linked = lifecycle.link_candidate_to_draft(
            candidate_id=candidate_id,
            sanity_draft_id=draft_id,
            brand_id_fk=brand_id,
            now=now,
        )
        outcome["draft_id"] = draft_id
        ledger.add(
            Stage(
                name="sanity draft + candidate link",
                inputs=f"EN draft, candidate #{candidate_id}",
                outputs=(
                    f"{draft_id}; link_candidate_to_draft → {linked} "
                    "(candidate `drafted`, approval row carries "
                    "candidate_id_fk)"
                ),
                db_writes=diff_counts(before, table_counts()),
                note=(
                    "THE fix of this session. On production this link was "
                    "filled on 0 of 337 candidates and 0 of 137 approvals, "
                    "which made `published` unreachable by definition"
                ),
            )
        )

    # ---- 16. approve --------------------------------------------------
    # Outside the intake cost context: approving is not part of the run.
    before = table_counts()
    from pipeline.admin.routes import drafts as drafts_routes

    approval = drafts_routes._upsert_approval(
        draft_id,
        brand_id,
        "approved",
        "e2e walkthrough",
        published_at=now + timedelta(minutes=4),
        sanity_published_id=await publisher.promote_draft_to_published(draft_id),
    )
    drafts_routes._advance_candidate_on_approve(
        sanity_draft_id=draft_id, brand_id=brand_id, note="e2e walkthrough"
    )
    ledger.add(
        Stage(
            name="approve",
            inputs=f"{draft_id} + completeness guard (NTS_090)",
            outputs=(
                f"approval status={approval.status}, "
                f"published_at={approval.published_at is not None}"
            ),
            db_writes=diff_counts(before, table_counts()),
            note=(
                "the review_decisions row is new in this session: the "
                "candidate endpoints wrote that table, the draft endpoints — "
                "where the editor's time actually goes — did not"
            ),
        )
    )

    # ---- 17. publication slot -----------------------------------------
    before = table_counts()
    with admin_db.get_session_factory()() as session:
        from pipeline.admin.models import Candidate

        final = session.get(Candidate, candidate_id)
        final_status = final.status
        final_slot = final.publication_slot
        final_published = final.published_at
    ledger.add(
        Stage(
            name="publication slot",
            inputs=(
                "publication_slots="
                f"{getattr(config, 'publication_slots', '[]')}, "
                f"tz={getattr(config, 'brand_timezone', 'UTC')}"
            ),
            outputs=(
                f"candidate status={final_status}, slot={final_slot}, "
                f"published_at={final_published}"
            ),
            db_writes=diff_counts(before, table_counts()),
            note=(
                "slot assigned on `drafted → ready`; `published` set only "
                "from draft_approvals.published_at (NTS_098 §2). Automatic "
                "publication does not exist and is not supposed to"
            ),
        )
    )

    client.record_run_finish(
        run_id,
        status="success",
        stats={"walkthrough": True, "candidate_id": candidate_id},
        log_excerpt="e2e walkthrough — mocked externals, zero spend",
    )
    outcome["final_status"] = final_status
    outcome["final_slot"] = str(final_slot)
    return outcome


# --------------------------------------------------------------------------
# 6. Entry point
# --------------------------------------------------------------------------


def render_markdown(ledger: Ledger, spend: dict[str, float]) -> str:
    lines = [
        "| # | этап | вход | выход | записи в БД | статус | оценка стоимости |",
        "|---|---|---|---|---|---|---|",
    ]
    total = sum(spend.values())
    for index, stage in enumerate(ledger.stages, start=1):
        owner = f" ({stage.owner})" if stage.owner else ""
        lines.append(
            "| {n} | {name} | {inp} | {out} | {db} | {status}{owner} | {cost} |".format(
                n=index,
                name=stage.name,
                inp=stage.inputs.replace("|", "\\|") or "—",
                out=stage.outputs.replace("|", "\\|") or "—",
                db=stage.db_writes.replace("|", "\\|") or "—",
                status=stage.badge,
                owner=owner,
                cost=f"${stage.cost_usd:.4f}" if stage.cost_usd else "—",
            )
        )
    lines.append("")
    lines.append(f"**Всего по `cost_records`: ${total:.4f}**")  # noqa: RUF001
    for operation, amount in sorted(spend.items(), key=lambda kv: -kv[1]):
        lines.append(f"* `{operation}` — ${amount:.4f}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="fixture DB path (default: a temp file, deleted on exit)",
    )
    parser.add_argument(
        "--from-dump",
        type=Path,
        default=None,
        help="copy this admin.db dump first, then migrate it to head",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print the report table for the session log",
    )
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)

    tmpdir: tempfile.TemporaryDirectory | None = None
    if args.db is None:
        tmpdir = tempfile.TemporaryDirectory(prefix="nts-e2e-")
        db_path = Path(tmpdir.name) / "admin.db"
    else:
        db_path = args.db

    print("=" * 78)
    print("NTS v3 e2e walkthrough — mocked externals, zero spend")
    print("=" * 78)

    from cryptography.fernet import Fernet

    os.environ["ADMIN_DB_PATH"] = str(db_path)
    # Overwritten, not defaulted, and in both directions:
    #   * the repo's .env holds a REAL OpenAI key, and a client built from it
    #     would be one blocked-httpx patch away from a live call;
    #   * the test suite's conftest sets it to "", and an empty key makes the
    #     guard raise "OPENAI_API_KEY not set" — a *skipped* stage reported as
    #     a failure to reach the model, which is not what this measures.
    # A fake, non-empty value is the only value that exercises the real path.
    os.environ["OPENAI_API_KEY"] = "sk-e2e-fake"
    os.environ["REPLICATE_API_TOKEN"] = "r8-e2e-fake"
    os.environ["BRANDS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    build_fixture_db(db_path, from_dump=args.from_dump)

    # Import AFTER ADMIN_DB_PATH is set, then replace the OpenAI client class
    # for every module that will construct one.
    import openai

    from pipeline.common import config as config_module

    config_module._settings = None
    openai.AsyncOpenAI = FakeOpenAI  # type: ignore[assignment,misc]

    from pipeline.admin import db as admin_db
    from pipeline.admin import encryption as enc_mod

    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    admin_db.get_engine(path=db_path)

    ledger = Ledger()
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    print()
    outcome = asyncio.run(walkthrough(ledger, now=now))
    spend = spend_by_operation()

    print()
    print("-" * 78)
    print("Spend that WOULD have been charged (real accounting, fake usage):")
    for operation, amount in sorted(spend.items(), key=lambda kv: -kv[1]):
        print(f"  {operation:<24} ${amount:.6f}")
    print(f"  {'TOTAL':<24} ${sum(spend.values()):.6f}")
    print()
    counts = {
        OK: sum(1 for s in ledger.stages if s.status == OK),
        GAP: sum(1 for s in ledger.stages if s.status == GAP),
        NOT_IMPLEMENTED: sum(
            1 for s in ledger.stages if s.status == NOT_IMPLEMENTED
        ),
    }
    print(
        f"Stages: {len(ledger.stages)} total — "
        f"{counts[OK]} ok, {counts[GAP]} gap, "
        f"{counts[NOT_IMPLEMENTED]} not implemented"
    )
    print(
        f"Chain end state: candidate #{outcome['candidate_id']} "
        f"status={outcome['final_status']} slot={outcome['final_slot']} "
        f"draft={outcome['draft_id']}"
    )
    assert (
        "guard" in FakeOpenAI.calls
    ), "the guard was never called — the walkthrough proved nothing"

    if args.markdown:
        print()
        print(render_markdown(ledger, spend))
    if args.json:
        print()
        print(
            json.dumps(
                {
                    "stages": [vars(s) for s in ledger.stages],
                    "spend_by_operation": spend,
                    "outcome": outcome,
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if tmpdir is not None:
        tmpdir.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
