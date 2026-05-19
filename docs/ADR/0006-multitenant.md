# ADR-006 — Multi-tenant by `brand_id` parameter, single deployment

**Status:** Accepted
**Date:** 2026-05-11

## Context

Five brands run on the same hardware. Two architectures:

1. **One deployment per brand.** Clean isolation, 5× the ops surface.
2. **One deployment, all brands routed by `brand_id`.** Single process,
   shared dedup index per (brand, language), shared budget.

For our scale (5 brands, ~200 posts/month total) the second wins on every
axis — cost, complexity, monitoring, debugging.

## Decision

Every module that touches per-brand behaviour takes `brand_id` as a
parameter; never hard-codes "icon". Specifically:

* `pipeline/sources/*` — sources are shared; `topic_picker` filters per brand.
* `pipeline/selector/dedup.py` — `seen` rows key on `(brand_id, language, hash)`.
* `pipeline/generator/*` — `comment_writer.write(topic, voice_profile_yaml, language)`.
* `pipeline/adapter/*` — pure functions, no brand state.
* `pipeline/publisher/*` — `dispatch(post, route)`, route has brand-specific account_ref.

## Consequences

* **Pro:** adding a new brand is a Directus row + voice profile, no code change.
* **Pro:** one .env, one VPS, one set of API keys.
* **Pro:** cost monitoring per brand by querying audit_log.
* **Con:** a bad voice profile for one brand can affect Claude's budget
  for all. Mitigated by per-brand `topics_banned` and the daily LLM
  budget alert.
* **Con:** harder to give a single brand limited access (no separate
  Directus role per brand). Acceptable: Andriy is the only operator.

## Anti-patterns to avoid

* Don't put brand names in `if` statements anywhere.
* Don't store brand-specific prompts in code — they live in
  `brands.voice_profile_yaml`.
* Don't hard-code account IDs — they live in `channels.account_ref`.
