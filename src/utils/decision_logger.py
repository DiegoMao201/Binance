"""
Enriched decision logger — captura completa de cada decisión.
PASO 2.3h-prep Fase F: sin esta data, validar hipótesis es imposible.

Escribe a /data/logs/decisions_enriched.jsonl (append-only).
Outcomes se actualizan via update_decision_outcome() cuando el trade cierra.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

LOG_PATH = os.getenv("DERIV_DECISION_LOG_PATH", "/data/logs/decisions_enriched.jsonl")


def log_decision(
    symbol: str,
    decision: str,
    score_breakdown: Dict[str, Any],
    maturity_info: Optional[Dict] = None,
    pattern_memory: Optional[Dict] = None,
    geometric_structure: Optional[Dict] = None,
    llm_info: Optional[Dict] = None,
    gates_passed: Optional[List[str]] = None,
    gates_failed: Optional[List[str]] = None,
    entry_action: str = "skipped",
    contract_id: Optional[str] = None,
) -> Optional[str]:
    """
    Loguea decisión enriquecida. Retorna decision_id para vincular con outcome.
    Nunca lanza excepción — el bot no debe fallar por telemetría.
    """
    decision_id = f"{symbol}_{int(time.time() * 1000)}"
    entry = {
        "decision_id": decision_id,
        "ts": time.time(),
        "symbol": symbol,
        "decision": decision,
        "entry_action": entry_action,
        "contract_id": contract_id,
        "score_breakdown": score_breakdown,
        "maturity": maturity_info,
        "pattern_memory": pattern_memory,
        "geometric_structure": geometric_structure,
        "llm": llm_info,
        "gates_passed": gates_passed or [],
        "gates_failed": gates_failed or [],
        "outcome": None,
    }
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return decision_id
    except Exception:
        return None


def update_decision_outcome(
    decision_id: str,
    realized_pnl: float,
    peak_profit: float,
    duration_s: float,
    exit_reason: str,
) -> None:
    """
    Vincula outcome al decision_id cuando el trade cierra.
    Append-only: el análisis posterior une por decision_id.
    """
    try:
        update = {
            "type": "outcome_update",
            "decision_id": decision_id,
            "ts": time.time(),
            "outcome": {
                "realized_pnl": realized_pnl,
                "peak_profit": peak_profit,
                "duration_s": duration_s,
                "exit_reason": exit_reason,
            },
        }
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(update, default=str) + "\n")
    except Exception:
        pass
