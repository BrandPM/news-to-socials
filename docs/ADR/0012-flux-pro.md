# ADR-012 — Flux 1.1 Pro via Replicate for image generation

**Status:** Accepted (provisional — final on Stage 1 Day 4 after bake-off)
**Date:** 2026-05-11

## Context

We need an image-generation API that:
* produces non-stock visuals consistent with five different brand looks
* costs <$0.05/image at our volume (200 posts/month → ~$10/month)
* exposes a stable HTTP API (no proprietary SDK lock-in)
* respects negative prompts (we must banish text, watermarks, signatures)

Candidates: Replicate (Flux 1.1 Pro), Google Vertex Imagen 3, OpenAI DALL-E 3,
Stable Diffusion via self-host, Midjourney (no API).

## Decision

**Flux 1.1 Pro via Replicate** (`black-forest-labs/flux-1.1-pro`).

Settings used in `pipeline/generator/image.py`:
* `aspect_ratio: "16:9"` for the master (we crop down per channel — ADR-008)
* `output_format: "png"`, `output_quality: 90`
* `safety_tolerance: 2` (standard)
* Negative prompt includes "text, watermark, logo, signature, blurry,
  low quality, deformed, extra fingers, jpeg artifacts"

## Why not the alternatives

* **Imagen 3** — comparable quality, but Google Cloud auth adds an extra
  service account to manage. Possible swap if Flux pricing changes.
* **DALL-E 3** — limited aspect-ratio control, generally weaker on
  abstract / non-photographic styles.
* **Self-hosted SDXL/Flux** — needs a GPU node we don't have; defeats
  the "single CX32 VPS" architecture.
* **Midjourney** — no programmatic API.

## Bake-off plan (Stage 1 Day 4)

Run all 5 brand `image_style_prompts` × 2 topics × {Flux Pro, Imagen 3}.
Operator scores subjectively on:
* on-brand visual style
* compositional quality at 16:9
* absence of text artefacts (the killer for our use case)

Record results in `docs/ADR/0014-image-generator.md` (the bake-off report).
If Flux wins → this ADR stays Accepted. If Imagen wins → this ADR is
Superseded by ADR-014; swap the model string in `image.py`.

## Consequences

* **Pro:** Replicate API is single-call, no async webhooks needed for
  our turn-around (~5-10s per image).
* **Pro:** Flux's negative-prompt handling is the strongest we tested
  for "no embedded text" (critical — we add captions/CTAs ourselves).
* **Con:** Replicate billing is post-paid; we set up usage alerts via
  monitoring (`check_llm_budget` extends to Replicate too).
* **Con:** model version pinning means we may miss free upgrades when
  flux-1.2-pro lands. Acceptable; pin and re-evaluate quarterly.
