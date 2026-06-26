"""
entrada_diego.py — Segunda línea de apertura autónoma.

BOOM500 / CRASH500:
  • Abre apenas arranca o después de cualquier cierre — sin ENTRY_WAIT, sin COOLDOWN
  • max_hold → cierra → reabre inmediato stake martingale ($10→$20→$40→$60)
  • profit+ → PROFIT_TIMER 3min → cierra → DISCHARGE si venia de reopen≥2 ($60 próximo ciclo)
  • DISCHARGE: capturar la descarga que sigue al spike de aviso de sequía larga
  • Spike durante PROFIT_TIMER → resetea los 3 min (rider de movimiento)

BOOM1000 / CRASH1000:
  • Abre apenas arranca o post-COOLDOWN
  • max_hold → cierra → reabre stake martingale ($10→$20→$20→$40→$40)
  • profit+ → PROFIT_TIMER 3min → cierra → COOLDOWN 10min → reabre
  • Al ganar desde reopen≥3: COOLDOWN reinicia en $20 (no $10, secuencia rica)
  • Si reopen≥8 sin win: DEEP PAUSE 15min → reinicia en $20 (corta martingale infinita)
  • Spike durante PROFIT_TIMER → resetea los 3 min

Activación: env ENTRADA_DIEGO_ENABLED=true
Estado:     {BOT_STATE_DIR}/entrada_diego_state.json
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("entrada_diego")

SYMBOLS_500  = {"CRASH500",  "BOOM500"}
SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_ED   = SYMBOLS_500 | SYMBOLS_1000

# Tablas de stake martingale por grupo
_STAKE_LADDER_500  = [10.0, 20.0, 40.0, 60.0]         # reopen #0,1,2,3+
_STAKE_LADDER_1000 = [10.0, 20.0, 20.0, 40.0, 40.0]   # reopen #0..4+ (inicia $10, $40 en reopen#3)

MULTIPLIER           = int(os.getenv("ENTRADA_DIEGO_MULTIPLIER",        "200"))
MAX_HOLD_S           = int(os.getenv("ENTRADA_DIEGO_MAX_HOLD_S",        "600"))   # 10 min
PROFIT_WAIT_S        = int(os.getenv("ENTRADA_DIEGO_PROFIT_WAIT_S",     "180"))   # 3 min rider
COOLDOWN_1000_S      = int(os.getenv("ENTRADA_DIEGO_COOLDOWN_1000_S",   "600"))   # 10 min cooldown normal
DEEP_PAUSE_1000_S    = int(os.getenv("ENTRADA_DIEGO_DEEP_PAUSE_1000_S", "900"))   # 15 min deep pause
DEEP_PAUSE_AT_1000   = int(os.getenv("ENTRADA_DIEGO_DEEP_PAUSE_AT",     "8"))     # reopen# que dispara deep pause
DISCHARGE_MIN_REOPEN = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_MIN",     "2"))     # reopen mínimo para discharge 500


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

    def remaining_s(self, now: float) -> float:
        if self.phase == "OPEN":
            return max(0.0, MAX_HOLD_S - (now - self.open_ts))
        if self.phase == "PROFIT_TIMER":
            return max(0.0, (self.profit_positive_ts + PROFIT_WAIT_S) - now)
        if self.phase == "COOLDOWN":
            return max(0.0, self.cooldown_until - now)
        return 0.0

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
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
        }


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
            _LOGGER.info(
                "[ENTRADA_DIEGO] ACTIVADO | symbols=%s | "
                "500: ladder=%s mult=%dx max_hold=%ds profit_wait=%ds discharge_min=reopen#%d | "
                "1000: ladder=%s cooldown=%ds deep_pause=%ds@reopen#%d",
                sorted(SYMBOLS_ED),
                _STAKE_LADDER_500, MULTIPLIER, MAX_HOLD_S, PROFIT_WAIT_S, DISCHARGE_MIN_REOPEN,
                _STAKE_LADDER_1000, COOLDOWN_1000_S, DEEP_PAUSE_1000_S, DEEP_PAUSE_AT_1000,
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
        return {
            "updated_at": now,
            "enabled":    self._enabled,
            **{sym: st.to_dict(now) for sym, st in self._states.items()},
        }

    # ── Máquina de estados ───────────────────────────────────────────────────

    async def _process(self, sym: str, tick: Any) -> None:
        state = self._states[sym]
        now   = time.time()

        # Actualizar profit en cada tick mientras hay contrato abierto
        if state.contract_id is not None:
            state.current_profit = self._query_profit(state.contract_id)

        last_spike_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)

        # ── IDLE: abrir inmediatamente ────────────────────────────────────────
        if state.phase == "IDLE":
            _LOGGER.info("[ENTRADA_DIEGO] %s IDLE → abriendo inmediato", sym)
            await self._open(sym, state, now)

        # ── OPEN ─────────────────────────────────────────────────────────────
        elif state.phase == "OPEN":

            # 1) max_hold expirado — cerramos nosotros (Deriv no lo cierra en spike_sl_only_mode)
            if now >= state.open_ts + MAX_HOLD_S:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s MAX_HOLD %ds expirado profit=%.4f → reopen#%d stake=%s",
                    sym, MAX_HOLD_S, state.current_profit,
                    state.reopens + 1, self._next_stake(sym, state.reopens + 1),
                )
                try:
                    if state.contract_id:
                        await self._executor.close_contract(int(state.contract_id))
                except Exception as exc:
                    _LOGGER.error("[ENTRADA_DIEGO] %s error cerrando max_hold: %s", sym, exc)
                state.last_close_profit  = state.current_profit
                state.contract_id        = None
                state.profit_positive_ts = 0.0
                state.reopens           += 1

                # DEEP PAUSE para 1000s al agotar todos los niveles del ladder
                if sym in SYMBOLS_1000 and state.reopens >= DEEP_PAUSE_AT_1000:
                    state.cooldown_until = now + DEEP_PAUSE_1000_S
                    state.phase          = "COOLDOWN"
                    state.reopens        = 1  # reinicia en $20 después del pause
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s DEEP PAUSE %ds → reinicia en $20 (reopen#%d alcanzado)",
                        sym, DEEP_PAUSE_1000_S, DEEP_PAUSE_AT_1000,
                    )
                    self._persist(now)
                    return

                await self._open(sym, state, now)
                return

            # 2) Contrato cerrado externamente (SL/TP Deriv) → reabrir
            if state.contract_id is not None and self._query_contract(state.contract_id) is None:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s contrato %s cerrado externamente → reopen#%d",
                    sym, state.contract_id, state.reopens + 1,
                )
                state.last_close_profit  = state.current_profit
                state.contract_id        = None
                state.reopens           += 1

                # DEEP PAUSE para 1000s también en cierre externo (ghost)
                if sym in SYMBOLS_1000 and state.reopens >= DEEP_PAUSE_AT_1000:
                    state.cooldown_until = now + DEEP_PAUSE_1000_S
                    state.phase          = "COOLDOWN"
                    state.reopens        = 1
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s DEEP PAUSE %ds (cierre externo) → reinicia en $20",
                        sym, DEEP_PAUSE_1000_S,
                    )
                    self._persist(now)
                    return

                await self._open(sym, state, now)
                return

            # 3) Profit positivo por primera vez → PROFIT_TIMER
            if state.current_profit > 0 and state.profit_positive_ts == 0.0:
                state.profit_positive_ts = now
                state.phase = "PROFIT_TIMER"
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s PROFIT POSITIVO %.4f → PROFIT_TIMER %ds",
                    sym, state.current_profit, PROFIT_WAIT_S,
                )
                self._persist(now)

        # ── PROFIT_TIMER ──────────────────────────────────────────────────────
        elif state.phase == "PROFIT_TIMER":

            # Contrato cerrado externamente mientras estábamos en profit → cierre ganador
            if state.contract_id is not None and self._query_contract(state.contract_id) is None:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s cerrado externo durante PROFIT_TIMER → cierre ganador",
                    sym,
                )
                state.last_close_profit  = max(state.current_profit, 0.01)
                state.contract_id        = None
                state.profit_positive_ts = 0.0
                prev_reopens             = state.reopens
                state.reopens            = 0
                await self._post_profit_close(sym, state, now, prev_reopens=prev_reopens)
                return

            # Spike mientras profit > 0 → resetea los 3 min (rider)
            if last_spike_ts > state.last_spike_ts and last_spike_ts > 0 and state.current_profit > 0:
                state.last_spike_ts      = last_spike_ts
                state.profit_positive_ts = now
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s SPIKE durante PROFIT_TIMER profit=%.4f → RESET TIMER (%ds)",
                    sym, state.current_profit, PROFIT_WAIT_S,
                )
                self._persist(now)
                return

            # Timer cumplido → cerrar
            if now >= state.profit_positive_ts + PROFIT_WAIT_S:
                await self._close_profit_timer(sym, state, now)

        # ── COOLDOWN (1000 tras profit o DEEP PAUSE) ──────────────────────────
        elif state.phase == "COOLDOWN":
            if now >= state.cooldown_until:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s COOLDOWN terminado → abriendo inmediato stake=%s",
                    sym, self._next_stake(sym, state.reopens),
                )
                # state.reopens fue fijado al entrar a COOLDOWN — no resetear aquí
                await self._open(sym, state, now)

    # ── Helpers de flujo ────────────────────────────────────────────────────

    async def _post_profit_close(
        self, sym: str, state: _SymState, now: float, prev_reopens: int = 0
    ) -> None:
        """
        Lógica post-cierre con profit+.
        500s: DISCHARGE si venia de reopen≥DISCHARGE_MIN → próximo ciclo a $60.
              Win temprano → ciclo fresco en $10.
        1000s: COOLDOWN siempre. Si ganó desde profundidad ≥3 → reinicia en $20.
        """
        if sym in SYMBOLS_500:
            if prev_reopens >= DISCHARGE_MIN_REOPEN:
                # Discharge mode: el símbolo salió de sequía larga → posicionar para la descarga
                state.reopens = len(_STAKE_LADDER_500) - 1  # = 3 → stake $60
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → DISCHARGE $60 (venia de reopen#%d)",
                    sym, state.last_close_profit, prev_reopens,
                )
            else:
                # Win temprano → ciclo fresco en $10
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → reabriendo inmediato $10",
                    sym, state.last_close_profit,
                )
                # state.reopens ya es 0 (reseteado antes de llamar aquí)
            await self._open(sym, state, now)

        else:
            # 1000s: siempre COOLDOWN
            if prev_reopens >= 3:
                # Venia de sequía larga → reinicio en $20 (secuencia de spikes ricos)
                state.reopens = 1  # ladder_1000[1] = 20.0
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → COOLDOWN %ds (reinicia $20, venia de reopen#%d)",
                    sym, state.last_close_profit, COOLDOWN_1000_S, prev_reopens,
                )
            else:
                # Win temprano → reinicio normal en $10
                state.reopens = 0  # ladder_1000[0] = 10.0
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → COOLDOWN %ds (reinicia $10)",
                    sym, state.last_close_profit, COOLDOWN_1000_S,
                )
            state.cooldown_until = now + COOLDOWN_1000_S
            state.phase          = "COOLDOWN"
            self._persist(now)

    # ── Operaciones de contrato ──────────────────────────────────────────────

    def _next_stake(self, sym: str, reopens: int) -> float:
        ladder = _STAKE_LADDER_500 if sym in SYMBOLS_500 else _STAKE_LADDER_1000
        return ladder[min(reopens, len(ladder) - 1)]

    async def _open(self, sym: str, state: _SymState, now: float, stake_override: float | None = None) -> None:
        from src.execution.deriv_trader import DerivOrder
        side  = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        stake = stake_override if stake_override is not None else self._next_stake(sym, state.reopens)
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s ABRIENDO %s $%.2f mult=%dx max_hold=%ds (reopen#%d)",
            sym, side, stake, MULTIPLIER, MAX_HOLD_S, state.reopens,
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
                state.contract_id        = int(cid) if cid else None
                state.open_ts            = now
                state.profit_positive_ts = 0.0
                state.current_profit     = 0.0
                state.phase              = "OPEN"
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
            # Estuvo positivo pero terminó negativo → reabrir con martingale
            state.reopens += 1
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s CIERRE PROFIT- %.4f → reopen#%d",
                sym, final_profit, state.reopens,
            )
            await self._open(sym, state, now)

        self._persist(now)

    # ── Query helpers ────────────────────────────────────────────────────────

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

    # ── Restaurar estado post-restart ────────────────────────────────────────

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
                        st.contract_id        = int(contract_id)
                        st.reopens            = reopens
                        st.open_ts            = float(s.get("open_ts", now))
                        st.last_spike_ts      = float(s.get("last_spike_ts", 0.0))
                        st.last_close_profit  = float(s.get("last_close_profit", 0.0))
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

                # Sin contrato — restaurar COOLDOWN/DEEP PAUSE si sigue vigente
                if phase == "COOLDOWN" and float(s.get("cooldown_until", 0.0)) > now:
                    cooldown_until = float(s["cooldown_until"])
                    st = self._states[sym]
                    st.phase          = "COOLDOWN"
                    st.cooldown_until = cooldown_until
                    st.reopens        = reopens  # preservar nivel de restart
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: COOLDOWN %.0fs restantes (reinicia stake=%s)",
                        sym, cooldown_until - now, self._next_stake(sym, reopens),
                    )
                    continue

                # Todo lo demás → IDLE
                _LOGGER.info("[ENTRADA_DIEGO] %s startup → IDLE → abre inmediato", sym)

        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] restore_from_disk error: %s", exc)

        self._persist(time.time())

    def _persist(self, now: float) -> None:
        try:
            self._state_file.write_text(json.dumps(self.get_state_snapshot(), indent=2))
        except Exception as exc:
            _LOGGER.debug("[ENTRADA_DIEGO] persist error: %s", exc)
