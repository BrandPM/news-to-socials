# ADR-009 — Languages: Icon RU/UK/EN/PL, wire brands EN only

**Status:** Accepted
**Date:** 2026-05-11

## Context

Icon serves four markets (RU/UK/EN/PL); the wire brands (Neovox, Creolix,
Vilatrix, Nexora) are EN-only for the MVP. Generating 4× content for Icon
quadruples cost and complexity — worth it only if the four-language
posture pays back in audience growth.

## Decision

* **Icon:** 4 languages on the blog (`/blog/{ru,uk,en,pl}`), Telegram/FB/IG
  in EN only. Same news peg → 4 separate `posts` rows (one per language).
* **Wire brands:** EN only on every channel.
* **Multilingual generation:** `asyncio.gather` over the 4 languages with
  a semaphore (cap concurrent Claude calls at 2-3 to avoid rate limits).
* **Voice profile:** the YAML has a `voice` root with sub-keys per
  language (`voice.ru`, `voice.uk`, `voice.en`, `voice.pl`). If a brand
  is single-language, only `voice.en` is filled.
* **Hreflang:** Icon blog pages declare `<link rel="alternate" hreflang="..." />`
  for cross-language SEO.

## Consequences

* **Pro:** Icon captures Russian/Ukrainian/Polish audiences without
  translation overhead — each language gets a native-feeling original
  post, not an MT translation.
* **Pro:** the per-language `voice.xx` block lets us tune formality and
  examples without sharing them across languages.
* **Con:** Icon's Claude bill is ~4× the wire brands'. Mitigated by the
  fact that Haiku draft is cheap and Sonnet polish is bounded by topic count.
* **Con:** dedup must operate per (brand, language) — same news peg
  produces 4 outputs, those are not duplicates of each other.

## When to revisit

If after Stage 7 observation Icon's UA/PL posts get <5% of Icon's total
engagement after 2 months, drop them and write a new ADR.
