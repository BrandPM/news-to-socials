"""Read / write per-language banned phrases inside ``brand.voice_profile_yaml``.

IT_PROJ_NTS_072. The banned phrases that GENERATION actually uses live in the
voice profile under ``voice.<lang>.banned_phrases`` (read by
``comment_writer.parse_voice_guardrails`` per language). The Settings page used
to edit the unrelated flat ``PipelineConfig.banned_phrases`` column, so a
manager's edits never reached generation. These helpers let the admin edit the
real per-language lists, preserving the rest of the YAML.

Pure + YAML-aware (the profile carries no comments on prod, so a
``safe_load → mutate → safe_dump`` round-trip is content-safe). We read the
RAW ``voice.<lang>.banned_phrases`` (no EN/flat fallback) so the editor shows
exactly what is stored for that language — empty when nothing is set yet.
"""

from __future__ import annotations

import yaml


def read_banned_by_language(
    voice_profile_yaml: str, languages: list[str]
) -> dict[str, list[str]]:
    """Return ``{lang: [phrases]}`` for each requested language (raw, no
    fallback). Malformed / missing → empty lists."""
    out: dict[str, list[str]] = {lang: [] for lang in languages}
    try:
        data = yaml.safe_load(voice_profile_yaml or "") or {}
    except yaml.YAMLError:
        return out
    if not isinstance(data, dict):
        return out
    voice = data.get("voice")
    if not isinstance(voice, dict):
        return out
    for lang in languages:
        section = voice.get(lang)
        if isinstance(section, dict):
            phrases = section.get("banned_phrases")
            if isinstance(phrases, list):
                out[lang] = [str(p) for p in phrases if p]
    return out


def write_banned_for_language(
    voice_profile_yaml: str, language: str, phrases: list[str]
) -> str:
    """Return a new voice YAML with ``voice.<language>.banned_phrases`` set to
    ``phrases`` (de-duped, order-preserving). Everything else — other
    languages, ``voice_principles``, ``topics_relevant``, ``style_examples``,
    ``glossary`` — is preserved. Creates the ``voice`` / ``voice.<lang>``
    sections if absent.

    Raises ``yaml.YAMLError`` only on an unparseable input profile (callers
    surface that as a 400/500); a valid-but-empty profile yields a minimal
    ``{voice: {<lang>: {banned_phrases: [...]}}}``.
    """
    data = yaml.safe_load(voice_profile_yaml or "") or {}
    if not isinstance(data, dict):
        data = {}
    voice = data.get("voice")
    if not isinstance(voice, dict):
        voice = {}
        data["voice"] = voice
    section = voice.get(language)
    if not isinstance(section, dict):
        section = {}
        voice[language] = section

    seen: set[str] = set()
    clean: list[str] = []
    for p in phrases:
        s = str(p).strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    section["banned_phrases"] = clean

    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
