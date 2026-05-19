# Operational runbook

When something goes wrong and the daily-summary TG message is empty for
12h+, walk this list. The most likely failure modes are at the top.

> All commands assume you're on the production VPS as the `news-deploy`
> user, in `/opt/news-to-socials`.

## 0. Quick triage

```bash
systemctl list-timers --all          # are timers active?
journalctl -u news-poll  -n 30       # any poll errors?
journalctl -u news-bot   -n 30       # is the bot alive?
docker compose -f cms/docker-compose.yml ps  # Directus + Postgres up?
df -h /                              # is disk full?
free -h                              # is RAM exhausted?
```

## 1. Pipeline is silent

**Symptoms:** no posts published in last 12h, no alerts.

* `journalctl -u news-poll -p err -n 50` — any source raising consistently?
* Check Directus → `sources` collection: any with `active=true` but stale
  `last_polled_at`?
* Run a one-shot poll: `nts poll`. Errors will surface immediately.

## 2. Queue stuck

**Symptoms:** posts approved in Directus but not appearing on channels.

```bash
sqlite3 /var/lib/news-to-socials/pipeline.db <<'SQL'
SELECT status, COUNT(*) FROM publish_queue GROUP BY status;
SELECT id, post_id, channel_id, scheduled_at, attempts, last_error
FROM publish_queue
WHERE status IN ('pending', 'in_flight')
ORDER BY scheduled_at LIMIT 20;
SQL
```

If many rows are stuck in `pending` with `scheduled_at` in the future,
the publish window is blocking — check `channels.publish_window` for
that channel in Directus.

If `attempts >= 5`, the entries are terminally `failed`. Read
`last_error` for the root cause (most commonly a stale Meta token).

## 3. Meta access token expired

**Symptoms:** `last_error` contains "OAuthException" or 401.

1. Regenerate a long-lived token from Graph API Explorer.
2. Update `META_ACCESS_TOKEN` in `/opt/news-to-socials/.env`.
3. `sudo systemctl restart news-bot.service`.
4. Reset failed queue entries:
   ```sql
   UPDATE publish_queue SET status='pending', attempts=0
   WHERE status='failed' AND last_error LIKE '%OAuthException%';
   ```

## 4. Image generation failing

**Symptoms:** posts published without images, or with placeholder.

* Replicate dashboard → recent runs — any 4xx?
* `REPLICATE_API_TOKEN` valid?
* If account is rate-limited, the worker retries with backoff
  automatically — this resolves itself.

## 5. Directus unreachable

**Symptoms:** pipeline errors with `httpx.ConnectError` to `cms.icon.finance`.

```bash
cd /opt/news-to-socials/cms
docker compose ps
docker compose logs --tail 100 directus
docker compose logs --tail 50 postgres
```

If Postgres is OK but Directus is restart-looping, check
`docker compose logs directus` for migration errors. Don't `down -v` —
that wipes the volume.

## 6. Approval bot not responding

```bash
systemctl status news-bot.service
journalctl -u news-bot -n 50 --no-pager
```

If the process is alive but not responding to `/start`, the Telegram
servers may be returning 409 (another instance already polling). Make
sure only one `news-bot.service` is running — `pgrep -af approval_bot`
should show exactly one process.

## 7. Disk full

```bash
df -h /
du -sh /var/lib/news-to-socials/* /var/log/news-to-socials/*
docker system df
```

Likely culprits: Docker images, journal logs (`journalctl --vacuum-time=7d`),
old backups under `/var/lib/news-to-socials/backups/`.

## 8. High LLM bill

Daily summary now includes a cost estimate (TODO post-MVP). Until then:

* OpenAI dashboard → Usage → check spikes per day.
* `journalctl -u news-poll | grep -c "comment_writer"` — was the volume
  unusual?
* Likely cause: dedup regression letting through duplicates, or a source
  spamming the feed. Pause the source in Directus and investigate.

---

When a brand new incident happens, append it here as a numbered section
with the fix, even if it took 2 minutes. Future-you will thank present-you.
