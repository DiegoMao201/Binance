#!/usr/bin/env bash
# coolify_env_guard.sh — Idempotent guard against Coolify .env regeneration.
#
# Problem: Coolify regenerates the application .env file from its panel on every
# deploy, wiping any manual `>>` appends. The Fase A env vars (per-symbol
# guardrails added 2026-05-29) are NOT in the Coolify panel and must persist.
#
# Behavior:
#   1. Read APP_BASE/.env.
#   2. For each REQUIRED_VAR, if missing OR value mismatches the canonical
#      value, append (or replace) it.
#   3. If anything changed, run `docker compose up -d --force-recreate` for
#      the app and then sync the AI sidecar via the existing sync script.
#   4. If nothing changed, exit silently (so cron is quiet).
#
# Safe to run every minute (idempotent + only acts on actual diffs).
#
# Install on server as: /opt/deriv-ai-sync/coolify_env_guard.sh
# Cron entry:  * * * * * /opt/deriv-ai-sync/coolify_env_guard.sh >>/var/log/coolify_env_guard.log 2>&1
#
# 2026-05-29  Fase H (operativo)

set -euo pipefail

APP_ID="${APP_ID:-o4w1ns4cceccmn2ozqt7sol2}"
APP_BASE="${APP_BASE:-/data/coolify/applications/${APP_ID}}"
ENV_FILE="${APP_BASE}/.env"
SIDECAR_NAME="${SIDECAR_NAME:-${APP_ID}-ai}"
SYNC_SCRIPT="${SYNC_SCRIPT:-/opt/deriv-ai-sync/deriv_ai_sidecar_sync.sh}"
LOCK_FILE="/var/run/coolify_env_guard.${APP_ID}.lock"

# Canonical Fase A vars derived from INFORME_PROFUNDO_72H_2026-05-29.md.
# Format: KEY=VALUE  (one per array entry). Whitespace-sensitive.
REQUIRED_VARS=(
  "DYNAMIC_AI_SYMBOL_DD_FLOOR_USDT=-3.0"
  "DYNAMIC_AI_SYMBOL_LOCKOUT_SEC=7200"
  "DYNAMIC_AI_SYMBOL_DD_MIN_TRADES=6"
  "DYNAMIC_AI_24H_FEEDBACK_INTERVAL_SEC=21600"
  "DERIV_SYMBOL_HOUR_VETO_MAP=CRASH500:21,BOOM600:15,BOOM500:6,BOOM500:7,BOOM500:11"
)

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }

# Acquire lock to avoid overlapping cron runs.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  log "another run in progress; exiting"
  exit 0
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  log "ERROR env file not found: ${ENV_FILE}"
  exit 1
fi

CHANGED=0

for KV in "${REQUIRED_VARS[@]}"; do
  KEY="${KV%%=*}"
  VAL="${KV#*=}"
  CURRENT_LINE="$(grep -E "^${KEY}=" "${ENV_FILE}" || true)"
  CURRENT_VAL="${CURRENT_LINE#*=}"

  if [[ -z "${CURRENT_LINE}" ]]; then
    log "MISSING ${KEY} -> append"
    echo "${KV}" >> "${ENV_FILE}"
    CHANGED=1
  elif [[ "${CURRENT_VAL}" != "${VAL}" ]]; then
    log "MISMATCH ${KEY} (have=${CURRENT_VAL} want=${VAL}) -> replace"
    # Use # delimiter to tolerate / and : in VAL
    ESC_VAL="$(printf '%s' "${VAL}" | sed -e 's/[\/&]/\\&/g')"
    sed -i "s#^${KEY}=.*#${KEY}=${ESC_VAL}#" "${ENV_FILE}"
    CHANGED=1
  fi
done

if [[ "${CHANGED}" -eq 0 ]]; then
  # quiet exit
  exit 0
fi

log "env file mutated; force-recreating bot container"
cd "${APP_BASE}"
if ! docker compose up -d --force-recreate; then
  log "ERROR docker compose up failed"
  exit 2
fi

if [[ -x "${SYNC_SCRIPT}" ]]; then
  log "syncing AI sidecar via ${SYNC_SCRIPT}"
  if ! "${SYNC_SCRIPT}" "${APP_ID}"; then
    log "WARN sidecar sync failed (non-fatal)"
  fi
else
  log "WARN sidecar sync script not executable at ${SYNC_SCRIPT}"
fi

log "guard cycle complete (changed=${CHANGED})"
