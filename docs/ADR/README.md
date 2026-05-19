# Architectural Decision Records

Short, dated decisions in [MADR](https://adr.github.io/madr/) lite format.
Each ADR is at most one page. When something changes, write a new ADR
that supersedes the old one — don't edit history.

| ADR | Title | Status |
|---|---|---|
| 001 | Original commentary, not rewrites | Accepted |
| 002 | Python pipeline, not n8n | Accepted |
| 003 | Directus as headless CMS | Accepted |
| 004 | Event-driven polling via systemd timers | Accepted |
| 005 | Telegram approval flow | Accepted |
| 006 | Multi-tenant by `brand_id` parameter | Accepted |
| 007 | AI-extracted, human-refined voice profile | Accepted |
| 008 | One master image, resized per channel | Accepted |
| 009 | Languages: Icon RU/UK/EN/PL, wire EN | Accepted |
| 011 | SQLite for queue+dedup, Postgres for Directus | Accepted |
| 012 | Flux 1.1 Pro via Replicate | Accepted (provisional) |
| 013 | Two-stage LLM: gpt-4o-mini draft → gpt-4o polish | Accepted (updated 2026-05-18) |
| 014 | Image-generator bake-off result | TODO (Stage 1 Day 4) |
| 015 | Frontend: Lovable vs Astro | TODO (Stage 5 Day 1) |
| 016 | Optional: Directus AI Assistant vs `brand_extractor.py` | TODO (Stage 2 Day 3) |
| 017 | OpenAI-only LLM stack (drop Anthropic) | Accepted |

The full reasoning behind every decision lives in the Obsidian vault
under `IT/projects/news-to-socials/IT_PROJ_NTS_010_master_documentation.md §4`.
The ADRs in this directory are short executable summaries for whoever
inherits the codebase.
