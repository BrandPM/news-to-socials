"""Read / write cover-image style prompts inside ``brand.voice_profile_yaml``.

NTS_075 L3. Cover-image styles that generation actually uses live in the voice
profile under ``image.style_prompts`` (read by
``comment_writer.parse_image_style_prompts`` → ``run._resolve_brand_image_styles``).
These helpers let the admin edit that list from Settings, preserving the rest of
the YAML — the same pattern as :mod:`pipeline.admin.voice_banned` for banned
phrases. Image styles are brand-wide (language-agnostic): one cover per topic is
shared across language siblings (NTS_069), so there is no per-language split.

Pure + YAML-aware (``safe_load → mutate → safe_dump`` round-trip).
"""

from __future__ import annotations

import yaml


def read_image_styles(voice_profile_yaml: str) -> list[str]:
    """Return the raw ``image.style_prompts`` list (no default fallback).

    The editor shows exactly what is stored — empty when nothing is set yet,
    in which case generation falls back to the built-in default set. A
    top-level ``image_style_prompts`` list is also accepted (back-compat with
    the flat shape). Malformed / missing → ``[]``.
    """
    try:
        data = yaml.safe_load(voice_profile_yaml or "") or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    image = data.get("image")
    if isinstance(image, dict) and isinstance(image.get("style_prompts"), list):
        return [str(s) for s in image["style_prompts"] if s and str(s).strip()]
    flat = data.get("image_style_prompts")
    if isinstance(flat, list):
        return [str(s) for s in flat if s and str(s).strip()]
    return []


def write_image_styles(voice_profile_yaml: str, styles: list[str]) -> str:
    """Return a new voice YAML with ``image.style_prompts`` set to ``styles``
    (trimmed, de-duped, order-preserving). Everything else in the profile —
    ``voice``, ``voice_principles``, ``topics_relevant``, ``glossary`` — is
    preserved. Creates the ``image`` section if absent.

    Raises ``yaml.YAMLError`` only on an unparseable input profile (callers
    surface that as a 4xx).
    """
    data = yaml.safe_load(voice_profile_yaml or "") or {}
    if not isinstance(data, dict):
        data = {}
    image = data.get("image")
    if not isinstance(image, dict):
        image = {}
        data["image"] = image

    seen: set[str] = set()
    clean: list[str] = []
    for s in styles:
        v = str(s).strip()
        if v and v not in seen:
            seen.add(v)
            clean.append(v)
    image["style_prompts"] = clean

    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
