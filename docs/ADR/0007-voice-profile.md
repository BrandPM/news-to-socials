# ADR-007 — Voice profile: AI extraction + human refinement

**Status:** Accepted
**Date:** 2026-05-11

## Context

The system writes commentary "from the brand's voice". That voice has to
come from somewhere. Options:

1. **AI-only.** Scrape the brand site, ask Claude to summarise the tone.
2. **Human-only.** Andriy writes voice profiles from scratch for 5 brands.
3. **Hybrid: AI draft → human refinement.**

Option 1 produces generic profiles ("professional, customer-focused" —
true of every B2B fintech). Option 2 is the bottleneck — 8h × 5 brands
of Andriy time before we can write anything. Option 3 is what we picked.

## Decision

* `pipeline/brand_extractor.py` (Stage 2) scrapes the brand site,
  parses headings/copy/CTAs, and asks Claude Sonnet to produce a
  voice_profile.yaml draft against a strict schema (mission, audience,
  tone.formality, style_examples, visual.image_style_prompts, etc.).
* Output goes into `brands.voice_profile_yaml` in Directus.
* **Andriy reviews and refines manually in the Directus UI** — 1-1.5h
  per brand. The refinement step is non-negotiable; W3 says the AI
  draft is generic without it.
* The refined profile is the input to every subsequent `comment_writer`
  call.

## Required hand-curated fields

These the AI cannot infer correctly from a public site — Andriy fills
them in:

* `tone.first_person` (we / brand_name / none) — depends on brand identity
* `topics_banned` — competitors, sensitive topics, sales-y language
* `style_examples` — at least 2-3 real-world quotes ("here's exactly how we'd phrase this")
* `visual.image_style_prompts` — at least 3 different prompts (W6 mitigation)

## Consequences

* **Pro:** the AI draft makes Andriy's job a *review* task, not a *writing*
  task — 2× faster.
* **Pro:** profiles are versioned in Directus; we can A/B different
  refinements and pick the best.
* **Con:** Stage 2 cannot complete in less than ~8h of Andriy's
  attention. This is on the critical path of the project plan.
* **Con:** voice drift over time is a real risk — see Phase 2 retro.

## Alternative considered

Directus v11.15+ ships an AI Assistant that could replace
`brand_extractor.py` entirely. We left this as a possible substitution —
see [§10 Master Doc / "Доп. идеи"]. The decision tree: if Directus AI
Assistant gives Andriy a good in-UI authoring experience for voice
profiles, drop `brand_extractor.py` and write ADR-016 to record it.
