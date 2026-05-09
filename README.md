# OptiFerre-Trader

Plataforma de trading algorítmico para Binance con análisis técnico, apoyo de IA y despliegue live principal en Coolify.

## Estado inicial

Proyecto base creado con arquitectura modular:

- `src/data`: acceso a datos de mercado vía API de Binance usando `ccxt`
- `src/analysis`: indicadores técnicos y opinión del modelo de IA
- `src/execution`: lógica de órdenes y sizing
- `src/safety`: reglas de riesgo, kill switch y dry run
- `src/ui`: panel de visualización en Streamlit
- `src/utils`: configuración y logging
- `logs`: salida persistente de logs
- `config`: configuraciones auxiliares

## Operación actual

El entorno live canónico es Coolify.

- `bot`: servicio Python continuo en Coolify
- `frontend`: servicio Next.js publicado en Coolify
- volumen compartido: `/data/logs`
- panel productivo: `https://tradingdiegomao.datovatenexuspro.com/`

La ejecución local queda solo para:

- depuración puntual
- validación en `DRY_RUN`
- recuperación manual si el servidor remoto no está disponible

No se debe ejecutar `main_loop.py` en local mientras el bot live de Coolify esté activo sobre la misma cuenta de Binance.

## Contrato operativo obligatorio

La fuente de verdad operativa no es un comentario suelto ni el estado de una sesión anterior. Es la combinación de:

- `logs/open_positions.json`: posición que el bot cree gestionar
- `logs/closed_trades.json`: cierres ya liquidados
- `logs/status.json`: heartbeat y estado deseado
- `logs/bot_state.json`: snapshot que consume el frontend
- `logs/recovery_status.json`: último intento de recuperación al arrancar el ciclo

Reglas que no se deben romper:

- Coolify es la fuente de verdad de producción.
- Local solo puede correr en `DRY_RUN` o con el bot remoto detenido.
- Si Binance tiene una posición spot abierta, el repo debe reflejarla en `open_positions.json` o reconstruirla desde exchange al arrancar.
- El frontend no inventa estado: solo presenta lo que lee de los JSON compartidos.
- Después de cada cambio operativo relevante, hay que dejar documentación y persistencia explícita; nada debe depender de “contexto recordado”.

Documentos que mandan:

- `DEPLOY_COOLIFY.md`: despliegue y recovery en producción
- `COOLIFY_SYNC_CHECKLIST.md`: sincronización exacta local/Coolify
- `AI_HANDOFF_PROMPT.md`: contrato de contexto para futuras sesiones

## Primer arranque local

1. Crear entorno virtual `.venv`
2. Instalar dependencias desde `requirements.txt`
3. Completar el archivo `.env` con claves reales
4. Ejecutar el panel Streamlit y el bucle principal

## Comandos de uso local

```bash
cd "/Users/diegogarcia/Aplicaciones IA/Binance trading"
./.venv/bin/python -c 'from main_loop import run_cycle; run_cycle()'
./.venv/bin/python main_loop.py
cd web && npm install
cd web && npm run start
```

El primer comando ejecuta un solo ciclo de prueba. El segundo deja el bot corriendo cada 60 segundos. Los dos últimos preparan y levantan el dashboard Next.js en `http://localhost:3000`.

Antes de usar el segundo comando en local, asegúrate de que el bot live de Coolify esté detenido o que `DRY_RUN=true` localmente.

## Despliegue canónico

La guía operativa principal está en `DEPLOY_COOLIFY.md`.

El despliegue recomendado es:

- un servicio `bot` con `Dockerfile.bot`
- un servicio `frontend` con `web/Dockerfile.frontend`
- un volumen compartido montado en `/data/logs`

El bot ya incluye healthcheck por heartbeat sobre `logs/status.json`, pensado para reinicio automático en Coolify.

## Garantía de reanudación tras caída

Comportamiento esperado del bot live:

- si el proceso o el servidor cae, Coolify debe volver a levantar el contenedor
- al arrancar un ciclo, el bot vuelve a leer `open_positions.json`
- si Binance todavía tiene holdings spot suficientes y el JSON local no trae esa posición, el bot la reconstruye desde `fetch_my_trades` y la persiste otra vez en `open_positions.json`
- si la posición ya fue cerrada fuera del bot, el ciclo la mueve a `closed_trades.json` con `exit_reason=external_reconcile`
- si hay un error transitorio de red/rate limit en Binance, el bot pasa a estado degradado y reintenta en el siguiente ciclo en vez de quedar detenido

Eso no significa “riesgo cero”, pero sí deja explícito el contrato de recuperación: una posición abierta no debe quedar en el aire por una caída temporal del proceso si el exchange sigue teniendo el activo y el bot puede volver a leerlo.

## Salida opcional por tiempo en ganancia

También existe una salida opcional, desactivada por defecto, para operaciones que se quedan demasiado tiempo abiertas sin llegar al `take_profit` duro:

- `MAX_POSITION_HOLD_MINUTES`: tiempo mínimo de permanencia para evaluar cierre por tiempo
- `TIME_PROFIT_TAKE_PCT`: beneficio mínimo requerido para cerrar por tiempo

Si ambos valores son mayores que `0`, el bot puede cerrar una operación con `exit_reason=time_profit_take` cuando ya excedió el tiempo configurado y sigue en ganancia suficiente.

## Restricciones de seguridad incorporadas

- `DRY_RUN=true` por defecto
- `OPENROUTER_MODEL=openai/gpt-4.1` por defecto para priorizar calidad analítica y salida JSON estable
- capital inicial limitado a 20 USD
- sizing live recomendado de 60% del balance por operación mientras se construye muestra estadística
- kill switch live recomendado al 7% de pérdida acumulada

## Filtro de IA para nuevas entradas

Las nuevas entradas deben pasar una aprobación explícita de la IA antes de abrir compra:

- `signal=buy`
- `approved=true`
- `direction_alignment=aligned`
- `confidence >= AI_CONFIDENCE_THRESHOLD`

Compuerta vigente por escenario:

- Escenario `A`: la IA puede aprobar con `setup_quality=medium` o `high` si mantiene aprobación explícita, alineación y confianza suficiente. Los `risk_flags` siguen registrándose en telemetría, pero ya no bloquean por sí solos este escenario si la IA aprueba el setup.
- Escenario `B`: se mantiene la política estricta, exigiendo `setup_quality=high` y `risk_flags=[]` además del resto de condiciones.

Además, la petición a OpenRouter ya no evalúa OHLCV “a ciegas”: ahora recibe el contexto del setup técnico candidato (`scenario`, `candidate_signal`, `atr_pct`, `volume_ratio`, etc.) para que actúe como filtro de confirmación, no como generador de ideas desde cero.

Si cualquiera de esos puntos falla, el bot se queda en `hold`. Este ajuste solo endurece entradas futuras; no modifica la gestión de posiciones ya abiertas ni el motor de cierres.

## Tiered Trailing Stop (regla "no devolver ganancia")

El bot eleva el SL en escalones a medida que el trade gana MFE. La idea operativa
es simple: **una operación que ya estuvo en positivo no puede regresar a perder
todo el capital**. La tabla activa es:

| Tier | Trigger MFE | Nuevo SL (offset desde entry) |
|------|-------------|-------------------------------|
| 1    | +0.50%      | entry + 0.20% (break-even +)  |
| 2    | +0.80%      | entry + 0.40%                 |
| 3    | +1.00%      | entry + 0.60%                 |
| 4    | +1.40%      | entry + 0.80%                 |
| 5    | +1.80%      | entry + 1.00%                 |

El tier 1 es el "seguro de ganancia": en cuanto el MFE cruza 0.5%, el SL salta
a entry +0.2% asegurando que un retroceso solo cierre la operación con
beneficio neto (cubre fees + slippage típicos). Los tiers superiores son
escalones acumulativos: el bot solo emite OCO nuevo en Binance al cruzar un
tier superior, evitando churn intra-tier y manteniendo bajo control el
presupuesto de rate-limit. El SL nunca baja, solo sube.

## Contexto extendido para la IA (orderbook + macro)

Cuando un símbolo pasa el pre-gate técnico, antes de consultar a OpenRouter
el bot enriquece el `candidate_context` con dos lecturas extra de Binance
spot público:

- **`orderbook_imbalance`**: snapshot del top 20 niveles del libro
  (`fetch_order_book`). Se calcula `bid_volume / (bid_volume + ask_volume)`.
  Valores `> 0.55` indican presión compradora real; `< 0.45`, vendedora.
  También se reporta `spread_pct` para detectar mercados ilíquidos.
- **`macro_trend`** (15m): EMA20/EMA50 sobre la temporalidad superior.
  Devuelve `bullish`, `bearish` o `neutral` y la `slope_pct` de la EMA50
  como proxy de fuerza. La IA usa esto para descartar long en tendencias
  bajistas confirmadas en 15m sin matar pullbacks legítimos.

Estas llamadas se ejecutan **solo cuando el símbolo es candidato real**
(escenario A/B + volumen + ATR válido), por lo que el costo de rate-limit
se mantiene acotado: una sola consulta a `/api/v3/depth` y `/api/v3/klines`
por candidato por ciclo.

## Centro de mando (frontend)

El dashboard de Next.js (`web/`) ya incluye:

- **Mesa de IA**: tabla con TODOS los símbolos vigilados mostrando signal,
  confidence, approved, setup_quality, risk_flags y edad de cache. Permite
  ver de un vistazo qué piensa la IA de cada mercado.
- **Posición viva**: cuando hay una operación abierta, muestra entrada,
  mark, SL/TP, MFE/MAE, PnL no realizado, hold time, tier de trailing
  activo, distancia al próximo tier y un strip de pills indicando qué
  tiers se han desbloqueado.
- **Profundidad orderbook + Régimen macro**: paneles dedicados al símbolo
  en foco con bid/ask, spread, imbalance bar y tendencia 15m con slope.
- **Radar enfocado**: la card de AI confidence ahora es por símbolo (no
  global), con etiqueta de cache vs evaluación fresca.

## Incidente 2026-05-07

El stop de producción del 2026-05-07 no fue un falso positivo del kill switch. El estado live reportó:

- `equity_usd=37.8726`
- `high_water_mark=40.0662`
- `drawdown_pct=0.05475`
- `control.desired_state=stopped`

Conclusión operativa:

- no conviene subir el kill switch directo a 10%
- tampoco conviene mantener `POSITION_SIZE_PCT=0.95` si el objetivo es llegar a una muestra de 50 trades sin cortar la curva por ruido de corto plazo
- el ajuste mínimo recomendado es `POSITION_SIZE_PCT=0.60` y `KILL_SWITCH_DRAWDOWN=0.07`

Ese ajuste no “cura” la estrategia por sí solo; corrige la desproporción entre exposición por trade y drawdown máximo permitido para que la muestra sea estadísticamente usable.

## Visibilidad operativa

- El panel canónico es `https://tradingdiegomao.datovatenexuspro.com/`.
- El panel local en `http://localhost:3000` se considera auxiliar y no debe gobernar el entorno live remoto salvo mantenimiento explícito.
- El dashboard ya muestra `PnL realizado`, `PnL flotante` y una tabla de `Resultado por operación` para las operaciones simuladas que cierren por take profit o stop loss.

## Nota

El sistema se ha diseñado para priorizar preservación de capital y validación incremental. No elimina el riesgo de mercado ni sustituye pruebas extensas en paper trading.
