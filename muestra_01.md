# MUESTRA_01 — HISTORIAL OPERATIVO COMPLETO DEL PROYECTO

> Este archivo es el registro maestro de cada muestra de datos tomada durante el desarrollo
> del bot OptiFerre-Trader. Se actualiza al inicio de cada nueva muestra.
> Es la fuente de verdad para cualquier IA que retome el trabajo.

---

# ==========================================================================================
# ⚠️  PROTOCOLO DE CAMBIOS — LEE ESTO ANTES DE TRABAJAR EN ESTE PROYECTO ⚠️
# ==========================================================================================
#
#  EL BOT CORRE EN COOLIFY. TODOS LOS CAMBIOS VAN POR GIT → COOLIFY → CONTENEDOR.
#  LAS VARIABLES DE ENTORNO VAN EN COOLIFY UI, NO EN ARCHIVOS DEL REPO.
#  EL WATCHDOG Y LOS ALERTAS TELEGRAM DEBEN ACTUALIZARSE EN CADA CAMBIO DE LÓGICA.
#
#  VER AI_HANDOFF_PROMPT.md SECCIÓN "PROTOCOLO OPERACIONAL OBLIGATORIO" PARA DETALLE.
#
# ==========================================================================================

---

## MUESTRA 6 — EN CURSO (desde 24-05-2026 UTC)

### Estado de arranque

| Componente                | Estado           |
|---------------------------|------------------|
| Bot principal             | running / healthy |
| AI Sidecar               | running / healthy |
| Watchdog host             | PASS (12/12)     |
| DB deriv_contracts        | 0 filas          |
| DB deriv_tick_snapshots   | 0 filas          |
| DB ai_entry_pattern_memory| 0 filas (limpia) |
| DB spike_events           | 0 filas          |
| deriv_spike_events.json   | [] (reseteado)   |
| deriv_closed_contracts.json | [] (reseteado) |
| deriv_ai_decisions.json   | [] (reseteado)   |
| deriv_lockout.json        | {} (reseteado)   |

### Bootstrap

- Bootstrap marker: `/data/logs/sample6_bootstrap_20260524T000140Z.json`
- Archivo de archivo pre-reset: `/data/logs/archive_muestra6_start_20260524T000124Z/`

### Objetivo de la muestra 6

Capturar operaciones y spikes desde cero con:
- Memoria de patrones IA limpia (ai_entry_pattern_memory truncada)
- Sin contaminación del historial de spikes de muestras anteriores
- Sin contaminación de operaciones cerradas previas
- El warmup de 1000 ticks ocurrió ANTES del reset de archivos JSON para no contaminar

### Variables de entorno activas (Coolify)

```
DERIV_ESCAPE_VALVE=false
DERIV_BLOCK_BC_ESCAPE_BOOM600=  (UNSET)
DERIV_BLOCK_BC_ESCAPE_BOOM900=  (UNSET)
LOGS_DIR=/data/logs
```

---

## MUESTRA 5 — COMPLETADA (23-05-2026 UTC)

### Resumen ejecutivo

Muestra de operaciones con el stack Deriv completo (bot + IA sidecar + guardrails dinámicos).
Documentó el comportamiento del sistema en fase-6 con:
- Filtro IA adaptativo por símbolo
- Memoria de patrones persistente en DB
- Guardrails de score ampliados
- Dashboard live de calidad IA y patrones

### Estado al cierre

- Backup pre-reset: `/data/deriv-logs/archive_stage5_restart_20260523_165659`
- Bootstrap muestra 5: `/data/deriv-logs/sample5_bootstrap_20260523_165859.json`
  - open=1, closed=0, spikes=16, ai_decisions=1
  - dynamic_config.enabled=true, dynamic_symbols_loaded=8

### Archivos reseteados para muestra 5

- deriv_closed_contracts.json → []
- deriv_open_contracts.json → []
- deriv_spike_events.json → []
- deriv_spikes.json → []
- deriv_ai_decisions.json → []
- deriv_lockout.json → {}
- deriv_status.json → {}
- dynamic_ai_config_diffs.jsonl → vacío

### Variables de entorno en fase-5

```
DERIV_BLOCK_BC_ESCAPE_BOOM600=false
DERIV_BLOCK_BC_ESCAPE_BOOM900=false
```

---

## HISTORIAL DE SESIONES DE DESARROLLO

### Sesión 24-05-2026 — Correcciones post-muestra 5 y arranque muestra 6

**Problemas detectados y corregidos:**

#### 1. AI Sidecar heartbeat (commit `03d96ee`)
- **Problema:** AI Sidecar tiene loop de 1200s pero healthcheck tenía max_diff_age=900s.
  La hysteresis bloqueaba todos los cambios de config → sidecar no escribía nada →
  diff_log se quedaba viejo → contenedores pasaban a unhealthy.
- **Solución:** Añadir heartbeat explícito en `dynamic_ai_orchestrator.py` que escribe
  al diff_log en CADA iteración aunque hysteresis bloquee los cambios de config.
  Subir max_diff_age en `Dockerfile.deriv` HEALTHCHECK a 1500s.
- **Archivos:** `scripts/dynamic_ai_orchestrator.py`, `Dockerfile.deriv`

#### 2. Watchdog advice text desactualizado (commit `4407b5a`)
- **Problema:** Watchdog enviaba alerta a Telegram diciendo que `ESCAPE_VALVE=true` era
  requerido (texto fase-5). En fase-6 el valor correcto es `false` y los BLOCK_BOOM
  deben estar UNSET, no `=false`.
- **Solución:** Actualizar `_advice_for()` en `deriv_runtime_watchdog.py` con texto fase-6.
- **Archivos:** `scripts/deriv_runtime_watchdog.py`

#### 3. Watchdog subprocess no pasaba --max-diff-age-sec (commit `52395fa`)
- **Problema:** El watchdog del HOST (`/opt/deriv-watchdog/`) llamaba al validator via
  subprocess sin pasar `--max-diff-age-sec 1500`. Usaba el default de 900s → siempre
  fallaba el check diff_log_freshness → alertas spam a Telegram.
- **Solución:** Hardcodear `--max-diff-age-sec 1500` en `_run_validator()` del watchdog.
- **Archivos:** `scripts/deriv_runtime_watchdog.py`, `/opt/deriv-watchdog/deriv_runtime_watchdog.py`
- **Verificación:** Watchdog manual → `{"status":"PASS","alert_reason":"recovery_fail_to_pass","telegram":"ok"}`

**Lecciones aprendidas:**
- El watchdog host NO es gestionado por Coolify. Cambios ahí son manuales via scp.
- Cada cambio de lógica de validación requiere actualizar TRES lugares:
  1. `scripts/deriv_runtime_watchdog.py` (repo)
  2. `scripts/validate_deriv_runtime.py` (repo)
  3. `/opt/deriv-watchdog/` en el servidor (manual scp)
- NUNCA cerrar una sesión sin verificar que el watchdog host está en PASS.

**Estado final verificado:**
```
DERIV_RUNTIME_VALIDATOR PASS  (12/12 OK)
/o4w1ns4cceccmn2ozqt7sol2     running health=healthy
/o4w1ns4cceccmn2ozqt7sol2-ai  running health=healthy
Watchdog: {"status":"PASS","alert_reason":"recovery_fail_to_pass","telegram":"ok"}
```

---

### Sesión 23-05-2026 — Arranque muestra 5 y reset DB

**Objetivo:** Iniciar muestra 5 limpia sin historial contaminado de muestras anteriores.

**Acciones:**
- Creada tabla `spike_events` (migración 007 aplicada desde contenedor via asyncpg)
- Backup 129 filas de `ai_entry_pattern_memory` → `ai_entry_pattern_memory_muestra5_backup`
- Truncado `ai_entry_pattern_memory` → 0 filas
- `deriv_contracts`, `deriv_tick_snapshots` ya en 0
- Bot reiniciado → `ai_reason=—`, `ai_conf=0.00` (memoria IA limpia confirmada)
- Deploy limpio: validación release-gate 12/12 PASS

**Cambios en código:**
- Migración 011: `ai_entry_pattern_memory` + score guardrail en DB
- `src/analysis/deriv_analyst.py`: filtro IA adaptativo por símbolo
- `scripts/dynamic_ai_orchestrator.py`: orquestador con hysteresis
- Frontend: dashboard con calidad IA y memoria de patrones live

---

### Sesiones anteriores (muestras 1-4)

Documentadas en `MUESTRA_01_ANALISIS_FORENSE.md`.

---

## ARQUITECTURA DEL STACK DERIV (resumen)

```
┌──────────────────────────────────────────────────────────────────┐
│  COOLIFY (panel.datovatenexuspro.com)                            │
│                                                                  │
│  ┌─────────────────────────────────┐  ┌──────────────────────┐  │
│  │  Bot Principal                  │  │  AI Sidecar          │  │
│  │  o4w1ns4cceccmn2ozqt7sol2       │  │  o4w1ns4cceccmn2..ai │  │
│  │  src/main_deriv.py              │  │  scripts/dynamic_ai_ │  │
│  │  src/execution/deriv_trader.py  │  │  orchestrator.py     │  │
│  │  src/analysis/deriv_analyst.py  │  │  Loop: 1200s         │  │
│  │  src/safety/deriv_risk.py       │  │  Escribe heartbeat   │  │
│  └──────────┬──────────────────────┘  └──────────────────────┘  │
│             │ /data/logs (volumen compartido)                    │
│  ┌──────────▼──────────────────────┐                            │
│  │  PostgreSQL 10.0.1.8:5432       │                            │
│  │  db: optiferre_pamm             │                            │
│  │  tablas clave:                  │                            │
│  │    deriv_contracts              │                            │
│  │    deriv_tick_snapshots         │                            │
│  │    ai_entry_pattern_memory      │                            │
│  │    spike_events                 │                            │
│  │    dynamic_symbol_config        │                            │
│  └─────────────────────────────────┘                            │
│                                                                  │
│  HOST (192.81.216.49)                                           │
│    /opt/deriv-watchdog/                  ← cron cada 5 min      │
│    /etc/cron.d/deriv-runtime-watchdog    ← cron entry           │
│    /data/deriv-logs/deriv_runtime_watchdog_state.json           │
└──────────────────────────────────────────────────────────────────┘
```

---

## ARCHIVOS CLAVE DEL PROYECTO

| Archivo | Propósito |
|---------|-----------|
| `src/main_deriv.py` | Loop principal del bot Deriv |
| `src/analysis/deriv_analyst.py` | Análisis técnico + IA adaptativa |
| `src/execution/deriv_trader.py` | Ejecución de contratos Deriv |
| `src/execution/position_manager.py` | Gestión de posiciones |
| `src/safety/deriv_risk.py` | Guardrails de riesgo |
| `scripts/dynamic_ai_orchestrator.py` | Orquestador IA sidecar (hysteresis + LLM) |
| `scripts/deriv_runtime_watchdog.py` | Watchdog con alertas Telegram |
| `scripts/validate_deriv_runtime.py` | Validador de runtime (12 checks) |
| `scripts/deriv_release_gate_remote.sh` | Gate de release: valida antes de cerrar sesión |
| `Dockerfile.deriv` | Imagen del bot principal (HEALTHCHECK incluido) |
| `db/migrations/` | Migraciones de PostgreSQL |
| `AI_HANDOFF_PROMPT.md` | Protocolo completo para cualquier IA que retome el trabajo |

---

## VALIDACIONES DE CIERRE DE SESIÓN

Antes de cerrar cualquier sesión de trabajo:

```bash
# 1. Release gate completo
bash scripts/deriv_release_gate_remote.sh

# 2. Estado de contenedores
sshpass -p 'R3cov3ry-N3xus!' ssh root@192.81.216.49 \
  "docker ps --format '{{.Names}} {{.Status}}' | grep o4w1"

# 3. Watchdog manual (si hubo cambios de lógica)
sshpass -p 'R3cov3ry-N3xus!' ssh root@192.81.216.49 \
  "bash /opt/deriv-watchdog/run_watchdog.sh"
# Debe retornar: {"status":"PASS", ...}
```


## Observaciones no bloqueantes

- Next.js muestra warnings conocidos de runtime/edge y trazado, pero no impiden compilacion ni despliegue.
- Aviso de API key de email en build (no bloqueante para el dashboard).

## Referencia forense completa

Ver analisis completo en MUESTRA_01_ANALISIS_FORENSE.md.

## Reinicio limpio Prueba 6 (23-05-2026 UTC)

Objetivo operativo:

- iniciar Fase 6 en runtime estable,
- bajar operaciones heredadas en linea,
- evitar falsos FAIL de watchdog,
- empezar muestra real hacia 100 operaciones cerradas.

### Incidente recibido en Telegram

- Alerta: `DERIV WATCHDOG EN FAIL`
- Motivo: `transition_pass_to_fail`
- Check fallido: `db_guardrails`
- Detalle: `rows=8, bad_score>0` por filas con `score_min_override=9.2`.

### Acciones ejecutadas (sin cambiar logica del bot)

1. Se cerraron manualmente las operaciones abiertas en broker para limpiar exposicion heredada.
2. Se corrigio estado DB para watchdog:
  - clamp operativo de `score_min_override` a `<=8.0` en `dynamic_symbol_config`.
3. Se alineo entorno de runtime en produccion (archivo `.env` de Coolify):
  - `DYNAMIC_AI_SCORE_MAX_GUARDRAIL=8.0`
  - `DYNAMIC_AI_SCORE_MAX_DB_COMPAT_FALLBACK=8.0`
4. Se archivo telemetria previa y se reinicio baseline de muestra:
  - backup: `/data/deriv-logs/archive_phase6_20260523T205249Z`
  - reset: `deriv_open_contracts.json`, `deriv_closed_contracts.json`, `dynamic_ai_config_diffs.jsonl`, `dynamic_ai_snapshots.jsonl`, `deriv_runtime_watchdog.log`
  - marker: `/data/deriv-logs/phase6_bootstrap_marker.json`
5. Reinicio completo del stack de trading:
  - contenedor bot `o4w1ns4cceccmn2ozqt7sol2` recreado,
  - contenedor IA `o4w1ns4cceccmn2ozqt7sol2-ai` recreado y saludable.

### Validacion post-reinicio

- Release gate remoto: PASS total.
- Watchdog manual: PASS (`db_guardrails` en verde).
- Estado de contenedores: bot healthy + ia healthy.
- IA activa y aprendiendo:
  - ciclos `dynamic-ai` con histeresis,
  - eventos `DIFF` en `dynamic_ai_config_diffs.jsonl`.

### Nota critica para analisis (sin cambio funcional)

- El bot hace precarga de 1000 ticks al arranque (`DERIV_HISTORY_TICKS=1000`).
- Esa precarga puede contaminar analisis tempranos de spikes al inicio de muestra.
- Regla de lectura para Fase 6:
  - tratar la ventana inicial post-boot como warmup,
  - no mezclar conclusiones de rendimiento temprano con el tramo estable de muestra.

### Baseline de muestra 6

- Inicio operativo fase 6: 2026-05-23T20:52Z (aprox).
- Contador actual al cierre de esta intervencion:
  - `open_count=3`
  - `closed_count=2`
- Objetivo de seguimiento: completar 100 operaciones cerradas reales desde este reinicio de fase.

### Reset de spikes para vigilancia clara (23-05-2026 UTC)

- Se reiniciaron en limpio (sin borrar historial, con backup previo) los archivos:
  - `deriv_spike_events.json`
  - `deriv_spikes.json`
- Backup generado:
  - `/data/deriv-logs/archive_phase6_reset_20260523T210250Z_deriv_spike_events.json`
  - `/data/deriv-logs/archive_phase6_reset_20260523T210250Z_deriv_spikes.json`
- Estado post-reset verificado:
  - `deriv_spike_events.json` = `[]` (count=0)
  - `deriv_spikes.json` = `[]` (count=0)
