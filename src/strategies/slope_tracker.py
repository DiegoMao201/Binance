"""D.10.1 — Slope Tracker con triple lógica de entrada para BOOM500/CRASH500.

CAMINO 1 — Slope Level (normalizado %/min, calibrado con N=13,639 spikes):
  BOOM500:  slope_pct >= +0.005%/min  → 53-56% spikes grandes
  CRASH500: slope_pct <= -0.005%/min  → 52-55% spikes grandes
  Pending: 120s (DERIV_D10_PENDING_CAMINO1_SEC)

CAMINO 2 — Pattern PN.5 (slope extremo + cambio grande, 64% predictividad):
  BOOM500:  slope_pct > +0.018% (P75) AND |cambio| > 0.015%
  CRASH500: slope_pct < -0.022% (P25) AND |cambio| > 0.015%
  Pending: 5s (DERIV_D10_PENDING_CAMINO2_SEC, casi inmediato)

CAMINO 3 — Pattern BREAKOUT (intuición Diego):
  BOOM500:  slope_pct < -0.005% (bajando) AND cambio > +0.015% (giro arriba)
  CRASH500: slope_pct > +0.005% (subiendo) AND cambio < -0.015% (giro abajo)
  Pending: 120s (DERIV_D10_PENDING_CAMINO3_SEC, conservador)

Kill switch global: DERIV_D10_SLOPE_GATE_ENABLED=false
Kill switches por camino: DERIV_D10_PN5_ENABLED, DERIV_D10_BREAKOUT_ENABLED
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
    slope_anterior_pct: Optional[float] = None
    slope_anterior_ts: float = 0.0


class SlopeTracker:
    SYMBOLS = {"BOOM500", "CRASH500"}

    def __init__(self) -> None:
        self.states: dict[str, SymbolSlopeState] = {
            sym: SymbolSlopeState(symbol=sym) for sym in self.SYMBOLS
        }
        self._reload_config()

    def _reload_config(self) -> None:
        def _bool(key: str, default: str = "true") -> bool:
            return os.getenv(key, default).lower() in {"1", "true", "yes", "on"}

        def _int(key: str, default: int) -> int:
            return int(os.getenv(key, str(default)) or default)

        def _float(key: str, default: float) -> float:
            return float(os.getenv(key, str(default)) or default)

        self.enabled = _bool("DERIV_D10_SLOPE_GATE_ENABLED")
        self.stabilize_sec = _int("DERIV_D10_SPIKE_STABILIZE_SEC", 180)
        self.window_sec = _int("DERIV_D10_SLOPE_WINDOW_SEC", 600)
        self.log_interval_sec = _int("DERIV_D10_LOG_INTERVAL_SEC", 30)
        self.log_path = os.getenv("DERIV_D10_LOG_PATH", "/data/logs/slope_history.jsonl")

        # CAMINO 1 — Slope Level (normalizado %/min)
        self.c1_boom500_min_pct = _float("DERIV_D10_BOOM500_SLOPE_MIN_PCT", 0.005)
        self.c1_crash500_max_pct = _float("DERIV_D10_CRASH500_SLOPE_MAX_PCT", -0.005)
        self.c1_pending_sec = _int("DERIV_D10_PENDING_CAMINO1_SEC", 120)

        # CAMINO 2 — Pattern PN.5 (slope extremo + cambio grande)
        self.c2_enabled = _bool("DERIV_D10_PN5_ENABLED")
        self.c2_boom500_min_pct = _float("DERIV_D10_PN5_BOOM500_SLOPE_MIN_PCT", 0.018)
        self.c2_crash500_max_pct = _float("DERIV_D10_PN5_CRASH500_SLOPE_MAX_PCT", -0.022)
        self.c2_cambio_min_pct = _float("DERIV_D10_PN5_CAMBIO_MIN_PCT", 0.015)
        self.c2_pending_sec = max(1, _int("DERIV_D10_PENDING_CAMINO2_SEC", 5))

        # CAMINO 3 — Pattern BREAKOUT (intuición Diego)
        self.c3_enabled = _bool("DERIV_D10_BREAKOUT_ENABLED")
        self.c3_boom500_slope_max_pct = _float("DERIV_D10_BREAKOUT_BOOM500_SLOPE_MAX_PCT", -0.005)
        self.c3_crash500_slope_min_pct = _float("DERIV_D10_BREAKOUT_CRASH500_SLOPE_MIN_PCT", 0.005)
        self.c3_cambio_min_pct = _float("DERIV_D10_BREAKOUT_CAMBIO_MIN_PCT", 0.015)
        self.c3_pending_sec = _int("DERIV_D10_PENDING_CAMINO3_SEC", 120)

    # ──────────────────────────────────────────────────────────────────────
    # Public API — llamado desde main_deriv.py
    # ──────────────────────────────────────────────────────────────────────

    def on_tick(
        self,
        symbol: str,
        price: float,
        last_spike_ts: Optional[float] = None,
    ) -> None:
        sym = symbol.upper()
        if sym not in self.SYMBOLS:
            return
        st = self.states[sym]
        now = time.time()

        if last_spike_ts and last_spike_ts > st.ultimo_spike_ts:
            old_buf = len(st.precios_buffer)
            st.ultimo_spike_ts = last_spike_ts
            st.precios_buffer.clear()
            st.estabilizado = False
            st.slope_anterior_pct = None
            st.slope_anterior_ts = 0.0
            logger.info(
                "[D10] %s nuevo spike ts=%.0f → buffer reset (buf_prev=%d)",
                sym, last_spike_ts, old_buf,
            )

        if not st.estabilizado and st.ultimo_spike_ts > 0:
            elapsed = now - st.ultimo_spike_ts
            if elapsed >= self.stabilize_sec:
                st.estabilizado = True
                logger.info("[D10] %s estabilizado (%.0fs desde spike)", sym, elapsed)

        st.precios_buffer.append((now, price))

        if (now - st.ultimo_log_ts) >= self.log_interval_sec:
            st.ultimo_log_ts = now
            self._log_state(sym, now, price)

    def on_position_close_tier(self, symbol: str) -> None:
        """CAMINO 4: Soft-reset post-tier para permitir re-entrada inmediata.

        Limpia el historial de slope_anterior (nuevo ciclo de medición)
        sin resetear el buffer de precios ni el spike_ts. El buffer sigue
        acumulando desde el spike actual → permite re-evaluar caminos.
        """
        sym = symbol.upper()
        if sym not in self.SYMBOLS:
            return
        st = self.states[sym]
        st.slope_anterior_pct = None
        st.slope_anterior_ts = 0.0
        logger.info("[D10] %s on_position_close_tier → slope_anterior reset", sym)

    def can_enter(
        self, symbol: str, ts: Optional[float] = None
    ) -> tuple[bool, str, dict]:
        """Evalúa los 3 caminos. Retorna (ok, camino_o_razon, details).

        details incluye: slope_pct, cambio_pct, n_precios, pending_sec.
        ok=True → caller crea pending con details['pending_sec'] segundos.
        ok=False → caller bloquea con block_reason='d10_slope_gate'.
        """
        sym = symbol.upper()
        if sym not in self.SYMBOLS:
            return True, "not_tracked", {}

        if not self.enabled:
            return True, "disabled", {}

        st = self.states[sym]
        now = ts or time.time()

        # Gate 0: estabilización post-spike
        if not st.estabilizado:
            elapsed = (now - st.ultimo_spike_ts) if st.ultimo_spike_ts > 0 else 0.0
            return False, f"stabilizing_{elapsed:.0f}s_of_{self.stabilize_sec}s", {}

        # Gate 1: suficientes precios en ventana
        cutoff = now - self.window_sec
        recent = [(t, p) for t, p in st.precios_buffer if t >= cutoff]
        if len(recent) < 10:
            return False, f"insuf_data_{len(recent)}_pts", {}

        # Calcular slope normalizado (%/min)
        slope_pct = self._calc_slope_pct(recent)
        if slope_pct is None:
            return False, "slope_calc_error", {}

        # Calcular cambio vs slope anterior
        cambio_pct: Optional[float] = None
        if st.slope_anterior_pct is not None:
            cambio_pct = slope_pct - st.slope_anterior_pct

        # Actualizar historial cada 30s (para detectar cambios reales)
        if st.slope_anterior_ts == 0 or (now - st.slope_anterior_ts) >= 30.0:
            st.slope_anterior_pct = slope_pct
            st.slope_anterior_ts = now

        details: dict = {
            "slope_pct": round(slope_pct, 6),
            "cambio_pct": round(cambio_pct, 6) if cambio_pct is not None else None,
            "n_precios": len(recent),
        }

        # ══════════════════════════════════════════════════════════════════
        # CAMINO 2 — Pattern PN.5 (prioridad: mayor predictividad)
        # Requiere cambio_pct disponible (al menos 30s desde último update)
        # ══════════════════════════════════════════════════════════════════
        if self.c2_enabled and cambio_pct is not None:
            abs_cambio = abs(cambio_pct)
            if sym == "BOOM500":
                if slope_pct >= self.c2_boom500_min_pct and abs_cambio >= self.c2_cambio_min_pct:
                    return True, "camino2_pn5", {**details, "pending_sec": self.c2_pending_sec}
            elif sym == "CRASH500":
                if slope_pct <= self.c2_crash500_max_pct and abs_cambio >= self.c2_cambio_min_pct:
                    return True, "camino2_pn5", {**details, "pending_sec": self.c2_pending_sec}

        # ══════════════════════════════════════════════════════════════════
        # CAMINO 3 — Pattern BREAKOUT (slope opuesto + giro grande)
        # ══════════════════════════════════════════════════════════════════
        if self.c3_enabled and cambio_pct is not None:
            if sym == "BOOM500":
                # Mercado bajando pero giro fuerte hacia arriba = breakout up
                if slope_pct <= self.c3_boom500_slope_max_pct and cambio_pct >= self.c3_cambio_min_pct:
                    return True, "camino3_breakout", {**details, "pending_sec": self.c3_pending_sec}
            elif sym == "CRASH500":
                # Mercado subiendo pero giro fuerte hacia abajo = breakout down
                if slope_pct >= self.c3_crash500_slope_min_pct and cambio_pct <= -self.c3_cambio_min_pct:
                    return True, "camino3_breakout", {**details, "pending_sec": self.c3_pending_sec}

        # ══════════════════════════════════════════════════════════════════
        # CAMINO 1 — Slope Level (gate por defecto)
        # ══════════════════════════════════════════════════════════════════
        if sym == "BOOM500":
            if slope_pct >= self.c1_boom500_min_pct:
                return True, "camino1_level", {**details, "pending_sec": self.c1_pending_sec}
            return (
                False,
                f"c1_bloqueado_slope={slope_pct:+.6f}_min={self.c1_boom500_min_pct:+.4f}",
                details,
            )
        else:  # CRASH500
            if slope_pct <= self.c1_crash500_max_pct:
                return True, "camino1_level", {**details, "pending_sec": self.c1_pending_sec}
            return (
                False,
                f"c1_bloqueado_slope={slope_pct:+.6f}_max={self.c1_crash500_max_pct:+.4f}",
                details,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _calc_slope_pct(
        self, precios: list[tuple[float, float]]
    ) -> Optional[float]:
        """OLS slope normalizado: (pts/min) / precio_promedio * 100 = %/min."""
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
        slope_pts_min = (n * sum_xy - sum_x * sum_y) / denom
        p_avg = sum_y / n
        if p_avg < 1e-9:
            return None
        return (slope_pts_min / p_avg) * 100.0

    def _log_state(self, symbol: str, ts: float, price: float) -> None:
        st = self.states[symbol]
        cutoff = ts - self.window_sec
        recent = [(t, p) for t, p in st.precios_buffer if t >= cutoff]
        slope_pct = self._calc_slope_pct(recent) if len(recent) >= 10 else None
        cambio_pct: Optional[float] = None
        if slope_pct is not None and st.slope_anterior_pct is not None:
            cambio_pct = slope_pct - st.slope_anterior_pct
        entry = {
            "ts": round(ts, 3),
            "symbol": symbol,
            "price": round(price, 4),
            "spike_ts": round(st.ultimo_spike_ts, 3),
            "estabilizado": st.estabilizado,
            "n_prices": len(recent),
            "slope_pct": round(slope_pct, 6) if slope_pct is not None else None,
            "cambio_pct": round(cambio_pct, 6) if cambio_pct is not None else None,
        }
        try:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("[D10] _log_state error: %s", exc)
