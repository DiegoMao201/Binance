#!/usr/bin/env bash
set -euo pipefail

APP_ID="${1:-o4w1ns4cceccmn2ozqt7sol2}"
ENV_FILE="/data/coolify/applications/${APP_ID}/.env"
BOT_CONTAINER="${APP_ID}"
AI_CONTAINER="${APP_ID}-ai"
NETWORK="${NETWORK:-coolify}"
LOGS_VOLUME="${LOGS_VOLUME:-/data/deriv-logs:/data/logs}"
AI_CMD="python -m scripts.dynamic_ai_orchestrator"

if ! docker ps --format '{{.Names}}' | grep -qx "${BOT_CONTAINER}"; then
  echo "[ai-sync] bot container ${BOT_CONTAINER} not running; skip"
  exit 0
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "[ai-sync] env file missing: ${ENV_FILE}"
  exit 1
fi

BOT_IMAGE="$(docker inspect -f '{{.Config.Image}}' "${BOT_CONTAINER}")"
CURRENT_AI_IMAGE=""
CURRENT_AI_CMD=""

if docker ps -a --format '{{.Names}}' | grep -qx "${AI_CONTAINER}"; then
  CURRENT_AI_IMAGE="$(docker inspect -f '{{.Config.Image}}' "${AI_CONTAINER}")"
  CURRENT_AI_CMD="$(docker inspect -f '{{join .Config.Cmd " "}}' "${AI_CONTAINER}")"
fi

if [[ "${CURRENT_AI_IMAGE}" == "${BOT_IMAGE}" && "${CURRENT_AI_CMD}" == "${AI_CMD}" ]]; then
  if docker ps --format '{{.Names}}' | grep -qx "${AI_CONTAINER}"; then
    echo "[ai-sync] up-to-date (${AI_CONTAINER} -> ${BOT_IMAGE})"
    exit 0
  fi
fi

echo "[ai-sync] reconcile ${AI_CONTAINER} -> ${BOT_IMAGE}"
docker rm -f "${AI_CONTAINER}" >/dev/null 2>&1 || true
docker run -d \
  --name "${AI_CONTAINER}" \
  --network "${NETWORK}" \
  --restart unless-stopped \
  --env-file "${ENV_FILE}" \
  -v "${LOGS_VOLUME}" \
  "${BOT_IMAGE}" \
  python -m scripts.dynamic_ai_orchestrator >/dev/null

echo "[ai-sync] done"
