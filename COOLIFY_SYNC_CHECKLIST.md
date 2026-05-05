# Coolify Sync Checklist

Usa este documento para dejar el entorno local y Coolify alineados variable por variable, sin volver a introducir deriva entre ambos.

## Regla base

- Coolify es la fuente de verdad para producción.
- El `.env` local debe espejar las mismas claves, pero no debe correr live en paralelo.

## Variables del bot: comparar una por una

### Credenciales y conectividad

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `BINANCE_PROXY_URL`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`

### Telegram

- `TELEGRAM_ENABLED`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### Rutas y estado compartido

- `LOGS_DIR`

En Coolify debe ser `/data/logs`.

### Universo y frecuencia

- `TRADING_SYMBOL`
- `TARGET_SYMBOLS`
- `MAX_GLOBAL_OPEN_POSITIONS`
- `SYMBOL_SCAN_PAUSE_SECONDS`
- `TIMEFRAME`
- `POLL_INTERVAL_SECONDS`

### Modo operativo

- `DRY_RUN`

### Sizing y riesgo

- `INITIAL_CAPITAL_USD`
- `POSITION_SIZE_PCT`
- `MAX_RISK_PER_TRADE`
- `MINIMUM_TRADE_USDT`
- `KILL_SWITCH_DRAWDOWN`
- `STOP_LOSS_PCT`
- `TAKE_PROFIT_PCT`

### Guardrails

- `AI_CONFIDENCE_THRESHOLD`
- `TECHNICAL_CONFIDENCE_THRESHOLD`
- `MIN_BB_WIDTH_PCT`
- `MIN_ATR_PCT`
- `MAX_ATR_PCT`
- `MIN_VOLUME_RATIO`
- `TRADE_COOLDOWN_MINUTES`
- `SCENARIO_A_RSI_MAX`
- `SCENARIO_B_RSI_MAX`
- `AI_MIN_INTERVAL_SECONDS`

### Observabilidad

- `BOT_HEALTH_MAX_AGE_SECONDS`
- `LOG_LEVEL`

## Variables del frontend: comparar una por una

- `BOT_STATE_DIR`
- `NODE_ENV`

En Coolify, `BOT_STATE_DIR` debe ser `/data/logs`.

## Checklist exacto de alineación

1. Abre el servicio del bot en Coolify y copia todas las variables visibles.
2. Abre el `.env` local y verifica que existan las mismas claves.
3. Si una clave existe en Coolify y no existe localmente, agrégala al `.env` local y a `.env.example`.
4. Si una clave existe localmente pero no existe en Coolify, decide si está obsoleta o si falta subirla al servidor.
5. Verifica que `DRY_RUN` tenga el valor correcto en ambos contextos.
6. Verifica que `TIMEFRAME`, `TARGET_SYMBOLS`, `POSITION_SIZE_PCT` y `KILL_SWITCH_DRAWDOWN` coincidan exactamente.
7. Verifica que Telegram esté completo en ambos lados o explícitamente deshabilitado en local.
8. Verifica que el frontend lea `/data/logs` en Coolify y no una ruta local accidental.
9. Verifica que no exista un `main_loop.py` live ejecutándose en la Mac.
10. Después de cualquier cambio en variables de Coolify, redeploy del servicio afectado.

## Qué revisar dentro de Coolify cuando vuelve el panel

### Bot

1. Estado del contenedor: `running`
2. Healthcheck: `healthy`
3. Logs recientes sin bucles de crash
4. Heartbeat actualizando `status.json`
5. `open_positions.json` consistente con Binance

### Frontend

1. Estado del contenedor: `running`
2. `GET /api/state` responde rápido
3. `heartbeat_at` visible y fresco
4. `openPositions` y `closedTrades` no congelados en valores viejos

### Proxy

1. `BINANCE_PROXY_URL` presente en el bot
2. El bot alcanza Binance sin `451`
3. No hay timeouts repetidos ni errores de red en logs

### Telegram

1. Variables presentes
2. Sin errores de envío en logs
3. Alerta de prueba recibida en Telegram/Watch

## Señales de desincronización que obligan a parar antes de seguir

- Binance muestra una BTC abierta y `open_positions.json` no
- `closed_trades.json` tiene cierres `external_reconcile` falsos
- `equity_history.json` cae a solo balance libre de USDT sin vender el activo
- El frontend muestra heartbeat viejo pero el contenedor sigue arriba
- El bot local vuelve a ejecutarse live

Si aparece cualquiera de esas señales, la prioridad es reconciliar estado antes de dejar operar de nuevo.