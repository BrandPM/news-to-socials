#!/usr/bin/env bash
# Daily backup: Postgres dump + SQLite + Directus uploads → Hetzner Storage Box.
# Cron: 03:00 UTC. Retention: 30 days local, then rclone clean-up.
#
# Env (sourced from /opt/news-to-socials/.env):
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
#   RCLONE_REMOTE  (e.g. "hetzner-storage:news-to-socials/")
#
# Restore: see ./restore.sh

set -euo pipefail

cd /opt/news-to-socials
# shellcheck disable=SC1091
source ./.env

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT_DIR=/var/lib/news-to-socials/backups
mkdir -p "$OUT_DIR"

# 1. Postgres dump (Directus)
PGPASSWORD="$POSTGRES_PASSWORD" \
    docker compose -f cms/docker-compose.yml exec -T postgres \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  | gzip > "$OUT_DIR/pg-$TS.sql.gz"

# 2. Pipeline SQLite (queue + dedup state)
if [ -f "/var/lib/news-to-socials/pipeline.db" ]; then
    sqlite3 /var/lib/news-to-socials/pipeline.db ".backup '$OUT_DIR/pipeline-$TS.db'"
    gzip "$OUT_DIR/pipeline-$TS.db"
fi

# 3. Directus uploads
docker run --rm \
    -v cms-directus-uploads:/uploads:ro \
    -v "$OUT_DIR":/out \
    alpine:3 \
    tar czf "/out/uploads-$TS.tar.gz" -C / uploads

# 4. Sync to Hetzner Storage Box
if [ -n "${RCLONE_REMOTE:-}" ]; then
    rclone copy "$OUT_DIR" "$RCLONE_REMOTE" \
        --include "*-$TS.*" --transfers 4 --checkers 8
fi

# 5. Local retention: 30 days
find "$OUT_DIR" -type f -mtime +30 -delete

echo "OK $TS"
