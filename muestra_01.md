# MUESTRA_01

Documento índice operativo para la última puesta en marcha (Prueba 5F).

## Documento forense completo

Ver detalle técnico, contexto y checklist en:
- MUESTRA_01_ANALISIS_FORENSE.md

## Resumen ejecutable (5F)

- Filtro IA adaptativo por símbolo activado en `src/analysis/deriv_analyst.py`.
- Orquestador endurecido por calidad (`ai_approval_rate_15m`, `win_rate_15m`, `ev_per_trade_15m`) en `scripts/dynamic_ai_orchestrator.py`.
- Memoria de patrones persistente en DB (`ai_entry_pattern_memory`) con migración `db/migrations/011_ai_pattern_memory_and_score_guardrail.sql`.
- Frontend Deriv (tab Telemetry) ampliado con:
  - panel `calidad IA adaptativa · live`
  - panel `pattern memory · combinaciones`

## Orden de despliegue recomendado

1. Aplicar migración 011.
2. Desplegar backend/orquestador/frontend.
3. Verificar endpoint `/api/deriv-analytics` y vista `/deriv`.
4. Validar en live aprobación IA vs winrate/EV por símbolo.
