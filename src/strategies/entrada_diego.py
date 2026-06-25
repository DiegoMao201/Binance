"""
entrada_diego.py — Segunda línea de apertura autónoma post-spike.

Lógica por símbolo (CRASH500 / BOOM500):

  IDLE         →(nuevo spike)→  ENTRY_WAIT (300s desde spike)
  ENTRY_WAIT   →(tiempo)→       OPEN (abre $10, sin filtros)
  OPEN         →(profit>0)→     PROFIT_TIMER (180s)
  OPEN         →(max_hold)→     OPEN (reabre inmediato, ciclo hasta profit+)
  PROFIT_TIMER →(180s cumplidos)→ cierra:
     profit final > 0  → COOLDOWN (300s) → IDLE
     profit final ≤ 0  → OPEN inmediato (reabre)

Activación: env ENTRADA_DIEGO_ENABLED=true
Cuando activo: D6 y D10 ghost se pausan (env ENTRADA_DIEGO_ENABLED maneja esto en main).

Logs:    [ENTRADA_DIEGO]
Estado:  {BOT_STATE_DIR}/entrada_diego_state.json
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

SYMBOLS_ED   = {"CRASH500", "BOOM500"}
STAKE        = float(os.getenv("ENTRADA_DIEGO_STAKE",        "10.0"))
_STAKE_LADDER = [10.0, 20.0, 40.0, 60.0]   # reopen#0, #1, #2, #3+
MULTIPLIER   = int(os.getenv("ENTRADA_DIEGO_MULTIPLIER",     "200"))
ENTRY_WAIT_S = int(os.getenv("ENTRADA_DIEGO_ENTRY_WAIT_S",  "300"))   # 5 min post-spike
MAX_HOLD_S   = int(os.getenv("ENTRADA_DIEGO_MAX_HOLD_S",    "600"))   # 10 min max hold
PROFIT_WAIT_S= int(os.getenv("ENTRADA_DIEGO_PROFIT_WAIT_S", "180"))   # 3 min tras profit+
COOLDOWN_S   = int(os.getenv("ENTRADA_DIEGO_COOLDOWN_S",    "300"))   # 5 min post profit+cierre


# ─── State por símbolo ──────────────────────────────────────────────────────

@dataclass
class _SymState:
    phase: str = "IDLE"                      # IDLE | ENTRY_WAIT | OPEN | PROFIT_TIMER | COOLDOWN
    last_spike_ts: float = 0.0               # último spike detectado
    entry_wait_until: float = 0.0            # abrir cuando now >= esto
    contract_id: Optional[int] = None
    open_ts: float = 0.0
    profit_positive_ts: float = 0.0          # primer momento profit > 0
    cooldown_until: float = 0.0
    reopens: int = 0                         # reentradas sin profit+ (estadística)
    last_close_profit: float = 0.0
    current_profit: float = 0.0             # snapshot último tick

    def remaining_s(self, now: float) -> float:
        if self.phase == "ENTRY_WAIT":
            return max(0.0, self.entry_wait_until - now)
        if self.phase == "PROFIT_TIMER":
            return max(0.0, (self.profit_positive_ts + PROFIT_WAIT_S) - now)
        if self.phase == "COOLDOWN":
            return max(0.0, self.cooldown_until - now)
        if self.phase == "OPEN":
            return max(0.0, MAX_HOLD_S - (now - self.open_ts))
        return 0.0

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "last_spike_ts": round(self.last_spike_ts, 3),
            "entry_wait_until": round(self.entry_wait_until, 3),
            "contract_id": self.contract_id,
            "open_ts": round(self.open_ts, 3),
            "profit_positive_ts": round(self.profit_positive_ts, 3),
            "cooldown_until": round(self.cooldown_until, 3),
            "reopens": self.reopens,
            "last_close_profit": round(self.last_close_profit, 4),
            "current_profit": round(self.current_profit, 4),
            "remaining_s": round(self.remaining_s(now), 1),
        }


# ─── Clase principal ─────────────────────────────────────────────────────────

class EntradaDiego:
    """
    Segunda línea de apertura autónoma.

    Uso desde main_deriv.py:
        # En DerivDaemon.__init__:
        from src.strategies.entrada_diego import EntradaDiego
        self._entrada_diego = EntradaDiego(
            executor=self._executor,
            risk=self._risk,
            logs_dir=Path(os.getenv("BOT_STATE_DIR", "/data/logs")),
        )

        # En DerivDaemon._handle_tick (antes de asyncio.create_task):
        await self._entrada_diego.on_tick(tick)

        # Para bloquear D6/D10 cuando entrada_diego activo (en _pipeline):
        if self._entrada_diego.is_enabled():
            return  # saltar D6/D10 ghost lógica
    """

    def __init__(self, executor: Any, risk: Any, logs_dir: Path) -> None:
        self._executor = executor
        self._risk = risk
        self._logs_dir = logs_dir
        self._state_file = logs_dir / "entrada_diego_state.json"
        self._enabled: bool = str(
            os.getenv("ENTRADA_DIEGO_ENABLED", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}

        self._states: dict[str, _SymState] = {sym: _SymState() for sym in SYMBOLS_ED}
        self._locks: dict[str, asyncio.Lock] = {sym: asyncio.Lock() for sym in SYMBOLS_ED}
        self._open_lock   = asyncio.Lock()
        self._restore_lock = asyncio.Lock()
        self._restored    = False

        if self._enabled:
            _LOGGER.info(
                "[ENTRADA_DIEGO] ACTIVADO | symbols=%s stake=$%.1f mult=%dx "
                "entry_wait=%ds max_hold=%ds profit_wait=%ds cooldown=%ds",
                sorted(SYMBOLS_ED), STAKE, MULTIPLIER,
                ENTRY_WAIT_S, MAX_HOLD_S, PROFIT_WAIT_S, COOLDOWN_S,
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
        # Restaurar estado persistido en el primer tick post-restart
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
            "enabled": self._enabled,
            **{sym: st.to_dict(now) for sym, st in self._states.items()},
        }

    # ── Máquina de estados ───────────────────────────────────────────────────

    async def _process(self, sym: str, tick: Any) -> None:
        state = self._states[sym]
        now   = time.time()

        # Actualizar profit si hay contrato abierto
        if state.contract_id is not None:
            state.current_profit = self._query_profit(state.contract_id)

        last_spike_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)

        if state.phase == "IDLE":
            self._check_new_spike(sym, state, last_spike_ts, now)

        elif state.phase == "ENTRY_WAIT":
            # Nuevo spike → reset timer
            if last_spike_ts > state.last_spike_ts and last_spike_ts > 0:
                state.last_spike_ts = last_spike_ts
                state.entry_wait_until = last_spike_ts + ENTRY_WAIT_S
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s SPIKE durante wait → reset: abre en %.0fs",
                    sym, state.entry_wait_until - now,
                )
                self._persist(now)
                return

            if now >= state.entry_wait_until:
                await self._open(sym, state, now)

        elif state.phase == "OPEN":
            # Si el contrato cerró externamente (max_hold por DPM) → reabrir
            if state.contract_id is not None and self._query_contract(state.contract_id) is None:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s contrato %s cerrado (max_hold/SL) → reabrir inmediato",
                    sym, state.contract_id,
                )
                state.last_close_profit = state.current_profit
                state.contract_id = None
                state.reopens += 1
                await self._open(sym, state, now)
                return

            # Primer momento en que profit > 0 → iniciar PROFIT_TIMER
            if state.current_profit > 0 and state.profit_positive_ts == 0.0:
                state.profit_positive_ts = now
                state.phase = "PROFIT_TIMER"
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s PROFIT POSITIVO %.4f → PROFIT_TIMER %ds",
                    sym, state.current_profit, PROFIT_WAIT_S,
                )
                self._persist(now)

        elif state.phase == "PROFIT_TIMER":
            # Contrato cerrado externamente durante el timer (sl/tp automático de Deriv)
            # Estábamos en PROFIT_TIMER → ya había profit positivo → tratamos como cierre ganador
            if state.contract_id is not None and self._query_contract(state.contract_id) is None:
                state.last_close_profit = max(state.current_profit, 0.01)
                state.contract_id = None
                state.profit_positive_ts = 0.0
                state.cooldown_until = now + COOLDOWN_S
                state.reopens = 0
                state.phase = "COOLDOWN"
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s cerrado externo durante PROFIT_TIMER → COOLDOWN %ds (reopens reset)",
                    sym, COOLDOWN_S,
                )
                self._persist(now)
                return

            # Nuevo spike mientras profit sigue positivo → reset timer (rider de spike)
            if last_spike_ts > state.last_spike_ts and last_spike_ts > 0 and state.current_profit > 0:
                state.last_spike_ts = last_spike_ts
                state.profit_positive_ts = now  # reinicia los 3 minutos
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s SPIKE durante PROFIT_TIMER profit=%.4f → RESET TIMER (%ds)",
                    sym, state.current_profit, PROFIT_WAIT_S,
                )
                self._persist(now)
                return

            if now >= state.profit_positive_ts + PROFIT_WAIT_S:
                await self._close_profit_timer(sym, state, now)

        elif state.phase == "COOLDOWN":
            if now >= state.cooldown_until:
                _LOGGER.info("[ENTRADA_DIEGO] %s COOLDOWN terminado → IDLE (espera nuevo spike)", sym)
                state.phase = "IDLE"
                self._persist(now)
            else:
                # Aprovechar para chequear si llega un spike nuevo durante cooldown
                self._check_new_spike(sym, state, last_spike_ts, now)

    # ── Operaciones ─────────────────────────────────────────────────────────

    def _check_new_spike(self, sym: str, state: _SymState, last_spike_ts: float, now: float) -> None:
        if last_spike_ts > state.last_spike_ts and last_spike_ts > 0:
            state.last_spike_ts = last_spike_ts
            state.entry_wait_until = last_spike_ts + ENTRY_WAIT_S
            state.phase = "ENTRY_WAIT"
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s SPIKE → ENTRY_WAIT %.0fs (abre ~%s)",
                sym, ENTRY_WAIT_S,
                time.strftime("%H:%M:%S", time.gmtime(state.entry_wait_until)),
            )
            self._persist(now)

    async def _open(self, sym: str, state: _SymState, now: float, stake_override: float | None = None) -> None:
        from src.execution.deriv_trader import DerivOrder  # import local para evitar circular
        side = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        martingale_stake = _STAKE_LADDER[min(state.reopens, len(_STAKE_LADDER) - 1)]
        stake = stake_override if stake_override is not None else martingale_stake
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s ABRIENDO %s $%.2f mult=%dx max_hold=%ds (reopen#%d)",
            sym, side, stake, MULTIPLIER, MAX_HOLD_S, state.reopens,
        )
        try:
            async with self._open_lock:  # serializa aperturas — evita conflicto de margen
                order = DerivOrder(
                    symbol=sym,
                    side=side,
                    stake_usdt=stake,
                    multiplier=MULTIPLIER,
                    stop_loss_pct=0.65,    # Deriv limita SL a ≤70% del stake; 65% pasa
                    take_profit_pct=0.65,  # TP alto para no auto-cerrar en condiciones normales
                    max_hold_seconds=float(MAX_HOLD_S),
                    score_breakdown={
                        "quality_tier": "entrada_diego",
                        "setup": "entrada_diego",
                        "grade": "ED",
                        "score": 0.0,
                        "entrada_diego": True,
                    },
                )
                result = await self._executor.execute(order)

            if result.get("status") == "live":
                cid = result.get("contract_id")
                entry = result.get("entry_price", 0.0)
                state.contract_id = int(cid) if cid else None
                state.open_ts = now
                state.profit_positive_ts = 0.0
                state.current_profit = 0.0
                state.phase = "OPEN"
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s OPEN OK contract=%s entry=%.5f stake=$%.2f",
                    sym, state.contract_id, entry, stake,
                )
            else:
                _LOGGER.warning("[ENTRADA_DIEGO] %s OPEN FAILED: %s → IDLE", sym, result)
                state.phase = "IDLE"

        except Exception as exc:
            exc_str = str(exc)
            # Deriv (y el broker interno) pueden capear el stake en múltiples niveles.
            # Reintentamos en cascada hasta que el stake propuesto sea menor al máximo permitido.
            if "LimitOrderAmountTooHigh" in exc_str:
                m = re.search(r"'code_args':\s*\['([\d.]+)'\]", exc_str)
                if m:
                    max_allowed = float(m.group(1))
                    retry_stake = round(max_allowed * 0.95, 2)
                    # Solo reintentamos si el nuevo stake es menor que el actual (evita bucle infinito)
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
            _LOGGER.error("[ENTRADA_DIEGO] %s error al cerrar %s: %s", sym, state.contract_id, exc)

        state.last_close_profit = final_profit
        state.contract_id = None
        state.profit_positive_ts = 0.0

        if final_profit > 0:
            state.cooldown_until = now + COOLDOWN_S
            state.reopens = 0
            state.phase = "COOLDOWN"
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → COOLDOWN %ds (reopens reset)",
                sym, final_profit, COOLDOWN_S,
            )
        else:
            # Fue positivo pero terminó negativo → reabrir inmediato
            state.reopens += 1
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s CIERRE PROFIT- %.4f (pasó por verde pero cerró rojo) → REOPEN",
                sym, final_profit,
            )
            await self._open(sym, state, now)

        self._persist(now)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _query_contract(self, contract_id: int) -> Optional[dict[str, Any]]:
        """Devuelve el contrato abierto o None si ya cerró."""
        try:
            for oc in self._executor.get_open_contracts_for_status():
                if oc.get("contract_id") == contract_id:
                    return oc
        except Exception:
            pass
        return None

    def _query_profit(self, contract_id: int) -> float:
        """Devuelve floating_pnl del contrato o 0 si no encontrado."""
        try:
            oc = self._query_contract(contract_id)
            if oc:
                return float(oc.get("floating_pnl") or 0.0)
        except Exception:
            pass
        return 0.0

    async def _restore_from_disk(self) -> None:
        """Re-adjunta contratos abiertos y restaura reopens tras restart del bot."""
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
                    # Verificar si el contrato sigue abierto en Deriv
                    live = self._query_contract(int(contract_id))
                    if live:
                        st = self._states[sym]
                        st.contract_id       = int(contract_id)
                        st.reopens           = reopens
                        st.open_ts           = float(s.get("open_ts", now))
                        st.last_spike_ts     = float(s.get("last_spike_ts", 0.0))
                        st.last_close_profit = float(s.get("last_close_profit", 0.0))
                        # Si estaba en PROFIT_TIMER y profit sigue > 0 → mantener timer
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

                # Sin contrato — restaurar COOLDOWN si sigue vigente
                if phase == "COOLDOWN":
                    cooldown_until = float(s.get("cooldown_until", 0.0))
                    if cooldown_until > now:
                        st = self._states[sym]
                        st.phase          = "COOLDOWN"
                        st.cooldown_until = cooldown_until
                        st.last_spike_ts  = float(s.get("last_spike_ts", 0.0))
                        st.reopens        = 0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s RESTAURADO: COOLDOWN %.0fs restantes",
                            sym, cooldown_until - now,
                        )
        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] restore_from_disk error: %s", exc)

    def _persist(self, now: float) -> None:
        try:
            payload = self.get_state_snapshot()
            self._state_file.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            _LOGGER.debug("[ENTRADA_DIEGO] persist error: %s", exc)
