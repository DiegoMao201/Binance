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
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("entrada_diego")

SYMBOLS_500  = {"CRASH500",  "BOOM500"}
SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_ED   = SYMBOLS_500 | SYMBOLS_1000

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
DISCHARGE_SPIKES_500  = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_SPIKES",     "1"))     # spikes en PROFIT_TIMER = descarga → fuerza QUIET
MIN_WIN_ACTIVE_500    = float(os.getenv("ENTRADA_DIEGO_MIN_WIN_ACTIVE",      "0.10"))  # profit mínimo real para QUIET→ACTIVE (filtra ghosts $0.01)

# CRASH500 en QUIET: cuándo hacer CIERRE INMEDIATO vs esperar 3-min timer
# Si ratio < umbral (spike pequeño) O gap > 5min (símbolo quieto) → CIERRE INMEDIATO → ACTIVE $40
# Si ninguna se cumple (spike grande Y spikes recientes) → 3-min timer → queda en QUIET
CRASH500_RATIO_THRESHOLD = float(os.getenv("ENTRADA_DIEGO_CRASH500_RATIO",  "90.0"))  # spike < 90x = pequeño → CIERRE INMEDIATO
CRASH500_QUIET_PERIOD_S  = int(os.getenv("ENTRADA_DIEGO_CRASH500_QUIET_S",  "1800"))   # 30min sin spike = símbolo quieto → CIERRE INMEDIATO

# Wins consecutivos en ACTIVE antes de volver a QUIET (proteger capital)
# CRASH500: 1 win → QUIET (mercado agotado, ciclo corto)
# BOOM500:  2 wins → QUIET (proteger profit, 3er win consecutivo es poco probable)
CRASH500_MAX_WINS_ACTIVE = int(os.getenv("ENTRADA_DIEGO_CRASH500_MAX_WINS", "1"))
BOOM500_MAX_WINS_ACTIVE  = int(os.getenv("ENTRADA_DIEGO_BOOM500_MAX_WINS",  "2"))

_ED_DISABLED_RAW    = os.getenv("ENTRADA_DIEGO_DISABLED_SYMBOLS", "BOOM1000,CRASH1000")
SYMBOLS_ED_DISABLED = {s.strip().upper() for s in _ED_DISABLED_RAW.split(",") if s.strip()}


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
    rest_mode: bool = False         # True = abriendo a $2 post-profit/deep-pause — solo 1000s
    is_readjusted: bool = False     # True cuando se re-adjuntó a contrato viejo (no abrir PROFIT_TIMER en ACTIVE)

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
            "rest_mode": self.rest_mode,
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
        async with self._locks[sym]:
            await self._process(sym, tick)

    def get_state_snapshot(self) -> dict[str, Any]:
        now = time.time()
        result: dict[str, Any] = {"updated_at": now, "enabled": self._enabled}
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
                state.last_spike_ts = last_spike_ts

                # CRASH500 en QUIET: CIERRE INMEDIATO solo si spike pequeño O lleva mucho sin spike
                # BOOM500 en QUIET: timer normal (spike → resetea 3min, cierra al vencer → ACTIVE $40)
                if sym == "CRASH500" and state.sym_mode == "QUIET":
                    spike_ratio = self._risk.get_last_spike_ratio(sym)
                    time_since_prev = (last_spike_ts - state.last_spike_ts
                                       if state.last_spike_ts > 0 else float("inf"))
                    is_small_spike  = 0 < spike_ratio < CRASH500_RATIO_THRESHOLD
                    is_quiet_period = time_since_prev > CRASH500_QUIET_PERIOD_S
                    if is_small_spike or is_quiet_period:
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] CRASH500 SPIKE en QUIET ratio=%.1fx gap=%.0fs → "
                            "CIERRE INMEDIATO → ACTIVE $%.0f (%s)",
                            spike_ratio, time_since_prev, ACTIVE_STAKE_500,
                            "ratio<%.0f" % CRASH500_RATIO_THRESHOLD if is_small_spike else "quieto>%.0fs" % CRASH500_QUIET_PERIOD_S,
                        )
                        await self._close_profit_timer(sym, state, now)
                        return
                    else:
                        # Spike grande + spikes recientes → 3-min timer (descarga, no saltar a $40)
                        state.profit_positive_ts = now
                        state.profit_timer_spikes += 1
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] CRASH500 SPIKE en QUIET ratio=%.1fx gap=%.0fs → "
                            "3-min timer (spike grande+reciente, descarga spike#%d)",
                            spike_ratio, time_since_prev, state.profit_timer_spikes,
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

    # ── Helpers de flujo ─────────────────────────────────────────────────────

    async def _post_profit_close(
        self, sym: str, state: _SymState, now: float, prev_reopens: int = 0
    ) -> None:
        if sym in SYMBOLS_500:
            spikes = state.profit_timer_spikes
            state.profit_timer_spikes = 0  # siempre resetear al cerrar

            profit = state.last_close_profit

            # DESCARGA solo aplica a CRASH500 desde ACTIVE ($40) — spike = mercado agotó la energía
            # Si ya éramos QUIET ($5), el spike fue capturado barato; al vencer el timer → ACTIVE
            is_discharge = sym == "CRASH500" and spikes >= DISCHARGE_SPIKES_500 and state.sym_mode == "ACTIVE"

            if is_discharge:
                # CRASH500: spike en PROFIT_TIMER desde ACTIVE = mercado descargó → QUIET $5
                prev_mode = state.sym_mode
                state.sym_mode           = "QUIET"
                state.consec_max_holds   = 0
                state.consec_wins_active = 0
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → DESCARGA (%d spike) → QUIET $%.0f "
                    "(era %s, mercado agotado)",
                    sym, profit, spikes, QUIET_STAKE_500, prev_mode,
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
                # Win real desde QUIET → símbolo despertó → ACTIVE (contador de wins parte en 0)
                state.sym_mode           = "ACTIVE"
                state.consec_max_holds   = 0
                state.consec_wins_active = 0
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

    def _next_stake(self, sym: str, reopens: int = 0, now: float = 0.0) -> float:
        if sym in SYMBOLS_500:
            state = self._states[sym]
            return ACTIVE_STAKE_500 if state.sym_mode == "ACTIVE" else QUIET_STAKE_500
        state = self._states[sym]
        if state.rest_mode:
            return REST_STAKE_1000
        ladder = _STAKE_LADDER_1000
        return ladder[min(reopens, len(ladder) - 1)]

    async def _open(self, sym: str, state: _SymState, now: float, stake_override: float | None = None) -> None:
        from src.execution.deriv_trader import DerivOrder
        side  = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        stake = stake_override if stake_override is not None else self._next_stake(sym, state.reopens, now)
        mode_tag = state.sym_mode if sym in SYMBOLS_500 else ("REST" if state.rest_mode else "normal")
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s ABRIENDO %s $%.2f mult=%dx max_hold=%ds (reopen#%d mode=%s)",
            sym, side, stake, MULTIPLIER, MAX_HOLD_S, state.reopens, mode_tag,
        )
        try:
            async with self._open_lock:
                order = DerivOrder(
                    symbol=sym,
                    side=side,
                    stake_usdt=stake,
                    multiplier=MULTIPLIER,
                    stop_loss_pct=0.65,
                    take_profit_pct=0.65,
                    max_hold_seconds=float(MAX_HOLD_S),
                    score_breakdown={
                        "quality_tier": "entrada_diego",
                        "setup":        "entrada_diego",
                        "grade":        "ED",
                        "score":        0.0,
                        "entrada_diego": True,
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
                        if sym in SYMBOLS_1000:
                            st.rest_mode = bool(s.get("rest_mode", False))
                        if phase == "PROFIT_TIMER" and float(s.get("profit_positive_ts", 0.0)) > 0:
                            st.phase              = "PROFIT_TIMER"
                            st.profit_positive_ts = float(s["profit_positive_ts"])
                        else:
                            st.phase              = "OPEN"
                            st.profit_positive_ts = 0.0
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
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: COOLDOWN legacy %.0fs restantes → abrirá en $%.0f",
                        sym, cooldown_until - now,
                        REST_STAKE_1000 if sym in SYMBOLS_1000 else QUIET_STAKE_500,
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

    def _persist(self, now: float) -> None:
        try:
            self._state_file.write_text(json.dumps(self.get_state_snapshot(), indent=2))
        except Exception as exc:
            _LOGGER.debug("[ENTRADA_DIEGO] persist error: %s", exc)
