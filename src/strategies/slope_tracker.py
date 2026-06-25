"""D.10.1 — Slope Tracker con triple lógica de entrada para BOOM500/CRASH500.

BOOM500 siempre tiene slope NEGATIVO (precio baja entre spikes → spike = reversal UP).
CRASH500 siempre tiene slope POSITIVO (precio sube entre spikes → spike = reversal DOWN).
Entramos en el estado NORMAL del mercado, esperando la reversión (spike).

CAMINO 1 — Slope Level + tendencia sostenida:
  BOOM500:  slope_pct <= -0.005%/min AND |cambio| >= C1_CAMBIO_MIN (dinámica detectada)
  CRASH500: slope_pct >= +0.005%/min AND |cambio| >= C1_CAMBIO_MIN (dinámica detectada)
  cambio=None bloquea: necesita 240s de historia (2 × cambio_window_sec)
  Pending: 120s (DERIV_D10_PENDING_CAMINO1_SEC)
  Stabilize: 0s — los 2×120s de ventanas son suficiente separación post-spike

CAMINO 2 — Tendencia extrema + estable (alta confianza en spike grande):
  BOOM500:  slope_pct <= -0.018%/min (bajada fuerte) AND |cambio| <= 0.010% (estable)
  CRASH500: slope_pct >= +0.022%/min (subida fuerte) AND |cambio| <= 0.010% (estable)
  Pending: 5s (DERIV_D10_PENDING_CAMINO2_SEC, casi inmediato)
  Stabilize: 60s propio (DERIV_D10_PN5_STABILIZE_SEC) — más rápido que C1/C3

CAMINO 3 — Breakout inminente (pendiente normal + giro hacia spike):
  BOOM500:  slope_pct <= -0.005% (normal bajando) AND cambio >= +0.015% (pendiente aumenta → spike UP inminente)
  CRASH500: slope_pct >= +0.005% (normal subiendo) AND cambio <= -0.015% (pendiente cae → spike DOWN inminente)
  Pending: 120s (DERIV_D10_PENDING_CAMINO3_SEC, conservador)
  Stabilize: 180s global post-spike

CAMBIO_PCT — diferencial de pendiente real:
  cambio_pct = slope(últimos CAMBIO_WINDOW_SEC) − slope(CAMBIO_WINDOW_SEC previos)
  Ventanas no-solapadas → elimina ruido de comparar OLS casi idénticos.
  Positivo = pendiente aumentando (BOOM: menos negativa = reversión inminente)
  Negativo = pendiente cayendo (CRASH: menos positiva = reversión inminente)
  Env: DERIV_D10_CAMBIO_WINDOW_SEC (default 120s, requiere 2x en buffer)

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


class SlopeTracker:
    SYMBOLS = {"BOOM500", "CRASH500"}           # gate activo + medición
    MEASURE_ONLY = {"BOOM1000", "CRASH1000"}    # solo medición, sin gate

    def __init__(self) -> None:
        self.states: dict[str, SymbolSlopeState] = {
            sym: SymbolSlopeState(symbol=sym)
            for sym in self.SYMBOLS | self.MEASURE_ONLY
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
        # 0s: los 2×120s de ventanas cambio ya garantizan datos post-spike separados.
        # El período de recuperación (primeros 0-120s) queda en "anterior"; el período
        # más estable (120-240s) en "reciente". Sin retraso extra encima de 2×120s.
        self.stabilize_sec = _int("DERIV_D10_SPIKE_STABILIZE_SEC", 0)
        self.window_sec = _int("DERIV_D10_SLOPE_WINDOW_SEC", 600)
        self.log_interval_sec = _int("DERIV_D10_LOG_INTERVAL_SEC", 30)
        self.log_path = os.getenv("DERIV_D10_LOG_PATH", "/data/logs/slope_history.jsonl")

        # Ventana para diferencial de pendiente (dos ventanas no-solapadas de este tamaño)
        # cambio_pct = slope(últimos N seg) − slope(N seg anteriores)
        self.cambio_window_sec = _int("DERIV_D10_CAMBIO_WINDOW_SEC", 120)

        # CAMINO 1 — Slope Level + dinámica detectada (|cambio| significativo)
        # cambio≈0 → BLOQUEA: mercado en estado estable, spike aún no llega
        # |cambio| >= MIN → PENDING: cualquier cambio en la pendiente = dinámica activa
        #   (aceleración: slope 1.0→1.2 ó desaceleración: 1.0→0.8 ambos son señal)
        # cambio=None → bloquea: menos de 240s de historia post-spike
        self.c1_boom500_max_pct = _float("DERIV_D10_BOOM500_SLOPE_MAX_PCT", -0.005)
        self.c1_crash500_min_pct = _float("DERIV_D10_CRASH500_SLOPE_MIN_PCT", 0.005)
        self.c1_cambio_min_pct = _float("DERIV_D10_C1_CAMBIO_MIN_PCT", 0.005)
        self.c1_pending_sec = _int("DERIV_D10_PENDING_CAMINO1_SEC", 120)

        # CAMINO 2 — Tendencia extrema + ESTABLE
        self.c2_enabled = _bool("DERIV_D10_PN5_ENABLED")
        self.c2_boom500_max_pct = _float("DERIV_D10_PN5_BOOM500_SLOPE_MAX_PCT", -0.018)
        self.c2_crash500_min_pct = _float("DERIV_D10_PN5_CRASH500_SLOPE_MIN_PCT", 0.022)
        self.c2_cambio_max_pct = _float("DERIV_D10_PN5_CAMBIO_MAX_PCT", 0.010)
        self.c2_stabilize_sec = _int("DERIV_D10_PN5_STABILIZE_SEC", 0)
        self.c2_pending_sec = max(1, _int("DERIV_D10_PENDING_CAMINO2_SEC", 5))

        # CAMINO 3 — Breakout inminente
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
        if sym not in self.SYMBOLS and sym not in self.MEASURE_ONLY:
            return
        st = self.states[sym]
        now = time.time()

        if last_spike_ts and last_spike_ts > st.ultimo_spike_ts:
            old_buf = len(st.precios_buffer)
            st.ultimo_spike_ts = last_spike_ts
            st.precios_buffer.clear()
            st.estabilizado = False
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
        """CAMINO 4: Soft-reset post-tier — no-op con el nuevo diferencial de ventanas."""
        pass

    def can_enter(
        self, symbol: str, ts: Optional[float] = None
    ) -> tuple[bool, str, dict]:
        """Evalúa los 3 caminos. Retorna (ok, camino_o_razon, details).

        Orden de evaluación:
          C2 (60s stabilize, entrada rápida 5s) → prioridad si condiciones extremas+estables
          C3 (180s stabilize, breakout reversal) → pendiente opuesta + giro
          C1 (180s stabilize, nivel base) → pendiente en dirección correcta

        ok=True  → caller crea pending con details['pending_sec'] segundos.
        ok=False → caller bloquea con block_reason='d10_slope_gate'.
        """
        sym = symbol.upper()
        if sym not in self.SYMBOLS:
            return True, "not_tracked", {}  # MEASURE_ONLY o no conocido → bypass gate

        if not self.enabled:
            return True, "disabled", {}

        st = self.states[sym]
        now = ts or time.time()
        elapsed = (now - st.ultimo_spike_ts) if st.ultimo_spike_ts > 0 else 0.0

        # Gate: suficientes precios (aplica siempre, independiente de estabilización)
        cutoff = now - self.window_sec
        recent = [(t, p) for t, p in st.precios_buffer if t >= cutoff]
        if len(recent) < 10:
            return False, f"insuf_data_{len(recent)}_pts", {}

        # Calcular slope normalizado (%/min) sobre ventana completa
        slope_pct = self._calc_slope_pct(recent)
        if slope_pct is None:
            return False, "slope_calc_error", {}

        # Diferencial de pendiente sobre dos ventanas no-solapadas
        # cambio_pct = slope(últimos W seg) − slope(W..2W seg atrás)
        # Positivo = pendiente aumentando, Negativo = pendiente cayendo
        cambio_pct = self._calc_cambio(st, now)

        details: dict = {
            "slope_pct": round(slope_pct, 6),
            "cambio_pct": round(cambio_pct, 6) if cambio_pct is not None else None,
            "n_precios": len(recent),
        }

        # ══════════════════════════════════════════════════════════════════
        # CAMINO 2 — Tendencia extrema + ESTABLE (solo 60s post-spike)
        # ══════════════════════════════════════════════════════════════════
        if self.c2_enabled and elapsed >= self.c2_stabilize_sec and cambio_pct is not None:
            abs_cambio = abs(cambio_pct)
            if sym == "BOOM500":
                if slope_pct <= self.c2_boom500_max_pct and abs_cambio <= self.c2_cambio_max_pct:
                    return True, "camino2_pn5", {**details, "pending_sec": self.c2_pending_sec}
            elif sym == "CRASH500":
                if slope_pct >= self.c2_crash500_min_pct and abs_cambio <= self.c2_cambio_max_pct:
                    return True, "camino2_pn5", {**details, "pending_sec": self.c2_pending_sec}

        # Gate global: estabilización post-spike 180s (C1 y C3 requieren esto)
        if not st.estabilizado:
            return False, f"stabilizing_{elapsed:.0f}s_of_{self.stabilize_sec}s", {}

        # ══════════════════════════════════════════════════════════════════
        # CAMINO 3 — Breakout inminente (pendiente normal + giro hacia spike)
        # ══════════════════════════════════════════════════════════════════
        if self.c3_enabled and cambio_pct is not None:
            if sym == "BOOM500":
                if slope_pct <= self.c3_boom500_slope_max_pct and cambio_pct >= self.c3_cambio_min_pct:
                    return True, "camino3_breakout", {**details, "pending_sec": self.c3_pending_sec}
            elif sym == "CRASH500":
                if slope_pct >= self.c3_crash500_slope_min_pct and cambio_pct <= -self.c3_cambio_min_pct:
                    return True, "camino3_breakout", {**details, "pending_sec": self.c3_pending_sec}

        # ══════════════════════════════════════════════════════════════════
        # CAMINO 1 — Slope Level + dinámica detectada
        # cambio≈0 (tendencia plana) → BLOQUEA: estado estable, spike aún lejos
        # |cambio| >= MIN → PENDING: cualquier movimiento en la pendiente es señal
        #   slope 0.012→0.008 ó 0.012→0.016 = ambos marcan posible spike
        # cambio=None → bloquea: menos de 240s de historia post-spike
        # ══════════════════════════════════════════════════════════════════
        if cambio_pct is None:
            return False, "c1_bloqueado_cambio_insuf_historia", details

        if abs(cambio_pct) < self.c1_cambio_min_pct:
            return (
                False,
                f"c1_bloqueado slope={slope_pct:+.6f} |cambio|={abs(cambio_pct):.6f} < min={self.c1_cambio_min_pct:.4f}",
                details,
            )

        if sym == "BOOM500":
            if slope_pct <= self.c1_boom500_max_pct:
                return True, "camino1_level", {**details, "pending_sec": self.c1_pending_sec}
            return (
                False,
                f"c1_bloqueado_slope={slope_pct:+.6f}_max={self.c1_boom500_max_pct:+.4f}",
                details,
            )
        else:  # CRASH500
            if slope_pct >= self.c1_crash500_min_pct:
                return True, "camino1_level", {**details, "pending_sec": self.c1_pending_sec}
            return (
                False,
                f"c1_bloqueado_slope={slope_pct:+.6f}_min={self.c1_crash500_min_pct:+.4f}",
                details,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _calc_cambio(self, st: SymbolSlopeState, now: float) -> Optional[float]:
        """Diferencial de pendiente entre dos ventanas no-solapadas.

        Ventana reciente:  [now - W, now)
        Ventana anterior:  [now - 2W, now - W)
        cambio = slope_reciente - slope_anterior

        IMPORTANTE: solo usa datos POST-ESTABILIZACIÓN (t >= spike_ts + stabilize_sec).
        Excluir el período de recuperación post-spike (0..stabilize_sec) que es muy
        volátil y haría el diferencial grande siempre, generando falsas señales en C1.
        Con esto, cambio solo refleja dinámica del mercado ya estabilizado.
        El primer cambio válido aparece en: spike_ts + stabilize_sec + 2×W
        """
        w = self.cambio_window_sec
        t_mid = now - w
        t_old = now - 2 * w

        # Excluir datos de la ventana de recuperación post-spike
        t_stabilized = (
            st.ultimo_spike_ts + self.stabilize_sec
            if st.ultimo_spike_ts > 0 else 0.0
        )

        reciente = [(t, p) for t, p in st.precios_buffer if t >= t_mid and t >= t_stabilized]
        anterior = [(t, p) for t, p in st.precios_buffer if t_old <= t < t_mid and t >= t_stabilized]

        if len(reciente) < 5 or len(anterior) < 5:
            return None

        sr = self._calc_slope_pct(reciente)
        sa = self._calc_slope_pct(anterior)
        if sr is None or sa is None:
            return None
        return sr - sa

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
        cambio_pct = self._calc_cambio(st, ts) if slope_pct is not None else None
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
