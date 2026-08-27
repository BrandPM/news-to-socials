"""``/api/v1/config`` route group."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from pipeline.admin.db import session_scope
from pipeline.admin.models import PipelineConfig
from pipeline.admin.schemas import PipelineConfigOut, PipelineConfigUpdate

router = APIRouter()

# Config columns stored as JSON-as-TEXT. The API speaks real lists/dicts; the
# column holds a string. Missing one here is the classic way a config surface
# ends up storing "['a', 'b']" (Python repr) or a stringified object that the
# reader then fails to parse — so the set is named once and applied in a loop.
_JSON_COLUMNS = (
    "banned_phrases",
    # NTS_098 §4 / NTS_099 §1 — v3 keys, migration 020.
    "publication_slots",
    "candidate_ttl_days",
    "jurisdiction_tiers",
    "prefilter_deny_title_patterns",
    "prefilter_languages",
)


@router.get("", response_model=PipelineConfigOut)
def get_config(brand_id: int) -> PipelineConfigOut:
    with session_scope() as session:
        cfg = session.get(PipelineConfig, brand_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail=f"no config row for brand_id={brand_id} — run seed_admin_db first",
            )
        return PipelineConfigOut.model_validate(cfg)


@router.put("", response_model=PipelineConfigOut)
def update_config(
    payload: PipelineConfigUpdate, brand_id: int
) -> PipelineConfigOut:
    with session_scope() as session:
        cfg = session.get(PipelineConfig, brand_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail=f"no config row for brand_id={brand_id}",
            )
        data = payload.model_dump(exclude_unset=True)
        for key in _JSON_COLUMNS:
            if data.get(key) is not None:
                data[key] = json.dumps(data[key], ensure_ascii=False)
        for k, v in data.items():
            setattr(cfg, k, v)
        cfg.updated_at = datetime.now(tz=timezone.utc)
        session.flush()
        return PipelineConfigOut.model_validate(cfg)
