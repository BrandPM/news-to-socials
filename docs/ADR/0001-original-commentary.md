# ADR-001 — Original commentary, not news rewriting

**Status:** Accepted
**Date:** 2026-05-11

## Context

The system processes news pegs from third-party sources and publishes
content on five brand channels. Two approaches were considered:

1. **Rewrite** the source article in the brand's voice.
2. **Comment** on the news peg with the brand's original perspective.

## Decision

Take approach 2. We never reproduce more than ≤15 words from a source
article, and the post is structurally an opinion/analysis piece, not a
news summary.

## Consequences

* No copyright risk (we don't reproduce source text).
* Quality bar is higher: bland summaries are unacceptable, the post must
  have a perspective. The voice profile (ADR-007) and two-stage LLM
  (ADR-013) exist to enable this.
* The dedup step works against our own posts, not against source
  duplicates — two outlets covering "Visa launched X" still result in
  one Icon post on that peg.
* SEO posture is "expert commentary" rather than "news desk" — slower
  ranking growth but more durable.
