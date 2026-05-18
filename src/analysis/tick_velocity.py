"""
src/analysis/tick_velocity.py
─────────────────────────────────────────────────────────────────────────────
TickVelocityAnalyzer — exponential tick-delta acceleration detector.

Purpose
───────
Synthetic spike indices (BOOM / CRASH) often exhibit a signature pattern
immediately before a spike fires: tick-level price deltas start accelerating
exponentially (e.g. 0.1 → 0.1 → 0.2 → 0.3 → 0.5 → 1.2).

This module detects that pattern in the live tick stream and emits an
acceleration signal that the pipeline can use to:
  1. Boost entry conviction when combined with MTF trend alignment (hd_bonus).
  2. Bypass minor regime filters when full velocity+HD confluence is detected.

Algorithm
─────────
For each symbol, maintain a rolling buffer of the last WINDOW tick prices.
On every ingested tick:
  • Compute the absolute deltas between consecutive ticks.
  • Examine the last ACCEL_WINDOW deltas.
  • Count how many consecutive steps satisfy: delta[i] ≥ delta[i-1] * ACCEL_RATIO.
  • If ≥ ACCEL_MIN_STEPS consecutive acceleration steps → signal active.

Direction is inferred from the SIGNED sum of the acceleration window: if the
net price movement over the accelerating window is positive → "MULTUP" hint,
negative → "MULTDOWN" hint.  For BOOM/CRASH the direction hint is advisory
only — the forced_side() rule from deriv_signals always takes precedence.

Public API
──────────
  analyzer = TickVelocityAnalyzer()
  analyzer.ingest_tick(symbol, price)
  is_acc, score, direction = analyzer.check_acceleration(symbol)
"""

from __future__ import annotations

from collections import deque
from typing import Deque

# ── Tunables (env-var overrideable in main if needed) ──────────────────────
WINDOW: int = 20          # tick buffer depth per symbol
ACCEL_WINDOW: int = 8     # how many deltas to examine for the pattern
ACCEL_MIN_STEPS: int = 3  # consecutive accelerating steps needed to fire
ACCEL_RATIO: float = 1.35 # each delta must be >= prev * ACCEL_RATIO
ACCEL_MIN_DELTA: float = 0.0001  # ignore deltas smaller than this (noise floor)


class TickVelocityAnalyzer:
    """Per-symbol exponential tick-acceleration detector.

    Thread-safe for single-threaded asyncio use (no locks).
    """

    def __init__(self) -> None:
        self._bufs: dict[str, Deque[float]] = {}

    def ingest_tick(self, symbol: str, price: float) -> None:
        """Append ``price`` to the rolling buffer for ``symbol``."""
        if price <= 0:
            return
        buf = self._bufs.setdefault(symbol, deque(maxlen=WINDOW))
        buf.append(float(price))

    def check_acceleration(
        self, symbol: str
    ) -> tuple[bool, float, str | None]:
        """Analyse the tick buffer for an exponential acceleration pattern.

        Returns
        -------
        (is_accelerating, acceleration_score, direction_hint)
            is_accelerating  : True when the exponential pattern is detected.
            acceleration_score : normalised [0, 1] — fraction of acceleration
                                 steps confirmed out of (ACCEL_WINDOW − 1).
            direction_hint   : "MULTUP" / "MULTDOWN" / None — advisory; caller
                               must apply forced_side() rules for spike markets.
        """
        buf = self._bufs.get(symbol)
        if buf is None or len(buf) < ACCEL_WINDOW + 1:
            return False, 0.0, None

        prices = list(buf)
        # Signed deltas (most recent at end)
        signed: list[float] = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        abs_deltas: list[float] = [abs(d) for d in signed]

        # Take the last ACCEL_WINDOW deltas
        last_abs = abs_deltas[-ACCEL_WINDOW:]
        last_sgn = signed[-ACCEL_WINDOW:]

        # Count consecutive accelerating steps from the END of the window
        accel_steps = 0
        for i in range(len(last_abs) - 1, 0, -1):
            prev_d = last_abs[i - 1]
            curr_d = last_abs[i]
            if prev_d < ACCEL_MIN_DELTA:
                break  # prev delta is noise — stop counting
            if curr_d >= prev_d * ACCEL_RATIO:
                accel_steps += 1
            else:
                break  # acceleration chain broken

        if accel_steps < ACCEL_MIN_STEPS:
            return False, 0.0, None

        # Normalise score: fraction of the maximum possible accel steps
        max_steps = ACCEL_WINDOW - 1
        accel_score = round(min(1.0, accel_steps / max_steps), 3)

        # Direction: sum of SIGNED deltas over the acceleration window
        net_move = sum(last_sgn)
        direction: str | None
        if net_move > 0:
            direction = "MULTUP"
        elif net_move < 0:
            direction = "MULTDOWN"
        else:
            direction = None

        return True, accel_score, direction

    def get_buffer_size(self, symbol: str) -> int:
        """Return the number of ticks currently buffered for ``symbol``."""
        buf = self._bufs.get(symbol)
        return len(buf) if buf is not None else 0
