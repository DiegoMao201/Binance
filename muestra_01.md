# MUESTRA_01 LIMPIA

Estado operativo consolidado y validado al 23-05-2026.

## Estado final

- Backend Deriv: OK
- Frontend Deriv: OK
- Migracion DB 011: incluida en repo
- Dashboard de observabilidad IA/patrones: OK
- Rama main: publicada y sincronizada en remoto

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
