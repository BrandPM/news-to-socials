"""Two-stage commentary writer (ADR-013, OpenAI-only — ADR-017).

Stage 1 -- gpt-4o-mini produces a fast first draft against the brand
voice-profile.
Stage 2 -- gpt-4o polishes the draft, targeting natural prose and
removing AI tells. The polish prompt is informed by what the anti-AI check
found in the draft.

Why two stages: gpt-4o-mini gives us speed and cost (~$0.001/post in our
volumes), but tends to fall into patterns. gpt-4o costs ~17x more but only
runs once per accepted topic, so the bill stays bounded.

Pattern reference: fin-thread composes news in a single OpenAI call
(/research/fin-thread/composer/composer.go). We deliberately split into two
because their output (1-2 sentence headline) and ours (200-400 word original
commentary) have very different quality requirements.

When to revisit: if gpt-4o output starts feeling generic, swap polish_model
to "gpt-4o-2024-11-20" or the latest snapshot. The constants below are the
only place that knows which model is in use.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from openai import AsyncOpenAI
from pydantic import BaseModel

from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import Draft, Language, Topic
from ..common.retry import with_retry
from .anti_ai_check import close_lacks_anchor, find_banned_phrase_hits, score_ai_tells

log = get_logger(__name__)


class _DraftJSON(BaseModel):
    """Wire format we ask the model to return."""

    title: str
    body: str
    key_takeaway: str = ""


# Leading markdown block markers (heading #, blockquote >, list -/*/+) that the
# polish/translate pass sometimes leaks into the *title*. The body carries the
# markdown; the title must be plain text. We require trailing whitespace so we
# never eat a legitimate "#1 ranking" or a hyphenated first word.
_TITLE_LEADING_MARKER = re.compile(r"^\s*(?:#{1,6}|>|[-*+])\s+")
# Inline emphasis wrappers: **bold**, __bold__, *italic*, _italic_.
_TITLE_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_TITLE_ITALIC = re.compile(r"\*(.+?)\*|_(.+?)_")


def sanitize_title(title: str) -> str:
    """Strip stray markdown from an LLM-produced title — the last line of defence.

    Translation/polish passes (non-English especially) sometimes return the
    title with leading heading markers ("## ", "# "), bold (``**...**``),
    backticks, or list bullets even though the title field should be plain
    text. This runs on every parsed draft, for every language, before the
    title is ever persisted to Sanity or the admin DB (NTS_060).

    Idempotent: a clean title passes through unchanged.
    """
    if not title:
        return title
    text = title.strip()
    # Peel leading block markers, possibly stacked ("## > foo"). Loop until stable.
    prev = None
    while prev != text:
        prev = text
        text = _TITLE_LEADING_MARKER.sub("", text).strip()
    # Drop backticks entirely (inline code has no place in a title).
    text = text.replace("`", "")
    # Unwrap emphasis: keep the inner text, drop the markers.
    text = _TITLE_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _TITLE_ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    # Collapse any whitespace the stripping opened up.
    text = re.sub(r"\s+", " ", text).strip()
    return text


_LANGUAGE_NAMES = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "pl": "Polish",
}


def _language_name(language: str | Language) -> str:
    """Map a 2-letter code to the language name we use inside the prompt.

    The prompts ship the *name* rather than the code because the LLM
    follows "Write everything in Russian" much more reliably than
    "Language: ru" — see NTS_022 (Variant A) for the test results behind
    that choice.
    """
    code = language.value if isinstance(language, Language) else str(language)
    return _LANGUAGE_NAMES.get(code, code)


_DRAFT_PROMPT = """\
OUTPUT LANGUAGE: {language_name}. Write the title, body, and key
takeaway in {language_name} only. Do not switch to English unless
quoting directly from the source (≤15 words per quote).

You write an original expert commentary from the brand below on the news peg.
This is NOT a rewrite. The news peg is just an excuse to share the brand's
informed perspective.

Brand voice profile (YAML):
{voice_profile_yaml}

News peg:
  Title: {title}
  Source URL: {url}
  Summary: {summary}

Language: {language}
Audience: people in the brand's target segment, NOT general public.
Length: 250-400 words.

SPECIFICITY (mandatory — highest priority):
* Every claim must be specific to THIS story — tied to a concrete fact,
  number, named entity, or mechanism from the news peg above.
* BAN any sentence that could be pasted into an article on a completely
  different topic: vague intensifiers and generic risk/urgency statements.
  Examples of the BANNED generic shape: "rising uncertainty creates
  challenges", "could cause serious damage", "requires immediate decisions".
  Replace each with the specific who / what / how-much from this story.
* One concrete number or named entity per paragraph — not vague
  intensifiers. Lead with a specific consequence, not a general framing.

NO REPETITION & DENSITY (mandatory):
* No sentence may restate a previous one. Every paragraph must add NEW
  specifics. Delete any sentence that carries no new information — but
  tighten, do not gut: keep the lede + H2 structure and a comparable length.

AUDIENCE LINK (mandatory):
* Connect the story to the brand's target segment using `topics_relevant`
  from the voice profile above. If the link isn't obvious, make it explicit
  in the body. Do NOT decline the topic or truncate the piece — topic
  selection happens upstream.

NO INVENTION (mandatory):
* Numbers, dates, and names may come ONLY from the source. Never invent
  them. If the source lacks a figure, write the piece without it — do not
  fabricate a statistic, date, or name to fill a gap.

STRUCTURE REQUIREMENTS (mandatory — markdown headings, not bold):
* Open with a 1-2 sentence lede paragraph that names the specific
  consequence, NOT a general framing. No heading on the lede.
* Then 2-3 H2 sections (`## Heading`). Heading names must be
  substantive and describe the actual content
  (e.g. "## The repricing of mezzanine credit", NOT
  "## What this means" or "## Key takeaways").
* End with a forward-looking close that is ANCHORED to the article: it must
  reference a specific named entity, number, or mechanism already mentioned
  in the body, and state the concrete shift it creates for the reader's next
  decision ON THIS topic. The final paragraph must answer: "So what does
  this mean specifically?" — referencing a concrete fact / number / entity /
  consequence from THIS article. Do NOT end on a generic call-to-action or a
  tidy restatement that would fit any article. No "in conclusion"-style wrap-up.
* Source quotes ≤ 15 words. The piece is commentary, not a rewrite.

Rules:
* Strictly follow the voice profile.
* Original perspective, not a summary of the article.
* No filler phrases ("moreover", "furthermore", "it's important to note").
* Em-dashes used sparingly.

VOICE GUARDRAILS:
* Banned phrases — do NOT use any of these (case-insensitive):
{banned_phrases}

TITLE FORMAT (mandatory): the title is PLAIN TEXT only — no markdown.
Never start it with ``#`` or ``##``, never wrap it in ``**bold**`` or
backticks, never make it a list item. Markdown belongs in ``body``, not
in ``title``.

Return ONLY a JSON object: {{"title": "...", "body": "...", "key_takeaway": "..."}}
where ``body`` is markdown including the H2 headings.
"""


_POLISH_PROMPT = """\
OUTPUT LANGUAGE: {language_name}. The polished output must remain in {language_name}. Do NOT translate or shift to English.

Rewrite this draft to sound more natural and less AI-generated, preserving
its meaning. Pay special attention to these tells found in the draft:
{ai_tells}

SPECIFICITY (mandatory — highest priority):
* Every claim must stay specific to THIS story — tied to a concrete fact,
  number, named entity, or mechanism from the draft. Cut or rewrite any
  sentence that could be pasted into an article on a different topic (vague
  intensifiers, generic risk/urgency statements). Examples of the BANNED
  generic shape: "rising uncertainty creates challenges", "could cause
  serious damage", "requires immediate decisions" — replace each with the
  specific who / what / how-much already present in the draft.
* Voice principles to enforce (from the brand profile):
{voice_principles}

NO REPETITION & DENSITY (mandatory):
* No sentence may restate a previous one. Every paragraph must add NEW
  specifics. Delete any sentence that carries no new information — but
  tighten, do not gut: keep the lede + H2 structure and a comparable length.

AUDIENCE LINK (mandatory):
* Connect the story to the brand's target segment, drawn from these
  topics_relevant:
{topics_relevant}
  If the link isn't obvious in the draft, make it explicit in the body. Do
  NOT decline or truncate — topic selection happens upstream.

NO INVENTION (mandatory):
* Numbers, dates, and names may come ONLY from the source draft. Never
  invent them; if a figure isn't present, write without it — do not
  fabricate one to fill a gap.

STRUCTURE REQUIREMENTS (preserve / enforce — markdown, not bold):
* The piece must open with a 1-2 sentence lede paragraph (no heading).
* Then 2-3 H2 sections (`## Heading`). Substantive heading names that
  describe the section content, e.g. "## The repricing of mezzanine
  credit" — NEVER "## What this means", "## Conclusion", "## Key
  takeaways", "## Overview", or other content-free labels.
* End with a forward-looking close ANCHORED to the article: it must
  reference a specific named entity, number, or mechanism already in the
  body and state the concrete shift for the reader's next decision ON THIS
  topic. The final paragraph must answer: "So what does this mean
  specifically?" — referencing a concrete fact / number / entity /
  consequence from THIS article. NOT a generic call-to-action or a
  restatement that fits any article. No "in conclusion" restatement.
* If the draft is one flat block, restructure it into lede + H2
  sections + close while keeping the meaning.

VOICE GUARDRAILS:
* Banned phrases — do NOT use any of these (case-insensitive):
{banned_phrases}
* Examples of the voice we want (mirror this register and cadence):
{good_examples}

TITLE FORMAT (mandatory): the title is PLAIN TEXT only — no markdown.
Never start it with ``#`` or ``##``, never wrap it in ``**bold**`` or
backticks. Markdown (the H2 headings) belongs in ``body``, not ``title``.

Draft:
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
where ``body`` is markdown with H2 headings.
"""


_BANNED_PHRASE_RETRY_PROMPT = """\
OUTPUT LANGUAGE: {language_name}. The rewrite must remain in {language_name}. Do NOT translate.

The previous polish still uses the following banned phrases:
{hit_phrases}

Rewrite the draft to remove ALL of these phrases. Preserve meaning,
structure (lede + 2-3 H2 sections + forward-looking close), and the
voice. Use specific concrete language instead of these clichés.

VOICE GUARDRAILS (still apply):
* Banned phrases — do NOT use any of these (case-insensitive):
{banned_phrases}
* Examples of the voice we want:
{good_examples}

TITLE FORMAT (mandatory): the title is PLAIN TEXT only — no markdown,
no leading ``#``/``##``, no ``**bold**`` or backticks.

Draft:
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
"""


_TRANSLATE_PROMPT = """\
OUTPUT LANGUAGE: {language_name}. Write the title, body, and key takeaway
in {language_name} only.

You are a faithful translator. Translate the English article below into
{language_name}. This is a TRANSLATION, not a rewrite and not a new piece
of writing. The English version is canonical and authoritative.

ABSOLUTE FIDELITY RULES (highest priority — override everything else):
* Do NOT invent, add, or introduce ANY fact, statistic, percentage,
  figure, date, name, or claim that is not present in the English source.
* Do NOT drop or omit any fact, number, or claim that IS present.
* Every number, percentage, currency amount and proper noun must appear in
  the translation with the SAME numeric value (e.g. "67%" stays "67%",
  "$2.4m" stays "$2.4m"). Localise only the surrounding words, never the
  figures themselves.
* Preserve the EXACT structure: the same opening lede paragraph (no
  heading), the SAME number of H2 sections in the SAME order, and the same
  forward-looking closing paragraph. Each `## Heading` in the source must
  become exactly one `## Heading` in the translation (translated text,
  still an H2). Do not add, merge, split, or reorder sections.
* Keep a comparable length — within roughly ±15% of the source. Do not
  expand with commentary or compress by summarising.
* Source quotes stay quotes; do not paraphrase a quote into a new claim.

LOCALISATION (lower priority — apply only without violating the rules above):
* Make the {language_name} read naturally and idiomatically — this is the
  ONLY thing the voice guardrails below are for. They license you to choose
  natural phrasing, NOT to add, drop, or alter facts.
* Glossary: keep established domain terms per the brand glossary (e.g.
  "family office" is rendered per glossary, not literally translated). When
  in doubt, prefer the recognised industry term in {language_name}.
* Voice guardrails — avoid these banned phrases / their direct calques
  where a natural alternative exists, but NEVER reword in a way that changes
  meaning or drops a fact just to dodge a phrase:
{banned_phrases}
* Register / cadence to mirror:
{good_examples}

TITLE FORMAT (mandatory): translate the title into {language_name}. The
title is PLAIN TEXT only — no markdown, no leading ``#``/``##``, no
``**bold**`` or backticks. Markdown (the H2 headings) belongs in ``body``.

English source article (JSON):
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
where ``body`` is markdown with the same H2 headings, translated into {language_name}.
"""


_GENERIC_CLOSE_RETRY_PROMPT = """\
OUTPUT LANGUAGE: {language_name}. The rewrite must remain in {language_name}. Do NOT translate.

The final paragraph of this piece reads as a GENERIC close — it does not
reference any specific named entity, number, or mechanism from the body, so
it would fit any article. Rewrite the piece so the CLOSING paragraph is
anchored to THIS story: it must name a specific entity / number / mechanism
already present in the body and state the concrete shift it creates for the
reader's next decision. Do NOT introduce new facts, and do NOT change the
meaning or structure of the rest of the piece (lede + 2-3 H2 sections + close).

Draft:
{draft_json}

Return ONLY a JSON object in the same shape: {{"title": "...", "body": "...", "key_takeaway": "..."}}
"""


_BANNED_PHRASE_RETRY_THRESHOLD = 2

# Placeholders a DB-stored prompt MUST contain (``required``) for the
# generation path to trust it, and the full set it MAY reference
# (``allowed`` = the kwargs we render with). A DB prompt that drops a
# required placeholder or introduces an unknown one is rejected in favour of
# the in-code fallback constant (see ``CommentWriter._resolve_template``) —
# so a bad admin-UI edit degrades to the canonical prompt instead of breaking
# generation with a KeyError.
_REQUIRED_PLACEHOLDERS: dict[str, set[str]] = {
    "writer_draft": {
        "voice_profile_yaml",
        "title",
        "summary",
        "language_name",
        "banned_phrases",
    },
    "writer_polish": {
        "ai_tells",
        "banned_phrases",
        "good_examples",
        "voice_principles",
        "topics_relevant",
        "draft_json",
        "language_name",
    },
    "writer_translate": {
        "draft_json",
        "language_name",
        "banned_phrases",
        "good_examples",
    },
}


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"  - {x}" for x in items)


def _record_openai_cost(resp: Any, *, model: str, operation: str) -> None:
    """Record a cost_records row for an OpenAI ChatCompletion response.

    Safe to call with mocks: missing ``usage`` → cost is 0, row still
    written iff a brand context is active (otherwise no-op). NTS_025 C1.
    """
    from pipeline.admin.cost_recorder import record_cost  # noqa: PLC0415
    from pipeline.common.pricing import openai_cost  # noqa: PLC0415

    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None)
    tokens_out = getattr(usage, "completion_tokens", None)
    record_cost(
        provider="openai",
        operation=operation,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=openai_cost(model, tokens_in, tokens_out),
    )


def parse_voice_guardrails(
    voice_profile_yaml: str,
    language: str | Language = Language.en,
) -> tuple[list[str], list[str]]:
    """Extract ``banned_phrases`` and ``style_examples.good`` for ``language``.

    Returns ``(banned_phrases, good_examples)``. Missing keys → empty lists.
    Malformed YAML is logged and treated as no guardrails so a bad voice
    profile never crashes the pipeline.

    Two YAML shapes are accepted:

    * **Flat** (pre-S6, EN-only)::

          banned_phrases: ["lorem", "ipsum"]
          style_examples:
            good: ["..."]
            bad: ["..."]

      Returned for any ``language`` the caller asks for — every language
      reads the same set so the EN-only deployment still works.

    * **Per-language** (S6+, Icon's RU/UK/PL fanout)::

          voice:
            en: { banned_phrases: [...], style_examples: {good: [...]} }
            ru: { banned_phrases: [...], style_examples: {good: [...]} }
            uk: { banned_phrases: [...], style_examples: {good: [...]} }
            pl: { banned_phrases: [...], style_examples: {good: [...]} }

      Returned for the requested language. If the requested section is
      absent we fall back to the EN section; if EN is also absent we
      fall back to top-level (flat) keys; if those are absent too we
      return empty lists.
    """
    try:
        data = yaml.safe_load(voice_profile_yaml) or {}
    except yaml.YAMLError as exc:
        log.warning("comment_writer.voice_profile_parse_failed", err=str(exc))
        return [], []
    if not isinstance(data, dict):
        return [], []

    lang_key = language.value if isinstance(language, Language) else str(language)
    voice = data.get("voice")
    section: dict | None = None
    if isinstance(voice, dict):
        candidate = voice.get(lang_key)
        if isinstance(candidate, dict):
            section = candidate
        elif lang_key != "en":
            # Fall back to EN within the per-language map before giving up.
            fallback = voice.get("en")
            if isinstance(fallback, dict):
                section = fallback
    if section is None:
        # Flat / legacy shape.
        section = data

    banned = section.get("banned_phrases") or []
    examples = section.get("style_examples") or []
    good: list[str] = []
    if isinstance(examples, dict):
        good = list(examples.get("good") or [])
    elif isinstance(examples, list):
        good = list(examples)

    banned = [str(p) for p in banned if p]
    good = [str(g) for g in good if g]
    return banned, good


def parse_voice_principles(
    voice_profile_yaml: str,
    language: str | Language = Language.en,
) -> list[str]:
    """Extract ``voice_principles`` from the brand voice profile (NTS_067).

    These ("one concrete number or named entity per paragraph, not vague
    intensifiers", "lead with a specific consequence") were only ever
    reaching the draft stage via the wholesale ``{voice_profile_yaml}`` dump;
    the polish stage never saw them. We now parse them explicitly so polish
    can enforce the same anti-generic discipline.

    Precedence: per-language ``voice.<lang>.voice_principles`` if present,
    else top-level ``voice_principles``. Missing / malformed → ``[]``.
    """
    try:
        data = yaml.safe_load(voice_profile_yaml) or {}
    except yaml.YAMLError as exc:
        log.warning("comment_writer.voice_principles_parse_failed", err=str(exc))
        return []
    if not isinstance(data, dict):
        return []

    lang_key = language.value if isinstance(language, Language) else str(language)
    voice = data.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get(lang_key), dict):
        per_lang = voice[lang_key].get("voice_principles")
        if isinstance(per_lang, list) and per_lang:
            return [str(p) for p in per_lang if p]

    principles = data.get("voice_principles")
    if isinstance(principles, list):
        return [str(p) for p in principles if p]
    return []


def parse_topics_relevant(
    voice_profile_yaml: str,
    language: str | Language = Language.en,
) -> list[str]:
    """Extract ``topics_relevant`` (the brand's audience segments) from the
    voice profile (NTS_070).

    The draft stage sees these via the wholesale YAML dump, but polish does
    not — so we parse them explicitly to power the AUDIENCE LINK rule there.
    Precedence: per-language ``voice.<lang>.topics_relevant`` if present, else
    top-level ``topics_relevant``. Missing / malformed → ``[]``.
    """
    try:
        data = yaml.safe_load(voice_profile_yaml) or {}
    except yaml.YAMLError as exc:
        log.warning("comment_writer.topics_relevant_parse_failed", err=str(exc))
        return []
    if not isinstance(data, dict):
        return []

    lang_key = language.value if isinstance(language, Language) else str(language)
    voice = data.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get(lang_key), dict):
        per_lang = voice[lang_key].get("topics_relevant")
        if isinstance(per_lang, list) and per_lang:
            return [str(t) for t in per_lang if t]

    topics = data.get("topics_relevant")
    if isinstance(topics, list):
        return [str(t) for t in topics if t]
    return []


class CommentWriter:
    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        draft_model: str = "gpt-4o-mini",
        polish_model: str = "gpt-4o",
        brand_id_fk: int | None = None,
    ) -> None:
        self.client = client or AsyncOpenAI(api_key=get_settings().openai_api_key)
        self.draft_model = draft_model
        self.polish_model = polish_model
        # NTS_067: when set, draft/polish/translate prompts are sourced from
        # the brand's ACTIVE row in the ``prompts`` table (so admin-UI edits
        # drive generation), with the in-code constant as a safe fallback.
        # When None (tests, ad-hoc), the constant is always used.
        self.brand_id_fk = brand_id_fk

    def _resolve_template(
        self, prompt_type: str, fallback: str, render_kwargs: dict[str, Any]
    ) -> str:
        """Return the prompt template to render: the brand's ACTIVE DB row
        when present and safe, else the in-code ``fallback`` constant.

        Safety (NTS_067): the DB template is used only if every placeholder it
        references is one we supply (``render_kwargs``) AND it still contains
        the required placeholders for its type. A drifted/broken admin-UI edit
        therefore degrades to the canonical constant instead of raising a
        ``KeyError`` mid-generation. Any DB/lookup error also falls back.
        """
        if self.brand_id_fk is None:
            return fallback
        try:
            import string  # noqa: PLC0415

            from sqlalchemy import select  # noqa: PLC0415

            from pipeline.admin import db as admin_db  # noqa: PLC0415
            from pipeline.admin.models import Prompt  # noqa: PLC0415

            factory = admin_db.get_session_factory()
            with factory() as session:
                row = session.scalars(
                    select(Prompt).where(
                        Prompt.brand_id_fk == self.brand_id_fk,
                        Prompt.prompt_type == prompt_type,
                        Prompt.is_active.is_(True),
                    )
                ).first()
            if row is None or not row.content:
                return fallback
            fields = {
                fname
                for _, fname, _, _ in string.Formatter().parse(row.content)
                if fname
            }
            allowed = set(render_kwargs)
            required = _REQUIRED_PLACEHOLDERS.get(prompt_type, set())
            if not (required <= fields <= allowed):
                log.warning(
                    "comment_writer.db_prompt_rejected",
                    prompt_type=prompt_type,
                    missing_required=sorted(required - fields),
                    unknown_placeholders=sorted(fields - allowed),
                )
                return fallback
            return row.content
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "comment_writer.db_prompt_resolve_failed",
                prompt_type=prompt_type,
                err=str(exc),
            )
            return fallback

    async def write(
        self,
        topic: Topic,
        voice_profile_yaml: str,
        language: Language,
    ) -> Draft:
        banned_phrases, good_examples = parse_voice_guardrails(
            voice_profile_yaml, language
        )
        voice_principles = parse_voice_principles(voice_profile_yaml, language)
        topics_relevant = parse_topics_relevant(voice_profile_yaml, language)

        # --- Stage 1: draft (banned phrases injected here too — NTS_067) ---
        draft = await self._draft(topic, voice_profile_yaml, language, banned_phrases)

        # --- Anti-AI check ---
        score, tells = score_ai_tells(draft.body)
        log.info("comment_writer.draft_ai_score", topic=topic.id, score=score, tells=tells)

        # --- Stage 2: polish (always; injects voice guardrails + principles) ---
        polished = await self._polish(
            draft,
            tells,
            banned_phrases,
            good_examples,
            language,
            voice_principles,
            topics_relevant,
        )

        # --- Banned-phrase retry (one extra pass, cap to avoid runaways) ---
        hits = find_banned_phrase_hits(polished.body, banned_phrases)
        if len(hits) > _BANNED_PHRASE_RETRY_THRESHOLD:
            log.info(
                "comment_writer.banned_phrase_retry",
                topic=topic.id,
                hits=hits,
            )
            polished = await self._retry_for_banned_phrases(
                polished, hits, banned_phrases, good_examples, language
            )
            remaining = find_banned_phrase_hits(polished.body, banned_phrases)
            log.info(
                "comment_writer.banned_phrase_retry_done",
                topic=topic.id,
                remaining=remaining,
            )

        # --- Generic-close retry (NTS_067; EN canon only, one extra pass) ---
        # If the closing paragraph cites no number/named-entity from the body,
        # it's a topic-agnostic CTA — re-anchor it once. Gated to English: the
        # heuristic is EN-oriented, and non-EN goes through translate() anyway.
        if language == Language.en and close_lacks_anchor(polished.body):
            log.info("comment_writer.generic_close_retry", topic=topic.id)
            polished = await self._retry_for_generic_close(polished, language)
            log.info(
                "comment_writer.generic_close_retry_done",
                topic=topic.id,
                still_generic=close_lacks_anchor(polished.body),
            )

        return Draft(
            topic_id=topic.id,
            brand_id=topic.brand_id,
            language=language,
            title=polished.title,
            body=polished.body,
            key_takeaway=polished.key_takeaway,
        )

    async def translate(
        self,
        en_draft: Draft,
        language: Language,
        voice_profile_yaml: str,
    ) -> Draft:
        """Translate a finished EN draft into ``language`` (NTS_065).

        The EN draft is canonical. Non-EN languages are an exact, faithful
        translation of it — same structure (H2 set), same facts and
        numbers, comparable length — NOT a fresh native generation from the
        topic. This is the whole point of the NTS_065 rework: native
        per-language generation drifted in structure/length and invented
        facts (e.g. a "67% of clients" stat in RU that EN never had).

        ``voice_profile`` / ``banned_phrases`` for the target language are
        applied ONLY as phrasing localisation (naturalness + glossary), not
        as licence to add or drop content — that constraint lives in the
        prompt and is the reason we do NOT run the banned-phrase rewrite
        loop here (it could shed facts).
        """
        if language == Language.en:
            raise ValueError(
                "translate() is for non-EN targets; EN is the canonical "
                "source and is produced by write()"
            )
        banned_phrases, good_examples = parse_voice_guardrails(
            voice_profile_yaml, language
        )
        source = _DraftJSON(
            title=en_draft.title,
            body=en_draft.body,
            key_takeaway=en_draft.key_takeaway,
        )
        translated = await self._translate(
            source, language, banned_phrases, good_examples
        )
        # Log (don't rewrite) any banned-phrase hits — a faithful
        # translation must not shed facts to dodge a cliché, so we surface
        # them for review instead of triggering the aggressive retry loop.
        hits = find_banned_phrase_hits(translated.body, banned_phrases)
        if hits:
            log.info(
                "comment_writer.translate_banned_hits",
                topic=en_draft.topic_id,
                language=language.value,
                hits=hits,
            )
        return Draft(
            topic_id=en_draft.topic_id,
            brand_id=en_draft.brand_id,
            language=language,
            title=translated.title,
            body=translated.body,
            key_takeaway=translated.key_takeaway,
        )

    @with_retry()
    async def _translate(
        self,
        draft: _DraftJSON,
        language: Language,
        banned_phrases: list[str],
        good_examples: list[str],
    ) -> _DraftJSON:
        kwargs = {
            "language_name": _language_name(language),
            "banned_phrases": _bullet_list(banned_phrases) or "  (none specified)",
            "good_examples": _bullet_list(good_examples) or "  (none specified)",
            "draft_json": draft.model_dump_json(),
        }
        template = self._resolve_template("writer_translate", _TRANSLATE_PROMPT, kwargs)
        prompt = template.format(**kwargs)
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=2000,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(resp, model=self.polish_model, operation="translate")
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _draft(
        self,
        topic: Topic,
        voice_profile_yaml: str,
        language: Language,
        banned_phrases: list[str] | None = None,
    ) -> _DraftJSON:
        kwargs = {
            "voice_profile_yaml": voice_profile_yaml,
            "title": topic.raw.title,
            "url": topic.raw.url,
            "summary": topic.raw.summary[:1000],
            "language": language.value,
            "language_name": _language_name(language),
            "banned_phrases": _bullet_list(banned_phrases or []) or "  (none specified)",
        }
        template = self._resolve_template("writer_draft", _DRAFT_PROMPT, kwargs)
        prompt = template.format(**kwargs)
        resp = await self.client.chat.completions.create(
            model=self.draft_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(resp, model=self.draft_model, operation="draft")
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _polish(
        self,
        draft: _DraftJSON,
        ai_tells: list[str],
        banned_phrases: list[str],
        good_examples: list[str],
        language: Language = Language.en,
        voice_principles: list[str] | None = None,
        topics_relevant: list[str] | None = None,
    ) -> _DraftJSON:
        kwargs = {
            "ai_tells": ", ".join(ai_tells) if ai_tells else "no specific tells noted",
            "banned_phrases": _bullet_list(banned_phrases) or "  (none specified)",
            "good_examples": _bullet_list(good_examples) or "  (none specified)",
            "voice_principles": _bullet_list(voice_principles or [])
            or "  (none specified)",
            "topics_relevant": _bullet_list(topics_relevant or [])
            or "  (none specified)",
            "draft_json": draft.model_dump_json(),
            "language_name": _language_name(language),
        }
        template = self._resolve_template("writer_polish", _POLISH_PROMPT, kwargs)
        prompt = template.format(**kwargs)
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(resp, model=self.polish_model, operation="polish")
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _retry_for_generic_close(
        self, draft: _DraftJSON, language: Language
    ) -> _DraftJSON:
        """One extra pass to re-anchor a generic closing paragraph (NTS_067).

        Uses the in-code retry prompt (not a versioned prompt_type) — it's an
        internal guard, not an operator-tunable template."""
        prompt = _GENERIC_CLOSE_RETRY_PROMPT.format(
            language_name=_language_name(language),
            draft_json=draft.model_dump_json(),
        )
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(
            resp, model=self.polish_model, operation="generic_close_retry"
        )
        return self._parse(resp.choices[0].message.content or "{}")

    @with_retry()
    async def _retry_for_banned_phrases(
        self,
        draft: _DraftJSON,
        hit_phrases: list[str],
        banned_phrases: list[str],
        good_examples: list[str],
        language: Language = Language.en,
    ) -> _DraftJSON:
        prompt = _BANNED_PHRASE_RETRY_PROMPT.format(
            hit_phrases=_bullet_list(hit_phrases),
            banned_phrases=_bullet_list(banned_phrases),
            good_examples=_bullet_list(good_examples) or "  (none specified)",
            draft_json=draft.model_dump_json(),
            language_name=_language_name(language),
        )
        resp = await self.client.chat.completions.create(
            model=self.polish_model,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_openai_cost(
            resp, model=self.polish_model, operation="anti_check_retry"
        )
        return self._parse(resp.choices[0].message.content or "{}")

    @staticmethod
    def _parse(text: str) -> _DraftJSON:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
            obj = _DraftJSON.model_validate(data)
            # Last line of defence: strip any markdown the model leaked into the
            # title (leading ##, **bold**, backticks). Body keeps its markdown.
            obj.title = sanitize_title(obj.title)
            return obj
        except Exception as exc:  # noqa: BLE001
            log.error("comment_writer.parse_failed", raw=text[:200], err=str(exc))
            return _DraftJSON(title="(parse failed)", body=text[:800], key_takeaway="")
