"""Keep the material an article was built on (NTS_096 part A).

The problem this closes, quoted from NTS_096: "Ресёрч отработал, статья
написана, пакет выброшен. Чтобы собрать построчную трассировку для приёмки,
потребовался **новый платный ресёрч-вызов** — и одно утверждение всё равно
осталось непроверяемым, потому что второй вызов вернул другой набор
источников."

So: one row per research call, written **before** anyone knows whether the
topic will publish. A store that only keeps packs for published articles cannot
answer why the rest came out thin, which is the question it exists for.

Two properties worth stating, because both are easy to lose later:

* **The parsed pack, never the raw provider dump.** The dump is bigger, its
  shape is the provider's to change, and nothing reads it.
* **The extracted document text, never the source PDF** (NTS_096 §Риски).

The row carries every id in the chain that exists at write time — candidate,
Sanity draft, v2 ``topic_id``, primary document + version + sections used — so
the walk "published article → draft → candidate → document → section" is a
sequence of indexed lookups rather than a new paid call. ``candidate_id`` stays
``None`` until the S4 production path has a candidate to pass; that is a gap in
the *writer*, not in the chain.

Writing here must never break a run: a lost audit row costs traceability for
one article, a raised exception costs the article.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from ..common.logging import get_logger

log = get_logger(__name__)


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def persist_fact_pack(
    *,
    brand_id_fk: int,
    pack: Mapping[str, Any] | None,
    candidate_id: int | None = None,
    sanity_draft_id: str | None = None,
    topic_id: str | None = None,
    sources: Iterable[str] = (),
    primary_doc_url: str | None = None,
    doc_version_id: str | None = None,
    doc_sections_used: Iterable[str] | None = None,
    doc_text: str | None = None,
    model: str | None = None,
    cost_usd: float = 0.0,
    now: datetime | None = None,
) -> int | None:
    """Store one fact pack. Returns the row id, or ``None`` if it could not.

    ``doc_sections_used`` is the S5 half of the chain (NTS_101 §2-7) and will
    arrive empty until the targeted extraction exists — recorded as absent
    rather than faked.
    """
    try:
        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import FactPack

        with get_session_factory()() as session:
            row = FactPack(
                brand_id_fk=brand_id_fk,
                candidate_id_fk=candidate_id,
                topic_id=topic_id,
                sanity_draft_id=sanity_draft_id,
                pack=_json_or_none(dict(pack or {})) or "{}",
                sources=_json_or_none(list(sources)) or "[]",
                primary_doc_url=primary_doc_url,
                doc_version_id=doc_version_id,
                doc_sections_used=(
                    _json_or_none(list(doc_sections_used))
                    if doc_sections_used is not None
                    else None
                ),
                doc_text=doc_text,
                model=model,
                cost_usd=float(cost_usd or 0.0),
                created_at=now or datetime.now(tz=UTC),
            )
            session.add(row)
            session.commit()
            log.info(
                "fact_pack.persisted",
                fact_pack_id=row.id,
                candidate_id=candidate_id,
                draft_id=sanity_draft_id,
                topic_id=topic_id,
                sources=len(list(sources)),
            )
            return int(row.id)
    except Exception as exc:
        log.warning(
            "fact_pack.persist_failed",
            err=f"{type(exc).__name__}: {exc}",
            topic_id=topic_id,
        )
        return None


def load_latest_fact_pack(candidate_id: int) -> tuple[int, dict[str, Any]] | None:
    """The newest **non-empty** pack for a candidate, as ``(row_id, pack)``.

    NTS_100 §4 — "частично записанные артефакты (fact pack, кэш документа)
    **сохраняются** и переиспользуются при повторе: повтор не платит за ресёрч
    заново". A production retry after a failure in composition or translation
    must not buy the same research a second time; research is 59% of the cost
    of an article (NTS_122), so this is the difference between a cheap retry
    and a doubled bill.

    Empty packs are skipped on purpose: a row saying "research produced
    nothing" is a valuable record and a useless input, and reusing it would
    make one bad research call permanent for that candidate.
    """
    try:
        from sqlalchemy import select

        from pipeline.admin.db import get_session_factory
        from pipeline.admin.models import FactPack

        with get_session_factory()() as session:
            rows = (
                session.execute(
                    select(FactPack.id, FactPack.pack)
                    .where(FactPack.candidate_id_fk == candidate_id)
                    .order_by(FactPack.created_at.desc(), FactPack.id.desc())
                    .limit(10)
                )
                .all()
            )
        for row_id, raw in rows:
            try:
                pack = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            if isinstance(pack, dict) and not pack.get("empty", True):
                return int(row_id), pack
        return None
    except Exception as exc:
        log.warning(
            "fact_pack.load_failed",
            candidate_id=candidate_id,
            err=f"{type(exc).__name__}: {exc}",
        )
        return None
