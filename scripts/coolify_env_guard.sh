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
  "DERIV_SYMBOL_HOUR_VETO_MAP=CRASH500:21,BOOM500:6,BOOM500:7,BOOM500:11"
  # Tick subscription: solo BOOM500/CRASH500 (activos) + BOOM1000/CRASH1000 (medición D10).
  # 600/900 completamente excluidos — sin ticks, sin trades, sin nada.
  "DERIV_SYMBOLS=BOOM500,CRASH500,BOOM1000,CRASH1000"
  # D.10.1 — Slope gate: 0s estabilización, C1 umbral 0.003%, pending C1=120s
  "DERIV_D10_SPIKE_STABILIZE_SEC=0"
  "DERIV_D10_PN5_STABILIZE_SEC=0"
  "DERIV_D10_C1_CAMBIO_MIN_PCT=0.003"
  "DERIV_D10_PENDING_CAMINO1_SEC=120"
  # D.9.0 — Hurst cancel: desactivado — no quiero que cancele ghost pending de D10
  "DERIV_D9_HURST_CANCEL_ENABLED=false"
  # 600/900/1000 completamente inhabilitados. 1000 mide pendiente pero no tradea.
  "DERIV_FORCE_DISABLED_SYMBOLS=BOOM300,BOOM600,CRASH600,BOOM900,CRASH900,BOOM1000,CRASH1000,R_50,R_75,R_100"
  # 2026-06-11: gate fixes — ghost data 361 blocks WR=100%, 0 LOSS en 24h
  "DERIV_DYNAMIC_STRUCTURAL_RELAX_BLOCK_SYMBOLS="
  "DERIV_ANTI_RETRACE_RANGE_FRAC=0.65"
  "DERIV_ANTI_RETRACE_HOT_BYPASS_MIN_SCORE=7.5"
  # 2026-06-11: TREND bloqueado — solo símbolos activos. 600/900 inhabilitados.
  "DERIV_TREND_BLOCK_SYMBOLS=BOOM500,BOOM1000,CRASH500,CRASH1000"
  "DERIV_TREND_SETUP_MIN_SCORE=7.0"
  # max_hold solo para símbolos activos (500) y medición (1000). 600/900 inhabilitados.
  "DERIV_MAX_HOLD_CRASH500=480"
  "DERIV_MAX_HOLD_CRASH1000=700"
  "DERIV_MAX_HOLD_BOOM500=480"
  "DERIV_MAX_HOLD_BOOM1000=700"
  # 2026-06-11: vision LLM model correcto (gemini-flash-1.5 no existe en OpenRouter)
  "DYNAMIC_AI_VISION_MODEL=google/gemini-2.5-flash-lite"
  # 2026-06-12: estructura 15m cada 5min (era 900s=15min, demasiado lenta para contexto bot)
  "DYNAMIC_AI_VISION_CACHE_SEC=300"
  # 2026-06-12: LISTO_RIPE_BYPASS — entra ANTES del spike cuando LISTO+RIPE+score≥6.0
  "DERIV_LISTO_RIPE_BYPASS_MIN_SCORE=6.0"
  # HOT_REENTRY: requiere cluster ≥2 spikes — no re-entrada tras spike solitario
  "DERIV_HOT_REENTRY_MIN_SPIKES=2"
  # MATURITY_GATE: revertido a 0.70 — 1.80 creaba gap imposible (FVG TTL=600s < threshold 795s)
  # El FVG ancla dura 600s. Con 0.70×P50 (244-400s), el FVG sigue activo al pasar el gate.
  "DERIV_MATURITY_GATE_FRAC=0.70"
  "DERIV_DRY_OVERRIDE_SCORE_MAP="
)

FRONTEND_ID="${FRONTEND_ID:-m0ks004osk4cw444gsokg8os}"
FRONTEND_ENV="/data/coolify/applications/${FRONTEND_ID}/.env"
FRONTEND_REQUIRED_VARS=(
  "DERIV_D10_C1_CAMBIO_MIN_PCT=0.003"
  "DERIV_D10_PENDING_CAMINO1_SEC=120"
  "DERIV_D9_HURST_CANCEL_ENABLED=false"
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

# Ensure the file ends with a newline before appending. Coolify sometimes
# regenerates .env WITHOUT a trailing newline, which would cause our first
# `echo "$KV" >> file` to glue our key onto the previous line and corrupt
# both variables (e.g. TRADE_COOLDOWN_MINUTES=3DYNAMIC_AI_...=-3.0).
if [[ -s "${ENV_FILE}" ]] && [[ "$(tail -c 1 "${ENV_FILE}" | wc -l | tr -d ' ')" -eq 0 ]]; then
  echo "" >> "${ENV_FILE}"
  log "appended trailing newline to env (was missing)"
fi

CHANGED=0

# Ensure volume mount for patched main_deriv.py survives Coolify redeploys.
PATCH_MOUNT="'\/data\/deriv-bot-patches\/main_deriv.py:\/app\/src\/main_deriv.py'"
COMPOSE_FILE="${APP_BASE}/docker-compose.yaml"
if [[ -f "${COMPOSE_FILE}" ]] && ! grep -q "deriv-bot-patches/main_deriv.py" "${COMPOSE_FILE}"; then
  log "MISSING volume mount for main_deriv.py patch → adding to compose"
  sed -i "s|'/data/deriv-logs:/data/logs'|'/data/deriv-logs:/data/logs'\n            - '/data/deriv-bot-patches/main_deriv.py:/app/src/main_deriv.py'|" "${COMPOSE_FILE}"
  CHANGED=1
fi

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

# Also guard frontend env vars
FRONTEND_CHANGED=0
if [[ -f "${FRONTEND_ENV}" ]]; then
  for KV in "${FRONTEND_REQUIRED_VARS[@]}"; do
    KEY="${KV%%=*}"; VAL="${KV#*=}"
    CURRENT_LINE="$(grep -E "^${KEY}=" "${FRONTEND_ENV}" || true)"
    CURRENT_VAL="${CURRENT_LINE#*=}"
    if [[ -z "${CURRENT_LINE}" ]]; then
      log "FRONTEND MISSING ${KEY} -> append"
      echo "${KV}" >> "${FRONTEND_ENV}"
      FRONTEND_CHANGED=1
    elif [[ "${CURRENT_VAL}" != "${VAL}" ]]; then
      log "FRONTEND MISMATCH ${KEY} (have=${CURRENT_VAL} want=${VAL}) -> replace"
      ESC_VAL="$(printf '%s' "${VAL}" | sed -e 's/[\/&]/\\&/g')"
      sed -i "s#^${KEY}=.*#${KEY}=${ESC_VAL}#" "${FRONTEND_ENV}"
      FRONTEND_CHANGED=1
    fi
  done
  if [[ "${FRONTEND_CHANGED}" -eq 1 ]]; then
    log "frontend env mutated; recreating frontend container"
    cd "/data/coolify/applications/${FRONTEND_ID}"
    docker compose up -d --force-recreate || log "WARN frontend recreate failed (non-fatal)"
    cd "${APP_BASE}"
  fi
fi

if [[ "${CHANGED}" -eq 0 && "${FRONTEND_CHANGED}" -eq 0 ]]; then
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
