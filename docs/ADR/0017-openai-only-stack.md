# ADR-017 — OpenAI-only LLM stack (no Anthropic dependency)

**Status:** Accepted
**Date:** 2026-05-18
**Supersedes:** original ADR-013 model choice (Claude Haiku + Sonnet)

## Context

The original architecture used Claude Haiku 4.5 + Sonnet 4.6 for the
two-stage LLM. This required maintaining an Anthropic API account in
addition to OpenAI (which we already need for embeddings).

The operator (Andriy) already has OpenAI tokens with sufficient budget
and prompt experience tuned for GPT models. Adding a second LLM provider
brings:

* Second billing surface to monitor
* Second rate-limit window to think about
* Second prompt engineering style to keep consistent
* Second SDK in the dependency graph

For a single-operator MVP, the operational simplicity of one provider
outweighs the small quality differential between Sonnet and gpt-4o for
the marketing-commentary use case.

## Decision

* **All LLM calls go through OpenAI.** No Anthropic SDK in the project.
* Embeddings: `text-embedding-3-small` (unchanged).
* Scoring: `gpt-4o-mini` ($0.15 / 1M input, $0.60 / 1M output).
* Draft generation: `gpt-4o-mini` (same model, cheaper than scoring would
  imply because of bounded output).
* Polish: `gpt-4o` ($2.50 / 1M input, $10 / 1M output) — the quality stage.

## Consequences

* **Pro:** one API key, one billing dashboard, one set of rate limits.
* **Pro:** Andriy already has prompt experience for GPT — voice profiles
  written with GPT in mind. No re-tuning needed at the prompt layer.
* **Pro:** `pyproject.toml` loses `anthropic>=0.40.0` dependency.
* **Pro:** `.env` loses `ANTHROPIC_API_KEY`. One less secret to manage.
* **Con:** if OpenAI has a multi-hour outage, the whole pipeline stops.
  Mitigated by retry policy and the fact that a 6h outage means we miss
  a polling cycle, not anything irrecoverable.
* **Con:** gpt-4o is generally considered slightly weaker than Sonnet 4.6
  for long-form marketing prose. Mitigated by the polish stage operating
  with explicit anti-AI feedback. We monitor this on Stage 3 Quality Gate
  (≥4.0/5 on subjective scoring across 10 MVP posts).
* **Con:** vendor lock-in to OpenAI for both embeddings and generation.
  Acceptable for MVP; revisit if scaling beyond 5 brands.

## Migration path (if quality regresses)

If on Stage 3 Quality Gate gpt-4o produces generic output:

1. Switch `polish_model` constant in `comment_writer.py` to a snapshot like
   `"gpt-4o-2024-11-20"` first — sometimes specific versions write better.
2. If still generic, add Anthropic back as a polish-only provider:
   `anthropic_api_key` env var becomes optional; if set, polish uses
   Sonnet, otherwise gpt-4o. This is a clean fallback, ~30 minutes of work.
3. Document the switch in a new ADR-018.

## Cost comparison vs original Anthropic-based decision

| Stage | Volume/month | OpenAI cost | Anthropic cost |
|---|---|---|---|
| Scoring (gpt-4o-mini / Haiku) | ~500 calls | ~$0.10 | ~$0.50 |
| Drafts (gpt-4o-mini / Haiku) | ~200 calls | ~$0.20 | ~$0.20 |
| Polish (gpt-4o / Sonnet) | ~200 calls | ~$2.00 | ~$3.00 |
| Embeddings (text-embedding-3-small) | ~500 calls | ~$0.01 | n/a |
| **Total LLM bill** | | **~$2.31** | **~$3.70** |

OpenAI is also cheaper for our volumes — though both are well within
budget. The decision is primarily about operational simplicity, not cost.
