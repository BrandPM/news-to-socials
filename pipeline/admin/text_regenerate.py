"""Regenerate the text of an existing Sanity draft (S5 Step 7; NTS_066).

Language-aware, per the NTS_065 translation architecture
([[IT_PROJ_NTS_041_translation_architecture]]):

* **EN draft** → re-polish in place (the canonical path, unchanged): read the
  current EN title/body, run the polish stage, patch back.
* **Non-EN draft (RU/UK/PL)** → re-**translate** from the CURRENT canonical
  EN draft of the same topic (``CommentWriter.translate``, gpt-4o) — the same
  path the pipeline uses after NTS_065. Preserves H2 set, facts/numbers and
  length; the target language's voice profile is applied only as phrasing
  localisation. The result is ALWAYS in the draft's own language — never
  silently switched to English (the NTS_066 bug: regenerate used to run polish
  with the default ``Language.en``, turning a RU draft into English).

The translated result is run through the same fidelity gates as the backfill
(``translation_check``): invented numbers and wrong-script output are HARD
failures that raise :class:`RegenerateError` instead of writing a bad draft;
H2-count / length drift is logged as a soft warning.

Cost is recorded against the draft via ``cost_recorder`` so the regenerated
polish/translate shows up on the draft's cost breakdown.
"""

from __future__ import annotations

from pipeline.common.logging import get_logger

log = get_logger(__name__)


class RegenerateError(ValueError):
    """A regenerate request can't proceed for a understandable, non-bug
    reason (e.g. no EN source to translate from, or the translation failed a
    fidelity gate). The text-job runner surfaces the message to the admin UI;
    it is NOT a 500 / raw stack."""


async def regenerate_draft_text(
    sanity_draft_id: str,
    brand_id_fk: int,
) -> None:
    """Read draft → polish (EN) or translate-from-EN (non-EN) → patch.

    Lazy imports for the same reason as ``image_regenerate``: tests of the
    dispatcher monkeypatch this whole function, so we don't want the
    OpenAI/Sanity stacks loaded just to verify route plumbing.
    """
    from pipeline.admin.cost_recorder import CostContext, cost_context  # noqa: PLC0415
    from pipeline.admin.db import session_scope  # noqa: PLC0415
    from pipeline.admin.encryption import get_encryption  # noqa: PLC0415
    from pipeline.admin.models import Brand  # noqa: PLC0415
    from pipeline.common.models import Language  # noqa: PLC0415
    from pipeline.generator.anti_ai_check import score_ai_tells  # noqa: PLC0415
    from pipeline.generator.comment_writer import (  # noqa: PLC0415
        CommentWriter,
        _DraftJSON,
        parse_topics_relevant,
        parse_voice_guardrails,
        parse_voice_principles,
    )
    from pipeline.publisher.sanity import SanityClient  # noqa: PLC0415

    # --- 1. Load brand creds + voice profile -----------------------------
    with session_scope() as session:
        brand = session.get(Brand, brand_id_fk)
        if brand is None:
            raise LookupError(f"brand {brand_id_fk} not found")
        project_id = brand.sanity_project_id
        dataset = brand.sanity_dataset or "production"
        api_version = brand.sanity_api_version or "2024-01-01"
        token_enc = brand.sanity_api_token_enc
        voice_profile_yaml = brand.voice_profile_yaml or ""

    if not project_id or not token_enc:
        raise LookupError(
            f"brand {brand_id_fk} has no Sanity credentials configured"
        )

    token = get_encryption().decrypt(token_enc)
    client = SanityClient(
        project_id=project_id,
        dataset=dataset,
        api_version=api_version,
        token=token,
    )
    del token

    # --- 2. Fetch the draft (incl. language + topicId) -------------------
    groq = '*[_id == $id][0]{title, body, keyTakeaway, language, topicId}'
    doc = await client.query(groq, {"id": sanity_draft_id})
    if not doc:
        raise LookupError(f"draft {sanity_draft_id!r} not found in Sanity")

    lang_code = doc.get("language") or "en"
    try:
        language = Language(lang_code)
    except ValueError:
        # Unknown code → treat as the canonical EN path; never guess a
        # translation target we don't support.
        log.warning("text_regenerate.unknown_language", code=lang_code)
        language = Language.en

    ctx = CostContext(brand_id_fk=brand_id_fk, draft_id=sanity_draft_id)

    if language == Language.en:
        # --- EN canon: re-polish in place (unchanged behaviour) ----------
        body_md = _portable_text_to_markdown(doc.get("body"))
        title = doc.get("title") or "Untitled"
        key_takeaway = doc.get("keyTakeaway") or ""
        score, tells = score_ai_tells(body_md)
        log.info(
            "text_regenerate.ai_score",
            draft_id=sanity_draft_id,
            score=score,
            tells=tells,
        )
        banned_phrases, good_examples = parse_voice_guardrails(
            voice_profile_yaml, Language.en
        )
        voice_principles = parse_voice_principles(voice_profile_yaml, Language.en)
        topics_relevant = parse_topics_relevant(voice_profile_yaml, Language.en)
        writer = CommentWriter(brand_id_fk=brand_id_fk)
        pre_draft = _DraftJSON(title=title, body=body_md, key_takeaway=key_takeaway)
        with cost_context(ctx):
            result = await writer._polish(  # noqa: SLF001
                pre_draft,
                tells,
                banned_phrases,
                good_examples,
                Language.en,
                voice_principles,
                topics_relevant,
            )
        new_title, new_body, new_kt = result.title, result.body, result.key_takeaway
    else:
        # --- Non-EN: translate the CURRENT canonical EN draft ------------
        topic_id = doc.get("topicId")
        if not topic_id:
            raise RegenerateError(
                f"draft {sanity_draft_id!r} has no topicId, so its canonical "
                "English source can't be located — cannot translate."
            )
        en_draft = await _load_en_source(client, str(topic_id), brand_id_fk)
        writer = CommentWriter(brand_id_fk=brand_id_fk)
        with cost_context(ctx):
            translated = await writer.translate(
                en_draft, language, voice_profile_yaml
            )
        _check_translation_fidelity(en_draft, translated, language, sanity_draft_id)
        new_title = translated.title
        new_body = translated.body
        new_kt = translated.key_takeaway

    # --- 3. Convert result body back to Portable Text + patch ------------
    new_blocks = _markdown_to_portable_text(new_body)
    await client.patch(
        sanity_draft_id,
        {
            "title": new_title,
            "body": new_blocks,
            "keyTakeaway": new_kt,
        },
    )
    log.info(
        "text_regenerate.done",
        draft_id=sanity_draft_id,
        language=language.value,
        mode="polish" if language == Language.en else "translate",
        new_length=len(new_body),
    )


async def _load_en_source(client, topic_id: str, brand_id_fk: int):
    """Return the canonical EN ``Draft`` for ``topic_id`` from Sanity.

    Prefers the published EN doc over its draft (the live canonical), mirroring
    the backfill. Raises :class:`RegenerateError` — not a raw error — when no
    EN source exists or its body is empty, so the UI shows a clear message
    instead of silently producing English text on a non-EN draft.
    """
    from pipeline.common.models import Draft, Language  # noqa: PLC0415

    groq = (
        '*[_type == "post" && topicId == $tid && language == "en"]'
        "{_id, title, body, keyTakeaway}"
    )
    rows = await client.query(groq, {"tid": topic_id})
    if not isinstance(rows, list) or not rows:
        raise RegenerateError(
            f"no English source draft found for topic {topic_id!r} — cannot "
            "translate this draft without the canonical EN version."
        )
    # Prefer the published id (no 'drafts.' prefix) as the canonical source.
    rows.sort(key=lambda r: str(r.get("_id", "")).startswith("drafts."))
    en = rows[0]
    body_md = _portable_text_to_markdown(en.get("body"))
    if not body_md.strip():
        raise RegenerateError(
            f"the English source for topic {topic_id!r} is empty — cannot "
            "translate from an empty canonical draft."
        )
    return Draft(
        topic_id=topic_id,
        brand_id=str(brand_id_fk),
        language=Language.en,
        title=en.get("title") or "",
        body=body_md,
        key_takeaway=en.get("keyTakeaway") or "",
    )


def _check_translation_fidelity(en_draft, translated, language, draft_id: str) -> None:
    """Gate a regenerated translation the same way the backfill does.

    HARD failures (raise :class:`RegenerateError`, no write): a number the EN
    source never had, or output in the wrong script for the language. Soft
    drift (H2 count, length, dropped numbers) is logged, not blocked.
    """
    from pipeline.common.models import Language  # noqa: PLC0415
    from pipeline.generator import translation_check as tc  # noqa: PLC0415

    invented = tc.invented_numbers(en_draft.body, translated.body)
    if language in (Language.ru, Language.uk):
        script_ok = tc.is_mostly_cyrillic(translated.body)
    elif language == Language.pl:
        script_ok = tc.is_polish_latin(translated.body)
    else:
        script_ok = True

    problems: list[str] = []
    if invented:
        problems.append(f"introduced numbers absent from the EN source: {invented}")
    if not script_ok:
        problems.append(
            f"output is not in the expected script for {language.value!r}"
        )
    if problems:
        raise RegenerateError(
            "regenerated translation failed fidelity gates — "
            + "; ".join(problems)
            + ". The draft was left unchanged."
        )

    log.info(
        "text_regenerate.translation_fidelity",
        draft_id=draft_id,
        language=language.value,
        en_h2=tc.h2_count(en_draft.body),
        tr_h2=tc.h2_count(translated.body),
        length_ratio=round(tc.length_ratio(en_draft.body, translated.body), 2),
        dropped_numbers=tc.dropped_numbers(en_draft.body, translated.body),
        title_clean=not tc.has_markdown_in_title(translated.title),
    )


def _portable_text_to_markdown(body: object) -> str:
    """Reduce Sanity Portable Text → markdown-ish (``##``/``###`` + paragraphs).

    Faithful to the structure the writer emits. Exotic blocks fall through as
    plain text — fine, we only need a textual source to polish/translate.
    """
    parts: list[str] = []
    if isinstance(body, list):
        for block in body:
            if not isinstance(block, dict) or block.get("_type") != "block":
                continue
            style = block.get("style", "normal")
            text = "".join(
                c.get("text", "")
                for c in block.get("children", [])
                if isinstance(c, dict)
            )
            if style == "h2":
                parts.append(f"## {text}")
            elif style == "h3":
                parts.append(f"### {text}")
            else:
                parts.append(text)
    elif isinstance(body, str):
        parts.append(body)
    return "\n\n".join(p for p in parts if p)


def _markdown_to_portable_text(md: str) -> list[dict]:
    """Naïve markdown → Portable Text. Handles paragraphs + ``##`` / ``###``.

    Faithful to the structure the writer emits (paragraphs separated by
    blank lines, H2/H3 prefixes). More exotic markdown (lists, links)
    falls through as plain paragraph text — acceptable because the
    polish/translate stage almost never introduces those.
    """
    blocks: list[dict] = []
    for chunk in md.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        style = "normal"
        text = chunk
        if chunk.startswith("## "):
            style = "h2"
            text = chunk[3:].strip()
        elif chunk.startswith("### "):
            style = "h3"
            text = chunk[4:].strip()
        blocks.append(
            {
                "_type": "block",
                "style": style,
                "children": [{"_type": "span", "text": text, "marks": []}],
                "markDefs": [],
            }
        )
    return blocks
