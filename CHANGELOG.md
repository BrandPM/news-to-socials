# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.0.1] — 2026-05-17

Initial skeleton drop.

### Added

* MIT license, `pyproject.toml`, `.env.example`, `.gitignore`, `Makefile`.
* `pipeline/common/` — config, logging, models, retry decorator.
* `pipeline/sources/` — Source ABC + registry, RSS, Telegram/Web stubs.
* `pipeline/selector/` — Claude Haiku topic picker, hybrid embedding+entity dedup.
* `pipeline/generator/` — two-stage commentary writer (Haiku→Sonnet),
  anti-AI heuristics, Flux Pro image, multi-format resizer.
* `pipeline/adapter/` — blog (markdown+frontmatter), Telegram (HTML),
  Facebook, Instagram.
* `pipeline/publisher/` — Directus client+publisher, Telegram Bot API,
  Meta Graph API (v18.0 pinned), dispatcher with audit-log writes.
* `pipeline/queue/` — SQLite publish queue, window + rate-limit parsing.
* `pipeline/scheduler/` — `poll_sources`, `dispatch_queue`, `stale_posts`.
* `pipeline/monitoring/` — daily TG summary, FastAPI health endpoint.
* `pipeline/cli.py` — `nts` Typer CLI.
* `bot/approval_bot.py` — Telegram inline-button approval flow.
* `cms/` — Directus + Postgres + Caddy docker-compose stack.
* `infra/systemd/` — 5 unit files (poll, dispatch, bot, stale, summary).
* `infra/backup/` — `backup.sh` + `restore.sh` with Hetzner Storage Box.
* `docs/` — README, ARCHITECTURE, RUNBOOK, ROADMAP, ADR-001 + ADR-013.
* `tests/unit/` — 37 tests covering dedup, anti-AI, adapters, windows,
  rate-limit, image resize, RSS parsing.
* `scripts/` — `tune_dedup.py`, `validate_multibrand.py`.

### Status

* 37/37 unit tests passing on Python 3.12.
* No `make check` mypy pass yet — types are best-effort.
* External calls (Claude, Replicate, Meta, Directus REST, TG Bot) are all
  written but only verified via signature; integration tests are TODO.

### Inspirations

Architectural patterns studied (no code copied). Full attribution in
[`README.md`](README.md#inspirations).
