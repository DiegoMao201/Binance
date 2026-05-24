# Prompt de Handoff Completo: OptiFerre-Trader

Usa este documento como contexto base para continuar el desarrollo de este proyecto con otra IA. La meta es que puedas retomar el trabajo sin perder decisiones previas, sin romper la arquitectura actual y sin reabrir problemas ya resueltos.

---

## ACTUALIZACION MUESTRA 07 (24-05-2026 UTC)

Estado live mas reciente:
- Commit bot/sidecar desplegado: `2199f12`.
- Contenedores bot y sidecar healthy en Coolify.
- Release gate remoto: PASS (post warm-up).

Cambio funcional clave aplicado:
- `scripts/dynamic_ai_orchestrator.py` ahora incluye regla explicita de recuperacion en regimen estable:
  - Si `0.8 <= Ratio <= 1.2`, relajar cuarentena y bajar `score_min_override` gradualmente hacia base `6.80`.
  - Ejemplo esperado: `8.00 -> 7.20 -> 6.80`.
- Adicionalmente, la logica local del sidecar implementa ese descenso por pasos en rama estable.

Regla de oro preservada:
- NO tocar logica de ticks ni `post_spike_chase_guard`.
- El ajuste es solo sobre el muro de `score_min_override` cuando el mercado ya esta estable.

Reset oficial para inicio de muestra 07:
1. Flatten broker (open contracts: 0 -> 0).
2. Stop bot y sidecar.
3. Reset JSON en `/data/deriv-logs`:
   - `deriv_spike_events.json=[]`
   - `deriv_closed_contracts.json=[]`
   - `deriv_open_contracts.json=[]`
   - `deriv_ai_decisions.json=[]`
   - `deriv_spikes.json=[]`
   - `deriv_market_context.json=[]`
   - `deriv_lockout.json={}`
4. Start bot + sidecar para comenzar captura operativa.

Nota operativa:
- Primer gate tras start puede fallar transitoriamente por `missing/invalid last_refresh` en cold-start.
- Revalidar de inmediato; en este ciclo paso a PASS en la siguiente corrida.

## ACTUALIZACION LIVE CRITICA (24-05-2026 UTC)

Estado operativo confirmado en produccion:
- Commit live bot y frontend: `8a9e58e`.
- Bot y sidecar healthy en Coolify.
- Release gate remoto en PASS.

Hallazgo clave de bloqueo (ULTIMAS 6H, `deriv_spike_events.json`):
- blocked_total: 454
- blocked_no_chase: 117
- Distribucion de bloqueos NO chase:
  - `trade_cooldown`: 32
  - `AI_VETO`: 24
  - `dynamic_symbol_inactive`: 22
  - `post_spike_strength_veto`: 22
  - `spike_forced_dir`: 16

Importante:
- En 30m y 1h el bloqueo dominante sigue siendo `post_spike_chase_guard`.
- Ese guardrail esta basado en ticks post-spike y SE MANTIENE por diseno.
- El problema actual no es abrir la puerta post-spike, sino la dureza de guardrails posteriores cuando la puerta ya se abre.

Configuracion dinamica observada en DB (`dynamic_symbol_config`):
- `score_min_override` alto en casi todos los simbolos (muchos en `8.0`).
- Ejemplos recientes de veto por `post_spike_strength_veto` con scores entre `6.5` y `7.1`.

Micro-flexibilizacion recomendada (sin perder robustez):
1. No tocar `post_spike_chase_guard`.
2. Relajar score minimo dinamico en `-0.5` donde hoy esta en `8.0` (pasar a `7.5`).
3. Mantener guardrail inferior global en `5.5` (sin bajar mas).
4. No quitar `AI_VETO`; solo permitir que setups de score medio-alto no se descarten tan pronto.

Aplicado en vivo (24-05-2026 UTC):
- `dynamic_symbol_config.score_min_override` ajustado en 6 simbolos:
  - `BOOM500: 8.0 -> 7.5`
  - `BOOM600: 8.0 -> 7.5`
  - `CRASH1000: 8.0 -> 7.5`
  - `CRASH500: 8.0 -> 7.5`
  - `CRASH600: 8.0 -> 7.5`
  - `CRASH900: 8.0 -> 7.5`
- Sin cambios en:
  - `BOOM1000: 6.5`
  - `BOOM900: 7.85`
- Validacion posterior: release gate remoto `PASS`.

Objetivo de esta relajacion:
- Subir conversion post-spike sin abrir entradas de baja calidad.
- Evitar sobre-filtrado en fase de confirmacion tardia cuando ya paso la proteccion de chase.


# ==========================================================================================
# ⚠️  PROTOCOLO OPERACIONAL OBLIGATORIO — LEE ESTO ANTES DE HACER CUALQUIER CAMBIO ⚠️
# ==========================================================================================
#
#  ESTE BOT CORRE EN COOLIFY. COOLIFY ES LA ÚNICA FUENTE DE VERDAD EN PRODUCCIÓN.
#  TODA IA QUE TRABAJE EN ESTE PROYECTO DEBE CONOCER Y RESPETAR ESTE PROTOCOLO.
#
# ==========================================================================================
#
#  1. DÓNDE VIVE EL BOT
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  - El bot principal corre en Coolify: https://panel.datovatenexuspro.com/
#  - Servidor: root@192.81.216.49
#  - Los contenedores son gestionados 100% por Coolify via Docker.
#  - El código fuente está en GitHub: github.com/DiegoMao201/Binance (rama main).
#  - Coolify detecta commits en main y hace auto-deploy cuando está configurado.
#  - NO se editan archivos directamente en el servidor como solución permanente.
#    Se puede parchear en emergencia pero el cambio DEBE ir al repo.
#
#  2. VARIABLES DE ENTORNO — VAN EN COOLIFY, NO EN ARCHIVOS
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  - TODAS las variables de entorno del bot se configuran en Coolify UI.
#  - NO se deben poner en .env en producción ni hardcodear en scripts.
#  - Las variables clave de fase-6 son:
#      DERIV_ESCAPE_VALVE=false                   (false = activo, no bypass)
#      DERIV_BLOCK_BC_ESCAPE_BOOM600=             (UNSET = no bloquear)
#      DERIV_BLOCK_BC_ESCAPE_BOOM900=             (UNSET = no bloquear)
#      LOGS_DIR=/data/logs
#  - Para ver las variables actuales: Coolify > Servicio > Environment
#  - Cambiar una variable en Coolify requiere redeploy del contenedor.
#
#  3. CÓMO HACER UN CAMBIO CORRECTAMENTE
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  PASO 1: Editar el archivo en el repo local (VSCode).
#  PASO 2: git add + git commit + git push origin main
#  PASO 3: Coolify detecta el commit y despliega automáticamente (o hacer redeploy manual).
#  PASO 4: Verificar que los contenedores volvieron a "healthy".
#  PASO 5: Correr el release-gate: bash scripts/deriv_release_gate_remote.sh
#
#  NO HAGAS:
#  - No edites scripts dentro del contenedor directamente como solución final.
#  - No cambies variables de entorno en archivos del repo (van en Coolify UI).
#  - No hagas docker build/push manual para producción (Coolify lo hace).
#  - No dejes el bot local y el bot remoto corriendo simultáneamente.
#
#  4. ⚠️  REGLA CRÍTICA: EL WATCHDOG Y TODOS LOS ALERTAS TELEGRAM DEBEN ACTUALIZARSE
#  ─────────────────────────────────────────────────────────────────────────────────────────
#
#  CADA VEZ QUE CAMBIAS LÓGICA, VARIABLES O COMPORTAMIENTO DEL BOT,
#  DEBES ACTUALIZAR LOS SIGUIENTES ARCHIVOS PARA EVITAR FALSAS ALERTAS:
#
#    scripts/deriv_runtime_watchdog.py  → función _advice_for() y _run_validator()
#    scripts/validate_deriv_runtime.py  → lógica de validación de env vars
#    /opt/deriv-watchdog/ (HOST)        → copia del watchdog que corre en cron cada 5 min
#
#  El watchdog corre en el HOST del servidor (no en el contenedor) via cron:
#    /etc/cron.d/deriv-runtime-watchdog  → cada 5 minutos, llama a run_watchdog.sh
#    /opt/deriv-watchdog/run_watchdog.sh → lanza deriv_runtime_watchdog.py
#    /opt/deriv-watchdog/deriv_runtime_watchdog.py → envía alertas a Telegram
#    /data/deriv-logs/deriv_runtime_watchdog_state.json → estado PASS/FAIL persistido
#
#  Después de actualizar el watchdog:
#    1. scp el archivo al servidor: /opt/deriv-watchdog/
#    2. Correr manualmente para verificar: bash /opt/deriv-watchdog/run_watchdog.sh
#    3. Confirmar que el resultado es {"status":"PASS"} antes de cerrar la sesión.
#    4. Si había un FAIL previo, el watchdog enviará "recovery_fail_to_pass" a Telegram.
#
#  NO CERRAR UNA SESIÓN DE TRABAJO SIN VERIFICAR QUE EL WATCHDOG ESTÁ EN PASS.
#  UN WATCHDOG EN FAIL GENERA ALERTAS SPAM EN TELEGRAM CADA 5 MINUTOS.
#
#  5. DEPLOY LIMPIO — CHECKLIST RÁPIDO
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  [ ] git push origin main (cambio en repo)
#  [ ] Coolify redeploy completado (contenedores healthy)
#  [ ] bash scripts/deriv_release_gate_remote.sh → PASS
#  [ ] scp watchdog actualizado a /opt/deriv-watchdog/ si cambió lógica de validación
#  [ ] bash /opt/deriv-watchdog/run_watchdog.sh → {"status":"PASS"}
#  [ ] Telegram no recibe alertas falsas (esperar 10 min post-deploy)
#
#  6. ESTRUCTURA DE CONTENEDORES
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  Contenedor bot:       o4w1ns4cceccmn2ozqt7sol2       (Python bot principal)
#  Contenedor IA:        o4w1ns4cceccmn2ozqt7sol2-ai    (Orquestador IA sidecar)
#  Contenedor frontend:  (Next.js - nombre en Coolify)   (Dashboard web)
#  Base de datos:        PostgreSQL en 10.0.1.8:5432, db=optiferre_pamm
#  Logs compartidos:     HOST=/data/deriv-logs  |  CONTENEDOR BOT=/data/logs
#                        (el frontend published lee /data/deriv-logs)
#
#  7. MUESTRAS Y DATOS DE ANÁLISIS
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  Las "muestras" son períodos de operación limpia para análisis forense.
#  Para iniciar una nueva muestra:
#    a) Truncar en DB: deriv_contracts, deriv_tick_snapshots, ai_entry_pattern_memory
#    b) Resetear JSON en /data/deriv-logs (host):
#         deriv_spike_events.json → []
#         deriv_closed_contracts.json → []
#         deriv_ai_decisions.json → []
#         deriv_lockout.json → {}
#    c) Crear bootstrap marker: /data/deriv-logs/sample{N}_bootstrap_{STAMP}.json
#    d) NO hacer restart del bot si ya está running y el warmup ya ocurrió.
#       El warmup de 1000 ticks es necesario para indicadores pero sus spikes
#       contaminan la muestra si se guardan en el archivo JSON ANTES del reset.
#       Respetar el orden: reset DESPUÉS de que el bot ya cargó su warmup.
#    e) Actualizar muestra_01.md con el estado del nuevo inicio.
#
#  7B. REINICIO TOTAL DEL FRONTEND (RESET REAL DE SPIKES Y OPERACIONES)
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  IMPORTANTE: EL BOTÓN "RESET·S" DEL HUD ES SOLO VISUAL.
#  SOLO MUEVE EL INICIO DE SESIÓN EN deriv_session.json.
#  NO BORRA deriv_spike_events.json NI deriv_closed_contracts.json.
#
#  SI QUIERES EMPEZAR UNA MUESTRA LIMPIA DE VERDAD, DEBES HACER ESTE RESET:
#
#  A) RESET DE ARCHIVOS JSON EN /data/deriv-logs (HOST)
#     - deriv_spike_events.json   -> []
#     - deriv_closed_contracts.json -> []
#     - deriv_open_contracts.json -> []
#     - deriv_ai_decisions.json   -> []
#     - deriv_lockout.json        -> {}
#     - deriv_spikes.json         -> []
#     - deriv_market_context.json -> []
#     - deriv_session.json        -> session_start_ts = now
#
#  B) RESET DE TABLAS ANALÍTICAS EN DB
#     - TRUNCATE deriv_contracts
#     - TRUNCATE spike_events
#     - TRUNCATE deriv_tick_snapshots
#
#  C) CREAR MARKER DE RESET
#     - /data/deriv-logs/sample6_reset_marker_{STAMP}.json
#
#  D) ACLARACIÓN DE HORARIOS
#     - El dashboard muestra hora local del navegador.
#     - El servidor usa UTC.
#     - Si ves "23/5" en pantalla y el reset fue "24/5 UTC", puede ser el
#       MISMO evento visto en otra zona horaria, no datos viejos.
#
#  E) REGLA OPERATIVA
#     - DESPUÉS DEL RESET, TODO LO QUE APARECE ES NUEVO EN VIVO.
#     - Si vuelven a salir spikes en minutos, no es contaminación: es mercado real.
#
#  7C. MUESTRA 6 — RESET FINAL ATÓMICO (CANÓNICO)
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  EJECUTADO: 20260524T011540Z (UTC)
#
#  SECUENCIA CANÓNICA OBLIGATORIA:
#  1) DETENER BOT + IA.
#  2) BACKUP DE JSON EN /data/deriv-logs/archive_muestra6_final_atomico_{STAMP}/
#  3) LIMPIAR JSON EN /data/deriv-logs:
#       deriv_spike_events.json=[]
#       deriv_closed_contracts.json=[]
#       deriv_open_contracts.json=[]
#       deriv_ai_decisions.json=[]
#       deriv_lockout.json={}
#       deriv_spikes.json=[]
#       deriv_market_context.json=[]
#  4) TRUNCAR DB:
#       TRUNCATE deriv_contracts RESTART IDENTITY CASCADE
#       TRUNCATE spike_events RESTART IDENTITY CASCADE
#       TRUNCATE deriv_tick_snapshots RESTART IDENTITY CASCADE
#  5) CREAR MARKER FINAL:
#       /data/deriv-logs/muestra6_FINAL_ATOMICO_{STAMP}.json
#  6) LEVANTAR BOT + IA Y VERIFICAR HEALTHY.
#
#  RESULTADO ESPERADO INMEDIATO:
#  - JSON EN CERO
#  - DB EN CERO
#  - lockout {}
#  - FRONTEND ACTIVO ÚNICO (SIN DUPLICADOS RUNNING)
#
#  REGLA DE ORO:
#  - SI APARECEN SPIKES/ÓRDENES DESPUÉS DEL MARKER FINAL, ES TRÁFICO NUEVO.
#  - NUNCA CONFUNDIR DATOS NUEVOS CON "BASURA DE DEPLOY" SI EL RESET ATÓMICO YA PASÓ.
#
#  7D. IA DINÁMICA: AUTORIDAD REAL SOBRE ENTRADAS (2026-05-24)
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  - EL SIDECAR IA SÍ MANDA SOBRE EL UMBRAL DE ENTRADA POR SÍMBOLO:
#      dynamic_symbol_config.score_min_override
#  - EL BOT REFRESCA ESA CONFIG EN CALIENTE Y LA USA COMO GATE DINÁMICO.
#  - EL LOOP IA QUEDÓ MÁS RÁPIDO (POR DEFECTO 500s, MÍNIMO 120s), YA NO 1200s FIJOS.
#  - SE RELAJÓ EL MÍNIMO DINÁMICO PARA ACTIVIDAD CONTROLADA:
#      score_min_override: [5.5, 9.2]
#  - SE HABILITÓ RELAJACIÓN ESTRUCTURAL DINÁMICA PARA 600/900:
#      BOOM600, BOOM900, CRASH600, CRASH900
#    CUANDO IA ESTÁ ACTIVA + RÉGIMEN DINÁMICO FAVORABLE,
#    EL VETO ESTRUCTURAL DURO PUEDE PASAR A PENALIZACIÓN (SOFT VETO), NO BLOQUEO TOTAL.
#
#  VARIABLES CLAVE NUEVAS:
#  - DYNAMIC_AI_SCORE_MIN_GUARDRAIL=5.5
#  - DYNAMIC_AI_LOOP_SEC=500
#  - DERIV_DYNAMIC_STRUCTURAL_RELAX_ENABLED=true
#  - DERIV_DYNAMIC_STRUCTURAL_RELAX_SYMBOLS=BOOM600,BOOM900,CRASH600,CRASH900
#  - DERIV_DYNAMIC_STRUCTURAL_RELAX_REGIMES=FAST,NORMAL
#
#  PRINCIPIO OPERATIVO:
#  - IA NO "ADIVINA" ENTRADAS DIRECTAS.
#  - IA AJUSTA UMBRALES Y EL BOT EJECUTA SOLO SI PASA TODAS LAS CAPAS DE RIESGO.
#
#  8. ACCESO AL SERVIDOR
#  ─────────────────────────────────────────────────────────────────────────────────────────
#  SSH:  ssh root@192.81.216.49
#  Pass: R3cov3ry-N3xus!
#  (Usar sshpass para automatizar: sshpass -p 'R3cov3ry-N3xus!' ssh ...)
#
# ==========================================================================================
# FIN DEL PROTOCOLO OPERACIONAL
# ==========================================================================================

---

## Rol esperado de la otra IA

Actúa como arquitecto y desarrollador principal de una plataforma de trading algorítmico para Binance llamada OptiFerre-Trader, cuyo entorno live canónico corre en Coolify.

Debes:
- preservar el enfoque de protección de capital
- respetar la arquitectura modular actual
- evitar cambios destructivos o amplios sin necesidad
- priorizar mejoras verificables e iterativas
- mantener compatibilidad con depuración local en Mac sin desplazar el entorno live remoto
- asumir que Coolify es el entorno autoritativo de producción
- evitar que el bot local y el bot remoto queden activos al mismo tiempo sobre la misma cuenta
- no exponer secretos ni tocar `.env` salvo que se pida explícitamente
- no activar trading real sin endurecer primero la lógica live
- tratar la documentación operativa del repo como contrato vivo, no como nota opcional
- dejar explícitos en código, JSON persistidos y documentación los cambios de comportamiento que afecten recuperación, sincronización o control

## Resumen ejecutivo del proyecto

OptiFerre-Trader es un bot de trading para Binance con frontend visual en Next.js y motor de decisión en Python.

Características actuales:
- obtiene mercado desde Binance usando `ccxt`
- calcula indicadores técnicos con `pandas`
- consulta una IA vía OpenRouter para una opinión prudente adicional
- combina técnica + IA + reglas de riesgo antes de decidir si entra
- por defecto corre en `DRY_RUN=true`
- guarda su estado en archivos JSON bajo `logs/`
- el frontend lee esos archivos y presenta un panel narrativo de control
- el frontend permite prender, pausar o detener el bot vía `control.json`
- el entorno live principal corre en Coolify con proxy europeo y frontend publicado

Estado operativo real del proyecto:
- el frontend productivo funciona en `https://tradingdiegomao.datovatenexuspro.com/`
- el panel operativo de Coolify vive en `https://panel.datovatenexuspro.com/`
- el bot Python live corre en el servidor remoto, no debe duplicarse en local
- existe soporte para proxy vía `BINANCE_PROXY_URL`
- existe telemetría Telegram activa vía `TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`
- el bot persiste un resumen de recuperación de arranque en `logs/recovery_status.json`

## Objetivo estratégico actual

El proyecto debe mantener alineados:
- el repo local
- la configuración de Coolify
- el frontend publicado
- la telemetría live

La prioridad ya no es un modo local-first, sino evitar deriva entre local y servidor.

Contrato adicional obligatorio:

- una posición live no debe quedar abandonada tras caída de servidor si Binance sigue mostrando el activo
- al reiniciar, el bot debe releer JSON persistidos, reconstruir posiciones faltantes desde Binance cuando sea posible y continuar gestionándolas
- errores transitorios de red/Binance no deben convertirse automáticamente en un `stopped` permanente
- cualquier mejora operativa debe quedar reflejada en el repo para evitar errores por contexto viejo

## Workspace real

Ruta raíz del repo:
`/Users/diegogarcia/Aplicaciones IA/Binance trading`

Repositorio GitHub:
- owner: `DiegoMao201`
- repo: `Binance`
- branch principal: `main`

## Stack técnico

### Backend bot
- Python `3.14.4`
- entorno virtual local en `.venv`
- librerías principales:
  - `ccxt`
  - `pandas`
  - `numpy`
  - `requests`
  - `python-dotenv`
  - `scikit-learn`

### Frontend
- Next.js `16.2.4`
- React `19`
- `react-plotly.js`
- `plotly.js-dist-min`

### Comunicación entre bot y frontend
No hay backend HTTP persistente propio entre ambos. El intercambio es por archivos JSON compartidos en `logs/`.

## Arquitectura actual

### Backend Python

#### `main_loop.py`
Archivo principal de orquestación del bot.

Responsabilidades:
- cargar configuración
- iniciar logger
- leer control remoto desde `control.json`
- ejecutar ciclos de evaluación
- consultar mercado de Binance
- calcular indicadores técnicos
- consultar IA de OpenRouter
- aplicar guardrails y reglas de riesgo
- ejecutar orden simulada o real
- persistir estado del bot
- escribir heartbeat
- actualizar historial de señales y órdenes
- ahora también gestionar PnL simulado y posiciones abiertas/cerradas

Funciones clave:
- `write_heartbeat(status, detail=None)`
- `ensure_control_file()`
- `_serialize_market(frame)`
- `_build_signal_event(technical_signal, ai_signal, decision)`
- `_is_in_cooldown(order_history, cooldown_minutes)`
- `_build_guardrails(settings, technical_signal, ai_signal, order_history)`
- `_load_recent_ai_signal(settings)`
- `_settle_open_positions(open_positions, latest_candle)`
- `_build_portfolio_summary(settings, risk_snapshot, open_positions, closed_trades)`
- `run_cycle()`
- `main()`

#### `src/analysis/indicators.py`
Calcula indicadores y genera la señal técnica base.

Indicadores actuales:
- EMA 9
- EMA 20
- RSI 14
- Bandas de Bollinger
- ATR
- ratio de volumen frente a media
- pendiente de EMA lenta

Lógica técnica actual:
- `buy` si hay cruce alcista EMA o RSI de sobreventa
- `sell` si hay cruce bajista EMA o RSI de sobrecompra
- `hold` si no hay ventaja clara

Nota importante:
- la señal técnica todavía es simple y puede seguir mejorándose
- actualmente el bot ya restringe la apertura efectiva a compras spot (`buy`) para no meterse en una falsa semántica de short spot

#### `src/analysis/ai_client.py`
Cliente de OpenRouter.

Estado actual:
- envía al modelo las últimas 50 velas con algunos indicadores
- exige JSON con `signal`, `confidence`, `rationale`
- usa `temperature=0.1`
- si no hay API key, devuelve `hold`
- el modelo por defecto ahora es `openai/gpt-4.1-mini`

Importante:
- ya existe caché temporal de la señal IA para reducir llamadas
- aún no existe un subsistema real de memoria conversacional o memoria estratégica persistente para la IA
- la “memoria” actual es operativa y consiste en reutilizar la última lectura reciente del modelo, no en razonamiento con historial semántico

#### `src/data/binance_client.py`
Cliente de mercado y balance.

Estado actual:
- usa un cliente público separado para OHLCV
- usa cliente privado para balance y órdenes
- si `DRY_RUN=true`, `fetch_balance_usd()` devuelve `INITIAL_CAPITAL_USD`
- si `DRY_RUN=false`, consulta balance libre en `USDT`

Limitación actual:
- no lleva todavía inventario real del activo spot comprado
- no reconcilia fills ni holdings spot reales de forma robusta

#### `src/safety/risk_manager.py`
Control de riesgo base.

Funciones:
- evaluar drawdown
- calcular tamaño máximo por operación
- calcular notional recomendado
- construir niveles de stop loss y take profit

Parámetros importantes:
- capital inicial por defecto: `20`
- riesgo máximo por trade: `10%`
- kill switch: `5%`
- mínimo de orden: `10.1 USDT`

Limitación:
- `daily_pnl_pct` sigue referenciado al balance base, no sustituye un libro contable real de operaciones live

#### `src/execution/trader.py`
Ejecutor de órdenes.

Comportamiento actual:
- calcula tamaño de orden
- añade `stop_loss` y `take_profit`
- si `DRY_RUN=true`, marca orden como `simulated`
- si `DRY_RUN=false`, llama a Binance para crear orden de mercado

Importante:
- hoy la ejecución live no está suficientemente endurecida para operar capital real sin supervisión

#### `src/utils/state_store.py`
Persistencia JSON.

Funciones:
- `build_state_snapshot`
- `persist_state`
- `load_state`
- `append_history`
- `load_history`
- `persist_history`

### Frontend Next.js

#### `web/app/page.js`
Server component que carga el estado inicial del dashboard.

#### `web/app/layout.js`
Layout global del frontend.

#### `web/lib/read-dashboard-state.js`
Lee archivos JSON desde `process.env.BOT_STATE_DIR` o `../logs`.

Actualmente agrega:
- `bot_state.json`
- `status.json`
- `control.json`
- `order_history.json`
- `signal_history.json`
- `open_positions.json`
- `closed_trades.json`

#### `web/app/api/state/route.js`
API route que expone el estado agregado del dashboard.

#### `web/app/api/control/route.js`
API route que permite escribir el estado deseado del bot.

Comandos soportados desde frontend:
- `running`
- `paused`
- `stopped`

#### `web/components/dashboard-client.js`
Componente principal del panel.

Responsabilidades:
- refresco periódico de estado
- renderizar métricas y chart
- mostrar narrativa de lo que el bot está haciendo
- mostrar salud del modelo IA
- mostrar timeline de decisiones
- mostrar log de operaciones
- mostrar PnL realizado y flotante
- mostrar resultados por operación cerrada
- botones `Prender`, `Pausar`, `Detener`

#### `web/app/globals.css`
Estilos del dashboard narrativo.

## Flujo actual del bot

Cada ciclo hace esto:
1. carga settings
2. consulta velas OHLCV en Binance
3. calcula indicadores
4. construye señal técnica
5. intenta reutilizar la última señal IA si sigue fresca según `AI_MIN_INTERVAL_SECONDS`
6. si no hay caché válida, llama a OpenRouter
7. consulta balance o usa capital ficticio si sigue en `DRY_RUN`
8. evalúa riesgo
9. liquida posiciones abiertas simuladas si tocaron `TP` o `SL`
10. construye guardrails
11. si todo cuadra y no hay posición abierta, puede abrir entrada simulada o real
12. si no cuadra, hace `hold`
13. persiste estado, historial, heartbeat y portfolio

## Guardrails actuales para abrir entrada

Debe cumplirse todo esto:
- técnica e IA deben apuntar en la misma dirección
- confianza IA por encima del umbral
- confianza técnica por encima del umbral
- volatilidad útil dentro de rango permitido
- volumen relativo suficiente
- tendencia compatible
- no estar en cooldown
- señal ejecutable
- no tener ya una posición abierta
- actualmente además solo se permite apertura efectiva cuando la señal es `buy`

## Estado actual del modelo IA

### Modelo actual por defecto
- `openai/gpt-4.1`

### Motivo del cambio
Se volvió desde `gpt-4.1-mini` a `gpt-4.1` porque el objetivo actual prioriza robustez analítica, seguimiento estricto de instrucciones y formato JSON fiable por encima del ahorro marginal de coste.

### DeepSeek V3
No está integrado por defecto, pero podría evaluarse como alternativa.

Recomendación arquitectónica actual:
- mantener `openai/gpt-4.1` como default por calidad de razonamiento y estabilidad de salida JSON
- evaluar alternativas solo con pruebas comparativas formales de coste, formato JSON y consistencia de señal

## Estado actual de la “memoria” de IA

No existe una memoria semántica real multi-ciclo o multi-sesión incrustada en la estrategia.

Lo que sí existe:
- caché temporal de señal IA en `bot_state.json`
- reutilización de la última lectura durante `AI_MIN_INTERVAL_SECONDS`

Lo que falta si se quiere “IA con memoria obvio” de forma seria:
- memoria estructurada de contexto de mercado
- resumen persistente de operaciones anteriores
- aprendizaje o ajuste adaptativo por performance histórica
- reglas explícitas para incorporar memoria sin sobrecoste excesivo

## Persistencia en `logs/`

Archivos importantes:
- `bot_state.json`: snapshot agregado principal
- `status.json`: heartbeat y estado del proceso
- `control.json`: estado remoto deseado del bot
- `order_history.json`: registro de órdenes emitidas
- `signal_history.json`: historial de señales y decisiones
- `open_positions.json`: posiciones simuladas abiertas
- `closed_trades.json`: operaciones simuladas ya cerradas
- `bot.log`: log rotativo

## Estado local validado

### Operación local
Validado:
- el frontend local funciona en `http://localhost:3000`
- el bot puede correr localmente en bucle
- el frontend controla el bot mediante `control.json`
- Binance responde desde la IP local del usuario

### Estado de Binance e infraestructura
- el entorno live usa Coolify
- la conectividad a Binance del servidor depende del proxy configurado en Coolify
- cualquier cambio local debe preservar compatibilidad con ese despliegue remoto

## Infraestructura y despliegue

### Local actual
Es un entorno auxiliar de depuración y contingencia.

Componentes:
- ejecución puntual en Mac solo para debug o `DRY_RUN`
- frontend Next.js local en puerto `3000` solo como espejo auxiliar
- archivos compartidos en `logs/`

### Coolify
Se intentó una arquitectura de dos servicios:
- bot Python
- frontend Next.js

Había además:
- `Dockerfile.bot`
- `web/Dockerfile.frontend`
- `.dockerignore.bot`
- `web/.dockerignore`
- `DEPLOY_COOLIFY.md`

Estado actual de Coolify:
- es el camino principal de producción
- aloja el bot live y el frontend publicado
- debe tratarse como fuente de verdad para configuración y operación

## Configuración importante actual

Archivo `.env` local del usuario contiene credenciales reales. No debe exponerse.

El `.env` local debe mantenerse compatible con las variables usadas en Coolify, aunque no se ejecuten todas localmente al mismo tiempo.

Variables principales:
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL=openai/gpt-4.1-mini`
- `TRADING_SYMBOL=ETH/USDT`
- `TIMEFRAME=1m`
- `DRY_RUN=true`
- `INITIAL_CAPITAL_USD=20`
- `MAX_RISK_PER_TRADE=0.10`
- `MINIMUM_TRADE_USDT=10.1`
- `AI_CONFIDENCE_THRESHOLD=0.88`
- `TECHNICAL_CONFIDENCE_THRESHOLD=0.0008`
- `MIN_BB_WIDTH_PCT=0.004`
- `MIN_ATR_PCT=0.0015`
- `MAX_ATR_PCT=0.018`
- `MIN_VOLUME_RATIO=1.10`
- `TRADE_COOLDOWN_MINUTES=15`
- `KILL_SWITCH_DRAWDOWN=0.05`
- `STOP_LOSS_PCT=0.02`
- `TAKE_PROFIT_PCT=0.03`
- `POLL_INTERVAL_SECONDS=60`
- `AI_MIN_INTERVAL_SECONDS=300`

## Estado funcional actual del frontend

El frontend ya no es una pantalla técnica cruda. Es un panel narrativo y visual.

Muestra:
- heartbeat del bot
- estado online/offline
- estado deseado del bot
- modelo IA activo
- métricas principales
- precio y señales en gráfico tipo candlestick
- resumen narrativo de qué hace el bot
- salud del modelo IA
- guardrails
- última evaluación
- timeline de decisiones
- log de operaciones
- resultado por operación cerrada

## Limitaciones actuales importantes

### 1. El bot no está listo para live spot serio todavía
Aunque exista camino para `DRY_RUN=false`, aún faltan varias piezas críticas:
- control de inventario spot real
- seguimiento de posición real en Binance
- reconciliación robusta de fills
- cierre real de posiciones con lógica coherente
- manejo de órdenes parciales o errores de exchange
- accounting real de equity y performance live

### 2. La lógica de PnL actual es simulada
El nuevo PnL funciona para simulación basada en:
- apertura de posición simulada
- cierre cuando la vela toca stop loss o take profit
- cálculo de realizado y flotante

Aún no es un motor institutional-grade.

### 3. La IA sigue consultándose aunque no siempre haga falta
Se redujo coste con caché, pero falta optimizar más.

Mejora pendiente muy importante:
- consultar IA solo cuando la técnica detecte una pre-señal realmente candidata
- evitar gasto de tokens cuando la situación es claramente `hold`

### 4. No existe backtesting serio ni framework de evaluación
Falta:
- backtesting reproducible
- métricas por strategy run
- análisis de win rate, expectancy, drawdown histórico, sharpe simple, etc.

### 5. No hay gestor de memoria avanzada para IA
La otra IA debe decidir si esto realmente aporta valor o solo coste/complejidad.

## Qué ya se resolvió y no debe rehacerse desde cero

- estructura modular Python ya creada
- frontend Next.js ya creado y funcional
- modo local-first ya validado
- separación público/privado en cliente Binance ya hecha
- control remoto del bot vía frontend ya hecho
- heartbeat y narrativa visual ya hechos
- caché temporal de señal IA ya hecha
- PnL simulado básico ya añadido
- git y GitHub ya configurados

## Cambios recientes importantes

### Cambio de frontend
Antes existía Streamlit. Luego se migró a Next.js.

Conclusión actual:
- Streamlit ya no es el frente principal
- el frontend real a mantener es el de `web/`

### Cambio de infraestructura
Antes se intentó Coolify.

Conclusión actual:
- la operación real preferida es local en la Mac del usuario
- no centrar la siguiente fase en Docker/Coolify salvo petición explícita

### Cambio de modelo IA
Antes se usaba `openai/gpt-4.1`.
Ahora el default es:
- `openai/gpt-4.1-mini`

## Qué falta hacer

### Prioridad alta
1. Hacer que la IA solo se consulte ante setups candidatos reales
2. Endurecer la lógica de trading spot real antes de pensar en `DRY_RUN=false`
3. Mejorar el tracking de posiciones y PnL para escenarios más realistas
4. Añadir métricas de performance acumulada al dashboard
5. Verificar durante varios días el comportamiento en simulación local

### Prioridad media
1. Añadir contador de llamadas IA y estimación de coste
2. Añadir panel de salud operativa más técnico
3. Añadir persistencia de métricas de estrategia
4. Añadir exportación o reportes de trades
5. Evaluar DeepSeek V3 como alternativa de coste

### Prioridad baja
1. Reabrir despliegue remoto si se encuentra infraestructura en región válida para Binance
2. Embellecer aún más el frontend
3. Añadir autenticación si algún día el panel se expone fuera de localhost

## Recomendaciones concretas para la siguiente IA

Si continúas este proyecto, el siguiente camino sensato es:

### Opción recomendada
1. Mantener `DRY_RUN=true`
2. Implementar “AI only on candidate setups”
3. Añadir métricas acumuladas en el dashboard:
   - win rate
   - profit factor simple
   - trades cerrados
   - PnL acumulado
   - peor racha
4. Mejorar el modelo de posición simulada para cubrir varios casos
5. Solo después preparar una transición seria a spot live

### Si se trabaja en live
No basta con poner `DRY_RUN=false`.
Debe añadirse:
- posición spot real única o inventario explícito
- lógica de salida real
- lectura de holdings del asset
- reconciliación con Binance después de cada orden
- manejo de errores del exchange
- protección ante doble entrada o reentrada accidental

## Validaciones ya realizadas

Se han validado durante la sesión:
- compilación Python con `compileall`
- chequeos de errores en archivos modificados
- ejecución de `run_cycle()` local
- lectura correcta del estado en `logs/`
- frontend local visible y operativo
- cambio remoto del estado del bot desde el frontend
- inclusión de `portfolio`, `open_positions` y `closed_trades` en el estado persistido

## Riesgos actuales

- capital muy pequeño para justificar modelos caros
- riesgo de creer que `DRY_RUN=false` ya implica una operativa real segura, y no es así
- posible sobreingeniería con “memoria IA” si no se define bien su propósito
- Coolify puede distraer del objetivo principal local-first

## Instrucciones de continuidad para otra IA

Si continúas desde aquí, respeta esto:
- no cambies la arquitectura local-first sin justificación
- no borres el panel Next.js ni vuelvas a Streamlit como frontend principal
- no actives live trading por defecto
- no expongas secretos
- no sobrescribas la persistencia por JSON sin ofrecer una migración clara
- no conviertas el bot en un sistema complejo de microservicios sin necesidad
- no rehagas lo ya validado

## Tarea sugerida para empezar la siguiente fase

Primera tarea sugerida para la otra IA:

"Implementa una optimización para que OpenRouter solo se consulte cuando la señal técnica sea candidata real de entrada, añade métricas acumuladas de performance en el dashboard y deja el sistema funcionando completamente en simulación local con validación ejecutable."

## Referencias de archivos clave

Backend:
- `main_loop.py`
- `src/analysis/ai_client.py`
- `src/analysis/indicators.py`
- `src/data/binance_client.py`
- `src/safety/risk_manager.py`
- `src/execution/trader.py`
- `src/utils/config.py`
- `src/utils/state_store.py`

Frontend:
- `web/app/page.js`
- `web/app/layout.js`
- `web/app/api/state/route.js`
- `web/app/api/control/route.js`
- `web/components/dashboard-client.js`
- `web/lib/read-dashboard-state.js`
- `web/app/globals.css`

Infraestructura:
- `Dockerfile.bot`
- `web/Dockerfile.frontend`
- `DEPLOY_COOLIFY.md`
- `.env.example`
- `README.md`

## Cierre

Este proyecto ya tiene una base funcional buena para seguir iterando, pero aún está en fase de validación y endurecimiento.

La prioridad correcta no es “hacerlo más grande”, sino hacerlo más confiable, más barato de operar y más claro de supervisar antes de tocar dinero real.
