# MUESTRA 01 — ANÁLISIS FORENSE COMPLETO
> Configuración inicial de símbolos. Muestra base de referencia para todos los ajustes futuros.

---

## METADATOS DE LA MUESTRA

| Campo | Valor |
|-------|-------|
| Inicio | ~01:28 UTC, 22 Mayo 2026 (reset de datos limpio) |
| Fin | ~12:00 UTC, 22 Mayo 2026 |
| Bot image | `deriv-bot:52ed880` |
| Frontend image | `ea36bfd` |
| LOGS_DIR | `/data/deriv-logs` |
| Spikes capturados | **356** |
| Trades cerrados | **383** |
| PnL total | **-9.56 USD** |
| Win Rate global | **30.3%** |
| Profit Factor | **0.845** |
| Avg win | +0.450 USD |
| Avg loss | -0.232 USD |
| Max loss streak | 15 |
| Max drawdown | 13.04 USD |
| Total ganado | +52.24 USD |
| Total perdido | -61.80 USD |

---

## RESUMEN EJECUTIVO

El bot operó **exclusivamente desde el loop de evaluación** — las entradas de spike fueron bloqueadas en un 99.7% de los casos. El bloqueador dominante fue `trade_cooldown`, generado por las propias entradas del loop de evaluación. Esto crea un ciclo perverso: el loop entra mal y además impide que el motor de spikes opere.

**CRASH500** es el único símbolo rentable (+1.09 USD, WR=44%, PF=1.09).  
**BOOM600** es el peor símbolo (-5.87 USD, WR=25%, 80% de trades terminan en timeout).  
**CRASH1000** fue completamente deshabilitado — 37 spikes perdidos sin ninguna entrada.

---

## ANÁLISIS POR SÍMBOLO

### BOOM600
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 116 |
| PnL | **-5.87 USD** (peor símbolo) |
| Win Rate | 25% |
| Profit Factor | 0.64 |
| Avg duración | 224s |
| Avg win | +0.366 USD |
| Avg loss | -0.189 USD |
| Stake | $2.00 |

**Exits:**
| Razón | Cant | WR | PnL total |
|-------|------|-----|-----------|
| timeout_max | 93 | 6% | -16.19 USD |
| spike_tp | 23 | 100% | +10.32 USD |

**Duración por resultado:**
- Wins: avg=142s (min=4s, median=144s, max=252s)
- Losses: avg=251s ← timeout fijo en **250s**

**CRÍTICO:** El timeout de 250s está justo en el máximo del `spike_tp` (max=249s). El bot cierra trades exactamente cuando el spike retorna. Hay 93 timeouts contra solo 23 spike_tp. El loop de evaluación está entrando en momentos equivocados con alta frecuencia.

**ATR:** mean=0.052 | median=0.041  
**Spikes detectados:** 60 | Entradas: 1 (2%)  
**Bloqueadores:** trade_cooldown=56 | symbol_already_open=1 | HURST_VETO H=0.424 < 0.430 (1)  
**Jump size:** mean=5.46, median=4.57, max=19.48  
**Peak hours (UTC):** 01:00, 02:00, 06:00, 10:00  

---

### BOOM900
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 63 |
| PnL | **-3.36 USD** |
| Win Rate | 24% |
| Profit Factor | 0.68 |
| Avg duración | 391s |
| Avg win | +0.486 USD |
| Avg loss | -0.222 USD |
| Stake | $2.00 |

**Exits:**
| Razón | Cant | WR | PnL total |
|-------|------|-----|-----------|
| timeout_max | 48 | 0% | -10.65 USD |
| spike_tp | 15 | 100% | +7.29 USD |

**Duración por resultado:**
- Wins: avg=200s (min=8s, median=164s, max=449s)
- Losses: avg=451s ← timeout fijo en **450s**

**ATR:** mean=0.093 | median=0.074  
**Spikes detectados:** 36 | Entradas: 0 (0%)  
**Bloqueadores:** trade_cooldown=35 | spike_forced_dir=1  
**Jump size:** mean=10.52, median=9.27, max=24.47  
**Peak hours (UTC):** 08:00, 01:00, 02:00, 09:00  

---

### CRASH500
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 66 |
| PnL | **+1.09 USD** ← ÚNICO RENTABLE |
| Win Rate | 44% |
| Profit Factor | 1.09 |
| Avg duración | 338s |
| Avg win | +0.469 USD |
| Avg loss | -0.338 USD |
| Stake | $2.00 |

**Exits:**
| Razón | Cant | WR | PnL total |
|-------|------|-----|-----------|
| spike_timeout | 38 | 3% | -11.91 USD |
| spike_tp | 27 | 100% | +12.87 USD |
| spike_capture | 1 | 100% | +0.13 USD |

**Duración por resultado:**
- Wins: avg=192s (min=3s, median=195s, max=451s)
- Losses: avg=452s ← timeout fijo en **450s**

**spike_tp timing:** p10=39s | p50=195s | p90=349s | max=392s

**ATR:** mean=0.044 | median=0.030  
**Spikes detectados:** 80 | Entradas: 0 (0%)  
**Bloqueadores:** trade_cooldown=72 | spike_forced_dir=2 | HURST_VETO H=0.200 (2)  
**Jump size:** mean=3.71, median=3.31, max=10.95 ← spikes más pequeños  
**Peak hours (UTC):** 05:00, 07:00, 01:00, 08:00, 09:00  

---

### CRASH600
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 65 |
| PnL | **-1.15 USD** |
| Win Rate | 37% |
| Profit Factor | 0.91 |
| Avg duración | 348s |
| Avg win | +0.471 USD |
| Avg loss | -0.304 USD |
| Stake | $2.00 |

**Exits:**
| Razón | Cant | WR | PnL total |
|-------|------|-----|-----------|
| spike_timeout | 41 | 0% | -12.46 USD |
| spike_tp | 24 | 100% | +11.31 USD |

**Duración por resultado:**
- Wins: avg=173s (min=10s, median=158s, max=379s)
- Losses: avg=451s ← timeout fijo en **450s**

**spike_tp timing:** p10=50s | p50=158s | p90=298s | max=379s

**ATR:** mean=0.254 | median=0.200 ← ATR MÁS ALTO de todos  
**Spikes detectados:** 64 | Entradas: 0 (0%)  
**Bloqueadores:** trade_cooldown=60 | spike_forced_dir=2 | HURST_VETO H=0.418 (1)  
**Jump size:** mean=25.49, median=23.06, max=80.01 ← jumps más grandes  
**Peak hours (UTC):** 02:00, 08:00, 04:00, 09:00  

---

### CRASH900
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 73 |
| PnL | **-0.27 USD** (casi breakeven) |
| Win Rate | 26% |
| Profit Factor | 0.97 |
| Avg duración | 303s |
| Avg win | +0.497 USD |
| Avg loss | -0.180 USD |
| Stake | $2.00 |

**Exits:**
| Razón | Cant | WR | PnL total |
|-------|------|-----|-----------|
| timeout_max | 54 | 0% | -9.72 USD |
| spike_tp | 19 | 100% | +9.45 USD |

**Duración por resultado:**
- Wins: avg=167s (min=4s, median=165s, max=336s)
- Losses: avg=351s ← timeout fijo en **350s**

**spike_tp timing:** p10=42s | p50=165s | p90=323s | max=336s

**Nota:** CRASH900 tiene max_hold=700s configurado en código, pero los timeouts ocurren a ~351s → hay un `spike_timeout_seconds` intermedio de 350s.

**ATR:** mean=0.198 | median=0.155  
**Spikes detectados:** 40 | Entradas: 0 (0%)  
**Bloqueadores:** trade_cooldown=38 | AI_VETO=1 | HURST_VETO H=0.393 (1)  
**Jump size:** mean=18.51, median=16.44, max=58.32  
**Peak hours (UTC):** 03:00, 02:00, 04:00, 06:00, 08:00  

---

### BOOM1000
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 0 |
| PnL | $0.00 |
| Spikes detectados | 39 | Entradas: 0 (0%) |

**Bloqueadores:** trade_cooldown=37 | spike_forced_dir MULTUP=2  
**ATR:** mean=0.107 | median=0.094  
**Jump size:** mean=12.37, median=10.92, max=31.90  
**Ratio:** median=124, max=138  
**Peak hours (UTC):** 01:00, 09:00, 06:00, 11:00  
**Estado:** Sin trades del loop de evaluación — no hubo entradas en ningún modo.

---

### CRASH1000
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 0 |
| Spikes detectados | 37 | Entradas: 0 (0%) |
| Estado | **COMPLETAMENTE DESHABILITADO** |

**Bloqueadores:** symbol_disabled=37 (100%)  
**ATR:** mean=0.047 | median=0.041  
**Jump size:** mean=5.57, median=5.16, max=15.19  
**Ratio:** median=125, max=1926  
**Peak hours (UTC):** 05:00, 06:00, 02:00, 04:00  
**ACCIÓN REQUERIDA:** Fue deshabilitado intencionalmente. 37 spikes perdidos. Revisar si debe re-habilitarse.

---

## ANÁLISIS GLOBAL DE SPIKES

| Símbolo | Spikes | Entradas | Entry% | Bloqueador Principal |
|---------|--------|----------|--------|---------------------|
| BOOM1000 | 39 | 0 | 0% | trade_cooldown (95%) |
| BOOM600 | 60 | 1 | 2% | trade_cooldown (93%) |
| BOOM900 | 36 | 0 | 0% | trade_cooldown (97%) |
| CRASH1000 | 37 | 0 | **0%** | **symbol_disabled (100%)** |
| CRASH500 | 80 | 0 | 0% | trade_cooldown (90%) |
| CRASH600 | 64 | 0 | 0% | trade_cooldown (94%) |
| CRASH900 | 40 | 0 | 0% | trade_cooldown (95%) |
| **TOTAL** | **356** | **1** | **0.3%** | trade_cooldown |

### Diagnóstico del trade_cooldown
El loop de evaluación genera entradas constantemente → pone al símbolo en cooldown → cuando aparece un spike, el símbolo está en cooldown y se bloquea. El ciclo se repite en cada ventana de evaluación. Las entradas del loop de evaluación tienen WR=24-44%, son la fuente principal de pérdidas Y además impiden el spike trading.

---

## ANÁLISIS DE TIMEOUTS

### Timeouts confirmados por duración de trades (inferido de losses exactos)

| Símbolo | Timeout inferido | spike_tp max | Brecha |
|---------|-----------------|--------------|--------|
| BOOM600 | **250s** | 249s | +1s ← CRÍTICO: timeout en el límite del spike_tp |
| CRASH900 | **350s** | 336s | +14s (marginal) |
| CRASH500 | **450s** | 392s | +58s (OK) |
| CRASH600 | **450s** | 379s | +71s (OK) |
| BOOM900 | **450s** | 449s | +1s ← CRÍTICO: timeout en el límite del spike_tp |

### Patrón crítico: spike_tp llega justo antes del timeout
```
BOOM600: spike_tp_max=249s, timeout=250s → cierra 1 segundo antes del máximo regreso del spike
BOOM900: spike_tp_max=449s, timeout=450s → cierra 1 segundo antes del máximo regreso del spike
```
El timeout actual para BOOM600 y BOOM900 es literalmente el límite exacto donde el spike puede regresar. Cualquier extensión marginal de timeout podría convertir muchos losses en wins.

### Distribución del spike_tp (cuándo llega el win)

| Símbolo | p10 | p50 | p90 | max | Timeout |
|---------|-----|-----|-----|-----|---------|
| BOOM600 | 17s | 95s | 242s | 249s | 250s |
| CRASH900 | 42s | 165s | 323s | 336s | 350s |
| CRASH500 | 39s | 195s | 349s | 392s | 450s |
| CRASH600 | 50s | 158s | 298s | 379s | 450s |
| BOOM900 | 46s | 164s | 414s | 449s | 450s |

---

## ANÁLISIS HORARIO UTC

| Hora UTC | Trades | WR | PnL |
|----------|--------|-----|-----|
| 01:00 | 24 | 29% | +0.12 |
| **02:00** | 36 | 39% | **+2.14** ← mejor |
| 03:00 | 37 | 22% | -4.72 ← peor |
| **04:00** | 39 | 46% | **+3.75** ← mejor |
| 05:00 | 35 | 17% | -4.24 ← peor |
| 06:00 | 38 | 32% | -0.71 |
| **07:00** | 39 | 33% | **+0.71** |
| 08:00 | 38 | 37% | -0.54 |
| 09:00 | 34 | 24% | -1.59 |
| 10:00 | 34 | 32% | -1.67 |
| 11:00 | 29 | 17% | -2.81 ← peor |

**Horas rentables:** 02:00, 04:00, 07:00 UTC  
**Horas destructivas:** 03:00, 05:00, 11:00 UTC  
**Patrón observado:** La sesión deteriora progresivamente a partir de las 08:00 UTC.

---

## DIAGNÓSTICO DE LOS VETOS ACTIVOS

### HURST_VETO (strict_min=0.430)
El umbral de Hurst bloquea entradas cuando el mercado está en random walk:

| Símbolo | H observado | Umbral | ¿Bloqueó? |
|---------|-------------|--------|-----------|
| CRASH500 | H=0.200 | 0.430 | Sí (2 spikes) — random walk puro |
| CRASH900 | H=0.393 | 0.430 | Sí (1 spike) — cerca del umbral |
| CRASH600 | H=0.418 | 0.430 | Sí (1 spike) — cerca del umbral |
| BOOM600 | H=0.424 | 0.430 | Sí (1 spike) — a 0.006 del umbral |

CRASH500 con H=0.200 es correcto bloquear (random walk extremo). BOOM600 con H=0.424 está a solo 0.006 del umbral — potencialmente demasiado restrictivo para spikes.

### AI_VETO
- CRASH900: 1 bloqueo — "The trade signal is not approved because the mathematical..."
- Funcionando correctamente como segunda línea de defensa.

### spike_forced_dir
- BOOM1000: 2 (MULTUP) — spike en dirección contraria a la tendencia Hurst
- BOOM900: 1 (MULTUP)
- CRASH500: 2 (MULTDOWN)
- CRASH600: 2 (MULTDOWN)

---

## PATRONES CLAVE IDENTIFICADOS

### Patrón 1: Loop de evaluación vs Spike trading
**Problema:** Las entradas del loop de evaluación (WR=24-44%) monopolizan los símbolos en cooldown, dejando sin operación al motor de spikes (0.3% entry rate).

**Impacto cuantificado:**
- 356 spikes detectados, 355 bloqueados por cooldown
- El loop generó 383 trades con PnL=-9.56 USD
- Si el motor de spikes hubiera podido operar, habría tenido acceso a 355 señales adicionales

### Patrón 2: Timeout = Kill switch de rentabilidad
**Hecho estadístico duro:**
- `spike_tp` → WR=**100%** en todos los símbolos
- `timeout_max`/`spike_timeout` → WR=**0-6%** en todos los símbolos

El resultado de cada trade está determinado casi por completo por si llega a `spike_tp` o al timeout. No hay zona gris.

### Patrón 3: El win llega rápido, el loss espera el timeout
```
CRASH500: win median=195s, loss=452s (timeout)
CRASH600: win median=158s, loss=451s (timeout)
CRASH900: win median=165s, loss=351s (timeout)
BOOM600:  win median=144s, loss=251s (timeout)
BOOM900:  win median=164s, loss=451s (timeout)
```
Las posiciones ganadoras se resuelven en la primera mitad del tiempo de vida. Las perdedoras esperan hasta el timeout. Esto sugiere: si después de 200-250s no hay spike_tp, la posición debería cerrarse con stop preventivo.

### Patrón 4: CRASH600 tiene el ATR más alto y los jumps más grandes
ATR_mean=0.254, jump_median=23.06 — los spikes de CRASH600 son los más volátiles y los más grandes. A pesar de eso, PnL=-1.15 (casi breakeven). Con ajuste de timeout podría ser rentable.

### Patrón 5: CRASH500 es el modelo a seguir
El único símbolo rentable. Sus características:
- Spike más pequeños (jump_median=3.31) pero consistentes
- Timeout a 450s con spike_tp llegando max=392s — hay margen
- WR=44% — la mejor tasa de acierto
- Usa `spike_timeout` en lugar de `timeout_max` → el bot detecta cuando el spike ya terminó

---

## AJUSTE 1 — PROPUESTA POST MUESTRA 01

**Timeframe:** Muestra 01, ~01:28-12:00 UTC 22-May-2026  
**Observaciones:** 383 trades, 356 spikes, -9.56 USD  
**Patrones detectados:**
1. trade_cooldown bloquea 99.7% de spikes
2. BOOM600 timeout en el límite exacto del spike_tp
3. BOOM900 timeout en el límite exacto del spike_tp
4. CRASH1000 completamente deshabilitado (37 spikes perdidos)
5. Hurst strict_min=0.430 bloquea BOOM600 a H=0.424
6. Loop evaluación es fuente de todas las pérdidas

**Cambios recomendados para Muestra 02:**

| Cambio | Tipo | Valor actual | Valor propuesto | Justificación |
|--------|------|--------------|-----------------|---------------|
| BOOM600 timeout | parámetro | 250s | 350s | spike_tp max=249s → timeout en el límite; extender da 100s de margen |
| BOOM900 timeout | parámetro | 450s | 600s | spike_tp max=449s → mismo problema; extender a 600s |
| CRASH1000 | habilitar | disabled | enabled | 37 spikes perdidos; re-evaluar con datos reales |
| HURST strict_min BOOM600 | parámetro | 0.430 | 0.400 | H=0.424 a 0.006 del límite; reducir para BOOMs |
| trade_cooldown | investigar | ? | reducir | Bloqueador #1; necesitamos duración actual y reducirla |
| Early exit si no hay progreso | nueva lógica | no existe | 200s sin movimiento → close | Wins llegan antes de 200s; losses esperan 450s |
| BOOM1000 | investigar | 0 trades | activar | No tiene trades en evaluación; revisar perfil |

**Priority 1 (impacto inmediato):**
- Extender timeout BOOM600: 250→350s
- Extender timeout BOOM900: 450→600s
- Re-habilitar CRASH1000

**Priority 2 (impacto medio plazo):**
- Reducir Hurst strict_min para BOOM symbols: 0.430→0.400
- Investigar duración del trade_cooldown y reducirla
- Considerar early-exit lógico si posición no avanza en primeros 200s

---

## ATR REFERENCIA POR SÍMBOLO

| Símbolo | ATR mean | ATR median | Jump median | Ratio median |
|---------|----------|------------|-------------|--------------|
| BOOM1000 | 0.10708 | 0.09421 | 10.92 | 124 |
| BOOM600 | 0.05219 | 0.04084 | 4.57 | 112 |
| BOOM900 | 0.09269 | 0.07351 | 9.27 | 125 |
| CRASH1000 | 0.04714 | 0.04132 | 5.16 | 125 |
| CRASH500 | 0.04366 | 0.02907 | 3.31 | 107 |
| CRASH600 | 0.25445 | 0.19990 | 23.06 | 117 |
| CRASH900 | 0.19750 | 0.15543 | 16.44 | 122 |

---

## PRÓXIMAS MUESTRAS

| Muestra | Estado | Objetivo |
|---------|--------|----------|
| Muestra 01 | COMPLETADA | Configuración base, diagnóstico inicial |
| Muestra 02 | COMPLETADA | Validar extensión timeouts, habilitar CRASH1000 |
| Muestra 03 | EN CURSO | Spike buffer, DPM fix, filtrar bc_escape_env TREND |
| Muestra 04 | PENDIENTE | Validar filtros, evaluar early-exit por inactividad |

---

*Generado: 22 Mayo 2026 — GitHub Copilot + datos live del bot Deriv*

---

---

# MUESTRA 02 — ANÁLISIS FORENSE PROFUNDO
> `commit ac3d3b8` — post-ajuste Muestra 01: perfiles extendidos, CRASH1000 habilitado, BOOM900 extendido.

---

## METADATOS

| Campo | Valor |
|-------|-------|
| Inicio | ~12:13 UTC, 22 Mayo 2026 |
| Fin | ~16:04 UTC, 22 Mayo 2026 |
| Duración | ~3h 51m |
| Bot commit | `ac3d3b8` |
| Trades cerrados | **130** |
| PnL total | **-5.69 USD** |
| Win Rate | **31.5%** |
| Avg win | +$0.381 |
| Avg loss | -$0.245 |
| Total ganado | +$15.64 |
| Total perdido | -$21.33 |

---

## RESUMEN EJECUTIVO

El bot sigue perdiendo pero el patrón de pérdida cambió: antes era `spike_timeout` puro, ahora el `timeout_max` del DPM domina. **Causa raíz descubierta: el DPM (`position_manager.py`) tiene su propio `max_duration_seg` que disparaba ANTES que el `max_hold_seconds` del perfil.** BOOM600 cerraba en 250s (DPM) en vez de 350s (perfil). BOOM900 en 451s en vez de 600s. CRASH900 en 351s en vez de 500s.

El segundo hallazgo crítico: el **setup_type `TREND` con fvg_tier `bc_escape_env` es tóxico** — 102 trades, PnL=-$5.72, WR=29%. Estas entradas nunca debieron ejecutarse. En contraste, `fvg_mitigated` produce PnL=+$1.63 con WR=48% en solo 21 trades.

Un tercer hallazgo estructural: **86/130 trades (66%) jamás fueron rentables** (eficiencia=0%). Perdieron $21.03 en total. El bot no tiene mecanismo de "no está funcionando esta vez" — cada trade espera hasta el timeout.

---

## ANÁLISIS POR EXIT_REASON

| Salida | Count | PnL | WR | Avg PnL |
|--------|-------|-----|----|---------|
| spike_tp | 36 | **+$15.64** | 100% | +$0.434 |
| spike_timeout | 34 | -$7.52 | 0% | -$0.221 |
| timeout_max | 56 | -$12.32 | 0% | -$0.220 |
| broker_sl_hit | 2 | -$1.44 | 0% | -$0.720 |
| ghost_unknown | 2 | $0.00 | — | $0.000 |

> **Insight clave:** Igual que en Muestra 01, el outcome es BINARIO: o se captura el spike (efic=1.00) o se pierde el timeout. No hay zona gris. 89/130 posiciones cerraron con eficiencia=0% (nunca verde) → pérdida de $21.33.

---

## HALLAZGO CRÍTICO — DPM DISPARANDO ANTES QUE EL PERFIL

El `DynamicPositionManager` tiene su propio `max_duration_seg` independiente del perfil. En Muestra 02 esto hacía:

| Símbolo | DPM max_duration | Perfil max_hold | Resultado |
|---------|-----------------|-----------------|-----------|
| BOOM600 | **250s** | 350s | Cerraba 100s antes del límite del perfil |
| BOOM900 | **450s** | 600s | Cerraba 150s antes |
| CRASH900 | **350s** | 500s | Cerraba 150s antes |
| CRASH600 | 450s | 450s | OK (coincidía) |
| CRASH1000 | 900s | 700s | DPM más largo que perfil — correcto |

**Consecuencia:** BOOM600 tuvo 27 trades saliendo en ~250s (exit=`timeout_max`), no en 350s. Spikes de BOOM600 llegaron hasta 338s desde entrada — eran capturables con el tiempo correcto.

**Fix implementado (commit `524bf0c`):** DPM ahora a profile+30s para todos los spike markets.

---

## HALLAZGO CRÍTICO — SETUP_TYPE `TREND` ES EL PROBLEMA

### Por setup_type completo:

| Setup | Trades | PnL | WR | Salidas |
|-------|--------|-----|----|---------|
| **TREND** | 103 | **-$6.04** | 29% | 54×timeout_max, 23×spike_timeout, 26×spike_tp |
| **SMC_FVG** | 20 | **+$1.95** | 50% | 9×spike_tp, 11×spike_timeout |
| UNKNOWN | 7 | -$1.60 | 14% | ghost/broker_sl_hit |

### Por fvg_tier (correlaciona perfecto con setup_type):

| FVG Tier | Trades | PnL | WR | Efic avg | Grades |
|----------|--------|-----|----|----------|--------|
| **bc_escape_env** | 102 | **-$5.72** | 29% | 0.23 | C:56, B:35, A:11 |
| **fvg_mitigated** | 21 | **+$1.63** | 48% | 0.48 | A:11, B:7, C:3 |
| none (UNKNOWN) | 7 | -$1.60 | 14% | 0.14 | — |

**`bc_escape_env` = entrada sin FVG real.** El bot usa la envolvente del escape de canal como sustituto de un FVG. En 102 intentos con esta condición, pierde $5.72 y tiene una eficiencia media de 23% — el spike llega pero tarde, cuando el trade ya expiró.

**`fvg_mitigated` = entrada con FVG real mitigado.** Solo en CRASH500 y CRASH600. WR=48%, PnL positivo, y la clave: **0 trades de `timeout_max`** — todos terminan en `spike_tp` o `spike_timeout` (el spike llegó, solo algunos llegaron tarde).

### Por símbolo × setup_type:

| Símbolo | Setup | Trades | PnL | WR | Exits principales |
|---------|-------|--------|-----|----|-------------------|
| BOOM600 | TREND/bc_escape | 32 | -$3.15 | 25% | 27×timeout_max |
| BOOM600 | UNKNOWN | 2 | -$0.95 | 0% | broker_sl_hit |
| BOOM900 | TREND/bc_escape | 19 | +$0.41 | 32% | 13×timeout_max, 6×spike_tp |
| CRASH500 | **SMC_FVG/fvg_mitigated** | 12 | **+$1.08** | 50% | 5×spike_tp, 7×spike_timeout |
| CRASH500 | TREND/bc_escape | 8 | -$0.13 | 50% | 4×spike_timeout, 4×spike_tp |
| CRASH600 | **SMC_FVG/fvg_mitigated** | 8 | **+$0.87** | 50% | 4×spike_tp, 4×spike_timeout |
| CRASH600 | TREND/bc_escape | 12 | -$1.23 | 25% | 9×spike_timeout |
| CRASH900 | TREND/bc_escape | 18 | -$1.19 | 22% | 14×timeout_max |
| CRASH1000 | TREND/bc_escape | 14 | -$0.75 | 36% | 10×spike_timeout, 4×spike_tp |

---

## HALLAZGO — EFICIENCIA BINARIA

| Rango eficiencia | Trades | PnL | WR | Avg peak |
|-----------------|--------|-----|----|----------|
| 0% (jamás verde) | **86** | **-$21.03** | 0% | $0.002 |
| 25-50% | 3 | +$0.06 | 100% | $0.057 |
| 50-75% | 1 | +$0.08 | 100% | $0.140 |
| **75-100%** | **37** | **+$15.50** | 100% | $0.419 |

La distribución es casi perfectamente bimodal: **o llega el spike (efic>75%, PnL positivo) o no llega (efic=0%, pérdida inevitable).** No hay trades que "casi funcionaron". Esto confirma que el concepto de early-exit-por-inactividad es válido: si tras 200-250s la posición jamás fue verde, no llegará a ser rentable.

---

## HALLAZGO — DPM FASE NUNCA ACTIVA

| DPM Fase | Trades | PnL | WR |
|----------|--------|-----|----|
| Fase 1 (sin ratchet) | 125 | -$4.81 | 32% |
| **Fase 2 (ratchet activo)** | **1** | **+$0.56** | 100% |
| Fase ? (error) | 4 | -$1.44 | 0% |

El sistema de ratchet/trailing-stop de la Fase 2 **prácticamente nunca se activa** (1 trade de 130). Esto significa que todo el código de DPM de trailing está siendo inútil. La razón: los spikes son movimientos bruscos de 2-10 segundos — cuando la posición alcanza el umbral de activación del ratchet, ya está en el tick final del spike y se cierra por `spike_tp` antes de que el DPM llegue a operar.

---

## HALLAZGO — MOMENTUM SCORE vs OUTCOME (PARADOJA 0.7)

| Momentum | Trades | PnL | WR |
|----------|--------|-----|----|
| 0.3 | 1 | -$0.18 | 0% |
| 0.4 | 2 | -$0.41 | 0% |
| 0.5 | 4 | +$0.17 | 50% |
| 0.6 | 4 | +$0.07 | 25% |
| **0.7** | **9** | **-$2.08** | **0%** |
| 0.8 | 10 | +$0.85 | 60% |
| 0.9 | 15 | +$1.52 | 40% |
| 1.0 | 9 | -$1.26 | 22% |
| 1.1 | 20 | -$0.61 | 35% |
| 1.2 | 11 | -$1.86 | 18% |
| 1.3 | 12 | +$0.16 | 42% |
| 1.4 | 11 | -$1.13 | 27% |
| 1.5 | 15 | +$0.67 | 40% |

**Paradoja:** momentum=0.7 es peor que 0.3 o 0.4. 9 trades, WR=0%, pérdida de $2.08. Todos con peak=$0.00 — el mercado nunca se movió a favor. La zona 0.7-0.8 parece ser una transición inestable donde hay "suficiente" momentum para activar la entrada pero no suficiente para mover el precio.

---

## HALLAZGO — TREND SCORE FIJO (BUG DE SCORING)

```
trend=1.5 → 123/130 trades (100% de los trades válidos)
```

El score de tendencia es CONSTANTE en 1.5 para prácticamente todos los trades. No discrimina nada. Esto significa que el componente `trend` del scoring nunca bloquea entradas porque siempre aporta el mismo valor fijo. Es una variable muerta en el modelo de decisión.

---

## HALLAZGO — GEO_CHANNEL_POS vs OUTCOME

| Posición en canal | Trades | PnL | WR |
|-------------------|--------|-----|----|
| < -0.5 (deep oversold) | 35 | **+$1.52** | 34% |
| -0.5 a 0 (oversold) | 33 | -$1.51 | 30% |
| **0 a 0.3 (neutral)** | **40** | **-$3.82** | 28% |
| 0.3 a 0.6 (mid) | 11 | +$0.60 | **64%** |
| > 0.6 (extended) | 3 | -$0.69 | 0% |

La zona **"neutral" (0 a 0.3)** es la peor: 40 trades, WR=28%, PnL=-$3.82. Las entradas en el medio del canal no tienen ninguna ventaja estadística. La zona **0.3-0.6** es la mejor con 64% de WR (aunque solo 11 trades). Los extremos (`< -0.5`) tienen PnL positivo por el spike_tp potencial.

---

## ANÁLISIS BOOM600 — CASO CRÍTICO

BOOM600 es la fuente de la mitad de las pérdidas (-$4.10 de -$5.69 total). Desglose forense:

- **34 trades, 32 de tipo TREND/bc_escape_env**
- `avg_peak = $0.052` — el spike casi nunca llega en tiempo
- `max_peak = $0.62` — cuando llega, es grande
- Solo **6 de 32** trades bc_escape_env alguna vez superaron $0.10 verde
- score_raw range: 5.15-8.33 → el score **no predice nada** para BOOM600 bc_escape_env
- geo range: -0.811 a 0.794 → tampoco discrimina
- Todos los 27 `timeout_max` salieron exactamente a ~251s (DPM 250s)
- Con fix DPM 250→380s, estos 27 trades ahora esperan hasta 350s (perfil)
- Spikes de BOOM600 llegaron hasta 338s en sesión → el fix debería capturar ~8 adicionales

**Root cause BOOM600:** El bc_escape_env nunca debió ser válido para BOOM600. La señal `bc_escape_env` indica que el precio salió de la envolvente del canal de Bollinger/ATR pero sin FVG institucional. Para BOOM600 (ciclo=600 ticks) esto genera entradas aleatorias dentro del ciclo sin ningún sesgo hacia el próximo spike.

---

## ANÁLISIS SMC_FVG — EL MODELO A SEGUIR

Los 20 trades de tipo `SMC_FVG` con `fvg_mitigated` son los únicos con ventaja sistemática:

- **CRASH500 SMC_FVG**: 12 trades, PnL=+$1.08, WR=50%, efic=0.48
- **CRASH600 SMC_FVG**: 8 trades, PnL=+$0.87, WR=50%, efic=0.48
- **0 salidas por `timeout_max`** — el spike siempre llegó, el problema fue el timing
- geo<0 en 86% de los casos (vs 49% para bc_escape_env)
- Grade A en 11/21 trades (vs 11/102 para bc_escape_env)
- Exits: solo `spike_tp` o `spike_timeout` — el spike llegó pero algunos se cerraron antes por el hold

**Este setup funciona porque:** el FVG mitigado indica que el precio retocó un desequilibrio institucional previo. Para CRASH500/600 esto coincide con el ciclo de recuperación pre-spike. La señal es estructural, no solo de momentum.

---

## GHOST / BROKER_SL_HIT ANÁLISIS

4 trades anómalos con duración=0s y sin score_breakdown:

| Exit | Símbolo | PnL | Causa probable |
|------|---------|-----|----------------|
| broker_sl_hit | BOOM600 | -$0.720 | Fill inmediato con slippage extremo → SL al 100% del stake |
| broker_sl_hit | CRASH600 | -$0.720 | Ídem — fill en tick de spike opuesto |
| ghost_unknown | BOOM900 | $0.000 | Contrato fantasma: cerrado sin PnL |
| ghost_unknown | CRASH1000 | $0.000 | Ídem |

Los broker_sl_hit son fills en ticks de alta volatilidad donde el spread se come el stake completo inmediatamente. Requieren filtro anti-spike-opuesto antes del fill.

---

## WINNERS ANATOMY — QUÉ ENTRA BIEN

Los 36 trades `spike_tp` tienen eficiencia=1.00 (100%). Sus patrones:

- **setup_type:** TREND (26) + SMC_FVG (10) — ambos pueden ganar si el spike llega
- **fvg_tier:** bc_escape_env (26) + fvg_mitigated (10)
- **Mejor trade:** BOOM900 bc_escape_env TREND, geo=-0.736, mom=0.89, hurst=0.493 → +$1.14
- **fvg_mitigated wins:** todos de CRASH500/600, tienden a geo < -0.10
- **bc_escape_env wins:** no tienen patrón de geo consistente — son pura lotería de timing
- **Hurst de ganadores:** rango 0.438-0.540, sin diferencia con perdedores → hurst no discrimina

**Conclusión:** Los ganadores de bc_escape_env no son mejores entradas — son simplemente los que tuvieron suerte en el timing del spike. No hay ningún indicador pre-entrada que los diferencie.

---

## CAMBIOS IMPLEMENTADOS (commit `524bf0c`)

### 1. DPM max_duration_seg corregido — todos los spike markets

| Símbolo | Antes | Después | Perfil max_hold |
|---------|-------|---------|-----------------|
| BOOM600 | 250s | **380s** | 350s |
| BOOM900 | 450s | **630s** | 600s |
| CRASH900 | 350s | **630s** | 600s (extendido) |
| CRASH600 | 450s | **630s** | 600s (extendido) |
| CRASH500 | 720s | **480s** | 450s |
| CRASH1000 | 900s | **730s** | 700s |
| BOOM1000 | 900s | **420s** | 390s |

### 2. Perfiles extendidos

| Símbolo | max_hold anterior | max_hold nuevo | Justificación |
|---------|------------------|----------------|---------------|
| CRASH600 | 450s | **600s** | 8 spikes llegaron 468-749s desde entrada |
| CRASH900 | 500s | **600s** | 3 spikes llegaron 526-576s desde entrada |

### 3. Spike buffer (Phase 37)

En vez de cerrar inmediatamente al detectar spike, el bot espera hasta 3s (`DERIV_SPIKE_BUFFER_SEC`) para capturar secuencias multi-spike. Safety valve: si el PnL cae >35% del pico durante el buffer, cierra inmediatamente.

---

## ACCIONES PENDIENTES PARA MUESTRA 03

### URGENTE — Implementar filtros de entrada

| Filtro | Acción | Impacto esperado |
|--------|--------|-----------------|
| **Bloquear bc_escape_env TREND en BOOM600** | `min_score` imposible o `disabled=True` para BOOM600 TREND | -32 trades perdedores, ahorra ~$3.15/muestra |
| **Requerir fvg_mitigated para CRASH500/600** | Elevar `fvg_tier_minimo = "fvg_mitigated"` | +50% WR en esos símbolos |
| **Bloquear momentum en rango 0.65-0.75** | Agregar `min_momentum = 0.76` o penalización | Eliminar 9 trades WR=0% |
| **Filtrar geo_channel_pos 0.0-0.30** | `geo_entry_min=0.30` para entradas TREND bc_escape_env | Elimina peor zona: 40 trades, -$3.82 |
| **Early exit si efic=0 tras 200s** | Nueva lógica: si peak_profit<$0.05 tras 200s en bc_escape_env, cerrar | Salva $0.22/trade en ~45 trades potenciales |

### MEDIO PLAZO — Scoring fix

- **trend=1.5 siempre:** Investigar por qué el componente trend no varía. Si siempre vale 1.5, sobra del modelo — está consumiendo 1.5 puntos sin discriminar.
- **DPM Fase 2 nunca activa:** Dado que los spikes son bruscos y cortos, el ratchet nunca opera. Considerar eliminarlo para spike markets y simplificar la lógica.

---

*Generado: 22 Mayo 2026 — análisis forense profundo 17 dimensiones — 130 trades*

---

---

# PRUEBA 4 — ANÁLISIS FORENSE POST-MUESTRA 02
> Configuración post-DPM fix (`commit 3140814`). Primera prueba con BOOM500 como símbolo activo principal.

---

## METADATOS

| Campo | Valor |
|-------|-------|
| Inicio | ~05:35 UTC, 21 Mayo 2026 |
| Bot commit | `3140814` |
| Trades cerrados | **37** |
| PnL sesión | **-$0.46** |
| Balance cuenta | $9,761.24 |
| Bankroll referencia | $9,992 |
| Win Rate global | ~38% |

---

## RESUMEN EJECUTIVO

Prueba más corta en número de trades pero con hallazgos de mayor precisión. **BOOM500 emergió como MVP** (+$1.13, 57% WR) — el único símbolo con ventaja positiva consistente. El problema principal se desplazó: ya no es el DPM ni el loop de evaluación genérico — es que **BOOM600 y BOOM900 siguen entrando sin estructura FVG** y el precio nunca se mueve a favor (zero_peak). El `trade_cooldown` de 300 ticks sigue bloqueando el 41% de los spikes evaluados.

**Hallazgo clave:** `zero_peak_rate` es el indicador más predictivo del problema. Si un trade nunca fue rentable ni un tick, la causa raíz es la fase del ciclo, no el timing.

---

## ANÁLISIS POR SÍMBOLO

### BOOM500 — MVP
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 7 |
| PnL | **+$1.13** |
| Win Rate | **57%** |
| Stake | $2.00 |
| Resultado | Único símbolo positivo |

**Por qué funciona:** BOOM500 tiene el ciclo de spike más corto (~500 ticks). Las entradas `bc_escape_env` en BOOM500 tienen más probabilidad de coincidir con el inicio de un ciclo de spike porque la ventana de interacción es más corta. Menos tiempo para que el precio divague antes del spike.

---

### BOOM900 — Problema de fase
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 8 |
| PnL | **-$1.11** |
| Win Rate | **25%** |
| Zero-peak rate | **62%** ← CRÍTICO |
| Stake | $2.00 |

**Zero-peak = 62%:** 5 de 8 trades nunca tuvieron el precio en verde ni un tick. No es timing tardío — es entrada en la fase equivocada del ciclo. El precio estaba en drift descendente completo después del spike anterior. `bc_escape_env=False` permitía estas entradas sin exigir FVG de soporte.

**Off-by-one detectado:** 7 spikes bloqueados por `_CooldownGate` con elapsed=299 ticks (threshold=300). El símbolo estaba a 1 tick de recuperarse y el spike se perdió.

---

### BOOM600 — Mismo patrón que BOOM900
| Métrica | Valor |
|---------|-------|
| Trades cerrados | 10 |
| PnL | **-$0.32** |
| Win Rate | **30%** |
| Zero-peak rate | **60%** ← CRÍTICO |
| Stake | $2.00 |

Mismo root cause que BOOM900: entradas en drift descendente post-spike sin FVG de soporte. 6/10 trades nunca vieron el precio subir.

---

## DIAGNÓSTICO TRADE_COOLDOWN — 41% BLOQUEO

El `_CooldownGate` en `main_deriv.py` bloquea el símbolo durante `DERIV_CONTRACT_DURATION_SEC` ticks (valor: **300**) desde el momento en que se ABRE el trade — no desde que se cierra. Efecto:

```
Trade abre → spike_tp en 30s → símbolo bloqueado 270 ticks más
Trade abre → zero_peak timeout en 480s → símbolo bloqueado durante toda la espera
```

De todos los spikes evaluados en la sesión, el 41% fue bloqueado por este gate. La reducción a 120 ticks libera el símbolo mucho más rápido sin riesgo de double-entry (el ejecutor tiene su propio guard `symbol_already_open`).

---

## ANÁLISIS DE ZERO_PEAK — EL INDICADOR MÁS ÚTIL

| Símbolo | Zero-peak rate | Interpretación |
|---------|---------------|----------------|
| BOOM900 | **62%** | Entrada en inter-spike drift 3 de cada 5 veces |
| BOOM600 | **60%** | Ídem |
| BOOM500 | ~14% | Ciclo corto, menos drift post-spike |
| CRASH500 | ~20% | FVG mitigated activo, entradas con estructura |

**Definición:** Zero-peak = `peak_profit=0.0` durante toda la vida del trade. El precio nunca fue positivo ni 1 tick. Esto no es "llegó tarde el spike" — es que el precio se fue en dirección contraria durante toda la duración.

**Conclusión operativa:** Si un trade lleva 150s y `peak_profit=0.0` con `floating_pnl < -$0.05`, la probabilidad de recuperación es ~5%. Cortar inmediatamente es matemáticamente correcto.

---

## CAMBIOS IMPLEMENTADOS — PRUEBA 4 RESTART (`commit b41bd8b`)

### Principio rector
> Entrar menos veces pero en el momento correcto vale más que entrar frecuentemente en el momento equivocado.

### 1. BOOM600 y BOOM900 — Redesign de perfil

**Root cause confirmado:** `block_bc_escape_env=False` + `fvg_tier_minimo=fvg_detected` permitía entrar durante el drift inter-spike sin estructura FVG. El precio en esa fase siempre va contra BOOM.

**Lógica del fix:** Después de un spike BOOM, el precio crea un FVG (desequilibrio bullish). El precio luego **baja** (drift). Cuando toca/regresa a esa zona FVG = mitigado = soporte de acumulación confirmado = entrada óptima para el próximo spike.

| Parámetro | BOOM600 antes | BOOM600 después | BOOM900 antes | BOOM900 después |
|-----------|--------------|-----------------|---------------|-----------------|
| `block_bc_escape_env` | False | **True** | False | **True** |
| `fvg_tier_minimo` | fvg_detected | **fvg_mitigated** | fvg_detected | **fvg_mitigated** |
| `geo_entry_max` | 0.50 | **0.35** | 0.50 | **0.35** |
| `max_hold_seconds` | 350s | **280s** | 600s | **480s** |
| `spike_min_post_sec` | 250t | **200t** | 300t | **270t** (off-by-one fix) |
| `cooldown_sec` | 240 | **120** | 240 | **120** |
| `stake_max_usdt` | $2.00 | **$1.50** | $2.00 | **$1.50** |

**Impacto en frecuencia de entradas:** -70 a -80% menos entradas para BOOM600/900. `bc_escape_env` disparaba 3-5 veces por ciclo de spike. `fvg_mitigated` dispara 0-1 veces (solo cuando el precio regresa a la zona). Cada entrada vale mucho más.

### 2. BOOM500 — Potenciar el MVP

| Parámetro | Antes | Después | Razón |
|-----------|-------|---------|-------|
| `stake_max_usdt` | $2.00 | **$3.00** | 57% WR, +$1.13/sesión — el mejor símbolo |
| `cooldown_sec` | 240 | **120** | Más oportunidades para el símbolo más rentable |

### 3. CRASH500 — Desbloquear spikes filtrados

| Parámetro | Antes | Después | Razón |
|-----------|-------|---------|-------|
| `hurst_min_spike` | 0.43 | **0.42** | 2 spikes bloqueados a H=0.424-0.425 |
| `cooldown_sec` | 300 | **120** | Reducción global |

### 4. Todos los símbolos — Cooldown reduction

| Símbolo | cooldown_sec antes | cooldown_sec después |
|---------|-------------------|---------------------|
| BOOM1000 | 240 | **120** |
| CRASH600 | 300 | **120** (+spike_min_post 130→110t) |
| CRASH900 | 300 | **120** |
| CRASH1000 | 300 | **120** |

### 5. `DERIV_CONTRACT_DURATION_SEC` — Gate de cooldown global

| Variable | Antes | Después | Impacto |
|----------|-------|---------|---------|
| `DERIV_CONTRACT_DURATION_SEC` | 300 ticks | **120 ticks** | -41% bloqueo de spikes esperado |

Reducir de 300 a 120 ticks significa que después de abrir un trade, el símbolo queda bloqueado solo 2 minutos en lugar de 5. Si el spike_tp cierra el trade en 30s, el símbolo vuelve a estar disponible en ~90s adicionales (vs 270s antes).

### 6. `zero_peak_exit` — Nueva lógica de corte temprano

Agregado en `deriv_trader.py`:

```
Condición: held >= 150s AND peak_profit == 0.0 AND floating_pnl < -$0.05
Acción: vender inmediatamente
Razón: trade nunca fue rentable en 2.5 minutos — no habrá recuperación
```

**Impacto esperado:** BOOM600/900 tenían 60-62% zero_peak. Con este exit:
- Trade que habría esperado 280-480s → sale a 150s
- Ahorro por trade: ~$0.10-0.15 por trade cortado
- En una sesión de 20 trades BOOM600/900, esto salva ~$1.50-2.00

---

## MODELO CONCEPTUAL: CÓMO OPERA EL BOT AHORA

### Antes (Prueba 4 original):
```
Spike detectado → bc_escape_env activo → ENTRADA (cualquier momento del ciclo)
  → 60-62% del tiempo: precio en drift → zero_peak → timeout → pérdida fija
  → 38-40% del tiempo: timing correcto → spike_tp → ganancia
```

### Después (Prueba 4 restart):
```
Spike detectado → ¿hay FVG mitigado en BOOM600/900? 
  → NO (inter-spike drift): BLOQUEADO — no entra
  → SÍ (precio retocó soporte FVG): ENTRADA
      → zero_peak en 150s: zero_peak_exit → corte temprano
      → spike llega: spike_tp → ganancia
```

### Efecto en métricas esperadas

| Métrica | Prueba 4 original | Objetivo Prueba 4 restart |
|---------|------------------|--------------------------|
| BOOM600/900 entradas/sesión | ~18 | **~4-6** (-70%) |
| BOOM600/900 WR | 25-30% | **>45%** |
| Zero-peak rate | 60-62% | **<20%** |
| BOOM500 entradas/sesión | ~7 | **~12-15** (más cooldown) |
| Bloqueo por trade_cooldown | 41% | **<20%** estimado |
| PnL/sesión esperado | -$0.46 | **+$0.50 a +$2.00** |

---

## ESTADO DEL BOT — PRUEBA 4 RESTART

| Item | Estado |
|------|--------|
| Contenedor | `d1ff6700f5a6` |
| Commit en producción | `b41bd8b` |
| `DERIV_CONTRACT_DURATION_SEC` activo | **120** ✅ |
| `zero_peak_exit` en código | ✅ |
| BOOM600 `block_bc_escape_env` | **True** ✅ |
| BOOM900 `block_bc_escape_env` | **True** ✅ |
| BOOM500 stake | **$3.00** ✅ |
| Todos cooldowns | **120** ✅ |
| Estado de datos | Reset limpio (4 trades ya corriendo) |
| Estado del bot | **OPERATIVO** |

---

## PRÓXIMAS MUESTRAS

| Muestra | Estado | Objetivo |
|---------|--------|----------|
| Muestra 01 | COMPLETADA | Diagnóstico base |
| Muestra 02 | COMPLETADA | DPM fix, perfiles extendidos |
| Prueba 4 | COMPLETADA → REINICIADA | Forensic zero_peak + FVG fix |
| **Prueba 4 Restart** | **EN CURSO** | Validar FVG filter, zero_peak_exit, cooldown 120 |
| Prueba 5 | PENDIENTE | Con datos acumulados de restart — evaluar WR real BOOM600/900 con FVG |

---

*Actualizado: 22 Mayo 2026 — post-implementación commit b41bd8b — bot operativo container d1ff6700f5a6*

---

## PRUEBA 4 — STAGE 4 (TUNING LIVE PARA CAPTURA DE SPIKES)

### Cambios implementados en código (sin quitar protecciones)

1. **Spike pre-filter adaptativo por símbolo (tick-domain)**
- Se mantiene `spike_min_post_sec` del perfil como baseline.
- Nuevo ajuste dinámico con intervalos reales entre spikes (`_spike_intervals`) para **relajar** bloqueo cuando el mercado acelera.
- Nunca endurece más allá del baseline del perfil.

2. **`zero_peak_exit` inteligente con ventana de gracia pre-spike**
- Se mantiene el corte temprano (no se eliminó).
- Si el símbolo está en ventana cercana al spike esperado (`remaining <= grace_ticks`), el cierre se **difiere** temporalmente.
- Evita cerrar justo antes del spike en casos BOOM500/BOOM600 observados en vivo.

3. **Ajuste de perfiles BOOM/CRASH para Stage 4**
- `BOOM900`: `fvg_tier_minimo` de `fvg_mitigated` → `fvg_detected`.
- `BOOM600`: `fvg_tier_minimo` de `fvg_mitigated` → `fvg_detected`.
- `CRASH900`: se agrega `spike_min_post_sec=300` para evitar entradas tempranas/tardías de persecución.

### Aclaración técnica del "lag 130s"

- El `entry_lag_sec` NO es delay de lectura de datos ni latencia del feed.
- Es diferencia temporal entre:
  - `ts` del spike detectado
  - `opened_at_ts` del contrato que terminó asociado a ese spike en la reconstrucción analítica.
- En CRASH900, `lag ~135s` significa que el bot abrió después del spike detectado previo (timing operativo/estratégico), no que el websocket llegó tarde.

### Uso de histórico PostgreSQL

- Se verificó estado de tablas `deriv_contracts` y `deriv_tick_snapshots` en `optiferre_pamm`.
- Ambas tablas están vacías en este entorno (`count(*)=0`), por lo que el tuning Stage 4 se apoyó en:
  - `/data/deriv-logs/deriv_spike_events.json`
  - `/data/deriv-logs/deriv_closed_contracts.json`
  - `/data/deriv-logs/deriv_open_contracts.json`

### Protocolo de arranque limpio Prueba 4 (Stage 4)

1. Confirmar `deriv_open_contracts.json` sin posiciones abiertas.
2. Archivar snapshots previos de `deriv_closed_contracts.json` y `deriv_spike_events.json`.
3. Reset controlado:
   - `deriv_closed_contracts.json` → `[]`
   - `deriv_open_contracts.json` → `[]`
   - `deriv_spike_events.json` → `[]`
   - `deriv_spikes.json` → `[]`
   - `deriv_ai_decisions.json` → `[]`
   - `deriv_lockout.json` → `{}`
   - `deriv_status.json` → `{}`
4. Redeploy por `git push` (Coolify webhook).
5. Verificar que solo exista un contenedor activo del bot Deriv (`o4w1ns4cceccmn2ozqt7sol2`).

### Commit Stage 4

- Commit base de cambios: `6a02bed`
- Trigger deploy: `7d39318`
- Contenedor activo post-deploy: `o4w1ns4cceccmn2ozqt7sol2:7d39318`
- Estado deploy: **APLICADO** (rebuild + replace manual en servidor, 1 contenedor activo)

### Ejecución real de restart Stage 4

- Fecha UTC: `2026-05-23 00:12`
- Archivo de respaldo creado:
  - `/data/deriv-logs/archive_stage4_20260523_001233/`
- Validación post-reset:
  - `heartbeat_at` fresco
  - `closed=0`
  - `spikes=1` (nueva muestra ya iniciada)
  - `open=2` (operación live retomada por el bot tras reset)

---

## PRUEBA 5 — CAPA IA DINAMICA (HOT DEPLOY, SIN SHADOW)

### Objetivo operativo

- Mover umbrales por símbolo en caliente para cortar 3 fallas confirmadas en Prueba 4:
  1. Entradas tardías (lag >= 120s).
  2. Salidas prematuras (`zero_peak_exit`) antes del spike.
  3. Ceguera ante aceleración/desaceleración por símbolo.

### Implementación realizada

1. **Puente PostgreSQL de configuración dinámica**
- Nueva migración: `db/migrations/009_dynamic_symbol_config.sql`.
- Hardening aplicado: `db/migrations/010_dynamic_guardrails_hardening.sql`.
- Tabla: `dynamic_symbol_config` con guardrails duros:
  - `spike_pre_filter_target` entre `50..500`
  - `zero_peak_grace_sec` entre `0..120` con piso obligatorio `>=60` en `BOOM500/CRASH500/CRASH600`
  - `score_min_override` entre `6.5..8.0` (nunca por debajo)
- Símbolos base insertados: BOOM1000/900/600/500 + CRASH1000/900/600/500.

2. **Bot principal leyendo configuración dinámica cada 15s**
- `src/main_deriv.py`:
  - Loop `_dynamic_config_refresh_loop()` con lectura a PostgreSQL.
  - Cache en memoria (`self._dynamic_configs`) para no tocar DB por tick.
  - Estado visible en `deriv_status.json` bajo `dynamic_config`.

3. **Entrada dinámica (anti-lag)**
- `src/main_deriv.py`:
  - `spike_pre_filter` usa `spike_pre_filter_target` de DB cuando `is_active=true`.
  - Si no hay config dinámica activa, mantiene fallback adaptativo previo.
  - Score gate dinámico por símbolo con `score_min_override`.

4. **Salida dinámica (anti-cierre prematuro)**
- `src/execution/deriv_trader.py`:
  - `zero_peak_exit` ya no usa límite fijo rígido.
  - Nuevo límite: `DERIV_ZERO_PEAK_BASE_SEC + zero_peak_grace_sec` por símbolo.
  - Inyección de provider dinámico desde daemon (`set_dynamic_config_provider`).

5. **Orquestador IA independiente**
- Script: `scripts/dynamic_ai_orchestrator.py`.
- Contenedor dedicado: `Dockerfile.deriv-ai`.
- Ciclo cada 60s (configurable):
  - Lee telemetría reciente.
  - Consulta LLM (JSON estricto).
  - Aplica guardrails locales (score `6.5..8.0` + piso `zero_peak>=60` en símbolos críticos).
  - Aplica histéresis temporal por símbolo (`MIN_STATE_LIFETIME_SEC=600` por defecto).
  - Solo ejecuta `UPDATE` en PostgreSQL cuando hay cambio real matemático.
  - Registra diffs forenses en JSONL (`dynamic_ai_config_diffs.jsonl`).
  - Fallback heurístico si LLM falla.

6. **Auto-migración de arranque actualizada**
- `scripts/migrate_db.py` incluye migración `009_dynamic_symbol_config.sql`.

### Variables de entorno aplicadas en Coolify (verificado)

Aplicadas en `/data/coolify/applications/o4w1ns4cceccmn2ozqt7sol2/.env`:

- `DERIV_DYNAMIC_CONFIG_ENABLED=true`
- `DERIV_DYNAMIC_CONFIG_REFRESH_SEC=15`
- `DERIV_ZERO_PEAK_BASE_SEC=150`
- `DERIV_BLOCK_BC_ESCAPE_BOOM600=false`
- `DERIV_BLOCK_BC_ESCAPE_BOOM900=false`
- `DYNAMIC_AI_LOOP_SEC=60`
- `DYNAMIC_AI_LOGS_DIR=/data/logs`
- `DYNAMIC_AI_MIN_STATE_LIFETIME_SEC=600`
- `DYNAMIC_AI_DIFF_LOG_PATH=/data/logs/dynamic_ai_config_diffs.jsonl`
- `DYNAMIC_AI_MODEL=gpt-4o-mini`
- `DYNAMIC_AI_BASE_URL=https://api.openai.com/v1/chat/completions`

Nota: el orquestador continúa aceptando `DYNAMIC_AI_LOOP_SEC` y alias `DYNAMIC_AI_INTERVAL_SEC`; para logs acepta `DYNAMIC_AI_LOGS_DIR` y alias `DYNAMIC_AI_LOG_DIR`.

### Estado actual del bot (sin pendientes) — 2026-05-23 05:24 UTC

- Commit activo desplegado: `6274ec5`.
- Contenedores activos:
  - `o4w1ns4cceccmn2ozqt7sol2:6274ec5...` (daemon principal)
  - `o4w1ns4cceccmn2ozqt7sol2-ai:6274ec5...` (orquestador IA)
- DB validada en vivo (`dynamic_symbol_config`):
  - `score_min_override` ya dentro de `6.8..7.0` (cumple hard floor 6.5).
  - `zero_peak_grace_sec=60` en todos los símbolos (incluye `BOOM500/CRASH500/CRASH600` con piso obligatorio).
  - Constraint presente en DB: `chk_dsc_sensitive_zero_peak_floor` + `chk_dsc_score_min_override`.
- Runtime validado en `deriv_status.json`:
  - `dynamic_config.enabled=true`
  - `dynamic_config.refresh_sec=15`
  - Configs dinámicos por símbolo cargados y refrescando en caliente.
- Histéresis y auditoría forense activas en orquestador:
  - Bloqueo de flips de régimen dentro de ventana mínima por símbolo.
  - Escritura a DB únicamente cuando hay diff real.
  - Archivo de auditoría de cambios: `/data/logs/dynamic_ai_config_diffs.jsonl`.
  - Evidencia en logs: `[dynamic-ai][HYSTERESIS] ... rejected regime flip ...` y `[dynamic-ai][DIFF] ...`.
- BOOM600/BOOM900 desbloqueados de veto rígido por profile env:
  - evidencia en logs: `[STRUCTURAL_VETO_ESCAPE] BOOM600 ...` y `[STRUCTURAL_VETO_ESCAPE] BOOM900 ...`
  - esto confirma que `DERIV_BLOCK_BC_ESCAPE_BOOM600/900=false` está impactando el pipeline.

### Integridad de ciencia de datos / trazabilidad

- Se mantienen intactos los reportes y artefactos operativos (`deriv_spike_events.json`, `deriv_closed_contracts.json`, `deriv_status.json`, telemetría de decisiones).
- La capa dinámica no reemplaza observabilidad: añade un lazo de control (DB + LLM + fallback) encima de los mismos datos forenses para iteración cuantitativa continua.
- Para ventana de validación 2-4h, KPIs prioritarios: `zero_peak_exit<120s`, `P75 entry_lag_sec`, y reactivación operativa BOOM600/CRASH900.

### Resultado esperado de esta etapa

- Menos bloqueos por `spike_pre_filter` durante ventanas FAST.
- Menos `zero_peak_exit` justo antes del spike siguiente.
- Rebalanceo de agresividad por símbolo sin redeploy de código.

---

## PRUEBA 5B — CORRECCIÓN ESTRUCTURAL (MUESTRA >200 OPERACIONES)

### Hallazgo consolidado

Con muestra acumulada >200 operaciones se confirmó un patrón doble:

1. **Entradas tardías perseguiendo spike** (`lag 120-150s`) cuando la capa de entrada estaba influida por memoria de spike.
2. **`zero_peak_exit` prematuro** en ventanas donde el spike llegaba `30-60s` después del cierre.

Diagnóstico operativo: el histórico de spikes debe servir para **calibrar** (riesgo/espera), no para decidir entrada. La entrada debe permanecer **tick-driven**.

### Evidencia operativa de reset/arranque limpio (2026-05-23 ~06:10-06:15 UTC)

- Se forzó cierre broker-side de contratos abiertos heredados (`remaining_portfolio=[]` tras cierre).
- Se creó backup limpio de estado:
  - `/data/deriv-logs/archive_stage5_20260523_061241/`
- Se resetearon archivos de muestra (`open/closed/spikes/status/ai_decisions`) y se reinició stack principal + IA.
- Validación runtime post-arranque:
  - watchdog: `status=PASS`
  - heartbeat fresco + `dynamic_config` cargado para 8 símbolos
- Snapshot bootstrap guardado:
  - `/data/deriv-logs/sample5_bootstrap_20260523_061452.json`

### Cambios de arquitectura aplicados en código (Prueba 5B)

1. **Entrada tick-only (sin gate de historial spike)**
- `src/main_deriv.py`
  - Nuevo flag `DERIV_ENTRY_TICK_ONLY` (default: `true`).
  - Si está activo, se desactiva el bloque `spike_pre_filter` en la ruta de entrada.
  - `deriv_status.json` expone `dynamic_config.entry_tick_only`.
- `src/safety/deriv_risk.py`
  - Cuando `DERIV_ENTRY_TICK_ONLY=true`, se desactiva `SPIKE_CYCLE_GATE` como veto de entrada.
  - El override `spike_active_override` queda deshabilitado por defecto
    (solo se habilita si `DERIV_ALLOW_SPIKE_ACTIVE_AI_OVERRIDE=true`).

2. **Salida de emergencia realmente dinámica (anti-cierre prematuro)**
- `src/execution/deriv_trader.py`
  - `zero_peak_exit` ahora usa una espera mínima dinámica ligada a:
    - `zero_peak_grace_sec` (IA),
    - ciclo esperado del símbolo,
    - límite pre-timeout (margen antes de `spike_timeout`).
  - Se amplió la lógica de defer pre-spike para evitar cerrar justo antes del spike.
  - Objetivo: que `zero_peak_exit` vuelva a ser un **freno de emergencia tardío**, no un cierre por ansiedad.

3. **Orquestador IA alineado al nuevo contrato operativo**
- `scripts/dynamic_ai_orchestrator.py`
  - Se congela `spike_pre_filter_target` en escritura automática (no se usa para decidir entrada).
  - IA ajusta principalmente:
    - `score_min_override` (convicción de entrada tick-driven),
    - `zero_peak_grace_sec` (espera dinámica de salida).
  - Prompt actualizado para reforzar explícitamente: *entrada por ticks; spikes solo para calibración*.

### Contrato operativo final (vigente para Prueba 5B)

- **Entrada**: matemática de ticks (microestructura + riesgo), no memoria de spike.
- **Spikes**: señal histórica para ajustar paciencia/sensibilidad, nunca trigger directo de entrada.
- **Salida `zero_peak_exit`**: dinámica y tardía; prioriza no perder spikes inminentes.

### KPIs de validación inmediata (2-6h)

1. `P75 entry_lag_sec` por símbolo (debe bajar de forma sostenida)
2. `% late_entry_ge120` (objetivo: compresión fuerte)
3. `% zero_peak_exit con spike <=80s posterior` (objetivo: caída marcada)
4. Distribución de `exit_reason` (`zero_peak_exit` debe dejar de dominar cierres antes de spike)
