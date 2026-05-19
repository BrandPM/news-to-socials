# ADR-013 — Two-stage LLM: gpt-4o-mini draft → gpt-4o polish

**Status:** Accepted (revised 2026-05-18 to switch from Claude to OpenAI;
                    supersedes original Anthropic-based decision)
**Date:** 2026-05-11 (original) / 2026-05-18 (OpenAI revision)

## Context

A single gpt-4o pass produces good prose but is moderately expensive at
~$0.015 per post (input + output combined for 300-word output). A single
gpt-4o-mini pass is very cheap (~$0.001) but tends to fall into recognisable
patterns — over-uses transition phrases, uniform sentence length, em-dash
abuse.

## Decision

Two-stage generation:

1. **Draft** (gpt-4o-mini) — produces a structured draft against the
   voice profile.
2. **Polish** (gpt-4o) — rewrites for natural prose; the prompt
   is informed by what `anti_ai_check.py` flagged in the draft.

Cost per post: ~$0.005 average across 5 brands and 4 languages (estimate
based on ~600 input tokens + ~400 output tokens per stage × 5 languages).

## Consequences

* Bounded cost growth even at 100+ posts/day across brands.
* Anti-AI check feedback loops into the polish prompt, not used as a
  hard gate — gates create regenerate loops that explode cost on hard
  topics.
* `comment_writer.py` is the only place that calls gpt-4o on a per-post
  basis; everything else (relevance scoring, dedup embeddings) stays on
  cheaper models to keep the bill bounded.
* When evaluating new models, swap the constants at the top of
  `comment_writer.py` — no other module knows which model is used.

## History

Original 2026-05-11 decision was: Claude Haiku 4.5 draft → Claude Sonnet 4.6
polish. Switched to OpenAI on 2026-05-18 — see ADR-017 for the reasoning.

The two-stage approach itself remains valid; only the model strings changed.
