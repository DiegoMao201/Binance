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

SYMBOLS_300  = set()   # ACCU 300N deshabilitado — WR 72.3% < BE 75.8% en 3676 trades
SYMBOLS_500  = {"CRASH500",  "BOOM500"}
SYMBOLS_600  = {"CRASH600",  "BOOM600"}
SYMBOLS_1000 = {"CRASH1000", "BOOM1000"}
SYMBOLS_900  = {"CRASH900",  "BOOM900"}
SYMBOLS_R    = set()   # R_75 y JD75 suspendidos — no estudiados aún
SYMBOLS_LADDER = SYMBOLS_300 | SYMBOLS_500 | SYMBOLS_600   # todos usan lógica LADDER
SYMBOLS_K1000  = SYMBOLS_1000 | SYMBOLS_900  # todos usan ciclo K1000
# Multipliers para recolección de datos: escalera $1→$2→$4→$8→$16, 5min/fase
# Nombres reales Deriv API: 50=sin-N, 150/300=con-N (verificado empíricamente)
SYMBOLS_MULTI_NEW  = {"BOOM50", "CRASH50", "BOOM150N", "CRASH150N", "BOOM300N", "CRASH300N"}
MULTI_MULTIPLIERS  = {"BOOM50": 1000, "CRASH50": 1000, "BOOM150N": 500, "CRASH150N": 500, "BOOM300N": 40, "CRASH300N": 40}
MULTI_STAKES       = [1.0, 2.0, 4.0, 8.0, 16.0]
MULTI_PHASE_SECS   = 300.0
SYMBOLS_ED   = SYMBOLS_LADDER | SYMBOLS_K1000 | SYMBOLS_R | SYMBOLS_MULTI_NEW

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

# 1000s: ventana p25-p75 — entrar a los 15min del spike, salir a los 30min si no llegó spike
K1000_ENTRY_DELAY_S = int(os.getenv("K1000_ENTRY_DELAY_S",  "480"))   # 8min espera entre contratos (1000s)
K1000_ENTRY_DELAY_900_S = int(os.getenv("K1000_ENTRY_DELAY_900_S", "480"))  # 8min espera entre contratos (900s)
K1000_OPEN_WINDOW_S = int(os.getenv("K1000_OPEN_WINDOW_S",  "900"))   # (legacy alias)
K1000_STAKE_START   = float(os.getenv("K1000_STAKE_START",  "3.0"))   # stake inicial
K1000_STAKE_MID     = float(os.getenv("K1000_STAKE_MID",    "6.0"))   # (legacy alias)
K1000_STAKE_MAX     = float(os.getenv("K1000_STAKE_MAX",    "30.0"))  # (legacy alias)
K1000_CONTRACT_S       = int(os.getenv("K1000_CONTRACT_S",     "1200"))  # 20 min duración contrato (timer-triggered)
K1000_SPIKE_CONTRACT_S = 240                                            # 4 min duración contrato (spike-triggered)
K1000_SPIKE_HOLD_S     = int(os.getenv("K1000_SPIKE_HOLD_S",  "240")) # 4 min espera post-spike antes de cerrar
K1000_STAKES_1000   = [10.0, 20.0]    # escalera: $10→$20; pérdida en $20 → reset a $10 (sin $40/$80)
# Aliases para compatibilidad con to_dict y referencias externas
K1000_SCOUT_STAKE = K1000_STAKE_START
K1000_S20_STAKE   = K1000_STAKE_MID
K1000_S40_STAKE   = K1000_STAKE_MAX
K1000_S80_STAKE   = K1000_STAKE_MAX
K1000_S200_STAKE  = K1000_STAKE_MAX
K1000_SCOUT_S     = K1000_ENTRY_DELAY_S
K1000_HOLD_S      = K1000_OPEN_WINDOW_S

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
#   S1 adaptativo: 0spk<17m→S20 | ≤1spk≥17m→S20 | 1spk@10-12m→S20 | 1spk@7m→S1 | 2spk→12m | 3spk→17m | 4+spk→20m
#   S20 20m: 0spk+WIN→S1(20m) | 1spk+WIN→S40 | 2+spk+WIN→S1(10m) | LOSE(any)→S40
#   S40 20m: WIN→S1(7m) | LOSE+spike→S1(7m) | LOSE 0spk→S60
#   S60 20m: WIN→S1(20m) | LOSE→retry S60 (infinito hasta ganar)
#   SL duro: 90% del stake activo → S1 (reset timer a 7min)
BURST_STAKE1_AMOUNT          = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE1",              "20.0"))
BURST_STAKE1_DURATION_S      = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_S",              "360"))   # 6min BOOM500
BURST_STAKE1_DROUGHT_S       = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_DROUGHT_S",      "1200"))  # 20min sequia BOOM500
BURST_STAKE1_CRASH500_S      = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_S",     "420"))   # 7min CRASH500 S1 base
BURST_STAKE1_CRASH500_10M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_10M_S", "600"))   # 10min: S20 2+spk+win
BURST_STAKE1_CRASH500_12M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_12M_S", "720"))   # 12min: S1 captura 2 spikes
BURST_STAKE1_CRASH500_17M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_17M_S", "1020"))  # 17min: S1 captura 3 spikes
BURST_STAKE1_CRASH500_20M_S  = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE1_CRASH500_20M_S", "1200"))  # 20min: max / S20 WIN 0spk / S60 WIN
BURST_STAKE5_AMOUNT          = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE5",              "5.0"))
BURST_STAKE5_DURATION_S      = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE5_S",              "900"))   # legacy CRASH500 S5 (ya sin ruta activa)
BURST_STAKE10_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE10",             "1.0"))
BURST_STAKE10_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE10_S",             "480"))   # 8min BOOM500 S10
BURST_STAKE10_CRASH500_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE10_CRASH500_S",    "240"))   # 4min CRASH500 S10
WAIT_GATE_TIMER_CRASH500_S   = int(os.getenv("ENTRADA_DIEGO_WAIT_GATE_CRASH500_S",        "420"))   # 7min espera tras n_spikes≥2
WAIT_GATE_TIMER_BOOM500_S    = int(os.getenv("ENTRADA_DIEGO_WAIT_GATE_BOOM500_S",         "360"))   # 6min espera tras n_spikes≥3+POWER
BURST_STAKE20_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE20",             "1.0"))
BURST_STAKE20_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE20_S",             "480"))   # 8min S20 (ambos símbolos)
BURST_STAKE20_CRASH500_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE20_CRASH500_S",    "480"))   # 8min CRASH500 S20
BURST_STAKE40_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE40",             "40.0"))
BURST_STAKE40_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE40_S",             "1200"))  # 20min BOOM500 S40
BURST_STAKE40_CRASH500_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE40_CRASH500_S",    "1200"))  # 20min CRASH500 S40
BURST_STAKE40_REAL_SPIKE_RATIO = float(os.getenv("ENTRADA_DIEGO_S40_REAL_SPIKE_RATIO",   "50.0"))  # ratio mínimo para contar spike como "real" en S40 → S60
BURST_STAKE60_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE60",             "60.0"))
BURST_STAKE60_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE60_S",             "1200"))  # 20min CRASH500 S60
BURST_STAKE80_AMOUNT         = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE80",             "80.0"))
BURST_STAKE80_DURATION_S     = int(os.getenv("ENTRADA_DIEGO_BURST_STAKE80_S",             "1200"))  # 20min recovery S80
BURST_STAKE40_MAX_HOUR_SPIKES = int(os.getenv("ENTRADA_DIEGO_BURST_S40_MAX_HOUR_SPKS", "5"))  # gate: máx spikes en hora UTC para abrir $40
HOUR_PROFIT_PROTECT_USD      = float(os.getenv("ENTRADA_DIEGO_HOUR_PROFIT_PROTECT", "4.0")) # protección: ≥$4 ganado en la hora → parar hasta siguiente hora

# PnL global acumulado: cuando alcanza GLOBAL_PNL_TARGET → pausa GLOBAL_PAUSE_HOURS horas
# PnL suma todos los cierres de BOOM500+CRASH500 (positivos y negativos)
GLOBAL_PNL_TARGET  = float(os.getenv("ENTRADA_DIEGO_GLOBAL_PNL_TARGET",  "60.0"))
GLOBAL_PAUSE_HOURS = float(os.getenv("ENTRADA_DIEGO_GLOBAL_PAUSE_HOURS", "8.0"))

# Gate PnL por símbolo: cada $30 ganados (desde referencia) → pausa 6h
#                       cada $20 perdidos (desde referencia) → pausa 2h
# La referencia se actualiza al disparar cada gate → "se va de 30 en 30"
SYM_PNL_WIN_GATE_USD   = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_WIN_GATE",    "30.0"))
SYM_PNL_LOSS_GATE_USD  = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_LOSS_GATE",   "20.0"))
SYM_PNL_WIN_PAUSE_H    = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_WIN_PAUSE_H",  "6.0"))
SYM_PNL_LOSS_PAUSE_H   = float(os.getenv("ENTRADA_DIEGO_SYM_PNL_LOSS_PAUSE_H", "2.0"))

# CRASH500 POWER gate: Σ(abs_jump) de spikes en últimos 30min ∈ [MIN, MAX] → S20
# Métrica abs(jump) en vez de ratio — ATR-independiente (no infla post-deploy).
# Thresholds [3, 8]: único bucket con PnL positivo en 4792 trades históricos.
# PJ=[3,7): WR=45.4% PnL=+$27 vs PJ=[0,3)=-$541 y PJ=[7+)=-$782 combinado.
CRASH500_POWER_MIN         = float(os.getenv("ENTRADA_DIEGO_CRASH500_POWER_MIN",     "3.0"))
CRASH500_POWER_MAX         = float(os.getenv("ENTRADA_DIEGO_CRASH500_POWER_MAX",     "8.0"))
# CRASH500 n_spikes gate: [2,4) — análisis 1187 trades: 4-6spk WR=42-45% PnL=-$2.3/-$2.7 por trade
CRASH500_NSPIKES_MIN       = int(os.getenv("ENTRADA_DIEGO_CRASH500_NSPIKES_MIN",     "2"))
CRASH500_NSPIKES_MAX       = int(os.getenv("ENTRADA_DIEGO_CRASH500_NSPIKES_MAX",     "4"))   # exclusive
CRASH500_SYM_PNL_WIN_PAUSE_H = float(os.getenv("ENTRADA_DIEGO_CRASH500_WIN_PAUSE_H", "4.0"))
# BOOM500 DOUBLE gate: POWER [15,30) AND n_spikes [3,8) → S20
# Análisis 1131 trades S20+ Jul-2026: gate doble n=150, WR=57.3%, PnL=+$187 (gate simple POWER: -$35)
BOOM500_POWER_MIN          = float(os.getenv("ENTRADA_DIEGO_BOOM500_POWER_MIN",    "14.5"))  # 14.5 en vez de 15.0 — margen float
BOOM500_POWER_MAX          = float(os.getenv("ENTRADA_DIEGO_BOOM500_POWER_MAX",    "30.0"))
BOOM500_NSPIKES_MIN        = int(os.getenv("ENTRADA_DIEGO_BOOM500_NSPIKES_MIN",    "3"))
BOOM500_NSPIKES_MAX        = int(os.getenv("ENTRADA_DIEGO_BOOM500_NSPIKES_MAX",    "8"))    # exclusive
# Gate POWER mínimo para abrir en 500s: bloquear mercado muerto (análisis 11k trades: P<10 = WR<38%)
POWER_GATE_500_MIN         = float(os.getenv("ENTRADA_DIEGO_POWER_GATE_500_MIN",   "10.0"))  # Σabs(jump) 30min

# Escalera progresiva 500s: $1→$3→$9→$20 (retries hasta win en S20)
BURST_STAKE3_AMOUNT           = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE3",         "1.0"))
BURST_STAKE9_AMOUNT           = float(os.getenv("ENTRADA_DIEGO_BURST_STAKE9",         "40.0"))
BURST_STAKE_LADDER_DURATION_S = int(os.getenv("ENTRADA_DIEGO_BURST_LADDER_S",        "720"))   # 12min todos los stakes
BURST_PROFIT_POSITIVE_CLOSE_S = int(os.getenv("ENTRADA_DIEGO_BURST_PROFIT_POS_S",    "90"))    # 1.5min profit+ → cerrar
WIN_PROBE_DURATION_S          = int(os.getenv("ENTRADA_DIEGO_WIN_PROBE_S",            "300"))   # 5min probe $1 post-win

# Horas UTC bloqueadas por análisis histórico (8,816 contratos, Mayo–Jul 2026)
# Las 5 peores horas por símbolo: pierden consistentemente hora tras hora
# BOOM500: 16h(WR=34%,avg=-$1.21) 13h(-$0.89) 3h(-$0.76) 10h(-$0.68) 4h(-$0.42)
# CRASH500: 22h(avg=-$1.05) 13h(-$0.94) 16h(-$0.77) 3h(-$0.71) 1h(-$0.64)
BOOM500_BLOCKED_UTC_HOURS:  frozenset = frozenset({3, 4, 10, 13, 16})
CRASH500_BLOCKED_UTC_HOURS: frozenset = frozenset({1, 3, 13, 16, 22})

_ED_DISABLED_RAW    = os.getenv("ENTRADA_DIEGO_DISABLED_SYMBOLS", "")
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
STAKE_500_FIXED        = float(os.getenv("ENTRADA_DIEGO_500_STAKE",   "3.0"))
SL_USD_500_SIMPLE      = float(os.getenv("ENTRADA_DIEGO_500_SL_USD",  "14.0"))
HOLD_TIME_S_500_SIMPLE = int(os.getenv("ENTRADA_DIEGO_500_HOLD_S",   "1800"))   # 30 min
# ── 500s Ladder: 2 tiers con gate POWER≥10 — $8 (0-6min) → $32 (6-12min) ──
# Mismo para BOOM500 y CRASH500. Gate POWER_GATE_500_MIN bloquea mercado muerto.
LADDER_500_TIERS: list[tuple[float, float]] = [
    (240, 32.0),    # BOOM500: 0-4min desde spike → $32 (contrato 4min), no hay más tiers
]
LADDER_500_TIERS_CRASH: list[tuple[float, float]] = [
    (120,  0.0),    # CRASH500 tier 0: 0-2min → zona muerta (espera)
    (480, 32.0),    # CRASH500 tier 1: 2-8min → $32 (contrato 6min)
]
LADDER_500_CYCLE_S       = 240.0   # BOOM500: 4min ventana
LADDER_500_CYCLE_S_CRASH = 480.0   # CRASH500: 8min ciclo total desde spike
# ── 600s Ladder: zona muerta 0-2min → operan minuto 2 a 9 ($32, 7min contrato) ──
LADDER_600_TIERS: list[tuple[float, float]] = [
    (120, 0.0),    # BOOM600/CRASH600 tier 0: 0-2min → zona muerta
    (540, 10.0),   # BOOM600/CRASH600 tier 1: 2-9min → $10 (fallback; p50 dinámico usado en _ladder_tier_500)
]
LADDER_600_CYCLE_S = 540.0
LADDER_600_CONTRACT_S_32 = 420.0  # 7 min — contrato 600s (minuto 2 al 9)
# Ventana burst: si hay ≥2 spikes en los últimos BURST_WINDOW_S → multiplicar stake ×2
LADDER_BURST_WINDOW_S   = 300.0   # 5 min
LADDER_BURST_MIN_SPIKES = 2       # spikes confirmados para burst
LADDER_BURST_MAX_STAKE  = 64.0    # cap burst
LADDER_500_CONTRACT_S       = 240.0   # 4 min — BOOM500 (minuto 0 al 4)
LADDER_500_CONTRACT_S_CRASH = 360.0   # 6 min — CRASH500 (minuto 2 al 8)
LADDER_DAILY_PNL_GATE       = 20.0    # $20 ganado desde el reset → pausa hasta siguiente reset
LADDER_500_REST_S           = 900.0   # 15 min descanso (500s y 600s)
LADDER_500_REST_S_BOOM      = 900.0   # 15 min descanso BOOM
S500_CONSEC_WINS_REST    = 1     # wins → REST (1 win > $1 → pausa 20min)
REST_MIN_STAKE_500      = 5.0   # stake mínimo para contar win hacia REST
REST_MIN_HOUR_PNL_500   = 4.0   # hora UTC debe tener >$4 ganados para activar REST
TP_RATCHET_MIN_PNL   = 15.0     # pnl mínimo en BROKER_CLOSE (no-FLOOR) para detectar tp_or_ratchet
TP_RATCHET_PAUSE_S   = 1800.0   # 30 min pausa global tras tp_or_ratchet (el mercado se revierte)
# Si ≥N spikes llegan DURANTE el REST → mercado quemado → extender REST 20min más (se reinicia el contador)
LADDER_REST_SPIKE_EXTEND = 4   # ≥4 spikes durante REST → +20min (aplica a 500s y 600s, BOOM y CRASH)
# Cycle budget gate: no abrir cuando ciclo activo agotado (80%+ prob de secarse)
_CYCLE_MEAN_RATIO: dict[str, float] = {
    "BOOM300N": 200.0, "CRASH300N": 200.0,  # placeholder — calibrar con datos reales
    "BOOM500": 195.0, "CRASH500": 199.0, "BOOM600": 236.0, "CRASH600": 229.0,
}
_CYCLE_BUDGET_MAX = 10.0   # ≥80% ciclos activos terminados en este umbral (n=600+ ciclos)
# n30 mínimo para disparar Q→A (reset budget tras sequía).
# Dato: BOOM500 n30=3 WR=62% vs n30=4 WR=30%; CRASH600 n30=3 WR=65% vs n30=4 WR=23%.
# CRASH500/BOOM600 usan n30=1 (reset inmediato al primer spike post-sequía).
_CYCLE_QA_MIN: dict[str, int] = {
    "CRASH300N": 2, "BOOM300N": 2,  # placeholder conservador — ajustar con datos
    "CRASH500": 1, "BOOM500": 3, "CRASH600": 3, "BOOM600": 1,
}
# Stake mínimo 500s (usado en _open() para LimitOrderAmountTooHigh guard)
S500_STAKE_LOW        = 4.0      # tier2 = $4 (mínimo)
# Target hora K1000 (reutilizado también en lógica K1000)
S500_HOUR_TARGET_USD  = float(os.getenv("S500_HOUR_TARGET_USD", "1.0"))

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
    consec_wins_500:        int   = 0    # wins consecutivos ladder 500 (2 + hour_pnl>0 → REST)
    ladder_rest_until_500:  float = 0.0  # epoch hasta cuando en REST (0 = activo)
    rest_spikes_500:        int   = 0    # spikes contados durante el REST actual
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
    crash500_s1_timer_s:       float = 420.0  # timer adaptativo S1 CRASH500 (7/10/12/17/20min)
    crash500_real_spikes_500:  int   = 0      # spikes con ratio ≥ 50x en contrato actual (S40→S60 gate)
    crash500_next_open_ts:     float = 0.0    # CRASH500: epoch cuando puede abrir el próximo contrato (timer 2.5m)
    s80_pending:               bool  = False  # legacy — ya no se usa, compatibilidad estado guardado
    profit_first_positive_ts:  float = 0.0   # epoch 1er tick profit>0 en contrato actual (cierre 1.5min)
    sym_pnl_since_reset:  float = 0.0   # PnL acumulado desde reset (gate por símbolo)
    sym_pnl_reference:    float = 0.0   # referencia flotante: gate +$30/-$20 se mide desde aquí
    sym_pnl_pause_until:  float = 0.0   # epoch hasta cuando pausado por gate PnL (0 = activo)
    rest_mode: bool = False         # True = abriendo a $2 post-profit/deep-pause — solo 1000s
    is_readjusted: bool = False     # True cuando se re-adjuntó a contrato viejo (no abrir PROFIT_TIMER en ACTIVE)
    pnl_accounted_by_floor: bool = False  # True cuando FLOOR ya contabilizó el PnL — evita doble conteo en BROKER_CLOSE
    current_stake_500: float = 0.0       # stake del contrato activo — para filtrar REST en stakes bajos
    power_window: list = field(default_factory=list)  # [(ts, abs_jump), ...] rolling 30min — POWER gate (ATR-independiente)
    spike_first_ts_in_contract: float = 0.0  # epoch del primer spike en contrato S40 actual (gate "spike temprano <5min")
    r75_direction: str = "MULTUP"  # R_75: MULTUP o MULTDOWN; flip si pierde, mantiene si gana
    # 1000s scout ladder
    k1000_phase:          str   = "WAIT"   # WAIT | IN_CONTRACT | SPIKE_HOLD
    k1000_phase_ts:       float = 0.0       # epoch inicio de la fase actual
    k1000_peak:           float = 0.0       # máximo profit visto en STAKE_200 (floor ratchet)
    k1000_profit_ts:      float = 0.0       # epoch cuando profit fue > 0 por primera vez (90s timer)
    k1000_spike_hold_until: float = 0.0     # (legacy, no usado)
    k1000_cycle_stake:      float = 0.0     # stake vigente para el ciclo actual (0=usar K1000_STAKE_START)
    hour_pnl_1000:      float = 0.0         # PnL acumulado en hora UTC actual — 1000s
    hour_start_ts_1000: float = 0.0         # inicio hora UTC vigente — 1000s
    k1000_blocked_until: float = 0.0        # (legacy — no usado en nueva lógica simple)
    k1000_stake_idx:    int   = 0           # posición en K1000_STAKES_1000 (0=$3, 1=$6, 2=$15, 3=$30)
    k1000_spike_triggered: bool  = False   # True = contrato abierto por spike (4min), False = timer (20min)
    ladder_active_contract_s: float = 0.0  # duración contrato activo 500s/600s por símbolo
    day_pnl_500:      float = 0.0          # PnL del día UTC (500s/600s) — gate $20 diario
    day_start_ts_500: float = 0.0          # epoch inicio día UTC vigente (500s/600s)
    day_pnl_1000:     float = 0.0          # PnL del día UTC (900s/1000s) — gate $20 diario
    day_start_ts_1000: float = 0.0         # epoch inicio día UTC vigente (900s/1000s)
    s500_drought_s1_ts:      float = 0.0   # epoch S1 post-sequía; 0 = sin drought recovery
    s500_cur_stake:          float = 0.0   # stake actual 500s; 0 = usar STAKE_500_FIXED
    cycle_budget_norm: float = 0.0   # suma ratio-norm ciclo activo (gate agotamiento)
    cycle_prev_quiet:  bool  = True  # último n30 fue quieto → Q→A resetea budget
    s500_pnl_counted_cid:    int   = 0     # contrato cuyo pnl ya fue sumado en PROFIT_CLOSE (evitar double-count en BROKER_CLOSE)
    spike_ts_buffer_500:      list  = field(default_factory=list)  # últimas 100 ts de spikes BOOM500 para p25/p50 dinámico
    boom500_p25:              float = 126.0  # p25 intervalo entre spikes (segundos) — fallback histórico 393 muestras
    boom500_p50:              float = 363.0  # p50 intervalo entre spikes (segundos) — fallback histórico 393 muestras
    spike_ts_buffer_crash500: list  = field(default_factory=list)  # últimas 100 ts de spikes CRASH500 para p50 dinámico
    crash500_p50:             float = 363.0  # p50 intervalo entre spikes CRASH500 (segundos) — fallback histórico
    spike_ts_buffer_boom600:  list  = field(default_factory=list)  # últimas 100 ts de spikes BOOM600 para p50 dinámico
    boom600_p50:              float = 420.0  # p50 intervalo entre spikes BOOM600 (segundos) — fallback 7min
    spike_ts_buffer_crash600: list  = field(default_factory=list)  # últimas 100 ts de spikes CRASH600 para p50 dinámico
    crash600_p50:             float = 420.0  # p50 intervalo entre spikes CRASH600 (segundos) — fallback 7min
    dead_zone_spikes_500:     int   = 0      # spikes llegados mientras sin contrato (dead zone) → ≥2 → REST 15min
    ladder_recovery_level_500: int  = 0      # 0=normal $4, 1=recovery $20/20m, 2=recovery $60/30m
    ladder_last_closed_cid:   int   = 0      # CID del último contrato cerrado por timer — ignora broker-close tardío
    multi_phase_idx:          int   = 0      # índice actual en MULTI_STAKES (0→$1 … 4→$16); avanza en loss

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
            "crash500_real_spikes_500":     self.crash500_real_spikes_500,
            "s40_real_spike_ratio":         BURST_STAKE40_REAL_SPIKE_RATIO,
            "sym_pnl_since_reset":          round(self.sym_pnl_since_reset, 4),
            "sym_pnl_reference":            round(self.sym_pnl_reference, 4),
            "sym_pnl_pause_until":          round(self.sym_pnl_pause_until, 3),
            "sym_pnl_win_gate":             SYM_PNL_WIN_GATE_USD,
            "sym_pnl_loss_gate":            SYM_PNL_LOSS_GATE_USD,
            "sym_pnl_win_pause_h":          SYM_PNL_WIN_PAUSE_H,
            "sym_pnl_loss_pause_h":         SYM_PNL_LOSS_PAUSE_H,
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
            "burst_stake80_s":              BURST_STAKE80_DURATION_S,
            "burst_stake1_amount":          BURST_STAKE1_AMOUNT,
            "burst_stake5_amount":          BURST_STAKE5_AMOUNT,
            "burst_stake20_amount":         BURST_STAKE20_AMOUNT,
            "burst_stake40_amount":         BURST_STAKE40_AMOUNT,
            "burst_stake60_amount":         BURST_STAKE60_AMOUNT,
            "burst_stake80_amount":         BURST_STAKE80_AMOUNT,
            "power_30min_jump": round(sum(r for t, r in self.power_window if t > now - 1800.0), 2),
            "n_spikes_30min": sum(1 for t, _ in self.power_window if t > now - 1800.0),
            "power_window": [[t, r] for t, r in self.power_window if t > now - 1800.0],
            "spike_first_ts_in_contract": round(self.spike_first_ts_in_contract, 3),
            "s80_pending": self.s80_pending,
            "profit_first_positive_ts": round(self.profit_first_positive_ts, 3),
            "burst_stake3_amount":      BURST_STAKE3_AMOUNT,
            "burst_stake9_amount":      BURST_STAKE9_AMOUNT,
            "burst_ladder_s":           BURST_STAKE_LADDER_DURATION_S,
            "burst_profit_pos_s":       BURST_PROFIT_POSITIVE_CLOSE_S,
            # 1000s scout ladder
            "k1000_phase":             self.k1000_phase,
            "k1000_phase_ts":          round(self.k1000_phase_ts, 3),
            "k1000_peak":              round(self.k1000_peak, 4),
            "k1000_profit_ts":         round(self.k1000_profit_ts, 3),
            "k1000_spike_hold_until":  round(self.k1000_spike_hold_until, 3),
            "k1000_cycle_stake":       round(self.k1000_cycle_stake or K1000_STAKE_START, 2),
            "hour_pnl_1000":           round(self.hour_pnl_1000, 4),
            "hour_start_ts_1000":      round(self.hour_start_ts_1000, 3),
            "k1000_blocked_until":     round(self.k1000_blocked_until, 3),
            "k1000_stake_idx":          self.k1000_stake_idx,
            "k1000_spike_triggered":    getattr(self, 'k1000_spike_triggered', False),
            "k1000_contract_s":         K1000_CONTRACT_S,
            "k1000_spike_contract_s":   K1000_SPIKE_CONTRACT_S,
            "k1000_spike_hold_s":       K1000_SPIKE_HOLD_S,
            "s500_drought_s1_ts":      round(self.s500_drought_s1_ts, 3),
            "s500_cur_stake":          round(self.s500_cur_stake, 2),
            "crash500_next_open_ts":   round(self.crash500_next_open_ts, 3),
            "k1000_scout_stake":       K1000_SCOUT_STAKE,
            "k1000_s20_stake":         K1000_S20_STAKE,
            "k1000_s40_stake":         K1000_S40_STAKE,
            "k1000_s80_stake":         K1000_S80_STAKE,
            "k1000_s200_stake":        K1000_S200_STAKE,
            "k1000_scout_s":           K1000_SCOUT_S,
            "k1000_hold_s":            K1000_HOLD_S,
            # ladder 500/600
            "peak_profit_500":          round(self.peak_profit_500, 4),
            "ladder_cycle_s":           LADDER_500_CYCLE_S,
            "ladder_cycle_crash_s":     LADDER_500_CYCLE_S_CRASH,
            "ladder_cycle_600_s":       LADDER_600_CYCLE_S,
            "ladder_contract_s":        LADDER_500_CONTRACT_S,
            "ladder_contract_crash_s":  LADDER_500_CONTRACT_S_CRASH,
            "ladder_contract_600_s":    LADDER_600_CONTRACT_S_32,
            "ladder_active_contract_s": round(getattr(self, 'ladder_active_contract_s', 0.0), 1),
            "ladder_daily_gate":        LADDER_DAILY_PNL_GATE,
            "day_pnl_500":              round(getattr(self, 'day_pnl_500', 0.0), 4),
            "day_start_ts_500":         round(getattr(self, 'day_start_ts_500', 0.0), 0),
            "day_pnl_1000":             round(getattr(self, 'day_pnl_1000', 0.0), 4),
            "ladder_rest_s":            LADDER_500_REST_S,
            "consec_wins_500":          self.consec_wins_500,
            "ladder_rest_until_500":   round(self.ladder_rest_until_500, 3),
            "rest_spikes_500":         self.rest_spikes_500,
            "current_stake_500":       round(self.current_stake_500, 2),
            # BOOM500 ventana dinámica p25/p50
            "boom500_p25":              round(getattr(self, 'boom500_p25', 126.0), 1),
            "boom500_p50":              round(getattr(self, 'boom500_p50', 363.0), 1),
            "spike_ts_buffer_500":      [round(t, 3) for t in getattr(self, 'spike_ts_buffer_500', [])],
            # CRASH500 salida dinámica p50
            "crash500_p50":              round(getattr(self, 'crash500_p50', 363.0), 1),
            "spike_ts_buffer_crash500":  [round(t, 3) for t in getattr(self, 'spike_ts_buffer_crash500', [])],
            # BOOM600 / CRASH600 salida dinámica p50
            "boom600_p50":               round(getattr(self, 'boom600_p50', 420.0), 1),
            "spike_ts_buffer_boom600":   [round(t, 3) for t in getattr(self, 'spike_ts_buffer_boom600', [])],
            "crash600_p50":              round(getattr(self, 'crash600_p50', 420.0), 1),
            "spike_ts_buffer_crash600":  [round(t, 3) for t in getattr(self, 'spike_ts_buffer_crash600', [])],
            "dead_zone_spikes_500":         getattr(self, 'dead_zone_spikes_500', 0),
            "ladder_recovery_level_500":    getattr(self, 'ladder_recovery_level_500', 0),
            "ladder_last_closed_cid":       getattr(self, 'ladder_last_closed_cid', 0),
            "cycle_budget_norm":            round(getattr(self, 'cycle_budget_norm', 0.0), 2),
            "cycle_prev_quiet":             getattr(self, 'cycle_prev_quiet', True),
            "multi_phase_idx":              getattr(self, 'multi_phase_idx', 0),
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
        self._k1000_pending_ts:    dict[str, float] = {}  # debounce reintento pendiente (no persistido)
        self._k1000_spike_check:   dict[str, float] = {}  # ventana 5s para detectar profit+ post-spike
        self._k1000_had_spike:     dict[str, bool]  = {}  # True si hubo spike en el contrato actual
        self._500_had_spike:       dict[str, bool]  = {}  # True si hubo spike en contrato 500s/600s activo
        self._evict_in_progress:   bool             = False  # guard re-entrante para _evict_lowest_stake
        self._max_contracts_until: dict[str, float] = {}     # debounce retry cuando max_contracts sin slot
        self._restore_lock = asyncio.Lock()
        # ── Análisis solapado spikes+contratos ───────────────────────────────
        self._ed_spike_hist: dict[str, list] = {sym: [] for sym in SYMBOLS_ED}
        self._ed_open_info:  dict[str, dict] = {}   # sym → {stake, t_open, ctx_at_open}
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
                    self._ed_seed_spike_hist()
                    self._restored = True
                    # R_75 no recibe ticks del daemon → loop independiente
                    for _r in SYMBOLS_R:
                        if _r not in SYMBOLS_ED_DISABLED:
                            asyncio.create_task(self._r75_independent_loop(_r))
                    # LADDER symbols (500+600) necesitan watchdog independiente
                    for _slad in SYMBOLS_LADDER:
                        if _slad not in SYMBOLS_ED_DISABLED and _slad not in self._watchdog_started:
                            self._watchdog_started.add(_slad)
                            asyncio.create_task(self._500_watchdog_loop(_slad))
                    # K1000 symbols (1000+900) scout también necesita watchdog propio
                    for _sk in SYMBOLS_K1000:
                        if _sk not in SYMBOLS_ED_DISABLED and _sk not in self._watchdog_started:
                            self._watchdog_started.add(_sk)
                            asyncio.create_task(self._1000_watchdog_loop(_sk))
                    # MULTI_NEW (50/150/300) watchdog de recolección — loop independiente
                    for _sm in SYMBOLS_MULTI_NEW:
                        if _sm not in SYMBOLS_ED_DISABLED and _sm not in self._watchdog_started:
                            self._watchdog_started.add(_sm)
                            asyncio.create_task(self._multi_watchdog_loop(_sm))
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
                if sym in SYMBOLS_LADDER:
                    _ctx = self._ed_ctx(sym, now)
                    result[sym]["ed_n30"]     = _ctx["n30"]
                    result[sym]["ed_gap_s"]   = round(_ctx["gap_s"], 1)
                    result[sym]["ed_gap_prev"] = round(_ctx["gap_prev_s"], 1)
                if sym in SYMBOLS_MULTI_NEW:
                    _pidx = getattr(st, 'multi_phase_idx', 0)
                    _pelap = (now - st.open_ts) if st.phase == "OPEN" else 0.0
                    result[sym]["multi_phase_idx"]   = _pidx
                    result[sym]["multi_stake"]       = MULTI_STAKES[_pidx]
                    result[sym]["multi_multiplier"]  = MULTI_MULTIPLIERS[sym]
                    result[sym]["multi_phase_rem_s"] = round(max(0.0, MULTI_PHASE_SECS - _pelap), 1)
        return result

    # ── Máquina de estados ───────────────────────────────────────────────────

    async def _process(self, sym: str, tick: Any) -> None:
        if sym in SYMBOLS_ED_DISABLED:
            return
        if sym in SYMBOLS_R:
            await self._process_r75(sym, tick)
            return
        if sym in SYMBOLS_300:
            await self._process_300n(sym)
            return
        if sym in SYMBOLS_LADDER:  # 500 + 600
            await self._process_500_simple(sym)
            return
        if sym in SYMBOLS_K1000:  # 1000 + 900
            await self._process_1000_scout(sym)
            return
        if sym in SYMBOLS_MULTI_NEW:
            # Los ticks solo rastrean spikes; la gestión de contratos queda en el watchdog (15s).
            # Sin este split, cada tick haría una consulta de profit al broker (excesivo).
            _ms_spk = float(self._risk.get_last_spike_ts(sym) or 0.0)
            _ms_h   = self._ed_spike_hist.get(sym, [])
            if _ms_spk > (_ms_h[-1][0] if _ms_h else 0.0) and _ms_spk > 0:
                self._ed_push_spike(sym, _ms_spk, self._risk.get_last_spike_ratio(sym) or 0.0)
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

    def _seed_boom500_buffer(self, state: Any) -> None:
        """Siembra spike_ts_buffer_500 desde deriv_spike_events.json al arrancar.
        Solo actúa si el buffer está vacío o tiene <5 entradas.
        Usa los spikes de las últimas 24h para reflejar condiciones actuales."""
        if len(getattr(state, 'spike_ts_buffer_500', [])) >= 5:
            return  # ya tiene datos suficientes — no sobreescribir
        events_path = self._logs_dir / "deriv_spike_events.json"
        if not events_path.exists():
            return
        try:
            events = json.loads(events_path.read_text())
            cutoff = time.time() - 86400  # últimas 24h para reflejar mercado actual
            boom_ts = sorted(
                float(e['ts']) for e in events
                if isinstance(e, dict) and e.get('symbol') == 'BOOM500'
                and float(e.get('ts', 0)) > cutoff
            )
            if len(boom_ts) < 6:
                # Si no hay suficientes en 24h, ampliar a 72h
                cutoff72 = time.time() - 259200
                boom_ts = sorted(
                    float(e['ts']) for e in events
                    if isinstance(e, dict) and e.get('symbol') == 'BOOM500'
                    and float(e.get('ts', 0)) > cutoff72
                )
            boom_ts = boom_ts[-100:]
            state.spike_ts_buffer_500 = boom_ts
            intervals = sorted(
                b - a for a, b in zip(boom_ts, boom_ts[1:]) if 0 < b - a < 7200
            )
            n = len(intervals)
            if n >= 5:
                def _pct(p: float) -> float:
                    idx = (p / 100.0) * (n - 1)
                    lo = int(idx)
                    return intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
                state.boom500_p25 = _pct(25.0)
                state.boom500_p50 = _pct(50.0)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] BOOM500 buffer sembrado: %d spikes, p25=%.0fs(%.1fm) p50=%.0fs(%.1fm)",
                    n + 1, state.boom500_p25, state.boom500_p25 / 60,
                    state.boom500_p50, state.boom500_p50 / 60,
                )
        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] BOOM500 seed buffer error: %s", exc)

    def _update_boom500_percentiles(self, state: Any, new_spike_ts: float) -> None:
        """Actualiza p25/p50 de intervalo entre spikes de BOOM500 con buffer rolling de 100 muestras."""
        buf: list = getattr(state, 'spike_ts_buffer_500', [])
        buf.append(new_spike_ts)
        buf = buf[-100:]
        state.spike_ts_buffer_500 = buf
        if len(buf) < 6:
            return  # insuficientes muestras — mantener fallback histórico
        intervals = sorted(
            b - a for a, b in zip(buf, buf[1:]) if 0 < b - a < 7200
        )
        n = len(intervals)
        if n < 5:
            return
        def _pct(p: float) -> float:
            idx = (p / 100.0) * (n - 1)
            lo = int(idx)
            return intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
        state.boom500_p25 = _pct(25.0)
        state.boom500_p50 = _pct(50.0)
        _LOGGER.info(
            "[ENTRADA_DIEGO] BOOM500 percentiles p25=%.0fs(%.1fm) p50=%.0fs(%.1fm) n=%d",
            state.boom500_p25, state.boom500_p25 / 60,
            state.boom500_p50, state.boom500_p50 / 60, n,
        )

    def _seed_crash500_buffer(self, state: Any) -> None:
        """Siembra spike_ts_buffer_crash500 desde deriv_spike_events.json al arrancar."""
        if len(getattr(state, 'spike_ts_buffer_crash500', [])) >= 5:
            return
        events_path = self._logs_dir / "deriv_spike_events.json"
        if not events_path.exists():
            return
        try:
            events = json.loads(events_path.read_text())
            cutoff = time.time() - 86400
            crash_ts = sorted(
                float(e['ts']) for e in events
                if isinstance(e, dict) and e.get('symbol') == 'CRASH500'
                and float(e.get('ts', 0)) > cutoff
            )
            if len(crash_ts) < 6:
                cutoff72 = time.time() - 259200
                crash_ts = sorted(
                    float(e['ts']) for e in events
                    if isinstance(e, dict) and e.get('symbol') == 'CRASH500'
                    and float(e.get('ts', 0)) > cutoff72
                )
            crash_ts = crash_ts[-100:]
            state.spike_ts_buffer_crash500 = crash_ts
            intervals = sorted(
                b - a for a, b in zip(crash_ts, crash_ts[1:]) if 0 < b - a < 7200
            )
            n = len(intervals)
            if n >= 5:
                def _pct(p: float) -> float:
                    idx = (p / 100.0) * (n - 1)
                    lo = int(idx)
                    return intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
                state.crash500_p50 = _pct(50.0)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] CRASH500 buffer sembrado: %d spikes, p50=%.0fs(%.1fm)",
                    n + 1, state.crash500_p50, state.crash500_p50 / 60,
                )
        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] CRASH500 seed buffer error: %s", exc)

    def _update_crash500_percentiles(self, state: Any, new_spike_ts: float) -> None:
        """Actualiza p50 de intervalo entre spikes de CRASH500 con buffer rolling de 100 muestras."""
        buf: list = getattr(state, 'spike_ts_buffer_crash500', [])
        buf.append(new_spike_ts)
        buf = buf[-100:]
        state.spike_ts_buffer_crash500 = buf
        if len(buf) < 6:
            return
        intervals = sorted(
            b - a for a, b in zip(buf, buf[1:]) if 0 < b - a < 7200
        )
        n = len(intervals)
        if n < 5:
            return
        def _pct(p: float) -> float:
            idx = (p / 100.0) * (n - 1)
            lo = int(idx)
            return intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
        state.crash500_p50 = _pct(50.0)
        _LOGGER.info(
            "[ENTRADA_DIEGO] CRASH500 percentiles p50=%.0fs(%.1fm) n=%d",
            state.crash500_p50, state.crash500_p50 / 60, n,
        )

    def _seed_boom600_buffer(self, state: Any) -> None:
        if len(getattr(state, 'spike_ts_buffer_boom600', [])) >= 5:
            return
        events_path = self._logs_dir / "deriv_spike_events.json"
        if not events_path.exists():
            return
        try:
            events = json.loads(events_path.read_text())
            cutoff = time.time() - 259200
            ts_list = sorted(
                float(e['ts']) for e in events
                if isinstance(e, dict) and e.get('symbol') == 'BOOM600'
                and float(e.get('ts', 0)) > cutoff
            )
            ts_list = ts_list[-100:]
            state.spike_ts_buffer_boom600 = ts_list
            intervals = sorted(b - a for a, b in zip(ts_list, ts_list[1:]) if 0 < b - a < 7200)
            n = len(intervals)
            if n >= 5:
                idx = (0.5) * (n - 1)
                lo = int(idx)
                state.boom600_p50 = intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
                _LOGGER.info("[ENTRADA_DIEGO] BOOM600 buffer sembrado: %d spikes p50=%.0fs", n + 1, state.boom600_p50)
        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] BOOM600 seed buffer error: %s", exc)

    def _update_boom600_percentiles(self, state: Any, new_spike_ts: float) -> None:
        buf: list = getattr(state, 'spike_ts_buffer_boom600', [])
        buf.append(new_spike_ts)
        buf = buf[-100:]
        state.spike_ts_buffer_boom600 = buf
        if len(buf) < 6:
            return
        intervals = sorted(b - a for a, b in zip(buf, buf[1:]) if 0 < b - a < 7200)
        n = len(intervals)
        if n < 5:
            return
        idx = 0.5 * (n - 1)
        lo = int(idx)
        state.boom600_p50 = intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
        _LOGGER.info("[ENTRADA_DIEGO] BOOM600 p50=%.0fs(%.1fm) n=%d", state.boom600_p50, state.boom600_p50 / 60, n)

    def _seed_crash600_buffer(self, state: Any) -> None:
        if len(getattr(state, 'spike_ts_buffer_crash600', [])) >= 5:
            return
        events_path = self._logs_dir / "deriv_spike_events.json"
        if not events_path.exists():
            return
        try:
            events = json.loads(events_path.read_text())
            cutoff = time.time() - 259200
            ts_list = sorted(
                float(e['ts']) for e in events
                if isinstance(e, dict) and e.get('symbol') == 'CRASH600'
                and float(e.get('ts', 0)) > cutoff
            )
            ts_list = ts_list[-100:]
            state.spike_ts_buffer_crash600 = ts_list
            intervals = sorted(b - a for a, b in zip(ts_list, ts_list[1:]) if 0 < b - a < 7200)
            n = len(intervals)
            if n >= 5:
                idx = 0.5 * (n - 1)
                lo = int(idx)
                state.crash600_p50 = intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
                _LOGGER.info("[ENTRADA_DIEGO] CRASH600 buffer sembrado: %d spikes p50=%.0fs", n + 1, state.crash600_p50)
        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] CRASH600 seed buffer error: %s", exc)

    def _update_crash600_percentiles(self, state: Any, new_spike_ts: float) -> None:
        buf: list = getattr(state, 'spike_ts_buffer_crash600', [])
        buf.append(new_spike_ts)
        buf = buf[-100:]
        state.spike_ts_buffer_crash600 = buf
        if len(buf) < 6:
            return
        intervals = sorted(b - a for a, b in zip(buf, buf[1:]) if 0 < b - a < 7200)
        n = len(intervals)
        if n < 5:
            return
        idx = 0.5 * (n - 1)
        lo = int(idx)
        state.crash600_p50 = intervals[lo] + (idx - lo) * (intervals[min(lo + 1, n - 1)] - intervals[lo])
        _LOGGER.info("[ENTRADA_DIEGO] CRASH600 p50=%.0fs(%.1fm) n=%d", state.crash600_p50, state.crash600_p50 / 60, n)

    def _ladder_tier_500(self, sym: str, t_sin_spike: float, state: Any = None) -> tuple[int, float]:
        """Retorna (tier_idx, stake). tier_idx=-1 si fuera de zona caliente o pausa programada."""
        if "BOOM" in sym and "300" in sym:
            # Spikes cada ~300 ticks (~5min). p50 inicial=300s — calibrar con datos reales.
            p50 = getattr(state, 'boom300_p50', 300.0) if state is not None else 300.0
            close_at = p50 + 120.0
            if t_sin_spike >= close_at:
                return -1, 0.0
            return 0, 20.0
        if "CRASH" in sym and "300" in sym:
            # Spikes cada ~300 ticks (~5min). p50 inicial=300s — calibrar con datos reales.
            p50 = getattr(state, 'crash300_p50', 300.0) if state is not None else 300.0
            close_at = p50 + 120.0
            if t_sin_spike >= close_at:
                return -1, 0.0
            return 1, 10.0
        if "BOOM" in sym and "500" in sym:
            # Abre inmediatamente al spike (t=0), cierra en p50+2min
            p50 = getattr(state, 'boom500_p50', 363.0) if state is not None else 363.0
            close_at = p50 + 120.0
            if t_sin_spike >= close_at:
                return -1, 0.0
            return 0, 20.0
        if "CRASH" in sym and "500" in sym:
            # Sin zona muerta — abre inmediato al spike, cierre dinámico en p50+2min
            p50 = getattr(state, 'crash500_p50', 363.0) if state is not None else 363.0
            close_at = p50 + 120.0
            if t_sin_spike >= close_at:
                return -1, 0.0
            return 1, 20.0
        if "BOOM" in sym and "600" in sym:
            # Sin zona muerta — abre inmediato al spike, cierre dinámico en p50+2min
            p50 = getattr(state, 'boom600_p50', 420.0) if state is not None else 420.0
            close_at = p50 + 120.0
            if t_sin_spike >= close_at:
                return -1, 0.0
            return 0, 20.0
        if "CRASH" in sym and "600" in sym:
            # Sin zona muerta — abre inmediato al spike, cierre dinámico en p50+2min
            p50 = getattr(state, 'crash600_p50', 420.0) if state is not None else 420.0
            close_at = p50 + 120.0
            if t_sin_spike >= close_at:
                return -1, 0.0
            return 1, 20.0
        if "600" in sym:
            tiers = LADDER_600_TIERS
        elif "CRASH" in sym:
            tiers = LADDER_500_TIERS_CRASH
        else:
            tiers = LADDER_500_TIERS
        for idx, (t_max, stake) in enumerate(tiers):
            if t_sin_spike < t_max:
                if stake <= 0.0:
                    return -1, 0.0
                return idx, stake
        return -1, 0.0

    def _burst_stake_500(self, sym: str, now: float, base_stake: float) -> float:
        """Aplica ×2 si hay ≥2 spikes en últimos 5min (burst confirmado). Cap $32."""
        state = self._states[sym]
        cutoff = now - LADDER_BURST_WINDOW_S
        n_recent = sum(1 for t in state.recent_spike_ts if t > cutoff)
        if n_recent >= LADDER_BURST_MIN_SPIKES:
            boosted = min(base_stake * 2.0, LADDER_BURST_MAX_STAKE)
            if boosted != base_stake:
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s LADDER BURST boost stake $%.0f→$%.0f (%d spikes/5min)",
                    sym, base_stake, boosted, n_recent,
                )
            return boosted
        return base_stake

    def _ladder_initial_stake(self, sym: str) -> float:
        """Stake del primer tier con stake>0 según símbolo."""
        if "600" in sym:
            tiers = LADDER_600_TIERS
        elif "CRASH" in sym:
            tiers = LADDER_500_TIERS_CRASH
        else:
            tiers = LADDER_500_TIERS
        return next((s for _, s in tiers if s > 0), 0.0)

    def _ladder_check_rest_500(self, sym: str, pnl: float, now: float) -> bool:
        """REST por win desactivado — operar continuamente para acumular datos de análisis."""
        state = self._states[sym]
        state.consec_wins_500 = 0
        return False

    # ── 500s: escalera de stake por tiempo sin spike ─────────────────────────
    # Ciclo infinito: $1→$2→$4→$8→$16→$32→$64 → wrap → $1  (broker cierra)
    # Hour gate: si hour_pnl_500 ≥ $6 en la hora UTC → parar hasta siguiente hora.

    async def _process_500_simple(self, sym: str) -> None:  # noqa: C901
        state = self._states[sym]
        now   = time.time()

        # ── Reset día UTC y gate $20 diario ──────────────────────────────────
        _cur_day_epoch = float(int(now // 86400) * 86400)
        if getattr(state, 'day_start_ts_500', 0.0) < _cur_day_epoch:
            state.day_pnl_500     = 0.0
            state.day_start_ts_500 = _cur_day_epoch
            _LOGGER.info("[ENTRADA_DIEGO] %s nuevo día UTC → day_pnl=0", sym)
        # gate sym_pnl_since_reset desactivado — recolección de datos sin restricciones
        # if getattr(state, 'sym_pnl_since_reset', 0.0) >= LADDER_DAILY_PNL_GATE:
        #     return  # objetivo $20 desde reset alcanzado — pausa hasta siguiente reset

        # ── Reset hora UTC ────────────────────────────────────────────────────
        _cur_hour_epoch = float(int(now // 3600) * 3600)
        if state.hour_start_ts_500 < _cur_hour_epoch:
            # Nueva hora: resetear PnL acumulada (usada para gate REST 2wins)
            _prev = state.hour_pnl_500
            state.prev_hour_pnl_500  = _prev
            state.hour_pnl_500       = 0.0
            state.hour_start_ts_500  = _cur_hour_epoch
            # Si venía de HOUR_DONE (estado legado) o REST extendido hasta esta hora → reactivar
            if state.burst_phase == "HOUR_DONE":
                state.burst_phase       = "LADDER"
                state.last_spike_ts_500 = now
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s LADDER nueva hora UTC → ciclo fresco (prev_pnl=+%.2f)",
                    sym, _prev,
                )

        # ── Gate REST 20min (post-ciclo sin spike o 2 wins+profit positivo) ────
        if state.ladder_rest_until_500 > 0:
            # Contar spikes durante el descanso (NO cancelar REST, solo registrar)
            _spk_check = float(self._risk.get_last_spike_ts(sym) or 0.0)
            if _spk_check > state.last_spike_ts_500 and _spk_check > 0:
                _gap = (_spk_check - state.last_spike_ts_500) if state.last_spike_ts_500 > 0 else 9999.0
                state.last_spike_ts_500 = _spk_check   # actualizar referencia
                self._ed_push_spike(sym, _spk_check, self._risk.get_last_spike_ratio(sym) or 0.0)
                state.rest_spikes_500  += 1
                _abs_jump_rest = self._risk.get_last_spike_jump(sym)
                if _abs_jump_rest > 0:
                    state.power_window.append((_spk_check, _abs_jump_rest))
                    state.power_window = [(t, r) for t, r in state.power_window if t > now - 1800.0]
                # Cycle budget durante REST
                _ratio_rst = self._risk.get_last_spike_ratio(sym) or 0.0
                _n30_rst   = sum(1 for t, _ in (self._ed_spike_hist.get(sym) or []) if _spk_check - t <= 1800)
                _norm_rst  = _ratio_rst / _CYCLE_MEAN_RATIO.get(sym, 200.0)
                _qa_min_rst = _CYCLE_QA_MIN.get(sym, 3)
                if _n30_rst < _qa_min_rst:
                    state.cycle_prev_quiet = True
                elif _n30_rst >= _qa_min_rst and state.cycle_prev_quiet:
                    state.cycle_budget_norm = 0.0
                    state.cycle_prev_quiet  = False
                if _n30_rst >= _qa_min_rst and _norm_rst > 0:
                    state.cycle_budget_norm = round(state.cycle_budget_norm + _norm_rst, 2)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s LADDER REST spike #%d (gap=%.0fs) pw=%.1f — acumulando en descanso",
                    sym, state.rest_spikes_500, _gap, self._get_power_30min(sym, now),
                )
                # No extender REST por spikes — cada spike perdido es oportunidad, no señal de peligro
            if now < state.ladder_rest_until_500:
                if state.contract_id is None:
                    return  # sin contrato abierto → esperar REST
                # Hay contrato abierto durante REST → seguir para gestionar (floor, timer)
                # El bloque de apertura al final del tick protege contra abrir otro
            # REST terminado
            _rspk = state.rest_spikes_500
            state.rest_spikes_500       = 0
            state.ladder_rest_until_500 = 0.0
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s LADDER REST terminado (%d spikes) → apertura inmediata 7min", sym, _rspk,
            )
            # REST terminado → abrir de inmediato sin importar t_sin_spike
            if state.contract_id is None:
                state.burst_phase            = "LADDER"
                state.burst_phase_started_at = now
                state.peak_profit_500        = 0.0
                state.dead_zone_spikes_500   = 0
                state.ladder_active_contract_s = 420.0  # 7min fijo post-REST
                await self._open(sym, state, now, stake_override=20.0)
            return

        # ── Spike tracking ────────────────────────────────────────────────────
        _last_spk = float(self._risk.get_last_spike_ts(sym) or 0.0)
        if _last_spk > state.last_spike_ts_500 and _last_spk > 0:
            _gap = (_last_spk - state.last_spike_ts_500) if state.last_spike_ts_500 > 0 else 9999.0
            state.last_spike_ts_500 = _last_spk
            self._ed_push_spike(sym, _last_spk, self._risk.get_last_spike_ratio(sym) or 0.0)
            _abs_jump = self._risk.get_last_spike_jump(sym)
            if _abs_jump > 0:
                state.power_window.append((_last_spk, _abs_jump))
                state.power_window = [(t, r) for t, r in state.power_window if t > now - 1800.0]
            # Cycle budget: acumular ratio-norm, detectar Q→A para reset
            _ratio_spk = self._risk.get_last_spike_ratio(sym) or 0.0
            _n30_bdg   = sum(1 for t, _ in (self._ed_spike_hist.get(sym) or []) if _last_spk - t <= 1800)
            _norm_bdg  = _ratio_spk / _CYCLE_MEAN_RATIO.get(sym, 200.0)
            _qa_min    = _CYCLE_QA_MIN.get(sym, 3)
            if _n30_bdg < _qa_min:
                state.cycle_prev_quiet = True
            elif _n30_bdg >= _qa_min and state.cycle_prev_quiet:
                state.cycle_budget_norm = 0.0
                state.cycle_prev_quiet  = False
                _LOGGER.info("[ENTRADA_DIEGO] %s CYCLE_RESET Q→A budget=0 n30=%d qa_min=%d", sym, _n30_bdg, _qa_min)
            if _n30_bdg >= _qa_min and _norm_bdg > 0:
                state.cycle_budget_norm = round(state.cycle_budget_norm + _norm_bdg, 2)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s CYCLE_BUDGET +%.2f → %.2f (n30=%d ratio=%.0fx)",
                    sym, _norm_bdg, state.cycle_budget_norm, _n30_bdg, _ratio_spk,
                )
            # Actualizar p50 dinámico por símbolo
            if "BOOM" in sym and "500" in sym:
                self._update_boom500_percentiles(state, _last_spk)
            elif "CRASH" in sym and "500" in sym:
                self._update_crash500_percentiles(state, _last_spk)
            elif "BOOM" in sym and "600" in sym:
                self._update_boom600_percentiles(state, _last_spk)
            elif "CRASH" in sym and "600" in sym:
                self._update_crash600_percentiles(state, _last_spk)
            # Spike durante contrato activo: marcar para lógica de recovery
            if state.contract_id is not None:
                self._500_had_spike[sym] = True
            # Dead zone spike tracking: si no hay contrato abierto y no estamos en recovery
            if state.contract_id is None and "500" in sym and getattr(state, 'ladder_recovery_level_500', 0) == 0:
                state.dead_zone_spikes_500 += 1
                if state.dead_zone_spikes_500 >= 2:
                    state.dead_zone_spikes_500 = 0
                    state.consec_wins_500      = 0
                    _LOGGER.info("[ENTRADA_DIEGO] %s DEAD_ZONE ≥2 spikes perdidos — reset contador, sin REST", sym)
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s LADDER spike gap=%.0fs jump=%.2f pw=%.1f → ciclo reset (dz_spk=%d)",
                sym, _gap, _abs_jump, self._get_power_30min(sym, now),
                getattr(state, 'dead_zone_spikes_500', 0),
            )
            self._persist(now)

        # ── Inicializar referencia de ciclo ──────────────────────────────────
        if state.last_spike_ts_500 == 0.0:
            _init = float(self._risk.get_last_spike_ts(sym) or 0.0)
            state.last_spike_ts_500 = _init if _init > 0 else now
            _LOGGER.info("[ENTRADA_DIEGO] %s LADDER t0=%s", sym, "spike_hist" if _init > 0 else "now")

        # ── Actualizar profit y peak ──────────────────────────────────────────
        if state.contract_id is not None:
            _qp = self._query_profit(state.contract_id)
            if _qp is not None:
                state.current_profit = _qp
                if state.current_profit > state.peak_profit_500:
                    state.peak_profit_500 = state.current_profit

        # ── Profit floor: cerrar si cae 15% desde pico ≥ $0.20 ───────────────
        _PROFIT_FLOOR_PCT = 0.85
        _PROFIT_FLOOR_MIN = 0.20
        if (state.contract_id is not None
                and state.peak_profit_500 >= _PROFIT_FLOOR_MIN
                and state.current_profit < state.peak_profit_500 * _PROFIT_FLOOR_PCT):
            _cid = int(state.contract_id)
            _pnl = state.current_profit
            _pk  = state.peak_profit_500
            state.contract_id     = None
            state.current_profit  = 0.0
            state.peak_profit_500 = 0.0
            try:
                await self._executor.close_contract(_cid)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s LADDER FLOOR close error: %s", sym, exc)
            state.hour_pnl_500 += _pnl
            state.day_pnl_500  += _pnl
            self._add_global_pnl(sym, _pnl, now)
            state.last_close_profit = _pnl
            state.pnl_accounted_by_floor = True  # evitar doble conteo si BROKER_CLOSE re-procesa
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s LADDER FLOOR cid=%s peak=%.4f pnl=%.4f (%.0f%%) hour=+%.2f day=+%.2f",
                sym, _cid, _pk, _pnl, 100 * _pnl / _pk if _pk else 0, state.hour_pnl_500, state.day_pnl_500,
            )
            self._500_had_spike.pop(sym, None)
            state.ladder_recovery_level_500 = 0  # floor = profit → reset recovery
            self._ed_log(sym, _pnl, True, 0, now, close_type="FLOOR")
            self._ladder_check_rest_500(sym, _pnl, now)
            _LOGGER.info("[ENTRADA_DIEGO] %s LADDER FLOOR → esperando siguiente spike", sym)
            return

        # ── Tier actual ───────────────────────────────────────────────────────
        t_sin_spike = now - state.last_spike_ts_500
        tier_idx, stake = self._ladder_tier_500(sym, t_sin_spike, state)
        stake = self._burst_stake_500(sym, now, stake)

        # ── Recovery pendiente sin contrato: abrir solo si mercado activo ──────
        # (el open falló antes por max_open_contracts; reintento en cada tick)
        if tier_idx < 0 and state.contract_id is None:
            _rl_pending = getattr(state, 'ladder_recovery_level_500', 0)
            if _rl_pending == 1:
                _n30_rec = len([t for t, _ in state.power_window if t > now - 1800.0])
                # Gate gap_prev CRASH: si ráfaga reciente, esperar mejor entrada (no cancelar rec_lvl)
                if "CRASH" in sym:
                    _gp_rec1 = self._ed_ctx(sym, now).get("gap_prev_s", -1.0)
                    if 0 <= _gp_rec1 < 60.0:
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s REC1 HOLD gap_prev=%.0fs<60s (ráfaga — mantener rec_lvl=1)",
                            sym, _gp_rec1,
                        )
                        return
                state.burst_phase              = "LADDER"
                state.burst_phase_started_at   = now
                state.peak_profit_500          = 0.0
                state.dead_zone_spikes_500     = 0
                state.ladder_active_contract_s = 1200.0
                _rec1_stk = 20.0
                _LOGGER.info("[ENTRADA_DIEGO] %s LADDER RECOVERY-1 $%.0f/20min (retry sequía n30=%d)", sym, _rec1_stk, _n30_rec)
                await self._open(sym, state, now, stake_override=_rec1_stk)
                return
            elif _rl_pending == 2:
                state.burst_phase              = "LADDER"
                state.burst_phase_started_at   = now
                state.peak_profit_500          = 0.0
                state.dead_zone_spikes_500     = 0
                state.ladder_active_contract_s = 1800.0
                _LOGGER.info("[ENTRADA_DIEGO] %s LADDER RECOVERY-2 $60/30min (retry sequía)", sym)
                await self._open(sym, state, now, stake_override=20.0)
                return

        # ── >ciclo sin spike: sequía — cerrar contrato si aplica, esperar próximo spike ���─
        if tier_idx < 0:
            if state.contract_id is not None:
                # Stale re-attach en sequía: el broker ya cerró este contrato (lo cerramos por timer/sequía)
                # pero nos re-adjuntamos antes de que la API procesara el cierre → detectar y liberar
                _stale_cid = state.contract_id
                if (_stale_cid == getattr(state, 'ladder_last_closed_cid', 0)
                        and self._query_contract(_stale_cid) is None):
                    state.ladder_last_closed_cid = 0
                    state.contract_id    = None
                    state.current_profit = 0.0
                    state.peak_profit_500 = 0.0
                    _LOGGER.info("[ENTRADA_DIEGO] %s LADDER stale re-attach (sequía) cid=%s → liberado", sym, _stale_cid)
                    # caer al bloque de recovery pendiente en la próxima iteración
                    return
                contract_age = now - state.burst_phase_started_at
                _contract_s = getattr(state, 'ladder_active_contract_s', 0.0) or LADDER_500_CONTRACT_S
                if contract_age < _contract_s:
                    # Dentro del tiempo de contrato: dejar correr (floor vigila)
                    state.burst_phase = "LADDER"
                    return
                # Spike llegó con profit → floor activo, suspender timer por sequía
                if state.peak_profit_500 >= _PROFIT_FLOOR_MIN:
                    state.burst_phase = "LADDER"
                    return
                # 4 min cumplidos y sin profit → cerrar (sin REST — sequía no activa descanso)
                _cid = int(state.contract_id)
                _pnl = state.current_profit
                state.contract_id     = None
                state.current_profit  = 0.0
                state.peak_profit_500 = 0.0
                state.ladder_last_closed_cid = _cid
                try:
                    await self._executor.close_contract(_cid)
                except Exception as exc:
                    _LOGGER.error("[ENTRADA_DIEGO] %s LADDER 12min CLOSE error: %s", sym, exc)
                state.hour_pnl_500 += _pnl
                state.day_pnl_500  += _pnl
                self._add_global_pnl(sym, _pnl, now)
                state.last_close_profit = _pnl
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s LADDER END cid=%s pnl=%.4f hour=+%.2f day=+%.2f → sequía",
                    sym, _cid, _pnl, state.hour_pnl_500, state.day_pnl_500,
                )
                _had_spk_rec = self._500_had_spike.pop(sym, False)
                if _pnl > 0 or _had_spk_rec:
                    state.ladder_recovery_level_500 = 0
                elif _pnl < 0:
                    _rl = getattr(state, 'ladder_recovery_level_500', 0)
                    state.ladder_recovery_level_500 = _rl + 1 if _rl < 2 else 0
                self._ed_log(sym, _pnl, _had_spk_rec, getattr(state, 'ladder_recovery_level_500', 0), now, close_type="SEQUIA")
                if self._ladder_check_rest_500(sym, _pnl, now):
                    return
                # Recovery tras sequía: abrir si mercado no en ráfaga
                _rl_seq = getattr(state, 'ladder_recovery_level_500', 0)
                if _rl_seq == 1:
                    _n30_rec = len([t for t, _ in state.power_window if t > now - 1800.0])
                    # Gate gap_prev CRASH: si ráfaga reciente, esperar (no cancelar rec_lvl)
                    if "CRASH" in sym:
                        _gp_rec1 = self._ed_ctx(sym, now).get("gap_prev_s", -1.0)
                        if 0 <= _gp_rec1 < 60.0:
                            _LOGGER.info(
                                "[ENTRADA_DIEGO] %s REC1 HOLD gap_prev=%.0fs<60s (ráfaga post-sequía — mantener rec_lvl=1)",
                                sym, _gp_rec1,
                            )
                            return
                    state.burst_phase              = "LADDER"
                    state.burst_phase_started_at   = now
                    state.peak_profit_500          = 0.0
                    state.dead_zone_spikes_500     = 0
                    state.ladder_active_contract_s = 1200.0
                    _rec1_stk = 20.0
                    _LOGGER.info("[ENTRADA_DIEGO] %s LADDER RECOVERY-1 $%.0f/20min (sequía n30=%d activo)", sym, _rec1_stk, _n30_rec)
                    await self._open(sym, state, now, stake_override=_rec1_stk)
                    return
                elif _rl_seq == 2:
                    # 2x sequía sin spike → reset nivel y continuar (sin REST)
                    state.ladder_recovery_level_500 = 0
                    state.rest_spikes_500           = 0
                    state.dead_zone_spikes_500      = 0
                    _LOGGER.info("[ENTRADA_DIEGO] %s LADDER 2x sequía sin spike → reset nivel, continúa sin REST", sym)
            # Sequía activa: no abrir, esperar próximo spike naturalmente
            return

        state.burst_phase = "LADDER"

        # ── Contrato activo ───────────────────────────────────────────────────
        if state.contract_id is not None:
            if self._query_contract(state.contract_id) is None:
                _cid = state.contract_id
                # Ignorar cierre tardío del broker si nosotros ya cerramos este contrato por timer
                # (el re-attach al contrato en proceso de cierre provoca un broker-close falso ~6s después)
                if _cid == getattr(state, 'ladder_last_closed_cid', 0):
                    state.ladder_last_closed_cid = 0
                    state.contract_id    = None
                    state.current_profit = 0.0
                    state.peak_profit_500 = 0.0
                    _LOGGER.info("[ENTRADA_DIEGO] %s LADDER skipping stale re-attach close cid=%s", sym, _cid)
                    return
                # Broker cerró (SL/TP): contabilizar PnL, recalcular tier, reabrir
                _pnl = state.current_profit
                _age = now - state.burst_phase_started_at
                _floor_counted = state.pnl_accounted_by_floor
                state.contract_id     = None
                state.current_profit  = 0.0
                state.peak_profit_500 = 0.0
                state.pnl_accounted_by_floor = False
                if not _floor_counted:
                    state.hour_pnl_500 += _pnl
                    state.day_pnl_500  += _pnl
                    self._add_global_pnl(sym, _pnl, now)
                state.last_close_profit = _pnl
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s LADDER BROKER_CLOSE cid=%s pnl=%.4f age=%.0fs hour=+%.2f day=+%.2f%s",
                    sym, _cid, _pnl, _age, state.hour_pnl_500, state.day_pnl_500,
                    " (floor_counted)" if _floor_counted else "",
                )
                # tp_or_ratchet: broker cerró con ganancia grande sin FLOOR nuestro → pausa global 30min
                if not _floor_counted and _pnl >= TP_RATCHET_MIN_PNL:
                    self._global_pause_until = now + TP_RATCHET_PAUSE_S
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s TP_OR_RATCHET detectado pnl=+%.2f → pausa global 30min hasta %s",
                        sym, _pnl, __import__('datetime').datetime.utcfromtimestamp(self._global_pause_until).strftime('%H:%M UTC'),
                    )
                    return
                _had_spk_rec = self._500_had_spike.pop(sym, False)
                if _pnl > 0 or _had_spk_rec:
                    state.ladder_recovery_level_500 = 0
                elif _pnl < 0:
                    _rl = getattr(state, 'ladder_recovery_level_500', 0)
                    state.ladder_recovery_level_500 = _rl + 1 if _rl < 2 else 0
                self._ed_log(sym, _pnl, _had_spk_rec, getattr(state, 'ladder_recovery_level_500', 0), now, close_type="BROKER")
                if not _floor_counted:
                    self._ladder_check_rest_500(sym, _pnl, now)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s LADDER broker-close pnl=%.4f spk=%s rec_lvl→%d",
                    sym, _pnl, _had_spk_rec, state.ladder_recovery_level_500,
                )
                return
            else:
                # ── Cierre al tiempo de contrato si el broker no cerró antes ──
                _contract_s = getattr(state, 'ladder_active_contract_s', 0.0) or LADDER_500_CONTRACT_S
                contract_age = now - state.burst_phase_started_at
                if contract_age >= _contract_s:
                    # Spike llegó con profit → floor activo, el 85% maneja el cierre
                    if state.peak_profit_500 >= _PROFIT_FLOOR_MIN:
                        return
                    _cid = int(state.contract_id)
                    _pnl = state.current_profit
                    state.contract_id     = None
                    state.current_profit  = 0.0
                    state.peak_profit_500 = 0.0
                    state.ladder_last_closed_cid = _cid  # marcar para ignorar broker-close tardío
                    try:
                        await self._executor.close_contract(_cid)
                    except Exception as exc:
                        _LOGGER.error("[ENTRADA_DIEGO] %s LADDER CLOSE error: %s", sym, exc)
                    state.hour_pnl_500 += _pnl
                    state.day_pnl_500  += _pnl
                    self._add_global_pnl(sym, _pnl, now)
                    state.last_close_profit = _pnl
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s LADDER %.0fm CLOSE cid=%s pnl=%.4f tier=%d hour=+%.2f day=+%.2f",
                        sym, _contract_s / 60, _cid, _pnl, tier_idx, state.hour_pnl_500, state.day_pnl_500,
                    )
                    _had_spk_rec = self._500_had_spike.pop(sym, False)
                    if _pnl > 0 or _had_spk_rec:
                        state.ladder_recovery_level_500 = 0
                    elif _pnl < 0:
                        _rl = getattr(state, 'ladder_recovery_level_500', 0)
                        state.ladder_recovery_level_500 = _rl + 1 if _rl < 2 else 0
                    self._ed_log(sym, _pnl, _had_spk_rec, getattr(state, 'ladder_recovery_level_500', 0), now, close_type="TIMER")
                    self._ladder_check_rest_500(sym, _pnl, now)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s LADDER timer-close pnl=%.4f spk=%s rec_lvl→%d",
                        sym, _pnl, _had_spk_rec, state.ladder_recovery_level_500,
                    )
                    return
                else:
                    return  # dentro del tiempo de contrato: dejar correr

        # ── Sin contrato: abrir en tier actual ───────────────────────────────
        # Guard: no abrir si REST todavía activo (caso contrato que se cerró durante REST)
        if state.ladder_rest_until_500 > 0 and now < state.ladder_rest_until_500:
            return
        # Recovery: abrir si mercado no en ráfaga
        _rec_lvl = getattr(state, 'ladder_recovery_level_500', 0)
        if _rec_lvl == 1:
            _n30_rec = len([t for t, _ in state.power_window if t > now - 1800.0])
            # Gate gap_prev CRASH: este spike llegó muy rápido → ráfaga → esperar (mantener rec_lvl=1)
            if "CRASH" in sym:
                _gp_rec1 = self._ed_ctx(sym, now).get("gap_prev_s", -1.0)
                if 0 <= _gp_rec1 < 60.0:
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s REC1 HOLD gap_prev=%.0fs<60s (ráfaga en spike — mantener rec_lvl=1)",
                        sym, _gp_rec1,
                    )
                    return
            state.burst_phase            = "LADDER"
            state.burst_phase_started_at = now
            state.peak_profit_500        = 0.0
            state.dead_zone_spikes_500   = 0
            state.ladder_active_contract_s = 1200.0  # 20 min
            _rec1_stk = 20.0
            _LOGGER.info("[ENTRADA_DIEGO] %s LADDER RECOVERY-1 $%.0f/20min (n30=%d activo)", sym, _rec1_stk, _n30_rec)
            await self._open(sym, state, now, stake_override=_rec1_stk)
            return
        elif _rec_lvl == 2:
            # 2x loss sin spike → reset nivel y continuar (sin REST)
            state.ladder_recovery_level_500 = 0
            state.rest_spikes_500           = 0
            state.dead_zone_spikes_500      = 0
            _LOGGER.info("[ENTRADA_DIEGO] %s LADDER 2x loss sin spike → reset nivel, continúa sin REST", sym)
        # Debounce: si max_contracts bloqueó, esperar
        if self._max_contracts_until.get(sym, 0.0) > now:
            return
        t_sin_spike = now - state.last_spike_ts_500
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s LADDER OPEN tier=%d stake=$%.0f t=%.0fs (%.1fm) power=%.1f hour_pnl=+%.2f",
            sym, tier_idx, stake, t_sin_spike, t_sin_spike / 60,
            self._get_power_30min(sym, now), state.hour_pnl_500,
        )
        state.burst_phase            = "LADDER"
        state.burst_phase_started_at = now
        state.peak_profit_500        = 0.0
        state.dead_zone_spikes_500   = 0  # entramos al mercado — reiniciar contador dead zone
        if "BOOM" in sym and "300" in sym:
            _p50_b3 = getattr(state, 'boom300_p50', 300.0)
            state.ladder_active_contract_s = max(30.0, (_p50_b3 + 120.0) - t_sin_spike)
        elif "CRASH" in sym and "300" in sym:
            _p50_c3 = getattr(state, 'crash300_p50', 300.0)
            state.ladder_active_contract_s = max(30.0, (_p50_c3 + 120.0) - t_sin_spike)
        elif "BOOM" in sym and "600" in sym:
            _p50_b6 = getattr(state, 'boom600_p50', 420.0)
            state.ladder_active_contract_s = max(30.0, (_p50_b6 + 120.0) - t_sin_spike)
        elif "CRASH" in sym and "600" in sym:
            _p50_c6 = getattr(state, 'crash600_p50', 420.0)
            state.ladder_active_contract_s = max(30.0, (_p50_c6 + 120.0) - t_sin_spike)
        elif "CRASH" in sym and "500" in sym:
            # Duración dinámica: tiempo restante hasta p50+2min desde el último spike
            _p50_crash = getattr(state, 'crash500_p50', 363.0)
            _close_at_crash = _p50_crash + 120.0
            state.ladder_active_contract_s = max(30.0, _close_at_crash - t_sin_spike)
        elif "BOOM" in sym and "500" in sym:
            # Duración dinámica: tiempo restante hasta p50+2min desde el último spike
            _p50 = getattr(state, 'boom500_p50', 363.0)
            _close_at = _p50 + 120.0
            state.ladder_active_contract_s = max(30.0, _close_at - t_sin_spike)
        else:
            state.ladder_active_contract_s = LADDER_500_CONTRACT_S
        # ── Gates L0: abrir solo si condiciones de mercado favorables ─────────
        _ctx_l0   = self._ed_ctx(sym, now)
        _n30_l0   = _ctx_l0["n30"]
        _gap_s_l0 = _ctx_l0["gap_s"] if _ctx_l0["gap_s"] >= 0 else 9999.0
        if "CRASH" in sym and "300" in sym:
            if _n30_l0 < 2:
                _LOGGER.info("[ENTRADA_DIEGO] %s GATE_L0 skip n30=%d<2", sym, _n30_l0)
                return
        elif "BOOM" in sym and "300" in sym:
            if _n30_l0 < 2:
                _LOGGER.info("[ENTRADA_DIEGO] %s GATE_L0 skip n30=%d<2", sym, _n30_l0)
                return
        elif "500" in sym or "600" in sym:
            if _n30_l0 < 4 or _n30_l0 >= 9:
                _LOGGER.info("[ENTRADA_DIEGO] %s GATE_N30 skip n30=%d (requiere 4≤n30<9)", sym, _n30_l0)
                return
        # Gate gap_prev: no abrir si los dos últimos spikes llegaron en ráfaga (<60s entre ellos)
        # Dato: n=25, WR=20%, ratio_sq/fl=4.0 — la ráfaga quema el ciclo y sigue sequía
        _gap_prev_l0 = _ctx_l0.get("gap_prev_s", -1.0)
        if 0 <= _gap_prev_l0 < 60.0:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s GATE_GAP_PREV skip gap_prev=%.0fs<60s (ráfaga — esperar ciclo)",
                sym, _gap_prev_l0,
            )
            return
        # Gate zona muerta: el spike disparador llegó 2.5-5min después del anterior
        # Dato: n=130 (nuevos), WR=35%, sq/fl=1.89 — zona de transición hacia sequía
        if 150.0 <= _gap_s_l0 < 300.0:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s GATE_GAP_DEAD skip gap_s=%.0fs (zona muerta 150-300s)",
                sym, _gap_s_l0,
            )
            return
        # Gate ciclo enfriando: actividad moderada + spike tardío = ciclo agotándose
        # Dato: n=19 (nuevos), WR=26%, sq/fl=1.60 — mercado en enfriamiento post-burst
        if 4 <= _n30_l0 < 7 and 180.0 <= _gap_s_l0 < 360.0:
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s GATE_COOLING skip n30=%d+gap_s=%.0fs (ciclo enfriando)",
                sym, _n30_l0, _gap_s_l0,
            )
            return
        # Gate ciclo agotado: no abrir si ratio-norm acumulado supera umbral (ciclo terminándose)
        _cycle_bdg = getattr(state, 'cycle_budget_norm', 0.0)
        if _cycle_bdg >= _CYCLE_BUDGET_MAX:
            state.cycle_prev_quiet = True  # armar Q→A para el primer spike post-sequía
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s GATE_CYCLE_BUDGET skip budget=%.2f>=%.1f (ciclo agotado — esperar Q→A)",
                sym, _cycle_bdg, _CYCLE_BUDGET_MAX,
            )
            return
        await self._open(sym, state, now, stake_override=stake)

    # ── BOOM300N/CRASH300N: exploración libre, martingala 2→4→8→16→32, 5min ──

    async def _process_300n(self, sym: str) -> None:
        """ACCU post-spike — BOOM300N/CRASH300N.

        Abre contratos ACCU secuenciales dentro de ventana de 90s post-spike.
        Stake $2, growth 2%, TP 10% ($0.20). Al cerrar re-abre si sigue en ventana.
        """
        state = self._states[sym]
        now   = time.time()

        # ── Mantener day_start_ts_500 / hour_start_ts_500 para persistencia ──
        _cur_day_epoch  = float(int(now // 86400) * 86400)
        _cur_hour_epoch = float(int(now // 3600)  * 3600)
        if getattr(state, 'day_start_ts_500', 0.0) < _cur_day_epoch:
            state.day_pnl_500      = 0.0
            state.day_start_ts_500 = _cur_day_epoch
        if getattr(state, 'hour_start_ts_500', 0.0) < _cur_hour_epoch:
            state.hour_pnl_500      = 0.0
            state.hour_start_ts_500 = _cur_hour_epoch

        # ── Spike tracking ───────────────────────────────────────────────────
        _last_spk = float(self._risk.get_last_spike_ts(sym) or 0.0)
        if state.last_spike_ts_500 == 0.0:
            state.last_spike_ts_500 = _last_spk if _last_spk > 0 else now
        if _last_spk > state.last_spike_ts_500 and _last_spk > 0:
            _gap = _last_spk - state.last_spike_ts_500
            state.last_spike_ts_500 = _last_spk
            self._ed_push_spike(sym, _last_spk, self._risk.get_last_spike_ratio(sym) or 0.0)
            _abs_jump = self._risk.get_last_spike_jump(sym)
            if _abs_jump > 0:
                state.power_window.append((_last_spk, _abs_jump))
                state.power_window = [(t, r) for t, r in state.power_window if t > now - 1800.0]
            _LOGGER.info("[ENTRADA_DIEGO] %s 300N spike gap=%.0fs jump=%.4f", sym, _gap, _abs_jump or 0.0)
            self._persist(now)

        # ── Verificar contrato ACCU abierto ──────────────────────────────────
        if state.contract_id is not None:
            try:
                _poc_resp = await self._executor._client.proposal_open_contract(int(state.contract_id))
            except Exception as _exc:
                _LOGGER.warning("[ENTRADA_DIEGO] %s 300N poc error: %s", sym, _exc)
                return
            if "error" in _poc_resp:
                _pnl = state.current_profit
                _LOGGER.info("[ENTRADA_DIEGO] %s 300N SPIKE/LOST cid=%s pnl=%.4f", sym, state.contract_id, _pnl)
                state.hour_pnl_500 += _pnl
                state.day_pnl_500  += _pnl
                self._add_global_pnl(sym, _pnl, now)
                state.last_close_profit  = _pnl
                state.last_spike_ts_500  = now   # gate: spike detectado por pérdida
                state.contract_id    = None
                state.current_profit = 0.0
                state.peak_profit_500 = 0.0
                self._ed_log(sym, _pnl, _pnl > 0, 2.0, now, close_type="SPIKE")
                self._persist(now)
                # fall through → gate bloqueará si spike reciente
            else:
                _poc = _poc_resp.get("proposal_open_contract", {})
                _is_sold = bool(_poc.get("is_sold", 0))
                _profit  = float(_poc.get("profit", 0.0) or 0.0)
                if not _is_sold:
                    state.current_profit = _profit
                    if _profit > state.peak_profit_500:
                        state.peak_profit_500 = _profit
                    return  # activo, esperar
                # Cerrado (TP o spike)
                _pnl = float(_poc.get("profit", state.current_profit) or state.current_profit)
                _close_type = "TP" if _pnl > 0 else "SPIKE"
                _LOGGER.info("[ENTRADA_DIEGO] %s 300N %s cid=%s pnl=%.4f", sym, _close_type, state.contract_id, _pnl)
                state.hour_pnl_500 += _pnl
                state.day_pnl_500  += _pnl
                self._add_global_pnl(sym, _pnl, now)
                state.last_close_profit = _pnl
                if _close_type == "SPIKE":
                    state.last_spike_ts_500 = now  # gate: spike detectado por cierre
                state.contract_id    = None
                state.current_profit = 0.0
                state.peak_profit_500 = 0.0
                self._ed_log(sym, _pnl, _pnl > 0, 2.0, now, close_type=_close_type)
                self._persist(now)
                # fall through → gate bloqueará si spike reciente

        # ── Gate post-spike: solo abrir si último spike > 45s (Poisson optimal) ──
        _t_sin_spike = now - state.last_spike_ts_500 if state.last_spike_ts_500 > 0 else -1.0
        _GATE_S      = 45.0
        if 0 <= _t_sin_spike < _GATE_S:  # 0 cubre spike en este mismo ciclo
            return  # periodo post-spike activo, λ alta → skip

        _STAKE  = 2.0
        _GROWTH = 0.02
        _TP     = round(_STAKE * 0.30, 2)   # $0.60 = 30%
        state.burst_phase            = "ACCU"
        state.burst_phase_started_at = now
        state.peak_profit_500        = 0.0
        state.pnl_accounted_by_floor = False
        try:
            _resp = await self._executor._client.buy_accu(
                sym, _STAKE, growth_rate=_GROWTH, take_profit=_TP,
            )
            _cid = int(_resp.get("contract_id", 0) or 0)
            if _cid:
                state.contract_id    = _cid
                state.current_profit = 0.0
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s 300N ACCU opened cid=%s stake=%.2f gr=%.0f%% tp=%.2f t_sin=%.1fs",
                    sym, _cid, _STAKE, _GROWTH * 100, _TP, _t_sin_spike,
                )
            else:
                _LOGGER.error("[ENTRADA_DIEGO] %s 300N buy_accu sin cid: %s", sym, _resp)
        except Exception as _exc:
            _LOGGER.error("[ENTRADA_DIEGO] %s 300N buy_accu error: %s", sym, _exc)

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
        """Dispara _process_500_simple/_process_300n cada N segundos."""
        _interval = 5 if sym in SYMBOLS_300 else 15
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog 500 iniciado (intervalo %ds)", sym, _interval)
        while self._enabled and sym not in SYMBOLS_ED_DISABLED:
            await asyncio.sleep(_interval)
            if not self._enabled or sym in SYMBOLS_ED_DISABLED:
                break
            try:
                async with self._locks[sym]:
                    if sym in SYMBOLS_300:
                        await self._process_300n(sym)
                    else:
                        await self._process_500_simple(sym)
            except Exception as exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s watchdog error: %s", sym, exc)
        _LOGGER.info("[ENTRADA_DIEGO] %s watchdog 500 terminado", sym)

    async def _1000_watchdog_loop(self, sym: str) -> None:
        """Dispara _process_1000_scout cada 15s aunque el WS esté caído."""
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

    # ── MULTIPLIER data collection (50/150/300) ──────────────────────────────

    async def _multi_watchdog_loop(self, sym: str) -> None:
        _LOGGER.info("[ENTRADA_DIEGO] %s MULTI watchdog iniciado (15s)", sym)
        while self._enabled and sym not in SYMBOLS_ED_DISABLED:
            await asyncio.sleep(15)
            if not self._enabled or sym in SYMBOLS_ED_DISABLED:
                break
            try:
                async with self._locks[sym]:
                    await self._process_multi_data(sym)
            except Exception as _exc:
                _LOGGER.error("[ENTRADA_DIEGO] %s MULTI watchdog error: %s", sym, _exc)
        _LOGGER.info("[ENTRADA_DIEGO] %s MULTI watchdog terminado", sym)

    async def _process_multi_data(self, sym: str) -> None:
        """Abre/cierra contratos MULTIPLIER con escalera por outcome. Solo recolección de datos.
        Escalada: loss sin spike → avanza nivel. Win o loss con spike → reset a $1."""
        state = self._states[sym]
        now   = time.time()

        # ── Sincronizar spikes al historial (para spikes_during_contract en logs) ──
        _cur_spk_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)
        _hist = self._ed_spike_hist.get(sym, [])
        _hist_last_ts = _hist[-1][0] if _hist else 0.0
        if _cur_spk_ts > _hist_last_ts:
            self._ed_push_spike(sym, _cur_spk_ts, self._risk.get_last_spike_ratio(sym) or 0.0)

        # Reset día UTC
        _cur_day = float(int(now // 86400) * 86400)
        if getattr(state, 'day_start_ts_500', 0.0) < _cur_day:
            state.day_pnl_500      = 0.0
            state.day_start_ts_500 = _cur_day

        # Reset hora UTC
        _cur_hour = float(int(now // 3600) * 3600)
        if state.hour_start_ts_500 < _cur_hour:
            state.hour_pnl_500      = 0.0
            state.hour_start_ts_500 = _cur_hour

        _phase_idx = getattr(state, 'multi_phase_idx', 0)
        _stake     = MULTI_STAKES[_phase_idx]
        _mult      = MULTI_MULTIPLIERS[sym]

        # ── Contrato abierto ──────────────────────────────────────────────────
        if state.contract_id is not None:
            _profit = self._query_profit(state.contract_id)
            if _profit is not None:
                state.current_profit = _profit
            else:
                # Broker cerró el contrato (SL hit)
                _pnl = state.current_profit
                _pre_spk = self._ed_open_info.get(sym, {}).get("pre_open_spike_ts", 0.0)
                _had_spk = float(self._risk.get_last_spike_ts(sym) or 0.0) > _pre_spk
                state.day_pnl_500       = round(state.day_pnl_500  + _pnl, 4)
                state.hour_pnl_500      = round(state.hour_pnl_500 + _pnl, 4)
                state.last_close_profit = _pnl
                state.contract_id       = None
                state.current_profit    = 0.0
                state.phase             = "IDLE"
                if _pnl >= 0 or _had_spk:
                    # Win o loss+spike → reset a $1
                    state.multi_phase_idx = 0
                    _ct = "SL_WIN" if _pnl >= 0 else "SL_LOSS_SPK"
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s MULTI %s pnl=%.4f stake=$%.0f → reset $1 day=%.2f",
                        sym, _ct, _pnl, _stake, state.day_pnl_500,
                    )
                    self._ed_log(sym, _pnl, _had_spk, _phase_idx, now, close_type=_ct)
                else:
                    # Loss sin spike → escalar hasta $8 (fase 3); datos: fase 4 ($16) WR=17-35% → reset
                    if _phase_idx >= 3:
                        _new_idx = 0
                    else:
                        _new_idx = _phase_idx + 1
                    state.multi_phase_idx = _new_idx
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s MULTI SL_LOSS pnl=%.4f stake=$%.0f → fase %d→%d ($%.0f) day=%.2f",
                        sym, _pnl, _stake, _phase_idx, _new_idx, MULTI_STAKES[_new_idx], state.day_pnl_500,
                    )
                    self._ed_log(sym, _pnl, False, _phase_idx, now, close_type="SL_LOSS")
                self._persist(now)
                return

            # Timer: cerrar al cumplir 5 min
            if now - state.open_ts >= MULTI_PHASE_SECS:
                _cid = int(state.contract_id)
                state.contract_id = None
                try:
                    await self._executor.close_contract(_cid)
                except Exception as _e:
                    _LOGGER.warning("[ENTRADA_DIEGO] %s MULTI close_contract error: %s", sym, _e)
                _pnl = state.current_profit
                _pre_spk = self._ed_open_info.get(sym, {}).get("pre_open_spike_ts", 0.0)
                _had_spk = float(self._risk.get_last_spike_ts(sym) or 0.0) > _pre_spk
                state.day_pnl_500       = round(state.day_pnl_500  + _pnl, 4)
                state.hour_pnl_500      = round(state.hour_pnl_500 + _pnl, 4)
                state.last_close_profit = _pnl
                state.current_profit    = 0.0
                state.phase             = "IDLE"
                if _pnl >= 0:
                    # Win → reset a $1
                    state.multi_phase_idx = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s MULTI TIMER_WIN pnl=%.4f stake=$%.0f → reset $1 day=%.2f",
                        sym, _pnl, _stake, state.day_pnl_500,
                    )
                    self._ed_log(sym, _pnl, True, _phase_idx, now, close_type="TIMER_WIN")
                elif _had_spk:
                    # Loss con spike → reset a $1, no escalar
                    state.multi_phase_idx = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s MULTI TIMER_LOSS_SPK pnl=%.4f stake=$%.0f → reset $1 day=%.2f",
                        sym, _pnl, _stake, state.day_pnl_500,
                    )
                    self._ed_log(sym, _pnl, True, _phase_idx, now, close_type="TIMER_LOSS_SPK")
                else:
                    # Loss sin spike → escalar hasta $8 (fase 3); datos: fase 4 ($16) WR=17-35% → reset
                    if _phase_idx >= 3:
                        _new_idx = 0
                    else:
                        _new_idx = _phase_idx + 1
                    state.multi_phase_idx = _new_idx
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s MULTI TIMER_LOSS pnl=%.4f stake=$%.0f → fase %d→%d ($%.0f) day=%.2f",
                        sym, _pnl, _stake, _phase_idx, _new_idx, MULTI_STAKES[_new_idx], state.day_pnl_500,
                    )
                    self._ed_log(sym, _pnl, False, _phase_idx, now, close_type="TIMER_LOSS")
                self._persist(now)
            return

        # ── Sin contrato abierto → abrir nuevo ───────────────────────────────
        await self._open_multi(sym, _stake, _mult, _phase_idx, now)

    async def _open_multi(
        self, sym: str, stake: float, multiplier: int, phase_idx: int, now: float
    ) -> None:
        from src.execution.deriv_trader import DerivOrder
        state = self._states[sym]
        side  = "MULTDOWN" if "CRASH" in sym else "MULTUP"
        _LOGGER.info(
            "[ENTRADA_DIEGO] %s MULTI ABRIENDO %s $%.0f mult=%dx phase=%d",
            sym, side, stake, multiplier, phase_idx,
        )
        try:
            async with self._open_lock:
                order = DerivOrder(
                    symbol=sym,
                    side=side,
                    stake_usdt=stake,
                    multiplier=multiplier,
                    stop_loss_pct=0.15,
                    take_profit_pct=0.0,
                    max_hold_seconds=300.0,
                    score_breakdown={
                        "quality_tier": "entrada_diego",
                        "setup":        "multi_data",
                        "grade":        "ED",
                        "score":        0.0,
                        "entrada_diego": True,
                        "skip_dpm":     True,
                    },
                )
                result = await self._executor.execute(order)
            if result and result.get("status") == "live":
                cid = result.get("contract_id")
                state.contract_id   = int(cid) if cid else None
                state.open_ts       = now
                state.current_profit = 0.0
                state.phase         = "OPEN"
                self._ed_save_open(sym, stake, now)
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s MULTI OPEN OK contract=%s $%.0f mult=%dx",
                    sym, state.contract_id, stake, multiplier,
                )
            elif result and result.get("status") == "symbol_already_open":
                _existing = result.get("open_contracts", [])
                if _existing:
                    state.contract_id = int(_existing[0])
                    state.open_ts     = now
                    state.phase       = "OPEN"
                    _LOGGER.info("[ENTRADA_DIEGO] %s MULTI re-adjuntado %s", sym, state.contract_id)
            else:
                _LOGGER.warning("[ENTRADA_DIEGO] %s MULTI OPEN no-live %s", sym, result)
            self._persist(now)
        except Exception as exc:
            _LOGGER.error("[ENTRADA_DIEGO] %s MULTI open error $%.0f: %s", sym, stake, exc)
            self._persist(now)

    async def _process_1000_simple(self, sym: str) -> None:
        """
        900s/1000s — ciclo escalado:
          WAIT (12min 900s / 15min 1000s) → IN_CONTRACT 10min
          Spike en contrato → SPIKE_HOLD 4min → cerrar → reset $20 → WAIT
          Sin spike 10min → cerrar → escalar → WAIT
          Escalada: $20→$20→$40→$40→$80→$80→$20 (reset al completar ciclo)
        """
        state = self._states[sym]
        now   = time.time()

        # ── Reset día UTC y gate $20 diario (900s/1000s) ─────────────────────
        _cur_day_epoch_1k = float(int(now // 86400) * 86400)
        if getattr(state, 'day_start_ts_1000', 0.0) < _cur_day_epoch_1k:
            state.day_pnl_1000     = 0.0
            state.day_start_ts_1000 = _cur_day_epoch_1k
            _LOGGER.info("[ENTRADA_DIEGO] %s K1000 nuevo día UTC → day_pnl=0", sym)
        # gate diario desactivado — recolección de datos sin restricciones

        # Normalizar índice de stake (0-3)
        _sidx = max(0, min(getattr(state, "k1000_stake_idx", 0), len(K1000_STAKES_1000) - 1))
        state.k1000_stake_idx  = _sidx
        _stake = K1000_STAKES_1000[_sidx]
        state.k1000_cycle_stake = _stake  # sync display

        # Normalizar fase (backward compat con fases antiguas)
        if state.k1000_phase not in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
            state.k1000_phase    = "WAIT"
            state.k1000_phase_ts = now

        # ── Detección de spike ──────────────────────────────────────────────
        _last_spk_ts = float(self._risk.get_last_spike_ts(sym) or 0.0)
        _new_spike   = _last_spk_ts > state.last_spike_ts and _last_spk_ts > 0
        if _new_spike:
            state.last_spike_ts = _last_spk_ts
            self._ed_push_spike(sym, _last_spk_ts, self._risk.get_last_spike_ratio(sym) or 0.0)

        # Recuperación post-restart: si hubo spike durante el contrato activo pero
        # _k1000_had_spike se perdió en RAM, restaurarlo desde historial
        _phase_ts = getattr(state, 'k1000_phase_ts', 0.0)
        if _last_spk_ts > _phase_ts > 0 and not getattr(state, 'k1000_had_spike', False):
            state.k1000_had_spike = True
            _LOGGER.info(
                "[ENTRADA_DIEGO] %s K1000 spike histórico post-restart (%.0fs en contrato) → had_spike restaurado",
                sym, _last_spk_ts - _phase_ts,
            )

        phase = state.k1000_phase

        # ────────────────────────────────────────────────────────────────────
        # WAIT: apertura inmediata sin espera
        # ────────────────────────────────────────────────────────────────────
        if phase == "WAIT":
            # Gate sequia: BOOM1000/CRASH1000/BOOM900 pierden cuando ningún símbolo está en sequía
            # Datos: BOOM1000 sequia=0 WR=18% PnL=-$38 (n=22); CRASH1000 WR=24% PnL=-$22 (n=21); BOOM900 WR=38% PnL=-$19 (n=26)
            _sequia_gate_syms = SYMBOLS_1000 | {"BOOM900"}
            if sym in _sequia_gate_syms and (now - state.k1000_phase_ts) < 3600:
                _n_seq = sum(
                    1 for s, h in self._ed_spike_hist.items()
                    if s != sym and (not h or (now - max((t for t, _ in h), default=0.0)) > 300)
                )
                if _n_seq == 0:
                    return  # WAIT — ningún símbolo en sequía, retry siguiente tick
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
        # IN_CONTRACT: contrato abierto, duración 10min
        # ────────────────────────────────────────────────────────────────────
        if phase == "IN_CONTRACT":
            # Sin contrato: apertura pendiente (max_open_contracts bloqueó) → reintentar cada 3s
            # No avanzar idx, no reiniciar timer — el spike ya llegó, solo falta espacio
            if state.contract_id is None:
                _last_retry = self._k1000_pending_ts.get(sym, 0.0)
                if now - _last_retry < 3.0:
                    return  # debounce: no spamear la API cada tick
                self._k1000_pending_ts[sym] = now
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 IN_CONTRACT pendiente (sin espacio) → reintento $%.0f",
                    sym, _stake,
                )
                await self._open_1000_simple(sym, state, now)
                return

            # Actualizar profit y detectar cierre por broker
            _broker_closed = False
            _ic_prev_cid   = state.contract_id  # capturar antes de posible None
            _qp = self._query_profit(state.contract_id)
            if _qp is not None:
                state.current_profit = _qp
            else:
                _broker_closed = True
                state.contract_id = None

            # Ventana post-spike: profit aún negativo cuando llegó el spike → chequear 5s
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
                        "[ENTRADA_DIEGO] %s K1000 spike check expiró profit=%.4f → continúa 10min",
                        sym, state.current_profit,
                    )

            # Spike en contrato: marca que hubo spike (reinicia ciclo al cerrar)
            # profit+ → SPIKE_HOLD 4min (capturar ganancia)
            # profit- → abrir ventana 5s esperando que suba
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

            # Broker cerró el contrato (SL/TP)
            # Si hubo spike → WAIT 8min (spike marca señal, esperar reset)
            # Si NO hubo spike → reabrir inmediatamente (escalar, sin espera)
            if _broker_closed:
                # Skip re-adjuntar stale: contrato ya procesado en TIMER/SPIKE_HOLD
                # Usar _ic_prev_cid (capturado antes de que state.contract_id se nulleara)
                _last_closed = getattr(state, 'k1000_last_closed_cid', None)
                _cur_cid     = _ic_prev_cid
                if _last_closed is not None and _cur_cid is not None and int(_cur_cid) == int(_last_closed):
                    state.k1000_last_closed_cid = None
                    state.contract_id = None
                    state.k1000_had_spike = True  # si vuelve a re-adjuntar, no escalar
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 skip re-adjuntar stale cid=%d (ya procesado) → reabrir",
                        sym, int(_last_closed),
                    )
                    state.k1000_phase = "IN_CONTRACT"
                    state.k1000_phase_ts = now
                    self._persist(now)
                    await self._open_1000_simple(sym, state, now)
                    return
                state.k1000_last_closed_cid = None
                _pnl = state.last_close_profit
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                _had_spk = getattr(state, 'k1000_had_spike', False)
                state.k1000_had_spike = False
                # CRASH900/BOOM1000: no escalar a $20 — datos: CRASH900 $20 WR=29.7% (n=37); BOOM1000 $20 WR=42.9% (n=28)
                # CRASH1000: sí escalar — datos: $20 WR=57.1% PnL=+$32 (positivo)
                _no_escalate = sym in {"CRASH900", "BOOM1000"}
                if _no_escalate:
                    _next_idx = 0
                    _reason = "no-escala-gate→reset"
                else:
                    _next_idx = (_sidx + 1 if _sidx < len(K1000_STAKES_1000) - 1 else 0) if (not _had_spk and _pnl < 0) else 0
                    _reason   = "loss-sin-spike→escala" if (not _had_spk and _pnl < 0) else "spike/win→reset"
                state.k1000_stake_idx   = _next_idx
                state.k1000_cycle_stake = K1000_STAKES_1000[_next_idx]
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, _had_spk, _sidx, now, close_type="BROKER")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 broker-close pnl=%.4f day=+%.2f %s idx→%d ($%.0f)",
                    sym, _pnl, state.day_pnl_1000, _reason, _next_idx, K1000_STAKES_1000[_next_idx],
                )
                if _ic_prev_cid is not None:
                    state.k1000_last_closed_cid = int(_ic_prev_cid)  # evitar re-adjuntar stale
                self._persist(now)
                state.k1000_phase = "IN_CONTRACT"
                await self._open_1000_simple(sym, state, now)
                return

            # Tiempo del contrato cumplido (4min si spike-triggered, 20min si timer)
            # Si hubo spike → WAIT 8min; si NO hubo spike → reabrir inmediatamente
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
                # CRASH900/BOOM1000: no escalar a $20 — datos: CRASH900 $20 WR=29.7% (n=37); BOOM1000 $20 WR=42.9% (n=28)
                # CRASH1000: sí escalar — datos: $20 WR=57.1% PnL=+$32 (positivo)
                _no_escalate = sym in {"CRASH900", "BOOM1000"}
                if _no_escalate:
                    _next_idx = 0
                    _reason = "no-escala-gate→reset"
                else:
                    _next_idx = (_sidx + 1 if _sidx < len(K1000_STAKES_1000) - 1 else 0) if (not _had_spk and _pnl < 0) else 0
                    _reason   = "loss-sin-spike→escala" if (not _had_spk and _pnl < 0) else "spike/win→reset"
                state.k1000_stake_idx      = _next_idx
                state.k1000_cycle_stake    = K1000_STAKES_1000[_next_idx]
                state.k1000_spike_triggered = False
                state.last_close_profit    = _pnl
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                state.k1000_phase_ts       = now
                self._ed_log(sym, _pnl, _had_spk, _sidx, now, close_type="TIMER")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 %.0fmin pnl=%.4f day=+%.2f %s idx→%d ($%.0f)",
                    sym, _this_contract_s / 60, _pnl, state.day_pnl_1000, _reason, _next_idx, K1000_STAKES_1000[_next_idx],
                )
                state.k1000_last_closed_cid = _cid  # evitar re-adjuntar stale
                self._persist(now)
                state.k1000_phase = "IN_CONTRACT"
                await self._open_1000_simple(sym, state, now)
            return

        # ────────────────────────────────────────────────────────────────────
        # SPIKE_HOLD: spike llegó con profit+, esperar 4min para capturar ganancia
        # ────────────────────────────────────────────────────────────────────
        if phase == "SPIKE_HOLD":
            # Actualizar profit y detectar cierre por broker
            _broker_closed_sh = False
            _sh_prev_cid = state.contract_id  # capturar antes de posible None
            if state.contract_id is not None:
                _qp = self._query_profit(state.contract_id)
                if _qp is not None:
                    state.current_profit = _qp
                else:
                    _broker_closed_sh = True
                    state.contract_id = None

            # Broker cerró antes de los 4min → spike ya llegó (fue lo que nos puso aquí)
            # Regla: spike = reset ciclo, perdamos o ganemos — abrir inmediatamente
            if _broker_closed_sh or state.contract_id is None:
                _pnl = state.current_profit
                state.k1000_stake_idx   = 0
                state.k1000_cycle_stake = K1000_STAKES_1000[0]
                state.last_close_profit = _pnl
                state.day_pnl_1000 = getattr(state, 'day_pnl_1000', 0.0) + _pnl
                state.k1000_phase       = "IN_CONTRACT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, True, _sidx, now, close_type="BROKER")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 SPIKE_HOLD broker-close pnl=%.4f → spike→reset $%.0f inmediato",
                    sym, _pnl, K1000_STAKES_1000[0],
                )
                if _sh_prev_cid is not None:
                    state.k1000_last_closed_cid = int(_sh_prev_cid)  # evitar re-adjuntar stale
                self._persist(now)
                await self._open_1000_simple(sym, state, now)
                return

            # 4min cumplidos → cerrar y reset stake $20 (ganamos el spike)
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
                state.k1000_phase       = "IN_CONTRACT"
                state.k1000_phase_ts    = now
                self._ed_log(sym, _pnl, True, _sidx, now, close_type="SPIKE_HOLD")
                _LOGGER.info(
                    "[ENTRADA_DIEGO] %s K1000 SPIKE_HOLD 4min END pnl=%.4f day=+%.2f → reset $%.0f inmediato",
                    sym, _pnl, state.day_pnl_1000, K1000_STAKES_1000[0],
                )
                state.k1000_last_closed_cid = _cid  # evitar re-adjuntar stale
                self._persist(now)
                await self._open_1000_simple(sym, state, now)

    async def _open_1000_scout(self, sym: str, state: _SymState, now: float) -> None:
        await self._open_1000_simple(sym, state, now)

    async def _open_1000_simple(self, sym: str, state: _SymState, now: float) -> None:
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
                state.k1000_phase_ts = now  # timer 10min desde la apertura real (no desde el spike)
                state.k1000_had_spike = False  # contrato fresco: aún no ha llegado spike
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
                    _LOGGER.info("[ENTRADA_DIEGO] %s K1000 pendiente (max_contracts) → reintento sin evicción", sym)
                else:
                    state.k1000_phase = "WAIT"
                    state.k1000_phase_ts = now
                    _LOGGER.warning("[ENTRADA_DIEGO] %s K1000 OPEN no-live %s → WAIT", sym, result)
            self._persist(now)
        except Exception as exc:
            _exc_str = str(exc)
            if "max_open_contracts" in _exc_str:
                _LOGGER.info("[ENTRADA_DIEGO] %s K1000 pendiente (max_contracts) → reintento sin evicción", sym)
            else:
                _LOGGER.error("[ENTRADA_DIEGO] %s K1000 open error $%.0f: %s", sym, stake, exc)
                state.k1000_phase = "WAIT"
                state.k1000_phase_ts = now
            self._persist(now)

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
        # Actualizar contador de hora (sin HOUR_PROFIT_PROTECT — eliminado)
        if sym in SYMBOLS_500:
            _cur_hour = int(now // 3600) * 3600.0
            if _cur_hour != state.hour_start_ts_500:
                state.prev_hour_pnl_500 = state.hour_pnl_500
                state.hour_pnl_500 = 0.0

        # Debounce max_contracts: si fallamos sin poder evicionar, no spamear por 3s
        if self._max_contracts_until.get(sym, 0.0) > now:
            return
        if sym in self._max_contracts_until and self._max_contracts_until[sym] <= now:
            del self._max_contracts_until[sym]

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
            _sl_pct = 0.70  # 70% del stake: $0.70 en $1, $2.10 en $3, $4.20 en $6
            _mult, _sl, _tp, _mh = MULTIPLIER, _sl_pct, 0.65, 7200.0
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
                        "skip_dpm":     True,
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
                if sym in SYMBOLS_LADDER | SYMBOLS_K1000:
                    self._ed_save_open(sym, stake, now)
                if sym in SYMBOLS_LADDER:
                    self._ed_open_info[sym]["rec_lvl_at_open"] = state.ladder_recovery_level_500
                    state.current_stake_500 = stake  # guarda stake activo (500s: filtro REST; 600s: duración contrato $32→10min)
                if sym in SYMBOLS_500:
                    state.spikes_in_contract_500     = 0
                    state.crash500_real_spikes_500   = 0
                    state.spike_first_ts_in_contract = 0.0
                    state.pnl_accounted_by_floor     = False  # contrato nuevo, reset flag
                    # last_spike_ts_500 NO se sobreescribe aquí — es el inicio de ventana
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
                if "max_open_contracts" in str(result):
                    # Sin evicción — esperar 3s hasta que un contrato cierre naturalmente
                    self._max_contracts_until[sym] = now + 3.0
                _LOGGER.warning("[ENTRADA_DIEGO] %s OPEN FAILED: %s → IDLE", sym, result)
                state.phase = "IDLE"

        except Exception as exc:
            exc_str = str(exc)
            if "max_open_contracts" in exc_str:
                # Sin evicción — esperar 3s hasta que un contrato cierre naturalmente
                self._max_contracts_until[sym] = now + 3.0
                _LOGGER.info("[ENTRADA_DIEGO] %s max_contracts sin slot → debounce 3s (sin evicción)", sym)
                state.phase = "IDLE"
                return
            if "LimitOrderAmountTooHigh" in exc_str:
                m = re.search(r"'code_args':\s*\['([\d.]+)'\]", exc_str)
                if m:
                    max_allowed = float(m.group(1))
                    retry_stake = round(max_allowed * 0.95, 2)
                    if sym in SYMBOLS_500 and max_allowed < S500_STAKE_LOW:
                        state.broker_blocked_until_500 = now + 15.0
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s precio post-spike max=$%.2f → reintento en 15s",
                            sym, max_allowed,
                        )
                        return
                    if max_allowed >= S500_STAKE_LOW and (stake_override is None or retry_stake < stake_override):
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

    # ── Priority eviction: liberar slot para stake alto ──────────────────────

    def _get_open_stake(self, sym: str) -> float:
        """Stake activo del contrato abierto en ese símbolo, 0 si no hay contrato."""
        state = self._states.get(sym)
        if not state or not state.contract_id:
            return 0.0
        if sym in SYMBOLS_LADDER:
            return state.current_stake_500 or 0.0
        return getattr(state, 'k1000_cycle_stake', 0.0) or 0.0

    async def _evict_lowest_stake(self, pending_stake: float, now: float) -> bool:
        """Cierra el contrato de menor stake si es menor que pending_stake.
        Retorna True si liberó un slot (el caller debe reintentar el open)."""
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
            _LOGGER.info(
                "[ENTRADA_DIEGO] PRIORITY skip — lowest open=$%.0f >= pending=$%.0f",
                lowest_stake, pending_stake,
            )
            return False
        _cid = int(victim.contract_id)
        _pnl = victim.current_profit
        _LOGGER.info(
            "[ENTRADA_DIEGO] PRIORITY EVICT %s cid=%s stake=$%.0f pnl=%.4f → slot para $%.0f",
            victim_sym, _cid, lowest_stake, _pnl, pending_stake,
        )
        victim.contract_id    = None
        victim.current_profit = 0.0
        if hasattr(victim, 'peak_profit_500'):
            victim.peak_profit_500 = 0.0
        if victim_sym in SYMBOLS_LADDER:
            victim.ladder_last_closed_cid = _cid
        try:
            await self._executor.close_contract(_cid)
        except Exception as _exc:
            _LOGGER.error("[ENTRADA_DIEGO] PRIORITY EVICT close error: %s", _exc)
        # Contabilizar PnL del contrato eviccionado
        if victim_sym in SYMBOLS_LADDER:
            victim.hour_pnl_500 = getattr(victim, 'hour_pnl_500', 0.0) + _pnl
            victim.day_pnl_500  = getattr(victim, 'day_pnl_500',  0.0) + _pnl
            self._add_global_pnl(victim_sym, _pnl, now)
            victim.last_close_profit = _pnl
            # rec_lvl: tratamos la evicción como "hubo spike" → no escalar, reset a $4
            self._500_had_spike.pop(victim_sym, None)
            victim.ladder_recovery_level_500 = 0
        else:
            # K1000: evicción = spike lógico → reset ciclo a WAIT
            victim.k1000_had_spike   = False
            victim.k1000_stake_idx   = 0
            victim.k1000_cycle_stake = K1000_STAKES_1000[0]
            victim.k1000_phase       = "WAIT"
            victim.k1000_phase_ts    = now
            victim.day_pnl_1000 = getattr(victim, 'day_pnl_1000', 0.0) + _pnl
            self._add_global_pnl(victim_sym, _pnl, now)
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
            # Sembrar buffers desde spike_events al arrancar (cubre primer arranque y restarts)
            for _sym, _st in self._states.items():
                if "BOOM" in _sym and "500" in _sym:
                    self._seed_boom500_buffer(_st)
                elif "CRASH" in _sym and "500" in _sym:
                    self._seed_crash500_buffer(_st)
                elif "BOOM" in _sym and "600" in _sym:
                    self._seed_boom600_buffer(_st)
                elif "CRASH" in _sym and "600" in _sym:
                    self._seed_crash600_buffer(_st)

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
                # Restaurar PnL gate por símbolo (independiente de contrato)
                if sym in SYMBOLS_LADDER:
                    _st_pnl = self._states[sym]
                    _st_pnl.sym_pnl_since_reset = float(s.get("sym_pnl_since_reset", 0.0))
                    _st_pnl.sym_pnl_reference   = float(s.get("sym_pnl_reference",   0.0))
                    _raw_sym_pause = float(s.get("sym_pnl_pause_until", 0.0))
                    if _raw_sym_pause > now:
                        _st_pnl.sym_pnl_pause_until = _raw_sym_pause
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s SYM_PNL_PAUSE restaurado — %.1fh restantes "
                            "(acum=$%.2f, ref=$%.2f)",
                            sym, (_raw_sym_pause - now) / 3600,
                            _st_pnl.sym_pnl_since_reset, _st_pnl.sym_pnl_reference,
                        )
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
                        if sym in SYMBOLS_LADDER:
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
                            st.burst_phase            = s.get("burst_phase", "WAIT_GATE")
                            st.burst_phase_started_at = float(s.get("burst_phase_started_at", 0.0))
                            # restaurar ventana de spikes 30min para preservar gate state
                            _pw = s.get("power_window", [])
                            st.power_window = [(float(t), float(r)) for t, r in _pw if float(t) > now - 1800.0]
                            # Buffer p25/p50 dinámico BOOM500
                            st.spike_ts_buffer_500 = [float(t) for t in s.get("spike_ts_buffer_500", [])]
                            st.boom500_p25 = float(s.get("boom500_p25", 126.0))
                            st.boom500_p50 = float(s.get("boom500_p50", 363.0))
                            if "BOOM" in sym and "500" in sym:
                                self._seed_boom500_buffer(st)
                            # Buffer p50 dinámico CRASH500
                            st.spike_ts_buffer_crash500 = [float(t) for t in s.get("spike_ts_buffer_crash500", [])]
                            st.crash500_p50 = float(s.get("crash500_p50", 363.0))
                            if "CRASH" in sym and "500" in sym:
                                self._seed_crash500_buffer(st)
                            # Buffer p50 dinámico BOOM600 / CRASH600
                            st.spike_ts_buffer_boom600 = [float(t) for t in s.get("spike_ts_buffer_boom600", [])]
                            st.boom600_p50 = float(s.get("boom600_p50", 420.0))
                            if "BOOM" in sym and "600" in sym:
                                self._seed_boom600_buffer(st)
                            st.spike_ts_buffer_crash600 = [float(t) for t in s.get("spike_ts_buffer_crash600", [])]
                            st.crash600_p50 = float(s.get("crash600_p50", 420.0))
                            if "CRASH" in sym and "600" in sym:
                                self._seed_crash600_buffer(st)
                            st.dead_zone_spikes_500 = 0
                            st.ladder_recovery_level_500 = int(s.get("ladder_recovery_level_500", 0))
                            st.ladder_last_closed_cid    = int(s.get("ladder_last_closed_cid", 0))
                            st.ladder_active_contract_s  = float(s.get("ladder_active_contract_s", 0.0))
                            st.spikes_in_contract_500 = int(s.get("spikes_in_contract", 0))
                            st.last_spike_ts_500      = float(s.get("last_spike_ts_500", 0.0))
                            st.hour_spike_count_500        = int(s.get("hour_spike_count_500", 0))
                            st.hour_start_ts_500           = float(s.get("hour_start_ts_500", 0.0))
                            st.contract_start_hour_ts_500  = float(s.get("contract_start_hour_ts_500", 0.0))
                            st.s20_consec_losses           = int(s.get("s20_consec_losses", 0))
                            st.s20_crash500_wins           = int(s.get("s20_crash500_wins", 0))
                            st.hour_pnl_500                = float(s.get("hour_pnl_500", 0.0))
                            st.prev_hour_pnl_500           = float(s.get("prev_hour_pnl_500", 0.0))
                            # Gate diario $20 — restaurar para no resetear al reiniciar
                            _saved_day_ts2 = float(s.get("day_start_ts_500", 0.0))
                            _cur_day_ts2   = float(int(now // 86400) * 86400)
                            if _saved_day_ts2 > 0 and _saved_day_ts2 == _cur_day_ts2:
                                st.day_pnl_500      = float(s.get("day_pnl_500", 0.0))
                                st.day_start_ts_500 = _saved_day_ts2
                            else:
                                st.day_pnl_500      = 0.0
                                st.day_start_ts_500 = 0.0
                            st.crash500_s1_timer_s           = float(s.get("crash500_s1_timer_s", BURST_STAKE1_CRASH500_S))
                            st.crash500_real_spikes_500      = int(s.get("crash500_real_spikes_500", 0))
                            st.spike_first_ts_in_contract    = float(s.get("spike_first_ts_in_contract", 0.0))
                            st.s80_pending                   = bool(s.get("s80_pending", False))
                            _c5ts = float(s.get("crash500_next_open_ts", 0.0))
                            st.crash500_next_open_ts = _c5ts if _c5ts > now else 0.0
                            st.consec_wins_500       = 0  # siempre reset: no heredar wins de sesión anterior
                            _rut = float(s.get("ladder_rest_until_500", 0.0))
                            st.ladder_rest_until_500 = _rut if _rut > now else 0.0
                            st.rest_spikes_500       = int(s.get("rest_spikes_500", 0))
                            st.current_stake_500     = float(s.get("current_stake_500", 0.0))
                            st.cycle_budget_norm     = float(s.get("cycle_budget_norm", 0.0))
                            st.cycle_prev_quiet      = bool(s.get("cycle_prev_quiet", True))
                        if sym in SYMBOLS_K1000:
                            _k1000_ph_raw2 = s.get("k1000_phase", "WAIT")
                            _K1000_PM2 = {
                                "WAIT_SPIKE": "WAIT", "COOLING": "WAIT",
                                "WINDOW": "IN_CONTRACT",
                                "SCOUT": "WAIT", "STAKE_20": "WAIT",
                                "STAKE_40": "WAIT", "STAKE_80": "WAIT", "STAKE_200": "WAIT",
                            }
                            if _k1000_ph_raw2 in ("WAIT", "IN_CONTRACT", "SPIKE_HOLD"):
                                st.k1000_phase = _k1000_ph_raw2
                            else:
                                st.k1000_phase = _K1000_PM2.get(_k1000_ph_raw2, "WAIT")
                            st.k1000_phase_ts         = float(s.get("k1000_phase_ts", now))
                            st.k1000_spike_hold_until = float(s.get("k1000_spike_hold_until", 0.0))
                            st.k1000_stake_idx        = int(s.get("k1000_stake_idx", 0))
                            st.k1000_cycle_stake      = K1000_STAKES_1000[max(0, min(st.k1000_stake_idx, len(K1000_STAKES_1000) - 1))]
                            st.hour_pnl_1000          = float(s.get("hour_pnl_1000", 0.0))
                            st.hour_start_ts_1000     = float(s.get("hour_start_ts_1000", 0.0))
                        if phase == "PROFIT_TIMER" and float(s.get("profit_positive_ts", 0.0)) > 0:
                            st.phase              = "PROFIT_TIMER"
                            st.profit_positive_ts = float(s["profit_positive_ts"])
                        else:
                            st.phase              = "OPEN"
                            st.profit_positive_ts = 0.0
                        if sym in SYMBOLS_R:
                            st.r75_direction = s.get("r75_direction", "MULTUP")
                        mode_tag = f" [{st.sym_mode} max_holds={st.consec_max_holds} wins={st.consec_wins_active}]" if sym in SYMBOLS_LADDER else ""
                        _LOGGER.info(
                            "[ENTRADA_DIEGO] %s RESTAURADO: phase=%s contract=%s reopens=%d%s",
                            sym, st.phase, contract_id, st.reopens, mode_tag,
                        )
                        continue

                # Sin contrato — restaurar k1000_phase para 1000s/900s (importante: no depender de contract_id)
                if sym in SYMBOLS_K1000:
                    _st_1k = self._states[sym]
                    _k1000_ph_raw = s.get("k1000_phase", "WAIT")
                    # Normalizar fases antiguas a nuevas
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
                    _st_1k.k1000_phase_ts    = float(s.get("k1000_phase_ts", now))
                    _st_1k.k1000_spike_hold_until = float(s.get("k1000_spike_hold_until", 0.0))
                    _st_1k.last_spike_ts     = float(s.get("last_spike_ts", 0.0))
                    _st_1k.k1000_stake_idx   = int(s.get("k1000_stake_idx", 0))
                    _st_1k.k1000_cycle_stake = K1000_STAKES_1000[max(0, min(_st_1k.k1000_stake_idx, len(K1000_STAKES_1000) - 1))]
                    _st_1k.hour_pnl_1000     = float(s.get("hour_pnl_1000", 0.0))
                    _st_1k.hour_start_ts_1000 = float(s.get("hour_start_ts_1000", 0.0))
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s K1000 RESTAURADO sin contrato → phase=%s stake=$%.0f idx=%d",
                        sym, _st_1k.k1000_phase, _st_1k.k1000_cycle_stake, _st_1k.k1000_stake_idx,
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
                    _next = REST_STAKE_1000 if sym in SYMBOLS_K1000 else (R75_STAKE if sym in SYMBOLS_R else QUIET_STAKE_500)
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: COOLDOWN %.0fs restantes → abrirá en $%.0f",
                        sym, cooldown_until - now, _next,
                    )
                    # Convertir COOLDOWN legacy a rest_mode en cuanto termine el timer
                    if sym in SYMBOLS_K1000:
                        st.rest_mode = True
                    continue

                # Sin contrato y sin COOLDOWN vigente — verificar si rest_mode persistido
                if sym in SYMBOLS_K1000 and bool(s.get("rest_mode", False)):
                    st = self._states[sym]
                    st.rest_mode = True
                    st.reopens   = 0
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: rest_mode=True → abrirá a $%.0f",
                        sym, REST_STAKE_1000,
                    )
                    continue

                # LADDER (500+600) sin contrato: restore COMPLETO — ningún campo crítico se pierde en deploy
                if sym in SYMBOLS_LADDER:
                    st = self._states[sym]

                    # ── Fase y timing del ciclo ───────────────────────────────
                    _bp = s.get("burst_phase", "LADDER")
                    st.burst_phase = _bp if _bp in (
                        "WAIT_GATE", "TIMER_GATE", "STAKE_10", "STAKE_20", "STAKE_40",
                        "STAKE_60", "STAKE_80", "LADDER", "STOP", "IDLE", "HOUR_DONE",
                    ) else "LADDER"
                    st.burst_phase_started_at = float(s.get("burst_phase_started_at", 0.0))

                    # ── Ventana de spikes 30min (para POWER y burst multiplier) ──
                    _pw = s.get("power_window", [])
                    st.power_window = [(float(t), float(r)) for t, r in _pw if float(t) > now - 1800.0]
                    # ── Buffer p25/p50 dinámico BOOM500 ──────────────────────────
                    st.spike_ts_buffer_500 = [float(t) for t in s.get("spike_ts_buffer_500", [])]
                    st.boom500_p25 = float(s.get("boom500_p25", 126.0))
                    st.boom500_p50 = float(s.get("boom500_p50", 363.0))
                    if "BOOM" in sym and "500" in sym:
                        self._seed_boom500_buffer(st)  # siembra desde spike_events si buffer vacío
                    # ── Buffer p50 dinámico CRASH500 ─────────────────────────────
                    st.spike_ts_buffer_crash500 = [float(t) for t in s.get("spike_ts_buffer_crash500", [])]
                    st.crash500_p50 = float(s.get("crash500_p50", 363.0))
                    if "CRASH" in sym and "500" in sym:
                        self._seed_crash500_buffer(st)
                    # ── Buffer p50 dinámico BOOM600 / CRASH600 ───────────────────
                    st.spike_ts_buffer_boom600 = [float(t) for t in s.get("spike_ts_buffer_boom600", [])]
                    st.boom600_p50 = float(s.get("boom600_p50", 420.0))
                    if "BOOM" in sym and "600" in sym:
                        self._seed_boom600_buffer(st)
                    st.spike_ts_buffer_crash600 = [float(t) for t in s.get("spike_ts_buffer_crash600", [])]
                    st.crash600_p50 = float(s.get("crash600_p50", 420.0))
                    if "CRASH" in sym and "600" in sym:
                        self._seed_crash600_buffer(st)
                    st.dead_zone_spikes_500 = 0  # no heredar contador dead zone entre sesiones
                    st.ladder_recovery_level_500 = int(s.get("ladder_recovery_level_500", 0))
                    st.ladder_last_closed_cid    = int(s.get("ladder_last_closed_cid", 0))
                    st.ladder_active_contract_s  = float(s.get("ladder_active_contract_s", 0.0))
                    st.cycle_budget_norm         = float(s.get("cycle_budget_norm", 0.0))
                    st.cycle_prev_quiet          = bool(s.get("cycle_prev_quiet", True))

                    # ── Contadores de hora ────────────────────────────────────
                    st.hour_spike_count_500  = int(s.get("hour_spike_count_500", 0))
                    st.hour_start_ts_500     = float(s.get("hour_start_ts_500", 0.0))

                    # ── PnL hora (solo si misma hora UTC) ────────────────────
                    _saved_hour_ts = float(s.get("hour_start_ts_500", 0.0))
                    _cur_hour_ts   = float(int(now // 3600) * 3600)
                    if _saved_hour_ts > 0 and _saved_hour_ts == _cur_hour_ts:
                        st.hour_pnl_500 = float(s.get("hour_pnl_500", 0.0))
                        _LOGGER.info("[ENTRADA_DIEGO] %s hour_pnl_500 restaurado $%.4f (misma hora UTC)",
                                     sym, st.hour_pnl_500)
                    else:
                        st.hour_pnl_500 = 0.0
                    st.prev_hour_pnl_500   = float(s.get("prev_hour_pnl_500", 0.0))
                    st.sym_pnl_since_reset = float(s.get("sym_pnl_since_reset", 0.0))
                    # ── Gate diario $20 — restaurar para no operar si ya se alcanzó ──
                    _saved_day_ts = float(s.get("day_start_ts_500", 0.0))
                    _cur_day_ts   = float(int(now // 86400) * 86400)
                    if _saved_day_ts > 0 and _saved_day_ts == _cur_day_ts:
                        st.day_pnl_500      = float(s.get("day_pnl_500", 0.0))
                        st.day_start_ts_500 = _saved_day_ts
                        if st.sym_pnl_since_reset >= LADDER_DAILY_PNL_GATE:
                            _LOGGER.info(
                                "[ENTRADA_DIEGO] %s gate reset restaurado: pnl_since_reset=+$%.2f >= $%.0f → DONE hasta siguiente reset",
                                sym, st.sym_pnl_since_reset, LADDER_DAILY_PNL_GATE,
                            )
                    else:
                        st.day_pnl_500      = 0.0
                        st.day_start_ts_500 = 0.0

                    # ── Timestamp del último spike (NO resetear a now) ────────
                    # Resetear a now crearía un spike falso → abriría contratos en sequía.
                    st.last_spike_ts_500 = float(s.get("last_spike_ts_500", 0.0))
                    _t_sin_spk_log = now - st.last_spike_ts_500 if st.last_spike_ts_500 > 0 else 9999.0
                    _cycle_s  = LADDER_600_CYCLE_S if "600" in sym else (LADDER_500_CYCLE_S_CRASH if "CRASH" in sym else LADDER_500_CYCLE_S)
                    _tier_log = "HOT" if _t_sin_spk_log < _cycle_s else "SEQUÍA"

                    # ── REST — restaurar para no abrir si el bot estaba descansando ──
                    st.consec_wins_500       = 0   # siempre 0: no heredar wins de sesión anterior
                    _rut = float(s.get("ladder_rest_until_500", 0.0))
                    # Para BOOM500: si el bloqueo guardado excede REST normal (>1800s desde ahora)
                    # es un post-cluster, no un REST de wins — no restaurar para BOOM500
                    _rest_s_sym = LADDER_500_REST_S_BOOM if "BOOM" in sym else LADDER_500_REST_S
                    _is_post_cluster = "BOOM" in sym and _rut > now + _rest_s_sym
                    st.ladder_rest_until_500 = (_rut if _rut > now else 0.0) if not _is_post_cluster else 0.0
                    if _is_post_cluster:
                        _LOGGER.info("[ENTRADA_DIEGO] %s post-cluster ignorado en restore → operando", sym)
                    st.rest_spikes_500       = int(s.get("rest_spikes_500", 0))

                    # ── Otros campos del ciclo ────────────────────────────────
                    st.peak_profit_500           = float(s.get("peak_profit_500", 0.0))
                    st.s20_consec_losses         = int(s.get("s20_consec_losses", 0))
                    st.s20_crash500_wins         = int(s.get("s20_crash500_wins", 0))
                    st.s500_drought_s1_ts        = 0.0   # No asumir drought en arranque
                    st.broker_blocked_until_500  = 0.0   # No heredar bloqueo de sesión anterior
                    _c5ts = float(s.get("crash500_next_open_ts", 0.0))
                    st.crash500_next_open_ts = _c5ts if _c5ts > now else 0.0
                    st.current_stake_500     = float(s.get("current_stake_500", 0.0))

                    _rest_tag = f" REST={st.ladder_rest_until_500 - now:.0f}s" if st.ladder_rest_until_500 > now else ""
                    _LOGGER.info(
                        "[ENTRADA_DIEGO] %s RESTAURADO: burst_phase=%s spike=%.0fs(%s) pw=%d pnl_h=$%.2f%s",
                        sym, st.burst_phase, _t_sin_spk_log, _tier_log,
                        sum(1 for t, _ in st.power_window if t > now - 1800.0),
                        st.hour_pnl_500, _rest_tag,
                    )
                    continue

                _LOGGER.info("[ENTRADA_DIEGO] %s startup → IDLE → abre inmediato", sym)

        except Exception as exc:
            _LOGGER.warning("[ENTRADA_DIEGO] restore_from_disk error: %s", exc)

        self._persist(time.time())

    def _get_power_30min(self, sym: str, now: float) -> float:
        """Σ(abs_jump) de todos los spikes del símbolo en los últimos 30 minutos. ATR-independiente."""
        cutoff = now - 1800.0
        return sum(r for t, r in self._states[sym].power_window if t > cutoff)

    def _get_spike_count_30min(self, sym: str, now: float) -> int:
        """Número de spikes del símbolo en los últimos 30 minutos."""
        cutoff = now - 1800.0
        return sum(1 for t, _ in self._states[sym].power_window if t > cutoff)

    # ── Análisis solapado: spike history + log por contrato ─────────────────

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
        cutoff = ts - 14400.0  # mantener 4h
        while h and h[0][0] < cutoff:
            h.pop(0)

    def _ed_ctx(self, sym: str, now: float) -> dict:
        h = self._ed_spike_hist.get(sym, [])
        # Conteos por ventana
        n30  = sum(1 for t, _ in h if now - t <=  1800)
        n60  = sum(1 for t, _ in h if now - t <=  3600)
        n120 = sum(1 for t, _ in h if now - t <=  7200)
        n180 = sum(1 for t, _ in h if now - t <= 10800)
        n240 = sum(1 for t, _ in h if now - t <= 14400)
        # Ratios por ventana (median + max)
        def _stats(secs: float):
            rs = sorted(r for t, r in h if now - t <= secs and r > 0)
            if not rs:
                return 0.0, 0.0
            return round(rs[len(rs) // 2], 1), round(rs[-1], 1)
        med_r30,  max_r30  = _stats(1800)
        med_r60,  max_r60  = _stats(3600)
        med_r120, max_r120 = _stats(7200)
        # Gap desde último spike y gaps inter-spike
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
        cross_n10 = sum(
            sum(1 for t, _ in h if now - t <= 600)
            for h in self._ed_spike_hist.values()
        )
        cross_n30 = sum(
            sum(1 for t, _ in h if now - t <= 1800)
            for h in self._ed_spike_hist.values()
        )
        syms_in_sequia = sum(
            1 for s, h in self._ed_spike_hist.items()
            if s != sym and (not h or (now - max((t for t, _ in h), default=0.0)) > 300)
        )
        self._ed_open_info[sym] = {
            "stake":              stake,
            "t_open":             now,
            "hour_utc":           int(now // 3600) % 24,
            "power":              round(self._get_power_30min(sym, now), 1),
            "ctx":                self._ed_ctx(sym, now),
            "cross_n10":          cross_n10,
            "cross_n30":          cross_n30,
            "syms_in_sequia":     syms_in_sequia,
            "pre_open_spike_ts":  float(self._risk.get_last_spike_ts(sym) or 0.0),
        }

    def _ed_log(self, sym: str, pnl: float, had_spk: bool, rec_lvl: int, now: float, close_type: str = "UNKNOWN") -> None:
        info = self._ed_open_info.pop(sym, {})
        ctx_open = info.get("ctx", {})
        t_open = info.get("t_open", now)
        h_sym = self._ed_spike_hist.get(sym, [])
        spikes_during = sum(1 for t, _ in h_sym if t_open <= t <= now)
        strat = "LADDER" if sym in SYMBOLS_LADDER else ("MULTI" if sym in SYMBOLS_MULTI_NEW else "K1000")
        rec = {
            "ts":        round(now, 1),
            "sym":       sym,
            "strategy":  strat,
            "stake":     info.get("stake", 0.0),
            "pnl":       round(pnl, 4),
            "win":       pnl > 0,
            "dur_s":     round(now - t_open, 1),
            "hour_utc":  info.get("hour_utc", int(now // 3600) % 24),
            "power_open": info.get("power", 0.0),
            "rec_lvl":   info.get("rec_lvl_at_open", rec_lvl),
            "had_spike": had_spk,
            "close_type": close_type,
            "spikes_during_contract": spikes_during,
            # Contexto al abrir
            **{f"open_{k}": v for k, v in ctx_open.items()},
            "open_cross_n10":       info.get("cross_n10", 0),
            "open_cross_n30":       info.get("cross_n30", 0),
            "open_syms_in_sequia":  info.get("syms_in_sequia", 0),
            # Contexto al cerrar
            **{f"close_{k}": v for k, v in self._ed_ctx(sym, now).items()},
        }
        path = self._logs_dir / "deriv_ed_analysis.jsonl"
        try:
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def _check_sym_pnl_gate(self, sym: str, now: float) -> None:
        pass  # Gates WIN/LOSS desactivados — evaluando edge del gate n_spikes/POWER

    def _add_global_pnl(self, sym: str, profit: float, now: float) -> None:
        if sym in SYMBOLS_LADDER:
            self._states[sym].sym_pnl_since_reset += profit
        if sym not in SYMBOLS_500:
            return
        self._check_sym_pnl_gate(sym, now)
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
            snapshot = self.get_state_snapshot()
            payload  = json.dumps(snapshot, indent=2)
            self._state_file.write_text(payload)
        except Exception as exc:
            import traceback
            _LOGGER.error("[ENTRADA_DIEGO] persist FAILED: %s\n%s", exc, traceback.format_exc())
