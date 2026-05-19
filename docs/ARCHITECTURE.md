# Architecture

> Living document. Keep in sync with the Obsidian Master Documentation §3.
> When the two disagree, the Obsidian doc wins.

## High-level

```
  ┌─────────────┐         ┌──────────────────┐         ┌──────────────┐
  │  sources/   │ raw     │  selector/       │ topic   │  generator/  │
  │  rss/tg/web │ ──────▶ │  topic_picker +  │ ──────▶ │  comment +   │
  │             │         │  dedup           │         │  image       │
  └─────────────┘         └──────────────────┘         └──────┬───────┘
                                                              │ draft
                                                              ▼
  ┌─────────────┐         ┌──────────────────┐         ┌──────────────┐
  │  publisher/ │ post    │  queue/          │ post    │  adapter/    │
  │  dispatcher │ ◀────── │  publish_queue + │ ◀────── │  per-channel │
  │             │         │  windows + rate  │         │  format      │
  └──────┬──────┘         └──────────────────┘         └──────────────┘
         │
         ▼
  Directus / TG Bot API / Meta Graph API
```

Approval flow (after `adapter/` produces a Post, status=`pending_approval`):

```
  pipeline ──sendPhoto──▶ Telegram (Andriy's DM)
                              │
                              │ inline-button tap
                              ▼
                          bot/approval_bot.py
                              │
                              ▼
              Directus.posts.status = approved
                              │
                              ▼
                          queue/publish_queue.enqueue
```

## Where state lives

| Where | What |
|---|---|
| **Directus (Postgres)** | brands, sources, topics, posts, channels, audit_log, all uploaded images |
| **`pipeline.db` (SQLite)** | publish_queue + dedup `seen` table (embeddings + entities) |
| **systemd journals** | structured logs, retained 7 days |

Directus is the source of truth. The SQLite file is local working state
and is rebuilt from Directus + audit_log on a fresh deploy.

## Module responsibilities

* `pipeline/sources/` — fetch raw items. Implements `Source` ABC + registry.
  Adding a new source type = new file + `@register`.
* `pipeline/selector/` — choose what to write about. `topic_picker` scores
  via Haiku; `dedup` rejects near-duplicates of our own past output.
* `pipeline/generator/` — `comment_writer` (Haiku draft → Sonnet polish),
  `anti_ai_check` (heuristic feedback), `image` (Flux Pro), `image_resizer`
  (per-channel crop).
* `pipeline/adapter/` — `Draft → Post`, channel-specific formatting.
* `pipeline/publisher/` — `Post → external publish call`. One module per
  external API + a `dispatcher` that picks by channel.
* `pipeline/queue/` — SQLite queue + window/rate-limit logic.
* `pipeline/scheduler/` — entry points invoked by systemd timers.
* `pipeline/monitoring/` — daily summary, alerts, health endpoint.
* `bot/` — long-running Telegram approval bot. Separate process from the
  pipeline worker.

## Operational layout (production)

```
/opt/news-to-socials/         git checkout
├── .venv/                    Python 3.12 virtualenv
├── .env                      secrets, owner news-deploy:news-deploy 0600
└── cms/                      docker-compose stack

/var/lib/news-to-socials/
├── pipeline.db               SQLite working state
└── backups/                  rolling 30-day local backups

/etc/systemd/system/news-*.service|.timer

Hetzner Storage Box:          off-site backups via rclone
```
