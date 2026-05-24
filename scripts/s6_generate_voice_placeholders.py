"""S6.5 — generate placeholder voice content for Icon's RU/UK/PL via GPT-4o.

Loads OPENAI_API_KEY from the news-to-socials .env, calls gpt-4o once per
target language with a structured prompt asking for language-specific
banned_phrases + style_examples, marks each block with placeholder: true,
combines into a single per-language YAML under `voice:`, prints the
result to stdout. Caller pipes this into a PUT /brands/1 (or writes it
directly into admin.db on the VPS).

Run from /Users/brandpro/Projects/news-to-socials with the .env loaded.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Load .env for OPENAI_API_KEY (script sits in scripts/ at the repo root).
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


EN_PROFILE = {
    "banned_phrases": [
        "in today's fast-paced", "ever-evolving", "ever-changing",
        "navigate the landscape", "navigate the complexities",
        "in the realm of", "in the world of", "harness the power of",
        "unlock the potential", "at the forefront of", "delve into",
        "moreover", "furthermore", "it's important to note",
        "it is important to note", "in conclusion", "in summary",
        "strategic perspectives on", "enhancing wealth management",
        "robust framework", "comprehensive approach", "tailored solutions",
        "bespoke solutions", "cutting-edge", "in an increasingly",
        "paradigm shift",
    ],
    "style_examples_good": [
        "The proposal moves the discussion, not the timeline.",
        "Trust planning rarely fails on tax. It fails on family.",
        "A 50bp move in base rates is not the story. The story is who can refinance and who cannot.",
        "For a family with operating assets in three jurisdictions, the question is not whether to restructure, but when the cost of not restructuring exceeds the cost of doing it.",
        "India's new credit-fund regime will reprice mezzanine paper before it reprices senior. Allocators who set their yield assumptions last quarter should revisit them this one.",
    ],
    "style_examples_bad": [
        "In today's fast-paced world of wealth management, it is important to note that the landscape is ever-evolving.",
        "Icon believes in harnessing the power of strategic perspectives to navigate the complexities of cross-border structuring.",
        "This article will delve into the comprehensive framework that enables families to unlock the potential of their wealth.",
        "Moreover, our cutting-edge solutions provide robust frameworks for navigating the ever-changing landscape.",
    ],
}


LANGUAGE_NAMES = {
    "ru": "Russian",
    "uk": "Ukrainian",
    "pl": "Polish",
}

GLOSSARY_HINTS = {
    "ru": "Use canonical financial Russian. 'wealth management' → 'управление капиталом' (NOT 'управление благосостоянием'). 'family office' → 'family-офис' (anglicism is accepted). 'private banking' → 'частный банкинг'. 'cross-border' → 'трансграничный'.",
    "uk": "Use canonical financial Ukrainian. 'wealth management' → 'управління капіталом'. 'family office' → 'family-офіс'. 'private banking' → 'приватний банкінг'. 'cross-border' → 'транскордонний'. Avoid russianisms.",
    "pl": "Use canonical financial Polish. 'wealth management' → 'zarządzanie majątkiem'. 'family office' → 'biuro rodzinne' or 'family office'. 'private banking' → 'bankowość prywatna'. 'cross-border' → 'transgraniczny'.",
}


PROMPT_TEMPLATE = """\
You are adapting Icon Finance's English voice profile to {language_name}.
Icon is a wealth-management partner for HNWI / family offices.

Glossary hints: {glossary}

Output STRICT JSON with this exact shape:

{{
  "banned_phrases": ["...", "..."],
  "style_examples_good": ["...", "..."],
  "style_examples_bad": ["...", "..."]
}}

Requirements:
- 8 banned_phrases: AI-tell clichés in {language_name} (e.g. the {language_name} equivalents of 'delve into', 'ever-evolving', 'navigate the landscape', 'moreover'). Include 2-3 specifically common to {language_name} that don't translate from English.
- 5 style_examples_good: native, idiomatic {language_name} sentences in Icon's voice. Mirror the cadence of the EN good examples: short, specific, name a consequence, ≤25 words each.
- 4 style_examples_bad: native {language_name} sentences using the banned clichés you just listed, so the polish prompt knows what to avoid.

EN reference for tone (do NOT translate literally — adapt to {language_name}'s register):

Good examples:
{good_en}

Bad examples (these are what we DON'T want, in any language):
{bad_en}

Return ONLY the JSON object.
"""


def generate_for_language(code: str) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        language_name=LANGUAGE_NAMES[code],
        glossary=GLOSSARY_HINTS[code],
        good_en="\n".join(f"  - {s}" for s in EN_PROFILE["style_examples_good"]),
        bad_en="\n".join(f"  - {s}" for s in EN_PROFILE["style_examples_bad"]),
    )
    resp = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    content = resp.choices[0].message.content or "{}"
    data = json.loads(content)
    return data


def build_yaml(per_lang: dict[str, dict]) -> str:
    """Assemble the per-language voice profile YAML. We hand-write the YAML
    rather than yaml.dump it so the file looks like the existing flat
    profile and Andriy can edit it later without surprises."""
    import yaml

    voice_block = {
        "en": {
            "banned_phrases": EN_PROFILE["banned_phrases"],
            "style_examples": {
                "good": EN_PROFILE["style_examples_good"],
                "bad": EN_PROFILE["style_examples_bad"],
            },
        }
    }
    for code in ("ru", "uk", "pl"):
        section = per_lang[code]
        voice_block[code] = {
            "placeholder": True,  # ← Andriy will replace manually
            "banned_phrases": section["banned_phrases"],
            "style_examples": {
                "good": section["style_examples_good"],
                "bad": section["style_examples_bad"],
            },
        }

    full = {
        "mission": "Wealth-management partner for international families and entrepreneurs.",
        "audience": "HNWI, family office principals, founders post-exit.",
        "tone": {
            "formality": "high-but-warm",
            "first_person": "brand_name",
            "emoji_allowed": False,
        },
        "voice_principles": [
            "Lead with a specific consequence, not a general observation.",
            "Name the mechanism: who is repriced, who is exposed, who absorbs the cost.",
            "One concrete number or named entity per paragraph, not vague intensifiers.",
            "Address the reader as someone already inside the conversation, not someone being briefed.",
            "End on what changes for the reader's next decision, not a restatement.",
            "Short sentences. Vary length. Do not chain three clauses with em-dashes.",
        ],
        "topics_relevant": [
            "cross-border tax structuring",
            "family office operations",
            "international wealth transfer",
            "investment-grade product launches",
            "M&A relevant to private capital",
        ],
        "topics_banned": [
            "crypto speculation",
            "retail trading",
            "day-trading systems",
        ],
        "voice": voice_block,
    }
    return yaml.safe_dump(full, allow_unicode=True, sort_keys=False, width=120)


def main() -> int:
    out: dict[str, dict] = {}
    for code in ("ru", "uk", "pl"):
        print(f"[generate] {code}…", file=sys.stderr, flush=True)
        out[code] = generate_for_language(code)
    yaml_text = build_yaml(out)
    print(yaml_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
