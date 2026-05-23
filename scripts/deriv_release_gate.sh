#!/usr/bin/env bash
set -euo pipefail

LOGS_DIR="${DERIV_LOGS_DIR:-/data/deriv-logs}"
VALIDATOR_PATH="${VALIDATOR_PATH:-scripts/validate_deriv_runtime.py}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -f "$VALIDATOR_PATH" ]]; then
  echo "[release-gate] ERROR: validator not found at $VALIDATOR_PATH"
  exit 2
fi

echo "[release-gate] Running Deriv runtime preflight..."
set +e
OUT="$($PYTHON_BIN "$VALIDATOR_PATH" --logs-dir "$LOGS_DIR" --check-db --json 2>&1)"
RC=$?
set -e

echo "$OUT"

if [[ $RC -eq 0 ]]; then
  echo "[release-gate] PASS: runtime validator is green."
  exit 0
fi

echo "[release-gate] BLOCKED: runtime validator failed."

# Human-friendly failure summary for CI logs.
echo "$OUT" | "$PYTHON_BIN" - <<'PY'
import json
import sys

raw = sys.stdin.read().strip()
try:
    payload = json.loads(raw)
except Exception:
    print("[release-gate] Could not parse JSON output from validator.")
    print(raw[:1200])
    raise SystemExit(1)

checks = payload.get("checks") or []
failed = [c for c in checks if isinstance(c, dict) and c.get("ok") is not True]
print(f"[release-gate] failed_checks={len(failed)}")
for item in failed:
    name = item.get("name", "unknown")
    detail = item.get("detail", "")
    print(f"- {name}: {detail}")

raise SystemExit(1)
PY