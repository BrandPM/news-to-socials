# ADR-002 — Python pipeline, not n8n

**Status:** Accepted
**Date:** 2026-05-11

## Context

The pipeline needs to run multiple LLM calls per post, score content, do
embedding-based dedup, generate and resize images, and publish across four
channel APIs. Two stacks were on the table: n8n (low-code) or pure Python.

## Decision

Python (3.12, asyncio). All logic lives in modules under `pipeline/`, no
n8n workflows.

## Consequences

* **Pro:** unit-tested code, version control with git, IDE refactoring,
  type hints, no vendor lock-in. Cost: $0 for the runtime itself.
* **Pro:** the same modules drive both the scheduled worker and one-shot
  CLI runs — no duplication.
* **Con:** higher initial setup time vs drag-and-drop workflows. Mitigated
  by the skeleton: 83 files, 37 unit tests, full structure preset.
* **Con:** harder to hand off to a non-engineer. Mitigated by Directus
  (the operator UI for brands/sources/channels) — n8n's "low-code"
  advantage rarely survives real-world debugging anyway.

## Notes

If we ever need to expose a no-code "create new pipeline" surface (Phase 2),
we can build it as a Directus extension that writes YAML configs which the
Python worker reads. Cleaner than embedding n8n inside.
