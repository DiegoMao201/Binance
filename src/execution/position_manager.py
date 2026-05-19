"""
src/execution/position_manager.py
─────────────────────────────────────────────────────────────────────────────
DynamicPositionManager — ratchet SL + momentum-based TP for Deriv multiplier
contracts.

Replaces the static tiered trailing stop (_compute_trail_floor) with a
dynamic algorithm that adapts to each symbol's volatility profile:

  Phase 1 — INITIAL PROTECTION
    Initial SL = -sl_inicial_pct × stake  (e.g. −$0.525 on $1.50)
    If PnL drops to SL → close (reason: "sl_inicial")
    If PnL rises past ratchet_step_pct × stake → activate Phase 2

  Phase 2 — ACTIVE RATCHET
    SL floor = ratchet_ratio × peak_profit  (never goes down)
    If PnL drops to floor → close (reason: "ratchet_sl_alcanzado")
    If momentum falls below agotamiento_threshold × momentum_peak → close
    (reason: "momentum_agotado")

  Phase 3 — TIME SAFETY
    If elapsed > max_duration_seg → close (reason: "timeout_max")
    Applied before Phase 1/2 checks on every tick.

Per-symbol parameters in SYMBOL_RATCHET_PARAMS.  Unknown symbols fall back
to _DEFAULT_PARAMS (R_50-like).

Key metric — efficiency:
  eficiencia = final_pnl / max_pnl_alcanzado
  Target: > 0.55 average.
  If < 0.40  → ratchet_ratio too low; raise it.
  If > 0.80  → ratchet too tight; lower ratchet_step_pct.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ─── Per-symbol ratchet parameters ───────────────────────────────────────────
# All pct values are fractions of stake_usdt (not price %).
# max_duration_seg: hard timeout in seconds (safety net for stagnant trades).
SYMBOL_RATCHET_PARAMS: dict[str, dict[str, Any]] = {
    "R_50": {
        "sl_inicial_pct":       0.35,
        "ratchet_step_pct":     0.15,
        "ratchet_ratio":        0.55,
        "momentum_window":      15,
        "agotamiento_threshold": 0.35,
        "max_duration_seg":     480,
    },
    "R_75": {
        "sl_inicial_pct":       0.30,
        "ratchet_step_pct":     0.20,
        "ratchet_ratio":        0.60,
        "momentum_window":      20,
        "agotamiento_threshold": 0.40,
        "max_duration_seg":     600,
    },
    "R_100": {
        "sl_inicial_pct":       0.40,
        "ratchet_step_pct":     0.20,
        "ratchet_ratio":        0.50,
        "momentum_window":      15,
        "agotamiento_threshold": 0.35,
        "max_duration_seg":     480,
    },
    "BOOM1000": {
        "sl_inicial_pct":       0.25,
        "ratchet_step_pct":     0.30,
        "ratchet_ratio":        0.65,
        "momentum_window":      30,
        "agotamiento_threshold": 0.45,
        "max_duration_seg":     900,
    },
    "CRASH1000": {
        "sl_inicial_pct":       0.25,
        "ratchet_step_pct":     0.30,
        "ratchet_ratio":        0.65,
        "momentum_window":      30,
        "agotamiento_threshold": 0.45,
        "max_duration_seg":     900,
    },
    "CRASH500": {
        "sl_inicial_pct":       0.30,
        "ratchet_step_pct":     0.25,
        "ratchet_ratio":        0.60,
        "momentum_window":      25,
        "agotamiento_threshold": 0.40,
        "max_duration_seg":     720,
    },
    "BOOM500": {
        "sl_inicial_pct":       0.25,
        "ratchet_step_pct":     0.30,
        "ratchet_ratio":        0.65,
        "momentum_window":      30,
        "agotamiento_threshold": 0.45,
        "max_duration_seg":     900,
    },
    # ── NEW: BOOM/CRASH 900 ─────────────────────────────────────────────────
    "BOOM900": {
        "sl_inicial_pct":       1.00,   # 100% stake = full oxygen (Phase 2 scoring)
        "ratchet_step_pct":     0.30,   # same as BOOM1000
        "ratchet_ratio":        0.65,
        "momentum_window":      30,
        "agotamiento_threshold": 0.45,
        "max_duration_seg":     810,    # 90% × 900 ticks
    },
    "CRASH900": {
        "sl_inicial_pct":       1.00,
        "ratchet_step_pct":     0.30,
        "ratchet_ratio":        0.65,
        "momentum_window":      30,
        "agotamiento_threshold": 0.45,
        "max_duration_seg":     810,
    },
    # ── NEW: BOOM/CRASH 600 ─────────────────────────────────────────────────
    "BOOM600": {
        "sl_inicial_pct":       1.00,
        "ratchet_step_pct":     0.25,   # slightly less aggressive (shorter duration)
        "ratchet_ratio":        0.62,
        "momentum_window":      25,
        "agotamiento_threshold": 0.43,
        "max_duration_seg":     540,    # 90% × 600 ticks
    },
    "CRASH600": {
        "sl_inicial_pct":       1.00,
        "ratchet_step_pct":     0.25,
        "ratchet_ratio":        0.62,
        "momentum_window":      25,
        "agotamiento_threshold": 0.43,
        "max_duration_seg":     540,
    },
    # ── NEW: BOOM/CRASH 300 (defined but inactive until data confirms edge) ──
    "BOOM300": {
        "sl_inicial_pct":       1.00,
        "ratchet_step_pct":     0.20,   # faster ratchet for short-duration spikes
        "ratchet_ratio":        0.58,
        "momentum_window":      20,
        "agotamiento_threshold": 0.40,
        "max_duration_seg":     270,    # 90% × 300 ticks
    },
    "CRASH300": {
        "sl_inicial_pct":       1.00,
        "ratchet_step_pct":     0.20,
        "ratchet_ratio":        0.58,
        "momentum_window":      20,
        "agotamiento_threshold": 0.40,
        "max_duration_seg":     270,
    },
}

_DEFAULT_PARAMS: dict[str, Any] = {
    "sl_inicial_pct":       0.35,
    "ratchet_step_pct":     0.20,
    "ratchet_ratio":        0.55,
    "momentum_window":      20,
    "agotamiento_threshold": 0.40,
    "max_duration_seg":     600,
}

_TICK_LOG_LIMIT = 300    # max tick-log entries per contract (memory bound)
_PRICE_HIST_LEN = 100   # rolling price history for ATR + momentum


def _get_params(symbol: str) -> dict[str, Any]:
    """Return per-symbol ratchet params, falling back to defaults."""
    return SYMBOL_RATCHET_PARAMS.get(symbol, _DEFAULT_PARAMS)


# ─── Per-contract state ───────────────────────────────────────────────────────
@dataclass
class _PositionState:
    contract_id: int
    symbol: str
    stake: float
    entry_price: float
    entry_ts: float
    params: dict[str, Any]

    # Phase: 1=initial, 2=ratchet active
    fase: int = 1
    # SL floor in USD PnL (negative = loss allowed; updates only upward)
    sl_ratchet: float = field(init=False)
    # Peak profit seen since entry (USD)
    peak_profit: float = 0.0
    # Rolling price buffer for momentum/ATR computation
    price_history: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=_PRICE_HIST_LEN)
    )
    # Absolute momentum peak since phase-2 activation
    momentum_peak: float = 0.0
    # Per-tick audit log
    tick_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.sl_ratchet = -self.params["sl_inicial_pct"] * self.stake

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _atr(self) -> float:
        """Average absolute tick change over the price history window."""
        prices = list(self.price_history)
        if len(prices) < 2:
            return 1.0
        diffs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        return max(sum(diffs) / len(diffs), 1e-10)

    def calc_momentum(self) -> float:
        """Momentum = (price_now − price_N_back) / ATR.

        Normalised by ATR so it is comparable across symbols (R_50 vs R_100
        have wildly different absolute price levels but similar ATR-relative
        moves).  Returns 0.0 when there is insufficient history.
        """
        window: int = self.params["momentum_window"]
        prices = list(self.price_history)
        if len(prices) < window + 1:
            return 0.0
        atr = self._atr()
        return (prices[-1] - prices[-(window + 1)]) / atr


# ─── Public manager ──────────────────────────────────────────────────────────
class DynamicPositionManager:
    """Manages post-entry lifecycle for all open Deriv contracts.

    Usage:
        dpm = DynamicPositionManager()
        dpm.register(contract_id, symbol, stake, entry_price)
        ...
        reason = dpm.on_tick(contract_id, float_pnl, current_price)
        if reason:
            # close the contract with this reason
            stats = dpm.get_close_stats(contract_id, final_pnl)
            dpm.unregister(contract_id)
    """

    def __init__(self) -> None:
        self._states: dict[int, _PositionState] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def register(
        self,
        contract_id: int,
        symbol: str,
        stake: float,
        entry_price: float,
        entry_ts: float | None = None,
    ) -> None:
        """Register a newly opened contract for dynamic management."""
        params = _get_params(symbol)
        state = _PositionState(
            contract_id=contract_id,
            symbol=symbol,
            stake=stake,
            entry_price=entry_price,
            entry_ts=entry_ts if entry_ts is not None else time.time(),
            params=params,
        )
        self._states[contract_id] = state
        _LOGGER.info(
            "[DPM] register contract_id=%s symbol=%s stake=%.2f "
            "sl_inicial=%.2f%% ratchet_step=%.2f%% ratio=%.2f "
            "agotamiento=%.2f max_dur=%ds sl_floor_usd=%.4f",
            contract_id, symbol, stake,
            params["sl_inicial_pct"] * 100,
            params["ratchet_step_pct"] * 100,
            params["ratchet_ratio"],
            params["agotamiento_threshold"],
            params["max_duration_seg"],
            state.sl_ratchet,
        )

    def unregister(self, contract_id: int) -> dict[str, Any] | None:
        """Remove a contract from tracking and return its final state summary."""
        state = self._states.pop(contract_id, None)
        if state is None:
            return None
        return {
            "max_pnl_alcanzado": state.peak_profit,
            "dpm_fase":          state.fase,
            "sl_ratchet_final":  state.sl_ratchet,
            "momentum_peak":     state.momentum_peak,
            "duracion_real_seg": round(time.time() - state.entry_ts, 1),
        }

    # ── Core evaluation ──────────────────────────────────────────────────────
    def on_tick(
        self,
        contract_id: int,
        float_pnl: float,
        current_price: float,
        current_ts: float | None = None,
    ) -> str | None:
        """Evaluate one market tick.  Returns a close-reason string or None.

        Args:
            contract_id:   Deriv contract ID.
            float_pnl:     Current floating PnL in USD (from proposal_open_contract).
            current_price: Current underlying spot price.
            current_ts:    Unix timestamp of the tick (defaults to now).

        Returns:
            One of: "sl_inicial" | "ratchet_sl_alcanzado" | "momentum_agotado" |
                    "timeout_max" | None (hold position).
        """
        state = self._states.get(contract_id)
        if state is None:
            return None

        now = current_ts if current_ts is not None else time.time()
        params = state.params

        # Feed price history (skip zero — missing data)
        if current_price > 0:
            state.price_history.append(current_price)

        # Ratchet up peak profit (monotonic)
        if float_pnl > state.peak_profit:
            state.peak_profit = float_pnl

        # ── Phase 3: Hard time cap (checked first, always active) ────────────
        elapsed = now - state.entry_ts
        if elapsed > params["max_duration_seg"]:
            self._log_tick(state, float_pnl, now, close_reason="timeout_max")
            _LOGGER.info(
                "[DPM] timeout_max contract_id=%s symbol=%s "
                "elapsed=%.0fs > max=%ds pnl=%.4f",
                contract_id, state.symbol, elapsed, params["max_duration_seg"], float_pnl,
            )
            return "timeout_max"

        # ── Phase 1: Initial SL protection ───────────────────────────────────
        if state.fase == 1:
            sl_floor_usd = -params["sl_inicial_pct"] * state.stake

            # Initial SL breach
            if float_pnl <= sl_floor_usd:
                self._log_tick(state, float_pnl, now, close_reason="sl_inicial")
                _LOGGER.info(
                    "[DPM] sl_inicial contract_id=%s symbol=%s "
                    "pnl=%.4f <= sl_floor=%.4f stake=%.2f",
                    contract_id, state.symbol, float_pnl, sl_floor_usd, state.stake,
                )
                return "sl_inicial"

            # Ratchet step reached → promote to Phase 2
            # Use a tiny epsilon to guard against floating-point representation
            # errors (e.g. 0.20 * 1.50 = 0.30000000000000004 in IEEE 754).
            ratchet_step_usd = params["ratchet_step_pct"] * state.stake
            if float_pnl >= ratchet_step_usd - 1e-9:
                new_sl = params["ratchet_ratio"] * float_pnl
                state.sl_ratchet = max(state.sl_ratchet, new_sl)
                state.fase = 2
                # Capture initial momentum peak so agotamiento has a reference
                mom = state.calc_momentum()
                if mom > 0:
                    state.momentum_peak = mom
                _LOGGER.info(
                    "[DPM] FASE1→2 contract_id=%s symbol=%s "
                    "pnl=%.4f ratchet_step=%.4f sl_ratchet=%.4f mom_peak=%.4f",
                    contract_id, state.symbol,
                    float_pnl, ratchet_step_usd, state.sl_ratchet, state.momentum_peak,
                )

        # ── Phase 2: Active ratchet + momentum ───────────────────────────────
        if state.fase == 2:
            # Update SL floor (only moves up — never down)
            new_sl_candidate = params["ratchet_ratio"] * state.peak_profit
            if new_sl_candidate > state.sl_ratchet:
                _LOGGER.info(
                    "[DPM] ratchet_up contract_id=%s symbol=%s "
                    "peak=%.4f sl %.4f → %.4f",
                    contract_id, state.symbol,
                    state.peak_profit, state.sl_ratchet, new_sl_candidate,
                )
                state.sl_ratchet = new_sl_candidate

            # SL floor breach
            if float_pnl <= state.sl_ratchet:
                self._log_tick(state, float_pnl, now, close_reason="ratchet_sl_alcanzado")
                _LOGGER.info(
                    "[DPM] ratchet_sl_alcanzado contract_id=%s symbol=%s "
                    "pnl=%.4f <= sl=%.4f peak=%.4f",
                    contract_id, state.symbol,
                    float_pnl, state.sl_ratchet, state.peak_profit,
                )
                return "ratchet_sl_alcanzado"

            # Momentum check
            mom = state.calc_momentum()
            if mom > state.momentum_peak:
                state.momentum_peak = mom

            # Exhaustion: momentum dropped to threshold fraction of peak
            if (
                state.momentum_peak > 0
                and mom < state.momentum_peak * params["agotamiento_threshold"]
            ):
                self._log_tick(state, float_pnl, now, close_reason="momentum_agotado")
                _LOGGER.info(
                    "[DPM] momentum_agotado contract_id=%s symbol=%s "
                    "mom=%.4f < threshold(%.2f × peak=%.4f) pnl=%.4f",
                    contract_id, state.symbol,
                    mom, params["agotamiento_threshold"], state.momentum_peak, float_pnl,
                )
                return "momentum_agotado"

        # No close trigger — log tick and hold
        self._log_tick(state, float_pnl, now)
        return None

    # ── Analytics ────────────────────────────────────────────────────────────
    def get_close_stats(self, contract_id: int, final_pnl: float) -> dict[str, Any]:
        """Compute close metrics including the key efficiency ratio.

        Call BEFORE unregister() to capture the last state.
        eficiencia = final_pnl / max_pnl_alcanzado  (target > 0.55)
        """
        state = self._states.get(contract_id)
        if state is None:
            return {}
        max_pnl = max(state.peak_profit, final_pnl)
        efficiency = round(final_pnl / max_pnl, 4) if max_pnl > 0 else 0.0
        duration = round(time.time() - state.entry_ts, 1)
        if efficiency < 0.40:
            _LOGGER.warning(
                "[DPM] LOW_EFFICIENCY contract_id=%s symbol=%s "
                "eficiencia=%.2f < 0.40 — consider raising ratchet_ratio",
                contract_id, state.symbol, efficiency,
            )
        elif efficiency > 0.80:
            _LOGGER.info(
                "[DPM] HIGH_EFFICIENCY contract_id=%s symbol=%s "
                "eficiencia=%.2f > 0.80 — ratchet may be too tight",
                contract_id, state.symbol, efficiency,
            )
        return {
            "max_pnl_alcanzado":  round(max_pnl, 4),
            "eficiencia":         efficiency,
            "dpm_fase":           state.fase,
            "sl_ratchet_final":   round(state.sl_ratchet, 4),
            "momentum_peak":      round(state.momentum_peak, 4),
            "duracion_real_seg":  duration,
        }

    def get_state_snapshot(self, contract_id: int) -> dict[str, Any]:
        """Return a live snapshot of the contract's DPM state (for dashboard sync)."""
        state = self._states.get(contract_id)
        if state is None:
            return {}
        mom = state.calc_momentum()
        return {
            "dpm_fase":       state.fase,
            "sl_ratchet":     round(state.sl_ratchet, 4),
            "peak_profit":    round(state.peak_profit, 4),
            "momentum_actual": round(mom, 4),
            "momentum_peak":  round(state.momentum_peak, 4),
            "elapsed_seg":    round(time.time() - state.entry_ts, 1),
        }

    def get_tick_log(self, contract_id: int) -> list[dict[str, Any]]:
        """Return the per-tick audit log for a contract (read-only copy)."""
        state = self._states.get(contract_id)
        return list(state.tick_log) if state else []

    def is_registered(self, contract_id: int) -> bool:
        return contract_id in self._states

    # ── Internal ─────────────────────────────────────────────────────────────
    def _log_tick(
        self,
        state: _PositionState,
        float_pnl: float,
        now: float,
        close_reason: str | None = None,
    ) -> None:
        """Append one entry to the per-contract tick log."""
        mom = state.calc_momentum()
        entry: dict[str, Any] = {
            "ts":               round(now, 2),
            "float_pnl":        round(float_pnl, 4),
            "sl_ratchet":       round(state.sl_ratchet, 4),
            "peak_profit":      round(state.peak_profit, 4),
            "momentum_actual":  round(mom, 4),
            "momentum_pico":    round(state.momentum_peak, 4),
            "fase":             state.fase,
        }
        if close_reason:
            entry["close_reason"] = close_reason
        state.tick_log.append(entry)
        # Keep log bounded in memory
        if len(state.tick_log) > _TICK_LOG_LIMIT:
            state.tick_log = state.tick_log[-_TICK_LOG_LIMIT:]
