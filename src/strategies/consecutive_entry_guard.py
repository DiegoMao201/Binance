"""
Consecutive Entry Guard — PASO 2.5 L.1

Limita corridas de entradas múltiples al mismo símbolo cuando el spike no llega.

Hallazgo auditoría 12h: 54 corridas SECO consecutivas, 121 trades, 37% all-loss,
-33.59 USDT. 5 casos confirmados: spike llegó 17-130s después del cierre de corrida.

Filosofía (memoria #11): NO penaliza scarcity_state. Gestión de riesgo pura.
Compatible con PRNG: si el spike es independiente, entrar 4 veces seguidas
no aumenta probabilidad — es capital quemado esperando lo mismo.

Comportamiento:
  - 2+ trades perdedores en mismo símbolo en ventana 600s → cooldown 300s
  - Cooldown se cancela automáticamente si se detecta spike real (capturar el spike)
  - NO bloquea si el símbolo ganó recientemente
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Dict

_LOGGER = logging.getLogger(__name__)

# Configurable via env (leído en tiempo de evaluación)
_DEFAULT_LOSS_WINDOW_S = 600    # ventana para contar pérdidas consecutivas
_DEFAULT_MIN_LOSSES = 2         # mínimo de pérdidas para activar cooldown
_DEFAULT_COOLDOWN_S = 300       # duración del cooldown (5 minutos)


class ConsecutiveEntryGuard:
    """Rastrea cierres recientes por símbolo y bloquea corridas de pérdidas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # symbol -> deque of {'ts': float, 'pnl': float, 'exit_reason': str}
        self._recent: Dict[str, deque] = {}
        # symbol -> cooldown_until_ts
        self._cooldown_until: Dict[str, float] = {}
        # symbol -> reason str
        self._cooldown_reason: Dict[str, str] = {}
        # symbol -> spike_ts at cooldown activation (to detect NEW spikes only)
        self._cooldown_spike_ts: Dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def record_close(
        self,
        symbol: str,
        pnl: float,
        exit_reason: str,
        last_spike_ts: float = 0.0,
    ) -> None:
        """Registrar cierre de trade. Llamar desde el reaper loop."""
        with self._lock:
            if symbol not in self._recent:
                self._recent[symbol] = deque(maxlen=10)
            self._recent[symbol].append({
                "ts": time.time(),
                "pnl": pnl,
                "exit_reason": exit_reason,
            })
            self._maybe_activate_cooldown(symbol, last_spike_ts=last_spike_ts)

    def is_blocked(self, symbol: str) -> dict:
        """
        Verificar si el símbolo está en cooldown por corrida de pérdidas.

        Returns dict:
          blocked        : bool
          reason         : str
          remaining_sec  : int
        """
        with self._lock:
            return self._check_blocked(symbol)

    def on_spike_detected(self, symbol: str, spike_ts: float = 0.0) -> None:
        """
        Spike detectado → cancelar cooldown si el spike es NUEVO (posterior al cooldown).
        Este es el caso exacto de L.1: el spike que buscábamos llegó después de la corrida.
        spike_ts=0 cancela incondicionalmente (compatibilidad).
        """
        with self._lock:
            if symbol not in self._cooldown_until:
                return
            stored_spike_ts = self._cooldown_spike_ts.get(symbol, 0.0)
            # Cancel only if this spike is newer than when cooldown was activated
            if spike_ts > 0 and stored_spike_ts > 0 and spike_ts <= stored_spike_ts:
                return  # same spike, not a new one
            remaining = int(self._cooldown_until[symbol] - time.time())
            _LOGGER.info(
                "[CONSECUTIVE_GUARD] %s NEW spike (ts=%.0f) → cooldown CANCELLED "
                "(was %ds remaining) — capturing real spike",
                symbol, spike_ts, max(0, remaining),
            )
            del self._cooldown_until[symbol]
            self._cooldown_reason.pop(symbol, None)
            self._cooldown_spike_ts.pop(symbol, None)

    def get_state(self) -> dict:
        """Para telemetría."""
        with self._lock:
            now = time.time()
            return {
                sym: {
                    "remaining_sec": int(until - now),
                    "reason": self._cooldown_reason.get(sym, ""),
                }
                for sym, until in self._cooldown_until.items()
                if until > now
            }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _maybe_activate_cooldown(self, symbol: str, last_spike_ts: float = 0.0) -> None:
        now = time.time()
        trades = self._recent.get(symbol)
        if not trades:
            return

        # Solo analizar trades dentro de la ventana
        window = [t for t in trades if (now - t["ts"]) < _DEFAULT_LOSS_WINDOW_S]
        if len(window) < _DEFAULT_MIN_LOSSES:
            return

        # Contar pérdidas en las últimas N entradas dentro de la ventana
        last_n = list(trades)[-4:]  # analizar máx últimas 4
        recent_window = [t for t in last_n if (now - t["ts"]) < _DEFAULT_LOSS_WINDOW_S]
        consecutive_losses = sum(1 for t in recent_window if t["pnl"] < 0)

        if consecutive_losses >= _DEFAULT_MIN_LOSSES:
            cooldown_end = now + _DEFAULT_COOLDOWN_S
            self._cooldown_until[symbol] = cooldown_end
            self._cooldown_reason[symbol] = (
                f"consecutive_losses:{consecutive_losses}/{len(recent_window)}_recent"
            )
            self._cooldown_spike_ts[symbol] = last_spike_ts
            _LOGGER.info(
                "[CONSECUTIVE_GUARD] %s COOLDOWN activated %.0fs "
                "| %d/%d recent trades lost | spike_ts_at_create=%.0f | reason=%s",
                symbol, _DEFAULT_COOLDOWN_S,
                consecutive_losses, len(recent_window),
                last_spike_ts,
                self._cooldown_reason[symbol],
            )

    def _check_blocked(self, symbol: str) -> dict:
        until = self._cooldown_until.get(symbol)
        if until is None:
            return {"blocked": False, "reason": "", "remaining_sec": 0}

        now = time.time()
        if now >= until:
            del self._cooldown_until[symbol]
            self._cooldown_reason.pop(symbol, None)
            return {"blocked": False, "reason": "cooldown_expired", "remaining_sec": 0}

        return {
            "blocked": True,
            "reason": self._cooldown_reason.get(symbol, "unknown"),
            "remaining_sec": int(until - now),
        }


# Singleton global — importado por main_deriv.py
CONSECUTIVE_GUARD = ConsecutiveEntryGuard()
