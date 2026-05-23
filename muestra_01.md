# MUESTRA_01 LIMPIA

Estado operativo consolidado y validado al 23-05-2026.

## Estado final

- Backend Deriv: OK
- Frontend Deriv: OK
- Migracion DB 011: incluida en repo
- Dashboard de observabilidad IA/patrones: OK
- Rama main: publicada y sincronizada en remoto

## Reinicio limpio Prueba 5 (23-05-2026 UTC)

1. Se dejo el broker sin posiciones heredadas y se ejecuto reset controlado de muestra.
2. Backup pre-reset creado en:
  - /data/deriv-logs/archive_stage5_restart_20260523_165659
3. Archivos reiniciados para nueva muestra:
  - deriv_closed_contracts.json -> []
  - deriv_open_contracts.json -> []
  - deriv_spike_events.json -> []
  - deriv_spikes.json -> []
  - deriv_ai_decisions.json -> []
  - deriv_lockout.json -> {}
  - deriv_status.json -> {}
  - dynamic_ai_config_diffs.jsonl -> vacio
4. Stack Deriv reiniciado (bot principal + orquestador IA) con health OK.
5. Validacion remota release-gate en verde total (PASS) con DB guardrails y runtime dinamico correctos.
6. Variables de escape explicitadas en env para consistencia de guardrails:
  - DERIV_BLOCK_BC_ESCAPE_BOOM600=false
  - DERIV_BLOCK_BC_ESCAPE_BOOM900=false

## Snapshot de arranque de la nueva muestra

- Archivo de bootstrap:
  - /data/deriv-logs/sample5_bootstrap_20260523_165859.json
- Estado al crear snapshot:
  - open=1 (operacion nueva ya iniciada tras el reset)
  - closed=0
  - spikes=16
  - ai_decisions=1
  - heartbeat fresco
  - dynamic_config.enabled=true
  - dynamic_symbols_loaded=8
  - entry_tick_only=true

## Cambios incluidos

1. Filtro IA adaptativo por simbolo y endurecimiento de calidad en backend.
2. Memoria de patrones persistente en base de datos.
3. Guardrails de score ampliados y hardening de configuracion dinamica.
4. Frontend Deriv con visibilidad live de:
  - calidad IA adaptativa por simbolo
  - memoria de patrones por combinaciones
5. Documentacion forense extendida con la prueba 5F.

## Archivos clave

- src/analysis/deriv_analyst.py
- scripts/dynamic_ai_orchestrator.py
- src/main_deriv.py
- src/execution/deriv_trader.py
- src/execution/position_manager.py
- db/migrations/011_ai_pattern_memory_and_score_guardrail.sql
- web/app/api/deriv-analytics/route.js
- web/components/deriv-analytics-client.js
- MUESTRA_01_ANALISIS_FORENSE.md

## Validaciones ejecutadas

1. Build frontend de produccion:

```bash
npm --prefix web run build
```

Resultado: compila correctamente.

2. Validacion de sintaxis Python (backend y scripts):

```bash
"/Users/diegogarcia/Aplicaciones IA/Binance trading/.venv/bin/python" -m compileall src scripts main_loop.py run_dashboard.py setup_env.py
```

Resultado: compilacion correcta tras corregir una condicion en src/execution/deriv_trader.py.

3. Chequeo de errores en archivos modificados:

- src/execution/deriv_trader.py: sin errores
- web/components/deriv-analytics-client.js: sin errores
- web/app/api/deriv-analytics/route.js: sin errores

## Verificacion operativa en UI/API

1. Abrir /deriv y entrar al tab Telemetry.
2. Confirmar paneles:
  - calidad IA adaptativa · live
  - pattern memory · combinaciones
3. Verificar API /api/deriv-analytics devuelve ai_quality y pattern_memory.

## Observaciones no bloqueantes

- Next.js muestra warnings conocidos de runtime/edge y trazado, pero no impiden compilacion ni despliegue.
- Aviso de API key de email en build (no bloqueante para el dashboard).

## Referencia forense completa

Ver analisis completo en MUESTRA_01_ANALISIS_FORENSE.md.
