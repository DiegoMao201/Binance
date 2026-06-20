#!/bin/bash
# D.6.3 — Reset muestra limpia (Opción B — Diego 2026-06-20)
#
# Archiva trades cerrados + resetea panel state.
# NO toca: ghost_trades.json, deriv_spike_events.json, Pattern Memory.

set -e

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
LOG_DIR="${DERIV_STATE_DIR:-${BOT_STATE_DIR:-/data/logs}}"
ARCHIVE_DIR="${LOG_DIR}/archive_muestra_limpia_${TIMESTAMP}"

echo "==========================================================="
echo "RESET MUESTRA LIMPIA D.6.3 (Opción B — Diego 2026-06-20)"
echo "==========================================================="
echo "Timestamp: $(date -u)"
echo "Log dir:   ${LOG_DIR}"
echo "Archive:   ${ARCHIVE_DIR}"
echo

mkdir -p "${ARCHIVE_DIR}"

# ============================================================
# 1. Archivar trades cerrados → panel a 0
# ============================================================
CLOSED="${LOG_DIR}/deriv_closed_contracts.json"
if [ -f "${CLOSED}" ]; then
    cp "${CLOSED}" "${ARCHIVE_DIR}/"
    echo '[]' > "${CLOSED}"
    echo "OK deriv_closed_contracts.json archivado"
else
    echo "SKIP deriv_closed_contracts.json no encontrado"
fi

# ============================================================
# 2. Reset ghost state → todos WAITING (via Python)
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
print(f"OK d6_ghost_state.json reseteado ({len(symbols)} simbolos WAITING)")
PYEOF

# ============================================================
# 3. NO TOCAR (Opción B)
# ============================================================
echo
echo "INTACTOS (Opcion B):"
echo "  ghost_trades.json          (ghost sigue aprendiendo)"
echo "  deriv_spike_events.json    (contadores 1H/6H/24H reales)"
echo "  Pattern Memory PostgreSQL"

# ============================================================
# 4. Registrar inicio de muestra
# ============================================================
SAMPLE_START_TS=$(date -u +%s)
SAMPLE_START_UTC=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
REV_12H_UTC=$(date -u -d "+12 hours" +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || \
              date -u -v+12H +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || echo "N/A")
REV_24H_UTC=$(date -u -d "+24 hours" +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || \
              date -u -v+24H +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null || echo "N/A")

AUDIT_DIR="${LOG_DIR}/audit-reports"
mkdir -p "${AUDIT_DIR}"

cat > "${AUDIT_DIR}/MUESTRA_LIMPIA_ACTIVA.txt" << EOF
=========================================================================
MUESTRA LIMPIA D.6.3 — Pending diferenciado por simbolo + calidad
=========================================================================

INICIO:
  ${SAMPLE_START_UTC}
  SAMPLE_START_TS=${SAMPLE_START_TS}

REVISION 12H: ${REV_12H_UTC}
REVISION 24H: ${REV_24H_UTC}

ARCHIVE: ${ARCHIVE_DIR}

CONFIG D.6.3:
  Pending FORTISIMA (todos los simbolos): ${DERIV_D6_PENDING_STRONG_SEC:-60}s
  Criterios fortisima: score>=${DERIV_D6_STRONG_SCORE_MIN:-8.5} + grade=A + setup=SMC_FVG + bos=True + RNG>=${DERIV_D6_STRONG_RNG_MIN:-80}

  Pending NORMAL por simbolo:
    BOOM500/CRASH500:   ${DERIV_D6_PENDING_NORMAL_BOOM500:-120}s / ${DERIV_D6_PENDING_NORMAL_CRASH500:-120}s
    BOOM600/CRASH600:   ${DERIV_D6_PENDING_NORMAL_BOOM600:-150}s / ${DERIV_D6_PENDING_NORMAL_CRASH600:-150}s
    BOOM900/CRASH900:   ${DERIV_D6_PENDING_NORMAL_BOOM900:-200}s / ${DERIV_D6_PENDING_NORMAL_CRASH900:-200}s
    BOOM1000/CRASH1000: ${DERIV_D6_PENDING_NORMAL_BOOM1000:-200}s / ${DERIV_D6_PENDING_NORMAL_CRASH1000:-200}s

  Filosofia spike-cancel: cualquier spike durante pending CANCEL + ciclo reinicia

QUE MEDIR EN 12H:
  - Ghost ALLOW total:    grep -c 'D6_GHOST_ALLOW' logs
  - Pending creados:      grep -c 'D6_PENDING_CREATED' logs
  - Quality fortisima:    grep 'D6_PENDING_CREATED' logs | grep -c 'quality=fortisima'
  - Quality normal:       grep 'D6_PENDING_CREATED' logs | grep -c 'quality=normal'
  - Ejecutados:           grep -c 'D6_PURE_EXECUTED' logs
  - Cancelados por spike: grep -c 'SPIKE_DURING_WAIT.*CANCEL' logs
  - WR real del bot

INTACTOS:
  - deriv_spike_events.json
  - ghost_trades.json
  - Pattern Memory PostgreSQL

COMMIT: $(cd /app 2>/dev/null && git rev-parse HEAD 2>/dev/null || echo "ver desde host")
=========================================================================
EOF

echo
echo "==========================================================="
echo "MUESTRA INICIADA"
echo "  Inicio:       ${SAMPLE_START_UTC}"
echo "  Revision 12H: ${REV_12H_UTC}"
echo "  Registro:     ${AUDIT_DIR}/MUESTRA_LIMPIA_ACTIVA.txt"
echo "==========================================================="
