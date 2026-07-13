#!/usr/bin/env bash
# NTS_088 — daily local snapshot of the ONLY stateful asset: admin.db.
#
# Uses the SQLite online-backup API (``.backup``), never ``cp`` — admin.db
# runs in WAL mode (NTS_059/061) and is written concurrently by the admin
# API and detached run-workers (NTS_074). ``.backup`` takes a consistent
# page-level snapshot under a read transaction; ``cp`` of a live WAL DB can
# capture a torn file. Output is gzipped to ``admin-YYYY-MM-DD.db.gz``.
#
# Retention: 30 most-recent daily dumps + Sunday dumps for 12 weeks (84d).
# On success, writes an ISO-8601 UTC timestamp to ``.last_ok`` — the
# heartbeat the nts-monitor alerter checks (stale >26h → Telegram alert).
#
# Runs unattended as ``news-deploy`` via nts-backup.timer (03:00 UTC daily).
# No sudo, no secrets: the DB and backup dir are both owned by news-deploy.
#
# Env overrides (defaults are the prod layout):
#   NTS_DB_PATH      live admin.db            [/opt/news-to-socials/repo/admin.db]
#   NTS_BACKUP_DIR   where dumps are written  [/opt/news-to-socials/backups]
#
# Restore: see the "Restore admin.db" runbook (IT_PROJ_NTS_088) or
# ./nts-restore.sh.

set -euo pipefail

DB_PATH="${NTS_DB_PATH:-/opt/news-to-socials/repo/admin.db}"
BACKUP_DIR="${NTS_BACKUP_DIR:-/opt/news-to-socials/backups}"
HEARTBEAT="$BACKUP_DIR/.last_ok"

DAILY_KEEP_DAYS=30    # keep every daily dump this new
WEEKLY_KEEP_DAYS=84   # keep Sunday dumps this far back (12 weeks)

log() { echo "[nts-backup $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

if [ ! -f "$DB_PATH" ]; then
    log "FATAL: live DB not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

TODAY=$(date -u +%F)                       # YYYY-MM-DD
DEST="$BACKUP_DIR/admin-$TODAY.db"

log "snapshotting $DB_PATH -> $DEST.gz (online .backup API)"
# .backup is safe against concurrent WAL writers. Snapshot to a temp name
# first so a crash mid-copy never leaves a truncated dated file behind.
TMP="$DEST.partial"
rm -f "$TMP" "$TMP-wal" "$TMP-shm"
sqlite3 "$DB_PATH" ".backup '$TMP'"

# Sanity-check the snapshot before we trust it as today's backup. The
# snapshot inherits WAL mode from the source, so opening it here spawns
# transient -wal/-shm sidecars — remove them so only the self-contained
# main file (all pages the backup API wrote) survives to be gzipped.
INTEGRITY=$(sqlite3 "$TMP" "PRAGMA integrity_check;" | head -1)
rm -f "$TMP-wal" "$TMP-shm"
if [ "$INTEGRITY" != "ok" ]; then
    log "FATAL: snapshot failed integrity_check: $INTEGRITY" >&2
    rm -f "$TMP"
    exit 1
fi

mv -f "$TMP" "$DEST"
gzip -f "$DEST"                            # -> $DEST.gz
log "wrote $DEST.gz ($(stat -c %s "$DEST.gz" 2>/dev/null || echo '?') bytes)"

# --- Retention -------------------------------------------------------------
# Keep: dumps <=30d old (daily) OR Sunday dumps <=84d old (weekly).
# Delete everything else. Dates are parsed from the filename, so this is
# independent of mtime (a re-run today never resets an old dump's age).
now_epoch=$(date -u +%s)
shopt -s nullglob
for f in "$BACKUP_DIR"/admin-*.db.gz; do
    base=$(basename "$f")
    d=${base#admin-}; d=${d%.db.gz}        # -> YYYY-MM-DD
    # Skip anything that doesn't parse as a date (defensive).
    f_epoch=$(date -u -d "$d" +%s 2>/dev/null) || continue
    age_days=$(( (now_epoch - f_epoch) / 86400 ))
    dow=$(date -u -d "$d" +%u)             # 1=Mon .. 7=Sun
    if [ "$age_days" -le "$DAILY_KEEP_DAYS" ]; then
        continue                           # within daily window
    fi
    if [ "$dow" = "7" ] && [ "$age_days" -le "$WEEKLY_KEEP_DAYS" ]; then
        continue                           # Sunday within weekly window
    fi
    log "retention: removing $base (age ${age_days}d, dow $dow)"
    rm -f "$f"
done
shopt -u nullglob

# --- Heartbeat -------------------------------------------------------------
# The nts-monitor alerter (pipeline.monitoring.alerts) reads this; if it is
# older than 26h it fires a Telegram alert. Limitation: this cannot fire if
# the whole VPS is down — external monitoring is a separate future item.
date -u +%Y-%m-%dT%H:%M:%SZ > "$HEARTBEAT"
log "OK; heartbeat -> $HEARTBEAT"
