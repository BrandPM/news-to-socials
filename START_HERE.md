# START HERE — Claude Code, read this first

Hi. You're picking up the News-to-Socials project.

## 30-second context

This repo is a **Python pipeline** that turns finance-news pegs into
original brand commentary for five fintech brands (Icon, Neovox,
Creolix, Vilatrix, Nexora) and publishes to blog + Telegram + Facebook +
Instagram with a Telegram-bot approval step.

The skeleton is built and tested. Your job is to take it to production.

## Right now, in order:

1. **Verify the skeleton runs locally** (5 min):
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev,ml,api]"
   pytest tests/unit/ -q
   ```
   Expect: **37 passed**. If not, stop and read the failure.

2. **Read the handoff document** (20 min) — it's the contract for how
   you should work:
   - In the Obsidian vault: `IT/projects/news-to-socials/IT_PROJ_NTS_004_handoff_for_claude_code.md`
   - It defines: what Andriy did manually (you don't redo it), what
     you do (5 phases with DoD), and the troubleshooting playbook.

3. **Read the architecture source-of-truth** (20 min):
   - In the vault: `IT/projects/news-to-socials/IT_PROJ_NTS_010_master_documentation.md`
   - §3-§7 = architecture. §4 = ADRs. §5 = the 12 known weak spots.
   - When the handoff and Master conflict, Master wins.

4. **Check Andriy's preconditions** (5 min):
   - Open `IT_PROJ_NTS_004_handoff_for_claude_code.md` § "Что Андрей делает руками"
   - Verify A1-A10: GitHub repo, VPS, DNS, API keys, Telegram bot,
     Meta App Review (long lead-time), Hetzner Storage Box, Lovable.
   - If A1-A7 are done → start Phase 1.
   - If something critical is missing → write `docs/blockers.md`
     listing exactly what you need from Andriy, stop, wait.

5. **Start Phase 1** (Stage 1 closure).
   - The DoD checklist is in the handoff doc.
   - First subtask: clone the repo, set up VPS environment.

## What's already done for you

| Area | State |
|---|---|
| Skeleton structure (83 files) | ✅ Done — see `docs/ROADMAP.md` for coverage by stage |
| 37 unit tests passing | ✅ Done |
| 11 ADRs written | ✅ `docs/ADR/0001` through `0013` (with 010 reserved) |
| End-to-end orchestrator | ✅ `scripts/run_pipeline.py` — wires all modules together |
| Directus docker-compose | ✅ `cms/docker-compose.yml` + Caddy |
| systemd unit files | ✅ `infra/systemd/` (5 services + timers) |
| Backup scripts | ✅ `infra/backup/backup.sh` + `restore.sh` |
| RUNBOOK (8 scenarios) | ✅ `docs/RUNBOOK.md` |

## What's NOT done (you'll do it)

- VPS deployment (Andriy provides credentials)
- Directus collections — schema-as-YAML is in `cms/schema/README.md`,
  but the actual `snapshot.yaml` is generated on first Directus boot
- 5 brand records with voice profiles in Directus
- The list of sources (Andriy provides on Stage 1 Day 3)
- Image generator bake-off (ADR-014 — Stage 1 Day 4)
- Frontend integration to Lovable / Astro (Stage 3 and 5)
- 7-day production observation (final stage)

## Architectural invariants — don't break these

1. **Original commentary**, never rewrite source content. ≤15 words of
   direct quote per post.
2. **Approval ON by default** for every channel. Off requires explicit
   per-channel config in Directus.
3. **No deploy without green tests.** Pre-commit + CI.
4. **Secrets only in `.env`** on the machine. Never in git, logs, or
   commit messages.
5. **Backup before migrations.** Test restore on dev quarterly.
6. **One channel at a time** for new code (Stage 3 → blog only;
   Stage 4 → all four for Icon EN; Stage 5 → all five brands).
7. **No "temporary hacks".** Either it's a TODO with a tracked issue,
   or it's not in the code.

## If you get stuck

- **API/UI step that needs human hands** → Andriy. Comment on the PR,
  don't try to automate `developers.facebook.com`.
- **Tests fail and you can't see why** → roll back, re-run, narrow
  down. Don't `pytest --skip` your way through.
- **The skeleton feels wrong** → it's not dogma. Change with an ADR.
- **Real blocker** → write `docs/blockers.md` and pause.

## Files to read in order

1. This file (`START_HERE.md`) — done
2. `README.md` — project overview, inspirations
3. `IT_PROJ_NTS_004_handoff_for_claude_code.md` (Obsidian) — your contract
4. `IT_PROJ_NTS_010_master_documentation.md` (Obsidian) — architecture
5. `docs/ARCHITECTURE.md` — local copy of the architecture
6. `docs/ROADMAP.md` — 7 stages with current status
7. `docs/ADR/README.md` — index of decisions
8. `docs/RUNBOOK.md` — when things break

Good luck. The hard architectural choices are made; you have a clear plan.
Execute it.
