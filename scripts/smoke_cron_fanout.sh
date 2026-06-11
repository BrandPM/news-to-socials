#!/usr/bin/env bash
#
# IT_PROJ_NTS_056 Task 5 — post-deploy smoke for the cron multilingual fanout.
#
# Runs ON THE VPS. Triggers POST /sources/run-all exactly the way the cron
# systemd unit does (X-Triggered-By: cron), then polls the run until it
# finishes and asserts:
#   - triggered_by == "cron"
#   - languages_completed covers all of the brand's languages (4 for Icon)
#
# Usage (on the VPS):
#   bash scripts/smoke_cron_fanout.sh                 # brand_id=1, defaults
#   BRAND_ID=1 TIMEOUT=900 bash scripts/smoke_cron_fanout.sh
#
# Reads ADMIN_TRIGGER_SECRET from the repo .env. NEVER prints the token.
set -euo pipefail

API="${API:-http://127.0.0.1:8080/api/v1}"
ENV_FILE="${ENV_FILE:-/opt/news-to-socials/repo/.env}"
BRAND_ID="${BRAND_ID:-1}"
TIMEOUT="${TIMEOUT:-900}"   # seconds to wait for the run to finish
POLL="${POLL:-10}"

if [[ -z "${ADMIN_TRIGGER_SECRET:-}" ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    ADMIN_TRIGGER_SECRET="$(grep -E '^ADMIN_TRIGGER_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  fi
fi
if [[ -z "${ADMIN_TRIGGER_SECRET:-}" ]]; then
  echo "ERROR: ADMIN_TRIGGER_SECRET not set and not found in $ENV_FILE" >&2
  exit 2
fi

echo "→ Triggering run-all (brand_id=$BRAND_ID, X-Triggered-By: cron)…"
RESP="$(curl -fsS -X POST \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: ${ADMIN_TRIGGER_SECRET}" \
  -H "X-Triggered-By: cron" \
  -d "{\"brand_id\": ${BRAND_ID}}" \
  "${API}/sources/run-all")"
RUN_ID="$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')"
echo "→ run_id=$RUN_ID"

echo "→ Polling /runs/$RUN_ID (timeout ${TIMEOUT}s)…"
elapsed=0
while (( elapsed < TIMEOUT )); do
  DETAIL="$(curl -fsS -H "X-Admin-Token: ${ADMIN_TRIGGER_SECRET}" "${API}/runs/${RUN_ID}")"
  STATUS="$(echo "$DETAIL" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run"]["status"])')"
  echo "   status=$STATUS (${elapsed}s)"
  [[ "$STATUS" == "running" ]] || break
  sleep "$POLL"
  elapsed=$(( elapsed + POLL ))
done

echo "$DETAIL" | python3 - "$RUN_ID" <<'PY'
import sys, json
detail = json.load(sys.stdin)
run = detail["run"]
trig = run.get("triggered_by")
langs = run.get("languages_completed") or []
print(f"→ triggered_by={trig!r}  languages_completed={langs}")
errs = []
if trig != "cron":
    errs.append(f"expected triggered_by='cron', got {trig!r}")
if len(set(langs)) < 4:
    errs.append(f"expected >=4 languages, got {langs}")
if errs:
    print("SMOKE FAILED:")
    for e in errs:
        print(f"  - {e}")
    sys.exit(1)
print("SMOKE PASSED ✅")
PY
