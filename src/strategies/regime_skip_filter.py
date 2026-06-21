"""
D.6.7 — Regime Skip Filter por símbolo (Diego, 2026-06-20)

Filtra ghost ALLOWs basado en régimen de mercado reciente por símbolo.
No modifica la lógica del ghost — solo decide si esta oportunidad específica
se activa o se saltea según la frecuencia configurada por régimen.

Regimes:
  BUENO    (skip=0, 1/1): timeout_pct <  30% AND spikes/h >= 2.0
  MEDIOCRE (skip=1, 1/2): timeout_pct <  50% AND spikes/h >= 1.0
  DIFÍCIL  (skip=2, 1/3): timeout_pct <  70%
  CRÍTICO  (skip=3, 1/4): otherwise

Evaluación cada 10 min con hysteresis de 2 evaluaciones consecutivas.
Ventana de análisis: últimos 30 minutos.
"""

import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("deriv.daemon")

_STATE_DIR  = os.getenv("DERIV_STATE_DIR") or os.getenv("BOT_STATE_DIR") or "/data/logs"
_STATE_FILE = os.path.join(_STATE_DIR, "d67_regime_state.json")

_ENABLED = (
    os.getenv("DERIV_D67_REGIME_FILTER_ENABLED", "false")
    .strip().lower() in {"1", "true", "yes", "on"}
)

_EVAL_INTERVAL_S = int(os.getenv("DERIV_D67_EVAL_INTERVAL_S",  "600") or 600)   # 10 min
_WINDOW_S        = int(os.getenv("DERIV_D67_WINDOW_S",         "1800") or 1800)  # 30 min
_HYSTERESIS      = int(os.getenv("DERIV_D67_HYSTERESIS",       "2") or 2)

# Umbrales de clasificación de régimen
_BUENO_TIMEOUT_MAX    = float(os.getenv("DERIV_D67_BUENO_TIMEOUT_MAX",    "0.30") or 0.30)
_BUENO_SPIKES_MIN     = float(os.getenv("DERIV_D67_BUENO_SPIKES_MIN",     "2.0")  or 2.0)
_MEDIOCRE_TIMEOUT_MAX = float(os.getenv("DERIV_D67_MEDIOCRE_TIMEOUT_MAX", "0.50") or 0.50)
_MEDIOCRE_SPIKES_MIN  = float(os.getenv("DERIV_D67_MEDIOCRE_SPIKES_MIN",  "1.0")  or 1.0)
_DIFICIL_TIMEOUT_MAX  = float(os.getenv("DERIV_D67_DIFICIL_TIMEOUT_MAX",  "0.70") or 0.70)

REGIME_NAMES = {0: "BUENO", 1: "MEDIOCRE", 2: "DIFÍCIL", 3: "CRÍTICO"}


def _classify_regime(timeout_pct: float, spikes_per_h: float) -> int:
    """Retorna skip count: 0=BUENO, 1=MEDIOCRE, 2=DIFÍCIL, 3=CRÍTICO."""
    if timeout_pct < _BUENO_TIMEOUT_MAX and spikes_per_h >= _BUENO_SPIKES_MIN:
        return 0
    if timeout_pct < _MEDIOCRE_TIMEOUT_MAX and spikes_per_h >= _MEDIOCRE_SPIKES_MIN:
        return 1
    if timeout_pct < _DIFICIL_TIMEOUT_MAX:
        return 2
    return 3


@dataclass
class _SymbolState:
    current_skip: int = 0           # skip count activo (post-hysteresis)
    pending_skip: int = 0           # candidato de cambio
    pending_count: int = 0          # evaluaciones consecutivas con pending_skip
    last_eval_ts: float = 0.0
    opportunity_count: int = 0      # oportunidades vistas en régimen actual
    trade_results: deque = field(default_factory=lambda: deque(maxlen=300))  # (ts, is_timeout)
    spike_ts_buf: deque = field(default_factory=lambda: deque(maxlen=300))   # timestamps de spikes


class RegimeSkipFilter:
    """
    Filtra ghost ALLOWs por régimen de mercado por símbolo.
    Thread-safe (GIL protege las operaciones de dict/deque, que son atómicas en CPython).
    """

    def __init__(self) -> None:
        self._syms: dict[str, _SymbolState] = defaultdict(_SymbolState)
        _LOGGER.info(
            "[D67_INIT] enabled=%s | eval=%ds window=%ds hysteresis=%d"
            " | BUENO(t<%.0f%% sp/h>=%.1f) MEDIOCRE(t<%.0f%% sp/h>=%.1f) DIFÍCIL(t<%.0f%%)",
            _ENABLED,
            _EVAL_INTERVAL_S, _WINDOW_S, _HYSTERESIS,
            _BUENO_TIMEOUT_MAX * 100, _BUENO_SPIKES_MIN,
            _MEDIOCRE_TIMEOUT_MAX * 100, _MEDIOCRE_SPIKES_MIN,
            _DIFICIL_TIMEOUT_MAX * 100,
        )

    # ─── Registro de eventos ──────────────────────────────────────────────────

    def record_trade_closed(self, symbol: str, exit_reason: str, closed_ts: float) -> None:
        """Llamar cuando cierra un trade. exit_reason='max_hold_timeout' → timeout."""
        if not _ENABLED:
            return
        is_timeout = exit_reason == "max_hold_timeout"
        self._syms[symbol].trade_results.append((closed_ts or time.time(), is_timeout))

    def record_spike(self, symbol: str, spike_ts: float) -> None:
        """Registrar spike detectado. Por diseño del símbolo todos son alineados."""
        if not _ENABLED:
            return
        st = self._syms[symbol]
        # Idempotente: solo agregar si es más nuevo que el último registrado
        if st.spike_ts_buf and st.spike_ts_buf[-1] >= spike_ts:
            return
        st.spike_ts_buf.append(spike_ts)

    # ─── Decisión de filtro ───────────────────────────────────────────────────

    def should_skip(self, symbol: str) -> bool:
        """
        True  → saltar esta oportunidad ghost (régimen desfavorable).
        False → permitir el ghost ALLOW.
        """
        if not _ENABLED:
            return False

        st = self._syms[symbol]
        self._maybe_eval(symbol, st)

        skip = st.current_skip
        if skip == 0:
            return False  # BUENO: siempre permitir

        # Incrementar contador y decidir si esta oportunidad se activa (1 de skip+1)
        st.opportunity_count += 1
        fire = (st.opportunity_count % (skip + 1)) == 1
        if not fire:
            _LOGGER.info(
                "[D67_SKIP] %s | regime=%s skip=%d oportunidad=%d → SALTADA",
                symbol, REGIME_NAMES.get(skip, "?"), skip, st.opportunity_count,
            )
        return not fire

    # ─── Evaluación de régimen ────────────────────────────────────────────────

    def _maybe_eval(self, symbol: str, st: _SymbolState) -> None:
        now = time.time()
        if now - st.last_eval_ts < _EVAL_INTERVAL_S:
            return
        st.last_eval_ts = now

        cutoff = now - _WINDOW_S

        recent_trades = [(ts, is_to) for ts, is_to in st.trade_results if ts >= cutoff]
        if recent_trades:
            timeouts = sum(1 for _, is_to in recent_trades if is_to)
            timeout_pct = timeouts / len(recent_trades)
        else:
            timeout_pct = 0.0

        recent_spikes = [ts for ts in st.spike_ts_buf if ts >= cutoff]
        window_h = _WINDOW_S / 3600.0
        spikes_per_h = len(recent_spikes) / window_h if window_h > 0 else 0.0

        new_skip = _classify_regime(timeout_pct, spikes_per_h)

        if new_skip == st.pending_skip:
            st.pending_count += 1
        else:
            st.pending_skip = new_skip
            st.pending_count = 1

        if st.pending_count >= _HYSTERESIS and new_skip != st.current_skip:
            old = REGIME_NAMES.get(st.current_skip, "?")
            new = REGIME_NAMES.get(new_skip, "?")
            st.current_skip = new_skip
            st.pending_count = 0
            st.opportunity_count = 0  # reset al cambiar régimen
            _LOGGER.info(
                "[D67_REGIME_CHANGE] %s | %s→%s (skip=%d)"
                " | timeout_pct=%.1f%% spikes/h=%.1f trades=%d",
                symbol, old, new, new_skip,
                timeout_pct * 100, spikes_per_h, len(recent_trades),
            )
            self.persist_state()
        else:
            _LOGGER.debug(
                "[D67_EVAL] %s | regime=%s | timeout_pct=%.1f%% spikes/h=%.1f"
                " trades=%d | pending=%s x%d",
                symbol, REGIME_NAMES.get(st.current_skip, "?"),
                timeout_pct * 100, spikes_per_h, len(recent_trades),
                REGIME_NAMES.get(st.pending_skip, "?"), st.pending_count,
            )

    # ─── Persistencia y utilidades ────────────────────────────────────────────

    def persist_state(self) -> None:
        """Escribir estado al JSON para panel/debug."""
        if not _ENABLED:
            return
        try:
            now = time.time()
            payload: dict = {}
            for sym, st in self._syms.items():
                payload[sym] = {
                    "regime": REGIME_NAMES.get(st.current_skip, "?"),
                    "skip": st.current_skip,
                    "opportunity_count": st.opportunity_count,
                    "pending_regime": REGIME_NAMES.get(st.pending_skip, "?"),
                    "pending_count": st.pending_count,
                    "last_eval_ts": round(st.last_eval_ts, 3),
                }
            Path(_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"updated_at": now, "symbols": payload}, f, ensure_ascii=False)
        except Exception as exc:
            _LOGGER.warning("[D67_PERSIST_ERR] %s", exc)

    def force_clear(self, symbol: Optional[str] = None) -> None:
        """Limpiar estado. symbol=None limpia todos los símbolos."""
        if symbol:
            self._syms.pop(symbol, None)
            _LOGGER.info("[D67_FORCE_CLEAR] %s", symbol)
        else:
            self._syms.clear()
            _LOGGER.info("[D67_FORCE_CLEAR_ALL]")
        self.persist_state()

    def get_state(self) -> dict:
        """Estado actual para API/panel."""
        return {
            sym: {
                "regime": REGIME_NAMES.get(st.current_skip, "?"),
                "skip": st.current_skip,
                "opportunity_count": st.opportunity_count,
            }
            for sym, st in self._syms.items()
        }


# Singleton global
REGIME_SKIP_FILTER = RegimeSkipFilter()
