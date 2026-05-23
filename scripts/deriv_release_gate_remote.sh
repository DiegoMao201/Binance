#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-192.81.216.49}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_APP_ID="${REMOTE_APP_ID:-o4w1ns4cceccmn2ozqt7sol2}"
REMOTE_ENV_FILE="/data/coolify/applications/${REMOTE_APP_ID}/.env"
REMOTE_VALIDATOR="/tmp/validate_deriv_runtime.py"
REMOTE_LOGS_DIR="${REMOTE_LOGS_DIR:-/data/deriv-logs}"

SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=no)

if [[ -n "${REMOTE_PASSWORD:-}" ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "[release-gate-remote] ERROR: REMOTE_PASSWORD set but sshpass is not installed"
    exit 2
  fi
  SSH_CMD=(sshpass -p "$REMOTE_PASSWORD" ssh "${SSH_OPTS[@]}")
  SCP_CMD=(sshpass -p "$REMOTE_PASSWORD" scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
else
  SSH_CMD=(ssh "${SSH_OPTS[@]}")
  SCP_CMD=(scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
fi

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

echo "[release-gate-remote] Uploading validator to ${REMOTE}:${REMOTE_VALIDATOR}"
"${SCP_CMD[@]}" "$ROOT_DIR/scripts/validate_deriv_runtime.py" "$REMOTE:${REMOTE_VALIDATOR}"

echo "[release-gate-remote] Running preflight validator on remote host"
set +e
OUT="$("${SSH_CMD[@]}" "$REMOTE" "set -a; source '${REMOTE_ENV_FILE}'; set +a; python3 '${REMOTE_VALIDATOR}' --logs-dir '${REMOTE_LOGS_DIR}' --check-db --json" 2>&1)"
RC=$?
set -e

echo "$OUT"

if [[ $RC -eq 0 ]]; then
  echo "[release-gate-remote] PASS: release gate green"
  exit 0
fi

echo "[release-gate-remote] BLOCKED: release gate failed"
exit 1