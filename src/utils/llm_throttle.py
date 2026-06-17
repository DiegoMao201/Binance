"""
LLM Throttle — limita llamadas LLM a 1 por símbolo por minuto.

PASO 2.6 O.1 — Diego insight (2026-06-17):
'máximo 8 llamadas por minuto, 1 por cada símbolo'

Con 8 símbolos activos × 1 llamada/min = 8/min máximo.
Cabe en DeepSeek free (20/min, 1000/día).

Filosofía: si gates pasan 5 veces en 60s para el mismo símbolo,
no llamamos al LLM 5 veces. Aguantamos.
"""
import time
from collections import defaultdict
from typing import Dict


class LLMCallThrottle:
    """Limita llamadas LLM a 1 por símbolo por intervalo configurable."""

    def __init__(self, interval_sec: int = 60):
        self.interval = interval_sec
        self._last_call: Dict[str, float] = {}
        self._call_counts: Dict[str, int] = defaultdict(int)
        self._skipped_counts: Dict[str, int] = defaultdict(int)

    def can_call(self, symbol: str) -> Dict:
        """
        Returns:
            {'allowed': bool, 'remaining_sec': int, 'reason': str}
        """
        now = time.time()
        last = self._last_call.get(symbol)

        if last is None:
            return {"allowed": True, "remaining_sec": 0, "reason": "first_call"}

        elapsed = now - last
        if elapsed >= self.interval:
            return {
                "allowed": True,
                "remaining_sec": 0,
                "reason": f"elapsed={elapsed:.0f}s>={self.interval}s",
            }

        return {
            "allowed": False,
            "remaining_sec": int(self.interval - elapsed),
            "reason": f"throttled_{int(elapsed)}s_since_last",
        }

    def record_call(self, symbol: str) -> None:
        """Llamar DESPUÉS de la llamada LLM exitosa."""
        self._last_call[symbol] = time.time()
        self._call_counts[symbol] += 1

    def record_skip(self, symbol: str) -> None:
        """Telemetría cuando se salta por throttle."""
        self._skipped_counts[symbol] += 1

    def get_stats(self) -> Dict:
        return {
            "calls": dict(self._call_counts),
            "skipped": dict(self._skipped_counts),
            "total_calls": sum(self._call_counts.values()),
            "total_skipped": sum(self._skipped_counts.values()),
        }


LLM_THROTTLE = LLMCallThrottle(interval_sec=60)
