"""``/api/v1/config`` route group."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from pipeline.admin.db import session_scope
from pipeline.admin.models import PipelineConfig
from pipeline.admin.schemas import PipelineConfigOut, PipelineConfigUpdate

router = APIRouter()


@router.get("", response_model=PipelineConfigOut)
def get_config(brand_id: str = "icon") -> PipelineConfigOut:
    with session_scope() as session:
        cfg = session.get(PipelineConfig, brand_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail=f"no config row for brand_id={brand_id!r} — run seed_admin_db first",
            )
        return PipelineConfigOut.model_validate(cfg, from_attributes=True)


@router.put("", response_model=PipelineConfigOut)
def update_config(
    payload: PipelineConfigUpdate, brand_id: str = "icon"
) -> PipelineConfigOut:
    with session_scope() as session:
        cfg = session.get(PipelineConfig, brand_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail=f"no config row for brand_id={brand_id!r}",
            )
        data = payload.model_dump(exclude_unset=True)
        if "banned_phrases" in data and data["banned_phrases"] is not None:
            data["banned_phrases"] = json.dumps(
                data["banned_phrases"], ensure_ascii=False
            )
        for k, v in data.items():
            setattr(cfg, k, v)
        cfg.updated_at = datetime.now(tz=timezone.utc)
        session.flush()
        return PipelineConfigOut.model_validate(cfg, from_attributes=True)
