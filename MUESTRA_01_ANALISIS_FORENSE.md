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
| Muestra 02 | PENDIENTE | Validar extensión timeouts BOOM600/BOOM900, habilitar CRASH1000 |
| Muestra 03 | PENDIENTE | Reducir trade_cooldown, Hurst ajuste |
| Muestra 04 | PENDIENTE | Early exit logic, evaluación de trading hours filter |

---

*Generado: 22 Mayo 2026 — GitHub Copilot + datos live del bot Deriv*
