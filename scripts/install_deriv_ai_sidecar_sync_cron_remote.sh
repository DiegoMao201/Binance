#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-192.81.216.49}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_APP_ID="${REMOTE_APP_ID:-o4w1ns4cceccmn2ozqt7sol2}"
REMOTE_SYNC_DIR="${REMOTE_SYNC_DIR:-/opt/deriv-ai-sync}"
REMOTE_LOGS_DIR="${REMOTE_LOGS_DIR:-/data/deriv-logs}"
CRON_FILE="/etc/cron.d/deriv-ai-sidecar-sync"

SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=no)

if [[ -n "${REMOTE_PASSWORD:-}" ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "[install-ai-sync] ERROR: REMOTE_PASSWORD set but sshpass is not installed"
    exit 2
  fi
  SSH_CMD=(sshpass -p "$REMOTE_PASSWORD" ssh "${SSH_OPTS[@]}")
  SCP_CMD=(sshpass -p "$REMOTE_PASSWORD" scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
else
  SSH_CMD=(ssh "${SSH_OPTS[@]}")
  SCP_CMD=(scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
fi

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

echo "[install-ai-sync] Preparing ${REMOTE_SYNC_DIR}"
"${SSH_CMD[@]}" "$REMOTE" "mkdir -p '${REMOTE_SYNC_DIR}' '${REMOTE_LOGS_DIR}'"

echo "[install-ai-sync] Uploading sync script"
"${SCP_CMD[@]}" "$ROOT_DIR/scripts/deriv_ai_sidecar_sync.sh" "$REMOTE:${REMOTE_SYNC_DIR}/deriv_ai_sidecar_sync.sh"

echo "[install-ai-sync] Writing runner"
"${SSH_CMD[@]}" "$REMOTE" "cat > '${REMOTE_SYNC_DIR}/run_ai_sync.sh' <<'SH'
#!/usr/bin/env bash
set -euo pipefail

APP_ID='${REMOTE_APP_ID}'
SYNC_DIR='${REMOTE_SYNC_DIR}'
LOGS_DIR='${REMOTE_LOGS_DIR}'

mkdir -p "\$LOGS_DIR"

exec /usr/bin/flock -n /tmp/deriv_ai_sidecar_sync.lock \
  "\$SYNC_DIR/deriv_ai_sidecar_sync.sh" "\$APP_ID"
SH
chmod +x '${REMOTE_SYNC_DIR}/run_ai_sync.sh'
chmod +x '${REMOTE_SYNC_DIR}/deriv_ai_sidecar_sync.sh'"

echo "[install-ai-sync] Installing cron (every 3 minutes)"
"${SSH_CMD[@]}" "$REMOTE" "cat > '${CRON_FILE}' <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

*/3 * * * * root ${REMOTE_SYNC_DIR}/run_ai_sync.sh >> ${REMOTE_LOGS_DIR}/deriv_ai_sidecar_sync.log 2>&1
CRON
chmod 644 '${CRON_FILE}'"

echo "[install-ai-sync] Running immediate sync"
"${SSH_CMD[@]}" "$REMOTE" "${REMOTE_SYNC_DIR}/run_ai_sync.sh"

echo "[install-ai-sync] Installed cron file:"
"${SSH_CMD[@]}" "$REMOTE" "cat '${CRON_FILE}'"

echo "[install-ai-sync] Last log lines:"
"${SSH_CMD[@]}" "$REMOTE" "tail -n 20 '${REMOTE_LOGS_DIR}/deriv_ai_sidecar_sync.log' || true"
