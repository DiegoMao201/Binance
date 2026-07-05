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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("entrada_diego")

SYMBOLS_500  = {"CRASH500",  "BOOM500"}
SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_R    = set()   # R_75 y JD75 suspendidos — no estudiados aún
SYMBOLS_ED   = SYMBOLS_500 | SYMBOLS_1000 | SYMBOLS_R

_STAKE_LADDER_1000 = [10.0, 20.0, 20.0, 40.0, 40.0]   # reopen #0..4+
# CRASH500 escalación gradual: $5 (sensor activo) → $20 (2do spike) → $40 (recovery) → $60 (máximo)
# MAX_HOLD logic: $5/$20 MH → idx=2 ($40); $40 MH → idx=3 ($60); $60 MH → idx=3 (stays)
_CRASH500_STAKE_LADDER = [5.0, 20.0, 40.0, 60.0]

MULTIPLIER         = int(os.getenv("ENTRADA_DIEGO_MULTIPLIER",        "200"))
MAX_HOLD_S         = int(os.getenv("ENTRADA_DIEGO_MAX_HOLD_S",        "600"))
PROFIT_WAIT_S      = int(os.getenv("ENTRADA_DIEGO_PROFIT_WAIT_S",     "180"))
# Sensor $1 QUIET: mínimo de vida antes de que SL_HARD/MAX_HOLD puedan cerrar
# Da tiempo al spike de llegar sin pagar comisión de reopen cada 2-5min
QUIET_MIN_HOLD_S   = int(os.getenv("ENTRADA_DIEGO_QUIET_MIN_HOLD_S",  "1400"))  # ~23min
# BOOM500: solo abrir $40 si el spike fue ≥7.0 (jump bruto). Datos limpios (ATR≥0.020):
#   jump 7-10 → p600=66.3% (mejor), jump <7 → 57-62%. Estable entre deploys (no usa ATR).
BOOM500_MIN_SPIKE_JUMP  = float(os.getenv("ENTRADA_DIEGO_BOOM_MIN_JUMP",        "7.0"))
# $40 ACTIVE: tiempo rideando profit+ antes de cerrar.
# 180s = 3min: el spike ya llegó y generó ganancia → cerrar rápido antes de que el mercado rebote.
# Los 8min eran para el MAX_HOLD en OPEN (aguantar antes del spike), no para el timer post-profit.
ACTIVE_PROFIT_WAIT_S   = int(os.getenv("ENTRADA_DIEGO_ACTIVE_PROFIT_WAIT_S",   "180"))  # 3min
DEEP_PAUSE_AT_1000 = int(os.getenv("ENTRADA_DIEGO_DEEP_PAUSE_AT",     "8"))

# 1000s: en vez de COOLDOWN/DEEP PAUSE fuera del mercado → REST MODE a $2 (captura spikes)
REST_STAKE_1000    = float(os.getenv("ENTRADA_DIEGO_REST_STAKE_1000", "2.0"))

# 500s: QUIET/ACTIVE
QUIET_STAKE_500       = float(os.getenv("ENTRADA_DIEGO_QUIET_STAKE",        "1.0"))   # sensor: stake mínimo, detecta spikes directamente
ACTIVE_STAKE_500      = float(os.getenv("ENTRADA_DIEGO_ACTIVE_STAKE",       "40.0"))  # símbolo activo
ACTIVE_MAX_HOLDS      = int(os.getenv("ENTRADA_DIEGO_ACTIVE_MAX_HOLDS",     "1"))     # max_holds para → QUIET (1 = cualquier pérdida vuelve a $5)
DISCHARGE_SPIKES_500        = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_SPIKES",              "1"))  # spikes en PROFIT_TIMER = descarga → fuerza QUIET
BOOM500_DISCHARGE_QUIET_SPIKES = int(os.getenv("ENTRADA_DIEGO_BOOM500_DISCHARGE_QUIET_SPIKES", "2"))  # BOOM500 QUIET: ≥2 spikes = no ir a ACTIVE (1 spike sí va)
MIN_WIN_ACTIVE_500    = float(os.getenv("ENTRADA_DIEGO_MIN_WIN_ACTIVE",      "0.10"))  # profit mínimo real para QUIET→ACTIVE (filtra ghosts $0.01)

# Gate MERCADO_DESCARGADO: si en la ventana hubo demasiados spikes, el mercado ya soltó energía → bloquear $40
# Umbrales por símbolo (datos: 219+177 trades BOOM+CRASH históricos):
#   BOOM500 n=3 WR=38% → bloquear; n=1-2 WR=57-59% → dejar pasar
#   CRASH500 n=3 WR=57% → dejar pasar; n>=4 WR=31% → bloquear
DISCHARGE_WINDOW_S         = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_WINDOW_S",        "900"))  # ventana 15 min
DISCHARGE_MAX_SPIKES_BOOM  = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_BOOM_MAX_SPIKES", "3"))    # BOOM: ≥3 bloquea
DISCHARGE_MAX_SPIKES_CRASH = int(os.getenv("ENTRADA_DIEGO_DISCHARGE_CRASH_MAX_SPIKES","4"))    # CRASH: ≥4 bloquea

# Gate SPIKE_FRESCO (CRASH500): después de un spike CRASH el mercado rebota hacia arriba
# Abrir MULTDOWN inmediatamente después del spike → WR=40% (datos: 40 trades)
# Esperar ≥45s → WR=48% en zona normal 180-360s
CRASH_FRESH_SPIKE_S = int(os.getenv("ENTRADA_DIEGO_CRASH_FRESH_SPIKE_S", "45"))  # segundos de espera post-spike

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

# 500s: SL duro — separado por modo para dar espacio al $40
# QUIET $1:  15% × $1  = -$0.15 (sensor mínimo, corte rápido)
# ACTIVE $40: 35% × $40 = -$14.00 (espacio para que el spike llegue sin cortar antes)
ED_SL_PCT        = float(os.getenv("ENTRADA_DIEGO_SL_PCT",        "0.15"))  # QUIET $1
ACTIVE_SL_PCT    = float(os.getenv("ENTRADA_DIEGO_ACTIVE_SL_PCT", "0.35"))  # ACTIVE $40

# 500s: gate de spike consumido — si el $5 QUIET ganó más de este umbral,
# el spike fue muy grande y la energía ya se consumió → no abrir $40 todavía.
# Aplica a CRASH500 y BOOM500.
SPIKE_CONSUMED_THRESHOLD = float(os.getenv("ENTRADA_DIEGO_SPIKE_CONSUMED", "2.0"))

# Gate ZONA_PELIGROSA (BOOM500): gap 2-10min + ratio≥p75(≈120x) → WIN=29%, avg=-$3.82 ← EVITAR
# Dato 580 $40 contratos:  gap2-10min + ratio≥p75: WIN=29%, avg=-$3.82  → BLOQUEAR
#                          gap2-10min + ratio<p75:  WIN=49%, avg=+$0.70  → PERMITIR
#                          gap>10min  + ratio≥p75:  WIN=50-67%           → PERMITIR (cambio tendencia)
# Spike grande en zona media = pico local, el mercado libera energía a trozos → retracción inmediata
BOOM_DANGER_GAP_MAX_S = int(os.getenv("ENTRADA_DIEGO_BOOM_DANGER_GAP_MAX",  "600"))   # 10min límite zona peligrosa
BOOM_DANGER_RATIO_MIN = float(os.getenv("ENTRADA_DIEGO_BOOM_DANGER_RATIO",  "120.0")) # ≈p75 BOOM500

# FAST_OPEN (BOOM500): burst fuerte (gap<2min + ratio≥p75) → cierre $5 inmediato → $40 sin timer
# Solución al timing lag: con PROFIT_TIMER=180s el $40 pierde el pico del burst
# (57%+ spikes llegan en los primeros 180s). Abriendo ya: P(spike en 600s) ≈ 87%.
# Rango [0, 120s) — NO se solapa con ZONA_PELIGROSA [120s, 600s) — cero conflicto.
FAST_OPEN_GAP_MAX_S  = int(os.getenv("ENTRADA_DIEGO_FAST_OPEN_GAP_MAX",    "120"))   # <2min = burst
FAST_OPEN_RATIO_MIN  = float(os.getenv("ENTRADA_DIEGO_FAST_OPEN_RATIO",    "120.0")) # ≈p75 BOOM500

# SPIKE_DETECTOR: ratio mínimo para que BOOM500 inicie el timer 180s vía SPIKE_DETECTOR
# Datos 7714 spikes: spike≥200x + n15≤2 → WIN=71.4%, avg=+$6.37 (n=14) ← GOLDEN
#                   spike<200x            → WIN=29.4%, avg=-$1.94 (n=34) ← EVITAR en SPIKE_TIMER
# Spikes<200x caen al path normal profit_timer (requieren profit+ como filtro natural)
SPIKE_DETECTOR_BOOM_MIN_RATIO = float(os.getenv("ENTRADA_DIEGO_SPIKE_DET_BOOM_MIN_RATIO", "200.0"))

# 500s: modo simple — stake fijo, 30min timer, SL en USD, trailing profit floor
STAKE_500_FIXED        = float(os.getenv("ENTRADA_DIEGO_500_STAKE",   "20.0"))
SL_USD_500_SIMPLE      = float(os.getenv("ENTRADA_DIEGO_500_SL_USD",  "14.0"))
HOLD_TIME_S_500_SIMPLE = int(os.getenv("ENTRADA_DIEGO_500_HOLD_S",   "1800"))   # 30 min
# Pisos de profit: 3, 5, 7, 9, 11... (de $2 en $2)
# Cuando el peak cruza un piso, ese piso se activa. Si el profit cae por debajo → cierra.
_PROFIT_TIERS_500 = [float(x) for x in range(3, 200, 1)]  # [3, 4, 5, 6, 7, ...]

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
    prev_spike_ts: float = 0.0   # timestamp del spike ANTERIOR al último — para medir intervalo
    spike_interval_s: float = 0.0  # intervalo entre los últimos 2 spikes (cuando ocurrió el activador)
    trigger_spike_ratio: float = 0.0  # ratio del spike que activó el PROFIT_TIMER
    trigger_spike_jump: float = 0.0   # jump bruto (sin ATR) del spike activador — estable entre deploys
    recent_spike_ts: list = field(default_factory=list)  # historial rolling de spikes (gate MERCADO_DESCARGADO)
    reopens: int = 0
    last_close_profit: float = 0.0
    current_profit: float = 0.0
    sym_mode: str = "QUIET"         # "QUIET" | "ACTIVE" — solo 500s
    consec_max_holds: int = 0       # max_holds consecutivos mientras ACTIVE — solo 500s
    consec_wins_active: int = 0     # wins consecutivos mientras ACTIVE — solo 500s
    profit_timer_spikes: int = 0    # spikes capturados durante el PROFIT_TIMER actual — solo 500s
    prev_discharge: bool = False    # True si el ACTIVE anterior terminó en descarga; exige timer limpio antes de $40
    spike_timer_active: bool = False  # ya no se usa; mantenido por compatibilidad con estado guardado
    crash_stake_idx: int = 0        # CRASH500 escalación: 0=$5 1=$20 2=$40 3=$60 — persiste entre ciclos QUIET/ACTIVE
    boom_stake_idx: int = 0         # BOOM500 escalación: 0=$5 1=$20 2=$40 3=$60 — igual que CRASH500
    peak_profit_500: float = 0.0    # máximo profit visto en el contrato actual — solo 500s modo simple
    spikes_in_contract_500: int   = 0    # reservado — compatibilidad estado guardado
    last_spike_ts_500:      float = 0.0  # reservado — compatibilidad estado guardado
    protecting_500:         bool  = False # True: estamos en modo protección $1/10min (post-2 wins seguidos)
    consec_wins_500:        int   = 0    # wins consecutivos con PnL>0 en $20 — al llegar a 2 activa protección
    protection_started_at:  float = 0.0  # epoch de cuando activó la protección; NO se resetea en reopens del $1
    broker_blocked_until_500: float = 0.0  # hasta cuándo esperar si broker cap < $1 (precio post-spike muy alto)
    rest_mode: bool = False         # True = abriendo a $2 post-profit/deep-pause — solo 1000s
    is_readjusted: bool = False     # True cuando se re-adjuntó a contrato viejo (no abrir PROFIT_TIMER en ACTIVE)
    r75_direction: str = "MULTUP"  # R_75: MULTUP o MULTDOWN; flip si pierde, mantiene si gana

    def remaining_s(self, now: float) -> float:
        if self.phase == "OPEN":
            return max(0.0, MAX_HOLD_S - (now - self.open_ts))
        if self.phase == "PROFIT_TIMER":
            _wait = ACTIVE_PROFIT_WAIT_S if self.sym_mode == "ACTIVE" else PROFIT_WAIT_S
            return max(0.0, (self.profit_positive_ts + _wait) - now)
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
            "spike_interval_s": round(self.spike_interval_s, 1),
            "trigger_spike_ratio": round(self.trigger_spike_ratio, 1),
            "trigger_spike_jump": round(self.trigger_spike_jump, 5),
            "reopens": self.reopens,
            "last_close_profit": round(self.last_close_profit, 4),
            "current_profit": round(self.current_profit, 4),
            "remaining_s": round(self.remaining_s(now), 1),
            "sym_mode": self.sym_mode,
            "consec_max_holds": self.consec_max_holds,
            "consec_wins_active": self.consec_wins_active,
            "profit_timer_spikes": self.profit_timer_spikes,
            "prev_discharge": self.prev_discharge,
            "crash_stake_idx": self.crash_stake_idx,
            "boom_stake_idx": self.boom_stake_idx,
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
        # Tolerante a env vars mal pegadas (ej: "trueDERIV_D10_SPIKE_STABILIZE_SEC=0")
        _ed_raw = str(os.getenv("ENTRADA_DIEGO_ENABLED", "false")).strip().lower()
        self._enabled: bool = _ed_raw.startswith("true") or _ed_raw in {"1", "yes", "on"}

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
        if sym in SYMBOLS_500:
            await self._process_500_simple(sym)
            return
        state = self._states[sym]
        now   = time.time()

        if state.contract_id is not None:
            state.current_profit = self._query_profit(state.contract_id)

        last_spike_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)

        # Tracking de spikes recientes (ventana deslizante para gate MERCADO_DESCARGADO)
        if sym in SYMBOLS_500 and last_spike_ts > (state.recent_spike_ts[-1] if state.recent_spike_ts else 0.0):
            state.recent_spike_ts.append(last_spike_ts)
            cutoff = now - DISCHARGE_WINDOW_S
            state.recent_spike_ts = [t for t in state.recent_spike_ts if t > cutoff]

        # ── IDLE ──────────────────────────────────────────────────────────────
        if state.phase == "IDLE":
            _LOGGER.info("[ENTRADA_DIEGO] %s IDLE → abriendo inmediato", sym)
            await self._open(sym, state, now)

        # ── OPEN ──────────────────────────────────────────────────────────────
        elif state.phase == "OPEN":

            # 0) SL duro: cortar pérdida antes de max_hold (solo 500s)
            if sym in SYMBOLS_500:
                if state.sym_mode == "ACTIVE":
                    _idx = state.crash_stake_idx if sym == "CRASH500" else state.boom_stake_idx
                    _stake_now = _CRASH500_STAKE_LADDER[min(_idx, len(_CRASH500_STAKE_LADDER) - 1)]
                    _sl_pct = ACTIVE_SL_PCT
                else:
                    _stake_now = QUIET_STAKE_500
                    _sl_pct    = ED_SL_PCT
                _sl_dollar = _stake_now * _sl_pct
                _quiet_held = (now - state.open_ts) >= QUIET_MIN_HOLD_S
                if (
                    state.current_profit < -_sl_dollar
                    and state.contract_id is not None
                    and (state.sym_mode == "ACTIVE" or _quiet_held)
                ):
                    # Guardar localmente y limpiar estado ANTES del await para evitar
                    # race condition: un tick puede llegar durante el await y re-disparar SL_HARD
                    _sl_contract_id = int(state.contract_id)
                    _sl_pnl        = state.current_profit
                    state.contract_id    = None
                    state.current_profit = 0.0
                    try:
                        await self._executor.close_contract(_sl_contract_id)
                    except Exception as exc:
                        _LOGGER.error("[ENTRADA_DIEGO] %s SL_HARD error: %s", sym, exc)
                    state.last_close_profit  = _sl_pnl
                    state.profit_positive_ts = 0.0
                    state.reopens           += 1
                    self._add_global_pnl(sym, state.last_close_profit, now)
                    if state.sym_mode == "ACTIVE":
                        state.sym_mode           = "QUIET"
                        state.consec_max_holds   = 0
                        state.consec_wins_active = 0
                        if sym == "CRASH500":
                            state.crash_stake_idx = 0  # SL = protección total, reiniciar ciclo
                        elif sym == "BOOM500":
                            state.boom_stake_idx = 0
                    next_stake = self._next_stake(sym, state.reopens)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s SL_HARD profit=%.4f < -%.2f → %s $%.0f reopen#%d",
                        sym, _sl_pnl, _sl_dollar,
                        state.sym_mode, next_stake, state.reopens,
                    )
                    # Esperar que Deriv registre el cierre antes de abrir nuevo contrato.
                    # Sin este delay, _open() recibe "symbol_already_open" y re-engancha
                    # al contrato que acaba de cerrar → SL_HARD vuelve a disparar.
                    await asyncio.sleep(3.0)
                    await self._open(sym, state, now)
                    return

            # 1) max_hold expirado (QUIET usa QUIET_MIN_HOLD_S para darle tiempo al spike)
            _effective_max_hold = (
                QUIET_MIN_HOLD_S
                if sym in SYMBOLS_500 and state.sym_mode == "QUIET"
                else MAX_HOLD_S
            )
            if now >= state.open_ts + _effective_max_hold:
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
                        if sym in SYMBOLS_500:
                            # Escalación secuencial por MAX_HOLD (sin saltos):
                            # $5(0)→$20(1)→$40(2)→$60(3)→$60(3)
                            if sym == "CRASH500":
                                _old_idx = state.crash_stake_idx
                                state.crash_stake_idx = min(_old_idx + 1, 3)
                                _new_idx = state.crash_stake_idx
                            else:
                                _old_idx = state.boom_stake_idx
                                state.boom_stake_idx = min(_old_idx + 1, 3)
                                _new_idx = state.boom_stake_idx
                            _LOGGER.info(
                                "[ENTRADA_DIEGO] %s MAX_HOLD $%.0f sin spike → QUIET $1,"
                                " próximo ciclo $%.0f",
                                sym,
                                _CRASH500_STAKE_LADDER[_old_idx],
                                _CRASH500_STAKE_LADDER[_new_idx],
                            )

                next_stake = self._next_stake(sym, state.reopens)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s MAX_HOLD %ds expirado profit=%.4f → reopen#%d stake=$%.0f",
                    sym, _effective_max_hold, state.last_close_profit, state.reopens, next_stake,
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

            # 2.5) SPIKE_DETECTOR (QUIET 500s) — CRASH500 y BOOM500 idénticos
            # Ambos símbolos: $1 es sensor, no vehículo de profit.
            # Spike detectado → CIERRE INMEDIATO del $1 → abrir stake ladder.
            # Solo se bloquea si DISCHARGE activo (demasiados spikes en ventana 15min).
            if (
                sym in SYMBOLS_500
                and state.sym_mode == "QUIET"
                and state.contract_id is not None
                and last_spike_ts > state.last_spike_ts
            ):
                _det_ivl   = (last_spike_ts - state.last_spike_ts) if state.last_spike_ts > 0 else 9999.0
                _det_ratio = self._risk.get_last_spike_ratio(sym)
                _det_jump  = self._risk.get_last_spike_jump(sym)
                state.spike_interval_s    = _det_ivl
                state.trigger_spike_ratio = _det_ratio
                state.trigger_spike_jump  = _det_jump
                state.profit_timer_spikes = 0
                state.last_spike_ts       = last_spike_ts  # consumir siempre

                _cutoff_d  = now - DISCHARGE_WINDOW_S
                _recent_d  = sum(1 for t in state.recent_spike_ts if t > _cutoff_d)
                _discharged_now = _recent_d >= DISCHARGE_MAX_SPIKES_CRASH  # mismo umbral para ambos

                if _discharged_now:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s SPIKE_DETECTOR ivl=%.0fs ratio=%.0fx"
                        " → DISCHARGE (%d/%d spikes) → $1 continua",
                        sym, _det_ivl, _det_ratio, _recent_d, DISCHARGE_MAX_SPIKES_CRASH,
                    )
                elif state.is_readjusted:
                    # Contrato re-adjuntado: no es el sensor $1 real — puede ser un $40/$60 viejo.
                    # Hacer CIERRE INMEDIATO aquí cerraría el contrato anterior a pérdida y
                    # abriría otro stake alto inmediatamente — doble pérdida.
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s SPIKE en re-adjuntado → esperar cierre natural (no CIERRE INMEDIATO)",
                        sym,
                    )
                else:
                    # Ratio mínimo requerido según el stake actual:
                    # $5→30x  $20→60x  $40→80x  $60→100x
                    # Un spike ratio=7x en $60 es ruido — evitar martingala sobre señal débil.
                    _idx       = state.crash_stake_idx if sym == "CRASH500" else state.boom_stake_idx
                    _stake_target = _CRASH500_STAKE_LADDER[min(_idx, len(_CRASH500_STAKE_LADDER) - 1)]
                    _min_ratio = [30, 60, 80, 100][min(_idx, 3)]

                    if _det_ratio < _min_ratio:
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s SPIKE_DETECTOR ivl=%.0fs ratio=%.0fx < %dx"
                            " para $%.0f → señal débil, sigue QUIET $1",
                            sym, _det_ivl, _det_ratio, _min_ratio, _stake_target,
                        )
                    else:
                        # CIERRE INMEDIATO — igual para CRASH500 y BOOM500
                        _cid  = int(state.contract_id)
                        _pnl  = state.current_profit
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s SPIKE_DETECTOR ivl=%.0fs ratio=%.0fx jump=%.2f"
                            " profit_$1=%.4f → CIERRE INMEDIATO → ACTIVE $%.0f",
                            sym, _det_ivl, _det_ratio, _det_jump, _pnl, _stake_target,
                        )
                        state.contract_id        = None
                        state.current_profit     = 0.0
                        try:
                            await self._executor.close_contract(_cid)
                        except Exception as _exc:
                            _LOGGER.error("[ENTRADA_DIEGO] %s SPIKE close error: %s", sym, _exc)
                        state.last_close_profit  = _pnl
                        state.profit_positive_ts = 0.0
                        state.reopens            = 0
                        state.spike_timer_active = False
                        self._add_global_pnl(sym, _pnl, now)
                        state.sym_mode           = "ACTIVE"
                        state.consec_max_holds   = 0
                        state.consec_wins_active = 0
                        state.prev_discharge     = False
                        await asyncio.sleep(3.0)  # Deriv requiere ~3s entre cierre y apertura
                        await self._open(sym, state, now)
                        return
                return

            # 3) Profit positivo por primera vez → PROFIT_TIMER
            # Excepción: si es un re-adjuntado en ACTIVE, no iniciar PROFIT_TIMER —
            # el contrato viejo cierra vía "cerrado externamente → reopen" y abre el $40 real.
            if state.current_profit > 0 and state.profit_positive_ts == 0.0:
                if state.is_readjusted and sym in SYMBOLS_500 and state.sym_mode == "ACTIVE":
                    pass  # esperar cierre externo del re-adjuntado → reopen real $40
                else:
                    if sym in SYMBOLS_500 and last_spike_ts > state.last_spike_ts > 0:
                        state.spike_interval_s    = last_spike_ts - state.last_spike_ts
                        state.trigger_spike_ratio = self._risk.get_last_spike_ratio(sym)
                        state.trigger_spike_jump  = self._risk.get_last_spike_jump(sym)
                    state.profit_timer_spikes = 0  # contador limpio al iniciar

                    # Normal: esperar PROFIT_TIMER completo (180s)
                    state.profit_positive_ts = now
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

                # QUIET: spike durante PROFIT_TIMER → resetear timer, contar descarga
                if sym in SYMBOLS_500 and state.sym_mode == "QUIET":
                    spike_ratio    = self._risk.get_last_spike_ratio(sym)
                    is_large_spike = spike_ratio >= CRASH500_RATIO_THRESHOLD  # mismo umbral ambos símbolos
                    state.profit_positive_ts = now  # extender timer
                    if is_large_spike:
                        state.profit_timer_spikes += 1
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s SPIKE en QUIET ratio=%.1fx → timer reset%s",
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
            # $40 ACTIVE: ACTIVE_PROFIT_WAIT_S (8min) para capturar estructura post-spike
            # $1 QUIET + SPIKE_TIMER: PROFIT_WAIT_S (3min)
            _timer_wait = (
                ACTIVE_PROFIT_WAIT_S
                if sym in SYMBOLS_500 and state.sym_mode == "ACTIVE"
                else PROFIT_WAIT_S
            )
            if now >= state.profit_positive_ts + _timer_wait:
                await self._close_profit_timer(sym, state, now)

        # ── COOLDOWN (1000s) ──────────────────────────────────────────────────
        elif state.phase == "COOLDOWN":
            if now >= state.cooldown_until:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s COOLDOWN terminado → abriendo stake=$%.0f",
                    sym, self._next_stake(sym, state.reopens),
                )
                await self._open(sym, state, now)

    # ── 500s: $20 fijo, SL=$15, sin timer (solo SL o floor cierran), protección $1/10min post-2-wins ──

    async def _process_500_simple(self, sym: str) -> None:
        state = self._states[sym]
        now   = time.time()

        if state.contract_id is not None:
            state.current_profit = self._query_profit(state.contract_id)

        # Sin contrato → abrir ($1 si estamos en protección, $20 si no)
        if state.phase == "IDLE" or state.contract_id is None:
            if now < state.broker_blocked_until_500:
                _secs_left = int(state.broker_blocked_until_500 - now)
                _LOGGER.debug("[ENTRADA_DIEGO] %s broker bloqueado %ds (precio post-spike alto)", sym, _secs_left)
                return
            state.phase = "OPEN"
            _stake = 1.0 if state.protecting_500 else None
            await self._open(sym, state, now, stake_override=_stake)
            return

        if state.phase != "OPEN":
            state.phase = "OPEN"

        # Actualizar peak profit (solo en contratos $20, no en protección $1)
        if not state.protecting_500 and state.current_profit > state.peak_profit_500:
            state.peak_profit_500 = state.current_profit

        # Calcular floor activo: el tier más alto que el peak ha cruzado
        _profit_floor = 0.0
        if not state.protecting_500:
            for _t in _PROFIT_TIERS_500:
                if state.peak_profit_500 >= _t:
                    _profit_floor = _t
                else:
                    break

        # ── SL=$15: cerrar y reabrir ──────────────────────────────────────────
        # Solo aplica a contratos $20: el $1 de protección nunca alcanza -$15.
        if state.current_profit < -SL_USD_500_SIMPLE and state.contract_id is not None:
            _cid = int(state.contract_id)
            _pnl = state.current_profit
            state.contract_id           = None
            state.current_profit        = 0.0
            state.peak_profit_500       = 0.0
            state.protecting_500        = False  # SL cancela protección
            state.consec_wins_500       = 0      # racha rota
            state.protection_started_at = 0.0
            try:
                await self._executor.close_contract(_cid)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s SL close error: %s", sym, exc)
            state.last_close_profit = _pnl
            state.reopens += 1
            self._add_global_pnl(sym, _pnl, now)
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s SL_HARD profit=%.4f < -%.0f → reopen#%d $%.0f",
                sym, _pnl, SL_USD_500_SIMPLE, state.reopens, STAKE_500_FIXED,
            )
            await asyncio.sleep(3.0)
            await self._open(sym, state, now)
            return

        # ── Trailing profit floor ─────────────────────────────────────────────
        # Tiers [3, 4, 5, 6, 7...]: cuando el peak cruza un tier y el profit cae
        # por debajo → cerrar y contabilizar como win. Tras 2 wins seguidos abrir
        # $1 por 10min antes de volver a $20.
        if _profit_floor > 0.0 and state.current_profit < _profit_floor and state.contract_id is not None:
            _cid  = int(state.contract_id)
            _pnl  = state.current_profit
            _peak = state.peak_profit_500
            state.contract_id     = None
            state.current_profit  = 0.0
            state.peak_profit_500 = 0.0
            try:
                await self._executor.close_contract(_cid)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s FLOOR close error: %s", sym, exc)
            state.last_close_profit = _pnl
            state.reopens = 0
            self._add_global_pnl(sym, _pnl, now)
            if _pnl > 0:
                state.consec_wins_500 += 1
                if state.consec_wins_500 >= 2:
                    state.protecting_500         = True
                    state.consec_wins_500        = 0
                    state.protection_started_at  = now
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s PROFIT_FLOOR peak=%.2f floor=$%.0f profit=%.4f wins=2 → PROTECCIÓN $1/10min",
                        sym, _peak, _profit_floor, _pnl,
                    )
                    await asyncio.sleep(1.0)
                    await self._open(sym, state, now, stake_override=1.0)
                else:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s PROFIT_FLOOR peak=%.2f floor=$%.0f profit=%.4f wins=%d → reopen $%.0f",
                        sym, _peak, _profit_floor, _pnl, state.consec_wins_500, STAKE_500_FIXED,
                    )
                    await asyncio.sleep(1.0)
                    await self._open(sym, state, now)
            else:
                state.consec_wins_500 = 0  # floor con PnL negativo (crash rápido) → racha rota
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s PROFIT_FLOOR peak=%.2f floor=$%.0f profit=%.4f (neg) → reopen $%.0f",
                    sym, _peak, _profit_floor, _pnl, STAKE_500_FIXED,
                )
                await asyncio.sleep(1.0)
                await self._open(sym, state, now)
            return

        # ── Timer: SOLO para protección $1 (10min) ───────────────────────────
        # El contrato $20 NO tiene timer — lo cierra el SL ($15) o el floor.
        # La protección $1 sí tiene un límite de 10min para volver a $20.
        if state.protecting_500 and state.protection_started_at > 0 and now >= state.protection_started_at + 600:
            _cid = state.contract_id
            _pnl = state.current_profit
            if _cid is not None:
                try:
                    await self._executor.close_contract(int(_cid))
                except Exception as exc:
                    _LOGGER.error("[ENTRADA_DIEGO] %s 10min PROT close error: %s", sym, exc)
            state.last_close_profit  = _pnl
            state.contract_id           = None
            state.current_profit        = 0.0
            state.peak_profit_500       = 0.0
            state.reopens               = 0
            state.protecting_500        = False
            state.consec_wins_500       = 0
            state.protection_started_at = 0.0
            self._add_global_pnl(sym, _pnl, now)
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s 10min PROT expirado profit=%.4f → protección OK, reopen $%.0f",
                sym, _pnl, STAKE_500_FIXED,
            )
            await asyncio.sleep(1.0)
            await self._open(sym, state, now)
            return

        # ── Cerrado externamente por broker (SL/TP del broker) ───────────────
        if state.contract_id is not None and self._query_contract(state.contract_id) is None:
            _pnl      = state.current_profit
            _was_prot = state.protecting_500
            state.last_close_profit  = _pnl
            state.contract_id        = None
            state.current_profit     = 0.0
            state.peak_profit_500    = 0.0
            self._add_global_pnl(sym, _pnl, now)
            if _was_prot:
                # El $1 de protección fue cerrado por el broker — reabrir $1
                # para mantener la protección activa hasta que expire el timer.
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s PROT $1 cerrado externamente profit=%.4f → reopen $1",
                    sym, _pnl,
                )
                await asyncio.sleep(1.0)
                await self._open(sym, state, now, stake_override=1.0)
            else:
                state.reopens += 1
                if _pnl > 0:
                    state.consec_wins_500 += 1
                    if state.consec_wins_500 >= 2:
                        state.protecting_500        = True
                        state.consec_wins_500       = 0
                        state.protection_started_at = now
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s cerrado externamente profit=%.4f wins=2 → PROTECCIÓN $1/10min",
                            sym, _pnl,
                        )
                        await asyncio.sleep(1.0)
                        await self._open(sym, state, now, stake_override=1.0)
                        return
                else:
                    state.consec_wins_500 = 0
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s cerrado externamente profit=%.4f reopen#%d $%.0f wins=%d",
                    sym, _pnl, state.reopens, STAKE_500_FIXED, state.consec_wins_500,
                )
                await asyncio.sleep(1.0)
                await self._open(sym, state, now)
            return

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

            # ── CRASH500 ACTIVE: lógica de escalación gradual ──────────────────────
            # $5 (idx=0) + profit real + sin descarga → escala a $20 dentro del mismo ciclo
            # $20/$40/$60 (idx≥1) + profit real → WIN de recuperación → QUIET, reset a $5
            # Descarga o ghost en cualquier nivel → QUIET, reset a $5
            if sym in SYMBOLS_500 and state.sym_mode == "ACTIVE":
                # Lógica de escalación ladder — CRASH500 y BOOM500 idénticos
                _idx   = state.crash_stake_idx if sym == "CRASH500" else state.boom_stake_idx
                _stake = _CRASH500_STAKE_LADDER[_idx]
                _is_dsch = spikes >= DISCHARGE_SPIKES_500

                if profit < MIN_WIN_ACTIVE_500:
                    # Ghost: reset total
                    if sym == "CRASH500":
                        state.crash_stake_idx = 0
                    else:
                        state.boom_stake_idx = 0
                    state.sym_mode           = "QUIET"
                    state.consec_wins_active = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s $%.0f GHOST %.4f → QUIET $1 (reset escalación)",
                        sym, _stake, profit,
                    )
                elif _is_dsch:
                    # Ganó pero mercado se descargó → QUIET, reset (energía agotada)
                    if sym == "CRASH500":
                        state.crash_stake_idx = 0
                    else:
                        state.boom_stake_idx = 0
                    state.sym_mode           = "QUIET"
                    state.consec_max_holds   = 0
                    state.consec_wins_active = 0
                    state.prev_discharge     = True
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s $%.0f WIN+DESCARGA %.4f (%d spike)"
                        " → QUIET $1 (reset escalación)",
                        sym, _stake, profit, spikes,
                    )
                elif _idx == 0:
                    # $5 ganó limpio → escalar a $20 (spike capturado, mercado activo)
                    if sym == "CRASH500":
                        state.crash_stake_idx = 1
                    else:
                        state.boom_stake_idx = 1
                    state.consec_max_holds = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s $5 WIN %.4f → escalando $20"
                        " (spike capturado, discharge OK — esperando 2do spike)",
                        sym, profit,
                    )
                else:
                    # $20/$40/$60 ganó → recuperación completa → QUIET, reset
                    if sym == "CRASH500":
                        state.crash_stake_idx = 0
                    else:
                        state.boom_stake_idx = 0
                    state.sym_mode           = "QUIET"
                    state.consec_wins_active = 0
                    state.consec_max_holds   = 0
                    state.prev_discharge     = False
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s $%.0f WIN %.4f → RECUPERACIÓN completa"
                        " → QUIET $1 (escalación reset)",
                        sym, _stake, profit,
                    )
                await self._open(sym, state, now)
                return

            # DESCARGA: spikes en PROFIT_TIMER = mercado agotó la energía → no escalar
            # Aplica en QUIET (si hubo spikes grandes, no ir a ACTIVE)
            quiet_discharge_thresh = (
                DISCHARGE_SPIKES_500  # =1: post-descarga → 1 spike basta para bloquear
                if state.prev_discharge
                else 2  # normal: ≥2 spikes bloquean QUIET
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
                # Win real desde QUIET con timer limpio → símbolo despertó → ACTIVE $5
                state.sym_mode           = "ACTIVE"
                state.consec_max_holds   = 0
                state.consec_wins_active = 0
                state.prev_discharge     = False
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CIERRE PROFIT+ %.4f → QUIET→ACTIVE $5 (símbolo despertó)",
                    sym, profit,
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
        ivl = state.spike_interval_s

        # Gate 1: spike consumido — el $5 previo ganó demasiado → energía liberada
        if state.last_close_profit > SPIKE_CONSUMED_THRESHOLD:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s SPIKE_CONSUMIDO prev=+%.2f > %.1f → $5",
                sym, state.last_close_profit, SPIKE_CONSUMED_THRESHOLD,
            )
            return False

        # Gate 2: spike era cluster (<60s desde el anterior) → WR=29%
        # Un cluster indica que la energía ya estaba disipándose, no acumulándose
        if sym in SYMBOLS_500 and 0 < ivl < 60:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s CLUSTER_SPIKE ivl=%.0fs < 60s → $5 (WR hist. 29%%)",
                sym, ivl,
            )
            return False

        # Gate 4 (500s): MERCADO_DESCARGADO — demasiados spikes recientes → energía agotada
        # Umbral por símbolo: BOOM≥3 bloquea (n=3 WR=38%), CRASH≥4 bloquea (n=3 WR=57% es bueno)
        now_ts = time.time()
        cutoff = now_ts - DISCHARGE_WINDOW_S
        recent_count = sum(1 for t in state.recent_spike_ts if t > cutoff)
        if recent_count >= DISCHARGE_MAX_SPIKES_CRASH:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s MERCADO_DESCARGADO %d spikes en %dmin → $5 (thresh=%d, esperar recarga)",
                sym, recent_count, DISCHARGE_WINDOW_S // 60, DISCHARGE_MAX_SPIKES_CRASH,
            )
            return False

        # Gate 5 (500s): SPIKE_FRESCO — rebote post-spike bloquea QUIET (aplica a ambos).
        # En ACTIVE (post CIERRE INMEDIATO), abrimos EN el spike (3s después) — no bloquear.
        if sym in SYMBOLS_500 and state.recent_spike_ts and state.sym_mode != "ACTIVE":
            sec_since_last = now_ts - max(state.recent_spike_ts)
            if 0 < sec_since_last < CRASH_FRESH_SPIKE_S:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s SPIKE_FRESCO %.0fs < %ds → sigue QUIET $1 (rebote post-crash esperado)",
                    sym, sec_since_last, CRASH_FRESH_SPIKE_S,
                )
                return False

        return True

    def _next_stake(self, sym: str, reopens: int = 0, now: float = 0.0) -> float:
        if sym in SYMBOLS_500:
            return STAKE_500_FIXED
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
        elif sym in SYMBOLS_500:
            _sl_pct = SL_USD_500_SIMPLE / STAKE_500_FIXED  # 12/20 = 0.60
            _mult, _sl, _tp, _mh = MULTIPLIER, _sl_pct, 0.65, float(HOLD_TIME_S_500_SIMPLE)
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
                if sym in SYMBOLS_500:
                    state.spikes_in_contract_500 = 0
                    state.last_spike_ts_500 = float(self._risk.get_last_spike_ts(sym) or 0.0)
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
                    if sym in SYMBOLS_500:
                        # Para 500s: stake es $20 fijo — no cascadear a $9, $4, $2
                        # Si el broker rechaza cualquier monto, esperar 60s y reintentar desde $20
                        state.broker_blocked_until_500 = now + 60.0
                        state.phase = "IDLE"
                        _LOGGER.warning(
                            "[ENTRADA_DIEGO] %s broker rechazó $%.2f (max=%.2f) → espera 60s",
                            sym, stake, max_allowed,
                        )
                        return
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
        final_profit     = state.current_profit
        _spike_det_timer = state.spike_timer_active
        state.spike_timer_active = False  # siempre resetear antes de cualquier await

        _tag = " [SPIKE_TIMER]" if _spike_det_timer else ""
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s PROFIT_TIMER cumplido%s → cerrando contract=%s profit=%.4f",
            sym, _tag, state.contract_id, final_profit,
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
                            st.sym_mode              = s.get("sym_mode", "QUIET")
                            st.consec_max_holds      = int(s.get("consec_max_holds", 0))
                            st.consec_wins_active    = int(s.get("consec_wins_active", 0))
                            st.prev_discharge        = bool(s.get("prev_discharge", False))
                            st.trigger_spike_ratio   = float(s.get("trigger_spike_ratio", 0.0))
                            st.trigger_spike_jump    = float(s.get("trigger_spike_jump",  0.0))
                            st.spike_timer_active    = bool(s.get("spike_timer_active",  False))
                            st.profit_timer_spikes   = int(s.get("profit_timer_spikes",  0))
                            st.crash_stake_idx       = int(s.get("crash_stake_idx",      0))
                            st.boom_stake_idx        = int(s.get("boom_stake_idx",       0))
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
