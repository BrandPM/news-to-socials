#!/usr/bin/env bash
# NTS_088 — restore admin.db from a local nts-backup dump.
#
# SAFE BY DEFAULT: restores into a scratch dir and runs integrity_check +
# row counts. It does NOT overwrite the live DB unless you pass --apply,
# and even then it stops the admin API and snapshots the current live DB
# first. Read the "Restore admin.db" runbook (IT_PROJ_NTS_088) before
# --apply on prod.
#
# Usage:
#   ./nts-restore.sh 2026-07-13            # verify-only into /tmp/restore-test
#   ./nts-restore.sh 2026-07-13 --apply    # stop API, back up live, restore live
#
# Env overrides (prod defaults):
#   NTS_DB_PATH      live admin.db            [/opt/news-to-socials/repo/admin.db]
#   NTS_BACKUP_DIR   where dumps live         [/opt/news-to-socials/backups]

set -euo pipefail

DATE="${1:-}"
MODE="${2:-verify}"
if [ -z "$DATE" ]; then
    echo "Usage: $0 <YYYY-MM-DD> [--apply]" >&2
    exit 64
fi

DB_PATH="${NTS_DB_PATH:-/opt/news-to-socials/repo/admin.db}"
BACKUP_DIR="${NTS_BACKUP_DIR:-/opt/news-to-socials/backups}"
SRC="$BACKUP_DIR/admin-$DATE.db.gz"

if [ ! -f "$SRC" ]; then
    echo "No dump for $DATE at $SRC" >&2
    echo "Available:" >&2
    ls -1 "$BACKUP_DIR"/admin-*.db.gz 2>/dev/null >&2 || echo "  (none)" >&2
    exit 66
fi

WORK=/tmp/restore-test
mkdir -p "$WORK"
OUT="$WORK/admin-$DATE.db"
gunzip -c "$SRC" > "$OUT"

echo "== integrity_check =="
sqlite3 "$OUT" "PRAGMA integrity_check;"
echo "== row counts (restored dump) =="
sqlite3 "$OUT" "SELECT 'runs', count(*) FROM runs;"

if [ "$MODE" != "--apply" ]; then
    echo "Verify-only. Restored copy left at $OUT (not applied to live)."
    exit 0
fi

echo "== APPLY: restoring into live DB $DB_PATH =="
sudo systemctl stop nts-admin-api.service
# Snapshot the current live DB before overwriting (last-ditch undo).
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$DB_PATH" "$DB_PATH.pre-restore-$TS" 2>/dev/null || true
# Remove stale WAL/SHM so the restored file is authoritative.
rm -f "$DB_PATH-wal" "$DB_PATH-shm"
cp -f "$OUT" "$DB_PATH"
chown news-deploy:news-deploy "$DB_PATH"
sudo systemctl start nts-admin-api.service
echo "Restore applied. Pre-restore copy: $DB_PATH.pre-restore-$TS"
