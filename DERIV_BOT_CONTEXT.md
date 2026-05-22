# DERIV BOT — CONTEXTO COMPLETO DE OPERACIÓN

> **Propósito de este archivo:** Referencia completa de estado actual del bot de Deriv.
> Usar como contexto de sistema para Gemini/GPT gems al configurar o auditar el bot.
> **NO modifica código. Es solo documentación de configuración y lógica operativa.**
> **ARCHIVO LOCAL — NO se pushea al repo git.**

---

## 🔴 ESTADO OPERACIONAL ACTUAL — 2026-05-21

**Fase:** OPERACIONES EN VIVO — midiendo rendimiento, analizando trades de `deriv_closed_contracts.json`

**Símbolos activos (live):** `BOOM1000, CRASH500, CRASH900, CRASH1000, BOOM600, BOOM900, CRASH600`

**Últimas acciones ejecutadas:**
| Cambio | Estado | Commit/Acción |
|---|---|---|
| BOOM300/CRASH300 eliminados (InvalidSymbol) | ✅ Live | env id=514 + env_file |
| Spike persistence: LOGS_DIR fallback en deriv_risk.py | ✅ Deployd | `e63668b` |
| /api/spikes lee de DERIV_STATE_DIR (volumen correcto) | ✅ Deployd | `01bbbbc` |
| RESET·S session filter: deriv-analytics lee de LOGS (BOT_STATE_DIR) | ✅ Deployd | `aada315` |
| ATR threshold BOOM1000=20, CRASH500=15 | ✅ Live | Coolify DB |
| ATR threshold CRASH900=20, CRASH1000=20 | ✅ Live | Coolify DB |
| DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN=3.50 | ✅ Live | Coolify DB |
| DERIV_SPIKE_CYCLE_FRAC=0.08 | ✅ Live | Coolify DB |
| **Enriquecimiento spike record + tab ⚡ Spikes en /deriv** | ✅ Deployd | `ea05e71` |

**Schema de spike enriquecido (desde commit ea05e71):**
```json
{
  "ts": 1779340451.18,
  "iso": "2026-05-21T05:14:11.180370+00:00",
  "symbol": "CRASH500",
  "direction": "DOWN",
  "jump": -2.168,
  "atr": 0.02104,
  "ratio": 103.04,
  "price": 1258.72,           ← precio exacto al momento del spike
  "loss_streak": 0,           ← pérdidas consecutivas al momento
  "since_last_trade_s": 320.5, ← segundos desde último trade del símbolo
  "lockout_active": false,    ← si el bot estaba en lockout
  "bot_entered": true,        ← si el bot entró (enriquecido post-evaluate)
  "block_reason": null,       ← razón de bloqueo si bot_entered=false
  "score": 7.42,              ← score del evaluate (enriquecido post-evaluate)
  "had_open_pos": false       ← si había posición abierta al momento
}
```

**Pendiente / siguiente análisis:**
- Hacer Coolify redeploy del bot Deriv para activar `ea05e71` (spike enrichment)
- Revisar `deriv_closed_contracts.json` para calcular win rate por símbolo y exit_reason
- Crear tabla PostgreSQL `deriv_spike_events` para persistencia de spikes entre reinicios
- Evaluar si BOOM600/BOOM900/CRASH600/CRASH900 tienen suficiente edge con los umbrales actuales

---

## 1. ARQUITECTURA GENERAL

El bot es un daemon async independiente (`src/main_deriv.py`) que opera Deriv Synthetic Indices via WebSocket. Corre en su propio proceso, completamente separado del bot de Binance Spot para no contaminar el presupuesto de latencia.

```
main_deriv.py (Daemon orquestador)
├── DerivClient          → Conexión WS, recepción de ticks, buy/sell
├── DerivRiskManager     → Scoring multi-factor, lockouts, sizing
├── DerivAnalyst         → Hurst, autocorrelación, AI Gate (LLM)
├── DerivTradeExecutor   → Ejecución, trailing SL, cierre de posiciones
├── OrderRouter          → Abstracción broker-agnóstica
└── HurstCalibrator      → Loop de calibración Hurst desde PostgreSQL
```

**Background tasks que corren en paralelo:**
- `_reaper_loop` — Cierra contratos por TakeProfit/StopLoss/timeout
- `timeout_clock_loop` — Timer independiente de 5s para BOOM/CRASH spike_timeout
- `_status_writer_loop` — Escribe `logs/deriv_status.json` cada 10s
- `_balance_refresh_loop` — Refresca balance Deriv cada 30s
- `_snapshot_ttl_loop` — Expira decisiones de AI cacheadas
- `HurstCalibrator.calibration_loop` — Actualiza Hurst desde DB cada 1h
- `DerivAnalyst.history_refresh_loop` — Refresca historial de ticks cada 5min

---

## 2. CONFIGURACIÓN: ENV VARS Y DEFAULTS

Todas las variables se leen desde `.env`. Carga via `DerivSettings` (dataclass).

### 2.1 Variables Esenciales

| Variable | Default | Descripción |
|---|---|---|
| `DERIV_API_TOKEN` | *(requerido)* | Token WS OAuth de Deriv |
| `DERIV_APP_ID` | `1089` | App ID público de Deriv |
| `DERIV_DRY_RUN` | `true` | **Fail-closed: false = modo real** |
| `DERIV_SYMBOLS` | `BOOM1000,CRASH500,CRASH900,CRASH1000,BOOM600,BOOM900,CRASH600` | **Universo LIVE actual (CSV) — R_* desactivados** |

### 2.2 Capital y Riesgo

| Variable | Default | Descripción |
|---|---|---|
| `DERIV_BANKROLL_USDT` | `50.0` | Capital ring-fenceado |
| `DERIV_RISK_PER_TRADE_PCT` | `0.01` | 1% del bankroll por trade |
| `DERIV_MAX_OPEN_CONTRACTS` | `3` | Posiciones simultáneas máximas |
| `DERIV_MIN_STAKE_USDT` | `10.0` | Stake mínimo por contrato |
| `DERIV_MIN_STAKE_USDT_HARD` | `1.00` | Floor absoluto (no bajable) |
| `DERIV_MAX_STAKE_USDT` | `3.00` | **CAP ABSOLUTO** (override de todos los multiplicadores) |

**IMPORTANTE: El stake real se calcula como:**
```
risk_base = bankroll × risk_per_trade_pct
× regime_multiplier (0.65–1.00)
× score_multiplier  (0.75–1.20)
× streak_factor     (1.0 - 0.15×streak, min 0.50 if streak≥1)
× ambiguous_penalty (0.80 si dirección fue inferida)

stake = clamp(risk_base, min_stake, MAX_STAKE_USDT=$3.00)
```

### 2.3 Umbral de Entrada y Score

| Variable | Default | Descripción |
|---|---|---|
| `DERIV_MIN_SCORE` | `6.0` | Score mínimo para entrada (0–10) |
| `DERIV_AI_GATE_ENABLED` | `true` | Segunda opinión LLM |
| `DERIV_AI_MIN_CONFIDENCE` | `0.55` | Confianza mínima AI para aprobar |
| `DERIV_AI_MODELS` | `gemini-2.5-flash, gpt-4o-mini, claude-3-5-haiku` | Modelos en orden de preferencia |

**Per-symbol effective_min_score (aplicado en el risk engine):**
- BOOM/CRASH: `6.2` (spike edge ≠ trend edge)
- R_100: `5.5` (alta vol, umbral bajo)
- R_75: `6.0` (umbral medio)
- R_25, R_50: `5.5` (baja vol, mean-reversion edge)

### 2.4 Drawdown y Lockouts

| Variable | Default | Descripción |
|---|---|---|
| `DERIV_MAX_DAILY_DD_PCT` | `0.02` | 2% DD diario → lockout 12h |
| `DERIV_LOSS_STREAK_LOCKOUT` | `3` | Pérdidas consecutivas → lockout 12h |
| `DERIV_LOCKOUT_HOURS` | `12` | Duración del lockout |
| `DERIV_RESET_LOCKOUT` | `false` | Si `true`, borra lockout al arrancar |

### 2.5 Contrato

| Variable | Default | Descripción |
|---|---|---|
| `DERIV_CONTRACT_DURATION_SEC` | `300` | Duración máxima de contrato |
| `DERIV_MULTIPLIER` | `200` | Apalancamiento por default |
| `DERIV_TAKE_PROFIT_PCT` | `0.012` | TP: 1.2% (precio) |
| `DERIV_STOP_LOSS_PCT` | `0.008` | SL: 0.8% (precio) |
| `DERIV_MAX_SPREAD_PCT` | `0.0010` | Spread máximo permitido (0.10%) |
| `DERIV_POLL_SECONDS` | `1.0` | Tick rate del engine |

### 2.6 BOOM/CRASH Específico

| Variable | **Valor Live Actual** | Descripción |
|---|---|---|
| `DERIV_BOOM_CRASH_SL_PCT` | `0.60` | SL = 60% del stake ($1.80 en $3) |
| `DERIV_BOOM_CRASH_TP_PCT` | `2.50` | TP = 250% del stake ($7.50 en $3) |
| `BOOM_CRASH_SPIKE_TIMEOUT_SEC` | `450` | Fuerza cierre tras 450s si no hubo spike |
| `DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN` | **`3.50`** | Score mín efectivo en régimen calm (override hacia bajo) |
| `DERIV_SPIKE_CYCLE_FRAC` | **`0.08`** | Fracción del ciclo del símbolo para cooldown post-spike (8% de N ticks) |

**Umbrales ATR por símbolo (BOOM/CRASH) — valores live:**

| Variable | Valor | Descripción |
|---|---|---|
| `DERIV_ATR_TH_BOOM1000` | **`20`** | ATR mínimo para permitir entrada BOOM1000 |
| `DERIV_ATR_TH_BOOM600` | *(default)* | ATR mínimo BOOM600 |
| `DERIV_ATR_TH_BOOM900` | *(default)* | ATR mínimo BOOM900 |
| `DERIV_ATR_TH_CRASH500` | **`15`** | ATR mínimo CRASH500 (más bajo por mayor frecuencia de spike) |
| `DERIV_ATR_TH_CRASH600` | *(default)* | ATR mínimo CRASH600 |
| `DERIV_ATR_TH_CRASH900` | **`20`** | ATR mínimo CRASH900 |
| `DERIV_ATR_TH_CRASH1000` | **`20`** | ATR mínimo CRASH1000 |

*Nota: El SL/TP de BOOM/CRASH es relativo al stake, NO al precio. Esto evita que el noise inter-spike (1–2 ticks) active el SL antes del evento.*

### 2.7 Trailing SL Tiers

El trailing stop es escalonado, relativo al stake:

| Tier | Condición (% de stake) | Acción |
|---|---|---|
| T1 (Break-Even) | `peak >= 15%` de stake | Floor = $0 (break-even) |
| T2 (Lock Profit) | `peak >= 35%` de stake | Floor = +15% de stake |
| T3 (Trail Tight) | `peak >= 60%` de stake | Floor = peak − 5% de stake |

**BOOM/CRASH:** T1 (break-even) está DESACTIVADO para evitar salidas prematuras por noise inter-spike. Solo activan desde T2.

Variables ajustables:
```
DERIV_TRAIL_T1_PCT=0.15   (15% de stake)
DERIV_TRAIL_T2_PCT=0.35   (35% de stake)
DERIV_TRAIL_T3_PCT=0.60   (60% de stake)
DERIV_TRAIL_T2_LOCK_PCT=0.15  (lock +15%)
DERIV_TRAIL_T3_STEP_PCT=0.05  (gap 5%)
```

### 2.8 Archivos de Estado

El bot escribe estos archivos en `logs/` (que en el container mapea al volumen `LOGS_DIR`):
- `deriv_open_contracts.json` — Posiciones abiertas
- `deriv_closed_contracts.json` — Historial de cierres (**fuente principal para análisis**)
- `deriv_status.json` — Estado general (actualizado cada 10s)
- `deriv_state.json` — Persistencia interna del engine
- `deriv_control.json` — Comandos remotos (pause/resume)
- `deriv_lockout.json` — Estado del lockout activo
- `deriv_spike_events.json` — **Spikes detectados** (jump ratio >20× ATR); path fijado commit `e63668b`
- `deriv_session.json` — Timestamp de sesión actual (escrito por `/api/session` POST en BOT_STATE_DIR)

---

## 3. PERFILES DE ACTIVOS (ASSET_INTEL_PROFILES)

Cada símbolo tiene un perfil que define cómo opera el bot en ese índice.

### 3.1 Índices de Volatilidad (R_*)

#### R_10 — Volatility 10 Index
```
type:          volatility
strategy:      mean_revert
side:          both (MULTUP y MULTDOWN)
min_hurst:     0.48
min_score:     5.5
cooldown:      90s
sl_mult:       1.0× (usa DERIV_STOP_LOSS_PCT base)
tp_mult:       1.0×
trailing:      aggressive
```

#### R_25 — Volatility 25 Index
```
type:          volatility
strategy:      mean_revert
side:          both
min_hurst:     0.48
min_score:     5.5
cooldown:      90s
sl_mult:       1.0×
tp_mult:       1.0×
trailing:      aggressive
```

#### R_50 — Volatility 50 Index
```
type:          volatility
strategy:      mean_revert
side:          both
min_hurst:     0.48
min_score:     5.5
cooldown:      90s
sl_mult:       1.0×
tp_mult:       1.0×
trailing:      aggressive
```

#### R_75 — Volatility 75 Index
```
type:          volatility
strategy:      hybrid (trend + mean-revert)
side:          both
min_hurst:     0.55
min_score:     6.5
cooldown:      120s
sl_mult:       1.2×
tp_mult:       1.5×
trailing:      atr_dynamic
```

#### R_100 — Volatility 100 Index
```
type:          volatility
strategy:      trend
side:          both
min_hurst:     0.58
min_score:     6.0
cooldown:      150s
sl_mult:       1.5×
tp_mult:       3.0×
trailing:      atr_wide
```

### 3.2 BOOM Indices (Spike UP — solo MULTUP)

**Regla de hierro:** BOOM solo entra LARGO (MULTUP). La edge es el spike alcista estadístico. Entrar corto es estructuralmente perdedor.

> **BOOM300 — RETIRADO** del universo activo. Causa `InvalidSymbol` en la API de Deriv. No operar.

> **BOOM500 — NO en universo activo** (no está en DERIV_SYMBOLS live). Perfil válido si se reactiva.

#### BOOM600 — Boom 600 Index *(ACTIVO)*
```
type:          spike_boom
strategy:      spike (SMC + EMA200 Spike Hunter)
side:          MULTUP ONLY
min_hurst:     0.0 (Hurst pipeline bypasado)
min_score:     7.0
cooldown:      240s
max_hold:      450s (spike_timeout)
sl_mult:       3.0× (usa DERIV_BOOM_CRASH_SL_PCT)
tp_mult:       6.0× (usa DERIV_BOOM_CRASH_TP_PCT)
trailing:      none (T1 desactivado)
ema_dist_pct:  0.025
require_fvg:   true
```

#### BOOM900 — Boom 900 Index *(ACTIVO)*
```
type:          spike_boom
strategy:      spike
side:          MULTUP ONLY
min_hurst:     0.0
min_score:     7.0
cooldown:      240s
max_hold:      450s
sl_mult:       3.0×
tp_mult:       6.0×
trailing:      none
ema_dist_pct:  0.04
require_fvg:   true
```

#### BOOM1000 — Boom 1000 Index *(ACTIVO)*
```
type:          spike_boom
strategy:      spike
side:          MULTUP ONLY
min_hurst:     0.0
min_score:     7.0
cooldown:      240s
max_hold:      450s
sl_mult:       3.0×
tp_mult:       6.0×
trailing:      none
ema_dist_pct:  0.05
require_fvg:   true
atr_threshold: 20 (DERIV_ATR_TH_BOOM1000)
```

### 3.3 CRASH Indices (Spike DOWN — solo MULTDOWN)

**Regla de hierro:** CRASH solo entra CORTO (MULTDOWN). La edge es el spike bajista estadístico.

> **CRASH300 — RETIRADO** del universo activo. Causa `InvalidSymbol` en la API de Deriv. No operar.

#### CRASH500 — Crash 500 Index *(ACTIVO)*
```
type:          spike_crash
strategy:      spike
side:          MULTDOWN ONLY
min_hurst:     0.0
min_score:     7.0
cooldown:      300s
max_hold:      450s
sl_mult:       3.0×
tp_mult:       6.0×
trailing:      none
ema_dist_pct:  0.03
require_fvg:   true
atr_threshold: 15 (DERIV_ATR_TH_CRASH500 — más bajo por mayor frecuencia de spike)
```

#### CRASH600 — Crash 600 Index *(ACTIVO)*
```
type:          spike_crash
strategy:      spike
side:          MULTDOWN ONLY
min_hurst:     0.0
min_score:     7.0
cooldown:      300s
max_hold:      450s
sl_mult:       3.0×
tp_mult:       6.0×
trailing:      none
ema_dist_pct:  0.025
require_fvg:   true
```

#### CRASH900 — Crash 900 Index *(ACTIVO)*
```
type:          spike_crash
strategy:      spike
side:          MULTDOWN ONLY
min_hurst:     0.0
min_score:     7.0
cooldown:      300s
max_hold:      450s
sl_mult:       3.0×
tp_mult:       6.0×
trailing:      none
ema_dist_pct:  0.04
require_fvg:   true
atr_threshold: 20 (DERIV_ATR_TH_CRASH900)
```

#### CRASH1000 — Crash 1000 Index *(ACTIVO)*
```
type:          spike_crash
strategy:      spike
side:          MULTDOWN ONLY
min_hurst:     0.0
min_score:     7.0
cooldown:      300s
max_hold:      450s
sl_mult:       3.0×
tp_mult:       6.0×
trailing:      none
ema_dist_pct:  0.05
require_fvg:   true
atr_threshold: 20 (DERIV_ATR_TH_CRASH1000)
```

---

## 4. PIPELINE DE DECISIÓN (por tick)

Cada tick pasa por los siguientes bloques en orden. Un VETO en cualquier bloque detiene el proceso.

### BLOQUE 0 — Cooldown Gate
```
¿Han pasado los segundos de cooldown del símbolo?
  NO → VETO (burst prevention)
  SÍ → continúa
```
Cooldown = `max(60s, contract_duration_sec)` para todos los símbolos,
excepto el cooldown por perfil de cada símbolo (ver sección 3).

### BLOQUE 1 — Risk Engine Scoring

El `DerivRiskManager.evaluate()` computa un score 0–10 con estos factores:

| Factor | Peso máximo | Descripción |
|---|---|---|
| Trend + LinReg | 3.0 pts | Multi-ventana (20/60/180 ticks) + pendiente OLS |
| Momentum | 1.5 pts | Aceleración de rate-of-change |
| ATR adaptativo | 2.0 pts | ATR actual vs percentil histórico |
| Spread tightness | 0.5 pts | spread ≤ 0.5 × max_spread_pct |
| Stability | 1.5 pts | Filtro spike/noise |
| Loss streak penalty | -2.0 pts | Si hay 1+ pérdidas consecutivas |
| Cooldown bonus | +1.0 pt | Tras cooldown limpio |
| Bankroll headroom | +0.5 pts | DD diario < 50% del cap |

**Score máximo crudo: 10.0**
**Soft-cap tanh** (hinge=7.5, range=2.4) aplica compresión por encima de 7.5 para preservar discriminación.

#### Sub-módulos adicionales del scoring:

**Hurst Calibration (solo R_*):**
- Bucket con win_rate < 45% → -3.0 penalty
- Hurst > 0.62 + win_rate > 60% → threshold baja a 7.0

**Random Walk Veto (solo R_*):**
- Si `0.45 ≤ H ≤ 0.55` → VETO (sin edge estadístico)
- **Bypass:** si estamos en modo mean-reversion Y z-score ≥ 2.0 (desvío profundo = edge válido)

**Mean-Reversion Mode (solo R_*):**
- Activa cuando: Hurst < 0.45, o R_* con Hurst ≤ 0.51, o regime = ranging/calm
- Dirección: fade la desviación de la media de 20 ticks
  - precio > media → MULTDOWN (fade)
  - precio < media → MULTUP (fade)
- Bonus: +3.0 score, threshold baja a 6.0

**Market Geometry (canal de regresión multi-TF):**
- Canal macro (500 ticks) + micro (150 ticks) vía NumPy
- Añade/resta score según posición del precio en el canal
- Conflicto geo vs risk direction → -1.0 penalty (no veto)

**SMC Engine (Fair Value Gap + Divergencia):**
- Detecta FVG no mitigado (zona de liquidez institucional)
- Divergencia de momentum confirmada
- Si FVG dirección == divergencia dirección → +bonus SMC (varía)
- Tag: `setup_type = "SMC_FVG"`

**Micro-Channel Scalp (Hurst < 0.40):**
- Toque de banda 1.5σ del canal de 150 ticks
- Solo en R_* (nunca BOOM/CRASH)
- +1.5 score si lado scalp == lado geo

**EMA-200 Spike Hunter (solo BOOM/CRASH):**
- Calcula EMA200 de los últimos 200 ticks
- BOOM: dip 0.02–0.10% bajo EMA200 → entrada
- CRASH: rise 0.02–0.10% sobre EMA200 → entrada
- Bonus lineal: hasta +3.5 en el borde interior, 0 en el exterior
- Filtro deceleration: si la presión adversa está desacelerando → +20% bonus; si acelera → -50% bonus
- Tag: `setup_type = "EMA200_SPIKE"`, `spike_entry = True`

**BOOM/CRASH Structural Gate (veto si no hay setup):**
```
Si símbolo es BOOM o CRASH:
  Si NO hay FVG activo Y NO hay EMA200 spike-hunter → VETO
```
Esta es la razón por la que el bot NO entra en BOOM/CRASH al azar.
Solo entra en setups estructuralmente validados.

### BLOQUE 2 — Score vs Threshold

```
score < effective_min_score → VETO (logged como ENTRY_BLOCKED)
```

### BLOQUE 2b — Hard Math Override

Si el AI Gate falla o devuelve confianza < `DERIV_AI_MIN_CONFIDENCE`:
```
Se activa si cumple alguno de:
  (a) Hurst > 0.62 + autocorr_lag1 alineado con trend_dir
  (b) SMC FVG-mitigation activo con smc_side == side
  (c) Micro-scalp H < 0.40 + band_touch == side (solo R_*)

Resultado: trade procede sin AI (math > LLM)
Tag: hurst_ai_override = True
```

### BLOQUE 3 — AI Gate

```python
analyst = DerivAnalyst(settings, client)
analysis = await analyst.analyze(symbol, ticks, score_snapshot)

if analysis.ai_approved and analysis.ai_confidence >= DERIV_AI_MIN_CONFIDENCE:
    → APROBADO
else:
    → VETO (o Math Override si aplica)
```

**Modelos AI (en orden de fallback):**
1. `google/gemini-2.5-flash` (más rápido, más barato)
2. `openai/gpt-4o-mini`
3. `anthropic/claude-3-5-haiku`

**Cache AI (anti-spam):**
- micro_scalp: TTL 15s
- SMC/trend: TTL 45s
- default: TTL 60s
- **Invalidación inteligente:** se borra la caché si el score deriva >0.5, Hurst deriva >0.03, cambia el regime, cambia la dirección, o ATR deriva >15%

**Circuit Breaker AI:** Si todos los modelos fallan con 403/404 → AI desactivada por 30min (no bloquea el pipeline).

### BLOQUE 4 — Construcción de Orden

```python
order = DerivOrder(
    symbol       = symbol,
    side         = snap.side,         # "MULTUP" | "MULTDOWN"
    stake_usdt   = snap.suggested_stake_usdt,
    multiplier   = snap.suggested_multiplier,
    stop_loss_pct  = profile.sl_mult × settings.stop_loss_pct,
    take_profit_pct = profile.tp_mult × settings.take_profit_pct,
    max_hold_seconds = spike_timeout_sec(symbol)  # 450s para BOOM/CRASH, 0 para R_*
)
```

Para BOOM/CRASH, el SL/TP usa los valores stake-relativos:
```
sl = DERIV_BOOM_CRASH_SL_PCT = 0.60  (60% del stake)
tp = DERIV_BOOM_CRASH_TP_PCT = 2.50  (250% del stake)
```

### BLOQUE 5 — Ejecución y Persistencia

```
OrderRouter → DerivTradeExecutor.execute()
  → Deriv WS: buy contract
  → Guarda en deriv_open_contracts.json
  → Suscribe a actualizaciones del contrato
  → Inicia trailing SL timer
```

---

## 5. CICLO DE VIDA DE UN CONTRATO

### Apertura
1. Pipeline aprueba → `DerivTradeExecutor.execute()`
2. WS `buy` enviado a Deriv
3. Contrato guardado en `logs/deriv_open_contracts.json`
4. Bot suscrito a `proposal_open_contract` para updates en tiempo real

### Monitoreo (durante vida del contrato)
- Trailing SL actualizado en cada tick según los 3 tiers
- `timeout_clock_loop` (loop independiente de 5s): si `max_hold_seconds > 0` y tiempo >= max_hold → fuerza cierre (BOOM/CRASH)
- `reconciliation_loop` detecta contratos que Deriv cerró por SL/TP

### Cierre
- Por **SL alcanzado:** Deriv lo cierra automáticamente
- Por **TP alcanzado:** Deriv lo cierra automáticamente
- Por **spike_timeout (450s):** Bot envía `sell` forzado
- Por **trailing SL floor:** Trailing SL progresivo activa cierre

**Post-cierre:**
- `register_close(pnl_usdt)` → actualiza streak y daily DD
- Contrato movido a `logs/deriv_closed_contracts.json`

---

## 6. RÉGIMEN DE MERCADO Y AJUSTE DE TAMAÑO

El engine detecta 5 regímenes y ajusta el tamaño de posición:

| Regime | Size Multiplier | Descripción |
|---|---|---|
| `trending` | 1.00 (100%) | Estructura lineal confirmada |
| `ranging` | 0.90 (90%) | Edge parcial de mean-reversion |
| `volatile` | 0.75 (75%) | Volatilidad elevada |
| `calm` | 0.65 (65%) | ATR muy bajo, spread domina |
| `unknown` | 0.80 (80%) | Ticks insuficientes |

Adicionalmente, el score ajusta el tamaño:
- Score ≥ 8.5 → hasta +20% de tamaño
- Score < 6.5 → hasta -25% de tamaño

---

## 7. SISTEMA DE LOCKOUTS

### Lockout por Drawdown Diario
```
Condición: daily_pnl ≤ -(bankroll × max_daily_dd_pct)
           = -(50 × 0.02) = -$1.00 de pérdida diaria
Duración: 12 horas
Archivo:  logs/deriv_lockout.json
```

### Lockout por Loss Streak
```
Condición: pérdidas consecutivas >= DERIV_LOSS_STREAK_LOCKOUT (default 3)
Duración: 12 horas
```

### Reset Manual
```bash
DERIV_RESET_LOCKOUT=true  # borra el lockout al reiniciar
```

---

## 8. HURST EXPONENT — CÓMO SE INTERPRETA

El Hurst exponent (H) se calcula vía variance-at-scale sobre los últimos 200+ ticks.

| Rango H | Interpretación | Estrategia |
|---|---|---|
| H < 0.45 | Mean-reverting (oscila en torno a la media) | Fade: vender fuerza, comprar debilidad |
| 0.45–0.55 | Random walk (sin edge) | **VETO** (no entry) |
| H > 0.55 | Trending / persistente | Seguir la tendencia |
| H > 0.62 | Trending fuerte | Threshold puede bajar a 7.0 si win_rate > 60% |

**BOOM/CRASH:** Hurst pipeline **completamente bypasado**. Su edge es el spike asimétrico, no la persistencia estadística.

---

## 9. ANÁLISIS ESTADÍSTICO (DerivAnalyst)

Ejecutado antes de la AI Gate:

| Métrica | Descripción |
|---|---|
| `hurst` | Hurst exponent (0–1) |
| `autocorr_lag1` | Autocorrelación de log-returns en lag 1 |
| `vol_regime` | `expanding` / `compressing` / `normal` |
| `rolling_vol` | Std de returns en ventana de 20 ticks |
| `trend_slope_1000` | Pendiente OLS sobre 1000 ticks |
| `r_squared_1000` | R² del fit lineal sobre 1000 ticks |
| `candles` | OHLCV sintético (construido de ticks) |

---

## 10. AI GATE — PROMPT Y RESPUESTA ESPERADA

El AI Gate envía al LLM un JSON con el resumen estadístico y espera:

```json
{
  "approved": true/false,
  "confidence": 0.0–1.0,
  "reason": "explicación breve"
}
```

**Reglas del AI Gate:**
- Si `approved=true` AND `confidence >= DERIV_AI_MIN_CONFIDENCE (0.55)` → trade va
- Si AI no responde o `confidence < 0.55` → se activa Hard Math Override (si aplica)
- Cache inteligente evita llamadas redundantes (TTL por setup type)

---

## 11. REGLAS ABSOLUTAS (NO NEGOCIABLES)

Estas reglas **nunca** pueden ser bypassadas por score, configuración o AI:

1. **Score debe ser ≥ effective_min_score** antes de cualquier orden
2. **N pérdidas consecutivas → lockout 12h** (por defecto N=3)
3. **Daily DD ≥ 2% de bankroll → lockout 12h**
4. **Spread > max_spread_pct → veto siempre**
5. **BOOM solo entra MULTUP, CRASH solo entra MULTDOWN** (hard veto)
6. **BOOM/CRASH requieren setup estructural** (FVG o EMA200 spike-hunter)
7. **NO Martingale:** stake siempre proporcional al bankroll, nunca se dobla en pérdidas
8. **Stake máximo absoluto: $3.00** (DERIV_MAX_STAKE_USDT, override de todos los multiplicadores)

---

## 12. TABLA RESUMEN: UNIVERSO DE SÍMBOLOS

### Activos en LIVE (DERIV_SYMBOLS actual)

| Símbolo | Edge principal | Dirección | Cooldown | max_hold | Score mín | SL | TP | ATR_TH |
|---|---|---|---|---|---|---|---|---|
| **BOOM600** | spike_up | **MULTUP** | 240s | **450s** | 7.0 | 60% stake | 250% stake | default |
| **BOOM900** | spike_up | **MULTUP** | 240s | **450s** | 7.0 | 60% stake | 250% stake | default |
| **BOOM1000** | spike_up | **MULTUP** | 240s | **450s** | 7.0 | 60% stake | 250% stake | **20** |
| **CRASH500** | spike_down | **MULTDOWN** | 300s | **450s** | 7.0 | 60% stake | 250% stake | **15** |
| **CRASH600** | spike_down | **MULTDOWN** | 300s | **450s** | 7.0 | 60% stake | 250% stake | default |
| **CRASH900** | spike_down | **MULTDOWN** | 300s | **450s** | 7.0 | 60% stake | 250% stake | **20** |
| **CRASH1000** | spike_down | **MULTDOWN** | 300s | **450s** | 7.0 | 60% stake | 250% stake | **20** |

### Retirados / Desactivados

| Símbolo | Razón | Fecha |
|---|---|---|
| BOOM300 | `InvalidSymbol` en API de Deriv | 2026-05-21 |
| CRASH300 | `InvalidSymbol` en API de Deriv | 2026-05-21 |
| R_100 | Fuera de DERIV_SYMBOLS (foco en BOOM/CRASH) | 2026-05-21 |
| R_75 | Fuera de DERIV_SYMBOLS | 2026-05-21 |
| R_50 | Fuera de DERIV_SYMBOLS | 2026-05-21 |
| BOOM500 | Fuera de DERIV_SYMBOLS | 2026-05-21 |

### Referencia: R_* (disponibles si se reactivan)

| Símbolo | Edge principal | Dirección | Cooldown | Score mín | SL | TP |
|---|---|---|---|---|---|---|
| R_10 | mean_revert | AMBOS | 90s | 5.5 | 1.0× base | 1.0× base |
| R_25 | mean_revert | AMBOS | 90s | 5.5 | 1.0× | 1.0× |
| R_50 | mean_revert | AMBOS | 90s | 5.5 | 1.0× | 1.0× |
| R_75 | hybrid | AMBOS | 120s | 6.5 | 1.2× | 1.5× |
| R_100 | trend | AMBOS | 150s | 6.0 | 1.5× | 3.0× |

*SL base = `DERIV_STOP_LOSS_PCT` × `sl_mult` del perfil. TP base = `DERIV_TAKE_PROFIT_PCT` × `tp_mult`.*

---

## 13. FLUJO DE SCORING COMPLETO (DIAGRAMA)

```
TICK ENTRANTE
    │
    ▼
[Cooldown Gate]  ──NO→  BLOCKED
    │
    ▼
[Risk Engine: 8 factores base → score 0–10]
    │
    ├─[Hurst calibration: +/- delta]
    ├─[Random Walk veto: H ∈ [0.45,0.55]]
    ├─[Mean-Rev router: H<0.45 → fade + +3.0]
    ├─[Market Geometry: canal regresión]
    ├─[SMC Engine: FVG + divergencia → +bonus]
    ├─[Micro-Channel Scalp: H<0.40 + 1.5σ]
    └─[EMA-200 Spike Hunter (BOOM/CRASH)]
    │
    ▼
[BOOM/CRASH Structural Gate]  ──NO FVG/EMA→  BLOCKED
    │
    ▼
[Score vs effective_min_score]  ──<threshold→  BLOCKED
    │
    ▼
[Spike Direction Veto: BOOM→MULTUP, CRASH→MULTDOWN]
    │
    ▼
[AI Gate (DerivAnalyst)]
    │
    ├─[AI approved + conf≥0.55]  →  GO
    └─[AI failed / low conf]
         ├─[Hard Math Override (Hurst/SMC/Scalp)]  →  GO (override)
         └─[No override]  →  BLOCKED
    │
    ▼
[Construir DerivOrder]
    │
    ▼
[OrderRouter → DerivTradeExecutor.execute()]
    │
    ▼
[WS buy → contrato abierto → trailing SL loop]
```

---

## 14. COMANDOS DE CONTROL REMOTO

El bot lee `logs/deriv_control.json` periódicamente:

```json
{
  "action": "pause"    // pausa el bot
  "action": "resume"   // reanuda
  "action": "close_all" // cierra todas las posiciones
}
```

---

## 15. TELEMETRÍA Y DEBUGGING

**`logs/deriv_status.json`** (actualizado cada 10s) contiene:
- Balance actual en USD
- Contratos abiertos
- Último score y breakdown por símbolo
- Últimas 30 decisiones (ring buffer)
- Counters: ticks_total, orders_sent, orders_ok, orders_failed
- Estado de lockout
- Equity history (últimos 200 puntos)

**Logs Python (`logs/bot.log`):**
- `[PIPELINE] ENTRY_BLOCKED` — razón detallada de cada rechazo
- `[deriv-risk] HARD_MATH_OVERRIDE` — override AI activado
- `[deriv-risk] [SMC ENGINE]` — setup SMC detectado
- `[hurst-calib]` — calibración de Hurst desde DB

---

## 16. CONFIGURACIÓN LIVE ACTUAL (Coolify — o4w1ns4cceccmn2ozqt7sol2)

> Esta es la configuración que está corriendo en producción a 2026-05-21.
> El `.env` real vive en el contenedor Coolify en `/var/www/html/storage/app/applications/o4w1ns4cceccmn2ozqt7sol2/.env`

```bash
# Símbolos activos (BOOM300/CRASH300 retirados por InvalidSymbol)
DERIV_SYMBOLS=BOOM1000,CRASH500,CRASH900,CRASH1000,BOOM600,BOOM900,CRASH600

# Capital
DERIV_BANKROLL_USDT=100.0       # (o valor real en Coolify)
DERIV_RISK_PER_TRADE_PCT=0.01
DERIV_MAX_STAKE_USDT=3.00
DERIV_MIN_STAKE_USDT=1.00
DERIV_MIN_STAKE_USDT_HARD=1.00

# Scoring
DERIV_MIN_SCORE=6.0
DERIV_AI_MIN_CONFIDENCE=0.55

# Seguridad
DERIV_MAX_DAILY_DD_PCT=0.02
DERIV_LOSS_STREAK_LOCKOUT=3

# BOOM/CRASH
DERIV_BOOM_CRASH_SL_PCT=0.60
DERIV_BOOM_CRASH_TP_PCT=2.50
BOOM_CRASH_SPIKE_TIMEOUT_SEC=450
DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN=3.50   # ← override score mín en régimen calm
DERIV_SPIKE_CYCLE_FRAC=0.08               # ← 8% del ciclo = cooldown post-spike

# ATR thresholds por símbolo (filtro volatilidad mínima para entrar)
DERIV_ATR_TH_BOOM1000=20
DERIV_ATR_TH_CRASH500=15
DERIV_ATR_TH_CRASH900=20
DERIV_ATR_TH_CRASH1000=20

# AI Models
OPENROUTER_API_KEY=<en Coolify>
DERIV_AI_GATE_ENABLED=true
DERIV_AI_MODELS=google/gemini-2.5-flash,openai/gpt-4o-mini,anthropic/claude-3-5-haiku
```

---

## 17. INFRAESTRUCTURA DE DEPLOYMENT

### Servidor
- **IP:** `192.81.216.49` — SSH: `root` / `R3cov3ry-N3xus!`
- **Coolify:** http://192.81.216.49:8000 (UI de gestión)
- **App UUID (Deriv bot):** `o4w1ns4cceccmn2ozqt7sol2`

### Contenedores activos

| Contenedor | Función | Imagen | Red |
|---|---|---|---|
| `o4w1ns4cceccmn2ozqt7sol2-*` | Bot Deriv (Python) | `o4w1...:sha` | coolify |
| `m0ks004osk4cw444gsokg8os-*` | Frontend Next.js | `m0ks...:sha` | coolify |
| `wgk08oogswwwc0sgcs4g4kgo-*` | Bot Binance (Python) | `wgk...:sha` | coolify |
| `t404gcck8cgo8soscowogg0s` | PostgreSQL | postgres | coolify |

### Mapa de Volúmenes (CRÍTICO)

```
HOST /data/deriv-logs  →  /data/logs en bot o4w1 (LOGS_DIR=/data/logs)
                       →  /data/deriv-logs en frontend (DERIV_STATE_DIR=/data/deriv-logs)

HOST /data/logs (Binance vol)  →  /data/logs en bot wgk (LOGS_DIR=/data/logs)
                               →  /data/logs en frontend (BOT_STATE_DIR=/data/logs)
```

### Dónde vive cada archivo de estado

| Archivo | Escrito por | Ruta en host | Env var de ruta |
|---|---|---|---|
| `deriv_closed_contracts.json` | Bot Deriv (Python) | `/data/deriv-logs/` | `LOGS_DIR` |
| `deriv_spike_events.json` | Bot Deriv (Python) | `/data/deriv-logs/` | `LOGS_DIR` (fallback) |
| `deriv_status.json` | Bot Deriv (Python) | `/data/deriv-logs/` | `LOGS_DIR` |
| `deriv_session.json` | Frontend `/api/session` POST | `/data/logs/` | `BOT_STATE_DIR` |
| `deriv_open_contracts.json` | Bot Deriv (Python) | `/data/deriv-logs/` | `LOGS_DIR` |

### PostgreSQL

- **DB:** `optiferre_pamm` en contenedor `t404gcck8cgo8soscowogg0s`
- **User:** `postgres`
- **Tablas clave:** `deriv_contracts`, `deriv_ticks`, `pamm_accounts`, `cohort_*`
- **Tabla pendiente:** `deriv_spike_events` (spikes actualmente solo en JSON)

### Cómo modificar env vars del bot Deriv

```bash
# 1. Copiar .env actual del contenedor Coolify
docker cp coolify:/var/www/html/storage/app/applications/o4w1ns4cceccmn2ozqt7sol2/.env /tmp/deriv_bot.env

# 2. Editar en host
sed -i "s|DERIV_SYMBOLS=.*|DERIV_SYMBOLS=NUEVO_VALOR|" /tmp/deriv_bot.env

# 3. Volver a copiar al contenedor
docker cp /tmp/deriv_bot.env coolify:/var/www/html/storage/app/applications/o4w1ns4cceccmn2ozqt7sol2/.env

# 4. Reiniciar bot con imagen actual (manual si Coolify no redeploya solo)
OLD_CONT=$(docker ps --format '{{.Names}}' | grep o4w1 | head -1)
IMAGE=$(docker inspect $OLD_CONT --format '{{.Config.Image}}')
NEW_NAME="o4w1ns4cceccmn2ozqt7sol2-0$(date +%m%d%H%M%S)"
docker stop $OLD_CONT
docker run -d --name "$NEW_NAME" --network coolify --restart unless-stopped \
  --env-file /tmp/deriv_bot.env \
  -v /data/deriv-logs:/data/logs \
  "$IMAGE" /app/entrypoint.deriv.sh
```

---

## 18. GUÍA DE ANÁLISIS DE OPERACIONES

### Leer operaciones cerradas desde el servidor

```bash
ssh root@192.81.216.49 "
CONT=\$(docker ps --format '{{.Names}}' | grep o4w1 | head -1)
docker exec \$CONT python3 -c '
import json
data = json.load(open(\"logs/deriv_closed_contracts.json\"))
print(f\"Total trades: {len(data)}\")
by_sym = {}
for t in data:
    s = t.get(\"symbol\",\"?\")
    r = t.get(\"realized_pnl_usdt\", 0)
    by_sym.setdefault(s, []).append(float(r or 0))
for sym, pnls in sorted(by_sym.items()):
    wins = sum(1 for p in pnls if p > 0)
    print(f\"{sym:<12} n={len(pnls):>3}  wr={wins/len(pnls)*100:.0f}%  pnl={sum(pnls):>+.2f}  avg={sum(pnls)/len(pnls):>+.3f}\")
'
"
```

### Leer spikes detectados

```bash
ssh root@192.81.216.49 "cat /data/deriv-logs/deriv_spike_events.json | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(f\"Total spikes: {len(d)}\")
for e in d[-20:]:
    print(e.get(\"iso\",\"?\"), e.get(\"symbol\",\"?\"), \"jump=\",e.get(\"jump\",\"?\"), \"ratio=\",e.get(\"ratio\",\"?\"))
'"
```

### API endpoints del frontend

| Endpoint | Descripción | Fuente |
|---|---|---|
| `GET /api/deriv-analytics` | KPIs principales, trades, session P&L | `deriv_closed_contracts.json` + `deriv_session.json` |
| `GET /api/spikes` | Spikes detectados con `?symbol=&since=&limit=` | `deriv_spike_events.json` |
| `POST /api/session` | Reset RESET·S (escribe timestamp de sesión) | → `deriv_session.json` |
| `GET /api/control` | Estado del bot (pause/resume) | `deriv_control.json` |

---

*Última actualización: 2026-05-21 — Commits relevantes: `e63668b` (spike path), `01bbbbc` (spikes API), `aada315` (session filter)*
*Archivos fuente: `src/utils/deriv_config.py`, `src/strategies/deriv_signals.py`, `src/safety/deriv_risk.py`, `src/execution/deriv_trader.py`, `src/analysis/deriv_analyst.py`, `src/main_deriv.py`*
*Infraestructura: Coolify en `192.81.216.49`, bot UUID `o4w1ns4cceccmn2ozqt7sol2`, DB `t404gcck8cgo8soscowogg0s`*
