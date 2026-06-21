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

_STATE_DIR    = os.getenv("DERIV_STATE_DIR") or os.getenv("BOT_STATE_DIR") or "/data/logs"
_STATE_FILE   = os.path.join(_STATE_DIR, "d67_regime_state.json")
_CLOSED_FILE  = os.path.join(_STATE_DIR, "deriv_closed_contracts.json")

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

# D.6.9 — Silence ratio: tiempo desde último spike / p50 gap típico
_D69_SILENCE_FALLBACK_S     = int(os.getenv("DERIV_D69_SILENCE_FALLBACK_SEC",    "300") or 300)
_D69_SILENCE_MEDIOCRE_RATIO = float(os.getenv("DERIV_D69_SILENCE_MEDIOCRE_RATIO", "1.0") or 1.0)
_D69_SILENCE_DIFICIL_RATIO  = float(os.getenv("DERIV_D69_SILENCE_DIFICIL_RATIO",  "1.5") or 1.5)
_D69_SILENCE_CRITICO_RATIO  = float(os.getenv("DERIV_D69_SILENCE_CRITICO_RATIO",  "2.0") or 2.0)

REGIME_NAMES = {0: "BUENO", 1: "MEDIOCRE", 2: "DIFÍCIL", 3: "CRÍTICO"}


def _classify_regime(timeout_pct: float, spikes_per_h: float, silence_ratio: float = 0.0) -> int:
    """
    Retorna skip: 0=BUENO, 1=MEDIOCRE, 2=DIFÍCIL, 3=CRÍTICO.
    D.6.9: lógica OR — el peor criterio gana. silence_ratio es métrica reactiva.
    """
    # CRÍTICO si CUALQUIERA falla:
    if (timeout_pct >= _DIFICIL_TIMEOUT_MAX
            or spikes_per_h < _MEDIOCRE_SPIKES_MIN
            or silence_ratio >= _D69_SILENCE_CRITICO_RATIO):
        return 3
    # DIFÍCIL si CUALQUIERA falla:
    if (timeout_pct >= _MEDIOCRE_TIMEOUT_MAX
            or spikes_per_h < _BUENO_SPIKES_MIN
            or silence_ratio >= _D69_SILENCE_DIFICIL_RATIO):
        return 2
    # MEDIOCRE si CUALQUIERA falla:
    if (timeout_pct >= _BUENO_TIMEOUT_MAX
            or silence_ratio >= _D69_SILENCE_MEDIOCRE_RATIO):
        return 1
    return 0


def _calc_silence_ratio(spike_ts_buf: deque) -> tuple:
    """
    Calcula (current_silence_s, typical_gap_s, silence_ratio) desde buffer de spikes.
    Usa p50 de gaps entre spikes consecutivos. Requiere >=5 spikes para p50 real.
    """
    if not spike_ts_buf:
        return 0.0, float(_D69_SILENCE_FALLBACK_S), 0.0

    now = time.time()
    current_silence = max(0.0, now - spike_ts_buf[-1])

    if len(spike_ts_buf) >= 5:
        sorted_ts = sorted(spike_ts_buf)
        gaps = sorted([sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))])
        typical_gap = gaps[len(gaps) // 2]  # p50
    else:
        typical_gap = float(_D69_SILENCE_FALLBACK_S)

    if typical_gap <= 0:
        return current_silence, typical_gap, 0.0

    return current_silence, typical_gap, current_silence / typical_gap


@dataclass
class _SymbolState:
    current_skip: int = 0           # skip count activo (post-hysteresis)
    pending_skip: int = 0           # candidato de cambio
    pending_count: int = 0          # evaluaciones consecutivas con pending_skip
    last_eval_ts: float = 0.0
    opportunity_count: int = 0      # oportunidades vistas en régimen actual
    trade_results: deque = field(default_factory=lambda: deque(maxlen=300))  # (ts, is_timeout)
    spike_ts_buf: deque = field(default_factory=lambda: deque(maxlen=300))   # timestamps de spikes
    # D.6.9 — último cálculo de silence_ratio (para panel y debug)
    last_silence_ratio: float = 0.0
    last_current_silence_s: float = 0.0
    last_typical_gap_s: float = 0.0


class RegimeSkipFilter:
    """
    Filtra ghost ALLOWs por régimen de mercado por símbolo.
    Thread-safe (GIL protege las operaciones de dict/deque, que son atómicas en CPython).
    """

    def __init__(self) -> None:
        self._syms: dict[str, _SymbolState] = defaultdict(_SymbolState)
        _LOGGER.info(
            "[D67_INIT] enabled=%s | eval=%ds window=%ds hysteresis=%d"
            " | tout: CRIT>=%.0f%% DIF>=%.0f%% MED>=%.0f%%"
            " | spikes: CRIT<%.1f DIF<%.1f"
            " | [D69] silence_fallback=%ds CRIT>=%.1f DIF>=%.1f MED>=%.1f",
            _ENABLED,
            _EVAL_INTERVAL_S, _WINDOW_S, _HYSTERESIS,
            _DIFICIL_TIMEOUT_MAX * 100, _MEDIOCRE_TIMEOUT_MAX * 100, _BUENO_TIMEOUT_MAX * 100,
            _MEDIOCRE_SPIKES_MIN, _BUENO_SPIKES_MIN,
            _D69_SILENCE_FALLBACK_S,
            _D69_SILENCE_CRITICO_RATIO, _D69_SILENCE_DIFICIL_RATIO, _D69_SILENCE_MEDIOCRE_RATIO,
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
        if not recent_trades:
            # Sin trades en la ventana → no hay suficiente datos para clasificar.
            # Mantener régimen actual (default BUENO en startup, conservador).
            _LOGGER.debug("[D67_EVAL] %s | sin trades en ventana %ds → régimen sin cambio", symbol, _WINDOW_S)
            return

        timeouts = sum(1 for _, is_to in recent_trades if is_to)
        timeout_pct = timeouts / len(recent_trades)

        recent_spikes = [ts for ts in st.spike_ts_buf if ts >= cutoff]
        window_h = _WINDOW_S / 3600.0
        spikes_per_h = len(recent_spikes) / window_h if window_h > 0 else 0.0

        # D.6.9 — silence_ratio: métrica reactiva sobre silencio actual
        cur_sil, typ_gap, silence_ratio = _calc_silence_ratio(st.spike_ts_buf)
        st.last_silence_ratio = silence_ratio
        st.last_current_silence_s = cur_sil
        st.last_typical_gap_s = typ_gap
        _LOGGER.info(
            "[D69_SILENCE_RATIO] %s silence=%.0fs typical=%.0fs ratio=%.2f n_spikes=%d",
            symbol, cur_sil, typ_gap, silence_ratio, len(st.spike_ts_buf),
        )

        new_skip = _classify_regime(timeout_pct, spikes_per_h, silence_ratio)

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
                " | timeout_pct=%.1f%% spikes/h=%.1f silence_ratio=%.2f trades=%d",
                symbol, old, new, new_skip,
                timeout_pct * 100, spikes_per_h, silence_ratio, len(recent_trades),
            )
            self.persist_state()
        else:
            _LOGGER.debug(
                "[D67_EVAL] %s | regime=%s | timeout_pct=%.1f%% spikes/h=%.1f"
                " silence_ratio=%.2f trades=%d | pending=%s x%d",
                symbol, REGIME_NAMES.get(st.current_skip, "?"),
                timeout_pct * 100, spikes_per_h, silence_ratio, len(recent_trades),
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
                    # D.6.9
                    "silence_ratio": round(st.last_silence_ratio, 2),
                    "current_silence_s": round(st.last_current_silence_s, 0),
                    "typical_gap_s": round(st.last_typical_gap_s, 0),
                }
            Path(_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"updated_at": now, "symbols": payload}, f, ensure_ascii=False)
        except Exception as exc:
            _LOGGER.warning("[D67_PERSIST_ERR] %s", exc)

    def force_clear(self, symbol: Optional[str] = None) -> None:
        """Limpiar estado y recargar historial del JSON de trades."""
        if symbol:
            self._syms.pop(symbol, None)
            _LOGGER.info("[D67_FORCE_CLEAR] %s", symbol)
        else:
            self._syms.clear()
            _LOGGER.info("[D67_FORCE_CLEAR_ALL]")
        self._preload_from_history()
        self.persist_state()

    def _preload_from_history(self) -> None:
        """
        Precarga trades cerrados del JSON histórico en el buffer en memoria.
        Solo carga los de la ventana activa (_WINDOW_S). Llamado en startup.
        """
        if not _ENABLED:
            return
        try:
            raw = Path(_CLOSED_FILE).read_text(encoding="utf-8")
            trades = json.loads(raw)
            if not isinstance(trades, list):
                return
            cutoff = time.time() - _WINDOW_S
            loaded = 0
            for rec in trades:
                ts = float(rec.get("closed_at_ts") or 0)
                if ts < cutoff:
                    continue
                sym = str(rec.get("symbol") or "")
                if not sym:
                    continue
                is_timeout = rec.get("exit_reason") == "max_hold_timeout"
                self._syms[sym].trade_results.append((ts, is_timeout))
                loaded += 1
            _LOGGER.info(
                "[D67_PRELOAD] %d trades cargados en ventana %ds desde %s",
                loaded, _WINDOW_S, _CLOSED_FILE,
            )
        except Exception as exc:
            _LOGGER.warning("[D67_PRELOAD_ERR] %s", exc)

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
