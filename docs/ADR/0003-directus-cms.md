# ADR-003 — Directus as the headless CMS

**Status:** Accepted
**Date:** 2026-05-11

## Context

The system needs persistent storage with an operator UI for:
* brand configs (voice profile, visual config)
* source feeds, channels, topics, posts
* audit log

Options considered: custom Django admin, Strapi, Directus, Payload CMS,
raw Postgres + small Flask UI.

## Decision

Directus, self-hosted in Docker against PostgreSQL 16.

## Consequences

* **Pro:** out-of-the-box REST + GraphQL API → the Python worker writes
  through one consistent layer. No bespoke API to maintain.
* **Pro:** operator UI is generated from the schema — Andriy edits voice
  profiles, brand visual configs, channel routes without writing code.
* **Pro:** schema snapshots are YAML, version-controlled in
  `cms/schema/snapshot.yaml`.
* **Pro:** built-in file storage (`/files` endpoint) handles image uploads
  and serves them via CDN-friendly URLs.
* **Pro:** RBAC, audit log, webhook, and (in v11.13+) MCP server are all
  built-in.
* **Con:** Directus has its own upgrade cadence; we pin the image tag.
* **Con:** an extra moving part vs raw Postgres. Mitigated by the fact
  that the worker can read Postgres directly in an emergency.

## Operator-edited fields

Andriy edits these in the Directus UI (not via code):
* `brands.voice_profile_yaml`, `brands.visual_config_json`
* `channels.publish_window`, `channels.rate_limit`, `channels.active`
* `sources.active`, `sources.polling_interval_minutes`
* Post approval through the Telegram bot writes back to `posts.status`.
