"""
D.6 Ghost Absolute — escritor de estado para el panel live.

El bot llama update_d6_state() cuando:
  - Ghost dispara ALLOW (estado PENDING con countdown 60s)
  - Pending ejecutado (EXECUTED)
  - Pending cancelado por spike (CANCELLED)
  - API rechazó (FAILED)

El panel Next.js lee /api/deriv/analytics/d6-ghost-state cada 1s.
TTL auto-clear: PENDING sin actualización >90s → EXPIRED_GHOST (bug visible).
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

# Si PENDING no se actualiza en estos segundos → EXPIRED_GHOST (indica bug)
_PENDING_TTL_S = 90


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
    now = time.time()

    with _lock:
        existing = _state.get(sym, {})
        # Merge ghost_data when updating a live PENDING → keeps quality_tier, wait_s, etc.
        # set at creation; tick-level updates (remaining_seconds only) don't wipe them.
        if state == "PENDING" and existing.get("state") == "PENDING" and ghost_data:
            merged = {**existing.get("ghost_data", {}), **ghost_data}
        else:
            merged = ghost_data or {}

        entry: Dict[str, Any] = {
            "symbol": sym,
            "state": state,
            "updated_at": now,
            "ghost_data": merged,
        }
        if reason is not None:
            entry["reason"] = reason

        _state[sym] = entry
        _flush()


def get_d6_state(symbol: Optional[str] = None) -> Dict[str, Any]:
    """
    Devuelve estado con TTL aplicado en memoria.
    PENDING sin update en >_PENDING_TTL_S → devuelve EXPIRED_GHOST.
    """
    now = time.time()
    with _lock:
        if symbol:
            sym = symbol.upper()
            entry = _state.get(sym)
            if entry is None:
                return {}
            return _apply_ttl(dict(entry), now)
        return {sym: _apply_ttl(dict(e), now) for sym, e in _state.items()}


# ── Internal ──────────────────────────────────────────────────────────────────

def _apply_ttl(entry: Dict[str, Any], now: float) -> Dict[str, Any]:
    state = entry.get("state", "WAITING")
    updated_at = float(entry.get("updated_at") or 0)
    age = now - updated_at
    if state == "PENDING" and age > _PENDING_TTL_S:
        entry["state"] = "EXPIRED_GHOST"
        entry["reason"] = f"pending_stale_{int(age)}s"
    return entry


def _flush() -> None:
    try:
        Path(_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception:
        pass


def d6_startup_cleanup() -> None:
    """
    D.6.3 — Limpieza al arrancar el bot post-deploy.

    Resetea el panel state a WAITING para todos los símbolos y limpia el
    PendingEntryWatcher. Evita que estados huérfanos del proceso anterior
    contaminen el panel o generen entradas fantasmas.
    """
    import logging
    _log = logging.getLogger("deriv.daemon")
    _log.info("[D6_STARTUP] Iniciando cleanup post-deploy D.6.3...")

    _SYMBOLS = [
        "BOOM500", "BOOM600", "BOOM900", "BOOM1000",
        "CRASH500", "CRASH600", "CRASH900", "CRASH1000",
    ]

    # 1. Resetear panel state → todos WAITING
    try:
        with _lock:
            old_non_waiting = sum(1 for v in _state.values() if v.get("state") != "WAITING")
            _state.clear()
            for sym in _SYMBOLS:
                _state[sym] = {
                    "symbol": sym,
                    "state": "WAITING",
                    "updated_at": time.time(),
                    "ghost_data": {},
                }
            _flush()
        _log.info(
            "[D6_STARTUP] Panel state limpiado | %d estados huérfanos → WAITING",
            old_non_waiting,
        )
    except Exception as exc:
        _log.warning("[D6_STARTUP] Panel cleanup error: %s", exc)

    # 2. Vaciar PostRachaCooldown
    try:
        from src.strategies.post_racha_cooldown import POST_RACHA_COOLDOWN
        POST_RACHA_COOLDOWN.force_clear()
        _log.info("[D6_STARTUP] PostRachaCooldown: todos los cooldowns limpiados")
    except Exception as exc:
        _log.warning("[D6_STARTUP] PostRachaCooldown clear error: %s", exc)

    # 2b. Vaciar RegimeSkipFilter (D.6.7)
    try:
        from src.strategies.regime_skip_filter import REGIME_SKIP_FILTER
        REGIME_SKIP_FILTER.force_clear()
        _log.info("[D6_STARTUP] RegimeSkipFilter: estado limpiado")
    except Exception as exc:
        _log.warning("[D6_STARTUP] RegimeSkipFilter clear error: %s", exc)

    # 2c. Vaciar RegimeDetectorV2 (D.7.0)
    try:
        from src.strategies.regime_detector_v2 import REGIME_DETECTOR_V2
        REGIME_DETECTOR_V2.force_clear()
        _log.info("[D6_STARTUP] RegimeDetectorV2: estado limpiado")
    except Exception as exc:
        _log.warning("[D6_STARTUP] RegimeDetectorV2 clear error: %s", exc)

    # 3. Vaciar PendingEntryWatcher
    try:
        from src.strategies.pending_entry_watcher import PENDING_ENTRY_WATCHER
        with PENDING_ENTRY_WATCHER._lock:
            n = len(PENDING_ENTRY_WATCHER._pending)
            PENDING_ENTRY_WATCHER._pending.clear()
        _log.info("[D6_STARTUP] PendingEntryWatcher: %d pendings huérfanos eliminados", n)
    except Exception as exc:
        _log.warning("[D6_STARTUP] Watcher clear error: %s", exc)

    # 4. Log de configuración activa para auditoría
    _log.info(
        "[D6_STARTUP] Config D.6.3 — FORTISIMA: score>=%s grade=A setup=SMC_FVG BOS=True RNG>=%s wait=%ss",
        os.getenv("DERIV_D6_STRONG_SCORE_MIN", "8.5"),
        os.getenv("DERIV_D6_STRONG_RNG_MIN", "80"),
        os.getenv("DERIV_D6_PENDING_STRONG_SEC", "60"),
    )
    for sym in _SYMBOLS:
        wait = os.getenv(f"DERIV_D6_PENDING_NORMAL_{sym}", os.getenv("DERIV_D6_PENDING_NORMAL_DEFAULT", "120"))
        _log.info("[D6_STARTUP] %s normal pending: %ss", sym, wait)

    _log.info("[D6_STARTUP] Cleanup completo. Bot listo para operar D.6.3.")
