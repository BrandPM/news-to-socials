"""``/api/v1/prompts`` route group."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from pipeline.admin.db import session_scope
from pipeline.admin.models import Prompt
from pipeline.admin.schemas import (
    PromptDiffOut,
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
    brand_id: int | None = None,
    prompt_type: PromptType | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PromptOut]:
    with session_scope() as session:
        stmt = select(Prompt).order_by(Prompt.created_at.desc())
        if brand_id is not None:
            stmt = stmt.where(Prompt.brand_id_fk == brand_id)
        if prompt_type is not None:
            stmt = stmt.where(Prompt.prompt_type == prompt_type)
        stmt = stmt.offset(offset).limit(limit)
        return [PromptOut.model_validate(p) for p in session.scalars(stmt)]


@router.get("/diff", response_model=PromptDiffOut)
def diff_prompts(a: int, b: int) -> PromptDiffOut:
    """Unified diff between two prompt versions (S5 Step 8).

    Allows ``a == b`` for the "current vs current" no-op case; the UI
    uses that to show a blank diff after a single-prompt edit reverted.
    Cross-brand or cross-type comparisons are allowed but flagged via
    the ``same_brand`` / ``same_prompt_type`` booleans so the UI can
    warn before the operator activates the wrong thing.
    """
    import difflib  # noqa: PLC0415

    with session_scope() as session:
        prompt_a = session.get(Prompt, a)
        prompt_b = session.get(Prompt, b)
        if prompt_a is None or prompt_b is None:
            missing = [
                pid
                for pid, row in ((a, prompt_a), (b, prompt_b))
                if row is None
            ]
            raise HTTPException(
                status_code=404,
                detail=f"prompt(s) not found: {missing}",
            )
        a_out = PromptOut.model_validate(prompt_a)
        b_out = PromptOut.model_validate(prompt_b)
        same_brand = prompt_a.brand_id_fk == prompt_b.brand_id_fk
        same_type = prompt_a.prompt_type == prompt_b.prompt_type
        content_a = prompt_a.content or ""
        content_b = prompt_b.content or ""
        version_a = prompt_a.version_name
        version_b = prompt_b.version_name

    diff_lines = difflib.unified_diff(
        content_a.splitlines(keepends=False),
        content_b.splitlines(keepends=False),
        fromfile=f"#{a} {version_a}",
        tofile=f"#{b} {version_b}",
        lineterm="",
        n=3,
    )
    return PromptDiffOut(
        a=a_out,
        b=b_out,
        unified_diff="\n".join(diff_lines),
        same_brand=same_brand,
        same_prompt_type=same_type,
    )


@router.get("/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: int) -> PromptOut:
    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        return PromptOut.model_validate(p)


@router.post("", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
def create_prompt(payload: PromptIn) -> PromptOut:
    with session_scope() as session:
        p = Prompt(
            brand_id_fk=payload.brand_id,
            prompt_type=payload.prompt_type,
            version_name=payload.version_name,
            content=payload.content,
            notes=payload.notes,
            is_active=False,
            created_by="human",
        )
        session.add(p)
        try:
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"brand_id={payload.brand_id} does not reference an existing brand",
            ) from exc
        return PromptOut.model_validate(p)


@router.post("/{prompt_id}/activate", response_model=PromptOut)
def activate_prompt(prompt_id: int) -> PromptOut:
    """Make this prompt the active one for its (brand_id_fk, prompt_type).

    Runs in a single transaction so we never end up with two active rows
    (the partial UNIQUE index would reject it anyway, but the transaction
    keeps the failure mode coherent for the caller).
    """
    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        session.execute(
            update(Prompt)
            .where(
                Prompt.brand_id_fk == p.brand_id_fk,
                Prompt.prompt_type == p.prompt_type,
                Prompt.is_active.is_(True),
                Prompt.id != p.id,
            )
            .values(is_active=False)
        )
        p.is_active = True
        session.flush()
        return PromptOut.model_validate(p)


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
    test runs of the same prompt are comparable.
    """
    from pipeline.admin import llm  # noqa: PLC0415

    with session_scope() as session:
        p = session.get(Prompt, prompt_id)
        if p is None:
            raise HTTPException(status_code=404, detail="prompt not found")
        prompt_content = p.content
        prompt_type = p.prompt_type
        brand_id_fk = p.brand_id_fk

    result = await llm.run_prompt_test(
        prompt_type=prompt_type,
        prompt_content=prompt_content,
        sample_topic=_SAMPLE_TOPIC,
        brand_id_fk=brand_id_fk,
    )
    return PromptTestOut(
        generated_text=result.text,
        cost_usd=result.cost_usd,
        ai_tells_count=result.ai_tells_count,
    )
