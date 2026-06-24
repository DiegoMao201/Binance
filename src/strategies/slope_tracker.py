"""D.10.0 — Slope Level Gate tracker para BOOM500/CRASH500.

Mide la pendiente del precio via regresión lineal sobre una ventana deslizante.
Kill switch: DERIV_D10_SLOPE_GATE_ENABLED=false
"""
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SymbolSlopeState:
    symbol: str
    ultimo_spike_ts: float = 0.0
    precios_buffer: deque = field(default_factory=lambda: deque(maxlen=1000))
    ultimo_log_ts: float = 0.0
    estabilizado: bool = False


class SlopeTracker:
    SYMBOLS = {"BOOM500", "CRASH500"}

    def __init__(self) -> None:
        self.states: dict[str, SymbolSlopeState] = {
            sym: SymbolSlopeState(symbol=sym) for sym in self.SYMBOLS
        }
        self._reload_config()

    def _reload_config(self) -> None:
        self.enabled: bool = (
            os.getenv("DERIV_D10_SLOPE_GATE_ENABLED", "true").lower()
            in {"1", "true", "yes", "on"}
        )
        self.stabilize_sec: int = int(
            os.getenv("DERIV_D10_SPIKE_STABILIZE_SEC", "180") or 180
        )
        self.window_sec: int = int(
            os.getenv("DERIV_D10_SLOPE_WINDOW_SEC", "600") or 600
        )
        self.boom500_min: float = float(
            os.getenv("DERIV_D10_BOOM500_SLOPE_MIN", "0.24") or 0.24
        )
        self.crash500_max: float = float(
            os.getenv("DERIV_D10_CRASH500_SLOPE_MAX", "-0.90") or -0.90
        )
        self.log_interval_sec: int = int(
            os.getenv("DERIV_D10_LOG_INTERVAL_SEC", "30") or 30
        )
        self.log_path: str = os.getenv(
            "DERIV_D10_LOG_PATH", "/data/logs/slope_history.jsonl"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def on_tick(
        self,
        symbol: str,
        price: float,
        last_spike_ts: Optional[float] = None,
    ) -> None:
        """Ingest a price tick. Call once per tick from _handle_tick."""
        sym = symbol.upper()
        if sym not in self.SYMBOLS:
            return
        st = self.states[sym]
        now = time.time()

        # Detect new spike → reset buffer and estabilizado flag
        if last_spike_ts and last_spike_ts > st.ultimo_spike_ts:
            st.ultimo_spike_ts = last_spike_ts
            st.precios_buffer.clear()
            st.estabilizado = False
            logger.info(
                "[D10] %s nuevo spike ts=%.0f → buffer reset", sym, last_spike_ts
            )

        # Mark as stabilized once DERIV_D10_SPIKE_STABILIZE_SEC has elapsed
        if not st.estabilizado and st.ultimo_spike_ts > 0:
            if (now - st.ultimo_spike_ts) >= self.stabilize_sec:
                st.estabilizado = True
                logger.info(
                    "[D10] %s estabilizado (%.0fs desde spike)",
                    sym,
                    now - st.ultimo_spike_ts,
                )

        st.precios_buffer.append((now, price))

        # Periodic disk log
        if (now - st.ultimo_log_ts) >= self.log_interval_sec:
            st.ultimo_log_ts = now
            self._log_state(sym, now, price)

    def can_enter(
        self, symbol: str, ts: Optional[float] = None
    ) -> tuple[bool, str, Optional[float]]:
        """Return (ok, reason_str, slope_pts_per_min).

        ok=True  → slope gate passes, caller may create pending.
        ok=False → caller must block with block_reason='d10_slope_gate'.
        slope is None when it couldn't be computed.
        """
        sym = symbol.upper()
        if sym not in self.SYMBOLS:
            return True, "not_tracked", None

        if not self.enabled:
            return True, "disabled", None

        st = self.states[sym]
        now = ts or time.time()

        # Gate 1: spike must have been detected and stabilization elapsed
        if not st.estabilizado:
            elapsed = (now - st.ultimo_spike_ts) if st.ultimo_spike_ts > 0 else 0.0
            return (
                False,
                f"stabilizing_{elapsed:.0f}s_of_{self.stabilize_sec}s",
                None,
            )

        # Gate 2: enough recent prices
        cutoff = now - self.window_sec
        recent = [(t, p) for t, p in st.precios_buffer if t >= cutoff]
        if len(recent) < 10:
            return False, f"insuf_data_{len(recent)}_pts", None

        # Gate 3: compute slope
        slope = self._calc_slope(recent)
        if slope is None:
            return False, "slope_calc_error", None

        # Gate 4: directional threshold
        if sym == "BOOM500":
            if slope >= self.boom500_min:
                return True, f"slope_{slope:+.3f}>={self.boom500_min:+.3f}", slope
            return False, f"slope_{slope:+.3f}<{self.boom500_min:+.3f}", slope
        else:  # CRASH500
            if slope <= self.crash500_max:
                return True, f"slope_{slope:+.3f}<={self.crash500_max:+.3f}", slope
            return False, f"slope_{slope:+.3f}>{self.crash500_max:+.3f}", slope

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _calc_slope(
        self, precios: list[tuple[float, float]]
    ) -> Optional[float]:
        """Linear regression slope in pts/minute via OLS normal equations."""
        n = len(precios)
        if n < 2:
            return None
        t0 = precios[0][0]
        xs = [(t - t0) / 60.0 for t, _ in precios]
        ys = [p for _, p in precios]
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)
        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-12:
            return None
        return (n * sum_xy - sum_x * sum_y) / denom

    def _log_state(self, symbol: str, ts: float, price: float) -> None:
        """Append a JSON line to the slope history log."""
        st = self.states[symbol]
        cutoff = ts - self.window_sec
        recent = [(t, p) for t, p in st.precios_buffer if t >= cutoff]
        slope = self._calc_slope(recent) if len(recent) >= 10 else None
        entry = {
            "ts": round(ts, 3),
            "symbol": symbol,
            "price": price,
            "spike_ts": st.ultimo_spike_ts,
            "estabilizado": st.estabilizado,
            "n_prices": len(recent),
            "slope": round(slope, 4) if slope is not None else None,
        }
        try:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("[D10] _log_state error: %s", exc)
