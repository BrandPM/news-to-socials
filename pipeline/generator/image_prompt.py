"""LLM-derived cover-image scene description (NTS_075 L2).

Root cause of the lookalike covers: the Flux prompt was ``{headline},
{style}``. A news headline is a weak *visual* signal, so Flux ignored it and
defaulted to its trained-in "luxury marble interior" for almost every topic.

Here we run the article's EN-canonical headline (plus a short summary when
available) through gpt-4o-mini to get a concrete, varied *visual scene* — a
description of what to actually depict — which the image generator then wraps
in a brand style directive: ``{scene}, {style}``.

This finally wires the ``image_prompt`` prompt_type (present in the schema but
unused) into generation, following the SAME contract as ``CommentWriter._
resolve_template``: the brand's ACTIVE ``image_prompt`` row in the ``prompts``
table is used when its placeholders are safe (``⊆ {title, summary}``), else we
fall back to :data:`_DEFAULT_IMAGE_PROMPT_TEMPLATE`. So an operator can edit /
A-B test the image brief from the Prompts UI, and a drifted edit degrades to the
canonical default instead of breaking generation.

Design contract:

* **Once per topic.** The caller (``run.generate_image_for_topic``) is the
  single per-topic seam (NTS_069 — one cover per topic, shared across language
  siblings), so building the scene there is structurally once-per-topic and
  never 4× per language. Built from the EN canon, never per-language.
* **Cost-recorded.** Logs a ``cost_records`` row with operation
  ``image_prompt`` via the shared OpenAI recorder, inside whatever cost
  context the caller has open (the run, or a regenerate draft context).
* **Never blocks generation.** Any failure (no API key, network, empty
  output) returns ``None`` so the caller falls back to the old headline+style
  prompt — an image without a smart scene beats no image.
"""

from __future__ import annotations

from ..common.config import get_settings
from ..common.logging import get_logger

log = get_logger(__name__)

_MODEL = "gpt-4o-mini"

# Placeholders the rendered template is allowed to reference. A DB row that
# references anything else is rejected (→ default), same safety posture as the
# writer's ``required <= fields <= allowed`` check.
_ALLOWED_FIELDS = {"title", "summary"}

_DEFAULT_IMAGE_PROMPT_TEMPLATE = (
    "You are an art director writing a single visual brief for an editorial "
    "cover image that accompanies a finance / wealth-management article.\n"
    "Given the article's headline and optional summary, describe ONE concrete, "
    "evocative visual SCENE that conveys the article's subject metaphorically "
    "or literally.\n"
    "Rules:\n"
    "- Output 1-2 sentences, max ~40 words. No preamble, no quotes, no lists.\n"
    "- Describe only what is visible: subject, setting, composition, mood, "
    "lighting. Be specific and concrete, not abstract corporate cliché.\n"
    "- The image must contain NO text, letters, words, numbers, labelled "
    "charts, logos or signage — describe a scene that needs none.\n"
    "- Avoid generic empty luxury interiors unless the topic truly calls for "
    "one. Prefer imagery genuinely tied to THIS topic.\n"
    "- Do not name the publication or any real brand.\n\n"
    "Article headline: {title}\n"
    "Article summary: {summary}\n\n"
    "Visual scene:"
)


def _resolve_image_template(brand_id_fk: int | None) -> str:
    """The brand's ACTIVE ``image_prompt`` row if its placeholders are safe,
    else the in-code default template. Any DB/lookup error → default."""
    if brand_id_fk is None:
        return _DEFAULT_IMAGE_PROMPT_TEMPLATE
    try:
        import string  # noqa: PLC0415

        from sqlalchemy import select  # noqa: PLC0415

        from pipeline.admin import db as admin_db  # noqa: PLC0415
        from pipeline.admin.models import Prompt  # noqa: PLC0415

        with admin_db.get_session_factory()() as session:
            row = session.scalars(
                select(Prompt).where(
                    Prompt.brand_id_fk == brand_id_fk,
                    Prompt.prompt_type == "image_prompt",
                    Prompt.is_active.is_(True),
                )
            ).first()
        if row is None or not row.content:
            return _DEFAULT_IMAGE_PROMPT_TEMPLATE
        fields = {
            fname
            for _, fname, _, _ in string.Formatter().parse(row.content)
            if fname
        }
        if not (fields <= _ALLOWED_FIELDS):
            log.warning(
                "image_prompt.db_prompt_rejected",
                unknown_placeholders=sorted(fields - _ALLOWED_FIELDS),
            )
            return _DEFAULT_IMAGE_PROMPT_TEMPLATE
        return row.content
    except Exception as exc:  # noqa: BLE001
        log.warning("image_prompt.db_prompt_resolve_failed", err=str(exc))
        return _DEFAULT_IMAGE_PROMPT_TEMPLATE


def _clean(scene: str) -> str:
    """Trim wrapping quotes / whitespace and collapse to a single line."""
    s = scene.strip().strip('"').strip("'").strip()
    return " ".join(s.split())


async def build_scene_prompt(
    title: str,
    summary: str = "",
    *,
    brand_id_fk: int | None = None,
    operation: str = "image_prompt",
) -> str | None:
    """Return a 1-2 sentence visual scene for the article, or ``None``.

    ``None`` means "no scene available" (LLM disabled or the call failed) —
    the caller falls back to the headline. Never raises.
    """
    settings = get_settings()
    title = (title or "").strip()
    if not settings.openai_api_key or not title:
        return None

    # Lazy imports keep module import cheap + mirror comment_writer's pattern.
    import openai  # noqa: PLC0415

    from .comment_writer import _record_openai_cost  # noqa: PLC0415

    template = _resolve_image_template(brand_id_fk)
    summary = (summary or "").strip()
    try:
        rendered = template.format(title=title, summary=summary or "(none provided)")
    except (KeyError, IndexError, ValueError):
        # A bad custom template that slipped past validation — fall back.
        rendered = _DEFAULT_IMAGE_PROMPT_TEMPLATE.format(
            title=title, summary=summary or "(none provided)"
        )

    try:
        client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": rendered}],
            max_tokens=120,
            temperature=0.9,  # variety across topics is the whole point
        )
    except Exception as exc:  # noqa: BLE001 — never block image generation
        log.warning("image_prompt.failed", err=str(exc))
        return None

    # Cost first (best-effort, never breaks on a mocked response), then parse.
    try:
        _record_openai_cost(resp, model=_MODEL, operation=operation)
    except Exception:  # noqa: BLE001
        pass

    try:
        scene = _clean(resp.choices[0].message.content or "")
    except (AttributeError, IndexError):
        return None
    if not scene:
        return None
    log.info("image_prompt.built", scene_len=len(scene))
    return scene
