"""
Pending Entry Watcher — aguantar afuera antes de entrar.

Cuando el pipeline aprueba una entrada, NO entra inmediatamente.
En cambio, inicia un período de confirmación (25% del ciclo, mín 60s, máx 300s).
Durante ese tiempo el bot monitorea si la señal se mantiene.

Comportamiento:
  - Primera señal: inicia "pending", espera wait_s segundos
  - Re-confirmaciones: el pipeline sigue evaluando; si score cae fuerte → cancela
  - Expiración del wait: si signal sigue viable → ENTRA
  - Si señal se cae (score < initial × DROP_FRAC): cancela, busca nueva señal

Env vars:
  DERIV_PENDING_ENTRY_WAIT_FRAC   — fracción del ciclo median a esperar (default 0.25)
  DERIV_PENDING_ENTRY_WAIT_MIN_S  — mínimo wait en segundos (default 60)
  DERIV_PENDING_ENTRY_WAIT_MAX_S  — máximo wait en segundos (default 300)
  DERIV_PENDING_ENTRY_DROP_FRAC   — cancela si score < initial × frac (default 0.65)
  DERIV_PENDING_ENTRY_ENABLED     — false para desactivar (default true)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

# Returned actions from on_signal()
ACTION_FIRST_WAIT = "first_wait"   # primera señal — iniciando espera
ACTION_WAIT       = "wait"         # dentro del período — seguir esperando
ACTION_ENTER      = "enter"        # período completo + señal confirmada → ENTRAR
ACTION_CANCEL     = "cancel"       # señal cayó fuerte → cancelar, buscar nueva


class PendingEntryWatcher:
    """Retiene señales aprobadas por wait_s segundos antes de ejecutar."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def on_signal(
        self,
        symbol: str,
        side: str,
        score: float,
        median_cycle_s: float = 600.0,
    ) -> Tuple[str, float]:
        """
        Llamar cuando el pipeline aprueba una entrada (todos los gates pasados).

        Returns:
            (action, value):
              - (ACTION_FIRST_WAIT, wait_s)  — primera aparición, iniciando espera
              - (ACTION_WAIT, remaining_s)   — dentro del período, seguir esperando
              - (ACTION_ENTER, 0.0)          — período OK + señal confirmada → ENTRAR
              - (ACTION_CANCEL, 0.0)         — señal cayó → cancelar
        """
        if not self._enabled():
            return (ACTION_ENTER, 0.0)

        now = time.time()
        wait_s = self._compute_wait_s(median_cycle_s)
        drop_frac = float(os.getenv("DERIV_PENDING_ENTRY_DROP_FRAC", "0.65"))

        with self._lock:
            pending = self._pending.get(symbol)

            if pending is None:
                # Primera señal — iniciar pending
                cancel_score = max(4.5, score * drop_frac)
                self._pending[symbol] = {
                    "created_at": now,
                    "expires_at": now + wait_s,
                    "initial_score": score,
                    "cancel_score": cancel_score,
                    "side": side,
                    "confirmations": 1,
                    "last_score": score,
                    "wait_s": wait_s,
                    "score_sum": score,
                    "score_count": 1,
                }
                _LOGGER.info(
                    "[PENDING_ENTRY] %s STARTED | score=%.2f → wait=%.0fs | "
                    "cancel_if_avg<%.2f | side=%s",
                    symbol, score, wait_s, cancel_score, side,
                )
                return (ACTION_FIRST_WAIT, wait_s)

            # Cambio de dirección — resetear
            if pending["side"] != side:
                cancel_score = max(4.5, score * drop_frac)
                _LOGGER.info(
                    "[PENDING_ENTRY] %s RESET (side %s→%s) | new score=%.2f wait=%.0fs",
                    symbol, pending["side"], side, score, wait_s,
                )
                self._pending[symbol] = {
                    "created_at": now,
                    "expires_at": now + wait_s,
                    "initial_score": score,
                    "cancel_score": cancel_score,
                    "side": side,
                    "confirmations": 1,
                    "last_score": score,
                    "wait_s": wait_s,
                    "score_sum": score,
                    "score_count": 1,
                }
                return (ACTION_FIRST_WAIT, wait_s)

            # Mismo lado — acumular score para promedio
            pending["last_score"] = score
            pending["confirmations"] += 1
            pending["score_sum"] = pending.get("score_sum", score) + score
            pending["score_count"] = pending.get("score_count", 1) + 1
            avg_score = pending["score_sum"] / pending["score_count"]

            # Cancelar solo si el PROMEDIO cae por debajo del umbral
            # (un tick bajo no cancela — el ruido tick-a-tick es normal)
            # D.6 GHOST ABSOLUTE: nunca cancelar por score drop — solo spike cancela
            _d6_abs = str(os.getenv("DERIV_D6_GHOST_ABSOLUTE_ENABLED", "false")).strip().lower() in {
                "1", "true", "yes", "on"
            }
            if avg_score < pending["cancel_score"] and not _d6_abs:
                _LOGGER.info(
                    "[PENDING_ENTRY] %s CANCELLED | avg=%.2f < cancel_thr=%.2f "
                    "(initial=%.2f last=%.2f) ticks=%d",
                    symbol, avg_score, pending["cancel_score"],
                    pending["initial_score"], score, pending["confirmations"],
                )
                del self._pending[symbol]
                return (ACTION_CANCEL, 0.0)

            remaining = pending["expires_at"] - now
            if remaining <= 0:
                _LOGGER.info(
                    "[PENDING_ENTRY] %s CONFIRMED → ENTERING | avg=%.2f "
                    "(initial=%.2f last=%.2f) | ticks=%d | waited=%.0fs",
                    symbol, avg_score, pending["initial_score"],
                    score, pending["confirmations"], pending["wait_s"],
                )
                del self._pending[symbol]
                return (ACTION_ENTER, 0.0)

            # Dentro del período, señal sigue bien
            return (ACTION_WAIT, remaining)

    def cancel(self, symbol: str) -> None:
        """Cancelar pending entry explícitamente (ej: spike detectado, posición abierta)."""
        with self._lock:
            if symbol in self._pending:
                self._pending.pop(symbol)
                _LOGGER.info("[PENDING_ENTRY] %s cancelled externally", symbol)

    def is_pending(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._pending

    def get_state(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                sym: {
                    "remaining_s": max(0, int(p["expires_at"] - now)),
                    "initial_score": p["initial_score"],
                    "last_score": p.get("last_score", p["initial_score"]),
                    "avg_score": p["score_sum"] / p["score_count"] if p.get("score_count") else p["initial_score"],
                    "cancel_score": p["cancel_score"],
                    "confirmations": p["confirmations"],
                    "side": p["side"],
                    "wait_s": p["wait_s"],
                    "created_at": p.get("created_at", 0.0),
                }
                for sym, p in self._pending.items()
            }

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _enabled() -> bool:
        return str(os.getenv("DERIV_PENDING_ENTRY_ENABLED", "true")).strip().lower() in (
            "1", "true", "yes", "on"
        )

    @staticmethod
    def _compute_wait_s(median_cycle_s: float) -> float:
        frac    = float(os.getenv("DERIV_PENDING_ENTRY_WAIT_FRAC",  "0.25"))
        min_s   = float(os.getenv("DERIV_PENDING_ENTRY_WAIT_MIN_S", "60"))
        max_s   = float(os.getenv("DERIV_PENDING_ENTRY_WAIT_MAX_S", "300"))
        return max(min_s, min(max_s, median_cycle_s * frac))


# Singleton global
PENDING_ENTRY_WATCHER = PendingEntryWatcher()
