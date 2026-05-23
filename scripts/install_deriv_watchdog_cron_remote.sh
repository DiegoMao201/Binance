#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-192.81.216.49}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_APP_ID="${REMOTE_APP_ID:-o4w1ns4cceccmn2ozqt7sol2}"
REMOTE_ENV_FILE="/data/coolify/applications/${REMOTE_APP_ID}/.env"
REMOTE_WATCHDOG_DIR="${REMOTE_WATCHDOG_DIR:-/opt/deriv-watchdog}"
REMOTE_LOGS_DIR="${REMOTE_LOGS_DIR:-/data/deriv-logs}"
CRON_FILE="/etc/cron.d/deriv-runtime-watchdog"

SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=no)

if [[ -n "${REMOTE_PASSWORD:-}" ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "[install-watchdog] ERROR: REMOTE_PASSWORD set but sshpass is not installed"
    exit 2
  fi
  SSH_CMD=(sshpass -p "$REMOTE_PASSWORD" ssh "${SSH_OPTS[@]}")
  SCP_CMD=(sshpass -p "$REMOTE_PASSWORD" scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
else
  SSH_CMD=(ssh "${SSH_OPTS[@]}")
  SCP_CMD=(scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
fi

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

echo "[install-watchdog] Preparing remote directory ${REMOTE_WATCHDOG_DIR}"
"${SSH_CMD[@]}" "$REMOTE" "mkdir -p '${REMOTE_WATCHDOG_DIR}' '${REMOTE_LOGS_DIR}'"

echo "[install-watchdog] Uploading validator and watchdog"
"${SCP_CMD[@]}" "$ROOT_DIR/scripts/validate_deriv_runtime.py" "$REMOTE:${REMOTE_WATCHDOG_DIR}/validate_deriv_runtime.py"
"${SCP_CMD[@]}" "$ROOT_DIR/scripts/deriv_runtime_watchdog.py" "$REMOTE:${REMOTE_WATCHDOG_DIR}/deriv_runtime_watchdog.py"

echo "[install-watchdog] Writing remote runner script"
"${SSH_CMD[@]}" "$REMOTE" "cat > '${REMOTE_WATCHDOG_DIR}/run_watchdog.sh' <<'SH'
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE='${REMOTE_ENV_FILE}'
WATCHDOG_DIR='${REMOTE_WATCHDOG_DIR}'
WATCHDOG_LOGS_DIR='${REMOTE_LOGS_DIR}'

if [[ -f \"\$ENV_FILE\" ]]; then
  set -a
  # shellcheck disable=SC1090
  source \"\$ENV_FILE\"
  set +a
fi

mkdir -p "\$WATCHDOG_LOGS_DIR"

exec /usr/bin/flock -n /tmp/deriv_runtime_watchdog.lock \
  /usr/bin/env python3 "\$WATCHDOG_DIR/deriv_runtime_watchdog.py" \
    --validator "\$WATCHDOG_DIR/validate_deriv_runtime.py" \
    --logs-dir "\$WATCHDOG_LOGS_DIR" \
    --state-file "\$WATCHDOG_LOGS_DIR/deriv_runtime_watchdog_state.json" \
    --check-db \
    --alert-reminder-min 30
SH
chmod +x '${REMOTE_WATCHDOG_DIR}/run_watchdog.sh'"

echo "[install-watchdog] Installing cron schedule (every 5 minutes)"
"${SSH_CMD[@]}" "$REMOTE" "cat > '${CRON_FILE}' <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

*/5 * * * * root ${REMOTE_WATCHDOG_DIR}/run_watchdog.sh >> ${REMOTE_LOGS_DIR}/deriv_runtime_watchdog.log 2>&1
CRON
chmod 644 '${CRON_FILE}'"

echo "[install-watchdog] Running one immediate test cycle"
"${SSH_CMD[@]}" "$REMOTE" "${REMOTE_WATCHDOG_DIR}/run_watchdog.sh || true"

echo "[install-watchdog] Installed. Current cron file:"
"${SSH_CMD[@]}" "$REMOTE" "cat '${CRON_FILE}'"

echo "[install-watchdog] Last watchdog log lines:"
"${SSH_CMD[@]}" "$REMOTE" "tail -n 20 '${REMOTE_LOGS_DIR}/deriv_runtime_watchdog.log' || true"