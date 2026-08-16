"""
entrada_diego.py — Segunda línea de apertura autónoma.

CRASH1000 / BOOM1000 / CRASH900 — ciclo K1000:
  WAIT → IN_CONTRACT (20min) → SPIKE_HOLD (4min) → WAIT
  Con spike durante contrato: cierra en SPIKE_HOLD. Sin spike: cierra en timer.
  Escalada: $10 → $20 (solo CRASH1000). CRASH900/BOOM1000: siempre $10 (no escalan).
  Win grande (≥$5): REST 2h con liberación anticipada por 3 spikes buenos.

Gates de fase (basados en 1068 trades post-reset 2026-08-09):
  CRASH900: bloquea gap <380s (FRESH+BUILDING WR≈43% -$79) y DRY >1593s (WR=37%)
             Solo opera RIPE (380-800s) y OVERDUE (800-1593s) → WR=55% +$36
  CRASH1000: bloquea gap <380s (FRESH WR=29%) y OVERDUE 760-1500s (WR=25%)
             Opera RIPE (380-760s) y DRY (>1500s) → WR=49% +$32
  BOOM1000:  bloquea gap <380s (FRESH WR=45%). Opera RIPE/OVERDUE/DRY.
  BOOM900:   SUSPENDIDO — todos los zones WR<47%, rec=1 WR=30.2% -$91.

Activación: env ENTRADA_DIEGO_ENABLED=true
Estado:     {BOT_STATE_DIR}/entrada_diego_state.json
"""

import asyncio
import datetime
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("entrada_diego")

SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_900  = {"CRASH900", "BOOM900"}
SYMBOLS_K1000  = SYMBOLS_1000 | SYMBOLS_900
SYMBOLS_ED     = SYMBOLS_K1000

_STAKE_LADDER_1000 = [10.0, 20.0, 20.0, 40.0, 40.0]  # legacy stake reference

MULTIPLIER         = int(os.getenv("ENTRADA_DIEGO_MULTIPLIER",    "200"))
MAX_HOLD_S         = int(os.getenv("ENTRADA_DIEGO_MAX_HOLD_S",    "600"))
PROFIT_WAIT_S      = int(os.getenv("ENTRADA_DIEGO_PROFIT_WAIT_S", "180"))
DEEP_PAUSE_AT_1000 = int(os.getenv("ENTRADA_DIEGO_DEEP_PAUSE_AT", "8"))

REST_STAKE_1000    = float(os.getenv("ENTRADA_DIEGO_REST_STAKE_1000", "2.0"))

K1000_ENTRY_DELAY_S     = int(os.getenv("K1000_ENTRY_DELAY_S",       "480"))
K1000_ENTRY_DELAY_900_S = int(os.getenv("K1000_ENTRY_DELAY_900_S",   "480"))
K1000_OPEN_WINDOW_S     = int(os.getenv("K1000_OPEN_WINDOW_S",       "900"))
K1000_STAKE_START       = float(os.getenv("K1000_STAKE_START",       "3.0"))
K1000_STAKE_MID         = float(os.getenv("K1000_STAKE_MID",         "6.0"))
K1000_STAKE_MAX         = float(os.getenv("K1000_STAKE_MAX",         "30.0"))
K1000_CONTRACT_S        = int(os.getenv("K1000_CONTRACT_S",          "1200"))
K1000_SPIKE_CONTRACT_S  = 240
K1000_SPIKE_HOLD_S      = int(os.getenv("K1000_SPIKE_HOLD_S",        "240"))
K1000_STAKES_1000       = [10.0, 20.0]
# Aliases
K1000_SCOUT_STAKE = K1000_STAKE_START
K1000_S20_STAKE   = K1000_STAKE_MID
K1000_S40_STAKE   = K1000_STAKE_MAX
K1000_S80_STAKE   = K1000_STAKE_MAX
K1000_S200_STAKE  = K1000_STAKE_MAX
K1000_SCOUT_S     = K1000_ENTRY_DELAY_S
K1000_HOLD_S      = K1000_OPEN_WINDOW_S

POST_WIN_REST_S      = 7200.0
POST_WIN_RELEASE_SPK = 3
POST_WIN_RELEASE_RAT = 0.30

GLOBAL_PNL_TARGET  = float(os.getenv("ENTRADA_DIEGO_GLOBAL_PNL_TARGET",  "60.0"))
GLOBAL_PAUSE_HOURS = float(os.getenv("ENTRADA_DIEGO_GLOBAL_PAUSE_HOURS", "8.0"))
SYM_PNL_WIN_GATE_USD  = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_WIN_GATE",    "10.0"))
SYM_PNL_LOSS_GATE_USD = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_LOSS_GATE",   "10.0"))
SYM_PNL_WIN_PAUSE_H   = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_WIN_PAUSE_H", "12.0"))
SYM_PNL_LOSS_PAUSE_H  = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_LOSS_PAUSE_H", "3.0"))

_ED_DISABLED_RAW    = os.getenv("ENTRADA_DIEGO_DISABLED_SYMBOLS", "")
SYMBOLS_ED_DISABLED = {s.strip().upper() for s in _ED_DISABLED_RAW.split(",") if s.strip()}


# ─── State por símbolo ──────────────────────────────────────────────────────

@dataclass
class _SymState:
    phase: str = "IDLE"
    contract_id: Optional[int] = None
    open_ts: float = 0.0
    profit_positive_ts: float = 0.0
    cooldown_until: float = 0.0
    last_spike_ts: float = 0.0
    prev_spike_ts: float = 0.0
    spike_interval_s: float = 0.0
    trigger_spike_ratio: float = 0.0
    trigger_spike_jump: float = 0.0
    recent_spike_ts: list = field(default_factory=list)
    reopens: int = 0
    last_close_profit: float = 0.0
    current_profit: float = 0.0
    sym_pnl_since_reset: float = 0.0
    sym_pnl_reference: float = 0.0
    sym_pnl_pause_until: float = 0.0
    rest_mode: bool = False
    is_readjusted: bool = False
    pnl_accounted_by_floor: bool = False
    power_window: list = field(default_factory=list)
    # K1000 state machine
    k1000_phase: str = "WAIT"
    k1000_phase_ts: float = 0.0
    k1000_peak: float = 0.0
    k1000_profit_ts: float = 0.0
    k1000_spike_hold_until: float = 0.0
    k1000_cycle_stake: float = 0.0
    k1000_stake_idx: int = 0
    k1000_spike_triggered: bool = False
    k1000_blocked_until: float = 0.0
    k1000_rest_until: float = 0.0
    k1000_rest_spike_ref: float = 0.0
    k1000_rest_good_spikes: int = 0
    k1000_had_spike: bool = False
    k1000_last_closed_cid: Optional[int] = None
    k1000_win_gate_triggered: bool = False
    k1000_loss_gate_floor: float = 0.0
    hour_pnl_1000: float = 0.0
    hour_start_ts_1000: float = 0.0
    day_pnl_1000: float = 0.0
    day_start_ts_1000: float = 0.0

    def remaining_s(self, now: float) -> float:
        if self.phase == "OPEN":
            return max(0.0, K1000_CONTRACT_S - (now - self.open_ts))
        if self.phase == "COOLDOWN":
            return max(0.0, self.cooldown_until - now)
        return 0.0

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "phase":              self.phase,
            "contract_id":        self.contract_id,
            "open_ts":            round(self.open_ts, 3),
            "profit_positive_ts": round(self.profit_positive_ts, 3),
            "cooldown_until":     round(self.cooldown_until, 3),
            "last_spike_ts":      round(self.last_spike_ts, 3),
            "spike_interval_s":   round(self.spike_interval_s, 1),
            "trigger_spike_ratio":round(self.trigger_spike_ratio, 1),
            "trigger_spike_jump": round(self.trigger_spike_jump, 5),
            "reopens":            self.reopens,
            "last_close_profit":  round(self.last_close_profit, 4),
            "current_profit":     round(self.current_profit, 4),
            "remaining_s":        round(self.remaining_s(now), 1),
            "rest_mode":          self.rest_mode,
            "sym_pnl_since_reset":round(self.sym_pnl_since_reset, 4),
            "sym_pnl_reference":  round(self.sym_pnl_reference, 4),
            "sym_pnl_pause_until":round(self.sym_pnl_pause_until, 3),
            "sym_pnl_win_gate":   SYM_PNL_WIN_GATE_USD,
            "sym_pnl_loss_gate":  SYM_PNL_LOSS_GATE_USD,
            "sym_pnl_win_pause_h":SYM_PNL_WIN_PAUSE_H,
            "sym_pnl_loss_pause_h":SYM_PNL_LOSS_PAUSE_H,
            "k1000_phase":            self.k1000_phase,
            "k1000_phase_ts":         round(self.k1000_phase_ts, 3),
            "k1000_peak":             round(self.k1000_peak, 4),
            "k1000_profit_ts":        round(self.k1000_profit_ts, 3),
            "k1000_spike_hold_until": round(self.k1000_spike_hold_until, 3),
            "k1000_cycle_stake":      round(self.k1000_cycle_stake or K1000_STAKE_START, 2),
            "k1000_stake_idx":        self.k1000_stake_idx,
            "k1000_spike_triggered":      self.k1000_spike_triggered,
            "k1000_had_spike":            self.k1000_had_spike,
            "k1000_win_gate_triggered":   self.k1000_win_gate_triggered,
            "k1000_loss_gate_floor":      round(self.k1000_loss_gate_floor, 2),
            "k1000_contract_s":       K1000_CONTRACT_S,
            "k1000_spike_contract_s": K1000_SPIKE_CONTRACT_S,
            "k1000_spike_hold_s":     K1000_SPIKE_HOLD_S,
            "k1000_scout_stake":      K1000_SCOUT_STAKE,
            "k1000_s20_stake":        K1000_S20_STAKE,
            "k1000_s40_stake":        K1000_S40_STAKE,
            "k1000_s80_stake":        K1000_S80_STAKE,
            "k1000_s200_stake":       K1000_S200_STAKE,
            "k1000_scout_s":          K1000_SCOUT_S,
            "k1000_hold_s":           K1000_HOLD_S,
            "k1000_blocked_until":    round(self.k1000_blocked_until, 3),
            "hour_pnl_1000":          round(self.hour_pnl_1000, 4),
            "hour_start_ts_1000":     round(self.hour_start_ts_1000, 3),
            "day_pnl_1000":           round(self.day_pnl_1000, 4),
            "day_start_ts_1000":      round(self.day_start_ts_1000, 3),
        }


# ─── Clase principal ─────────────────────────────────────────────────────────

class EntradaDiego:

    def __init__(self, executor: Any, risk: Any, logs_dir: Path) -> None:
        self._executor = executor
        self._risk     = risk
        self._logs_dir = logs_dir
        self._state_file = logs_dir / "entrada_diego_state.json"
        _ed_raw = str(os.getenv("ENTRADA_DIEGO_ENABLED", "false")).strip().lower()
        self._enabled: bool = _ed_raw.startswith("true") or _ed_raw in {"1", "yes", "on"}

        self._states: dict[str, _SymState] = {sym: _SymState() for sym in SYMBOLS_ED}
        self._locks:  dict[str, asyncio.Lock] = {sym: asyncio.Lock() for sym in SYMBOLS_ED}
        self._open_lock    = asyncio.Lock()
        self._k1000_pending_ts:  dict[str, float] = {}
        self._k1000_spike_check: dict[str, float] = {}
        self._k1000_had_spike:   dict[str, bool]  = {}
        self._evict_in_progress: bool = False
        self._max_contracts_until: dict[str, float] = {}
        self._restore_lock = asyncio.Lock()
        self._ed_spike_hist: dict[str, list] = {sym: [] for sym in SYMBOLS_ED}
        self._ed_open_info:  dict[str, dict] = {}
        self._restored = False
        self._global_pnl: float = 0.0
        self._global_pause_until: float = 0.0
        self._global_pnl_next_target: float = GLOBAL_PNL_TARGET
        self._watchdog_started: set[str] = set()

        if self._enabled:
            active = sorted(SYMBOLS_ED - SYMBOLS_ED_DISABLED)
            paused = sorted(SYMBOLS_ED_DISABLED)
            _LOGGER.info(
                "[ENTRADA_DIEGO] ACTIVADO | activos=%s paused=%s | "
                "K1000: stakes=%s rest=$%.0f",
                active, paused, K1000_STAKES_1000, REST_STAKE_1000,
            )
        else:
            _LOGGER.info("[ENTRADA_DIEGO] inactivo (ENTRADA_DIEGO_ENABLED=false)")

    # ── API pública ──────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return self._enabled

    async def on_tick(self, tick: Any) -> None:
        if not self._enabled:
            return
        sym = str(tick.symbol).upper()
        if sym not in self._states:
            return
        if not self._restored:
            async with self._restore_lock:
                if not self._restored:
                    await self._restore_from_disk()
                    self._ed_seed_spike_hist()
                    self._restored = True
                    for _sk in SYMBOLS_K1000:
                        if _sk not in SYMBOLS_ED_DISABLED and _sk not in self._watchdog_started:
                            self._watchdog_started.add(_sk)
                            asyncio.create_task(self._1000_watchdog_loop(_sk))
        async with self._locks[sym]:
            await self._process(sym, tick)

    def get_state_snapshot(self) -> dict[str, Any]:
        now = time.time()
        result: dict[str, Any] = {
            "updated_at": now,
            "enabled": self._enabled,
            "global_pnl": round(self._global_pnl, 4),
            "global_pause_until": round(self._global_pause_until, 3),
            "global_pnl_next_target": round(self._global_pnl_next_target, 2),
        }
        for sym, st in self._states.items():
            if sym in SYMBOLS_ED_DISABLED:
                result[sym] = {"phase": "DISABLED", "reopens": 0, "current_profit": 0.0, "remaining_s": 0.0}
            else:
                result[sym] = st.to_dict(now)
                _ctx_k = self._ed_ctx(sym, now)
                result[sym]["ed_gap_s"] = round(_ctx_k["gap_s"], 1)
                _imm_k = self._risk.get_spike_imminence_state(sym)
                result[sym]["imminence_state"] = _imm_k.get("state", "UNKNOWN")
                result[sym]["imminence_score"] = round(_imm_k.get("score", 0.0), 3)
        return result

    # ── Máquina de estados ───────────────────────────────────────────────────

    async def _process(self, sym: str, tick: Any) -> None:
        if sym in SYMBOLS_ED_DISABLED:
            return
        if sym in SYMBOLS_K1000:
            await self._process_1000_scout(sym)
            return

    # ── K1000 watchdog ───────────────────────────────────────────────────────

    async def _1000_watchdog_loop(self, sym: str) -> None:
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog 1000 iniciado (intervalo 15s)", sym)
        while self._enabled and sym not in SYMBOLS_ED_DISABLED:
            await asyncio.sleep(15)
            if not self._enabled or sym in SYMBOLS_ED_DISABLED:
                break
            try:
                async with self._locks[sym]:
                    await self._process_1000_scout(sym)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s watchdog 1000 error: %s", sym, exc)
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog 1000 terminado", sym)

    async def _process_1000_scout(self, sym: str) -> None:
        await self._process_1000_simple(sym)

    # ── K1000 state machine ──────────────────────────────────────────────────

    async def _process_1000_simple(self, sym: str) -> None:
        """
        K1000 ciclo: WAIT(zona) → IN_CONTRACT(20min) → SPIKE_HOLD(4min) → WAIT(zona)

        Zonas de entrada (gap desde último spike):
          CRASH900 / BOOM900 : 380–1593s  (RIPE+OVERDUE, WR≈58% / 47%)
          CRASH1000           : 380–760s   (solo RIPE, WR≈52%)
          BOOM1000            : 380–1500s  (RIPE+OVERDUE)

        Stake $10→$20 (todos los símbolos):
          Win o loss CON spike  → $10, WAIT zona
          Loss SIN spike en $10 → $20, WAIT zona
          Loss SIN spike en $20 → $10, WAIT zona (DRY natural: espera próximo ciclo)
        """
        state = self._states[sym]
        now   = time.time()

        # Reset día UTC
        _cur_day_epoch = float(int(now // 86400) * 86400)
        if getattr(state, 'day_start_ts_1000', 0.0) < _cur_day_epoch:
            state.day_pnl_1000          = 0.0
            state.day_start_ts_1000     = _cur_day_epoch
            state.k1000_win_gate_triggered = False
            state.k1000_loss_gate_floor    = 0.0
            _LOGGER.info("[ENTRADA_DIEGO] %s K1000 nuevo día UTC → day_pnl=0 gates reset", sym)

        # Normalizar stake index
        _sidx = max(0, min(getattr(state, "k1000_stake_idx", 0), len(K1000_STAKES_1000) - 1))
        state.k1000_stake_idx  = _sidx
        _stake = K1000_STAKES_1000[_sidx]
        state.k1000_cycle_stake = _stake

        # Normalizar fase
        if state.k1000_phase not in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
            state.k1000_phase    = "WAIT"
            state.k1000_phase_ts = now

        # Detección de spike
        _last_spk_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)
        _new_spike   = _last_spk_ts > state.last_spike_ts and _last_spk_ts > 0
        if _new_spike:
            state.last_spike_ts = _last_spk_ts
            self._ed_push_spike(sym, _last_spk_ts, self._risk.get_last_spike_ratio(sym) or 0.0)

        # Recuperación post-restart: restaurar had_spike desde historial
        _phase_ts = getattr(state, 'k1000_phase_ts', 0.0)
        if _last_spk_ts > _phase_ts > 0 and not getattr(state, 'k1000_had_spike', False):
            state.k1000_had_spike = True
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s K1000 spike histórico post-restart (%.0fs en contrato) → had_spike restaurado",
                sym, _last_spk_ts - _phase_ts,
            )

        phase = state.k1000_phase

        # ────────────────────────────────────────────────────────────────────
        # WAIT: verificar gate de fase y abrir contrato
        # ────────────────────────────────────────────────────────────────────
        if phase == "WAIT":
            # ── WIN gate: primera vez que day_pnl ≥ +$10 → pausa 12h ─────
            if state.day_pnl_1000 >= SYM_PNL_WIN_GATE_USD:
                if not getattr(state, 'k1000_win_gate_triggered', False):
                    state.k1000_win_gate_triggered = True
                    state.k1000_rest_until = now + SYM_PNL_WIN_PAUSE_H * 3600.0
                    self._persist(now)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 WIN GATE day=+$%.2f ≥ +$%.0f → pausa %.0fh hasta %s UTC",
                        sym, state.day_pnl_1000, SYM_PNL_WIN_GATE_USD, SYM_PNL_WIN_PAUSE_H,
                        datetime.datetime.utcfromtimestamp(state.k1000_rest_until).strftime('%H:%M'),
                    )
                if state.k1000_rest_until > now:
                    return

            # ── LOSS gate: cada -$10 adicional acumulado → pausa 3h ──────
            _loss_floor = getattr(state, 'k1000_loss_gate_floor', 0.0)
            _next_loss_floor = _loss_floor - SYM_PNL_LOSS_GATE_USD
            if state.day_pnl_1000 <= _next_loss_floor:
                if state.k1000_blocked_until <= now:
                    state.k1000_loss_gate_floor = _next_loss_floor
                    state.k1000_blocked_until   = now + SYM_PNL_LOSS_PAUSE_H * 3600.0
                    self._persist(now)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 LOSS GATE day=-$%.2f ≤ -$%.0f → pausa %.0fh hasta %s UTC",
                        sym, abs(state.day_pnl_1000), abs(_next_loss_floor), SYM_PNL_LOSS_PAUSE_H,
                        datetime.datetime.utcfromtimestamp(state.k1000_blocked_until).strftime('%H:%M'),
                    )
                return

            # ── Pausa activa (WIN rest o LOSS block) ──────────────────────
            if state.k1000_rest_until > now or state.k1000_blocked_until > now:
                return

            _ctx_k = self._ed_ctx(sym, now)
            _gap_k = _ctx_k.get("gap_s", -1.0)

            # ── Gate de zona solo para entrada $10 (idx=0) ───────────────
            # Escalada a $20 tras loss-sin-spike: abre inmediato, sin esperar zona.
            # CRASH900 / BOOM900 : RIPE+OVERDUE → gap 380-1593s
            # CRASH1000           : solo RIPE    → gap 380-760s
            # BOOM1000            : RIPE+OVERDUE → gap 380-1500s
            if _sidx == 0:
                if sym in {"CRASH900", "BOOM900"}:
                    if _gap_k < 0 or _gap_k < 380.0 or _gap_k > 1593.0:
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s GATE zona RIPE/OVERDUE (380-1593s): gap=%.0fs → esperar",
                            sym, _gap_k,
                        )
                        return
                elif sym == "CRASH1000":
                    if _gap_k < 0 or _gap_k < 380.0 or _gap_k > 760.0:
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s GATE zona RIPE (380-760s): gap=%.0fs → esperar",
                            sym, _gap_k,
                        )
                        return
                else:  # BOOM1000
                    if _gap_k < 0 or _gap_k < 380.0 or _gap_k > 1500.0:
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s GATE zona RIPE/OVERDUE (380-1500s): gap=%.0fs → esperar",
                            sym, _gap_k,
                        )
                        return

            state.k1000_spike_triggered = False
            state.k1000_phase    = "IN_CONTRACT"
            state.k1000_phase_ts = now
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s K1000 WAIT → IN_CONTRACT $%.0f (idx=%d) inmediato",
                sym, _stake, _sidx,
            )
            await self._open_1000_simple(sym, state, now)
            return

        # ────────────────────────────────────────────────────────────────────
        # IN_CONTRACT: contrato abierto, duración 20min (4min si spike-triggered)
        # ────────────────────────────────────────────────────────────────────
        if phase == "IN_CONTRACT":
            if state.contract_id is None:
                _last_retry = self._k1000_pending_ts.get(sym, 0.0)
                if now - _last_retry < 3.0:
                    return
                self._k1000_pending_ts[sym] = now
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 IN_CONTRACT pendiente (sin espacio) → reintento $%.0f",
                    sym, _stake,
                )
                await self._open_1000_simple(sym, state, now)
                return

            _broker_closed = False
            _ic_prev_cid   = state.contract_id
            _qp = self._query_profit(state.contract_id)
            if _qp is not None:
                state.current_profit = _qp
            else:
                _broker_closed = True
                state.contract_id = None

            # Ventana post-spike: profit aún negativo → chequear 5s
            _spk_chk = self._k1000_spike_check.get(sym, 0.0)
            if _spk_chk > 0 and not _broker_closed:
                if state.current_profit > 0:
                    self._k1000_spike_check.pop(sym, None)
                    state.k1000_had_spike        = True
                    state.k1000_phase            = "SPIKE_HOLD"
                    state.k1000_spike_hold_until = now + K1000_SPIKE_HOLD_S
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 spike check → profit=+%.4f → SPIKE_HOLD 4min",
                        sym, state.current_profit,
                    )
                    self._persist(now)
                    return
                elif now - _spk_chk > 5.0:
                    self._k1000_spike_check.pop(sym, None)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 spike check expiró profit=%.4f → continúa",
                        sym, state.current_profit,
                    )

            # Spike en contrato
            if _new_spike and not _broker_closed:
                state.k1000_had_spike = True
                if state.current_profit > 0:
                    self._k1000_spike_check.pop(sym, None)
                    state.k1000_phase            = "SPIKE_HOLD"
                    state.k1000_spike_hold_until = now + K1000_SPIKE_HOLD_S
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 spike profit=+%.4f → SPIKE_HOLD 4min",
                        sym, state.current_profit,
                    )
                    self._persist(now)
                    return
                else:
                    self._k1000_spike_check[sym] = now
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 spike profit=%.4f (neg) → ventana 5s",
                        sym, state.current_profit,
                    )

            # Broker cerró el contrato
            if _broker_closed:
                _last_closed = getattr(state, 'k1000_last_closed_cid', None)
                if _last_closed is not None and _ic_prev_cid is not None and int(_ic_prev_cid) == int(_last_closed):
                    state.k1000_last_closed_cid = None
                    state.contract_id = None
                    state.k1000_had_spike = True
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 skip re-adjuntar stale cid=%d (ya procesado) → reabrir",
                        sym, int(_last_closed),
                    )
                    state.k1000_phase = "IN_CONTRACT"
                    state.k1000_phase_ts = now
                    self._persist(now)
                    await self._open_1000_simple(sym, state, now)
                    return

                # Usar current_profit (actualizado por _query_profit en el tick anterior)
                # NO usar last_close_profit — ese era el del contrato anterior (siempre 0 tras reset)
                _pnl = state.current_profit
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                _had_spk = getattr(state, 'k1000_had_spike', False)
                state.k1000_had_spike = False
                if _pnl > 0 or _had_spk:
                    _next_idx = 0
                    _reason = "win→$10" if _pnl > 0 else "loss+spike→$10"
                elif _sidx < len(K1000_STAKES_1000) - 1:
                    _next_idx = _sidx + 1
                    _reason = f"loss-sin-spike→${K1000_STAKES_1000[_next_idx]:.0f}"
                else:
                    _next_idx = 0
                    _reason = "loss-sin-spike-$20→esperar-zona"
                state.k1000_stake_idx   = _next_idx
                state.k1000_cycle_stake = K1000_STAKES_1000[_next_idx]
                state.last_close_profit = _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, _had_spk, _sidx, now, close_type="BROKER")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 broker-close pnl=%.4f day=%.2f %s → idx=%d ($%.0f) WAIT zona",
                    sym, _pnl, state.day_pnl_1000, _reason, _next_idx, K1000_STAKES_1000[_next_idx],
                )
                if _ic_prev_cid is not None:
                    state.k1000_last_closed_cid = int(_ic_prev_cid)
                self._persist(now)
                return

            # Tiempo del contrato cumplido
            _this_contract_s = K1000_SPIKE_CONTRACT_S if getattr(state, 'k1000_spike_triggered', False) else K1000_CONTRACT_S
            if now >= state.k1000_phase_ts + _this_contract_s:
                _cid = int(state.contract_id)
                _pnl = state.current_profit
                state.contract_id    = None
                state.current_profit = 0.0
                try:
                    await self._executor.close_contract(_cid)
                except Exception as _e:
                    _LOGGER.error("[ENTRADA_DIEGO] %s K1000 contrato CLOSE error: %s", sym, _e)
                _had_spk = getattr(state, 'k1000_had_spike', False)
                state.k1000_had_spike = False
                if _pnl > 0 or _had_spk:
                    _next_idx = 0
                    _reason = "win→$10" if _pnl > 0 else "loss+spike→$10"
                elif _sidx < len(K1000_STAKES_1000) - 1:
                    _next_idx = _sidx + 1
                    _reason = f"loss-sin-spike→${K1000_STAKES_1000[_next_idx]:.0f}"
                else:
                    _next_idx = 0
                    _reason = "loss-sin-spike-$20→esperar-zona"
                state.k1000_stake_idx      = _next_idx
                state.k1000_cycle_stake    = K1000_STAKES_1000[_next_idx]
                state.k1000_spike_triggered = False
                state.last_close_profit    = _pnl
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, _had_spk, _sidx, now, close_type="TIMER")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 %.0fmin pnl=%.4f day=%.2f %s → idx=%d ($%.0f) WAIT zona",
                    sym, _this_contract_s / 60, _pnl, state.day_pnl_1000, _reason, _next_idx, K1000_STAKES_1000[_next_idx],
                )
                state.k1000_last_closed_cid = _cid
                self._persist(now)
            return

        # ────────────────────────────────────────────────────────────────────
        # SPIKE_HOLD: spike llegó con profit+, esperar 4min
        # ────────────────────────────────────────────────────────────────────
        if phase == "SPIKE_HOLD":
            _broker_closed_sh = False
            _sh_prev_cid = state.contract_id
            if state.contract_id is not None:
                _qp = self._query_profit(state.contract_id)
                if _qp is not None:
                    state.current_profit = _qp
                else:
                    _broker_closed_sh = True
                    state.contract_id = None

            if _broker_closed_sh or state.contract_id is None:
                _pnl = state.current_profit
                state.k1000_stake_idx   = 0
                state.k1000_cycle_stake = K1000_STAKES_1000[0]
                state.last_close_profit = _pnl
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, True, _sidx, now, close_type="BROKER")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 SPIKE_HOLD broker-close pnl=%.4f → $10 WAIT zona",
                    sym, _pnl,
                )
                if _sh_prev_cid is not None:
                    state.k1000_last_closed_cid = int(_sh_prev_cid)
                self._persist(now)
                return

            if now >= state.k1000_spike_hold_until:
                _cid = int(state.contract_id)
                _pnl = state.current_profit
                state.contract_id    = None
                state.current_profit = 0.0
                try:
                    await self._executor.close_contract(_cid)
                except Exception as _e:
                    _LOGGER.error("[ENTRADA_DIEGO] %s K1000 SPIKE_HOLD close error: %s", sym, _e)
                state.k1000_stake_idx   = 0
                state.k1000_cycle_stake = K1000_STAKES_1000[0]
                state.last_close_profit = _pnl
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, True, _sidx, now, close_type="SPIKE_HOLD")
                if _pnl >= 5.0:
                    state.k1000_rest_until       = now + POST_WIN_REST_S
                    state.k1000_rest_spike_ref   = float(self._risk.get_last_spike_ts(sym) or now)
                    state.k1000_rest_good_spikes = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 SPIKE_HOLD win pnl=+%.2f → REST 2h hasta %s",
                        sym, _pnl, datetime.datetime.utcfromtimestamp(state.k1000_rest_until).strftime('%H:%M UTC'),
                    )
                else:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 SPIKE_HOLD 4min END pnl=%.4f day=+%.2f → reset $%.0f inmediato",
                        sym, _pnl, state.day_pnl_1000, K1000_STAKES_1000[0],
                    )
                state.k1000_last_closed_cid = _cid
                self._persist(now)
                # watchdog reanuda: revisará zona en ≤15s (o después de REST)

    async def _open_1000_scout(self, sym: str, state: _SymState, now: float) -> None:
        await self._open_1000_simple(sym, state, now)

    async def _open_1000_simple(self, sym: str, state: _SymState, now: float) -> None:
        # POST_WIN_REST: descanso 2h tras win grande con spike
        if state.k1000_rest_until > now:
            _last_spk_ts  = float(self._risk.get_last_spike_ts(sym) or 0.0)
            _last_spk_rat = float(self._risk.get_last_spike_ratio(sym) or 0.0)
            if _last_spk_ts > state.k1000_rest_spike_ref and _last_spk_rat >= 60.0:
                state.k1000_rest_spike_ref   = _last_spk_ts
                state.k1000_rest_good_spikes += 1
                if state.k1000_rest_good_spikes >= POST_WIN_RELEASE_SPK:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 REST liberado anticipado — %d spikes buenos (ratio>=60)",
                        sym, state.k1000_rest_good_spikes,
                    )
                    state.k1000_rest_until       = 0.0
                    state.k1000_rest_good_spikes = 0
            if state.k1000_rest_until > now:
                state.k1000_phase = "WAIT"
                return

        from src.execution.deriv_trader import DerivOrder
        stake      = state.k1000_cycle_stake or K1000_STAKES_1000[0]
        _spk_trig  = getattr(state, 'k1000_spike_triggered', False)
        _c_s       = K1000_SPIKE_CONTRACT_S if _spk_trig else K1000_CONTRACT_S
        max_hold_s = _c_s + K1000_SPIKE_HOLD_S + 60
        side       = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s K1000 ABRIENDO %s $%.0f max_hold=%ds (%s)",
            sym, side, stake, max_hold_s, "4min spike" if _spk_trig else "20min timer",
        )
        try:
            async with self._open_lock:
                order = DerivOrder(
                    symbol=sym,
                    side=side,
                    stake_usdt=stake,
                    multiplier=MULTIPLIER,
                    stop_loss_pct=0.70,
                    take_profit_pct=0.65,
                    max_hold_seconds=float(max_hold_s),
                    score_breakdown={
                        "quality_tier": "entrada_diego",
                        "setup":        "k1000_simple",
                        "grade":        "ED",
                        "score":        0.0,
                        "entrada_diego": True,
                        "skip_dpm":     True,
                    },
                )
                result = await self._executor.execute(order)
            if result and result.get("status") == "live":
                cid = result.get("contract_id")
                state.contract_id    = int(cid) if cid else None
                state.open_ts        = now
                state.current_profit = 0.0
                state.phase          = "OPEN"
                state.k1000_phase_ts = now
                state.k1000_had_spike = False
                self._ed_save_open(sym, stake, now)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 OPEN OK contract=%s stake=$%.0f",
                    sym, state.contract_id, stake,
                )
            elif result and result.get("status") == "symbol_already_open":
                _existing = result.get("open_contracts", [])
                if _existing:
                    state.contract_id = int(_existing[0])
                    state.open_ts     = now
                    state.phase       = "OPEN"
                    _LOGGER.info("[ENTRADA_DIEGO] %s K1000 re-adjuntado contrato %s", sym, state.contract_id)
            else:
                _res_str = str(result)
                if "max_open_contracts" in _res_str:
                    _LOGGER.info("[ENTRADA_DIEGO] %s K1000 pendiente (max_contracts) → reintento", sym)
                else:
                    state.k1000_phase = "WAIT"
                    state.k1000_phase_ts = now
                    _LOGGER.warning("[ENTRADA_DIEGO] %s K1000 OPEN no-live %s → WAIT", sym, result)
            self._persist(now)
        except Exception as exc:
            _exc_str = str(exc)
            if "max_open_contracts" in _exc_str:
                _LOGGER.info("[ENTRADA_DIEGO] %s K1000 pendiente (max_contracts) → reintento", sym)
            else:
                _LOGGER.error("[ENTRADA_DIEGO] %s K1000 open error $%.0f: %s", sym, stake, exc)
                state.k1000_phase = "WAIT"
                state.k1000_phase_ts = now
            self._persist(now)

    # ── Operaciones de contrato ───────────────────────────────────────────────

    def _get_open_stake(self, sym: str) -> float:
        state = self._states.get(sym)
        if not state or not state.contract_id:
            return 0.0
        return getattr(state, 'k1000_cycle_stake', 0.0) or 0.0

    async def _evict_lowest_stake(self, pending_stake: float, now: float) -> bool:
        """Cierra el contrato de menor stake si es menor que pending_stake."""
        candidates = [
            (self._get_open_stake(sym), sym, self._states[sym])
            for sym in self._states
            if self._states[sym].contract_id and self._get_open_stake(sym) > 0
        ]
        if not candidates:
            return False
        candidates.sort(key=lambda x: x[0])
        lowest_stake, victim_sym, victim = candidates[0]
        if lowest_stake >= pending_stake:
            return False
        _cid = int(victim.contract_id)
        _pnl = victim.current_profit
        _LOGGER.info(
            "[ENTRADA_DIEGO] PRIORITY EVICT %s cid=%s stake=$%.0f pnl=%.4f → slot para $%.0f",
            victim_sym, _cid, lowest_stake, _pnl, pending_stake,
        )
        victim.contract_id    = None
        victim.current_profit = 0.0
        try:
            await self._executor.close_contract(_cid)
        except Exception as _exc:
            _LOGGER.error("[ENTRADA_DIEGO] PRIORITY EVICT close error: %s", _exc)
        victim.k1000_had_spike   = False
        victim.k1000_stake_idx   = 0
        victim.k1000_cycle_stake = K1000_STAKES_1000[0]
        victim.k1000_phase       = "WAIT"
        victim.k1000_phase_ts    = now
        victim.day_pnl_1000 = getattr(victim, 'day_pnl_1000', 0.0) + _pnl
        return True

    # ── Query helpers ─────────────────────────────────────────────────────────

    def _query_contract(self, contract_id: int) -> Optional[dict[str, Any]]:
        try:
            for oc in self._executor.get_open_contracts_for_status():
                if oc.get("contract_id") == contract_id:
                    return oc
        except Exception:
            pass
        return None

    def _query_profit(self, contract_id: int) -> Optional[float]:
        """Retorna el floating PnL del contrato abierto, o None si ya no existe."""
        try:
            for oc in self._executor.get_open_contracts_for_status():
                if oc.get("contract_id") == contract_id:
                    # El executor expone "floating_pnl" (no "profit") en get_open_contracts_for_status
                    pnl = oc.get("floating_pnl")
                    if pnl is None:
                        pnl = oc.get("profit", 0.0)
                    return float(pnl)
        except Exception:
            pass
        return None

    # ── Restore desde disco ───────────────────────────────────────────────────

    async def _restore_from_disk(self) -> None:
        try:
            if not self._state_file.exists():
                return
            data = json.loads(self._state_file.read_text())
            now  = time.time()

            self._global_pnl = float(data.get("global_pnl", 0.0))
            self._global_pnl_next_target = float(data.get("global_pnl_next_target", GLOBAL_PNL_TARGET))
            raw_pause = float(data.get("global_pause_until", 0.0))
            self._global_pause_until = raw_pause if raw_pause > now else 0.0

            for sym in SYMBOLS_ED:
                s = data.get(sym, {})
                if not s:
                    continue
                contract_id = s.get("contract_id")
                phase       = s.get("phase", "IDLE")
                reopens     = int(s.get("reopens", 0))

                if contract_id is not None:
                    live = self._query_contract(int(contract_id))
                    if live:
                        st = self._states[sym]
                        st.contract_id       = int(contract_id)
                        st.reopens           = reopens
                        st.open_ts           = float(s.get("open_ts", now))
                        st.last_spike_ts     = float(s.get("last_spike_ts", 0.0))
                        st.last_close_profit = float(s.get("last_close_profit", 0.0))
                        # Restaurar estado K1000
                        _k1000_ph_raw = s.get("k1000_phase", "WAIT")
                        _K1000_PM = {
                            "WAIT_SPIKE": "WAIT", "COOLING": "WAIT",
                            "WINDOW": "IN_CONTRACT",
                            "SCOUT": "WAIT", "STAKE_20": "WAIT",
                            "STAKE_40": "WAIT", "STAKE_80": "WAIT", "STAKE_200": "WAIT",
                        }
                        if _k1000_ph_raw in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
                            st.k1000_phase = _k1000_ph_raw
                        else:
                            st.k1000_phase = _K1000_PM.get(_k1000_ph_raw, "WAIT")
                        st.k1000_phase_ts         = float(s.get("k1000_phase_ts", now))
                        st.k1000_spike_hold_until = float(s.get("k1000_spike_hold_until", 0.0))
                        st.k1000_stake_idx        = int(s.get("k1000_stake_idx", 0))
                        st.k1000_cycle_stake      = K1000_STAKES_1000[max(0, min(st.k1000_stake_idx, len(K1000_STAKES_1000) - 1))]
                        st.k1000_had_spike        = bool(s.get("k1000_had_spike", False))
                        st.k1000_win_gate_triggered = bool(s.get("k1000_win_gate_triggered", False))
                        st.k1000_loss_gate_floor  = float(s.get("k1000_loss_gate_floor", 0.0))
                        st.day_pnl_1000           = float(s.get("day_pnl_1000", 0.0))
                        st.day_start_ts_1000      = float(s.get("day_start_ts_1000", 0.0))
                        st.hour_pnl_1000          = float(s.get("hour_pnl_1000", 0.0))
                        st.hour_start_ts_1000     = float(s.get("hour_start_ts_1000", 0.0))
                        if phase == "PROFIT_TIMER" and float(s.get("profit_positive_ts", 0.0)) > 0:
                            st.phase              = "PROFIT_TIMER"
                            st.profit_positive_ts = float(s["profit_positive_ts"])
                        else:
                            st.phase              = "OPEN"
                            st.profit_positive_ts = 0.0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s RESTAURADO: phase=%s contract=%s reopens=%d",
                            sym, st.phase, contract_id, st.reopens,
                        )
                        continue

                # Sin contrato — restaurar k1000_phase
                _st_1k = self._states[sym]
                _k1000_ph_raw = s.get("k1000_phase", "WAIT")
                _K1000_PHASE_MAP = {
                    "WAIT_SPIKE": "WAIT", "COOLING": "WAIT",
                    "WINDOW": "IN_CONTRACT",
                    "SCOUT": "WAIT", "STAKE_20": "WAIT",
                    "STAKE_40": "WAIT", "STAKE_80": "WAIT", "STAKE_200": "WAIT",
                }
                if _k1000_ph_raw in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
                    _st_1k.k1000_phase = _k1000_ph_raw
                else:
                    _st_1k.k1000_phase = _K1000_PHASE_MAP.get(_k1000_ph_raw, "WAIT")
                _st_1k.k1000_phase_ts         = float(s.get("k1000_phase_ts", now))
                _st_1k.k1000_spike_hold_until = float(s.get("k1000_spike_hold_until", 0.0))
                _st_1k.last_spike_ts          = float(s.get("last_spike_ts", 0.0))
                _st_1k.k1000_stake_idx        = int(s.get("k1000_stake_idx", 0))
                _st_1k.k1000_cycle_stake      = K1000_STAKES_1000[max(0, min(_st_1k.k1000_stake_idx, len(K1000_STAKES_1000) - 1))]
                _st_1k.k1000_had_spike        = bool(s.get("k1000_had_spike", False))
                _st_1k.k1000_win_gate_triggered = bool(s.get("k1000_win_gate_triggered", False))
                _st_1k.k1000_loss_gate_floor  = float(s.get("k1000_loss_gate_floor", 0.0))
                _st_1k.day_pnl_1000           = float(s.get("day_pnl_1000", 0.0))
                _st_1k.day_start_ts_1000      = float(s.get("day_start_ts_1000", 0.0))
                _st_1k.hour_pnl_1000          = float(s.get("hour_pnl_1000", 0.0))
                _st_1k.hour_start_ts_1000     = float(s.get("hour_start_ts_1000", 0.0))
                # Restaurar REST/BLOCK si estaban activos
                _rut = float(s.get("k1000_rest_until", 0.0))
                if _rut > now:
                    _st_1k.k1000_rest_until       = _rut
                    _st_1k.k1000_rest_spike_ref   = float(s.get("k1000_rest_spike_ref", 0.0))
                    _st_1k.k1000_rest_good_spikes = int(s.get("k1000_rest_good_spikes", 0))
                _but = float(s.get("k1000_blocked_until", 0.0))
                if _but > now:
                    _st_1k.k1000_blocked_until = _but
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 RESTAURADO sin contrato → phase=%s stake=$%.0f idx=%d day_pnl=%.2f",
                    sym, _st_1k.k1000_phase, _st_1k.k1000_cycle_stake, _st_1k.k1000_stake_idx, _st_1k.day_pnl_1000,
                )

        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] restore_from_disk error: %s", exc)

        self._persist(time.time())

    # ── Análisis solapado: spike history + log por contrato ─────────────────

    def _get_power_30min(self, sym: str, now: float) -> float:
        cutoff = now - 1800.0
        return sum(r for t, r in self._states[sym].power_window if t > cutoff)

    def _get_spike_count_30min(self, sym: str, now: float) -> int:
        cutoff = now - 1800.0
        return sum(1 for t, _ in self._states[sym].power_window if t > cutoff)

    def _ed_seed_spike_hist(self) -> None:
        """Siembra _ed_spike_hist desde deriv_spike_events.json al arranque (últimas 4h)."""
        path = self._logs_dir / "deriv_spike_events.json"
        try:
            with open(path) as f:
                events = json.load(f)
            cutoff = time.time() - 14400.0
            seeded: dict[str, int] = {}
            for ev in events:
                ts = float(ev.get("ts", 0))
                if ts < cutoff:
                    continue
                sym = str(ev.get("symbol", "")).upper()
                ratio = float(ev.get("ratio", 0.0))
                if sym in self._ed_spike_hist and ratio > 0:
                    self._ed_spike_hist[sym].append((ts, ratio))
                    seeded[sym] = seeded.get(sym, 0) + 1
            for sym, h in self._ed_spike_hist.items():
                h.sort(key=lambda x: x[0])
            for sym, n in sorted(seeded.items()):
                _LOGGER.info("[ENTRADA_DIEGO] ed_seed %s: %d spikes (4h)", sym, n)
        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] ed_seed fallo: %s", exc)

    def _ed_push_spike(self, sym: str, ts: float, ratio: float) -> None:
        h = self._ed_spike_hist.get(sym)
        if h is None:
            return
        h.append((ts, ratio))
        cutoff = ts - 14400.0
        while h and h[0][0] < cutoff:
            h.pop(0)

    def _ed_ctx(self, sym: str, now: float) -> dict:
        h = self._ed_spike_hist.get(sym, [])
        n30  = sum(1 for t, _ in h if now - t <=  1800)
        n60  = sum(1 for t, _ in h if now - t <=  3600)
        n120 = sum(1 for t, _ in h if now - t <=  7200)
        n180 = sum(1 for t, _ in h if now - t <= 10800)
        n240 = sum(1 for t, _ in h if now - t <= 14400)
        def _stats(secs: float):
            rs = sorted(r for t, r in h if now - t <= secs and r > 0)
            if not rs:
                return 0.0, 0.0
            return round(rs[len(rs) // 2], 1), round(rs[-1], 1)
        med_r30,  max_r30  = _stats(1800)
        med_r60,  max_r60  = _stats(3600)
        med_r120, max_r120 = _stats(7200)
        ts_sorted = sorted(t for t, _ in h)
        last = ts_sorted[-1] if ts_sorted else 0.0
        gap_s      = round(now - last, 1) if last > 0 else -1.0
        gap_prev_s = round(ts_sorted[-1] - ts_sorted[-2], 1) if len(ts_sorted) >= 2 else -1.0
        gap_3rd_s  = round(ts_sorted[-2] - ts_sorted[-3], 1) if len(ts_sorted) >= 3 else -1.0
        return {
            "n30": n30, "n60": n60, "n120": n120, "n180": n180, "n240": n240,
            "med_r30": med_r30, "max_r30": max_r30,
            "med_r60": med_r60, "max_r60": max_r60,
            "med_r120": med_r120, "max_r120": max_r120,
            "gap_s": gap_s, "gap_prev_s": gap_prev_s, "gap_3rd_s": gap_3rd_s,
        }

    def _ed_save_open(self, sym: str, stake: float, now: float) -> None:
        cross_n10 = sum(sum(1 for t, _ in h if now - t <= 600) for h in self._ed_spike_hist.values())
        cross_n30 = sum(sum(1 for t, _ in h if now - t <= 1800) for h in self._ed_spike_hist.values())
        syms_in_sequia = sum(
            1 for s, h in self._ed_spike_hist.items()
            if s != sym and (not h or (now - max((t for t, _ in h), default=0.0)) > 300)
        )
        self._ed_open_info[sym] = {
            "stake":             stake,
            "t_open":            now,
            "hour_utc":          int(now // 3600) % 24,
            "power":             round(self._get_power_30min(sym, now), 1),
            "ctx":               self._ed_ctx(sym, now),
            "cross_n10":         cross_n10,
            "cross_n30":         cross_n30,
            "syms_in_sequia":    syms_in_sequia,
            "pre_open_spike_ts": float(self._risk.get_last_spike_ts(sym) or 0.0),
        }

    def _ed_log(self, sym: str, pnl: float, had_spk: bool, rec_lvl: int, now: float, close_type: str = "UNKNOWN") -> None:
        info = self._ed_open_info.pop(sym, {})
        ctx_open = info.get("ctx", {})
        t_open = info.get("t_open", now)
        h_sym = self._ed_spike_hist.get(sym, [])
        spikes_during = sum(1 for t, _ in h_sym if t_open <= t <= now)
        rec = {
            "ts":        round(now, 1),
            "sym":       sym,
            "strategy":  "K1000",
            "stake":     info.get("stake", 0.0),
            "pnl":       round(pnl, 4),
            "win":       pnl > 0,
            "dur_s":     round(now - t_open, 1),
            "hour_utc":  info.get("hour_utc", int(now // 3600) % 24),
            "power_open":info.get("power", 0.0),
            "rec_lvl":   info.get("rec_lvl_at_open", rec_lvl),
            "had_spike": had_spk,
            "close_type":close_type,
            "spikes_during_contract": spikes_during,
            **{f"open_{k}": v for k, v in ctx_open.items()},
            "open_cross_n10":      info.get("cross_n10", 0),
            "open_cross_n30":      info.get("cross_n30", 0),
            "open_syms_in_sequia": info.get("syms_in_sequia", 0),
            **{f"close_{k}": v for k, v in self._ed_ctx(sym, now).items()},
        }
        path = self._logs_dir / "deriv_ed_analysis.jsonl"
        try:
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def _check_sym_pnl_gate(self, sym: str, now: float) -> None:
        pass

    def _add_global_pnl(self, sym: str, profit: float, now: float) -> None:
        pass  # Gates PnL desactivados — recolección de datos sin restricciones

    def _persist(self, now: float) -> None:
        try:
            snapshot = self.get_state_snapshot()
            payload  = json.dumps(snapshot, indent=2)
            self._state_file.write_text(payload)
        except Exception as exc:
            import traceback
            _LOGGER.error("[ENTRADA_DIEGO] persist FAILED: %s\n%s", exc, traceback.format_exc())
