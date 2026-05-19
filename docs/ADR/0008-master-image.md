# ADR-008 — One master image, resized per channel

**Status:** Accepted
**Date:** 2026-05-11

## Context

Four channels need four image aspect ratios:

| Channel   | Size       | Aspect |
|-----------|------------|--------|
| blog      | 1792×1008  | 16:9   |
| facebook  | 1200×628   | ~1.91:1 |
| instagram | 1080×1080  | 1:1    |
| telegram  | 1280×720   | 16:9   |

Two paths:
1. **Generate four images per post** with four prompts. ~$0.16/post,
   visually inconsistent across channels.
2. **Generate one master image, resize to four formats locally.**
   ~$0.04/post, visually consistent.

## Decision

Path 2. One Flux Pro generation per post; `pipeline/generator/image_resizer.py`
center-crops the master to each target size using `PIL.ImageOps.fit`.

## Consequences

* **Pro:** 4× cheaper image bill.
* **Pro:** the post looks like it's "from the same campaign" across channels.
* **Pro:** the resizer is pure-Python, deterministic, easy to test
  (`tests/unit/test_image_resizer.py`).
* **Con:** the master must compose well at multiple aspect ratios. The
  `image_style_prompt` field in voice profile reminds the operator to
  favor balanced compositions ("avoid hard-edge-heavy framing").
* **Con:** for IG (1:1) we crop horizontally, which can clip wide
  compositions. If this becomes a problem, we add a second master at
  1:1 (fall back to path 1 for IG specifically).

## Storage

Master and four resized variants are uploaded to Directus `/files`. The
`posts.image_url` points to the channel-specific resized version; the
master is kept for reuse if we ever republish or repurpose.
