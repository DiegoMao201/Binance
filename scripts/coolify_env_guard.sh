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
  # 2026-06-01: CRASH900 reactivado — debe seguir FUERA de force-disabled aunque
  # Coolify regenere .env desde su DB. Guard self-healing enforcement.
  "DERIV_FORCE_DISABLED_SYMBOLS=BOOM300,BOOM900,BOOM1000,CRASH1000,R_50,R_75,R_100"
  # 2026-06-11: gate fixes — ghost data 361 blocks WR=100%, 0 LOSS en 24h
  "DERIV_DYNAMIC_STRUCTURAL_RELAX_BLOCK_SYMBOLS=BOOM900,CRASH900"
  "DERIV_ANTI_RETRACE_RANGE_FRAC=0.65"
  "DERIV_ANTI_RETRACE_HOT_BYPASS_MIN_SCORE=7.5"
  # 2026-06-11: TREND bloqueado POR SÍMBOLO — solo CRASH (WR=27-38%). BOOM500/600 usan 7.0.
  # CRASH WR: CRASH500=27.1%, CRASH600=38.2%, CRASH900=28.6% → pérdidas sistemáticas.
  # BOOM500 TREND WR=54.5% → no bloquear. BOOM600 sin historial TREND → no bloquear.
  "DERIV_TREND_BLOCK_SYMBOLS=BOOM500,BOOM600,BOOM900,BOOM1000,CRASH500,CRASH600,CRASH900,CRASH1000"
  "DERIV_TREND_SETUP_MIN_SCORE=7.0"
  # 2026-06-11: max_hold reducido 900→480s — 110 trades timeout costaron -$160 (avg -$1.46 cada uno)
  # 2026-06-12: CRASH900 max_hold 600→750s — P50 gap=523t, con 600s solo 55% prob spike; 750s → ~62%
  "DERIV_MAX_HOLD_CRASH500=480"
  "DERIV_MAX_HOLD_CRASH600=540"
  "DERIV_MAX_HOLD_CRASH900=750"
  "DERIV_MAX_HOLD_CRASH1000=700"
  "DERIV_MAX_HOLD_BOOM500=480"
  "DERIV_MAX_HOLD_BOOM600=540"
  "DERIV_MAX_HOLD_BOOM900=600"
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
  # DRY_GATE por símbolo: BOOM600 en SECO puede entrar a score≥6.5 (sin FVG activo)
  # CRASH900 ya estaba en 6.0. Ahora BOOM600 también puede entrar en sequía.
  "DERIV_DRY_OVERRIDE_SCORE_MAP=CRASH900:6.0,BOOM600:6.5"
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
