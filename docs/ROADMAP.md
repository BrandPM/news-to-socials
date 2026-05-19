# Roadmap (high level)

For day-by-day plans see the Obsidian vault `IT/projects/news-to-socials/`.

| Stage | Window | Goal | Skeleton coverage |
|---|---|---|---|
| 1 — Discovery | 2026-05-11 → 16 | API keys, VPS, GitHub repo, Meta App Review, sources/channels v0 | n/a — operational |
| 2 — Directus + voice | 2026-05-17 → 23 | Directus up, schema, AI-extracted + human-refined voice profiles for 5 brands | `cms/`, `cms/schema/README.md` |
| 3 — MVP blog | 2026-05-24 → 06-01 | One source → Icon EN blog. **Quality gate.** | `sources/rss.py`, `selector/`, `generator/`, `adapter/blog.py`, `publisher/directus.py` |
| 4 — Channels + images | 2026-06-02 → 12 | + TG + FB + IG for Icon EN. Image generation. | `generator/image*.py`, `adapter/{telegram,facebook,instagram}.py`, `publisher/{telegram_bot,meta_graph,dispatcher}.py` |
| 5 — Multibrand + langs | 2026-06-13 → 21 | All 5 brands; Icon also in RU/UK/PL. **W2 decision: Lovable vs Astro.** | per-brand parametrisation across modules; no new files needed for the skeleton |
| 6 — Scheduler + dedup | 2026-06-22 → 28 | Event-driven via systemd, hybrid dedup, queue with windows + rate-limit | `scheduler/`, `queue/`, `infra/systemd/`, `selector/dedup.py` tuned |
| 7 — Approval + prod | 2026-06-29 → 07-09 | Telegram bot, retry, monitoring, backups, 7-day observation | `bot/`, `monitoring/`, `infra/backup/`, `docs/RUNBOOK.md` |
