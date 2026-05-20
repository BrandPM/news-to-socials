# ADR-018 — Use the existing Sanity CMS, drop Directus

**Status:** Accepted
**Date:** 2026-05-20
**Supersedes:** ADR-003 (Directus as headless CMS)

## Context

The original plan (ADR-003) called for self-hosting Directus on the same
VPS as the pipeline worker, fronted by Caddy at `cms.icon.finance`, with
6 collections we'd build out by hand: brands, sources, topics, posts,
channels, audit_log.

During Stage 1 setup, we discovered that **the production Lovable
project for icon.finance already runs Sanity**, with:

* Routes: `/:lang/insights` (list), `/:lang/insights/:slug` (article),
  `/studio/*` (editor)
* Schema: `src/sanity/schemas/post.ts`
* Internationalization plugin: `@sanity/document-internationalization`
  with `en/ru/uk/pl`
* Components: `PortableBody.tsx`, `FallbackBanner.tsx`, and an `Insights.tsx`
  section already integrated on the main page

Standing up Directus alongside Sanity would mean two CMSes, two admin
UIs for the operator (Andriy) to learn, two backup procedures, two
schemas to keep in sync, and either dual-write or a second `/blog/*`
route on top of the existing `/insights/*`.

## Decision

The pipeline writes drafts directly into the existing Sanity project.
Directus is removed from the active deployment.

* **Storage of brand-managed content** — Sanity Cloud, project
  `yqrl1o3x`, dataset `production`.
* **Operator UI** — Sanity Studio at `icon.finance/studio` (Andriy
  already uses this).
* **Approval flow** — Sanity's native draft/publish:
  pipeline creates `drafts.{id}`, Andriy presses Publish in Studio.
* **VPS** — kept, but now hosts only the Python worker (cron-driven
  pipeline tasks). No Caddy, no public service, no `cms.icon.finance`
  subdomain.
* **Worker-local state** — SQLite at `/var/lib/news-to-socials/pipeline.db`
  for the queue and the dedup cache (unchanged from ADR-011).
* **Pipeline writes images** to Sanity Assets (uploaded via the REST API).

The new publisher lives at `pipeline/publisher/sanity.py`. The previous
`pipeline/publisher/directus.py` stays in the repository as deprecated
reference code (it may be useful again for a wire-brand at Stage 5 that
wants a self-hosted CMS — kept until that decision lands).

## Why this is right

* **Zero front-end work for Wave 1.** Icon's `/insights` already renders
  Sanity content; pipeline-generated drafts that get published appear
  on the site automatically.
* **No new admin UI for Andriy.** Studio is the editor he already knows.
* **No CMS to operate.** Sanity Cloud handles uptime, backups, and
  schema versioning. Free tier covers ~50× our planned throughput.
* **Schema lives next to the front-end.** The Lovable project owns
  `src/sanity/schemas/post.ts`. Schema changes are PR'd in the Lovable
  repo, not in our worker repo — the right separation of concerns.
* **i18n already wired.** `@sanity/document-internationalization` gives
  us the en/ru/uk/pl translation linking for free.

## Consequences

* **Pro:** Stage 2 Day 1 (deploy Directus stack), Day 2 (build 6
  collections) — removed. Estimated 2-3 days saved from the project
  schedule.
* **Pro:** the worker is operationally simpler — just Python + cron, no
  reverse proxy, no Postgres on the host.
* **Pro:** content lives in Sanity, so Andriy's existing editorial
  workflow (drafts, internationalization, Vision tool, history) applies.
* **Con:** brand config (voice profile, audience, banned topics,
  category list) needs a home. Solution: create a small `brand`
  document type in Sanity (one row per brand, edited in Studio). For
  Wave 1 with only Icon, `scripts/run_pipeline.py` hard-codes a
  reasonable default and we move the values to a `brand` doc at Stage 2
  Day 3.
* **Con:** wire-brands at Stage 5 won't necessarily be on Sanity. Each
  brand will get its own Sanity project (different project IDs), or a
  different CMS entirely. Pipeline must be parameterised by brand →
  Sanity client. Architecture supports this — `SanityClient` takes
  project/dataset/token as constructor args.
* **Con:** the dedup story now has two tiers. Local SQLite remains the
  primary fast path; we also check Sanity (GROQ query against the
  `topicId` field) as a backstop. If the SQLite file is lost (VPS
  rebuild), we still don't republish thanks to the Sanity check.

## Migration steps performed

1. Renamed `cms/` → `cms_DEPRECATED/` with a README pointing here.
2. Wrote `pipeline/publisher/sanity.py` (SanityClient + SanityPublisher
   + Portable Text conversion helpers).
3. Updated `pipeline/common/config.py`:
   added `SANITY_PROJECT_ID`, `SANITY_DATASET`, `SANITY_API_VERSION`,
   `SANITY_API_TOKEN`. Kept `DIRECTUS_*` empty fields for
   backwards-compat (not used).
4. Rewrote `scripts/run_pipeline.py` for the Sanity flow (publishes
   drafts; supports `--dry-run`).
5. Updated `pipeline/cli.py` `run` command to match the new orchestrator
   signature (`--source-url`, no `--channel` — implicit blog for Wave 1).
6. Added `tests/unit/test_sanity.py` covering slugify, read-time,
   excerpt, and Portable Text conversion.
7. Wrote `docs/sanity-post-patch.md` — the schema diff Andriy applies
   in the Lovable project.
8. Removed Anthropic remnants (already done by ADR-017) and Directus
   collection references from `.env.example`.

## Tests

* `tests/unit/test_sanity.py`: 15 tests covering helper functions.
* Total: 52/52 unit tests passing on Python 3.12.

## When to reconsider

* If pipeline throughput grows past Sanity's free tier (10k docs or
  100k requests/month) — buy a paid plan or shard per brand.
* If a wire-brand at Stage 5 lacks Sanity and wants a self-hosted CMS
  — un-deprecate `cms/` and write ADR-019 for that specific brand.

## References

* Lovable schema: `src/sanity/schemas/post.ts`
* Lovable client: `src/lib/sanity.ts`
* Schema diff this ADR implies: `docs/sanity-post-patch.md`
* Sanity HTTP API: https://www.sanity.io/docs/http-api
* document-internationalization plugin:
  https://www.sanity.io/plugins/document-internationalization
