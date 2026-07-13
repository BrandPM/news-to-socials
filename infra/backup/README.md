# admin.db backup (NTS_088)

`admin.db` is the **only** stateful asset (prompt versions, source configs,
banned phrases, run history, draft↔Sanity links). Sanity content is
recoverable; `admin.db` is not. Live DB: `/opt/news-to-socials/repo/admin.db`
(WAL mode).

Two layers, both replicate the same DB:

| Layer | What | RPO | Status |
|---|---|---|---|
| **Variant B** — `nts-backup.sh` + timer | daily local `.backup` dump, gzip, 30 daily + 12 weekly | 24h | **active** |
| **Variant A** — Litestream → DO Spaces | continuous S3 replication | ~seconds | prepared, activate when creds exist |

The dead Directus-era `backup.sh`/`restore.sh` (pg_dump + docker + Hetzner)
were removed in NTS_088.

## Files

- `nts-backup.sh` — daily snapshot. Uses SQLite `.backup` (never `cp` on a live
  WAL DB). Writes `admin-YYYY-MM-DD.db.gz` to `/home/news-deploy/backups/` and
  an ISO-8601 heartbeat to `.last_ok`. Env: `NTS_DB_PATH`, `NTS_BACKUP_DIR`.
- `nts-restore.sh` — verify-only by default; `--apply` overwrites the live DB
  (stops the API, snapshots current DB first).
- `nts-backup.service` / `nts-backup.timer` — 03:00 UTC daily.
- `litestream/` — Variant A templates (`litestream.yml`, `litestream.service`,
  `backup.env.example`).

## Install — Variant B (cron dump)

```bash
cd /opt/news-to-socials/repo && git pull --ff-only origin main
sudo cp infra/backup/nts-backup.service infra/backup/nts-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nts-backup.timer
sudo systemctl start nts-backup.service      # run once now
systemctl list-timers | grep nts-backup
ls -la /home/news-deploy/backups/
```

The heartbeat is monitored by the existing `nts-monitor.timer` alerter
(`pipeline.monitoring.alerts`): if `.last_ok` is missing or > 26h old, one
Telegram alert fires per day (`backup_stale:DATE`). **Limitation:** this alert
cannot fire if the whole VPS is down — external uptime monitoring is a separate
future item.

## Litestream activation — Variant A (only when creds exist)

Preconditions: `admin.db` in WAL mode (already true); a DO Space in FRA1 + an
access-key pair in Bitwarden.

1. Create the Space (FRA1) + access keys in the DO console; store keys in
   Bitwarden ("NTS DO Spaces backup keys").
2. On the VPS:
   ```bash
   sudo mkdir -p /etc/news-to-socials
   sudo cp /opt/news-to-socials/repo/infra/backup/litestream/backup.env.example \
           /etc/news-to-socials/backup.env
   sudo nano /etc/news-to-socials/backup.env          # paste values, set BUCKET
   sudo chown news-deploy:news-deploy /etc/news-to-socials/backup.env
   sudo chmod 600 /etc/news-to-socials/backup.env
   sudo cp /opt/news-to-socials/repo/infra/backup/litestream/litestream.yml /etc/litestream.yml
   sudo cp /opt/news-to-socials/repo/infra/backup/litestream/litestream.service /etc/systemd/system/
   ```
3. Install the binary + start:
   ```bash
   # verify WAL first — do NOT flip journal mode live without a window
   sqlite3 /opt/news-to-socials/repo/admin.db "PRAGMA journal_mode;"   # must print: wal
   curl -sLO https://github.com/benbjohnson/litestream/releases/latest/download/litestream-linux-amd64.deb
   sudo dpkg -i litestream-linux-amd64.deb
   sudo systemctl daemon-reload
   sudo systemctl enable --now litestream.service
   litestream snapshots /opt/news-to-socials/repo/admin.db     # verify replication
   ```

## Restore

See the "Restore admin.db" runbook in
`Obsidian Vault/.../03_session_logs/IT_PROJ_NTS_088_backup_implementation_*.md`,
or use `./nts-restore.sh <YYYY-MM-DD>` (verify) / `--apply` (live).
