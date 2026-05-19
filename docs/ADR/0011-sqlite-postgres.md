# ADR-011 — SQLite for queue + dedup, Postgres for Directus

**Status:** Accepted
**Date:** 2026-05-11

## Context

We need persistent storage for two distinct workloads:

1. **Operator-facing entities** (brands, sources, channels, posts, audit log)
   — read by Directus, edited by Andriy, queried by Lovable/Astro sites.
2. **Worker-local state** (publish queue, embedding cache, `seen` table
   for dedup) — high-write, single-writer, never seen by Directus.

Putting (2) in Postgres works, but adds a network hop on the hot path,
inflates the Postgres backup, and lights up Postgres logs with traffic
that isn't interesting to humans.

## Decision

* **Postgres** (run by Directus, port 5432) — collections from §A.7.2:
  brands, sources, topics, posts, channels, audit_log.
* **SQLite** (`pipeline.db`, file path from `PIPELINE_DB_PATH` env) —
  worker-local tables:
  - `publish_queue` (id, post_id, channel_id, scheduled_at, attempts, status, last_error)
  - `seen` (hash, brand_id, language, embedding_json, entities_json, first_seen_at)
* Both backed up daily by `infra/backup/backup.sh`.

## Consequences

* **Pro:** zero network latency on hot-path reads/writes (dispatch tick,
  dedup check).
* **Pro:** `sqlite3 pipeline.db` is the operator's tool for inspecting
  queue state — `select status, count(*) from publish_queue group by status;`
  in 2 seconds.
* **Pro:** SQLite WAL mode handles our single-writer + readers cleanly.
* **Con:** SQLite doesn't scale across processes. We never have >1
  worker process today — but if we ever scale out the worker
  (multiple VPSes), this becomes a Postgres migration. See "When to
  revisit" in ADR-004.
* **Con:** two backup procedures vs one. Mitigated by `backup.sh`
  handling both files.

## Schemas

Defined in code:
* SQLite: created on first run by `pipeline.queue.publish_queue.PublishQueue.init()`.
* Postgres: managed by Directus from `cms/schema/snapshot.yaml`.

## Anti-patterns

* Don't put `seen` / `publish_queue` in Postgres "for consistency".
* Don't query Postgres from inside the dispatch loop — always go through
  the `DirectusClient` so we get retry + audit_log for free.
