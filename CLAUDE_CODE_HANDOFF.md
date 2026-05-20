# HANDOFF FOR CLAUDE CODE — News-to-Socials production launch

You are picking up the **News-to-Socials** project at a critical moment.
Andriy has done all the manual setup (GitHub repo, VPS, API keys, Sanity
project + token, post.ts schema patch). Local pipeline runs end-to-end
in `--dry-run` and produces sensible titles in the right Sanity
categories. **The only remaining work is: produce a real first post on
icon.finance/insights and prepare a deployable cron-driven worker on the
VPS.**

This document is your contract. Read it top to bottom before touching
anything else.

## What is already true

* Local Python environment works (`.venv`, `pip install -e ".[dev,ml,api]"`,
  pytest 54/54 green on a Mac).
* `~/.env` contains valid `OPENAI_API_KEY`, `REPLICATE_API_TOKEN`,
  `SANITY_API_TOKEN`. Sanity project is `yqrl1o3x`, dataset `production`.
* GitHub repo `BrandPM/news-to-socials` has the latest code pushed.
* Schema in Lovable (`src/sanity/schemas/post.ts`) was patched with five
  new pipeline-managed fields (`keyTakeaway`, `coverImageAlt`,
  `sourceUrl`, `topicId`, `generatedBy`). Lovable is redeployed.
* DigitalOcean Droplet `nts-prod` (IP `161.35.70.83`, Ubuntu 24.04) is up.
  User `news-deploy` exists, has Docker access, kernel was rebooted, UFW
  + fail2ban configured. SSH from Andriy's Mac works:
  `ssh news-deploy@161.35.70.83` (no password, key auth).
* On the VPS: nothing has been deployed yet. `/opt/news-to-socials/` is
  empty (Andriy never ran the clone).

## What you must produce

### Phase A — First real post in Sanity (run on Andriy's Mac)

1. Apply the GROQ-via-POST fix (already in this archive's
   `pipeline/publisher/sanity.py`) and confirm `pytest tests/unit/ -q`
   shows **54 passed**.
2. Run end-to-end (no `--dry-run`) against a wealth-focused source:
   ```bash
   cd ~/Projects/news-to-socials
   source .venv/bin/activate
   python -m scripts.run_pipeline \
     --brand icon \
     --source-id privatebanker \
     --source-url https://www.privatebankerinternational.com/feed/ \
     --language en \
     --limit 2
   ```
   Expected: `Processed 2 topics:` with two `draft_id`s.
3. Verify in Sanity:
   ```bash
   curl -s -H "Authorization: Bearer $(grep '^SANITY_API_TOKEN=' .env | cut -d= -f2)" \
     "https://yqrl1o3x.api.sanity.io/v2024-01-01/data/query/production" \
     -X POST -H "Content-Type: application/json" \
     -d '{"query":"*[_type==\"post\" && generatedBy==\"pipeline\"]{_id,title,category}"}' \
     | python3 -m json.tool
   ```
   Expected: `result` array with 2 objects, `_id` starting with `drafts.post-`.
4. Ping Andriy: "2 drafts live in /studio for your review. Approve or
   reject through the Sanity Studio UI; pipeline does not touch them
   after creation."

### Phase B — Deploy worker to VPS

1. **On the VPS** (`ssh news-deploy@161.35.70.83`):
   ```bash
   cd /opt/news-to-socials
   # Set up SSH key for git clone
   ssh-keygen -t ed25519 -C "news-deploy@nts-prod" -f ~/.ssh/id_ed25519 -N ""
   cat ~/.ssh/id_ed25519.pub
   ```
   Take the public key and ask Andriy to add it as a Deploy Key in
   `https://github.com/BrandPM/news-to-socials/settings/keys` with
   write access disabled.
2. Clone the repo:
   ```bash
   git clone git@github.com:BrandPM/news-to-socials.git .
   ```
3. Set up Python 3.12+:
   ```bash
   sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -e ".[ml,api]"
   ```
   (no `[dev]` extras on production — those are for local testing)
4. Create `/opt/news-to-socials/.env` from `.env.example` and fill in
   the four critical values from Andriy's Mac `.env` (he will copy them
   to you out-of-band; never log or persist):
   * `OPENAI_API_KEY`
   * `REPLICATE_API_TOKEN`
   * `SANITY_API_TOKEN`
   * everything else: `SANITY_PROJECT_ID=yqrl1o3x`, `SANITY_DATASET=production`,
     `SANITY_API_VERSION=2024-01-01` — already preset.
   File permission: `chmod 600 .env`.
5. Create state directories:
   ```bash
   sudo mkdir -p /var/lib/news-to-socials /var/log/news-to-socials
   sudo chown news-deploy:news-deploy /var/lib/news-to-socials /var/log/news-to-socials
   ```
6. Verify on VPS:
   ```bash
   pytest tests/unit/ -q
   ```
   Expected: 54 passed.
7. One dry-run on VPS:
   ```bash
   python -m scripts.run_pipeline \
     --brand icon \
     --source-id privatebanker \
     --source-url https://www.privatebankerinternational.com/feed/ \
     --language en \
     --limit 1 \
     --dry-run
   ```
   Expected: `Processed 1 topics:` with `status='dry_run'`.

### Phase C — systemd timer (cron replacement)

The skeleton ships systemd units in `infra/systemd/`. **Don't enable them yet** —
they were designed for the Directus era and reference services we no
longer have. Instead, create a single minimal timer that runs the
pipeline once a day at noon Europe/Madrid time, against a hard-coded
source list. Defer scheduler intelligence to Stage 6.

1. Create `/etc/systemd/system/nts-run.service`:
   ```ini
   [Unit]
   Description=News-to-Socials daily run
   After=network-online.target

   [Service]
   Type=oneshot
   User=news-deploy
   WorkingDirectory=/opt/news-to-socials
   EnvironmentFile=/opt/news-to-socials/.env
   ExecStart=/opt/news-to-socials/.venv/bin/python -m scripts.run_pipeline \
       --brand icon \
       --source-id privatebanker \
       --source-url https://www.privatebankerinternational.com/feed/ \
       --language en \
       --limit 3
   MemoryMax=1G
   ```

2. Create `/etc/systemd/system/nts-run.timer`:
   ```ini
   [Unit]
   Description=News-to-Socials daily timer

   [Timer]
   OnCalendar=*-*-* 12:00:00 Europe/Madrid
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

3. Enable:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now nts-run.timer
   systemctl list-timers --all | grep nts
   ```
   Expected: timer is `active` and next-run shows tomorrow noon.

### Phase D — Documentation and handback

1. Update Obsidian (`IT_PROJ_NTS_011_execution_checklist.md` and a new
   `IT_PROJ_NTS_012_production_launch.md`):
   * Mark every step you completed with timestamp.
   * Record the deploy-key UUID, the systemd timer status, the first
     draft IDs in Sanity, and any errors you ran into with their
     resolutions.
2. Update ClickUp epic `869bz88g3` and Stage 1 `869d8hgj7` with a
   comment summarising the launch.
3. Hand back to Andriy with a one-paragraph summary in chat: "Pipeline
   live on VPS, daily timer set for 12:00 Madrid, X drafts in Sanity
   awaiting your review at icon.finance/studio. Next: refine voice
   profile in Stage 2."

## Architectural invariants (don't break these)

1. **No secrets in git.** Ever. `.env` is gitignored; if you need a
   credential, ask Andriy or pull from his Mac's `.env`.
2. **No `--no-verify` commits.** All pre-commit / pytest must pass.
3. **No `Telegram`-related code execution.** Telegram bot is in the
   repo but `TELEGRAM_BOT_TOKEN` is empty — never start the bot.
4. **No Meta API calls.** Meta App Review is pending; `META_ACCESS_TOKEN`
   is empty.
5. **Drafts only.** Pipeline creates Sanity drafts (`_id` starts with
   `drafts.`). Never publish a document directly — that's Andriy's
   call in `/studio`.
6. **Don't touch posts you didn't create.** A document with
   `generatedBy=human` (the default) is off-limits. Only modify or
   delete documents where `generatedBy=pipeline`.
7. **Replicate budget alarm at $20/month.** If you hit costs above
   $5/day in a tight loop, stop the pipeline and ping Andriy.

## Known issues to be aware of

* `pipeline/cli.py` `nts run` command imports from `scripts.run_pipeline`
  via the entry-point shim. This worked on Andriy's Mac via `python -m`,
  but `nts` (installed as console-script) may need a small fix:
  `pip install -e .` again after any change. Use
  `python -m scripts.run_pipeline ...` everywhere if in doubt.
* `cms_DEPRECATED/` is dead code from the abandoned Directus path. Do
  not touch.
* `pipeline/publisher/directus.py` and `dispatcher.py` are kept for
  reference (Stage 5 may revisit). Don't depend on them in production.
* Telegram-related code in `bot/` and `pipeline/publisher/telegram_bot.py`
  is Wave-3 placeholder. Do not import it.

## When you get stuck

* SSH to VPS doesn't connect → check the IP `161.35.70.83` is still
  alive: `ping 161.35.70.83`. Sometimes DigitalOcean restarts droplets.
* Sanity returns 401 → token may have been rotated; ask Andriy.
* RSS source returns 0 items → the feed may have moved. Try
  `https://citywire.com/wealth-manager/rss` as a backup.
* OpenAI returns 429 → wait 60s and retry; if persistent, check
  `https://platform.openai.com/usage` for budget.

## Final commit message template

When you're done, commit and push with:

```
feat(ops): production launch — VPS deploy, daily timer, first drafts in Sanity

- Fixed GROQ query transport (POST body, not URL params)
- Added regression tests for SanityClient.query (54 → 56 tests)
- Deployed worker to DigitalOcean Droplet 161.35.70.83
- Created nts-run.timer (daily 12:00 Europe/Madrid)
- First N drafts created in Sanity production dataset
- Pipeline awaits human approval in /studio

Closes A1-A6 + A10. A4/A7/A8/A9 intentionally skipped (see ADR-017,
ADR-018, IT_PROJ_NTS_007 phased rollout).
```

Good luck. The hard architectural work is done. Execute cleanly.
