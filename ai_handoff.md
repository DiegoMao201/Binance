# AI Handoff (Estado Actual)

## Estado live confirmado (24-05-2026 UTC)
- Entorno productivo: Coolify.
- Commit activo bot/sidecar: 2199f12.
- Regla nueva desplegada en prompt del LLM: recuperacion de score en regimen estable (0.8 <= ratio <= 1.2).
- Bot + sidecar: healthy.
- Frontend: healthy.
- Release gate remoto: PASS.

## Cambio clave aplicado
- Archivo: scripts/dynamic_ai_orchestrator.py
- Se agrego la regla exacta de recuperacion al prompt del LLM para que no mantenga score alto cuando el mercado vuelve a ritmo normal.
- Se reforzo en logica local del sidecar: en rama estable reduce score_min_override por pasos hasta acercarlo a 6.80, sin tocar la logica de ticks.

## Restriccion estrategica preservada
- NO se toco post_spike_chase_guard.
- NO se modifico el flujo de entrada tick-driven.
- El ajuste es solo sobre score_min_override en regimen estable.

## Diagnostico operativo actual
- El bloqueo dominante sigue siendo post_spike_chase_guard en ventanas cortas (esperado por diseno).
- En capas posteriores persisten bloqueos de cooldown/fuerza/direccion, pero con el muro de score ahora con politica explicita de relajacion al normalizarse el ratio.

## Reset oficial de Muestra 07 (ejecutado)
1. Flatten broker: contratos abiertos 0 -> 0 (sin residuos).
2. Stop bot y sidecar.
3. Reset de archivos en /data/deriv-logs:
   - deriv_spike_events.json = []
   - deriv_closed_contracts.json = []
   - deriv_open_contracts.json = []
   - deriv_ai_decisions.json = []
   - deriv_spikes.json = []
   - deriv_market_context.json = []
   - deriv_lockout.json = {}
4. Arranque operativo posterior (bot + sidecar).
5. Gate post-arranque: un fallo transitorio por cold-start (missing last_refresh) y PASS en revalidacion inmediata.

## Estado para continuidad
- Muestra 07 oficialmente iniciada sobre deploy con regla de recuperacion estable.
- Proximo chequeo recomendado: ventana 30-90 min para confirmar reduccion de rechazos por score en simbolos con ratio estable, sin degradar robustez.
