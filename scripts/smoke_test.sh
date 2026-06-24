#!/usr/bin/env bash
# news-to-socials — production smoke test (NTS_076 audit deliverable).
#
# Idempotent invariant checks against a LIVE deployment. Prints PASS/FAIL per
# check and exits non-zero if any FAIL. Safe to re-run.
#
# Run on the VPS:
#     bash scripts/smoke_test.sh
#
# Reads ADMIN_TRIGGER_SECRET (+ optional knobs) from the repo .env.
# Env knobs (all optional):
#   BASE_URL            admin API base       (default http://127.0.0.1:8080)
#   ENV_FILE            path to .env         (default <repo>/.env)
#   SMOKE_BRAND_ID      brand for image-styles check        (default 1)
#   SMOKE_SOURCE_ID     source to trigger a run on  (default: first source)
#   SMOKE_TRIGGER_RUN   1=do the run create/pid/cancel check (default 1)
#   SMOKE_ZOMBIE_MIN    zombie threshold, minutes            (default 30)
#   HEALTH_MS           /health latency budget, ms           (default 100)
#
# NOTE: with SMOKE_TRIGGER_RUN=1 the script triggers a REAL pipeline run and
# cancels it ~immediately (to prove detached-process + idempotent cancel).
# That leaves one 'cancelled' run row per invocation. Set SMOKE_TRIGGER_RUN=0
# to skip if you don't want that side effect.

set -uo pipefail

# --- locate repo root (this script lives in <repo>/scripts/) ---------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
SMOKE_BRAND_ID="${SMOKE_BRAND_ID:-1}"
SMOKE_TRIGGER_RUN="${SMOKE_TRIGGER_RUN:-1}"
SMOKE_ZOMBIE_MIN="${SMOKE_ZOMBIE_MIN:-30}"
HEALTH_MS="${HEALTH_MS:-100}"

# --- load .env (only the keys we need; tolerate quotes/spaces) -------------
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC2046
  ADMIN_TRIGGER_SECRET="${ADMIN_TRIGGER_SECRET:-$(grep -E '^ADMIN_TRIGGER_SECRET=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ' )}"
fi
ADMIN_TRIGGER_SECRET="${ADMIN_TRIGGER_SECRET:-}"

AUTH=(-H "X-Admin-Token: ${ADMIN_TRIGGER_SECRET}")
PASS=0; FAIL=0; SKIP=0

green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }
pass()  { PASS=$((PASS+1)); echo "  $(green PASS)  $1"; }
fail()  { FAIL=$((FAIL+1)); echo "  $(red FAIL)  $1"; }
skip()  { SKIP=$((SKIP+1)); echo "  SKIP  $1"; }

# JSON field extractor (python3 always present in this project).
jget() { python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(2)
k=sys.argv[1]
v=d
for part in k.split("."):
    if isinstance(v,list):
        try: part=int(part)
        except ValueError: print(""); sys.exit(0)
    try: v=v[part]
    except Exception: print(""); sys.exit(0)
print(v if v is not None else "")' "$1" 2>/dev/null; }

echo "== news-to-socials smoke test =="
echo "base=$BASE_URL  repo=$REPO_ROOT"
if [[ -z "$ADMIN_TRIGGER_SECRET" ]]; then
  echo "  $(red FATAL)  ADMIN_TRIGGER_SECRET not found (set it or ENV_FILE=$ENV_FILE)"; exit 2
fi

# --- 1. /health: 200 and under the latency budget --------------------------
echo "[1] GET /health (200 < ${HEALTH_MS}ms)"
HB=$(curl -s -o /tmp/_sm_health.json -w '%{http_code} %{time_total}' "$BASE_URL/health" 2>/dev/null || echo "000 9")
CODE="${HB%% *}"; T="${HB##* }"
MS=$(python3 -c "print(int(float('$T')*1000))" 2>/dev/null || echo 9999)
if [[ "$CODE" == "200" && "$MS" -lt "$HEALTH_MS" ]]; then pass "200 in ${MS}ms (ver=$(jget status </tmp/_sm_health.json >/dev/null; jget version </tmp/_sm_health.json))"
elif [[ "$CODE" == "200" ]]; then fail "200 but ${MS}ms >= ${HEALTH_MS}ms"
else fail "HTTP $CODE"; fi

# --- 2. /health/deep components --------------------------------------------
echo "[2] GET /api/v1/health/deep"
DCODE=$(curl -s "${AUTH[@]}" -o /tmp/_sm_deep.json -w '%{http_code}' "$BASE_URL/api/v1/health/deep" 2>/dev/null || echo 000)
if [[ "$DCODE" == "200" ]]; then
  DB=$(jget db </tmp/_sm_deep.json); SAN=$(jget sanity </tmp/_sm_deep.json)
  [[ "$DB" == "ok" ]] && pass "db=$DB" || fail "db=$DB"
  [[ -n "$SAN" ]] && pass "sanity=$SAN" || fail "sanity component missing"
else fail "HTTP $DCODE"; fi

# --- 3. image-styles endpoint (NTS_075) ------------------------------------
echo "[3] GET /api/v1/brands/$SMOKE_BRAND_ID/image-styles"
ICODE=$(curl -s "${AUTH[@]}" -o /tmp/_sm_styles.json -w '%{http_code}' "$BASE_URL/api/v1/brands/$SMOKE_BRAND_ID/image-styles" 2>/dev/null || echo 000)
[[ "$ICODE" == "200" ]] && pass "200 (styles key present: $([[ -n "$(jget styles.0 </tmp/_sm_styles.json)" ]] && echo yes || echo empty-list-ok))" || fail "HTTP $ICODE"

# --- 4. alembic current == head --------------------------------------------
echo "[4] alembic current == head (013)"
ALEMBIC="alembic"; [[ -x "$REPO_ROOT/.venv/bin/alembic" ]] && ALEMBIC="$REPO_ROOT/.venv/bin/alembic"
CUR=$("$ALEMBIC" current 2>/dev/null | grep -oE '[0-9a-z_]+ \(head\)|[0-9a-z_]+' | head -1 | awk '{print $1}')
HEAD=$("$ALEMBIC" heads 2>/dev/null | awk '{print $1}' | head -1)
if [[ -n "$CUR" && "$CUR" == "$HEAD" ]]; then pass "current=$CUR == head"
elif [[ -z "$CUR" || -z "$HEAD" ]]; then fail "could not read alembic state (cur='$CUR' head='$HEAD')"
else fail "current=$CUR != head=$HEAD — run 'alembic upgrade head'"; fi

# --- 5. no zombie 'running' runs older than N minutes ----------------------
echo "[5] no 'running' runs older than ${SMOKE_ZOMBIE_MIN}m"
ZCODE=$(curl -s "${AUTH[@]}" -o /tmp/_sm_running.json -w '%{http_code}' "$BASE_URL/api/v1/runs?status=running" 2>/dev/null || echo 000)
if [[ "$ZCODE" == "200" ]]; then
  ZOMBIES=$(python3 -c "
import sys,json,datetime
try: rows=json.load(open('/tmp/_sm_running.json'))
except Exception: print('ERR'); sys.exit()
if not isinstance(rows,list): print('ERR'); sys.exit()
now=datetime.datetime.now(datetime.timezone.utc); n=0
for r in rows:
    s=r.get('started_at')
    if not s: continue
    try: t=datetime.datetime.fromisoformat(s.replace('Z','+00:00'))
    except Exception: continue
    if t.tzinfo is None: t=t.replace(tzinfo=datetime.timezone.utc)
    if (now-t).total_seconds() > $SMOKE_ZOMBIE_MIN*60: n+=1
print(n)" 2>/dev/null)
  if [[ "$ZOMBIES" == "0" ]]; then pass "no stale running runs"
  elif [[ "$ZOMBIES" == "ERR" ]]; then fail "could not parse runs list"
  else fail "$ZOMBIES running run(s) older than ${SMOKE_ZOMBIE_MIN}m (stale-run sweeper should force-fail >6h)"; fi
else fail "HTTP $ZCODE"; fi

# --- 6. run lifecycle: create → detached pid → idempotent cancel -----------
echo "[6] run create → pid recorded → cancel idempotent"
if [[ "$SMOKE_TRIGGER_RUN" != "1" ]]; then
  skip "disabled (SMOKE_TRIGGER_RUN=$SMOKE_TRIGGER_RUN)"
else
  SRC="${SMOKE_SOURCE_ID:-}"
  if [[ -z "$SRC" ]]; then
    curl -s "${AUTH[@]}" -o /tmp/_sm_src.json "$BASE_URL/api/v1/sources" 2>/dev/null
    SRC=$(jget 0.id </tmp/_sm_src.json)
  fi
  if [[ -z "$SRC" ]]; then
    skip "no source available to trigger a run (set SMOKE_SOURCE_ID)"
  else
    RID=$(curl -s "${AUTH[@]}" -H "X-Triggered-By: cli" -X POST "$BASE_URL/api/v1/sources/$SRC/run" 2>/dev/null | jget run_id)
    if [[ -z "$RID" ]]; then fail "trigger returned no run_id"; else
      pass "triggered run_id=$RID on source $SRC"
      # poll up to ~5s for the detached worker to record its pid
      PID=""; for _ in 1 2 3 4 5 6 7 8 9 10; do
        PID=$(curl -s "${AUTH[@]}" "$BASE_URL/api/v1/runs/$RID" 2>/dev/null | jget pid)
        [[ -n "$PID" && "$PID" != "None" ]] && break; sleep 0.5; done
      [[ -n "$PID" && "$PID" != "None" ]] && pass "detached worker pid=$PID recorded" || fail "no pid recorded (run not detached?)"
      # cancel, then cancel again — must be idempotent (both 200, status cancelled)
      C1=$(curl -s "${AUTH[@]}" -o /tmp/_sm_c1.json -w '%{http_code}' -X POST "$BASE_URL/api/v1/runs/$RID/cancel" 2>/dev/null)
      C2=$(curl -s "${AUTH[@]}" -o /tmp/_sm_c2.json -w '%{http_code}' -X POST "$BASE_URL/api/v1/runs/$RID/cancel" 2>/dev/null)
      ST=$(jget status </tmp/_sm_c2.json)
      if [[ "$C1" == "200" && "$C2" == "200" ]]; then pass "cancel idempotent (status=$ST)"
      else fail "cancel not idempotent (1st=$C1 2nd=$C2)"; fi
    fi
  fi
fi

# --- summary ---------------------------------------------------------------
echo "-----------------------------------------"
echo "PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
[[ "$FAIL" -eq 0 ]] && { echo "$(green 'SMOKE OK')"; exit 0; } || { echo "$(red 'SMOKE FAILED')"; exit 1; }
