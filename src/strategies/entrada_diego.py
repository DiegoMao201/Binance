"""
entrada_diego.py — Segunda línea de apertura autónoma.

CRASH500 / BOOM500 — lógica QUIET/ACTIVE:
  • QUIET ($5): símbolo quieto (sin spikes, max_holds). Espera señal de vida.
  • ACTIVE ($40): símbolo normalizado (spikes activos). Capitaliza el movimiento.
  • Transiciones:
      QUIET  + 1 WIN           → ACTIVE ($40)   ← símbolo despertó, estructurar
      ACTIVE + 2 max_holds seg → QUIET  ($5)    ← símbolo se quietó, defender
      ACTIVE + 1 max_hold      → sigue ACTIVE    ← un miss no es señal de quietud
  • profit+ → PROFIT_TIMER 3min → cierra → transición QUIET/ACTIVE según estado
  • max_hold → cierra → reabre con stake según modo actual
  • Spike durante PROFIT_TIMER → resetea los 3 min (rider de movimiento)

CRASH1000 / BOOM1000:
  • Abre apenas arranca o tras cualquier cierre — nunca fuera del mercado
  • max_hold → cierra → reabre stake martingale ($10→$20→$20→$40→$40)
    Si reopen≥DEEP_PAUSE_AT=8: → REST MODE $2 (sin escalar más)
    Si ya en REST MODE: sigue en $2 (reopens se resetea, no escala)
  • profit+ → PROFIT_TIMER 3min → cierra:
    - Win normal → REST MODE $2 (spikes ocurren durante descanso, los captura)
    - Win desde REST MODE → $10 normal (martingale reinicia)
  • Spike durante PROFIT_TIMER → resetea los 3 min
  • REST MODE: stake=$2, max_holds no escalan. Sale al ganar.

Activación: env ENTRADA_DIEGO_ENABLED=true
Estado:     {BOT_STATE_DIR}/entrada_diego_state.json
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("entrada_diego")

SYMBOLS_500  = {"CRASH500",  "BOOM500"}
SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_R    = set()   # R_75 y JD75 suspendidos — no estudiados aún
SYMBOLS_ED   = SYMBOLS_500 | SYMBOLS_1000 | SYMBOLS_R

_STAKE_LADDER_1000 = [10.0, 20.0, 20.0, 40.0, 40.0]   # reopen #0..4+

MULTIPLIER         = int(os.getenv("ENTRADA_DIEGO_MULTIPLIER",        "200"))
MAX_HOLD_S         = int(os.getenv("ENTRADA_DIEGO_MAX_HOLD_S",        "600"))
PROFIT_WAIT_S      = int(os.getenv("ENTRADA_DIEGO_PROFIT_WAIT_S",     "180"))
DEEP_PAUSE_AT_1000 = int(os.getenv("ENTRADA_DIEGO_DEEP_PAUSE_AT",     "8"))

# 1000s: en vez de COOLDOWN/DEEP PAUSE fuera del mercado → REST MODE a $2 (captura spikes)
REST_STAKE_1000    = float(os.getenv("ENTRADA_DIEGO_REST_STAKE_1000", "2.0"))

# 500s: QUIET/ACTIVE
QUIET_STAKE_500       = float(os.getenv("ENTRADA_DIEGO_QUIET_STAKE",        "5.0"))   # símbolo quieto
ACTIVE_STAKE_500      = float(os.getenv("ENTRADA_DIEGO_ACTIVE_STAKE",       "40.0"))  # símbolo activo
ACTIVE_MAX_HOLDS      = int(os.getenv("ENTRADA_DIEGO_ACTIVE_MAX_HOLDS",     "1"))     # max_holds para → QUIET (1 = cualquier pérdida vuelve a $5)
DISCHARGE_SPIKES_500        = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_SPIKES",              "1"))  # spikes en PROFIT_TIMER = descarga → fuerza QUIET
BOOM500_DISCHARGE_QUIET_SPIKES = int(os.getenv("ENTRADA_DIEGO_BOOM500_DISCHARGE_QUIET_SPIKES", "2"))  # BOOM500 QUIET: ≥2 spikes = no ir a ACTIVE (1 spike sí va)
MIN_WIN_ACTIVE_500    = float(os.getenv("ENTRADA_DIEGO_MIN_WIN_ACTIVE",      "0.10"))  # profit mínimo real para QUIET→ACTIVE (filtra ghosts $0.01)

# CRASH500 en QUIET: cuándo hacer CIERRE INMEDIATO vs esperar 3-min timer
# Si ratio < umbral (spike pequeño) O gap > quiet_period (símbolo quieto) → CIERRE INMEDIATO → ACTIVE $40
# Si ninguna se cumple (spike grande Y spikes recientes) → 3-min timer → queda en QUIET
CRASH500_RATIO_THRESHOLD = float(os.getenv("ENTRADA_DIEGO_CRASH500_RATIO",  "90.0"))
CRASH500_QUIET_PERIOD_S  = int(os.getenv("ENTRADA_DIEGO_CRASH500_QUIET_S",  "1800"))
BOOM500_RATIO_THRESHOLD  = float(os.getenv("ENTRADA_DIEGO_BOOM500_RATIO",   "90.0"))
BOOM500_QUIET_PERIOD_S   = int(os.getenv("ENTRADA_DIEGO_BOOM500_QUIET_S",   "1800"))

# Wins consecutivos en ACTIVE antes de volver a QUIET (proteger capital)
# CRASH500: 2 wins → QUIET (aprovechar momentum, 3er win consecutivo poco probable)
# BOOM500:  2 wins → QUIET (proteger profit, 3er win consecutivo es poco probable)
CRASH500_MAX_WINS_ACTIVE = int(os.getenv("ENTRADA_DIEGO_CRASH500_MAX_WINS", "2"))
BOOM500_MAX_WINS_ACTIVE  = int(os.getenv("ENTRADA_DIEGO_BOOM500_MAX_WINS",  "2"))

# PnL global acumulado: cuando alcanza GLOBAL_PNL_TARGET → pausa GLOBAL_PAUSE_HOURS horas
# PnL suma todos los cierres de BOOM500+CRASH500 (positivos y negativos)
GLOBAL_PNL_TARGET  = float(os.getenv("ENTRADA_DIEGO_GLOBAL_PNL_TARGET",  "60.0"))
GLOBAL_PAUSE_HOURS = float(os.getenv("ENTRADA_DIEGO_GLOBAL_PAUSE_HOURS", "8.0"))

_ED_DISABLED_RAW    = os.getenv("ENTRADA_DIEGO_DISABLED_SYMBOLS", "BOOM1000,CRASH1000")
SYMBOLS_ED_DISABLED = {s.strip().upper() for s in _ED_DISABLED_RAW.split(",") if s.strip()}

# 500s: SL duro — cierra si pierde más de X% del stake (evita pérdidas de max_hold)
# $5 stake → -$0.75 SL | $40 stake → -$6.00 SL
ED_SL_PCT = float(os.getenv("ENTRADA_DIEGO_SL_PCT", "0.15"))

# 500s: Gate de hora UTC para abrir a $40 — análisis de 1354 trades
# Horas con WR ≤ 33% en trades $40 (históricamente malas para cada símbolo)
CRASH500_BAD_HOURS_UTC = frozenset(
    int(h) for h in os.getenv("ENTRADA_DIEGO_CRASH500_BAD_HOURS", "1,2,5,6,8,9,13,22").split(",") if h.strip()
)
BOOM500_BAD_HOURS_UTC = frozenset(
    int(h) for h in os.getenv("ENTRADA_DIEGO_BOOM500_BAD_HOURS", "9,13,15,21,23").split(",") if h.strip()
)
# CRASH500: si el $5 que activó ACTIVE ganó más de este umbral → spike consumido → esperar 1 trade más
CRASH500_MAX_PREV_WIN = float(os.getenv("ENTRADA_DIEGO_CRASH500_MAX_PREV_WIN", "2.0"))

# R_75 / JD75 — bucle simple TP/SL, flip dirección en pérdida
R75_STAKE      = float(os.getenv("ENTRADA_DIEGO_R75_STAKE",      "5.0"))
R75_TP_PCT     = float(os.getenv("ENTRADA_DIEGO_R75_TP_PCT",     "0.30"))   # $1.50 on $5 stake
R75_SL_PCT     = float(os.getenv("ENTRADA_DIEGO_R75_SL_PCT",     "0.40"))   # $2.00 on $5 stake
R75_MAX_HOLD_S = int(os.getenv("ENTRADA_DIEGO_R75_MAX_HOLD_S",   "300"))
R75_COOLDOWN_S = int(os.getenv("ENTRADA_DIEGO_R75_COOLDOWN_S",   "30"))

# Multiplicador por símbolo: R_75 soporta 100x, JD75 soporta {15,30,50,75,150}
_R_MULTIPLIER: dict[str, int] = {
    "R_75":  int(os.getenv("ENTRADA_DIEGO_R75_MULTIPLIER",  "100")),
    "JD75":  int(os.getenv("ENTRADA_DIEGO_JD75_MULTIPLIER", "75")),
}


# ─── State por símbolo ──────────────────────────────────────────────────────

@dataclass
class _SymState:
    phase: str = "IDLE"           # IDLE | OPEN | PROFIT_TIMER | COOLDOWN
    contract_id: Optional[int] = None
    open_ts: float = 0.0
    profit_positive_ts: float = 0.0
    cooldown_until: float = 0.0
    last_spike_ts: float = 0.0
    reopens: int = 0
    last_close_profit: float = 0.0
    current_profit: float = 0.0
    sym_mode: str = "QUIET"         # "QUIET" | "ACTIVE" — solo 500s
    consec_max_holds: int = 0       # max_holds consecutivos mientras ACTIVE — solo 500s
    consec_wins_active: int = 0     # wins consecutivos mientras ACTIVE — solo 500s
    profit_timer_spikes: int = 0    # spikes capturados durante el PROFIT_TIMER actual — solo 500s
    prev_discharge: bool = False    # True si el ACTIVE anterior terminó en descarga; exige timer limpio antes de $40
    rest_mode: bool = False         # True = abriendo a $2 post-profit/deep-pause — solo 1000s
    is_readjusted: bool = False     # True cuando se re-adjuntó a contrato viejo (no abrir PROFIT_TIMER en ACTIVE)
    r75_direction: str = "MULTUP"  # R_75: MULTUP o MULTDOWN; flip si pierde, mantiene si gana

    def remaining_s(self, now: float) -> float:
        if self.phase == "OPEN":
            return max(0.0, MAX_HOLD_S - (now - self.open_ts))
        if self.phase == "PROFIT_TIMER":
            return max(0.0, (self.profit_positive_ts + PROFIT_WAIT_S) - now)
        if self.phase == "COOLDOWN":
            return max(0.0, self.cooldown_until - now)
        return 0.0

    def to_dict(self, now: float) -> dict[str, Any]:
        d: dict[str, Any] = {
            "phase": self.phase,
            "contract_id": self.contract_id,
            "open_ts": round(self.open_ts, 3),
            "profit_positive_ts": round(self.profit_positive_ts, 3),
            "cooldown_until": round(self.cooldown_until, 3),
            "last_spike_ts": round(self.last_spike_ts, 3),
            "reopens": self.reopens,
            "last_close_profit": round(self.last_close_profit, 4),
            "current_profit": round(self.current_profit, 4),
            "remaining_s": round(self.remaining_s(now), 1),
            "sym_mode": self.sym_mode,
            "consec_max_holds": self.consec_max_holds,
            "consec_wins_active": self.consec_wins_active,
            "profit_timer_spikes": self.profit_timer_spikes,
            "prev_discharge": self.prev_discharge,
            "rest_mode": self.rest_mode,
            "r75_direction": self.r75_direction,
        }
        return d


# ─── Clase principal ─────────────────────────────────────────────────────────

class EntradaDiego:

    def __init__(self, executor: Any, risk: Any, logs_dir: Path) -> None:
        self._executor = executor
        self._risk     = risk
        self._logs_dir = logs_dir
        self._state_file = logs_dir / "entrada_diego_state.json"
        self._enabled: bool = str(
            os.getenv("ENTRADA_DIEGO_ENABLED", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}

        self._states: dict[str, _SymState] = {sym: _SymState() for sym in SYMBOLS_ED}
        self._locks:  dict[str, asyncio.Lock] = {sym: asyncio.Lock() for sym in SYMBOLS_ED}
        self._open_lock    = asyncio.Lock()
        self._restore_lock = asyncio.Lock()
        self._restored     = False
        self._global_pnl:              float = 0.0                # PnL acumulado total
        self._global_pause_until:     float = 0.0                # epoch hasta cuando pausados
        self._global_pnl_next_target: float = GLOBAL_PNL_TARGET  # próximo umbral: 60→120→180

        if self._enabled:
            active = sorted(SYMBOLS_ED - SYMBOLS_ED_DISABLED)
            paused = sorted(SYMBOLS_ED_DISABLED)
            _LOGGER.info(
                "[ENTRADA_DIEGO] ACTIVADO | activos=%s paused=%s | "
                "500: QUIET=$%.0f ACTIVE=$%.0f max_holds_to_quiet=%d mult=%dx max_hold=%ds profit_wait=%ds | "
                "1000: ladder=%s rest=$%.0f deep_pause_at=reopen#%d",
                active, paused,
                QUIET_STAKE_500, ACTIVE_STAKE_500, ACTIVE_MAX_HOLDS,
                MULTIPLIER, MAX_HOLD_S, PROFIT_WAIT_S,
                _STAKE_LADDER_1000, REST_STAKE_1000, DEEP_PAUSE_AT_1000,
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
                    self._restored = True
                    # R_75 no recibe ticks del daemon → loop independiente
                    for _r in SYMBOLS_R:
                        if _r not in SYMBOLS_ED_DISABLED:
                            asyncio.create_task(self._r75_independent_loop(_r))
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
        return result

    # ── Máquina de estados ───────────────────────────────────────────────────

    async def _process(self, sym: str, tick: Any) -> None:
        if sym in SYMBOLS_ED_DISABLED:
            return
        if sym in SYMBOLS_R:
            await self._process_r75(sym, tick)
            return
        state = self._states[sym]
        now   = time.time()

        if state.contract_id is not None:
            state.current_profit = self._query_profit(state.contract_id)

        last_spike_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)

        # ── IDLE ──────────────────────────────────────────────────────────────
        if state.phase == "IDLE":
            _LOGGER.info("[ENTRADA_DIEGO] %s IDLE → abriendo inmediato", sym)
            await self._open(sym, state, now)

        # ── OPEN ──────────────────────────────────────────────────────────────
        elif state.phase == "OPEN":

            # 0) SL duro: cortar pérdida antes de max_hold (solo 500s)
            if sym in SYMBOLS_500:
                _stake_now = ACTIVE_STAKE_500 if state.sym_mode == "ACTIVE" else QUIET_STAKE_500
                _sl_dollar = _stake_now * ED_SL_PCT
                if state.current_profit < -_sl_dollar and state.contract_id is not None:
                    try:
                        await self._executor.close_contract(int(state.contract_id))
                    except Exception as exc:
                        _LOGGER.error("[ENTRADA_DIEGO] %s SL_HARD error: %s", sym, exc)
                    state.last_close_profit  = state.current_profit
                    state.current_profit     = 0.0   # evita re-disparo si _open falla
                    state.contract_id        = None
                    state.profit_positive_ts = 0.0
                    state.reopens           += 1
                    self._add_global_pnl(sym, state.last_close_profit, now)
                    if state.sym_mode == "ACTIVE":
                        state.sym_mode           = "QUIET"
                        state.consec_max_holds   = 0
                        state.consec_wins_active = 0
                    next_stake = self._next_stake(sym, state.reopens)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s SL_HARD profit=%.4f < -%.2f → %s $%.0f reopen#%d",
                        sym, state.last_close_profit, _sl_dollar,
                        state.sym_mode, next_stake, state.reopens,
                    )
                    await self._open(sym, state, now)
                    return

            # 1) max_hold expirado
            if now >= state.open_ts + MAX_HOLD_S:
                try:
                    if state.contract_id:
                        await self._executor.close_contract(int(state.contract_id))
                except Exception as exc:
                    _LOGGER.error("[ENTRADA_DIEGO] %s error cerrando max_hold: %s", sym, exc)
                state.last_close_profit  = state.current_profit
                state.contract_id        = None
                state.profit_positive_ts = 0.0
                state.reopens           += 1

                self._add_global_pnl(sym, state.last_close_profit, now)

                # 500s: actualizar modo QUIET/ACTIVE antes de calcular stake del log
                if sym in SYMBOLS_500 and state.sym_mode == "ACTIVE":
                    state.consec_max_holds += 1
                    if state.consec_max_holds >= ACTIVE_MAX_HOLDS:
                        state.sym_mode           = "QUIET"
                        state.consec_max_holds   = 0
                        state.consec_wins_active = 0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s ACTIVE → QUIET $%.0f (%d max_holds consecutivos)",
                            sym, QUIET_STAKE_500, ACTIVE_MAX_HOLDS,
                        )

                next_stake = self._next_stake(sym, state.reopens)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s MAX_HOLD %ds expirado profit=%.4f → reopen#%d stake=$%.0f",
                    sym, MAX_HOLD_S, state.last_close_profit, state.reopens, next_stake,
                )

                # 1000s: rest_mode o deep pause → rest_mode
                if sym in SYMBOLS_1000:
                    if state.rest_mode:
                        state.reopens = 0  # no escalar mientras descansando
                    elif state.reopens >= DEEP_PAUSE_AT_1000:
                        state.rest_mode = True
                        state.reopens   = 0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s → REST MODE $%.0f (reopen#%d sin spike → en posición, no fuera)",
                            sym, REST_STAKE_1000, DEEP_PAUSE_AT_1000,
                        )

                await self._open(sym, state, now)
                return

            # 2) Contrato cerrado externamente
            if state.contract_id is not None and self._query_contract(state.contract_id) is None:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s contrato %s cerrado externamente → reopen#%d",
                    sym, state.contract_id, state.reopens + 1,
                )
                state.last_close_profit = state.current_profit
                state.contract_id       = None
                state.is_readjusted     = False
                state.reopens          += 1

                if sym in SYMBOLS_1000:
                    if state.rest_mode:
                        state.reopens = 0
                    elif state.reopens >= DEEP_PAUSE_AT_1000:
                        state.rest_mode = True
                        state.reopens   = 0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s → REST MODE $%.0f (cierre externo reopen#%d)",
                            sym, REST_STAKE_1000, DEEP_PAUSE_AT_1000,
                        )

                await self._open(sym, state, now)
                return

            # 3) Profit positivo por primera vez → PROFIT_TIMER
            # Excepción: si es un re-adjuntado en ACTIVE, no iniciar PROFIT_TIMER —
            # el contrato viejo cierra vía "cerrado externamente → reopen" y abre el $40 real.
            if state.current_profit > 0 and state.profit_positive_ts == 0.0:
                if state.is_readjusted and sym in SYMBOLS_500 and state.sym_mode == "ACTIVE":
                    pass  # esperar cierre externo del re-adjuntado → reopen real $40
                else:
                    state.profit_positive_ts   = now
                    state.profit_timer_spikes  = 0   # contador limpio al iniciar
                    state.phase = "PROFIT_TIMER"
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s PROFIT POSITIVO %.4f → PROFIT_TIMER %ds",
                        sym, state.current_profit, PROFIT_WAIT_S,
                    )
                    self._persist(now)

        # ── PROFIT_TIMER ──────────────────────────────────────────────────────
        elif state.phase == "PROFIT_TIMER":

            # Cerrado externamente durante profit
            if state.contract_id is not None and self._query_contract(state.contract_id) is None:
                spikes_tag = f" [spikes={state.profit_timer_spikes}]" if sym in SYMBOLS_500 else ""
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s cerrado externo durante PROFIT_TIMER → cierre ganador%s",
                    sym, spikes_tag,
                )
                state.last_close_profit  = max(state.current_profit, 0.01)
                state.contract_id        = None
                state.profit_positive_ts = 0.0
                prev_reopens             = state.reopens
                state.reopens            = 0
                await self._post_profit_close(sym, state, now, prev_reopens=prev_reopens)
                return

            # Spike durante PROFIT_TIMER
            if last_spike_ts > state.last_spike_ts and last_spike_ts > 0 and state.current_profit > 0:
                prev_spike_ts       = state.last_spike_ts   # guardar ANTES de actualizar
                state.last_spike_ts = last_spike_ts

                # BOOM500 y CRASH500 en QUIET: sin CIERRE INMEDIATO
                # Siempre esperar el cooldown completo de 3 minutos.
                # Solo contar spikes grandes para discharge; al vencer el timer:
                #   → si ganó (profit real) = ACTIVE $40 | si no = sigue QUIET $5
                if sym in SYMBOLS_500 and state.sym_mode == "QUIET":
                    spike_ratio  = self._risk.get_last_spike_ratio(sym)
                    ratio_thresh = CRASH500_RATIO_THRESHOLD if sym == "CRASH500" else BOOM500_RATIO_THRESHOLD
                    is_large_spike = spike_ratio >= ratio_thresh

                    state.profit_positive_ts = now  # extender timer
                    if is_large_spike:
                        state.profit_timer_spikes += 1
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s SPIKE en QUIET ratio=%.1fx → timer reset%s (cooldown 3min)",
                        sym, spike_ratio,
                        f" descarga#{state.profit_timer_spikes}" if is_large_spike else "",
                    )
                    self._persist(now)
                    return

                # ACTIVE ($40): resetear timer y seguir rideando + contar descarga (CRASH500)
                state.profit_positive_ts = now
                if sym in SYMBOLS_500:
                    state.profit_timer_spikes += 1
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s SPIKE durante PROFIT_TIMER profit=%.4f → RESET TIMER (%ds)%s",
                    sym, state.current_profit, PROFIT_WAIT_S,
                    f" [descarga spike#{state.profit_timer_spikes}]" if sym in SYMBOLS_500 else "",
                )
                self._persist(now)
                return

            # Timer cumplido → cerrar
            if now >= state.profit_positive_ts + PROFIT_WAIT_S:
                await self._close_profit_timer(sym, state, now)

        # ── COOLDOWN (1000s) ──────────────────────────────────────────────────
        elif state.phase == "COOLDOWN":
            if now >= state.cooldown_until:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s COOLDOWN terminado → abriendo stake=$%.0f",
                    sym, self._next_stake(sym, state.reopens),
                )
                await self._open(sym, state, now)

    # ── R_75: bucle simple TP/SL ─────────────────────────────────────────────

    async def _process_r75(self, sym: str, tick: Any) -> None:
        state = self._states[sym]
        now   = time.time()

        # Actualizar profit solo si el contrato sigue vivo; si ya cerró, preservar
        # el último valor conocido (evita que _query_profit devuelva 0.0 cuando el
        # broker ya liquidó el contrato justo antes de que detectemos el cierre).
        _live_oc = None
        if state.contract_id is not None:
            _live_oc = self._query_contract(state.contract_id)
            if _live_oc is not None:
                state.current_profit = float(_live_oc.get("floating_pnl") or 0.0)

        if state.phase == "IDLE":
            _LOGGER.info("[ENTRADA_DIEGO] %s IDLE → abriendo $%.0f", sym, R75_STAKE)
            await self._open(sym, state, now)

        elif state.phase == "OPEN":
            # Broker cerró (TP o SL alcanzado)
            if state.contract_id is not None and _live_oc is None:
                close_profit = state.current_profit  # último valor conocido (no 0)
                # Flip de dirección: si perdió → invierte, si ganó → mantiene
                if close_profit <= 0:
                    state.r75_direction = "MULTDOWN" if state.r75_direction == "MULTUP" else "MULTUP"
                    _LOGGER.info("[ENTRADA_DIEGO] %s PÉRDIDA %.4f → próxima: %s", sym, close_profit, state.r75_direction)
                else:
                    _LOGGER.info("[ENTRADA_DIEGO] %s GANANCIA %.4f → mantiene: %s", sym, close_profit, state.r75_direction)
                state.last_close_profit = close_profit
                state.contract_id       = None
                state.phase             = "COOLDOWN"
                state.cooldown_until    = now + R75_COOLDOWN_S
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s cerrado profit=%.4f → COOLDOWN %ds",
                    sym, state.last_close_profit, R75_COOLDOWN_S,
                )
                self._persist(now)
                return
            # Solo SL/TP del broker cierran R_75 — no max_hold ni reglas extra

        elif state.phase == "COOLDOWN":
            if now >= state.cooldown_until:
                state.phase = "IDLE"
                _LOGGER.info("[ENTRADA_DIEGO] %s COOLDOWN terminado → IDLE", sym)
                await self._open(sym, state, now)

    async def _r75_independent_loop(self, sym: str) -> None:
        _LOGGER.info("[ENTRADA_DIEGO] %s loop independiente iniciado (tick interval 5s)", sym)
        while self._enabled and sym not in SYMBOLS_ED_DISABLED:
            try:
                async with self._locks[sym]:
                    await self._process_r75(sym, None)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s loop error: %s", sym, exc)
            await asyncio.sleep(5)
        _LOGGER.info("[ENTRADA_DIEGO] %s loop independiente terminado", sym)

    # ── Helpers de flujo ─────────────────────────────────────────────────────

    async def _post_profit_close(
        self, sym: str, state: _SymState, now: float, prev_reopens: int = 0
    ) -> None:
        if sym in SYMBOLS_500:
            spikes = state.profit_timer_spikes
            state.profit_timer_spikes = 0  # siempre resetear al cerrar

            profit = state.last_close_profit

            # DESCARGA: spikes en PROFIT_TIMER = mercado agotó la energía → no escalar
            # Aplica en ACTIVE (obvia) y en QUIET (si hubo spikes grandes, no ir a ACTIVE)
            # Post-descarga: si el ACTIVE anterior terminó en descarga, threshold baja a 1 para
            # el siguiente ciclo QUIET — cualquier spike grande = sigue QUIET (protege el $40).
            quiet_discharge_thresh = (
                DISCHARGE_SPIKES_500          # =1: post-descarga → 1 spike basta para bloquear $40
                if state.prev_discharge
                else BOOM500_DISCHARGE_QUIET_SPIKES  # normal: ≥2 spikes bloquean
            )
            is_discharge       = spikes >= DISCHARGE_SPIKES_500 and state.sym_mode == "ACTIVE"
            is_discharge_quiet = spikes >= quiet_discharge_thresh and state.sym_mode == "QUIET"

            if is_discharge:
                # Desde ACTIVE con spikes → QUIET $5; marcar: siguiente ciclo QUIET exige timer limpio
                state.sym_mode           = "QUIET"
                state.consec_max_holds   = 0
                state.consec_wins_active = 0
                state.prev_discharge     = True
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → DESCARGA (%d spike) → QUIET $%.0f "
                    "(ACTIVE, mercado agotado — próximo ciclo QUIET exige timer limpio)",
                    sym, profit, spikes, QUIET_STAKE_500,
                )
            elif is_discharge_quiet:
                # Desde QUIET con spikes (thresh=%d) → sigue QUIET $5; resetear flag (ciclo QUIET consumido)
                state.prev_discharge = False
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → DESCARGA en QUIET (%d spikes, thresh=%d) → "
                    "sigue QUIET $%.0f%s",
                    sym, profit, spikes, quiet_discharge_thresh, QUIET_STAKE_500,
                    " [post-descarga]" if quiet_discharge_thresh == DISCHARGE_SPIKES_500 else "",
                )
            elif profit < MIN_WIN_ACTIVE_500:
                # Ghost close ($0.01) — si estamos en ACTIVE, vuelve a QUIET:
                # el cerrado externo desperdicia el momentum del spike y el segundo
                # $40 abre en frío → max_hold garantizado. Cortar el ciclo aquí.
                if state.sym_mode == "ACTIVE":
                    state.sym_mode           = "QUIET"
                    state.consec_wins_active = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → GHOST en ACTIVE → QUIET $%.0f "
                        "(cerrado externo mata momentum, corta ciclo)",
                        sym, profit, QUIET_STAKE_500,
                    )
                else:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → GHOST (< $%.2f) → modo sin cambio [%s]",
                        sym, profit, MIN_WIN_ACTIVE_500, state.sym_mode,
                    )
            elif state.sym_mode == "QUIET":
                # Win real desde QUIET con timer limpio → símbolo despertó → ACTIVE
                state.sym_mode           = "ACTIVE"
                state.consec_max_holds   = 0
                state.consec_wins_active = 0
                state.prev_discharge     = False  # ciclo limpio completado
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → QUIET→ACTIVE $%.0f (símbolo despertó)",
                    sym, profit, ACTIVE_STAKE_500,
                )
            else:
                # Win real en ACTIVE → contar wins consecutivos
                state.consec_wins_active += 1
                state.consec_max_holds    = 0
                max_wins = CRASH500_MAX_WINS_ACTIVE if sym == "CRASH500" else BOOM500_MAX_WINS_ACTIVE
                if state.consec_wins_active >= max_wins:
                    # Umbral de wins alcanzado → QUIET $5 (proteger capital)
                    state.sym_mode           = "QUIET"
                    state.consec_wins_active = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → %d WIN(s) ACTIVE → QUIET $%.0f "
                        "(capital protegido)",
                        sym, profit, max_wins, QUIET_STAKE_500,
                    )
                else:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → ACTIVE sigue $%.0f "
                        "(win#%d/%d)",
                        sym, profit, ACTIVE_STAKE_500,
                        state.consec_wins_active, max_wins,
                    )
            await self._open(sym, state, now)

        else:
            # 1000s
            state.reopens = 0
            if state.rest_mode:
                # Win desde REST MODE → salir a $10 normal
                state.rest_mode = False
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → REST WIN → $10 martingale normal",
                    sym, state.last_close_profit,
                )
            else:
                # Win normal → entrar en REST MODE a $2 (en posición, no fuera)
                state.rest_mode = True
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → REST MODE $%.0f (spikes en posición%s)",
                    sym, state.last_close_profit, REST_STAKE_1000,
                    f" | venia de reopen#{prev_reopens}" if prev_reopens >= 3 else "",
                )
            await self._open(sym, state, now)

    # ── Operaciones de contrato ───────────────────────────────────────────────

    def _gate_active(self, sym: str, state: "_SymState") -> bool:
        """True = puede abrir $40. False = abrir $5 aunque esté en ACTIVE."""
        hour = datetime.now(timezone.utc).hour
        # Gate 1: hora mala según análisis histórico de 1354 trades
        if sym == "CRASH500" and hour in CRASH500_BAD_HOURS_UTC:
            _LOGGER.info("[ENTRADA_DIEGO] %s HORA_BLOQUEADA %dh UTC → $5 (WR hist. ≤33%%)", sym, hour)
            return False
        if sym == "BOOM500" and hour in BOOM500_BAD_HOURS_UTC:
            _LOGGER.info("[ENTRADA_DIEGO] %s HORA_BLOQUEADA %dh UTC → $5 (WR hist. ≤33%%)", sym, hour)
            return False
        # Gate 2 (CRASH500): spike previo muy grande → energía consumida, esperar
        if sym == "CRASH500" and state.last_close_profit > CRASH500_MAX_PREV_WIN:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s SPIKE_CONSUMIDO prev_profit=%.2f > %.1f → $5 (WR hist. 27%%)",
                sym, state.last_close_profit, CRASH500_MAX_PREV_WIN,
            )
            return False
        return True

    def _next_stake(self, sym: str, reopens: int = 0, now: float = 0.0) -> float:
        if sym in SYMBOLS_500:
            state = self._states[sym]
            if state.sym_mode == "ACTIVE" and self._gate_active(sym, state):
                return ACTIVE_STAKE_500
            return QUIET_STAKE_500
        if sym in SYMBOLS_R:
            return R75_STAKE
        state = self._states[sym]
        if state.rest_mode:
            return REST_STAKE_1000
        ladder = _STAKE_LADDER_1000
        return ladder[min(reopens, len(ladder) - 1)]

    async def _open(self, sym: str, state: _SymState, now: float, stake_override: float | None = None) -> None:
        # Pausa global por PnL: no abrir hasta que venza el timer
        if self._global_pause_until > 0 and now < self._global_pause_until:
            remaining_h = (self._global_pause_until - now) / 3600
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s GLOBAL_PNL_PAUSE — %.1fh restantes → COOLDOWN",
                sym, remaining_h,
            )
            state.phase         = "COOLDOWN"
            state.cooldown_until = self._global_pause_until
            self._persist(now)
            return
        # Pausa expiró → limpiar
        if self._global_pause_until > 0 and now >= self._global_pause_until:
            _LOGGER.info("[ENTRADA_DIEGO] GLOBAL_PNL_PAUSE expiró → reanudando operación")
            self._global_pause_until = 0.0

        from src.execution.deriv_trader import DerivOrder
        if sym in SYMBOLS_R:
            side = state.r75_direction
        else:
            side = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        stake = stake_override if stake_override is not None else self._next_stake(sym, state.reopens, now)
        mode_tag = state.sym_mode if sym in SYMBOLS_500 else ("REST" if state.rest_mode else "normal")
        if sym in SYMBOLS_R:
            _mult = _R_MULTIPLIER.get(sym, 100)
            _mult, _sl, _tp, _mh = _mult, R75_SL_PCT, R75_TP_PCT, float(R75_MAX_HOLD_S)
        else:
            _mult, _sl, _tp, _mh = MULTIPLIER, 0.65, 0.65, float(MAX_HOLD_S)
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s ABRIENDO %s $%.2f mult=%dx max_hold=%ds (reopen#%d mode=%s)",
            sym, side, stake, _mult, int(_mh), state.reopens, mode_tag,
        )
        try:
            async with self._open_lock:
                order = DerivOrder(
                    symbol=sym,
                    side=side,
                    stake_usdt=stake,
                    multiplier=_mult,
                    stop_loss_pct=_sl,
                    take_profit_pct=_tp,
                    max_hold_seconds=_mh,
                    score_breakdown={
                        "quality_tier": "entrada_diego",
                        "setup":        "entrada_diego",
                        "grade":        "ED",
                        "score":        0.0,
                        "entrada_diego": True,
                        "skip_dpm":     sym in SYMBOLS_R,
                    },
                )
                result = await self._executor.execute(order)

            if result.get("status") == "live":
                cid   = result.get("contract_id")
                entry = result.get("entry_price", 0.0)
                state.contract_id           = int(cid) if cid else None
                state.open_ts               = now
                state.profit_positive_ts    = 0.0
                state.current_profit        = 0.0
                state.profit_timer_spikes   = 0
                state.is_readjusted         = False
                state.phase                 = "OPEN"
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s OPEN OK contract=%s entry=%.5f stake=$%.2f",
                    sym, state.contract_id, entry, stake,
                )
            elif result.get("status") == "symbol_already_open":
                existing = result.get("open_contracts", [])
                if existing:
                    cid = int(existing[0])
                    state.contract_id        = cid
                    state.open_ts            = now
                    state.profit_positive_ts = 0.0
                    state.current_profit     = 0.0
                    state.is_readjusted      = True
                    state.phase              = "OPEN"
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s re-adjuntado a contrato existente %s",
                        sym, cid,
                    )
                else:
                    _LOGGER.warning("[ENTRADA_DIEGO] %s symbol_already_open sin contract → IDLE", sym)
                    state.phase = "IDLE"
            else:
                _LOGGER.warning("[ENTRADA_DIEGO] %s OPEN FAILED: %s → IDLE", sym, result)
                state.phase = "IDLE"

        except Exception as exc:
            exc_str = str(exc)
            if "LimitOrderAmountTooHigh" in exc_str:
                m = re.search(r"'code_args':\s*\['([\d.]+)'\]", exc_str)
                if m:
                    max_allowed = float(m.group(1))
                    retry_stake = round(max_allowed * 0.95, 2)
                    if max_allowed >= 1.0 and (stake_override is None or retry_stake < stake_override):
                        _LOGGER.warning(
                            "[ENTRADA_DIEGO] %s stake=$%.2f rechazado (max=%.2f) → reintento $%.2f",
                            sym, stake, max_allowed, retry_stake,
                        )
                        await self._open(sym, state, now, stake_override=retry_stake)
                        return
            _LOGGER.error("[ENTRADA_DIEGO] %s error _open: %s → IDLE", sym, exc)
            state.phase = "IDLE"

        self._persist(now)

    async def _close_profit_timer(self, sym: str, state: _SymState, now: float) -> None:
        final_profit = state.current_profit
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s PROFIT_TIMER cumplido → cerrando contract=%s profit=%.4f",
            sym, state.contract_id, final_profit,
        )
        try:
            if state.contract_id:
                await self._executor.close_contract(int(state.contract_id))
        except Exception as exc:
            _LOGGER.error("[ENTRADA_DIEGO] %s error al cerrar: %s", sym, exc)

        state.last_close_profit  = final_profit
        state.contract_id        = None
        state.profit_positive_ts = 0.0

        self._add_global_pnl(sym, final_profit, now)

        if final_profit > 0:
            prev_reopens  = state.reopens
            state.reopens = 0
            await self._post_profit_close(sym, state, now, prev_reopens=prev_reopens)
        else:
            state.reopens += 1
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s CIERRE PROFIT- %.4f → reopen#%d",
                sym, final_profit, state.reopens,
            )
            await self._open(sym, state, now)

        self._persist(now)

    # ── Query helpers ─────────────────────────────────────────────────────────

    def _query_contract(self, contract_id: int) -> Optional[dict[str, Any]]:
        try:
            for oc in self._executor.get_open_contracts_for_status():
                if oc.get("contract_id") == contract_id:
                    return oc
        except Exception:
            pass
        return None

    def _query_profit(self, contract_id: int) -> float:
        try:
            oc = self._query_contract(contract_id)
            if oc:
                return float(oc.get("floating_pnl") or 0.0)
        except Exception:
            pass
        return 0.0

    # ── Restaurar estado post-restart ─────────────────────────────────────────

    async def _restore_from_disk(self) -> None:
        try:
            if not self._state_file.exists():
                return
            data = json.loads(self._state_file.read_text())
            now  = time.time()

            # Restaurar estado global PnL
            self._global_pnl = float(data.get("global_pnl", 0.0))
            self._global_pnl_next_target = float(
                data.get("global_pnl_next_target", GLOBAL_PNL_TARGET)
            )
            raw_pause = float(data.get("global_pause_until", 0.0))
            if raw_pause > now:
                self._global_pause_until = raw_pause
                _LOGGER.info(
                    "[ENTRADA_DIEGO] GLOBAL_PNL_PAUSE restaurado — %.1fh restantes "
                    "(acumulado=$%.2f, próximo target=$%.0f)",
                    (raw_pause - now) / 3600, self._global_pnl, self._global_pnl_next_target,
                )
            else:
                self._global_pause_until = 0.0  # expiró mientras el container estaba apagado

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
                        if sym in SYMBOLS_500:
                            st.sym_mode           = s.get("sym_mode", "QUIET")
                            st.consec_max_holds   = int(s.get("consec_max_holds", 0))
                            st.consec_wins_active = int(s.get("consec_wins_active", 0))
                            st.prev_discharge     = bool(s.get("prev_discharge", False))
                        if sym in SYMBOLS_1000:
                            st.rest_mode = bool(s.get("rest_mode", False))
                        if phase == "PROFIT_TIMER" and float(s.get("profit_positive_ts", 0.0)) > 0:
                            st.phase              = "PROFIT_TIMER"
                            st.profit_positive_ts = float(s["profit_positive_ts"])
                        else:
                            st.phase              = "OPEN"
                            st.profit_positive_ts = 0.0
                        if sym in SYMBOLS_R:
                            st.r75_direction = s.get("r75_direction", "MULTUP")
                        mode_tag = f" [{st.sym_mode} max_holds={st.consec_max_holds} wins={st.consec_wins_active}]" if sym in SYMBOLS_500 else ""
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s RESTAURADO: phase=%s contract=%s reopens=%d%s",
                            sym, st.phase, contract_id, st.reopens, mode_tag,
                        )
                        continue

                # Sin contrato — restaurar COOLDOWN legacy (transición de versión anterior)
                # o abrir en rest_mode si así quedó guardado
                if phase == "COOLDOWN" and float(s.get("cooldown_until", 0.0)) > now:
                    cooldown_until = float(s["cooldown_until"])
                    st = self._states[sym]
                    st.phase          = "COOLDOWN"
                    st.cooldown_until = cooldown_until
                    st.reopens        = reopens
                    if sym in SYMBOLS_R:
                        st.r75_direction = s.get("r75_direction", "MULTUP")
                    _next = REST_STAKE_1000 if sym in SYMBOLS_1000 else (R75_STAKE if sym in SYMBOLS_R else QUIET_STAKE_500)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: COOLDOWN %.0fs restantes → abrirá en $%.0f",
                        sym, cooldown_until - now, _next,
                    )
                    # Convertir COOLDOWN legacy a rest_mode en cuanto termine el timer
                    if sym in SYMBOLS_1000:
                        st.rest_mode = True
                    continue

                # Sin contrato y sin COOLDOWN vigente — verificar si rest_mode persistido
                if sym in SYMBOLS_1000 and bool(s.get("rest_mode", False)):
                    st = self._states[sym]
                    st.rest_mode = True
                    st.reopens   = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: rest_mode=True → abrirá a $%.0f",
                        sym, REST_STAKE_1000,
                    )
                    continue

                _LOGGER.info("[ENTRADA_DIEGO] %s startup → IDLE → abre inmediato", sym)

        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] restore_from_disk error: %s", exc)

        self._persist(time.time())

    def _add_global_pnl(self, sym: str, profit: float, now: float) -> None:
        if sym not in SYMBOLS_500:
            return
        self._global_pnl += profit
        if self._global_pnl >= self._global_pnl_next_target and self._global_pause_until == 0.0:
            self._global_pause_until      = now + GLOBAL_PAUSE_HOURS * 3600
            prev_target                   = self._global_pnl_next_target
            self._global_pnl_next_target += GLOBAL_PNL_TARGET   # 60→120→180→...
            _LOGGER.info(
                "[ENTRADA_DIEGO] GLOBAL_PNL $%.2f >= target $%.0f → PAUSA %.0fh "
                "(próximo target=$%.0f, reanuda %s UTC)",
                self._global_pnl, prev_target, GLOBAL_PAUSE_HOURS,
                self._global_pnl_next_target,
                __import__("datetime").datetime.utcfromtimestamp(
                    self._global_pause_until
                ).strftime("%Y-%m-%d %H:%M"),
            )

    def _persist(self, now: float) -> None:
        try:
            self._state_file.write_text(json.dumps(self.get_state_snapshot(), indent=2))
        except Exception as exc:
            _LOGGER.debug("[ENTRADA_DIEGO] persist error: %s", exc)
