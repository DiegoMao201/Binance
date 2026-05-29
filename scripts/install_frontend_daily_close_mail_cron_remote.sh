#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-192.81.216.49}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_PORT="${REMOTE_PORT:-22}"
FRONTEND_APP_ID="${FRONTEND_APP_ID:-m0ks004osk4cw444gsokg8os}"
FRONTEND_URL="${FRONTEND_URL:-https://tradingdiegomao.datovatenexuspro.com}"
REMOTE_ENV_FILE="/data/coolify/applications/${FRONTEND_APP_ID}/.env"
REMOTE_RUN_DIR="${REMOTE_RUN_DIR:-/opt/optiferre-daily-close-mail}"
REMOTE_LOG_FILE="${REMOTE_LOG_FILE:-/data/deriv-logs/daily_close_email_cron.log}"
CRON_FILE="/etc/cron.d/optiferre-daily-close-mail"

SSH_OPTS=(-p "$REMOTE_PORT" -o StrictHostKeyChecking=no)

if [[ -n "${REMOTE_PASSWORD:-}" ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "[install-daily-mail] ERROR: REMOTE_PASSWORD set but sshpass is not installed"
    exit 2
  fi
  SSH_CMD=(sshpass -p "$REMOTE_PASSWORD" ssh "${SSH_OPTS[@]}")
  SCP_CMD=(sshpass -p "$REMOTE_PASSWORD" scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
else
  SSH_CMD=(ssh "${SSH_OPTS[@]}")
  SCP_CMD=(scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no)
fi

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

echo "[install-daily-mail] Preparing remote directories"
"${SSH_CMD[@]}" "$REMOTE" "mkdir -p '${REMOTE_RUN_DIR}' '$(dirname "$REMOTE_LOG_FILE")'"

TMP_RUNNER="$(mktemp)"
trap 'rm -f "$TMP_RUNNER"' EXIT

cat > "$TMP_RUNNER" <<EOF
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE='${REMOTE_ENV_FILE}'
FRONTEND_URL='${FRONTEND_URL}'

if [[ -f "\$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "\$line" ]]; do
    [[ -z "\$line" || "\$line" =~ ^[[:space:]]*# ]] && continue
    key="\${line%%=*}"
    value="\${line#*=}"
    key="\$(echo "\$key" | tr -d '[:space:]')"
    [[ -z "\$key" ]] && continue
    export "\$key=\$value"
  done < "\$ENV_FILE"
fi

SECRET="\${DAILY_CLOSE_EMAIL_SECRET:-\${WEBHOOK_SECRET:-}}"
if [[ -z "\$SECRET" ]]; then
  echo "[daily-close-mail] ERROR: DAILY_CLOSE_EMAIL_SECRET/WEBHOOK_SECRET missing in env"
  exit 1
fi

TARGET_DAY="\$(date -u -d 'yesterday' +%F)"
EXTRA_QUERY=""
if [[ "\${1:-}" == "--dry-run" ]]; then
  EXTRA_QUERY="&dryRun=1"
fi

curl -fsS -X POST \
  "\${FRONTEND_URL}/api/internal/daily-close-emails?date=\${TARGET_DAY}\${EXTRA_QUERY}" \
  -H "Authorization: Bearer \${SECRET}" \
  -H 'Content-Type: application/json'
EOF

echo "[install-daily-mail] Uploading remote runner script"
"${SCP_CMD[@]}" "$TMP_RUNNER" "$REMOTE:${REMOTE_RUN_DIR}/run_daily_close_email.sh"
"${SSH_CMD[@]}" "$REMOTE" "chmod +x '${REMOTE_RUN_DIR}/run_daily_close_email.sh'"

echo "[install-daily-mail] Installing cron schedule (00:05 UTC daily)"
"${SSH_CMD[@]}" "$REMOTE" "cat > '${CRON_FILE}' <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

5 0 * * * root /usr/bin/flock -n /tmp/optiferre_daily_close_email.lock ${REMOTE_RUN_DIR}/run_daily_close_email.sh >> ${REMOTE_LOG_FILE} 2>&1
CRON
chmod 644 '${CRON_FILE}'"

echo "[install-daily-mail] Running dry-run test"
"${SSH_CMD[@]}" "$REMOTE" "${REMOTE_RUN_DIR}/run_daily_close_email.sh --dry-run || true"

echo "[install-daily-mail] Installed cron file:"
"${SSH_CMD[@]}" "$REMOTE" "cat '${CRON_FILE}'"

echo "[install-daily-mail] Last log lines:"
"${SSH_CMD[@]}" "$REMOTE" "tail -n 20 '${REMOTE_LOG_FILE}' || true"
