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
import bisect
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

# BOOM500: máquina 4 niveles (STAKE_1 → STAKE_10 → STAKE_20 → STAKE_40)
# CRASH500: máquina adaptativa — S1(7-20m adapt) → S20(20m) → S40(20m) → S60(20m)
#   S1 adaptativo: 0spk<20m→S20 | ≤1spk@20m→S20 | 1spk<20m→S1 | 2spk→12m | 3spk→17m | 4+spk→20m
#   S20 20m: 0spk+WIN→S1(20m) | 1spk+WIN→S40 | 2+spk+WIN→S1(10m) | LOSE(any)→S40
#   S40 20m: WIN→S1(7m) | LOSE+spike→S1(7m) | LOSE 0spk→S60
#   S60 20m: WIN→S1(20m) | LOSE→retry S60 (infinito hasta ganar)
#   SL duro: 90% del stake activo → S1 (reset timer a 7min)
BURST_STAKE1_AMOUNT          = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE1",              "1.0"))
BURST_STAKE1_DURATION_S      = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_S",              "360"))   # 6min BOOM500
BURST_STAKE1_DROUGHT_S       = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_DROUGHT_S",      "1200"))  # 20min sequia BOOM500
BURST_STAKE1_CRASH500_S      = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_S",     "420"))   # 7min CRASH500 S1 base
BURST_STAKE1_CRASH500_10M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_10M_S", "600"))   # 10min: S20 2+spk+win
BURST_STAKE1_CRASH500_12M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_12M_S", "720"))   # 12min: S1 captura 2 spikes
BURST_STAKE1_CRASH500_17M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_17M_S", "1020"))  # 17min: S1 captura 3 spikes
BURST_STAKE1_CRASH500_20M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_20M_S", "1200"))  # 20min: max / S20 WIN 0spk / S60 WIN
BURST_STAKE5_AMOUNT          = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE5",              "5.0"))
BURST_STAKE5_DURATION_S      = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE5_S",              "900"))   # legacy CRASH500 S5 (ya sin ruta activa)
BURST_STAKE10_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE10",             "10.0"))
BURST_STAKE10_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE10_S",             "900"))   # 15min defensivo BOOM500
BURST_STAKE20_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE20",             "20.0"))
BURST_STAKE20_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE20_S",             "900"))   # 15min BOOM500
BURST_STAKE20_CRASH500_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE20_CRASH500_S",    "1200"))  # 20min CRASH500 S20
BURST_STAKE40_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE40",             "40.0"))
BURST_STAKE40_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE40_S",             "900"))   # 15min BOOM500
BURST_STAKE40_CRASH500_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE40_CRASH500_S",    "1200"))  # 20min CRASH500 S40
BURST_STAKE60_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE60",             "60.0"))
BURST_STAKE60_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE60_S",             "1200"))  # 20min CRASH500 S60
BURST_STAKE40_MAX_HOUR_SPIKES = int(os.getenv("ENTRADA_DIEGO_BURST_S40_MAX_HOUR_SPKS", "5"))  # gate: máx spikes en hora UTC para abrir $40
HOUR_PROFIT_PROTECT_USD      = float(os.getenv("ENTRADA_DIEGO_HOUR_PROFIT_PROTECT", "4.0")) # protección: ≥$4 ganado en la hora → parar hasta siguiente hora

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

# Timeout mercado seco: $20 negativo ≥N min con ≤M spikes → cerrar y abrir $1
# Dato 12h: contratos con SL duran 28-66min en negativo sin spikes = mercado seco
TIMEOUT_DRY_S        = int(os.getenv("ENTRADA_DIEGO_500_TIMEOUT_DRY_S",     "1200"))  # 20 min
TIMEOUT_DRY_MAX_SPKS = int(os.getenv("ENTRADA_DIEGO_500_TIMEOUT_DRY_SPKS",  "2"))     # ≤2 spikes durante el contrato = seco
# Dead burst kill: si $20 lleva >N segundos SIN UN SOLO spike durante el contrato → burst muerto
# Dato: 100% de losses tienen 0 spikes en el contrato. 100% de wins tienen ≥1 spike.
DEAD_BURST_KILL_S    = int(os.getenv("ENTRADA_DIEGO_500_DEAD_BURST_S",      "600"))   # 10 min sin spike → salir

# Gate descarga: si el símbolo tuvo MUCHOS spikes en la última hora = descargando energía
# → próxima apertura en $1 (proteger). Si POCOS spikes = acumulando tensión → abrir $20.
DISCHARGE_SPKS_1H    = int(os.getenv("ENTRADA_DIEGO_500_DISCHARGE_SPKS_1H", "8"))     # ≥8/h = descargado → $1

# Gate DISPLACEMENT: cuando el índice se movió demasiado en dirección favorable en las
# últimas DISP_WINDOW_H horas → el mercado está "construyendo estructura" → abrir $1 (no $20).
# BOOM500: desplazamiento = (precio_ahora - precio_Xh_atrás) / precio_Xh_atrás > +THRESH% → $1
# CRASH500: desplazamiento < -THRESH% (precio bajó más de X%) → $1
# Datos históricos 3119 contratos Jun22-Jul6:
#   >+1.0%: WR=38.4% vs 45.5% normal (diff=-7.1%)   EV=-$76/h vs -$2/h zona NEUTRO_NEG
#   >+1.5%: WR=27.7% vs 45.2% normal (diff=-17.5%)
DISP_WINDOW_H    = float(os.getenv("ENTRADA_DIEGO_DISP_WINDOW_H",   "6.0"))   # ventana en horas
DISP_THRESH_PCT  = float(os.getenv("ENTRADA_DIEGO_DISP_THRESH_PCT", "1.0"))   # threshold %
_SLOPE_CACHE_TTL = 300.0   # refrescar caché de slope cada 5 min
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
    protecting_500:         bool  = False # True: estamos en $1 sensor
    consec_wins_500:        int   = 0    # wins consecutivos con PnL>0 en $20
    protection_started_at:  float = 0.0  # epoch inicio del $1 sensor actual
    protection_spikes:      int   = 0    # spikes desde que entró a este $1 sensor (se resetea al volver de $20)
    burst_spikes_total:     int   = 0    # acumulado global del burst en la hora UTC actual
    burst_started_at:       float = 0.0  # inicio de la hora UTC anclada; 0 = sin burst activo
    burst_phase:            str   = "IDLE"   # IDLE | COOLDOWN | STAKE_20 | STAKE_40
    burst_phase_started_at: float = 0.0     # epoch inicio de la fase actual
    s1_drought_mode:        bool  = False   # True = S1 extendido 20min post-S20 sin spike
    s20_consec_losses:      int   = 0       # losses consecutivos en S20 (2 → drought S1 20min)
    s20_crash500_wins:      int   = 0       # wins consecutivos en S20 CRASH500 (2 → reset a S1)
    hour_pnl_500:           float = 0.0     # PnL acumulado en la hora UTC actual (protección $4)
    prev_hour_pnl_500:      float = 0.0     # PnL hora anterior — protección $4 solo si prev>=0
    hour_spike_count_500:  int   = 0        # spikes en la hora UTC actual (gate STAKE_40)
    hour_start_ts_500:     float = 0.0      # inicio de la hora UTC vigente (epoch redondeado a hora)
    contract_start_hour_ts_500: float = 0.0  # hora UTC (epoch) cuando se abrió el contrato actual
    broker_blocked_until_500: float = 0.0  # hasta cuándo esperar si broker cap (precio post-spike muy alto)
    crash500_s1_timer_s:     float = 420.0  # timer adaptativo S1 CRASH500 (7/10/12/17/20min)
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
            "spikes_in_contract":     self.spikes_in_contract_500,
            "last_spike_ts_500":      round(self.last_spike_ts_500, 3),
            "hour_spike_count_500":        self.hour_spike_count_500,
            "hour_start_ts_500":           round(self.hour_start_ts_500, 3),
            "contract_start_hour_ts_500":  round(self.contract_start_hour_ts_500, 3),
            "burst_phase":                 self.burst_phase,
            "burst_phase_started_at": round(self.burst_phase_started_at, 3),
            "s1_drought_mode":        self.s1_drought_mode,
            "s20_consec_losses":      self.s20_consec_losses,
            "s20_crash500_wins":      self.s20_crash500_wins,
            "hour_pnl_500":           round(self.hour_pnl_500, 4),
            "prev_hour_pnl_500":      round(self.prev_hour_pnl_500, 4),
            "hour_profit_protect_usd": HOUR_PROFIT_PROTECT_USD,
            "crash500_s1_timer_s":          self.crash500_s1_timer_s,
            "burst_stake1_s":               BURST_STAKE1_DURATION_S,
            "burst_stake1_drought_s":       BURST_STAKE1_DROUGHT_S,
            "burst_stake1_crash500_s":      BURST_STAKE1_CRASH500_S,
            "burst_stake1_crash500_10m_s":  BURST_STAKE1_CRASH500_10M_S,
            "burst_stake1_crash500_12m_s":  BURST_STAKE1_CRASH500_12M_S,
            "burst_stake1_crash500_17m_s":  BURST_STAKE1_CRASH500_17M_S,
            "burst_stake1_crash500_20m_s":  BURST_STAKE1_CRASH500_20M_S,
            "burst_stake5_s":               BURST_STAKE5_DURATION_S,
            "burst_stake20_s":              BURST_STAKE20_DURATION_S,
            "burst_stake20_crash500_s":     BURST_STAKE20_CRASH500_S,
            "burst_stake40_s":              BURST_STAKE40_DURATION_S,
            "burst_stake40_crash500_s":     BURST_STAKE40_CRASH500_S,
            "burst_stake60_s":              BURST_STAKE60_DURATION_S,
            "burst_stake1_amount":          BURST_STAKE1_AMOUNT,
            "burst_stake5_amount":          BURST_STAKE5_AMOUNT,
            "burst_stake20_amount":         BURST_STAKE20_AMOUNT,
            "burst_stake40_amount":         BURST_STAKE40_AMOUNT,
            "burst_stake60_amount":         BURST_STAKE60_AMOUNT,
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

        # Displacement gate cache — precarga y TTL
        self._slope_cache:    dict[str, list[tuple[float, float]]] = {}
        self._slope_cache_ts: float = 0.0
        self._watchdog_started: set[str] = set()

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
                    # BOOM/CRASH 500 necesitan watchdog independiente por si el WS se cae
                    for _s5 in SYMBOLS_500:
                        if _s5 not in SYMBOLS_ED_DISABLED and _s5 not in self._watchdog_started:
                            self._watchdog_started.add(_s5)
                            asyncio.create_task(self._500_watchdog_loop(_s5))
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
            _queried_profit = self._query_profit(state.contract_id)
            if _queried_profit is not None:
                state.current_profit = _queried_profit

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

    # ── 500s: máquina 3 niveles STAKE_1 → STAKE_20 → STAKE_40 ──────────────────

    async def _process_500_simple(self, sym: str) -> None:
        """
        BOOM500: STAKE_1(6m) → STAKE_10(15m) → STAKE_20(15m) → STAKE_40(15m) — spike-gated
        CRASH500: escalera $1(5m) → $5(10m) → $20(15m) → $40(15m)
          S1/S5: siempre escalan al timer (ganen o pierdan)
          S20: spike+win → S40 | lo demás → S1
          S40: siempre → S1 (fin ciclo)
          SL duro 90% stake → S1; protección hora=$4 → reset ciclo (sigue operando)
        """
        state = self._states[sym]
        now   = time.time()

        if state.contract_id is not None:
            _queried_profit = self._query_profit(state.contract_id)
            if _queried_profit is not None:
                state.current_profit = _queried_profit
            else:
                _LOGGER.debug(
                    "[ENTRADA_DIEGO] %s contrato %s ya cerrado por broker — conservando profit=%.4f",
                    sym, state.contract_id, state.current_profit,
                )

        # ── Spike tracking ────────────────────────────────────────────────────
        _last_spk_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)
        if _last_spk_ts > state.last_spike_ts_500 and _last_spk_ts > 0:
            state.last_spike_ts_500 = _last_spk_ts
            # Contador de spikes por hora UTC (gate STAKE_40)
            _cur_hour_start = int(_last_spk_ts // 3600) * 3600.0
            if state.hour_start_ts_500 != _cur_hour_start:
                state.hour_start_ts_500    = _cur_hour_start
                state.hour_spike_count_500 = 0
                state.hour_pnl_500         = 0.0
            state.hour_spike_count_500 += 1
            if state.burst_phase in ("STAKE_1", "STAKE_10", "STAKE_20", "STAKE_40") and state.contract_id is not None:
                state.spikes_in_contract_500 += 1
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s spike fase=%s spk_contrato=%d spk_hora=%d",
                sym, state.burst_phase, state.spikes_in_contract_500, state.hour_spike_count_500,
            )
            self._persist(now)  # guarda spikes_in_contract, last_spike_ts_500, hour_spike_count en disco

        # ── ORPHAN: contrato con fase sin definir → cerrar → STAKE_1 ─────────
        if state.burst_phase == "IDLE" and state.contract_id is not None:
            _cid = int(state.contract_id)
            _pnl = state.current_profit
            state.contract_id            = None
            state.current_profit         = 0.0
            state.peak_profit_500        = 0.0
            state.spikes_in_contract_500 = 0
            try:
                await self._executor.close_contract(_cid)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s ORPHAN close error: %s", sym, exc)
            state.last_close_profit = _pnl
            self._add_global_pnl(sym, _pnl, now)
            _np_orphan = "STAKE_1"
            _LOGGER.info("[ENTRADA_DIEGO] %s ORPHAN %d cerrado pnl=%.4f → %s", sym, _cid, _pnl, _np_orphan)
            state.burst_phase            = _np_orphan
            state.burst_phase_started_at = 0.0
            return

        # ── Sin contrato → abrir el stake del nivel actual ────────────────────
        if state.contract_id is None:
            if now < state.broker_blocked_until_500:
                return
            # ── HOUR_PROFIT_PROTECT: ≥$4 ganado esta hora → esperar siguiente hora ──
            _cur_hour = int(now // 3600) * 3600.0
            if _cur_hour != state.hour_start_ts_500:
                state.prev_hour_pnl_500 = state.hour_pnl_500  # guarda PnL de la hora que terminó
                state.hour_pnl_500 = 0.0                       # nueva hora → reset acumulado
            # BOOM500: protección $4 solo si la hora anterior fue ganadora (>=0)
            # Si la hora anterior fue pérdida, esta hora corre sin límite para recuperar
            _prev_was_win = state.prev_hour_pnl_500 >= 0.0
            if state.hour_pnl_500 >= HOUR_PROFIT_PROTECT_USD and sym != "CRASH500" and _prev_was_win:
                if state.burst_phase != "STAKE_1" or state.burst_phase_started_at != 0.0:
                    _wait_s = int((_cur_hour + 3600.0) - now)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s HOUR_PROFIT_PROTECT +$%.2f≥$%.0f (prev_hora=+$%.2f) → pausa %ds",
                        sym, state.hour_pnl_500, HOUR_PROFIT_PROTECT_USD, state.prev_hour_pnl_500, _wait_s,
                    )
                state.burst_phase            = "STAKE_1"
                state.burst_phase_started_at = 0.0
                return
            if state.burst_phase not in ("STAKE_1", "STAKE_5", "STAKE_10", "STAKE_20", "STAKE_40", "STAKE_60"):
                state.burst_phase = "STAKE_1"
            if state.burst_phase_started_at == 0.0:
                state.burst_phase_started_at = now
            state.spikes_in_contract_500 = 0
            stake = (BURST_STAKE1_AMOUNT   if state.burst_phase == "STAKE_1"
                else BURST_STAKE5_AMOUNT   if state.burst_phase == "STAKE_5"
                else BURST_STAKE10_AMOUNT  if state.burst_phase == "STAKE_10"
                else BURST_STAKE20_AMOUNT  if state.burst_phase == "STAKE_20"
                else BURST_STAKE40_AMOUNT  if state.burst_phase == "STAKE_40"
                else BURST_STAKE60_AMOUNT)
            state.contract_start_hour_ts_500 = int(now // 3600) * 3600.0
            await self._open(sym, state, now, stake_override=stake)
            return

        state.phase = "OPEN"

        # Actualizar peak profit del contrato actual
        if state.current_profit > state.peak_profit_500:
            state.peak_profit_500 = state.current_profit

        # ── SL duro: 90% del stake → STAKE_1 ─────────────────────────────────
        _sl_limit = (-(BURST_STAKE60_AMOUNT * 0.90) if state.burst_phase == "STAKE_60"
            else    -(BURST_STAKE40_AMOUNT * 0.90)  if state.burst_phase == "STAKE_40"
            else    -(BURST_STAKE20_AMOUNT * 0.90)  if state.burst_phase == "STAKE_20"
            else    -(BURST_STAKE10_AMOUNT * 0.90)  if state.burst_phase == "STAKE_10"
            else    -(BURST_STAKE5_AMOUNT  * 0.90)  if state.burst_phase == "STAKE_5"
            else    -(BURST_STAKE1_AMOUNT  * 0.90))
        if state.current_profit < _sl_limit and state.contract_id is not None:
            _cid = int(state.contract_id)
            _pnl = state.current_profit
            state.contract_id            = None
            state.current_profit         = 0.0
            state.peak_profit_500        = 0.0
            state.spikes_in_contract_500 = 0
            try:
                await self._executor.close_contract(_cid)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s SL close error: %s", sym, exc)
            state.last_close_profit = _pnl
            self._add_global_pnl(sym, _pnl, now)
            if int(state.contract_start_hour_ts_500 // 3600) == int(now // 3600):
                state.hour_pnl_500 += _pnl
            # CRASH500: SL siempre a S1 (reset timer 7min); BOOM500: STAKE_40 se mantiene en S40
            _sl_next = ("STAKE_1" if sym == "CRASH500"
                        else "STAKE_40" if state.burst_phase == "STAKE_40"
                        else "STAKE_1")
            if sym == "CRASH500":
                state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_S  # reset tras SL
            _LOGGER.warning("[ENTRADA_DIEGO] %s SL_HARD pnl=%.4f fase=%s → %s", sym, _pnl, state.burst_phase, _sl_next)
            state.burst_phase            = _sl_next
            state.burst_phase_started_at = 0.0
            return

        # ── Helper interno: cerrar + poll + abrir siguiente ───────────────────
        async def _transition(next_phase: str, next_stake: float,
                              label: str, _cid: int, _pnl: float, _spk: int) -> None:
            state.contract_id            = None
            state.current_profit         = 0.0
            state.peak_profit_500        = 0.0
            state.spikes_in_contract_500 = 0
            try:
                await self._executor.close_contract(_cid)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s %s close error: %s", sym, label, exc)
            state.last_close_profit = _pnl
            self._add_global_pnl(sym, _pnl, now)
            if int(state.contract_start_hour_ts_500 // 3600) == int(now // 3600):
                state.hour_pnl_500 += _pnl
            _LOGGER.info("[ENTRADA_DIEGO] %s %s pnl=%.4f spk=%d → %s", sym, label, _pnl, _spk, next_phase)
            state.burst_phase = next_phase
            _t0 = time.time()
            while time.time() - _t0 < 5.0:
                if self._query_contract(_cid) is None:
                    break
                await asyncio.sleep(0.3)
            state.burst_phase_started_at      = time.time()
            state.spikes_in_contract_500      = 0
            state.contract_start_hour_ts_500  = int(time.time() // 3600) * 3600.0
            await self._open(sym, state, time.time(), stake_override=next_stake)

        # ── Helper: spikes en la hora UTC actual (con reset si cambió la hora) ─
        def _get_hour_spike_count() -> int:
            _cur = int(now // 3600) * 3600.0
            if state.hour_start_ts_500 != _cur:
                state.hour_start_ts_500    = _cur
                state.hour_spike_count_500 = 0
                state.hour_pnl_500         = 0.0
            return state.hour_spike_count_500

        # ── Helper: ¿el contrato cruzó un límite de hora UTC? ────────────────
        def _hour_changed_since_open() -> bool:
            if state.contract_start_hour_ts_500 == 0.0:
                return False
            return int(state.contract_start_hour_ts_500 // 3600) != int(now // 3600)

        # ── Helper: siguiente fase/stake en cambio de hora ───────────────────
        # Decide según spikes acumulados en la NUEVA hora UTC
        def _next_on_hour_change() -> tuple:
            _h_spk = _get_hour_spike_count()
            if _h_spk == 0:
                return "STAKE_1",  BURST_STAKE1_AMOUNT
            elif _h_spk == 1:
                return "STAKE_10", BURST_STAKE10_AMOUNT
            else:
                return "STAKE_20", BURST_STAKE20_AMOUNT

        # ── Helper: gap gate — bloquea S20 si el mercado lleva >30min sin spikes ──
        # Datos 54+ días: gap 0-30min → 15-22% cuartos vacíos (activo)
        #                 gap 30-60min → 61% vacíos BOOM / 32% CRASH (sequía)
        #                 gap >60min   → 67-100% vacíos (sequía profunda)
        def _gap_gate_s20() -> bool:
            if state.last_spike_ts_500 <= 0:
                return True
            return (now - state.last_spike_ts_500) < 30.0 * 60

        def _gap_gate_s40() -> bool:
            """S40 solo abre si el mercado está muy activo: último spike <15min."""
            if state.last_spike_ts_500 <= 0:
                return True
            return (now - state.last_spike_ts_500) < 15.0 * 60

        def _crash500_mid_hour_s20() -> bool:
            """CRASH500 mid-hora: Q1 fue activo (≥3spk acum) + quedan cuartos (min<45) + mercado vivo.
            Datos 54d: si Q1≥3spk → 97% prob de ≥1 spike en Q3+Q4 restantes (avg 4.4 spikes).
            Permite escalar a S20 aunque S1 tuviera 0 spikes — la hora ya demostró actividad."""
            _acum = _get_hour_spike_count()
            _min  = (now % 3600.0) / 60.0
            return _acum >= 3 and _min < 45 and _gap_gate_s20()

        def _discharge_s20_gate() -> bool:
            """Bloquea S1→S20 cuando la hora ya está descargada (aplica BOOM+CRASH).
            Q1+Q2 (<30min): si >7 spikes acumulados → hora quemada, esperar siguiente.
            Q4   (≥45min): si >10 spikes acumulados → hora agotada, esperar siguiente.
            """
            _acum = _get_hour_spike_count()
            _min  = (now % 3600.0) / 60.0
            if _min < 30 and _acum > 7:
                return False   # Q1+Q2 quemado
            if _min >= 45 and _acum > 10:
                return False   # Q4 agotado
            return True

        # ── Helper: drought gate para STAKE_1 — decide S1/S10/S20 ────────────
        # Se llama cuando STAKE_1 termina con <2 spikes en el contrato.
        # Usa el minuto actual de la hora y los spikes acumulados.
        #   0 spk a min>=30: 68% prob MALA → quedarse en S1
        #   0 spk a min>=25 ó <=1 spk a min>=30: zona defensiva → S10
        #   resto: hora normal → S20
        def _drought_gate_s1() -> tuple:
            _min_in_hour = (now % 3600.0) / 60.0
            _acum_spk    = _get_hour_spike_count()
            if _acum_spk == 0 and _min_in_hour >= 30:
                return "STAKE_1",  BURST_STAKE1_AMOUNT
            if (_acum_spk == 0 and _min_in_hour >= 25) or (_acum_spk <= 1 and _min_in_hour >= 30):
                return "STAKE_10", BURST_STAKE10_AMOUNT
            if not _gap_gate_s20():
                return "STAKE_1",  BURST_STAKE1_AMOUNT
            return "STAKE_20", BURST_STAKE20_AMOUNT

        # ── Helper: gate BUENO para STAKE_40 ─────────────────────────────────
        # True solo si la hora proyecta hacia BUENO (≥6 spk/hora).
        # Calibrado en 682-709 horas válidas (54+ días), P(BUENO)≥60% en cada umbral.
        # La lógica es INCLUSIVA (mínimo requerido), no exclusiva (máximo permitido).
        #   QA [0-15 min]  → ≥1 spk  (60-65% BUENO)
        #   QB [15-30 min] → ≥3 spk  (65-66% BUENO)
        #   QC [30-45 min] → ≥5 spk  (85-88% BUENO)
        #   QD [45-60 min] → ≥6 spk  (100% BUENO)
        def _s40_bueno_gate() -> bool:
            _min = (now % 3600.0) / 60.0
            _spk = _get_hour_spike_count()
            if _min < 15:  return _spk >= 1
            if _min < 30:  return _spk >= 3
            if _min < 45:  return _spk >= 5
            return             _spk >= 6

        def _s40_gate_label() -> str:
            _min = (now % 3600.0) / 60.0
            _spk = _get_hour_spike_count()
            _q   = ('A','B','C','D')[min(3, int(_min // 15))]
            _req = (1, 3, 5, 6)[min(3, int(_min // 15))]
            return f"Q{_q}:{_spk}/{'≥'+str(_req)}"

        # ── Piso de profit al 80% del peak (STAKE_10/20/40 — STAKE_1 siempre corre 10min) ──
        _peak_trigger = (4.00 if state.burst_phase == "STAKE_40"
                    else 2.00 if state.burst_phase == "STAKE_20"
                    else 1.00 if state.burst_phase == "STAKE_10"
                    else 9999.0)  # STAKE_1: nunca dispara
        if (state.burst_phase != "STAKE_1"
                and state.peak_profit_500 >= _peak_trigger
                and state.current_profit < state.peak_profit_500 * 0.80
                and state.contract_id is not None):
            _cid   = int(state.contract_id)
            _pnl   = state.current_profit
            _spk   = state.spikes_in_contract_500
            _peak  = state.peak_profit_500
            _phase = state.burst_phase
            if _phase == "STAKE_10":
                # profit floor en $10: si capturó 2+ spikes → S1; 1spk → S20; 0spk → S20
                if _spk >= 2:
                    _np, _ns = "STAKE_1",  BURST_STAKE1_AMOUNT
                else:
                    _np, _ns = "STAKE_20", BURST_STAKE20_AMOUNT
            elif _phase == "STAKE_20":
                if sym == "CRASH500":
                    # 0spk+WIN→S1(20m) | 1spk+WIN→S40 | 2+spk+WIN→S1(10m) | LOSE→S40
                    if _pnl > 0:
                        if _spk == 0:
                            state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                            _np, _ns = "STAKE_1", BURST_STAKE1_AMOUNT
                        elif _spk == 1:
                            _np, _ns = "STAKE_40", BURST_STAKE40_AMOUNT
                        else:
                            state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_10M_S
                            _np, _ns = "STAKE_1", BURST_STAKE1_AMOUNT
                    else:
                        _np, _ns = "STAKE_40", BURST_STAKE40_AMOUNT
                elif _spk >= 2:
                    _np, _ns = "STAKE_1",  BURST_STAKE1_AMOUNT
                elif _spk == 1:
                    _np, _ns = ("STAKE_1", BURST_STAKE1_AMOUNT) if _pnl > 0 else ("STAKE_20", BURST_STAKE20_AMOUNT)
                else:  # 0 spikes: único camino a S40 — solo si hora proyecta BUENO
                    if _s40_bueno_gate():
                        _np, _ns = "STAKE_40", BURST_STAKE40_AMOUNT
                    else:
                        _np, _ns = "STAKE_20", BURST_STAKE20_AMOUNT
            elif _phase == "STAKE_40":
                if sym == "CRASH500":
                    # WIN→S1(7m) | LOSE+spike→S1(7m) | LOSE 0spk→S60
                    if _pnl > 0 or _spk >= 1:
                        state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_S
                        _np, _ns = "STAKE_1", BURST_STAKE1_AMOUNT
                    else:
                        _np, _ns = "STAKE_60", BURST_STAKE60_AMOUNT
                else:
                    _np = "STAKE_1"  if _pnl > 0 else "STAKE_40"
                    _ns = BURST_STAKE1_AMOUNT if _pnl > 0 else BURST_STAKE40_AMOUNT
            elif _phase == "STAKE_60":
                # CRASH500: WIN→S1(20m) | LOSE→retry S60
                if _pnl > 0:
                    state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                    _np, _ns = "STAKE_1", BURST_STAKE1_AMOUNT
                else:
                    _np, _ns = "STAKE_60", BURST_STAKE60_AMOUNT
            else:  # STAKE_1 — nunca llega aquí (peak_trigger=9999), pero por seguridad
                _np, _ns = "STAKE_1", BURST_STAKE1_AMOUNT
            await _transition(_np, _ns,
                f"PROFIT_FLOOR peak={_peak:.2f} floor={_peak*0.80:.2f} spk={_spk}",
                _cid, _pnl, _spk)
            return

        # ── Timer STAKE_1 CRASH500: adaptativo (7→12→17→20min según spikes) ──────
        if (sym == "CRASH500"
                and state.burst_phase == "STAKE_1"
                and now >= state.burst_phase_started_at + state.crash500_s1_timer_s
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            _cur_t  = state.crash500_s1_timer_s
            # ≥17min: ≤1 spike ya permite escalar; <17min: solo 0 spikes escalan
            _at_max = (_cur_t >= BURST_STAKE1_CRASH500_17M_S)
            if (_spk == 0 and not _at_max) or (_spk <= 1 and _at_max):
                # escalada a S20: 0spk<17m | ≤1spk@17-20m
                state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_S  # reset para siguiente ciclo
                await _transition("STAKE_20", BURST_STAKE20_AMOUNT,
                    f"CRASH500 S1 {int(_cur_t//60)}min {_spk}spk→S20", _cid, _pnl, _spk)
            elif _spk == 1:
                # 1 spike, timer < 17min → quedar en S1 con mismo timer
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT,
                    f"CRASH500 S1 {int(_cur_t//60)}min 1spk→S1(mismo_timer)", _cid, _pnl, _spk)
            elif _spk == 2:
                state.crash500_s1_timer_s = max(_cur_t, BURST_STAKE1_CRASH500_12M_S)
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT,
                    f"CRASH500 S1 {int(_cur_t//60)}min 2spk→S1({int(state.crash500_s1_timer_s//60)}min)", _cid, _pnl, _spk)
            elif _spk == 3:
                state.crash500_s1_timer_s = max(_cur_t, BURST_STAKE1_CRASH500_17M_S)
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT,
                    f"CRASH500 S1 {int(_cur_t//60)}min 3spk→S1({int(state.crash500_s1_timer_s//60)}min)", _cid, _pnl, _spk)
            else:  # 4+ spikes → 20min máximo
                state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT,
                    f"CRASH500 S1 {int(_cur_t//60)}min {_spk}spk→S1(20min)", _cid, _pnl, _spk)
            return

        # ── Timer STAKE_5 CRASH500: 10min — siempre escala a S20 (gane o pierda) ──
        if (sym == "CRASH500"
                and state.burst_phase == "STAKE_5"
                and now >= state.burst_phase_started_at + BURST_STAKE5_DURATION_S
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            await _transition("STAKE_20", BURST_STAKE20_AMOUNT, f"CRASH500 S5 10min→S20 pnl={_pnl:.2f} spk={_spk}", _cid, _pnl, _spk)
            return

        # ── Timer STAKE_1 BOOM500: 6min (normal) o 20min (sequia post-S20 sin spike) ─
        _s1_dur   = BURST_STAKE1_DROUGHT_S if state.s1_drought_mode else BURST_STAKE1_DURATION_S
        _s1_label = "20min(sequia)" if state.s1_drought_mode else "6min"
        if (sym != "CRASH500"
                and state.burst_phase == "STAKE_1"
                and now >= state.burst_phase_started_at + _s1_dur
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            state.s1_drought_mode = False  # salir de S1 siempre limpia el modo sequia
            # 1. Cambio de hora: la siguiente apertura se calibra con la hora nueva
            if _hour_changed_since_open():
                _np, _ns = _next_on_hour_change()
                await _transition(_np, _ns, f"STAKE_1 {_s1_label} cambio_hora→{_np}", _cid, _pnl, _spk)
                return
            # BOOM500: ≥2 spikes → S1; <2 → drought gate + discharge gate
            if _spk >= 2:
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"STAKE_1 {_s1_label} ≥2spk→S1", _cid, _pnl, _spk)
            else:
                _np, _ns = _drought_gate_s1()
                if _np == "STAKE_20" and not _discharge_s20_gate():
                    _h = _get_hour_spike_count(); _m = int((now % 3600.0) / 60.0)
                    _np, _ns = "STAKE_1", BURST_STAKE1_AMOUNT
                    await _transition(_np, _ns, f"STAKE_1 {_s1_label} <2spk→S1(hora_desc:{_h}spkh,min{_m})", _cid, _pnl, _spk)
                else:
                    await _transition(_np, _ns, f"STAKE_1 {_s1_label} <2spk→{_np}", _cid, _pnl, _spk)
            return

        # ── Timer STAKE_10: 15min (defensivo) ────────────────────────────────
        if (state.burst_phase == "STAKE_10"
                and now >= state.burst_phase_started_at + BURST_STAKE10_DURATION_S
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            # 1. Cambio de hora
            if _hour_changed_since_open():
                _np, _ns = _next_on_hour_change()
                await _transition(_np, _ns, f"STAKE_10 15min cambio_hora→{_np}", _cid, _pnl, _spk)
                return
            # 2. Reglas por spikes y resultado
            if _spk >= 2:
                # 2+ spikes: hora activa — win/loss ambos escalan a S20 (buen momento)
                if _pnl > 0:
                    await _transition("STAKE_20", BURST_STAKE20_AMOUNT, "STAKE_10 15min ≥2spk+win→S20",  _cid, _pnl, _spk)
                else:
                    await _transition("STAKE_20", BURST_STAKE20_AMOUNT, "STAKE_10 15min ≥2spk+loss→S20", _cid, _pnl, _spk)
            elif _spk == 1:
                # 1 spike: si ganó escala a S20; si perdió reintenta S10
                if _pnl > 0:
                    await _transition("STAKE_20", BURST_STAKE20_AMOUNT, "STAKE_10 15min 1spk+win→S20",   _cid, _pnl, _spk)
                else:
                    await _transition("STAKE_10", BURST_STAKE10_AMOUNT, "STAKE_10 15min 1spk+loss→retry", _cid, _pnl, _spk)
            else:
                # 0 spikes: escalar solo si el mercado está activo (último spike <30min)
                if _gap_gate_s20():
                    await _transition("STAKE_20", BURST_STAKE20_AMOUNT, "STAKE_10 15min 0spk→S20",           _cid, _pnl, _spk)
                else:
                    await _transition("STAKE_1",  BURST_STAKE1_AMOUNT,  "STAKE_10 15min 0spk+gap>30min→S1", _cid, _pnl, _spk)
            return

        # ── Timer STAKE_20: 15min BOOM500 / 20min CRASH500 ───────────────────
        _s20_dur = BURST_STAKE20_CRASH500_S if sym == "CRASH500" else BURST_STAKE20_DURATION_S
        if (state.burst_phase == "STAKE_20"
                and now >= state.burst_phase_started_at + _s20_dur
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            # 1. CRASH500: 0spk+WIN→S1(20m) | 1spk+WIN→S40 | 2+spk+WIN→S1(10m) | LOSE→S40
            if sym == "CRASH500":
                if _pnl > 0:
                    if _spk == 0:
                        state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                        await _transition("STAKE_1", BURST_STAKE1_AMOUNT, "CRASH500 S20 20min 0spk+win→S1(20min)", _cid, _pnl, _spk)
                    elif _spk == 1:
                        await _transition("STAKE_40", BURST_STAKE40_AMOUNT, "CRASH500 S20 20min 1spk+win→S40", _cid, _pnl, _spk)
                    else:
                        state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_10M_S
                        await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"CRASH500 S20 20min {_spk}spk+win→S1(10min)", _cid, _pnl, _spk)
                else:
                    await _transition("STAKE_40", BURST_STAKE40_AMOUNT, f"CRASH500 S20 20min {_spk}spk+loss→S40", _cid, _pnl, _spk)
                return
            # 2. Cambio de hora (BOOM500 solamente)
            if _hour_changed_since_open():
                _np, _ns = _next_on_hour_change()
                await _transition(_np, _ns, f"STAKE_20 15min cambio_hora→{_np}", _cid, _pnl, _spk)
                return
            # 3. BOOM500: contador de losses consecutivos en S20
            if _pnl > 0:
                state.s20_consec_losses = 0
            else:
                state.s20_consec_losses += 1
                if state.s20_consec_losses >= 2:
                    state.s20_consec_losses = 0
                    state.s1_drought_mode = True
                    await _transition("STAKE_1", BURST_STAKE1_AMOUNT,
                                      f"STAKE_20 15min loss×2→S1(20min)", _cid, _pnl, _spk)
                    return
            # 3. Reglas por spikes y resultado
            if _spk >= 3:
                # 3+ spikes: win → S1(20min) para recargar; loss → S1 normal
                if _pnl > 0:
                    state.s1_drought_mode = True
                    await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"STAKE_20 15min ≥3spk+win→S1(20min)", _cid, _pnl, _spk)
                else:
                    await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"STAKE_20 15min ≥3spk+loss→S1", _cid, _pnl, _spk)
            elif _spk >= 1:
                # Win: seguir el cluster — 1spk→S40, 2spk→S20(retry)
                # Loss: retry S20 si BUENO (BOOM), S1 si CRASH (invertido)
                if _pnl > 0:
                    if _spk == 1:
                        await _transition("STAKE_40", BURST_STAKE40_AMOUNT, f"STAKE_20 15min 1spk+win→S40", _cid, _pnl, _spk)
                    else:
                        await _transition("STAKE_20", BURST_STAKE20_AMOUNT, f"STAKE_20 15min {_spk}spk+win→S20(cluster)", _cid, _pnl, _spk)
                else:
                    if _s40_bueno_gate() and sym != "CRASH500":
                        await _transition("STAKE_20", BURST_STAKE20_AMOUNT, f"STAKE_20 15min {_spk}spk+loss+BUENO({_s40_gate_label()})→retry", _cid, _pnl, _spk)
                    else:
                        _tag = "CRASH_inv" if sym == "CRASH500" else f"noBUENO({_s40_gate_label()})"
                        await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"STAKE_20 15min {_spk}spk+loss+{_tag}→S1", _cid, _pnl, _spk)
            else:
                # 0 spikes: único camino a STAKE_40 — solo si hora BUENO y mercado activo
                if _s40_bueno_gate() and _gap_gate_s40():
                    await _transition("STAKE_40", BURST_STAKE40_AMOUNT, f"STAKE_20 15min 0spk+BUENO({_s40_gate_label()})→S40", _cid, _pnl, _spk)
                else:
                    _why = "noBUENO" if not _s40_bueno_gate() else f"gap>15min"
                    state.s1_drought_mode = True  # S20 sin spike → S1 extendido 20min
                    await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"STAKE_20 15min 0spk+{_why}({_s40_gate_label()})→S1(sequia)", _cid, _pnl, _spk)
            return

        # ── Timer STAKE_60 CRASH500: 20min ───────────────────────────────────
        if (sym == "CRASH500"
                and state.burst_phase == "STAKE_60"
                and now >= state.burst_phase_started_at + BURST_STAKE60_DURATION_S
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            if _pnl > 0:
                state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"CRASH500 S60 20min win→S1(20min)", _cid, _pnl, _spk)
            else:
                await _transition("STAKE_60", BURST_STAKE60_AMOUNT, f"CRASH500 S60 20min loss→S60(retry)", _cid, _pnl, _spk)
            return

        # ── Timer STAKE_40: 15min BOOM500 / 20min CRASH500 ───────────────────
        _s40_dur = BURST_STAKE40_CRASH500_S if sym == "CRASH500" else BURST_STAKE40_DURATION_S
        if (state.burst_phase == "STAKE_40"
                and now >= state.burst_phase_started_at + _s40_dur
                and state.contract_id is not None):
            _cid, _pnl, _spk = int(state.contract_id), state.current_profit, state.spikes_in_contract_500
            # 1. CRASH500: WIN→S1(7m) | LOSE+spike→S1(7m) | LOSE 0spk→S60
            if sym == "CRASH500":
                if _pnl > 0 or _spk >= 1:
                    state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_S
                    _tag = "win" if _pnl > 0 else f"{_spk}spk+loss"
                    await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"CRASH500 S40 20min {_tag}→S1(7min)", _cid, _pnl, _spk)
                else:
                    await _transition("STAKE_60", BURST_STAKE60_AMOUNT, "CRASH500 S40 20min 0spk+loss→S60", _cid, _pnl, _spk)
                return
            # 2. Cambio de hora (BOOM500)
            if _hour_changed_since_open():
                _np, _ns = _next_on_hour_change()
                await _transition(_np, _ns, f"STAKE_40 15min cambio_hora→{_np}", _cid, _pnl, _spk)
                return
            # 3. BOOM500: reglas por resultado
            if _pnl > 0:
                await _transition("STAKE_1", BURST_STAKE1_AMOUNT, "STAKE_40 15min profit+→S1", _cid, _pnl, _spk)
            else:
                if _s40_bueno_gate() and _gap_gate_s40():
                    await _transition("STAKE_40", BURST_STAKE40_AMOUNT, f"STAKE_40 15min profit-+BUENO({_s40_gate_label()})→retry", _cid, _pnl, _spk)
                else:
                    _why = "noBUENO" if not _s40_bueno_gate() else "gap>15min"
                    await _transition("STAKE_1", BURST_STAKE1_AMOUNT, f"STAKE_40 15min profit-+{_why}({_s40_gate_label()})→S1", _cid, _pnl, _spk)
            return

        # ── Cerrado externamente por broker → aplica misma regla de spikes ──────
        if state.contract_id is not None and self._query_contract(state.contract_id) is None:
            _pnl   = state.current_profit
            _phase = state.burst_phase
            _spk   = state.spikes_in_contract_500
            state.last_close_profit      = _pnl
            state.contract_id            = None
            state.current_profit         = 0.0
            state.peak_profit_500        = 0.0
            state.spikes_in_contract_500 = 0
            self._add_global_pnl(sym, _pnl, now)
            if int(state.contract_start_hour_ts_500 // 3600) == int(now // 3600):
                state.hour_pnl_500 += _pnl

            # 1. Cambio de hora: solo BOOM500 — CRASH500 ignora hora (PNRG)
            if sym != "CRASH500" and _hour_changed_since_open():
                # Si S1 cerró completo durante el cambio de hora, limpiar sequía
                if _phase == "STAKE_1":
                    _el_hc  = now - state.burst_phase_started_at if state.burst_phase_started_at > 0 else 9999.0
                    _dur_hc = BURST_STAKE1_DROUGHT_S if state.s1_drought_mode else BURST_STAKE1_DURATION_S
                    if _el_hc >= _dur_hc:
                        state.s1_drought_mode = False
                _np, _ = _next_on_hour_change()
                _LOGGER.info("[ENTRADA_DIEGO] %s broker close %s pnl=%.4f cambio_hora→%s", sym, _phase, _pnl, _np)
                state.burst_phase            = _np
                state.burst_phase_started_at = 0.0
                return

            # 2. Reglas por fase
            if _phase == "STAKE_1":
                _elapsed   = now - state.burst_phase_started_at if state.burst_phase_started_at > 0 else 9999.0
                _s1_dur_bc = (state.crash500_s1_timer_s if sym == "CRASH500"
                              else (BURST_STAKE1_DROUGHT_S if state.s1_drought_mode else BURST_STAKE1_DURATION_S))
                if _elapsed < _s1_dur_bc:
                    # DPM cerró antes del timer → reabre STAKE_1, preserva el timer original
                    _LOGGER.info("[ENTRADA_DIEGO] %s broker close STAKE_1 prematuro %.0fs/<%.0fs → reabre sin resetear timer",
                                 sym, _elapsed, _s1_dur_bc)
                    state.burst_phase = "STAKE_1"
                    return  # burst_phase_started_at NO se toca → el timer sigue desde el inicio original
                # CRASH500: lógica adaptativa (mismo comportamiento que timer block)
                if sym == "CRASH500":
                    _cur_t  = state.crash500_s1_timer_s
                    _at_max = (_cur_t >= BURST_STAKE1_CRASH500_17M_S)
                    if (_spk == 0 and not _at_max) or (_spk <= 1 and _at_max):
                        state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_S
                        next_phase = "STAKE_20"
                    elif _spk == 1:
                        next_phase = "STAKE_1"  # 1spk timer<17min → mismo timer
                    elif _spk == 2:
                        state.crash500_s1_timer_s = max(_cur_t, BURST_STAKE1_CRASH500_12M_S)
                        next_phase = "STAKE_1"
                    elif _spk == 3:
                        state.crash500_s1_timer_s = max(_cur_t, BURST_STAKE1_CRASH500_17M_S)
                        next_phase = "STAKE_1"
                    else:
                        state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                        next_phase = "STAKE_1"
                else:
                    state.s1_drought_mode = False  # cierre completo BOOM500 → limpia modo sequia
                    if _spk >= 2:
                        next_phase = "STAKE_1"
                    else:
                        next_phase, _ = _drought_gate_s1()
                        if next_phase == "STAKE_20" and not _discharge_s20_gate():
                            next_phase = "STAKE_1"
            elif _phase == "STAKE_5":
                # CRASH500 S5: premature → reabre; completo → S20 siempre
                _elapsed = now - state.burst_phase_started_at if state.burst_phase_started_at > 0 else 9999.0
                if _elapsed < BURST_STAKE5_DURATION_S:
                    _LOGGER.info("[ENTRADA_DIEGO] %s broker close STAKE_5 prematuro %.0fs/<%.0fs → reabre sin resetear timer",
                                 sym, _elapsed, BURST_STAKE5_DURATION_S)
                    state.burst_phase = "STAKE_5"
                    return
                next_phase = "STAKE_20"
            elif _phase == "STAKE_10":
                _elapsed = now - state.burst_phase_started_at if state.burst_phase_started_at > 0 else 9999.0
                if _elapsed < BURST_STAKE10_DURATION_S:
                    # DPM cerró antes de los 15min → reabre STAKE_10, preserva timer
                    _LOGGER.info("[ENTRADA_DIEGO] %s broker close STAKE_10 prematuro %.0fs/<%.0fs → reabre sin resetear timer",
                                 sym, _elapsed, BURST_STAKE10_DURATION_S)
                    state.burst_phase = "STAKE_10"
                    return
                if _spk >= 2:
                    next_phase = "STAKE_20"  # win o loss: buen momento → escalar
                elif _spk == 1:
                    next_phase = "STAKE_20" if _pnl > 0 else "STAKE_10"
                else:
                    next_phase = "STAKE_20" if _gap_gate_s20() else "STAKE_1"
            elif _phase == "STAKE_20":
                if sym == "CRASH500":
                    # 0spk+WIN→S1(20m) | 1spk+WIN→S40 | 2+spk+WIN→S1(10m) | LOSE→S40
                    if _pnl > 0:
                        if _spk == 0:
                            state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                            next_phase = "STAKE_1"
                        elif _spk == 1:
                            next_phase = "STAKE_40"
                        else:
                            state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_10M_S
                            next_phase = "STAKE_1"
                    else:
                        next_phase = "STAKE_40"
                else:
                    # BOOM500: contador de losses consecutivos en S20
                    if _pnl > 0:
                        state.s20_consec_losses = 0
                    else:
                        state.s20_consec_losses += 1
                    # 2 losses seguidos → drought S1 20min (override todo)
                    if _pnl <= 0 and state.s20_consec_losses >= 2:
                        state.s20_consec_losses = 0
                        state.s1_drought_mode = True
                        next_phase = "STAKE_1"
                    elif _spk >= 3:
                        if _pnl > 0:
                            state.s1_drought_mode = True  # 3+spk win → S1(20min) para recargar
                        next_phase = "STAKE_1"
                    elif _spk >= 1:
                        if _pnl > 0:
                            next_phase = "STAKE_40" if _spk == 1 else "STAKE_20"
                        else:
                            next_phase = "STAKE_20" if _s40_bueno_gate() else "STAKE_1"
                    else:  # 0 spikes: único camino a S40 — solo si hora BUENO y mercado activo
                        if _s40_bueno_gate() and _gap_gate_s40():
                            next_phase = "STAKE_40"
                        else:
                            next_phase = "STAKE_1"
                            state.s1_drought_mode = True  # broker S20 sin spike → S1 extendido 20min
            elif _phase == "STAKE_40":
                if sym == "CRASH500":
                    # WIN→S1(7m) | LOSE+spike→S1(7m) | LOSE 0spk→S60
                    if _pnl > 0 or _spk >= 1:
                        state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_S
                        next_phase = "STAKE_1"
                    else:
                        next_phase = "STAKE_60"
                elif _pnl > 0:
                    next_phase = "STAKE_1"
                else:
                    next_phase = "STAKE_40" if (_s40_bueno_gate() and _gap_gate_s40()) else "STAKE_1"
            elif _phase == "STAKE_60":
                # CRASH500: WIN→S1(20m) | LOSE→retry S60
                if _pnl > 0:
                    state.crash500_s1_timer_s = BURST_STAKE1_CRASH500_20M_S
                    next_phase = "STAKE_1"
                else:
                    next_phase = "STAKE_60"
            else:
                next_phase = "STAKE_1"
            _LOGGER.info("[ENTRADA_DIEGO] %s broker close fase=%s pnl=%.4f spk=%d → %s",
                         sym, _phase, _pnl, _spk, next_phase)
            state.burst_phase            = next_phase
            state.burst_phase_started_at = 0.0
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

    async def _500_watchdog_loop(self, sym: str) -> None:
        """Dispara _process_500_simple cada 15s aunque el WS esté caído."""
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog 500 iniciado (intervalo 15s)", sym)
        while self._enabled and sym not in SYMBOLS_ED_DISABLED:
            await asyncio.sleep(15)
            if not self._enabled or sym in SYMBOLS_ED_DISABLED:
                break
            try:
                async with self._locks[sym]:
                    await self._process_500_simple(sym)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s watchdog error: %s", sym, exc)
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog 500 terminado", sym)

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

    @staticmethod
    def _load_slope_tail(sym: str, logs_dir: Path) -> list[tuple[float, float]]:
        """Lee los últimos ~1.5 MB de slope_history.jsonl para el símbolo dado.
        Con 4 símbolos × ~236B/línea ≈ 6350 líneas ≈ ~16h de historia por símbolo.
        """
        path = logs_dir / "slope_history.jsonl"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(size, 1_500_000)
                f.seek(max(0, size - chunk))
                raw = f.read()
        except Exception:
            return []
        result: list[tuple[float, float]] = []
        for line in raw.decode("utf-8", errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("symbol") == sym:
                    result.append((float(d["ts"]), float(d["price"])))
            except Exception:
                pass
        result.sort()
        return result

    def _get_displacement_stake(self, sym: str, now: float) -> float | None:
        """
        Devuelve 1.0 si el desplazamiento en DISP_WINDOW_H horas supera el umbral.
        BOOM500: precio subió > +DISP_THRESH_PCT% → spikes agotados → stake $1.
        CRASH500: precio bajó > DISP_THRESH_PCT% (desplazamiento negativo) → stake $1.
        Devuelve None si condiciones normales (usar stake por defecto).
        """
        if sym not in SYMBOLS_500:
            return None
        if now - self._slope_cache_ts > _SLOPE_CACHE_TTL:
            self._slope_cache_ts = now
            for s in ("BOOM500", "CRASH500"):
                loaded = self._load_slope_tail(s, self._logs_dir)
                if loaded:
                    self._slope_cache[s] = loaded
        entries = self._slope_cache.get(sym, [])
        if len(entries) < 2:
            return None
        p_now = entries[-1][1]
        target_ts = now - DISP_WINDOW_H * 3600
        tss = [e[0] for e in entries]
        idx = bisect.bisect_left(tss, target_ts)
        if idx == 0:
            return None
        idx = min(idx, len(entries) - 1)
        if abs(tss[idx] - target_ts) > abs(tss[idx - 1] - target_ts):
            idx -= 1
        p_past = entries[idx][1]
        if p_past == 0:
            return None
        disp_pct = (p_now - p_past) / p_past * 100
        # Para BOOM500 el peligro es que el precio suba (spikes hacia arriba se agotan)
        # Para CRASH500 el peligro es que el precio baje (spikes hacia abajo se agotan)
        in_danger = disp_pct > DISP_THRESH_PCT if sym == "BOOM500" else disp_pct < -DISP_THRESH_PCT
        if in_danger:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s DISP_GATE disp=%.2f%% (ventana=%.0fH umbral=±%.1f%%) → stake=$1",
                sym, disp_pct, DISP_WINDOW_H, DISP_THRESH_PCT,
            )
            return 1.0
        return None

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
        # ── HOUR_PROFIT_PROTECT (500s): ≥$4 ganado esta hora → no abrir ─────────
        if sym in SYMBOLS_500:
            _cur_hour = int(now // 3600) * 3600.0
            if _cur_hour != state.hour_start_ts_500:
                state.prev_hour_pnl_500 = state.hour_pnl_500
                state.hour_pnl_500 = 0.0  # nueva hora → reset
            _prev_was_win = state.prev_hour_pnl_500 >= 0.0
            if state.hour_pnl_500 >= HOUR_PROFIT_PROTECT_USD and sym != "CRASH500" and _prev_was_win:
                _wait_s = int((_cur_hour + 3600.0) - now)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s HOUR_PROFIT_PROTECT +$%.2f≥$%.0f (prev=+$%.2f) → no abre, espera %ds",
                    sym, state.hour_pnl_500, HOUR_PROFIT_PROTECT_USD, state.prev_hour_pnl_500, _wait_s,
                )
                state.burst_phase            = "STAKE_1"
                state.burst_phase_started_at = 0.0
                return

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

    def _query_profit(self, contract_id: int) -> Optional[float]:
        """Retorna el P&L flotante, o None si el contrato ya no existe (cerrado por broker)."""
        try:
            oc = self._query_contract(contract_id)
            if oc:
                return float(oc.get("floating_pnl") or 0.0)
            return None  # Contrato no encontrado → broker ya lo cerró
        except Exception:
            pass
        return None

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
                            # burst machine — críticos para evitar race condition en restart
                            st.burst_phase            = s.get("burst_phase", "STAKE_1")
                            st.burst_phase_started_at = float(s.get("burst_phase_started_at", 0.0))
                            st.spikes_in_contract_500 = int(s.get("spikes_in_contract", 0))
                            st.last_spike_ts_500      = float(s.get("last_spike_ts_500", 0.0))
                            st.hour_spike_count_500        = int(s.get("hour_spike_count_500", 0))
                            st.hour_start_ts_500           = float(s.get("hour_start_ts_500", 0.0))
                            st.contract_start_hour_ts_500  = float(s.get("contract_start_hour_ts_500", 0.0))
                            st.s20_consec_losses           = int(s.get("s20_consec_losses", 0))
                            st.s20_crash500_wins           = int(s.get("s20_crash500_wins", 0))
                            st.hour_pnl_500                = float(s.get("hour_pnl_500", 0.0))
                            st.prev_hour_pnl_500           = float(s.get("prev_hour_pnl_500", 0.0))
                            st.crash500_s1_timer_s         = float(s.get("crash500_s1_timer_s", BURST_STAKE1_CRASH500_S))
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
