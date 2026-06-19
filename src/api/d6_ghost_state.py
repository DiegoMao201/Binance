"""
D.6 Ghost Absolute — escritor de estado para el panel live.

El bot llama update_d6_state() cuando:
  - Ghost dispara ALLOW (estado PENDING con countdown 60s)
  - Pending ejecutado (EXECUTED)
  - Pending cancelado por spike (CANCELLED)
  - API rechazó (FAILED)

El panel Next.js lee /api/deriv/analytics/d6-ghost-state cada 1s.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}

_LOG_DIR = os.getenv("DERIV_STATE_DIR") or os.getenv("BOT_STATE_DIR") or "/data/logs"
_STATE_FILE = os.path.join(_LOG_DIR, "d6_ghost_state.json")


def update_d6_state(
    symbol: str,
    state: str,
    ghost_data: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """
    Actualizar estado D.6 para un símbolo.

    state: WAITING | PENDING | EXECUTED | CANCELLED | FAILED
    """
    sym = symbol.upper()
    entry: Dict[str, Any] = {
        "symbol": sym,
        "state": state,
        "updated_at": time.time(),
        "ghost_data": ghost_data or {},
    }
    if reason is not None:
        entry["reason"] = reason

    with _lock:
        _state[sym] = entry
        try:
            Path(_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(_state, f)
        except Exception:
            pass


def get_d6_state(symbol: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        if symbol:
            return dict(_state.get(symbol.upper(), {}))
        return dict(_state)
