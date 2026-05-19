#!/usr/bin/env bash
# Restore from a specific backup timestamp.
# Usage: ./restore.sh 20260712T030001Z
# Test this on a dev VPS BEFORE prod (Master Doc §A.7.2 Buffer).

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <TIMESTAMP, e.g. 20260712T030001Z>"
    exit 64
fi
TS=$1

cd /opt/news-to-socials
# shellcheck disable=SC1091
source ./.env

OUT_DIR=/var/lib/news-to-socials/backups
PG_FILE="$OUT_DIR/pg-$TS.sql.gz"
SQ_FILE="$OUT_DIR/pipeline-$TS.db.gz"
UP_FILE="$OUT_DIR/uploads-$TS.tar.gz"

# Pull from Hetzner if not local
for f in "$PG_FILE" "$SQ_FILE" "$UP_FILE"; do
    if [ ! -f "$f" ] && [ -n "${RCLONE_REMOTE:-}" ]; then
        rclone copy "$RCLONE_REMOTE$(basename "$f")" "$OUT_DIR/"
    fi
done

# 1. Postgres
echo "[1/3] Restoring Postgres…"
gunzip -c "$PG_FILE" | \
    docker compose -f cms/docker-compose.yml exec -T postgres \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB"

# 2. SQLite
if [ -f "$SQ_FILE" ]; then
    echo "[2/3] Restoring pipeline.db…"
    sudo systemctl stop news-bot.service || true
    gunzip -c "$SQ_FILE" > /var/lib/news-to-socials/pipeline.db
    chown news-deploy:news-deploy /var/lib/news-to-socials/pipeline.db
    sudo systemctl start news-bot.service
fi

# 3. Uploads
echo "[3/3] Restoring Directus uploads…"
docker run --rm \
    -v cms-directus-uploads:/uploads \
    -v "$OUT_DIR":/in:ro \
    alpine:3 \
    sh -c "rm -rf /uploads/* && tar xzf /in/uploads-$TS.tar.gz -C / "

echo "Restore complete: $TS"
