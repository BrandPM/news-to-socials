# News-to-Socials

Automated content pipeline that turns news pegs into **original expert
commentary** from 5 fintech brands (Icon, Neovox, Creolix, Vilatrix, Nexora)
and publishes to blog, Telegram, Facebook, and Instagram with a human
approval step in Telegram.

> **Status:** skeleton. Stages 1–7 implemented incrementally per
> [`docs/ROADMAP.md`](docs/ROADMAP.md). The full project plan and ADRs live in
> the Obsidian vault under `IT/projects/news-to-socials/`.

---

## What this is *not*

- Not a news *rewriter*. We never reproduce someone else's text — we generate
  original brand-voice commentary on the news peg. See ADR-001.
- Not a generic posting tool. Each post is filtered, voice-matched, image-paired,
  channel-adapted, and approved before publication.
- Not a SaaS. Single-tenant, self-hosted on one Hetzner VPS (~$11/mo).

## What it *is*

A modular Python pipeline:

```
sources/        RSS / Telegram / web — fetch raw items
   ↓
selector/       Relevance scoring + hybrid embedding-based dedup per (brand, language)
   ↓
generator/      gpt-4o-mini draft → gpt-4o polish + Flux Pro image
   ↓
adapter/        Format per channel (blog markdown / TG HTML / FB / IG)
   ↓
queue/          Publish-window + rate-limit-aware queue (SQLite)
   ↓
bot/            Telegram approval flow with inline buttons
   ↓
publisher/      Directus (blog) / Telegram Bot API / Meta Graph API
   ↓
monitoring/     Daily summary + critical alerts + /health endpoint
```

State of the world lives in **Directus** (headless CMS, PostgreSQL); local
queues and embedding caches live in SQLite next to the worker.

---

## Quick start (development)

```bash
# 1. Python 3.12+
python --version

# 2. Install
pip install -e ".[dev,ml,api]"

# 3. Configure
cp .env.example .env
$EDITOR .env   # fill API keys

# 4. Run a single source through the pipeline (smoke test)
nts run --brand icon --source <uuid> --language en --channel blog --limit 3 --dry-run
```

The `--dry-run` flag prints what would be published without calling any
external publish API.

## Project layout

```
news-to-socials/
├── pipeline/
│   ├── sources/      # base, rss, telegram, web — fetch raw items
│   ├── selector/     # topic_picker, dedup — pick + deduplicate
│   ├── generator/    # comment_writer, anti_ai_check, image, image_resizer
│   ├── adapter/      # blog, telegram, facebook, instagram — per-channel format
│   ├── publisher/    # directus, telegram_bot, meta_graph, dispatcher
│   ├── queue/        # publish_queue, publish_windows, rate_limit
│   ├── scheduler/    # poll_sources, dispatch_queue, stale_posts
│   ├── monitoring/   # daily_summary, alerts, health_check
│   └── common/       # config, db, retry, logging, models
├── bot/              # approval_bot.py — Telegram inline-button approval
├── cms/              # Directus docker-compose + schema snapshots
├── infra/            # systemd units + backup scripts
├── docs/             # README, ARCHITECTURE, RUNBOOK, ADR/
├── scripts/          # one-shot tools: tune_dedup, validate_multibrand, observation_report
└── tests/            # unit/ + integration/
```

## Architectural decisions

All decisions live as ADRs in [`docs/ADR/`](docs/ADR/). Highlights:

| ADR | Decision |
|---|---|
| 001 | Original commentary, not rewrites |
| 002 | Python pipeline (not n8n) |
| 003 | Directus as headless CMS for all 5 brand sites |
| 004 | Event-driven polling, not fixed schedule |
| 005 | Telegram-bot approval, disable-able per channel |
| 007 | AI-extracted voice-profile, human-refined |
| 008 | One master image per post, resized per channel |
| 011 | SQLite for queue+dedup, Postgres only for Directus |
| 013 | gpt-4o-mini draft → gpt-4o polish (two-stage LLM) |
| 017 | OpenAI-only stack |

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full picture.

## Inspirations

This project draws architectural ideas from several OSS projects. **No code
was copied** — only patterns were studied. Cross-references in source files
point to specific files for further reading.

| Project | License | What we learned |
|---|---|---|
| [`samgozman/fin-thread`](https://github.com/samgozman/fin-thread) | GPL-3.0 | Module split: Journalist / Composer / Publisher / Archivist / Job. Provider-interface for source pluggability. Two-stage LLM (Filter → Compose → Summarise). |
| [`iliane5/meridian`](https://github.com/iliane5/meridian) | MIT | `multilingual-e5-small` embeddings for cross-language dedup. UMAP→HDBSCAN clustering as a longer-term path beyond cosine thresholds. Browser-rendering fallback for paywalled sources. |
| [`finaldie/auto-news`](https://github.com/finaldie/auto-news) | MIT | Modular `ops_<source>.py` convention. Multiple embedding backends (HF / OpenAI / Ollama) behind one interface. |
| [`Rongronggg9/RSS-to-Telegram-Bot`](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) | AGPL-3.0 | Long-post splitting strategy. RSS parser edge-cases (HTML in description, missing dates). |

> **License note.** fin-thread is GPL-3.0 and RSS-to-Telegram-Bot is AGPL-3.0.
> Both are copyleft; copying code wholesale would force this project to adopt
> their license. We deliberately studied patterns only and wrote a clean
> MIT-licensed implementation.

## License

MIT — see [`LICENSE`](LICENSE).
