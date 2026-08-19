"""
entrada_diego.py — Experimento factorial 8 símbolos.

Ocho símbolos (BOOM/CRASH 500·600·900·1000) en una sola máquina de estados K1000.
Diseño factorial 4×2 asignado por contract_id % 8:
  Factor A (arm_dur_s): 0.25× / 0.5× / 1× / 2× del hueco medio de cada símbolo
  Factor B (arm_hold_s): 0 s / 240 s post-spike

Objetivo: separar c (coste fijo por apertura) de b (sangrado por hora)
          y medir λ·J−b en los 8 símbolos con datos comparables.

Stake: $10 plano en los ocho. Sin puertas. Sin escalada.
Sin cooldown entre contratos. Reabre inmediatamente tras cada cierre.

Activación: env ENTRADA_DIEGO_ENABLED=true
Estado:     {BOT_STATE_DIR}/entrada_diego_state.json
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("entrada_diego")

SYMBOLS_500  = {"BOOM500",  "CRASH500"}
SYMBOLS_600  = {"BOOM600",  "CRASH600"}
SYMBOLS_900  = {"CRASH900", "BOOM900"}
SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_K1000 = SYMBOLS_500 | SYMBOLS_600 | SYMBOLS_900 | SYMBOLS_1000
SYMBOLS_ED    = SYMBOLS_K1000

_STAKE_LADDER_1000 = [10.0]  # flat — kept for API compat

MULTIPLIER = int(os.getenv("ENTRADA_DIEGO_MULTIPLIER", "200"))

# ── Factorial design ─────────────────────────────────────────────────────────
# arm_dur: 0.25× / 0.5× / 1× / 2× del hueco medio por símbolo
K1000_DUR_ARMS: dict[str, list[int]] = {
    "BOOM500":  [130, 260,  520, 1050],
    "CRASH500": [130, 260,  530, 1060],
    "BOOM600":  [170, 340,  690, 1380],
    "CRASH600": [170, 340,  680, 1360],
    "BOOM900":  [240, 490,  980, 1950],
    "CRASH900": [240, 480,  960, 1920],
    "BOOM1000": [240, 480,  960, 1930],
    "CRASH1000":[240, 480,  960, 1920],
}
K1000_HOLD_ARMS: list[int] = [0, 240]

def _assign_arms(sym: str, counter: int) -> tuple[int, int, int]:
    """Deterministic arm assignment from a per-symbol counter (NOT contract_id).
    contract_id % 8 only ever yields 3 or 7 because Deriv IDs advance by 20.
    Returns (arm_dur_s, arm_hold_s, k)."""
    k = int(counter) % 8
    return K1000_DUR_ARMS[sym][k % 4], K1000_HOLD_ARMS[k // 4], k

# Flat stake — no escalation
K1000_STAKES_1000 = [10.0]
K1000_STAKE_START = 10.0
K1000_STAKE_MID   = 10.0
K1000_STAKE_MAX   = 10.0

# All gates disabled
POST_WIN_REST_S      = 0.0
POST_WIN_RELEASE_SPK = 0
POST_WIN_RELEASE_RAT = 0.0
GLOBAL_PNL_TARGET    = float("inf")
GLOBAL_PAUSE_HOURS   = 0.0
SYM_PNL_WIN_GATE_USD  = float("inf")
SYM_PNL_LOSS_GATE_USD = float("inf")
SYM_PNL_WIN_PAUSE_H   = 0.0
SYM_PNL_LOSS_PAUSE_H  = 0.0

# Only disable symbols that are not in our experiment set
_ED_DISABLED_RAW    = os.getenv("ENTRADA_DIEGO_DISABLED_SYMBOLS", "")
SYMBOLS_ED_DISABLED = {s.strip().upper() for s in _ED_DISABLED_RAW.split(",") if s.strip()} - SYMBOLS_K1000


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
    k1000_cycle_stake: float = 10.0
    k1000_stake_idx: int = 0
    k1000_spike_triggered: bool = False
    k1000_blocked_until: float = 0.0
    k1000_rest_until: float = 0.0
    k1000_rest_spike_ref: float = 0.0
    k1000_rest_good_spikes: int = 0
    k1000_had_spike: bool = False
    k1000_last_closed_cid: Optional[int] = None
    k1000_win_gate_triggered: bool = False
    k1000_win_threshold: float = 0.0       # disabled
    k1000_loss_gate_floor: float = 0.0
    hour_pnl_1000: float = 0.0
    hour_start_ts_1000: float = 0.0
    day_pnl_1000: float = 0.0
    day_start_ts_1000: float = 0.0
    # Factorial experiment fields
    k1000_arm_dur_s: int = 240
    k1000_arm_hold_s: int = 240
    k1000_arm_k: int = 0           # celda 0-7 del último contrato abierto
    k1000_arm_counter: int = 0     # contador propio por símbolo; persisted; avanza en cada apertura exitosa
    k1000_profit_at_spike: float = 0.0    # profit en el PRIMER spike (no se sobrescribe)
    k1000_spike_ts_first: float = 0.0     # timestamp del primer spike en el contrato
    k1000_spike_details: list = field(default_factory=list)  # [{jump, ratio, offset_s}]

    def remaining_s(self, now: float) -> float:
        if self.phase == "OPEN" and self.k1000_arm_dur_s > 0:
            return max(0.0, self.k1000_arm_dur_s - (now - self.open_ts))
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
            "sym_pnl_win_gate":   0.0,
            "sym_pnl_loss_gate":  0.0,
            "k1000_phase":            self.k1000_phase,
            "k1000_phase_ts":         round(self.k1000_phase_ts, 3),
            "k1000_peak":             round(self.k1000_peak, 4),
            "k1000_profit_ts":        round(self.k1000_profit_ts, 3),
            "k1000_spike_hold_until": round(self.k1000_spike_hold_until, 3),
            "k1000_cycle_stake":      10.0,
            "k1000_stake_idx":        0,
            "k1000_spike_triggered":      False,
            "k1000_had_spike":            self.k1000_had_spike,
            "k1000_win_gate_triggered":   False,
            "k1000_win_threshold":        0.0,
            "k1000_loss_gate_floor":      0.0,
            "k1000_arm_dur_s":        self.k1000_arm_dur_s,
            "k1000_arm_hold_s":       self.k1000_arm_hold_s,
            "k1000_arm_k":            self.k1000_arm_k,
            "k1000_arm_counter":      self.k1000_arm_counter,
            "k1000_blocked_until":    0.0,
            "k1000_rest_until":       0.0,
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
        self._evict_in_progress: bool = False
        self._restore_lock = asyncio.Lock()
        self._ed_spike_hist: dict[str, list] = {sym: [] for sym in SYMBOLS_ED}
        self._ed_open_info:  dict[str, dict] = {}
        self._last_close_ts: dict[str, float] = {sym: 0.0 for sym in SYMBOLS_ED}
        self._restored = False
        self._global_pnl: float = 0.0
        self._global_pause_until: float = 0.0
        self._watchdog_started: set[str] = set()

        if self._enabled:
            active = sorted(SYMBOLS_ED - SYMBOLS_ED_DISABLED)
            paused = sorted(SYMBOLS_ED_DISABLED)
            _LOGGER.info(
                "[ENTRADA_DIEGO] FACTORIAL 8SYM ACTIVADO | activos=%s paused=%s | stake=$10 plano",
                active, paused,
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
                    for _sk in SYMBOLS_ED:
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
            "global_pause_until": 0.0,
            "global_pnl_next_target": 0.0,
        }
        for sym, st in self._states.items():
            if sym in SYMBOLS_ED_DISABLED:
                result[sym] = {"phase": "DISABLED", "reopens": 0, "current_profit": 0.0, "remaining_s": 0.0}
            else:
                result[sym] = st.to_dict(now)
                _ctx_k = self._ed_ctx(sym, now)
                result[sym]["ed_gap_s"] = round(_ctx_k["gap_s"], 1)
                try:
                    _imm_k = self._risk.get_spike_imminence_state(sym)
                    result[sym]["imminence_state"] = _imm_k.get("state", "UNKNOWN")
                    result[sym]["imminence_score"] = round(_imm_k.get("score", 0.0), 3)
                except Exception:
                    result[sym]["imminence_state"] = "UNKNOWN"
                    result[sym]["imminence_score"] = 0.0
        return result

    # ── Máquina de estados ───────────────────────────────────────────────────

    async def _process(self, sym: str, tick: Any) -> None:
        if sym in SYMBOLS_ED_DISABLED:
            return
        if sym in SYMBOLS_K1000:
            await self._process_1000_scout(sym)
            return

    # ── K1000 watchdog — 5s para reapertura rápida ───────────────────────────

    async def _1000_watchdog_loop(self, sym: str) -> None:
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog iniciado (intervalo 5s)", sym)
        while self._enabled and sym not in SYMBOLS_ED_DISABLED:
            await asyncio.sleep(5)
            if not self._enabled or sym in SYMBOLS_ED_DISABLED:
                break
            try:
                async with self._locks[sym]:
                    await self._process_1000_scout(sym)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s watchdog error: %s", sym, exc)
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog terminado", sym)

    async def _process_1000_scout(self, sym: str) -> None:
        await self._process_1000_simple(sym)

    # ── K1000 state machine ──────────────────────────────────────────────────

    async def _process_1000_simple(self, sym: str) -> None:
        """
        Factorial K1000: WAIT → IN_CONTRACT(arm_dur_s) → SPIKE_HOLD(arm_hold_s) → WAIT
        Sin puertas. Reabre inmediatamente. Stake $10 plano. 8 símbolos.
        """
        state = self._states[sym]
        now   = time.time()

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
        _phase_ts = getattr(state, "k1000_phase_ts", 0.0)
        if _last_spk_ts > _phase_ts > 0 and not getattr(state, "k1000_had_spike", False):
            state.k1000_had_spike = True
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s spike histórico post-restart (%.0fs en contrato) → had_spike restaurado",
                sym, _last_spk_ts - _phase_ts,
            )

        phase = state.k1000_phase

        # ────────────────────────────────────────────────────────────────────
        # WAIT: abrir inmediatamente, sin condiciones
        # ────────────────────────────────────────────────────────────────────
        if phase == "WAIT":
            state.k1000_phase    = "IN_CONTRACT"
            state.k1000_phase_ts = now
            state.k1000_had_spike       = False
            state.k1000_spike_details   = []
            state.k1000_profit_at_spike = 0.0
            state.k1000_spike_ts_first  = 0.0
            _LOGGER.info("[ENTRADA_DIEGO] %s WAIT → IN_CONTRACT, abriendo…", sym)
            await self._open_1000_simple(sym, state, now)
            return

        # ────────────────────────────────────────────────────────────────────
        # IN_CONTRACT: contrato abierto, duración arm_dur_s
        # ────────────────────────────────────────────────────────────────────
        if phase == "IN_CONTRACT":
            if state.contract_id is None:
                _last_retry = self._k1000_pending_ts.get(sym, 0.0)
                if now - _last_retry < 3.0:
                    return
                self._k1000_pending_ts[sym] = now
                _LOGGER.info("[ENTRADA_DIEGO] %s IN_CONTRACT pendiente → reintento $10", sym)
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
                    state.k1000_had_spike = True
                    state.k1000_phase = "SPIKE_HOLD"
                    state.k1000_spike_hold_until = now + state.k1000_arm_hold_s
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s spike-check profit=+%.4f → SPIKE_HOLD %ds",
                        sym, state.current_profit, state.k1000_arm_hold_s,
                    )
                    self._persist(now)
                    return
                elif now - _spk_chk > 5.0:
                    self._k1000_spike_check.pop(sym, None)
                    _LOGGER.info("[ENTRADA_DIEGO] %s spike-check expiró profit=%.4f → continúa", sym, state.current_profit)

            # Spike en contrato — registrar detalles y decidir
            if _new_spike and not _broker_closed:
                state.k1000_had_spike = True
                # Capturar detalles del spike
                _jump_abs = 0.0
                try:
                    _jump_abs = abs(float(self._risk.get_last_spike_jump(sym) or 0.0))
                except AttributeError:
                    pass
                _ratio    = float(self._risk.get_last_spike_ratio(sym) or 0.0)
                _offset_s = round(now - state.k1000_phase_ts, 1)
                state.k1000_spike_details.append({"jump": round(_jump_abs, 6), "ratio": round(_ratio, 2), "offset_s": _offset_s})
                # Primer spike: guardar profit_at_spike (no se sobrescribe)
                if state.k1000_spike_ts_first == 0.0:
                    state.k1000_profit_at_spike = state.current_profit
                    state.k1000_spike_ts_first  = now

                if state.current_profit > 0:
                    self._k1000_spike_check.pop(sym, None)
                    state.k1000_phase            = "SPIKE_HOLD"
                    state.k1000_spike_hold_until = now + state.k1000_arm_hold_s
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s spike profit=+%.4f → SPIKE_HOLD %ds (arm=%d/%d)",
                        sym, state.current_profit, state.k1000_arm_hold_s,
                        state.k1000_arm_dur_s, state.k1000_arm_hold_s,
                    )
                    self._persist(now)
                    return
                else:
                    self._k1000_spike_check[sym] = now
                    _LOGGER.info("[ENTRADA_DIEGO] %s spike profit=%.4f (neg) → ventana 5s", sym, state.current_profit)

            # Broker cerró el contrato
            if _broker_closed:
                _last_closed = getattr(state, "k1000_last_closed_cid", None)
                if _last_closed is not None and _ic_prev_cid is not None and int(_ic_prev_cid) == int(_last_closed):
                    state.k1000_last_closed_cid = None
                    state.contract_id = None
                    state.k1000_had_spike = True
                    state.k1000_phase    = "IN_CONTRACT"
                    state.k1000_phase_ts = now
                    self._persist(now)
                    await self._open_1000_simple(sym, state, now)
                    return

                _pnl     = state.current_profit
                _had_spk = getattr(state, "k1000_had_spike", False)
                state.day_pnl_1000 = getattr(state, "day_pnl_1000", 0.0) + _pnl
                state.last_close_profit = _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, _had_spk, 0, now, close_type="BROKER")
                state.k1000_had_spike = False
                _LOGGER.info("[ENTRADA_DIEGO] %s broker-close pnl=%.4f day=%.2f → WAIT", sym, _pnl, state.day_pnl_1000)
                if _ic_prev_cid is not None:
                    state.k1000_last_closed_cid = int(_ic_prev_cid)
                self._persist(now)
                return

            # Timer del contrato cumplido
            if now >= state.k1000_phase_ts + state.k1000_arm_dur_s:
                _cid  = int(state.contract_id)
                _pnl  = state.current_profit
                _had_spk = getattr(state, "k1000_had_spike", False)
                state.contract_id    = None
                state.current_profit = 0.0
                try:
                    await self._executor.close_contract(_cid)
                except Exception as _e:
                    _LOGGER.error("[ENTRADA_DIEGO] %s TIMER close error: %s", sym, _e)
                state.day_pnl_1000 = getattr(state, "day_pnl_1000", 0.0) + _pnl
                state.last_close_profit = _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, _had_spk, 0, now, close_type="TIMER")
                state.k1000_had_spike = False
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s TIMER %ds pnl=%.4f day=%.2f → WAIT",
                    sym, state.k1000_arm_dur_s, _pnl, state.day_pnl_1000,
                )
                state.k1000_last_closed_cid = _cid
                self._persist(now)
            return

        # ────────────────────────────────────────────────────────────────────
        # SPIKE_HOLD: spike con profit+, esperar arm_hold_s
        # arm_hold_s=0 → la condición se cumple en el mismo tick (o el siguiente)
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

            # Seguir acumulando spike_details (NO actualizar profit_at_spike/spike_ts_first)
            if _new_spike and not _broker_closed_sh:
                _jump_abs = 0.0
                try:
                    _jump_abs = abs(float(self._risk.get_last_spike_jump(sym) or 0.0))
                except AttributeError:
                    pass
                _ratio    = float(self._risk.get_last_spike_ratio(sym) or 0.0)
                _offset_s = round(now - state.k1000_phase_ts, 1)
                state.k1000_spike_details.append({"jump": round(_jump_abs, 6), "ratio": round(_ratio, 2), "offset_s": _offset_s})

            if _broker_closed_sh or state.contract_id is None:
                _pnl = state.current_profit
                state.day_pnl_1000 = getattr(state, "day_pnl_1000", 0.0) + _pnl
                state.last_close_profit = _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, True, 0, now, close_type="BROKER")
                state.k1000_had_spike = False
                _LOGGER.info("[ENTRADA_DIEGO] %s SPIKE_HOLD broker-close pnl=%.4f → WAIT", sym, _pnl)
                if _sh_prev_cid is not None:
                    state.k1000_last_closed_cid = int(_sh_prev_cid)
                self._persist(now)
                return

            if now >= state.k1000_spike_hold_until:
                _cid  = int(state.contract_id)
                _pnl  = state.current_profit
                state.contract_id    = None
                state.current_profit = 0.0
                try:
                    await self._executor.close_contract(_cid)
                except Exception as _e:
                    _LOGGER.error("[ENTRADA_DIEGO] %s SPIKE_HOLD close error: %s", sym, _e)
                state.day_pnl_1000 = getattr(state, "day_pnl_1000", 0.0) + _pnl
                state.last_close_profit = _pnl
                state.k1000_phase       = "WAIT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, True, 0, now, close_type="SPIKE_HOLD")
                state.k1000_had_spike = False
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s SPIKE_HOLD END hold=%.0fs pnl=%.4f day=%.2f → WAIT",
                    sym, state.k1000_arm_hold_s, _pnl, state.day_pnl_1000,
                )
                state.k1000_last_closed_cid = _cid
                self._persist(now)

    async def _open_1000_scout(self, sym: str, state: "_SymState", now: float) -> None:
        await self._open_1000_simple(sym, state, now)

    async def _open_1000_simple(self, sym: str, state: "_SymState", now: float) -> None:
        from src.execution.deriv_trader import DerivOrder
        stake = 10.0
        side  = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        # Assign arms from per-symbol counter BEFORE building the order.
        # Using contract_id % 8 is broken: Deriv IDs advance by 20, so % 8 only yields 3 or 7.
        _counter = state.k1000_arm_counter
        dur_s, hold_s, arm_k = _assign_arms(sym, _counter)
        # Broker safety timeout: specific arm + hold + margin
        max_hold_s = dur_s + hold_s + 60
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s ABRIENDO %s $%.0f arm=%d/%d k=%d counter=%d max_hold=%ds",
            sym, side, stake, dur_s, hold_s, arm_k, _counter, max_hold_s,
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
                        "setup":        "k1000_factorial",
                        "grade":        "ED",
                        "score":        0.0,
                        "entrada_diego": True,
                        "skip_dpm":     True,
                    },
                )
                result = await self._executor.execute(order)
            if result and result.get("status") == "live":
                cid = result.get("contract_id")
                state.contract_id      = int(cid) if cid else None
                state.k1000_arm_dur_s  = dur_s
                state.k1000_arm_hold_s = hold_s
                state.k1000_arm_k      = arm_k
                state.k1000_arm_counter = _counter + 1
                state.k1000_spike_details   = []
                state.k1000_profit_at_spike = 0.0
                state.k1000_spike_ts_first  = 0.0
                state.open_ts        = now
                state.current_profit = 0.0
                state.phase          = "OPEN"
                state.k1000_phase_ts = now
                state.k1000_had_spike = False
                _oc = self._query_contract(state.contract_id) if state.contract_id else None
                _entry_price = float(_oc.get("entry_price", 0.0)) if _oc else 0.0
                self._ed_save_open(sym, stake, now, side=side, cid=state.contract_id,
                                   dur_s=dur_s, hold_s=hold_s, entry_price=_entry_price,
                                   arm_k=arm_k, arm_counter=_counter)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s OPEN OK cid=%s $%.0f arm=%d/%d k=%d counter=%d",
                    sym, state.contract_id, stake, dur_s, hold_s, arm_k, _counter,
                )
            elif result and result.get("status") == "symbol_already_open":
                _existing = result.get("open_contracts", [])
                if _existing:
                    state.contract_id = int(_existing[0])
                    state.open_ts     = now
                    state.phase       = "OPEN"
                    # Keep the pre-assigned arms; counter NOT incremented (no new contract opened)
                    state.k1000_arm_dur_s  = dur_s
                    state.k1000_arm_hold_s = hold_s
                    state.k1000_arm_k      = arm_k
                    _LOGGER.info("[ENTRADA_DIEGO] %s re-adjuntado cid=%s arm=%d/%d k=%d", sym, state.contract_id, dur_s, hold_s, arm_k)
            else:
                _res_str = str(result)
                if "max_open_contracts" in _res_str:
                    _LOGGER.info("[ENTRADA_DIEGO] %s pendiente (max_contracts) → reintento", sym)
                else:
                    state.k1000_phase    = "WAIT"
                    state.k1000_phase_ts = now
                    _LOGGER.warning("[ENTRADA_DIEGO] %s OPEN no-live %s → WAIT", sym, result)
            self._persist(now)
        except Exception as exc:
            _exc_str = str(exc)
            if "max_open_contracts" in _exc_str:
                _LOGGER.info("[ENTRADA_DIEGO] %s pendiente (max_contracts) → reintento", sym)
            else:
                _LOGGER.error("[ENTRADA_DIEGO] %s open error $%.0f: %s", sym, stake, exc)
                state.k1000_phase    = "WAIT"
                state.k1000_phase_ts = now
            self._persist(now)

    # ── Operaciones de contrato ───────────────────────────────────────────────

    def _get_open_stake(self, sym: str) -> float:
        state = self._states.get(sym)
        if not state or not state.contract_id:
            return 0.0
        return 10.0

    async def _evict_lowest_stake(self, pending_stake: float, now: float) -> bool:
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
        _cid  = int(victim.contract_id)
        _pnl  = victim.current_profit
        _LOGGER.info("[ENTRADA_DIEGO] EVICT %s cid=%s pnl=%.4f → slot para $%.0f", victim_sym, _cid, _pnl, pending_stake)
        victim.contract_id    = None
        victim.current_profit = 0.0
        try:
            await self._executor.close_contract(_cid)
        except Exception as _exc:
            _LOGGER.error("[ENTRADA_DIEGO] EVICT close error: %s", _exc)
        victim.k1000_had_spike   = False
        victim.k1000_phase       = "WAIT"
        victim.k1000_phase_ts    = now
        victim.day_pnl_1000 = getattr(victim, "day_pnl_1000", 0.0) + _pnl
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

    def _get_closed_prices(self, cid: Optional[int]) -> tuple[float, float]:
        if not cid:
            return 0.0, 0.0
        try:
            path = self._logs_dir / "deriv_closed_contracts.json"
            contracts = json.loads(path.read_text())
            for c in reversed(contracts):
                if c.get("contract_id") == cid:
                    return float(c.get("entry_price", 0.0)), float(c.get("exit_price", 0.0))
        except Exception:
            pass
        return 0.0, 0.0

    def _query_profit(self, contract_id: int) -> Optional[float]:
        try:
            for oc in self._executor.get_open_contracts_for_status():
                if oc.get("contract_id") == contract_id:
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

            for sym in SYMBOLS_ED:
                s = data.get(sym, {})
                if not s:
                    continue
                contract_id = s.get("contract_id")
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
                        # K1000 phase restore
                        _k1000_ph_raw = s.get("k1000_phase", "WAIT")
                        _K1000_PM = {
                            "WAIT_SPIKE": "WAIT", "COOLING": "WAIT",
                            "WINDOW": "IN_CONTRACT",
                            "SCOUT": "WAIT", "STAKE_20": "WAIT",
                        }
                        if _k1000_ph_raw in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
                            st.k1000_phase = _k1000_ph_raw
                        else:
                            st.k1000_phase = _K1000_PM.get(_k1000_ph_raw, "WAIT")
                        st.k1000_phase_ts         = float(s.get("k1000_phase_ts", now))
                        st.k1000_spike_hold_until = float(s.get("k1000_spike_hold_until", 0.0))
                        st.k1000_had_spike        = bool(s.get("k1000_had_spike", False))
                        st.day_pnl_1000           = float(s.get("day_pnl_1000", 0.0))
                        st.day_start_ts_1000      = float(s.get("day_start_ts_1000", 0.0))
                        st.hour_pnl_1000          = float(s.get("hour_pnl_1000", 0.0))
                        st.hour_start_ts_1000     = float(s.get("hour_start_ts_1000", 0.0))
                        # Restore arm assignments from persisted values (counter-based, not cid)
                        st.k1000_arm_dur_s   = int(s.get("k1000_arm_dur_s", 240))
                        st.k1000_arm_hold_s  = int(s.get("k1000_arm_hold_s", 0))
                        st.k1000_arm_k       = int(s.get("k1000_arm_k", 0))
                        st.k1000_arm_counter = int(s.get("k1000_arm_counter", 0))
                        st.phase              = "OPEN"
                        st.profit_positive_ts = 0.0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s RESTAURADO: phase=%s cid=%s arm=%d/%d",
                            sym, st.k1000_phase, contract_id,
                            st.k1000_arm_dur_s, st.k1000_arm_hold_s,
                        )
                        continue

                # Sin contrato — restaurar k1000_phase
                _st = self._states[sym]
                _k1000_ph_raw = s.get("k1000_phase", "WAIT")
                _K1000_PHASE_MAP = {
                    "WAIT_SPIKE": "WAIT", "COOLING": "WAIT",
                    "WINDOW": "IN_CONTRACT",
                    "SCOUT": "WAIT", "STAKE_20": "WAIT",
                }
                if _k1000_ph_raw in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
                    _st.k1000_phase = _k1000_ph_raw
                else:
                    _st.k1000_phase = _K1000_PHASE_MAP.get(_k1000_ph_raw, "WAIT")
                _st.k1000_phase_ts         = float(s.get("k1000_phase_ts", now))
                _st.k1000_spike_hold_until = float(s.get("k1000_spike_hold_until", 0.0))
                _st.last_spike_ts          = float(s.get("last_spike_ts", 0.0))
                _st.k1000_had_spike        = bool(s.get("k1000_had_spike", False))
                _st.day_pnl_1000           = float(s.get("day_pnl_1000", 0.0))
                _st.day_start_ts_1000      = float(s.get("day_start_ts_1000", 0.0))
                _st.hour_pnl_1000          = float(s.get("hour_pnl_1000", 0.0))
                _st.hour_start_ts_1000     = float(s.get("hour_start_ts_1000", 0.0))
                _st.k1000_arm_counter      = int(s.get("k1000_arm_counter", 0))
                _st.k1000_arm_k            = int(s.get("k1000_arm_k", 0))
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s RESTAURADO sin contrato → phase=%s day_pnl=%.2f counter=%d",
                    sym, _st.k1000_phase, _st.day_pnl_1000, _st.k1000_arm_counter,
                )

        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] restore_from_disk error: %s", exc)

        self._persist(time.time())

    # ── Spike history ─────────────────────────────────────────────────────────

    def _get_power_30min(self, sym: str, now: float) -> float:
        cutoff = now - 1800.0
        return sum(r for t, r in self._states[sym].power_window if t > cutoff)

    def _get_spike_count_30min(self, sym: str, now: float) -> int:
        cutoff = now - 1800.0
        return sum(1 for t, _ in self._states[sym].power_window if t > cutoff)

    def _ed_seed_spike_hist(self) -> None:
        path = self._logs_dir / "deriv_spike_events.json"
        try:
            with open(path) as f:
                content = f.read().strip()
            # Handle double-bracket bug in rolling buffer
            if content.endswith("]]"):
                content = content[:-1]
            events = json.loads(content)
            if not isinstance(events, list):
                return
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

    # ── Logging por contrato ──────────────────────────────────────────────────

    def _ed_save_open(self, sym: str, stake: float, now: float,
                      side: str = "", cid: Optional[int] = None,
                      dur_s: int = 0, hold_s: int = 0,
                      entry_price: float = 0.0,
                      arm_k: int = -1, arm_counter: int = -1) -> None:
        idle_before_s = 0.0
        _prev_close = self._last_close_ts.get(sym, 0.0)
        if _prev_close > 0:
            idle_before_s = round(now - _prev_close, 1)
        try:
            atr_at_open = float(self._risk.get_last_atr(sym) or 0.0)
        except AttributeError:
            atr_at_open = 0.0
        self._ed_open_info[sym] = {
            "stake":          stake,
            "t_open":         now,
            "hour_utc":       int(now // 3600) % 24,
            "side":           side,
            "contract_id":    cid,
            "arm_dur_s":      dur_s,
            "arm_hold_s":     hold_s,
            "atr_at_open":    round(atr_at_open, 6),
            "idle_before_s":  idle_before_s,
            "entry_price":    entry_price,
            "arm_k":          arm_k,
            "arm_counter":    arm_counter,
            "ctx":            self._ed_ctx(sym, now),
            "pre_open_spike_ts": float(self._risk.get_last_spike_ts(sym) or 0.0),
        }

    def _ed_log(self, sym: str, pnl: float, had_spk: bool, rec_lvl: int,
                now: float, close_type: str = "UNKNOWN") -> None:
        info  = self._ed_open_info.pop(sym, {})
        state = self._states[sym]
        ctx_open = info.get("ctx", {})
        t_open   = info.get("t_open", now)

        h_sym = self._ed_spike_hist.get(sym, [])
        n_spikes_contract = sum(1 for t, _ in h_sym if t_open <= t <= now)

        spike_ts_first  = getattr(state, "k1000_spike_ts_first", 0.0)
        hold_applied_s  = round(now - spike_ts_first, 1) if spike_ts_first > 0 else 0.0
        spike_details   = list(getattr(state, "k1000_spike_details", []))

        rec = {
            # Identificación
            "ts":            round(now, 1),
            "sym":           sym,
            "contract_id":   info.get("contract_id"),
            "strategy":      "K1000_FACTORIAL",
            # Brazos factoriales — v2 usa counter propio, no contract_id % 8
            "exp_version":   "v2",
            "arm_k":         info.get("arm_k", -1),
            "arm_counter":   info.get("arm_counter", -1),
            "arm_dur_s":     info.get("arm_dur_s", getattr(state, "k1000_arm_dur_s", 0)),
            "arm_hold_s":    info.get("arm_hold_s", getattr(state, "k1000_arm_hold_s", 0)),
            # Contrato
            "stake":         info.get("stake", 0.0),
            "multiplier":    MULTIPLIER,
            "side":          info.get("side", ""),
            "opened_at_ts":  round(t_open, 3),
            "closed_at_ts":  round(now, 3),
            "dur_s":         round(now - t_open, 1),
            "idle_before_s": info.get("idle_before_s", 0.0),
            "entry_price":   info.get("entry_price", 0.0),
            "exit_price":    self._get_closed_prices(info.get("contract_id"))[1],
            "pnl":           round(pnl, 4),
            "win":           pnl > 0,
            "close_reason":  close_type,
            "close_type":    close_type,   # legacy alias
            # Spikes
            "n_spikes":              n_spikes_contract,
            "spikes_during_contract":n_spikes_contract,   # legacy
            "had_spike":             had_spk,
            "profit_at_spike":       round(getattr(state, "k1000_profit_at_spike", 0.0), 4),
            "spike_ts":              round(spike_ts_first, 3),
            "hold_applied_s":        hold_applied_s,
            "spike_jumps":           [d.get("jump", 0.0) for d in spike_details],
            "spike_ratios":          [d.get("ratio", 0.0) for d in spike_details],
            "spike_offsets_s":       [d.get("offset_s", 0.0) for d in spike_details],
            # ATR y gap
            "atr_at_open":   info.get("atr_at_open", 0.0),
            "open_gap_s":    ctx_open.get("gap_s", -1.0),
            # Contexto at-open (herencia)
            "rec_lvl":       0,
            "hour_utc":      info.get("hour_utc", int(now // 3600) % 24),
            **{f"open_{k}": v for k, v in ctx_open.items()},
            **{f"close_{k}": v for k, v in self._ed_ctx(sym, now).items()},
        }

        path = self._logs_dir / "deriv_ed_analysis.jsonl"
        try:
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

        # Actualizar timestamp de último cierre para idle_before_s del siguiente contrato
        self._last_close_ts[sym] = now

        # Reset spike tracking para el próximo contrato
        state.k1000_spike_details   = []
        state.k1000_profit_at_spike = 0.0
        state.k1000_spike_ts_first  = 0.0

    def _check_sym_pnl_gate(self, sym: str, now: float) -> None:
        pass

    def _add_global_pnl(self, sym: str, profit: float, now: float) -> None:
        pass

    def _persist(self, now: float) -> None:
        try:
            snapshot = self.get_state_snapshot()
            payload  = json.dumps(snapshot, indent=2)
            self._state_file.write_text(payload)
        except Exception as exc:
            import traceback
            _LOGGER.error("[ENTRADA_DIEGO] persist FAILED: %s\n%s", exc, traceback.format_exc())
