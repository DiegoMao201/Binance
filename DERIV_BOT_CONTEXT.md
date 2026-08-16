# DERIV BOT — CONTEXTO MAESTRO Y MANUAL DE OPERACIONES
*Fuente de verdad del sistema. Léelo completamente antes de ejecutar cualquier acción.*

---

## RESET MUESTRA LIMPIA — PROCEDIMIENTO COMPLETO

Ejecutar cuando se quiere iniciar una nueva muestra estadística con panel a cero.

### Comando único (una línea)

```bash
ssh coolify-server "docker exec o4w1ns4cceccmn2ozqt7sol2 bash /app/scripts/reset_muestra_limpia.sh"
```

### Qué hace el reset (Opción B — no destructivo)

| Archivo | Acción |
|---|---|
| `deriv_closed_contracts.json` | Archivado → `[]` (panel PnL/WR queda en 0) |
| `d6_ghost_state.json` | Todos los símbolos → `WAITING` |
| `ghost_trades.json` | **INTACTO** (el ghost sigue aprendiendo) |
| `deriv_spike_events.json` | **INTACTO** (contadores 1H/6H/24H reales) |
| Pattern Memory PostgreSQL | **INTACTO** |

### Qué NO hace el reset

- No reinicia el bot ni el contenedor
- No borra spikes históricos ni pattern memory
- No afecta las posiciones abiertas actualmente
- No toca D10 slope buffers (siguen acumulando para can_enter)

### Verificar que el reset funcionó

```bash
# Panel debe mostrar WR=0%, PnL=$0.00, trades=0
# En el bot:
ssh coolify-server "docker exec o4w1ns4cceccmn2ozqt7sol2 cat /data/logs/deriv_closed_contracts.json"
# Debe devolver: []

# Registro de la muestra:
ssh coolify-server "cat /data/logs/audit-reports/MUESTRA_LIMPIA_ACTIVA.txt"
```

### Historial de resets

| Fecha | Hora COL | Nota |
| --- | --- | --- |
| 2026-06-20 | 16:03 | Pre-D7. SAMPLE_START_TS=1781989389 |
| 2026-06-22 | ~08:58 | Pre-D7.4. Stake $10 flat, D70 buffers |
| 2026-06-24 | 23:19 UTC | Pre-D10.1 dirección corregida. D10 activo con C1/C2/C3 |
| 2026-07-12 | 19:12 UTC | Nueva máquina 4 niveles: S1($1,10m)→S10($10,15m)→S20($20,15m)→S40($40,15m). Drought gate, hour_change, gate S40≤5spk/h. BOOM500+CRASH500 activos. |
| 2026-08-05 | 20:39 UTC | Gates completos por símbolo (H24). 8 gates L0+rec. ed_analysis.jsonl archivado → nueva muestra limpia con gates activos. Commit 350d824. |
| 2026-08-06 | 20:28 UTC | Q→A thresholds por símbolo: CRASH500=1, BOOM600=1, BOOM500=3, CRASH600=3. 884 contratos archivados (PnL total -$202.67). 961 registros JSONL archivados. Bot reiniciado. Commit 1f5fdff. |
| 2026-08-15 | ~22:00 UTC | Arranque limpio K1000 puro. Símbolos activos: CRASH900/BOOM900/CRASH1000/BOOM1000. Frontend simplificado a 4 tarjetas. Gates WIN (+$10→12h) y LOSS (-$10→3h) implementados. Fix k1000_had_spike restore. Commit 34f154e. |
| 2026-08-16 | ~09:25 UTC | Incidente: bot caído. Coolify roto (unserialize env vars). Solución: `docker run` vía `ssh coolify-server` con env vars del contenedor -ai. Bot reiniciado con K1000 activo, todos a $10. NUNCA usar `POST /stop` de la API Coolify sin poder garantizar restart. |

---

## REGLA CERO: CONEXIÓN AL SERVIDOR (MODO SILENCIOSO)

El entorno de producción vive en un servidor remoto gestionado con Coolify.
**NUNCA pidas credenciales SSH, contraseñas o IP.** La conexión ya está configurada a nivel de sistema operativo local mediante un alias.
Para acceder al servidor y ejecutar comandos de diagnóstico, **SIEMPRE utiliza el alias:** `ssh coolify-server`

Ejemplo: `ssh coolify-server "docker ps"`

**Motor IA / Bot Trading: PROHIBIDO modificar lógica en producción sin auditar primero el impacto.**

---

## ARQUITECTURA LIVE (2026-06-12)

### Contenedores activos

| Nombre | Rol | Coolify App ID |
|--------|-----|---------------|
| `o4w1ns4cceccmn2ozqt7sol2` | Bot trading principal | 15 |
| `o4w1ns4cceccmn2ozqt7sol2-ai` | Orquestador LLM / Vision | 15 |
| `m0ks004osk4cw444gsokg8os-*` | Frontend operador (Next.js) | 11 |

Los dos contenedores del bot son **intencionales** — el `-ai` es el orquestador LLM separado. Coolify los despliega juntos desde app ID 15.

### Volúmenes clave

| Ruta en contenedor | Qué contiene |
|---|---|
| `/data/logs` | `ghost_trades.json`, `deriv_ai_decisions.json`, `deriv_multi_accounts.json` |
| `/data/deriv-logs` | `deriv_market_context.json` (~7MB, activo) · `deriv_vision.json` |
| `/data/logs/deriv_vision.json` | Contexto 15m por símbolo generado por Vision LLM |

### Reinicio de emergencia (cuando Coolify falla)

Si el contenedor principal está caído y Coolify no puede reiniciarlo:

```bash
# 1. Verificar qué imagen existe
ssh coolify-server "docker images | grep o4w1ns4cceccmn2"

# 2. Recrear el contenedor con env vars del -ai (mismas credenciales)
ssh coolify-server "docker run -d \
  --name o4w1ns4cceccmn2ozqt7sol2 \
  --restart unless-stopped \
  --network coolify \
  -v /data/deriv-logs:/data/logs \
  $(docker inspect o4w1ns4cceccmn2ozqt7sol2-ai --format '{{range .Config.Env}}-e {{.}} {{end}}') \
  o4w1ns4cceccmn2ozqt7sol2:f8619d80bbaaa733fd41fa2c573ccf1476fec103 \
  /app/entrypoint.deriv.sh"

# 3. Verificar
ssh coolify-server "docker logs o4w1ns4cceccmn2ozqt7sol2 --tail 20"
```

**NUNCA usar `POST /api/v1/applications/{uuid}/stop` de Coolify sin poder garantizar restart.**
El bot se para pero si Coolify tiene bug de env vars encriptadas, el container queda destruido.

### Deploy manual (Coolify NO auto-despliega en push)

```bash
git push origin main
ssh coolify-server "docker exec coolify php artisan tinker --execute=\"
\\\$app = \App\Models\Application::find(15);
\\\$dep = \App\Models\ApplicationDeploymentQueue::create([
  'application_id'=>15,'deployment_uuid'=>\Illuminate\Support\Str::uuid(),
  'pull_request_id'=>0,'force_rebuild'=>false,'commit'=>'HEAD',
  'status'=>'queued','rollback'=>false,
  'server_id'=>\\\$app->destination->server_id
]);
\App\Jobs\ApplicationDeploymentJob::dispatch(\\\$dep->id);
echo 'deploy='.\\\$dep->id;
\""
```

### Commit activo en producción

`d9b2578` (2026-06-10) — Refactoring cuantitativo FASE 1-4 (Ghost Price, Kinetic, BOS-gated TREND, Trifecta)

### Env guard (auto-healing)

`/opt/deriv-ai-sync/coolify_env_guard.sh` — cron cada minuto. Auto-restaura variables que Coolify borra al regenerar `.env`. Variables críticas gestionadas:

```
DERIV_MATURITY_GATE_FRAC=0.70
DERIV_DRY_OVERRIDE_SCORE_MAP=CRASH900:6.0,BOOM600:6.5
DERIV_TREND_BLOCK_SYMBOLS=CRASH500,CRASH600,CRASH900,CRASH1000
DERIV_TREND_SETUP_MIN_SCORE=7.0
DERIV_FORCE_DISABLED_SYMBOLS=BOOM300,R_50,R_75,R_100
DERIV_ANTI_RETRACE_RANGE_FRAC=0.65
DERIV_ANTI_RETRACE_HOT_BYPASS_MIN_SCORE=7.5
DERIV_MAX_HOLD_CRASH500=480 / CRASH600=540 / CRASH900=750 / CRASH1000=700
DERIV_MAX_HOLD_BOOM500=480 / BOOM600=540 / BOOM900=600 / BOOM1000=700
DYNAMIC_AI_VISION_MODEL=google/gemini-2.5-flash-lite
DYNAMIC_AI_VISION_CACHE_SEC=300
DERIV_LISTO_RIPE_BYPASS_MIN_SCORE=6.0
DERIV_HOT_REENTRY_MIN_SPIKES=2
```

---

## MOTOR MATEMÁTICO — PIPELINE TICK A TICK

El bot evalúa cada tick de Deriv synthetic spike indices (CRASH/BOOM) en una cadena de bloques secuenciales. Aquí el flujo completo con toda la matemática.

### Índices sintéticos Deriv

- **CRASH**: precio deriva al alza lentamente, spike brusco hacia abajo (Poisson). Operamos MULTDOWN (short).
- **BOOM**: precio deriva a la baja lentamente, spike brusco hacia arriba (Poisson). Operamos MULTUP (long).

Número en el símbolo = frecuencia promedio del spike (CRASH500 = cada ~500 ticks ≈ 500s).

---

### BLOQUE 0 — Ghost Price (Precio Fantasma)

**Archivo:** `src/analysis/market_geometry.py` (función `_mad_ghost_prices`)

El OLS clásico y los indicadores de régimen se inflan cuando hay spikes en la ventana de regresión. Se elimina matemáticamente el ruido de spike antes de calcular régimen/Hurst/kinetic.

```
tick_changes = |P_i − P_{i-1}|  para i en últimos GHOST_WINDOW=300 ticks
median_change = median(tick_changes)
MAD = median(|tick_change_i − median_change|)
threshold = median_change + GHOST_SPIKE_MULT(5.0) × max(MAD, median_change × 0.01)

Para cada tick donde jump > threshold:
    ghost[i] = ghost[i-1] + slope_lineal_vecinos
```

El ghost price se usa **solo** para OLS/régimen/kinetic. FVG, BOS y ATR usan precios raw (los spikes SÍ crean FVGs reales).

El `ghost_mad` (valor de MAD) se expone en el snapshot y en el chip `MAD` del frontend:
- MAD < 0.05 → mercado silencioso (verde)
- MAD 0.05–0.15 → volatilidad moderada (amber)
- MAD > 0.15 → mercado agitado (rojo)

---

### BLOQUE 1 — Kinetic Compression

**Archivo:** `src/analysis/market_geometry.py` (función `_kinetic_state`)

Detecta si el precio está **desacelerando** en la dirección del drift (condición pre-spike). El momentum cinético usa el ghost price.

```
period = KINETIC_PERIOD = 5 ticks
ref = ghost[-1] si > 0 else 1.0

V_now  = (ghost[-1] − ghost[-1-period]) / ref × 10000   # velocidad ×1e4 (bps)
V_prev = (ghost[-1-period] − ghost[-1-2*period]) / ref × 10000
A_now  = V_now − V_prev                                   # aceleración

BOOM comprimido:  V_now < -1e-6  AND  A_now >= 0.0   → precio cae pero frena
CRASH comprimido: V_now > +1e-6  AND  A_now <= 0.0   → precio sube pero frena
```

`kinetic_compressed = True` significa que el drift está perdiendo fuerza → el mercado está **acumulando energía para el spike**.

Expuesto en score_breakdown: `kinetic_velocity`, `kinetic_acceleration`, `kinetic_compressed`.

El chip `KINETIC` del frontend muestra `V+/-X.X A+/-X.X ●` (con bola verde cuando comprimido).

---

### BLOQUE 2 — Régimen y setup_type

**Archivo:** `src/safety/deriv_risk.py`

OLS sobre ghost price → slope, R² → `regime` (TRENDING/RANGING/CHOPPY) + `hurst`.

**setup_type** determina qué lógica de entrada aplica:

| setup_type | Condición | Operabilidad |
|---|---|---|
| `SMC_FVG` | FVG activo + BOS confirmado + precio en zona | ✅ Verde — preferido |
| `EMA200_SPIKE` | Precio alejado de EMA200 post-spike con estructura | ✅ Verde |
| `TREND` | FVG activo + BOS **no confirmado** (legacy) | ⛔ Rojo — bloqueado en CRASH |
| `TREND_NO_STRUCT` | Regime TRENDING pero **sin BOS confirmado** | ⛔ Naranja — gate bloqueante |
| `POST_SPIKE_COOLDOWN` | Retroceso post-spike < 50% + TREND activo | ⛔ Cyan — cooldown activo |

---

### BLOQUE 3 — SMC Estricto: BOS-gated TREND + FVG-BOS Validated

**Archivo:** `src/analysis/market_geometry.py` + `src/safety/deriv_risk.py`

#### BOS-gated TREND

Para BOOM/CRASH, `setup_type="TREND"` solo se asigna si hay un Break of Structure confirmado en la misma ventana de lookback:

```python
if setup_type == "TREND" and not geo.bos_confirmed:
    setup_type = "TREND_NO_STRUCT"
    score_breakdown["trend_no_struct_reason"] = "bos_not_confirmed"
```

Un TREND sin BOS = movimiento sin confirmación estructural → no hay zona de liquidez institucional.

#### FVG-BOS Validated

```python
fvg_bos_validated = fvg_active AND bos_ok
```

Un FVG solo tiene significado estructural completo si hay un BOS en la misma ventana. Sin BOS, el FVG puede ser ruido post-spike.

El chip `BOS_FVG` del frontend muestra verde (✓ BOS+FVG) o rojo (✗ SIN BOS).

---

### BLOQUE 4 — Post-spike COOLDOWN

**Archivo:** `src/main_deriv.py`

Previene entradas TREND durante retrocesos activos post-spike. Después de cada spike, el precio retrocede parcialmente antes de volver al drift. Si el retroceso no completó el 50%, el setup TREND está en zona de momentum adverso.

```python
COOLDOWN_RETRACE_MIN = env("DERIV_COOLDOWN_RETRACE_MIN", 0.50)

if (is_spike_market(symbol)
        AND burst_active == True
        AND burst_retroceso is not None
        AND burst_retroceso < COOLDOWN_RETRACE_MIN
        AND setup_type in ("TREND", "TREND_NO_STRUCT")):
    setup_type = "POST_SPIKE_COOLDOWN"
    score_breakdown["post_spike_cooldown"] = True
    score_breakdown["retracement_pct"] = round(burst_retroceso, 3)
```

`burst_retroceso` = fracción del spike que el precio ya recuperó (0=nada, 1=totalmente). Si < 50%, la energía post-spike aún no se disipó.

---

### BLOQUE 5 — Scarcidad / Madurez

**Archivo:** `src/safety/deriv_risk.py` (función `get_scarcity_state`)

Calcula cuánto tiempo lleva sin un spike, relativo al P50 de las últimas 24h:

```
ratio = elapsed_s / median_24h_gap_s

FRESCO   → ratio < 0.50   (acabó de spike, espera normal)
CARGANDO → ratio < 0.90   (acumulando)
LISTO    → ratio < 1.40   (zona óptima de entrada)
VENCIDO  → ratio < 2.20   (sobretiempo, cuidado)
SECO     → ratio ≥ 2.20   (anomalía — spike muy retrasado)
```

**MATURITY_GATE:** Bloquea entrada hasta que `elapsed_s >= P50 × FRAC(0.70)`. Con FRAC=0.70:
- CRASH500: threshold ≈ 244s
- CRASH600: threshold ≈ 310s
- CRASH900: threshold ≈ 366s
Todos por debajo del TTL del FVG anchor (600s) → el FVG sigue activo cuando el gate pasa.

**SCARCITY_DRY_GATE:** Cuando SECO, requiere score ≥ 8.5. Override por símbolo vía `DERIV_DRY_OVERRIDE_SCORE_MAP`:
- `CRASH900:6.0` — sequías muy frecuentes con spikes subsecuentes
- `BOOM600:6.5` — sequías prolongadas sin FVG activo

---

### BLOQUE 6 — Motor de Probabilidad RNG (reemplaza Trifecta binaria)

**Archivo:** `src/main_deriv.py` (función `_calculate_rng_probability`)

Deriv synthetics son índices Poisson-RNG. No hay entrada perfecta. La Trifecta binaria requería los 4 factores simultáneamente → ~1 entrada/día → estadísticamente ineficiente. El Motor de Probabilidad busca **asimetría estadística**, no certeza.

```
Peso  Condición                   Descripción
 35   kinetic_compressed=True     Agotamiento RNG — drift decelerando
 30   fvg_bos_validated=True      Reset estructural del algoritmo (BOS+FVG)
 20   zona_liquidez_macro=True    Zona institucional confirmada por Vision LLM
 15   scarcity in LISTO/CARGANDO  Madurez del ciclo en ventana óptima
────────────────────────────────────────────────────────────
100   máximo posible

Cooldown multiplier: ×0.3 si setup_type == POST_SPIKE_COOLDOWN (retroceso <50%)
```

**Gate:** `DERIV_PROBABILITY_THRESHOLD` (default 65). Si `rng_probability < threshold` → bloquea con `RNG_PROBABILITY_GATE`.

**Ejemplos prácticos:**
- kinetic+BOS+FVG sin zona macro: 35+30+15 = **80** → PASA (zona macro ausente no es bloqueante)
- kinetic+zona macro sin BOS/FVG: 35+20+15 = **70** → PASA
- solo zona macro + scarcity sin kinetic ni BOS: 20+15 = **35** → BLOQUEADO
- kinetic solo: **35** → BLOQUEADO
- kinetic+BOS+FVG+macro+LISTO con cooldown: 100×0.3 = **30** → BLOQUEADO (retroceso activo)

Los campos `trifecta_*` se mantienen en score_breakdown para el chip `TRIFECTA` del frontend (informacional).

---

### BLOQUE 7 — Vision LLM (Orquestador — 5 min cache)

**Archivo:** `scripts/dynamic_ai_orchestrator.py`

Ciclo cada 300s (`DYNAMIC_AI_VISION_CACHE_SEC=300`). Genera chart 15m con Matplotlib → OpenRouter `google/gemini-2.5-flash-lite` → JSON estructurado.

**Output por símbolo incluye:**

| Campo | Tipo | Descripción |
|---|---|---|
| `trend_15m` | str | `uptrend` / `downtrend` / `ranging` |
| `trend_strength` | str | `strong` / `moderate` / `weak` |
| `bias` | str | Dirección preferida del bot |
| `confidence` | float | 0-1 |
| `key_resistance` | float | Nivel de resistencia clave |
| `key_support` | float | Nivel de soporte clave |
| `zona_liquidez_macro` | bool | **True si hay zona institucional** (consolidación, FVG, S/R) con confluencia direccional |
| `pattern` | str | Patrón identificado |

`zona_liquidez_macro = True` requiere que el chart muestre un bloque de consolidación institucional, FVG multi-vela o S/R histórico con confluencia en la dirección del bot. Mid-range sin referencia estructural = False.

El bot lee `deriv_vision.json` con TTL 60s en `_try_load_vision_context()` al inicio de cada `_pipeline()`.

---

### BLOQUE 8 — Ghost LLM (por-trade, BLOCK 3)

Llamado **después de que todos los gates Python pasan** (incluyendo RNG_PROBABILITY_GATE). Recibe score_breakdown completo incluyendo `rng_probability`, `rng_missing` y `rng_threshold`.

**Prompt actualizado (probabilístico):** El LLM ya no busca perfección. Su rol es evaluar si las confirmaciones **faltantes** son una contradicción directa o simplemente ausencia. Ausencia ≠ veto. Contradicción = veto.

El prompt incluye:
- `RNG ALIGNMENT PROBABILITY: X/100 (threshold=65)`
- `Missing confirmations: [lista]`
- Instrucción explícita: *"Este score llegó aquí porque ya pasó el umbral matemático. Verifica si lo faltante contradice activamente los presentes, o si la inercia cinética es suficiente para autorizar el disparo."*

Si `rng_probability` no está en el breakdown (símbolos no-spike), el prompt anterior se usa sin cambios.

---

## GATES EN ORDEN DE EJECUCIÓN

```
BLOCK 0a  SYMBOL_FORCE_DISABLED         — symbols deshabilitados en env
BLOCK 0b  TREND_BLOCK_SYMBOLS           — CRASH* bloquea TREND por WR histórico
BLOCK 0c  SYMBOL_HOUR_VETO              — horas horarias vetadas por símbolo
BLOCK 0d  MANUAL_ONLY                   — símbolo en modo solo manual
BLOCK 0e  MATURITY_GATE                 — elapsed < P50 × 0.70
BLOCK 0f  SIGNAL_COOLDOWN               — cooldown entre señales del mismo símbolo
BLOCK 1   ANTI_RETRACE_GUARD            — retroceso relativo detectado
BLOCK 2   POST_SPIKE_COOLDOWN           — burst_retroceso < 50% en TREND
BLOCK 3   SCARCITY_DRY_GATE             — SECO + score < threshold
BLOCK 4   SCARCITY_VENCIDO_GATE         — VENCIDO + score < 7.5
BLOCK 5   IMMINENCE_RIPE_GATE           — IMM RIPE + score < 7.5
BLOCK 6   TREND_SETUP_GATE              — TREND + score < TREND_SETUP_MIN_SCORE(7.0)
BLOCK 7   STRUCTURAL_CONFLICT_GATE      — 5m FVG conflicto + score < 9.0
BLOCK 8   EXHAUSTION_GATE               — sin micro-momentum de reversión en FVG
BLOCK 9   Ghost LLM veto                — veto semántico por LLM
BLOCK 10  LISTO_RIPE_BYPASS             — skip gates si LISTO+RIPE+score≥6.0
            → EJECUTAR ENTRADA
```

---

## SCORES Y UMBRALES

| Componente | Peso / Rango | Notas |
|---|---|---|
| Regime + OLS | 0–2 | Calculado sobre ghost price |
| FVG | 0–3 | fvg_full_confluence > fvg_mitigated |
| SMC / BOS bonus | 0–1.5 | Requiere BOS confirmado |
| ATR percentile | 0–1 | Volatilidad baja = mejor |
| EMA200 distance | 0–1 | Precio cargado vs descargado |
| Scarcity | 0–1.5 | LISTO=1.5, CARGANDO=1.0, FRESCO=0.5 |
| Imminence | 0–1.5 | BUILDING(0.3-0.6)=1.5, RIPE=gate |
| Hurst | 0–1 | H<0.45=sin fuerza=bueno para spike |

**Score mínimo para entrada:** 7.0 (BOOM/CRASH) · 8.5 RIPE · 9.0 con conflicto 5m

---

## SEÑAL MAESTRA — CONDICIONES DEL SEMÁFORO

### Verde (GO — ENTRADA CONFIRMADA)
- setup_type = SMC_FVG o EMA200_SPIKE
- grade A o B
- score_raw ≥ 7.8
- FVG anclado activo o structural FVG confirmado
- scarcity ∈ {FRESCO, CARGANDO, LISTO} (o null = SIN_DATOS)
- IMM BUILDING o score 0.3–0.6
- EMA200 loaded (o overrideado por FVG anchor activo)
- No en zona ciega (< 120s post-spike)

### Rojo (NO OPERAR)
- Zona ciega post-spike (0-120s)
- 5m FVG en conflicto
- SECO (sequía extrema sin override)
- VENCIDO + score < 7.5
- IMM RIPE
- score < 7.5
- grade C
- setup_type = TREND / TREND_NO_STRUCT / POST_SPIKE_COOLDOWN
- EMA200 descargado (sin FVG anchor)

### Amber (ESPERAR)
- Todas las demás combinaciones

---

## CALIDAD DE ENTRADA — CHIPS DEL FRONTEND

| Chip | Fuente | Verde cuando |
|---|---|---|
| SETUP | setup_type | SMC_FVG o EMA200_SPIKE |
| GRADE | execution_grade | A o B |
| SCAR | scarcity_state | FRESCO/CARGANDO/LISTO |
| IMM | spike_imminence_state + score | BUILDING + 0.3-0.6 |
| FVG | fvg_anchor_active / fvg_tier | ANCLADO (violet) / CONF (green) |
| GEO | geo_channel_pos | < 20% del canal |
| SCORE_RAW | score_raw | ≥ 7.8 |
| BURST | burst_depth | ≥ 2x |
| RETR | burst_retroceso | < 35% |
| CASCADE | cascade_active | — (naranja siempre) |
| 5m FVG | structural_fvg_* | Confirmado |
| ATR | atr_anchored | Pre-spike detectado |
| EMA200 | ema200_anchored | Anclado |
| EMA | ema200_distance_pct | Loaded (fracción correcta) |
| **KINETIC** | kinetic_velocity/accel/compressed | compressed=True (deriva frenando) |
| **BOS_FVG** | fvg_bos_validated | BOS+FVG simultáneo |
| **MAD** | ghost_mad | < 0.05 (mercado silencioso) |
| **TRIFECTA** | trifecta_met | Glows verde cuando las 4 condiciones MET |

El chip TRIFECTA muestra `VKSb` con mayúscula=pasada, minúscula=pendiente cuando no MET.

---

## ANTI_RETRACE_GUARD

Detecta retrocesos relativos post-spike usando el rango del burst como referencia:

```
retroceso_relativo = (precio_actual - precio_post_spike) / rango_burst_total
RANGE_FRAC = DERIV_ANTI_RETRACE_RANGE_FRAC = 0.65

Si retroceso_relativo >= RANGE_FRAC → bloquear entrada
HOT_BYPASS: si score >= DERIV_ANTI_RETRACE_HOT_BYPASS_MIN_SCORE (7.5) → bypass del gate
```

---

## FVG ANCHOR (post-spike)

Cuando ocurre un spike, las coordenadas del FVG (top, bottom, mid) se congelan durante `DERIV_SPIKE_FVG_ANCHOR_TTL=600s`. El precio puede moverse sin perder la referencia de la zona.

**Tolerancia:** El anchor solo se invalida si precio cruza `fvg_top + ATR × DERIV_FVG_ANCHOR_ATR_MARGIN(1.5)`. Micro-wicks de normalización no destruyen el anchor.

**Penalizaciones:**
- Penetración en zona de tolerancia: 0 → 1.5 proporcional a `penetración/tolerancia`
- Momentum adverso (precio aún subiendo): penalty extra 0.5

---

## GHOST TRADES — MONITOREO DE GATES

Los ghost trades registran oportunidades que los gates bloquearon. Filtro de calidad: SMC_FVG/EMA200 + grade A/B + score ≥ 7.0.

**Estado gates (2026-06-10):**

| Gate | Ghosts | Ghost WR | Acción |
|---|---|---|---|
| IMMINENCE_RIPE | ~15 | 86.7% | Umbral 8.5→7.5 ✅ |
| TREND_SETUP | 62 | 78% ghost / 23% real | KEEP — sin FVG, SL antes del spike |
| SPIKE_NOT_LOADED | 17 | 76.5% | Umbral relajado ✅ |
| POST_SPIKE_STRENGTH_VETO | nuevo | — | Acumulando datos |

**Por qué ghost WR ≠ trade WR en TREND:** Sin FVG, el SL se activa antes de que el spike de mayor magnitud llegue. El 78% ghost WR es real (spikes sí ocurren) pero sin FVG el bot pierde antes de beneficiarse. No bajar TREND_SETUP_GATE.

---

## PROTOCOLO DE MODIFICACIÓN

1. **Frontend / Dashboard:** Modificar libremente tras confirmar requerimientos.
2. **Motor IA / Bot:** PROHIBIDO modificar lógica en producción sin auditar primero.
   - Toda nueva feature debe planificarse verificando tablas PostgreSQL + logs primero.
3. **Env vars:** Toda variable nueva debe añadirse a `scripts/coolify_env_guard.sh` REQUIRED_VARS array. De lo contrario, Coolify la borrará en el siguiente deploy.
4. **Deploy:** Siempre usar el comando tinker de Coolify. Verificar commit en contenedor post-deploy.

---

## REGISTRO DE CAMBIOS

### 2026-06-10/12 — Refactoring cuantitativo FASE 1-4 (commit `d9b2578`)

Implementación completa del motor matemático HFT descrito en este documento.

**Archivos modificados:**
- `src/analysis/market_geometry.py` — `_mad_ghost_prices()`, `_kinetic_state()`, nuevos campos en `GeometryResult` dataclass: `kinetic_velocity`, `kinetic_acceleration`, `kinetic_compressed`, `ghost_mad`, `fvg_bos_validated`
- `src/safety/deriv_risk.py` — `_mad_volatility()`, `_ghost_price_series()`, ghost price inyectado en regime/OLS/momentum, BOS-gated TREND, campos kinetic+BOS en score_breakdown
- `src/main_deriv.py` — `_try_load_vision_context()`, inyección burst state en score_breakdown, POST_SPIKE_COOLDOWN block, TRIFECTA tracker (4 conditions + met flag)
- `scripts/dynamic_ai_orchestrator.py` — `zona_liquidez_macro` añadido al prompt Vision LLM y al schema JSON
- `web/app/api/deriv/analytics/market-context/route.ts` — 10 nuevos campos en snapshot: kinetic_*, ghost_mad, fvg_bos_validated, trifecta_*
- `web/components/deriv-operator-console.js` — TREND_NO_STRUCT (naranja), POST_SPIKE_COOLDOWN (cyan) en setupColor/Label; `_redSetup` incluye los 3 tipos malos; `_masterRed` actualizado; fila Quant con chips KINETIC, BOS_FVG, MAD, TRIFECTA

**Variables nuevas (con defaults, no requieren env guard excepto las listadas arriba):**

| Variable | Default | Descripción |
|---|---|---|
| `DERIV_COOLDOWN_RETRACE_MIN` | 0.50 | Umbral retroceso para POST_SPIKE_COOLDOWN |
| `DERIV_PROBABILITY_THRESHOLD` | 65 | Score mínimo RNG (0-100) para pasar al Ghost LLM |
| `DERIV_VISION_FILE` | `/data/logs/deriv_vision.json` | Path archivo Vision LLM |

---

### 2026-06-10 — Bug: MATURITY_GATE_FRAC 1.80 creaba gap imposible (commit `526bc3d`)

FVG anchor TTL=600s. Con FRAC=1.80: CRASH600 threshold=1.80×442s=795s → FVG moría 195s antes de que el gate pasara. El bot veía FVG activo, lo esperaba, pero cuando llegaba la madurez el FVG ya había expirado → todo bloqueado para siempre.

Fix: Revertido a `DERIV_MATURITY_GATE_FRAC=0.70`. Thresholds resultantes: CRASH500=244s, CRASH600=310s, CRASH900=366s — todos < 600s TTL.

### 2026-06-10 — BOOM600 DRY override (commit `526bc3d`)

BOOM600 en SECO tenía score max ≈ 6.4 sin FVG (gate pedía 8.5) → bloqueado 100%. Añadido `BOOM600:6.5` a `DERIV_DRY_OVERRIDE_SCORE_MAP`.

---

### 2026-06-10 — Reaper bug: posición CRASH500 atrapada >1h (commit `b34f0a2`)

`asyncio.TimeoutError` en WebSocket durante `sell()` no era capturado por `except DerivClientError` → `self._closing.discard(cid)` nunca se llamaba → contrato bloqueado permanentemente.

Fix: Añadido bloque `except BaseException` que llama `self._closing.discard(cid)` y re-raise.

---

### 2026-06-09/10 — 3 fixes de gates (commit `54c74ed`)

1. **EXHAUSTION_GATE:** comparaba `"SELL"/"BUY"` pero Deriv usa `"MULTDOWN"/"MULTUP"` → gate siempre bloqueaba. Fix: strings corregidos.
2. **IMMINENCE_RIPE umbral:** 8.5 → 7.5 (evidencia ghost: 86.7% WR en RIPE con score≥7.0).
3. **ANTI_RETRACE_GUARD:** umbral relativo de 20 ticks implementado para evitar falsos positivos.

---

### 2026-06-09 — SESIÓN CRÍTICA: 7 bugs (commits `8a88d2d`→`16e8a61`)

1. TS build roto: clave `ema200_distance_pct` duplicada en route.ts
2. Ghost Logger nunca escribía (SPIKE_NOT_LOADED retornaba antes del logger)
3. SPIKE_NOT_LOADED bloqueaba 100% CRASH (chequeo EMA invertido, 92–95% spikes ocurren bajo EMA200)
4. FVG anchor amnesia: micro-wicks destruían el anchor → solución: tolerancia ATR×1.5
5. **CRÍTICO:** `UnboundLocalError: _is_bc_bias` — variable usada antes de definir → bot NUNCA ejecutaba una entrada desde el commit del cascade detector
6. EMA rojo post-spike bloqueaba GO cuando hay FVG anchor activo
7. `_redSetup is not defined` — crash frontal pantalla negra

---

### 2026-06-09 — MATURITY_GATE + EXHAUSTION_TRIGGER (commit `c474f83`)

- **MATURITY_GATE:** Bloquea entradas antes de `elapsed_s < P50 × 0.70`. Inmune a resets burst/cascade (usa wall-clock).
- **EXHAUSTION_TRIGGER:** En zona FVG, exige micro-momentum de reversión: `delta_5t ≤ -ATR×0.10` (MULTDOWN) o `≥ +ATR×0.10` (MULTUP).

---

### 2026-06-09 — Cascade Detection + PRESIÓN REAL (commit `9cb64c0`)

- **Cascade:** `burst_depth≥2 AND burst_retroceso=None AND gap≤60t` → `cascade_active`. Gate: `DERIV_CASCADE_ENTRY_ENABLED=false`.
- **PRESIÓN:** Score 0-10 desde Z-score+tick_velocity+range_compression+scarcity. Expuesto como `presion` en API.
- **BOOM500 habilitado** en producción (eliminado de MANUAL_ONLY).

---

### 2026-06-08 — Ghost Logger (commit `c705737`)

Sistema de auditoría de gates. Registra trades fantasma cuando un gate bloquea setup de calidad (SMC_FVG/EMA200 + A/B + score≥7.0). Outcomes: GHOST_WIN/GHOST_LOSS/GHOST_EXPIRED en ventana 600s. Persiste en `/data/logs/ghost_trades.json`.

---

### 2026-06-08 — Phase 1+2+3: Gates, Dual-Path Anchors, SEÑAL MAESTRA (commit `2d38ac5`)

- FVG static anchor: coordenadas congeladas al momento del spike
- Dual-path architecture: buffer 5m OHLC + anchors pre-spike ATR/EMA200 + Hurst nullification
- IMMINENCE_RIPE_GATE, SCARCITY_VENCIDO_GATE, STRUCTURAL_CONFLICT_GATE implementados
- SEÑAL MAESTRA semáforo en frontend con estados GO/ESPERAR/NO OPERAR

---

### 2026-06-06 — Analytics cards con deriv_market_context.json (commit previo)

Endpoint `market-context/route.ts` lee el JSON del bot directamente (3.6MB, ~10k snapshots). Reemplazó APIs que consultaban `deriv_tick_snapshots` (tabla vacía).

---

## ANÁLISIS deriv_ed_analysis.jsonl

### Sobre Deriv Boom/Crash — fundamento importante

**Deriv es proceso Poisson sintético puro.** No hay mercado financiero, no hay liquidez, no hay sesiones de trading, no hay horario europeo ni americano. Los spikes son generados por un proceso Poisson independiente del reloj real. Memorylessness: el tiempo hasta el próximo spike no depende de cuánto tiempo ha pasado desde el último.

### Schema completo: campos guardados en cada registro

```
ts              Timestamp Unix (segundos) del cierre del contrato
sym             Símbolo: BOOM500/CRASH500/BOOM600/CRASH600/BOOM900/CRASH900/BOOM1000/CRASH1000
strategy        "LADDER" (500/600s) o "K1000" (900/1000s)
stake           Stake en dólares al momento de abrir
pnl             PnL del contrato (positivo=ganancia)
win             True si pnl > 0
dur_s           Duración real del contrato en segundos
hour_utc        Hora UTC al momento de apertura (0-23)
power_open      Intensidad de actividad en ventana 30min al abrir
rec_lvl         Nivel de escalada al momento de apertura (0=normal, 1+=escalado)
had_spike       True si llegó spike durante el contrato
close_type      FLOOR / SEQUIA / BROKER / TIMER / SPIKE_HOLD (desde deploy ~julio 2026)
spikes_during_contract  Número de spikes que llegaron durante el contrato
open_gap_s      Segundos desde el último spike al ABRIR el contrato
open_gap_prev_s Segundos del intervalo anterior al último spike (ritmo)
open_gap_3rd_s  Segundos del tercer intervalo hacia atrás
open_n30/60/120/180/240  Número de spikes en ventanas 30/60/120/180/240 minutos al abrir
open_max_r30/60/120      Ratio máximo de spike en ventana 30/60/120min al abrir
open_med_r30/60/120      Ratio mediano de spike en ventana 30/60/120min al abrir
open_cross_n10/n30       Total spikes en TODOS los símbolos en ventana 10/30min al abrir
open_syms_in_sequia      Cuántos otros símbolos llevan >5min sin spike al abrir
close_gap_s / close_gap_prev_s / close_gap_3rd_s   (mismos que open_* pero al CERRAR)
close_n30..n240 / close_max_r* / close_med_r*      (mismos que open_* pero al CERRAR)
```

### Cómo correr análisis en servidor

```bash
ssh coolify-server "python3 << 'PYEOF'
import json
records = []
with open('/data/deriv-logs/deriv_ed_analysis.jsonl') as f:
    for line in f:
        try: records.append(json.loads(line.strip()))
        except: pass
# filtrar por campo completo: solo registros con open_gap_s
full = [r for r in records if 'open_gap_s' in r and 'open_n30' in r]
print('Total:', len(records), '| Con contexto open:', len(full))
PYEOF"
```

Registros con stake=$0 (~107) son inválidos: el contrato abrió antes de que `_ed_save_open` corriera. Filtrar siempre por `open_gap_s` o `stake > 0`.

### Reglas de análisis — NUNCA inventar

1. Reportar n exacto de cada subgrupo. Si n < 30, señalarlo como muestra insuficiente.
2. Separar siempre LADDER de K1000 — estrategias distintas, escaladas distintas.
3. Separar rec_lvl=0 de rec_lvl>=1. El escalado destruye cualquier señal.
4. No mencionar horarios de mercado, liquidez, ni sesiones. Esto es Poisson sintético.
5. Reportar spike%, PnL, y n para cada bucket. No reportar solo uno de los tres.
6. Señal real = señal que predice PnL positivo, no solo que predice spike arrival.

### Hallazgos verificados — muestra ~360 registros (2026-08-03)

**rec_lvl: hallazgo más importante**
- rec_lvl=0 (estado normal): WR=56%, PnL=+42 → único estado rentable
- rec_lvl=1: WR=13%, catastrófico
- rec_lvl>=2: WR=13%, catastrófico
- El escalado en K1000 destruye todo el PnL. La señal de "cuándo operar" aplica solo a rec_lvl=0.

**spikes_during_contract (campo nuevo)**
- 0 spikes durante contrato: WR=17%, PnL=-119 → siempre pierde
- 1 spike durante contrato: WR=100%, PnL=+31
- 2+ spikes: WR=100%, PnL positivo
- Señal definitoria: si el spike no llega, siempre se pierde. (Obvio en retrospectiva, pero confirma que la estrategia depende 100% de la llegada del spike.)

**close_type (campo nuevo)**
- FLOOR: 100% WR, PnL=+23 → única fuente real de ganancias en LADDER
- SPIKE_HOLD: 86% WR, PnL=+7 → K1000 capturando spike
- BROKER: 39% WR, PnL=-3
- SEQUIA: 0% WR, PnL=-34 → siempre pierde
- TIMER: 0% WR, PnL=-34 → siempre pierde

**delta_n30 = close_n30 - open_n30** (campo calculado, no directo)
- delta subió: 100% WR, PnL=+143
- delta bajó: 0-39% WR, PnL=-135
- No es gate de entrada (se mide al cerrar), pero confirma que la sequía creciente causa las pérdidas.

**Paradoja Poisson P_teo = 1 - exp(-lambda_30 * dur_s)**
- P_teo<40%: spike_real=100%, PnL=+97 → MÁS CONFIABLE que cuando Poisson dice "seguro"
- P_teo>80% + dur_s<600s: spike=100%, PnL=+8 → ok
- P_teo>80% + dur_s>600s: spike=75%, PnL=-79 → DESTRUCTOR
- Explicación: dur_s largo = contrato en sequía activa = K1000 escalado perdiendo
- El modelo Poisson no agrega valor. La duración del contrato es mejor señal que P_teo.

**Lambda ratio: n30/1800 vs n120/7200 (tasa reciente vs tasa 2h)**
- Estable (0.8-1.2x): spike=78%, PnL=-3 → mejor de los 5 buckets
- Enfriando fuerte (<0.5x): spike=66%, PnL=-68 → peor
- Acelerando fuerte (>2x): spike=57%, PnL=-23 → malo
- Nota: todos los buckets tienen PnL negativo porque incluyen escalados K1000

**Señal compuesta fuerte: rec_lvl=0 + cross_n30 entre 10-40**
- n=45, spike=91%, PnL=+47
- BOOM500 100% spike, CRASH500 100%, CRASH600 100%
- 900s/1000s en este subgrupo: resultados pobres (n pequeño)

**Intensidad spikes: max_r30 / med_r30 > 5x (hubo spike enorme recientemente)**
- n=17, spike=76%, PnL=+19 → señal positiva
- Uniforme (<2x): spike=69%, PnL=-44

**gap × n30 → spike arrival** (señal de entrada)
- Mejor ventana: gap 3-20min + n30=1-5 → spike llega 73-85%
- gap >20min: spike llega 50-63%, PnL siempre negativo
- gap <3min + n30 alto (>=6): spike llega 60%, peor de lo esperado

**Ritmo (gap_actual vs gap_prev)**
- Desacelerando (gap > 140% prev): PnL=-87 aunque spike llega 70% — DESTRUCTOR
- Acelerando (gap < 70% prev): PnL=-2, neutral

**3 gaps consecutivos (gap_3rd > gap_2 > gap_1 = bajando = acelerando)**
- Ningún patrón claro. PnL negativo en todos los buckets.

**open_syms_in_sequia (otros símbolos en sequía)**
- 6-7 símbolos en sequía: WR bueno (n pequeño, no estadísticamente sólido)
- Patrón inconsistente, necesita más datos (n=69 registros con este campo)

### Campos NO analizados / pendientes

- open_n180, open_n240 (pocas variaciones, separar de n30)
- open_max_r60, open_max_r120, open_med_r60, open_med_r120
- close_gap_prev_s, close_gap_3rd_s, close_n60-n240, close_max/med_r*
- Señal cross_n30 vs cross_n10 separada (solo 69 registros)
- Patrones por hora UTC (Poisson puro → no debería importar, pero validar)

### Cuándo hacer el próximo análisis

**Mínimo n=500 registros con campo open_gap_s** para:
- Validar matriz gap×n30 con >30 en cada celda
- Analizar cross_n30 con suficiente muestra
- Separar por símbolo individualmente con n>=30 por símbolo
- Buscar combinación gate de 3 variables (rec_lvl=0 + gap + n30 + lambda_ratio)

**Mínimo n=1000** para proponer un gate real con confianza estadística.
