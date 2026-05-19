# ADR-004 — Event-driven polling via systemd timers

**Status:** Accepted
**Date:** 2026-05-11

## Context

The pipeline reacts to two kinds of events:
1. New items appearing in RSS / Telegram / web sources.
2. Approved posts in the queue becoming ready to publish (window + rate-limit).

We don't get push notifications from sources — we have to poll. The choice
was between a long-running event loop (apscheduler in-process), or
out-of-process triggers (systemd timers, cron, Kubernetes CronJobs).

## Decision

* **systemd timers** invoke short-lived Python processes every 5 minutes:
  - `news-poll.timer` → `pipeline.scheduler.poll_sources`
  - `news-dispatch.timer` → `pipeline.scheduler.dispatch_queue`
  - `news-stale.timer` → `pipeline.scheduler.stale_posts` (hourly)
  - `news-summary.timer` → `pipeline.monitoring.daily_summary` (09:00 daily)
* **One long-running service:** `news-bot.service` for the Telegram
  approval bot (which needs to hold the long-poll connection).

## Consequences

* **Pro:** restart on crash is automatic and free (systemd does it).
* **Pro:** OOM in one tick doesn't kill the bot or the next tick.
* **Pro:** `journalctl -u news-poll` gives structured per-run logs.
* **Pro:** resource limits per service (`MemoryMax=1G`) — mitigates W8
  (image processing RAM peaks).
* **Pro:** zero new dependencies; systemd is on the OS already.
* **Con:** can't share in-memory caches between ticks. Mitigated by
  SQLite for queue + dedup state.
* **Con:** ticks at fixed 5-minute boundary; no backpressure. Acceptable
  given our volume (~100-200 posts/month).

## When to revisit

If we hit >5 sources × every-30-second polling demand, move to a single
long-running asyncio process. We're far from there.
