"""
D.7.0 — Regime Detector v2 (Burst + Performance + Frequency).

Reemplazo completo de D.6.7 (regime_skip_filter) + D.6.9 (silence_ratio).

Filosofía Diego 2026-06-21:
- Detectar distribución temporal real (burst/uniform/dead)
- Incluir performance (WR, PnL, racha perdedora)
- Operar a nivel PENDING INTENT, no tick GHOST_ALLOW
- 4 estados reales con acción clara

Bugs D.6.7 corregidos:
1. Skip se contaba por tick → ahora por pending intent
2. timeout_pct con muestras chicas → ahora WR_last_5
3. Solo BUENO/CRÍTICO → ahora 4 estados reales con histéresis asimétrica
4. No miraba performance real → ahora WR + PnL + consecutive_losses
"""

import json
import logging
import math
import os
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_LOGGER = logging.getLogger("deriv.daemon")

STATE_FILE = "/data/logs/d70_regime_state.json"


@dataclass
class TradeResult:
    """Trade cerrado para tracking performance."""
    ts: float
    pnl: float
    is_win: bool
    is_timeout: bool


@dataclass
class SpikeRecord:
    """Spike registrado."""
    ts: float
    direction: str
    ratio: float
    aligned_with_bot: bool


@dataclass
class SymbolRegimeState:
    """Estado de régimen por símbolo."""
    current_regime: str = "MEDIOCRE"  # Inicial seguro (no BUENO false-positive)
    skip_rate: int = 1

    # Counter de PENDING INTENTS (no ALLOWs ticks)
    pending_intent_counter: int = 0

    # Última evaluación
    last_eval_ts: float = 0.0

    # Histéresis: candidato mejor
    candidate_better: str = "MEDIOCRE"
    streak_better: int = 0  # Necesita 2 para confirmar mejora

    # Métricas última eval (para debug/panel)
    last_timing_state: str = "UNIFORM_NORMAL"
    last_performance_state: str = "MEDIOCRE"
    last_frequency_state: str = "MEDIOCRE"

    last_gap_cv: float = 0.0
    last_gap_p25_s: float = 0.0
    last_gap_p50_s: float = 0.0
    last_gap_p75_s: float = 0.0
    last_current_silence_s: float = 0.0

    last_wr_5: float = 0.0
    last_pnl_2h: float = 0.0
    last_consecutive_losses: int = 0
    last_aligned_per_h: float = 0.0


class RegimeDetectorV2:
    """
    Detector de régimen v2 — 3 dimensiones, 4 estados, a nivel pending.

    Env vars principales:
        DERIV_D70_ENABLED (default: true)
        DERIV_D70_EVAL_INTERVAL_SEC (default: 300 = 5 min)
        DERIV_D70_MIN_SPIKES_FOR_TIMING (default: 5)
        DERIV_D70_MIN_TRADES_FOR_PERF (default: 3)
    """

    REGIME_SKIP_RATE = {
        "BUENO": 0,
        "MEDIOCRE": 1,
        "DIFICIL": 2,
        "CRITICO": 3,
    }

    REGIME_SEVERITY = {
        "BUENO": 0,
        "MEDIOCRE": 1,
        "DIFICIL": 2,
        "CRITICO": 3,
    }

    def __init__(self) -> None:
        self._enabled = os.getenv("DERIV_D70_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
        self._eval_interval = int(os.getenv("DERIV_D70_EVAL_INTERVAL_SEC", "300") or 300)
        self._min_spikes_timing = int(os.getenv("DERIV_D70_MIN_SPIKES_FOR_TIMING", "5") or 5)
        self._min_trades_perf = int(os.getenv("DERIV_D70_MIN_TRADES_FOR_PERF", "3") or 3)

        # Buffers por símbolo
        self._spikes_buffer: dict = defaultdict(lambda: deque(maxlen=300))
        self._trades_buffer: dict = defaultdict(lambda: deque(maxlen=50))

        # Estados por símbolo (inicial MEDIOCRE = seguro)
        self._states: dict = defaultdict(lambda: SymbolRegimeState())

        _LOGGER.info(
            "[D70_INIT] enabled=%s eval=%ds min_spikes=%d min_trades=%d",
            self._enabled, self._eval_interval,
            self._min_spikes_timing, self._min_trades_perf,
        )

    def is_enabled(self) -> bool:
        return self._enabled

    def record_trade_closed(self, symbol: str, closed_ts: float,
                            pnl: float, is_timeout: bool) -> None:
        """Registrar trade cerrado para performance tracking."""
        if not self._enabled:
            return
        try:
            self._trades_buffer[symbol].append(
                TradeResult(ts=closed_ts, pnl=pnl, is_win=(pnl > 0), is_timeout=is_timeout)
            )
        except Exception as exc:
            _LOGGER.warning("[D70_TRADE_ERR] %s: %s", symbol, exc)

    def record_spike(self, symbol: str, spike_ts: float, direction: str,
                     ratio: float, aligned_with_open_position: bool) -> None:
        """Registrar spike."""
        if not self._enabled:
            return
        try:
            buf = self._spikes_buffer[symbol]
            # Idempotente: solo agregar si es más nuevo que el último
            if buf and buf[-1].ts >= spike_ts:
                return
            buf.append(
                SpikeRecord(ts=spike_ts, direction=direction, ratio=ratio,
                            aligned_with_bot=aligned_with_open_position)
            )
        except Exception as exc:
            _LOGGER.warning("[D70_SPIKE_ERR] %s: %s", symbol, exc)

    # ============================================================
    # DIMENSION 1: TIMING
    # ============================================================

    def _classify_timing(self, symbol: str) -> Tuple[str, dict]:
        """Clasificar timing por distribución temporal de spikes alineados."""
        now = time.time()
        aligned = [s for s in self._spikes_buffer[symbol] if s.aligned_with_bot]

        # Tomar últimos 10 spikes alineados
        aligned = sorted(aligned, key=lambda x: x.ts)[-10:]

        info: dict = {
            "state": "UNIFORM_NORMAL",
            "gap_cv": 0.0,
            "gap_p25_s": 0.0,
            "gap_p50_s": 0.0,
            "gap_p75_s": 0.0,
            "gap_mean_s": 0.0,
            "current_silence_s": 0.0,
            "n_spikes": len(aligned),
        }

        # Sin datos suficientes
        if len(aligned) < self._min_spikes_timing:
            if aligned:
                current_silence = now - aligned[-1].ts
                info["current_silence_s"] = current_silence
                if current_silence > 1800:  # 30 min sin spike
                    info["state"] = "DEAD"
            return info["state"], info

        # Calcular gaps
        gaps = [aligned[i].ts - aligned[i - 1].ts for i in range(1, len(aligned))]
        gaps_sorted = sorted(gaps)
        n = len(gaps_sorted)

        gap_p25 = gaps_sorted[n // 4]
        gap_p50 = gaps_sorted[n // 2]
        gap_p75 = gaps_sorted[3 * n // 4]
        gap_mean = statistics.mean(gaps)
        gap_std = statistics.stdev(gaps) if len(gaps) > 1 else 0.0
        gap_cv = gap_std / gap_mean if gap_mean > 0 else 0.0

        current_silence = now - aligned[-1].ts

        info.update({
            "gap_cv": gap_cv,
            "gap_p25_s": gap_p25,
            "gap_p50_s": gap_p50,
            "gap_p75_s": gap_p75,
            "gap_mean_s": gap_mean,
            "current_silence_s": current_silence,
        })

        # DEAD: silencio extremo
        if current_silence > gap_p75 * 3:
            info["state"] = "DEAD"
            return "DEAD", info

        # IN_BURST: silence corto + alta variabilidad
        if current_silence < gap_p25 and gap_cv > 0.7:
            info["state"] = "IN_BURST"
            return "IN_BURST", info

        # POST_BURST_DEEP: silence > 1.5x p75 + variabilidad
        if current_silence > gap_p75 * 1.5 and gap_cv > 0.7:
            info["state"] = "POST_BURST_DEEP"
            return "POST_BURST_DEEP", info

        # POST_BURST_EARLY: silence > p50 + variabilidad
        if current_silence > gap_p50 and gap_cv > 0.7:
            info["state"] = "POST_BURST_EARLY"
            return "POST_BURST_EARLY", info

        # UNIFORM clasificación
        if gap_cv < 0.5:
            if gap_mean < 360:   # < 6 min
                info["state"] = "UNIFORM_FAST"
            elif gap_mean < 600:  # < 10 min
                info["state"] = "UNIFORM_NORMAL"
            else:
                info["state"] = "UNIFORM_SLOW"
        else:
            info["state"] = "UNIFORM_NORMAL"  # default conservador

        return info["state"], info

    def _timing_to_regime(self, timing_state: str) -> str:
        """Mapear timing state a régimen."""
        mapping = {
            "IN_BURST": "BUENO",
            "UNIFORM_FAST": "BUENO",
            "UNIFORM_NORMAL": "MEDIOCRE",
            "UNIFORM_SLOW": "DIFICIL",
            "POST_BURST_EARLY": "DIFICIL",
            "POST_BURST_DEEP": "CRITICO",
            "DEAD": "CRITICO",
        }
        return mapping.get(timing_state, "MEDIOCRE")

    # ============================================================
    # DIMENSION 2: PERFORMANCE
    # ============================================================

    def _classify_performance(self, symbol: str) -> Tuple[str, dict]:
        """Clasificar performance por resultado real reciente."""
        now = time.time()
        trades = list(self._trades_buffer[symbol])

        info: dict = {
            "state": "MEDIOCRE",
            "wr_5": 0.0,
            "pnl_2h": 0.0,
            "consecutive_losses": 0,
            "n_trades_5": 0,
            "n_trades_2h": 0,
        }

        # Sin datos suficientes
        if len(trades) < self._min_trades_perf:
            return "MEDIOCRE", info

        # Últimos 5 trades
        last_5 = trades[-5:]
        wins_5 = sum(1 for t in last_5 if t.is_win)
        wr_5 = (wins_5 / len(last_5)) * 100 if last_5 else 0.0

        # PnL últimas 2h
        ts_2h_ago = now - (2 * 3600)
        trades_2h = [t for t in trades if t.ts >= ts_2h_ago]
        pnl_2h = sum(t.pnl for t in trades_2h)

        # Consecutive losses (desde el más reciente)
        consecutive_losses = 0
        for t in reversed(trades):
            if t.is_win:
                break
            consecutive_losses += 1

        info.update({
            "wr_5": wr_5,
            "pnl_2h": pnl_2h,
            "consecutive_losses": consecutive_losses,
            "n_trades_5": len(last_5),
            "n_trades_2h": len(trades_2h),
        })

        # Clasificación: el PEOR criterio gana
        # CRITICO
        if consecutive_losses >= 3 and wr_5 < 20:
            return "CRITICO", {**info, "state": "CRITICO"}
        if pnl_2h <= -5.0:
            return "CRITICO", {**info, "state": "CRITICO"}

        # DIFICIL
        if consecutive_losses >= 2 and wr_5 < 30:
            return "DIFICIL", {**info, "state": "DIFICIL"}
        if pnl_2h <= -3.0:
            return "DIFICIL", {**info, "state": "DIFICIL"}
        if wr_5 < 25:
            return "DIFICIL", {**info, "state": "DIFICIL"}

        # MEDIOCRE
        if consecutive_losses == 1 and pnl_2h < 0:
            return "MEDIOCRE", {**info, "state": "MEDIOCRE"}
        if wr_5 < 40:
            return "MEDIOCRE", {**info, "state": "MEDIOCRE"}
        if pnl_2h <= -1.0:
            return "MEDIOCRE", {**info, "state": "MEDIOCRE"}

        # BUENO
        return "BUENO", {**info, "state": "BUENO"}

    # ============================================================
    # DIMENSION 3: FRECUENCIA
    # ============================================================

    def _classify_frequency(self, symbol: str) -> Tuple[str, dict]:
        """Clasificar por frecuencia de spikes alineados/hora."""
        now = time.time()
        ts_30m_ago = now - (30 * 60)
        aligned_30m = [
            s for s in self._spikes_buffer[symbol]
            if s.aligned_with_bot and s.ts >= ts_30m_ago
        ]
        aligned_per_h = len(aligned_30m) * 2  # 30min * 2 = /h

        info: dict = {"state": "MEDIOCRE", "aligned_per_h": aligned_per_h}

        if aligned_per_h < 1:
            return "CRITICO", {**info, "state": "CRITICO"}
        if aligned_per_h < 2:
            return "DIFICIL", {**info, "state": "DIFICIL"}
        if aligned_per_h < 3:
            return "MEDIOCRE", {**info, "state": "MEDIOCRE"}
        return "BUENO", {**info, "state": "BUENO"}

    # ============================================================
    # EVALUACIÓN COMBINADA
    # ============================================================

    def _evaluate_symbol(self, symbol: str) -> None:
        """Evaluar régimen combinando 3 dimensiones."""
        state = self._states[symbol]
        now = time.time()

        # 3 dimensiones
        timing_regime, timing_info = self._classify_timing(symbol)
        timing_regime_mapped = self._timing_to_regime(timing_regime)

        perf_regime, perf_info = self._classify_performance(symbol)
        freq_regime, freq_info = self._classify_frequency(symbol)

        # Guardar métricas en estado
        state.last_timing_state = timing_regime
        state.last_performance_state = perf_regime
        state.last_frequency_state = freq_regime
        state.last_gap_cv = timing_info["gap_cv"]
        state.last_gap_p25_s = timing_info["gap_p25_s"]
        state.last_gap_p50_s = timing_info["gap_p50_s"]
        state.last_gap_p75_s = timing_info["gap_p75_s"]
        state.last_current_silence_s = timing_info["current_silence_s"]
        state.last_wr_5 = perf_info["wr_5"]
        state.last_pnl_2h = perf_info["pnl_2h"]
        state.last_consecutive_losses = perf_info["consecutive_losses"]
        state.last_aligned_per_h = freq_info["aligned_per_h"]

        # Combinar: el PEOR manda
        regimes = [timing_regime_mapped, perf_regime, freq_regime]
        max_sev = max(self.REGIME_SEVERITY[r] for r in regimes)
        new_regime = next(k for k, v in self.REGIME_SEVERITY.items() if v == max_sev)

        # Histéresis ASIMÉTRICA
        old_regime = state.current_regime
        old_sev = self.REGIME_SEVERITY[old_regime]
        new_sev = max_sev

        if new_sev > old_sev:
            # Peor → cambiar inmediato (reactivo)
            state.current_regime = new_regime
            state.skip_rate = self.REGIME_SKIP_RATE[new_regime]
            state.streak_better = 0
            _LOGGER.info(
                "[D70_CHANGE] %s %s→%s (WORSE) | timing=%s perf=%s freq=%s | "
                "wr5=%.0f%% pnl2h=$%.2f loss_streak=%d aligned=%.1f/h cv=%.2f",
                symbol, old_regime, new_regime, timing_regime, perf_regime, freq_regime,
                perf_info["wr_5"], perf_info["pnl_2h"], perf_info["consecutive_losses"],
                freq_info["aligned_per_h"], timing_info["gap_cv"],
            )
        elif new_sev < old_sev:
            # Mejor → necesita 2 evaluaciones consecutivas
            if state.candidate_better == new_regime:
                state.streak_better += 1
            else:
                state.candidate_better = new_regime
                state.streak_better = 1

            if state.streak_better >= 2:
                state.current_regime = new_regime
                state.skip_rate = self.REGIME_SKIP_RATE[new_regime]
                state.streak_better = 0
                _LOGGER.info(
                    "[D70_CHANGE] %s %s→%s (BETTER confirmed) | timing=%s perf=%s freq=%s",
                    symbol, old_regime, new_regime, timing_regime, perf_regime, freq_regime,
                )
            else:
                _LOGGER.info(
                    "[D70_PENDING] %s candidate=%s streak=%d (need 2)",
                    symbol, new_regime, state.streak_better,
                )
        else:
            state.streak_better = 0

        state.last_eval_ts = now

        _LOGGER.info(
            "[D70_EVAL] %s regime=%s skip=%d | T=%s P=%s F=%s | "
            "wr5=%.0f%% pnl2h=$%.2f loss=%d aligned=%.1f/h silence=%.0fs cv=%.2f",
            symbol, state.current_regime, state.skip_rate,
            timing_regime, perf_regime, freq_regime,
            perf_info["wr_5"], perf_info["pnl_2h"], perf_info["consecutive_losses"],
            freq_info["aligned_per_h"], timing_info["current_silence_s"], timing_info["gap_cv"],
        )

    # ============================================================
    # D.7.1: PENDING EXTENSION — reemplaza should_skip como mecanismo activo
    # ============================================================

    REGIME_PENDING_EXTENSION: Dict[str, int] = {
        "BUENO":    0,    # sin extensión (pending base normal)
        "MEDIOCRE": 120,  # +2 min
        "DIFICIL":  240,  # +4 min
        "CRITICO":  360,  # +6 min
    }

    def get_pending_extension(self, symbol: str) -> Tuple[int, dict]:
        """
        D.7.1 — Retornar segundos a SUMAR al pending wait base del símbolo.

        BUENO:    +0s   (pending base normal)
        MEDIOCRE: +120s (+2 min)
        DIFICIL:  +240s (+4 min)
        CRITICO:  +360s (+6 min)

        Evalúa régimen si toca (cada eval_interval).
        Returns: (segundos_extra, info_dict)
        """
        if not self._enabled:
            return 0, {"regime": "DISABLED", "extension_s": 0}

        state = self._states[symbol]
        now = time.time()

        if (now - state.last_eval_ts) >= self._eval_interval:
            try:
                self._evaluate_symbol(symbol)
            except Exception as exc:
                _LOGGER.warning("[D71_EVAL_ERR] %s: %s", symbol, exc)

        extension_s = self.REGIME_PENDING_EXTENSION.get(state.current_regime, 0)

        info = {
            "regime": state.current_regime,
            "extension_s": extension_s,
            "timing": state.last_timing_state,
            "performance": state.last_performance_state,
            "wr_5": state.last_wr_5,
            "pnl_2h": state.last_pnl_2h,
            "consecutive_losses": state.last_consecutive_losses,
            "aligned_per_h": state.last_aligned_per_h,
        }

        return extension_s, info

    def should_skip(self, symbol: str) -> Tuple[bool, dict]:
        """OBSOLETO en D.7.1 — mantenido por compatibilidad, retorna siempre False."""
        _, info = self.get_pending_extension(symbol)
        return False, info

    def get_state_snapshot(self) -> dict:
        """Snapshot para panel/debug."""
        return {
            sym: {
                "regime": state.current_regime,
                "skip_rate": state.skip_rate,
                "pending_intent_counter": state.pending_intent_counter,
                "timing_state": state.last_timing_state,
                "performance_state": state.last_performance_state,
                "frequency_state": state.last_frequency_state,
                "gap_cv": round(state.last_gap_cv, 2),
                "gap_p25_s": round(state.last_gap_p25_s, 0),
                "gap_p50_s": round(state.last_gap_p50_s, 0),
                "gap_p75_s": round(state.last_gap_p75_s, 0),
                "current_silence_s": round(state.last_current_silence_s, 0),
                "wr_5": round(state.last_wr_5, 0),
                "pnl_2h": round(state.last_pnl_2h, 2),
                "consecutive_losses": state.last_consecutive_losses,
                "aligned_per_h": round(state.last_aligned_per_h, 1),
                "last_eval_ts": state.last_eval_ts,
            }
            for sym, state in self._states.items()
        }

    def persist_state(self) -> None:
        """Guardar snapshot al JSON para panel/debug."""
        if not self._enabled:
            return
        try:
            snapshot = {
                "updated_at": time.time(),
                "symbols": self.get_state_snapshot(),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)
        except Exception as exc:
            _LOGGER.warning("[D70_PERSIST_ERR] %s", exc)

    def force_clear(self) -> None:
        """Limpieza de estado en startup."""
        self._states.clear()
        self._spikes_buffer.clear()
        self._trades_buffer.clear()
        _LOGGER.info("[D70_FORCE_CLEAR_ALL]")

    def _preload_from_json(self) -> None:
        """D.7.4: Precarga buffers desde JSON en disco al startup para evitar WR5=0%.

        Lee los últimos 4h de spikes y 2h de trades desde los archivos live.
        Operación best-effort: cualquier error es silencioso.
        """
        import os
        base = os.path.dirname(STATE_FILE)
        cutoff_trades = time.time() - 7200   # 2h para WR5 y pnl_2h
        cutoff_spikes = time.time() - 14400  # 4h para timing

        # Preload trades
        trades_path = os.path.join(base, "deriv_closed_contracts.json")
        if os.path.exists(trades_path):
            try:
                with open(trades_path, "r", encoding="utf-8") as _f:
                    _all = json.load(_f)
                _loaded = 0
                for _t in _all:
                    _ts = float(_t.get("closed_at_ts") or 0)
                    if _ts < cutoff_trades:
                        continue
                    _sym = str(_t.get("symbol") or "").upper()
                    _pnl = float(_t.get("realized_pnl_usdt") or 0)
                    _reason = str(_t.get("exit_reason") or _t.get("closed_by") or "")
                    _is_timeout = "timeout" in _reason.lower() or "max_hold" in _reason.lower()
                    self.record_trade_closed(_sym, _ts, _pnl, _is_timeout)
                    _loaded += 1
                _LOGGER.info("[D74_PRELOAD] trades=%d (last 2h) from %s", _loaded, trades_path)
            except Exception as _e:
                _LOGGER.debug("[D74_PRELOAD] trades error: %s", _e)

        # Preload spikes
        spikes_path = os.path.join(base, "deriv_spike_events.json")
        if os.path.exists(spikes_path):
            try:
                with open(spikes_path, "r", encoding="utf-8") as _f:
                    _all = json.load(_f)
                _loaded = 0
                for _s in _all:
                    _ts = float(_s.get("ts") or 0)
                    if _ts < cutoff_spikes:
                        continue
                    _sym = str(_s.get("symbol") or "").upper()
                    _dir = str(_s.get("direction") or "UP")
                    _ratio = float(_s.get("ratio") or 0)
                    _had_pos = bool(_s.get("had_open_pos") or False)
                    self.record_spike(_sym, _ts, _dir, _ratio, _had_pos)
                    _loaded += 1
                _LOGGER.info("[D74_PRELOAD] spikes=%d (last 4h) from %s", _loaded, spikes_path)
            except Exception as _e:
                _LOGGER.debug("[D74_PRELOAD] spikes error: %s", _e)


# Singleton global
REGIME_DETECTOR_V2 = RegimeDetectorV2()
# D.7.4: precarga buffers desde disco para evitar WR5=0% post-restart
REGIME_DETECTOR_V2._preload_from_json()
