"""K.5 — PendingOrderManager: tick-by-tick guardian for PREPARE_AND_WAIT orders.

Flow:
  LLM returns PREPARE_AND_WAIT → create PendingOrder for symbol.
  Every subsequent tick for that symbol → evaluate() → WAIT | ABORT | ENTER.
  ABORT: spike arrived after order created, OR score dropped below min_score_hold.
  ENTER: wait_seconds elapsed with all conditions maintained.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    symbol: str
    created_at_ts: float        # wall-clock when order was created
    spike_ts_at_create: float   # last spike ts at creation — detect NEW spikes only
    wait_seconds: int           # LLM directive: wait this long before entering
    min_score_hold: float       # abort if score drops below this threshold (0=disabled)
    score_at_create: float      # informational — score when PREPARE_AND_WAIT was issued
    size_multiplier: float      # carry LLM sizing directive through to execution
    confidence: float           # LLM confidence at decision time
    llm_reason: str             # LLM reason, truncated
    side: str                   # CALL | PUT


class PendingOrderManager:
    """Per-symbol singleton that holds at most one pending order per symbol."""

    def __init__(self) -> None:
        self._orders: dict[str, PendingOrder] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def create(self, order: PendingOrder) -> None:
        with self._lock:
            self._orders[order.symbol] = order
        _LOGGER.info(
            "[K5] PENDING created %s | side=%s wait=%ds min_score=%.2f size=%.1fx",
            order.symbol, order.side, order.wait_seconds,
            order.min_score_hold, order.size_multiplier,
        )

    def evaluate(
        self,
        symbol: str,
        current_score: float,
        current_spike_ts: float,
    ) -> dict:
        """Evaluate the pending order for symbol against current conditions.

        Returns:
            dict with keys:
              action  : 'NONE' | 'WAIT' | 'ABORT' | 'ENTER'
              reason  : short string describing the decision
              order   : PendingOrder if action != 'NONE', else None
        """
        with self._lock:
            order = self._orders.get(symbol)
            if order is None:
                return {"action": "NONE", "reason": "no_pending", "order": None}

            elapsed = time.time() - order.created_at_ts

            # Abort: a new spike arrived AFTER the order was created
            if current_spike_ts > order.spike_ts_at_create:
                del self._orders[symbol]
                reason = f"spike_arrived({current_spike_ts - order.spike_ts_at_create:.1f}s_after_create)"
                _LOGGER.info("[K5] ABORT %s — %s", symbol, reason)
                return {"action": "ABORT", "reason": reason, "order": order}

            # Abort: score degraded below the LLM's minimum hold threshold
            if order.min_score_hold > 0 and current_score < order.min_score_hold:
                del self._orders[symbol]
                reason = f"score_degraded({current_score:.2f}<{order.min_score_hold:.2f})"
                _LOGGER.info("[K5] ABORT %s — %s", symbol, reason)
                return {"action": "ABORT", "reason": reason, "order": order}

            # Enter: wait window elapsed with all conditions maintained
            if elapsed >= order.wait_seconds:
                del self._orders[symbol]
                reason = f"waited_{elapsed:.0f}s/{order.wait_seconds}s score={current_score:.2f}"
                _LOGGER.info("[K5] ENTER %s — %s", symbol, reason)
                return {"action": "ENTER", "reason": reason, "order": order}

            # Keep waiting
            reason = f"waiting_{elapsed:.0f}s/{order.wait_seconds}s"
            _LOGGER.debug("[K5] WAIT %s — %s", symbol, reason)
            return {"action": "WAIT", "reason": reason, "order": order}

    def cancel(self, symbol: str) -> None:
        with self._lock:
            removed = self._orders.pop(symbol, None)
        if removed:
            _LOGGER.info("[K5] CANCEL %s", symbol)

    def has_pending(self, symbol: str) -> bool:
        return symbol in self._orders

    def pending_symbols(self) -> list[str]:
        with self._lock:
            return list(self._orders.keys())


# Module-level singleton — imported directly by main_deriv.py
PENDING_MANAGER = PendingOrderManager()
