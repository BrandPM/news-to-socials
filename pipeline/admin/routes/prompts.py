"""``/api/v1/prompts`` route group."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update

from pipeline.admin.db import session_scope
from pipeline.admin.models import Prompt
from pipeline.admin.schemas import (
    PromptIn,
    PromptOut,
    PromptTestIn,
    PromptTestOut,
    PromptType,
)

router = APIRouter()


# --- Fixture used by the /test endpoint ---------------------------------

_SAMPLE_TOPIC = {
    "id": "sample-001",
    "title": "India announces new credit-fund regime for private capital",
    "summary": (
        "The Securities and Exchange Board of India is expected to publish "
        "implementing rules for the new credit-fund category in the next "
        "quarter, with allocations open to qualified institutional buyers "
        "and family offices."
    ),
    "url": "https://example.com/india-credit-fund",
}


@router.get("", response_model=list[PromptOut])
def list_prompts(
    brand_id: str | None = None,
    prompt_type: PromptType | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PromptOut]:
    with session_scope() as session:
        stmt = select(Prompt).order_by(Prompt.created_at.desc())
        if brand_id is not None:
            stmt = stmt.where(Prompt.brand_id == brand_id)
        if prompt_type is not None:
            stmt = stmt.where(Prompt.prompt_type == prompt_type)
        stmt = stmt.offset(offset).limit(limit)
        return [
            PromptOut.model_validate(p, from_attributes=True)
            for p in session.scalars(stmt)
        ]


@router.get("/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: int) -> PromptOut:
    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        return PromptOut.model_validate(p, from_attributes=True)


@router.post("", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
def create_prompt(payload: PromptIn) -> PromptOut:
    with session_scope() as session:
        p = Prompt(
            brand_id=payload.brand_id,
            prompt_type=payload.prompt_type,
            version_name=payload.version_name,
            content=payload.content,
            notes=payload.notes,
            is_active=False,
            created_by="human",
        )
        session.add(p)
        session.flush()
        return PromptOut.model_validate(p, from_attributes=True)


@router.post("/{prompt_id}/activate", response_model=PromptOut)
def activate_prompt(prompt_id: int) -> PromptOut:
    """Make this prompt the active one for its (brand_id, prompt_type).

    Runs in a single transaction so we never end up with two active rows
    (the partial UNIQUE index would reject it anyway, but the transaction
    keeps the failure mode coherent for the caller).
    """
    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        # Deactivate the current active row first to satisfy the partial
        # UNIQUE index, then activate the requested one.
        session.execute(
            update(Prompt)
            .where(
                Prompt.brand_id == p.brand_id,
                Prompt.prompt_type == p.prompt_type,
                Prompt.is_active.is_(True),
                Prompt.id != p.id,
            )
            .values(is_active=False)
        )
        p.is_active = True
        session.flush()
        return PromptOut.model_validate(p, from_attributes=True)


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prompt(prompt_id: int) -> None:
    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        if p.is_active:
            raise HTTPException(
                status_code=409,
                detail="cannot delete the active prompt — activate another first",
            )
        session.delete(p)


@router.post("/{prompt_id}/test", response_model=PromptTestOut)
async def test_prompt(prompt_id: int, payload: PromptTestIn) -> PromptTestOut:
    """Render the prompt against a sample topic and return the LLM output.

    Doesn't save anything. The sample topic is a fixed string so two
    test runs of the same prompt are comparable — this becomes a real
    "snapshot diff" feature in S2.
    """
    from pipeline.admin import llm  # noqa: PLC0415

    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        prompt_content = p.content
        prompt_type = p.prompt_type

    result = await llm.run_prompt_test(
        prompt_type=prompt_type,
        prompt_content=prompt_content,
        sample_topic=_SAMPLE_TOPIC,
    )
    return PromptTestOut(
        generated_text=result.text,
        cost_usd=result.cost_usd,
        ai_tells_count=result.ai_tells_count,
    )
