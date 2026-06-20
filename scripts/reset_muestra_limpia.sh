#!/bin/bash
# D.6.2 — Reset para muestra limpia comparativa ghost vs bot
#
# OPCIÓN B (Diego 2026-06-20):
# - Panel queda visualmente limpio (trades del bot archivados)
# - Ghost state → todos WAITING
# - Spikes NO se tocan (contadores 1H/6H/24H siguen reales)
# - Ghost trades NO se tocan (ghost sigue aprendiendo)
# - Pattern Memory NO se toca

set -e

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
LOG_DIR="${DERIV_STATE_DIR:-${BOT_STATE_DIR:-/data/logs}}"
ARCHIVE_DIR="${LOG_DIR}/archive_muestra_limpia_${TIMESTAMP}"

echo "==========================================================="
echo "RESET MUESTRA LIMPIA (Opción B — Diego 2026-06-20)"
echo "==========================================================="
echo "Timestamp: $(date -u)"
echo "Log dir:   ${LOG_DIR}"
echo "Archive:   ${ARCHIVE_DIR}"
echo

mkdir -p "${ARCHIVE_DIR}"

# ============================================================
# 1. ARCHIVAR trades cerrados → panel a 0
# ============================================================
CLOSED="${LOG_DIR}/deriv_closed_contracts.json"
if [ -f "${CLOSED}" ]; then
    cp "${CLOSED}" "${ARCHIVE_DIR}/"
    echo '[]' > "${CLOSED}"
    echo "OK deriv_closed_contracts.json archivado (panel a 0)"
else
    echo "SKIP deriv_closed_contracts.json no encontrado en ${CLOSED}"
fi

# ============================================================
# 2. RESETEAR ghost state → todos WAITING
# ============================================================
GHOST_STATE="${LOG_DIR}/d6_ghost_state.json"
if [ -f "${GHOST_STATE}" ]; then
    cp "${GHOST_STATE}" "${ARCHIVE_DIR}/"
fi

python3 << 'PYEOF'
import json, time, os

log_dir = os.getenv("DERIV_STATE_DIR") or os.getenv("BOT_STATE_DIR") or "/data/logs"
state_file = os.path.join(log_dir, "d6_ghost_state.json")

symbols = [
    "BOOM500", "BOOM600", "BOOM900", "BOOM1000",
    "CRASH500", "CRASH600", "CRASH900", "CRASH1000",
]
clean_state = {
    sym: {"symbol": sym, "state": "WAITING", "updated_at": time.time(), "ghost_data": {}}
    for sym in symbols
}
os.makedirs(os.path.dirname(state_file), exist_ok=True)
with open(state_file, "w") as f:
    json.dump(clean_state, f)
print(f"OK d6_ghost_state.json reseteado a WAITING ({len(symbols)} símbolos)")
PYEOF

# ============================================================
# 3. NO TOCAR (Opción B)
# ============================================================
echo
echo "INTACTOS (Opción B):"
echo "  ghost_trades.json         (ghost sigue aprendiendo)"
echo "  deriv_spike_events.json   (contadores 1H/6H/24H reales)"
echo "  Pattern Memory PostgreSQL"

# ============================================================
# 4. REGISTRAR HORA DE INICIO DE MUESTRA
# ============================================================
SAMPLE_START_TS=$(date -u +%s)
SAMPLE_START_UTC=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
REV_12H_UTC=$(date -u -d "+12 hours" +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || date -u -v+12H +"%Y-%m-%d %H:%M:%S UTC")
REV_24H_UTC=$(date -u -d "+24 hours" +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || date -u -v+24H +"%Y-%m-%d %H:%M:%S UTC")

AUDIT_DIR="${LOG_DIR}/audit-reports"
mkdir -p "${AUDIT_DIR}"

cat > "${AUDIT_DIR}/MUESTRA_LIMPIA_ACTIVA.txt" << EOF
=========================================================================
MUESTRA LIMPIA ACTIVA — D.6.2 Ghost puro con spike cancel
=========================================================================

INICIO:
  ${SAMPLE_START_UTC}
  timestamp Unix: ${SAMPLE_START_TS}

REVISIÓN 12H: ${REV_12H_UTC}
REVISIÓN 24H: ${REV_24H_UTC}

ARCHIVE: ${ARCHIVE_DIR}

COMMIT: $(cd /app 2>/dev/null && git rev-parse HEAD 2>/dev/null || echo "ver desde host")

QUÉ MEDIR EN 12H:
  - Ghost ALLOW total    → grep -c 'D6_GHOST_ALLOW' logs
  - Bot EXECUTED total   → grep -c 'D6_PURE_EXECUTED\|D6_GHOST_ALLOW.*executed' logs
  - Cancelados por spike → grep -c 'SPIKE_DURING_WAIT.*CANCEL' logs
  - WR real del bot

INTACTOS (Opción B):
  - deriv_spike_events.json (contadores 1H/6H/24H reales)
  - ghost_trades.json       (ghost sigue aprendiendo)
  - Pattern Memory PostgreSQL

=========================================================================
EOF

echo
echo "==========================================================="
echo "MUESTRA INICIADA"
echo "  Inicio:       ${SAMPLE_START_UTC}"
echo "  Revisión 12H: ${REV_12H_UTC}"
echo "  Registro:     ${AUDIT_DIR}/MUESTRA_LIMPIA_ACTIVA.txt"
echo "==========================================================="
