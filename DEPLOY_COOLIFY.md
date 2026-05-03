# Deploy en Coolify

Arquitectura recomendada:

- `optiferre-bot`: ejecuta `main_loop.py`
- `optiferre-frontend`: sirve el dashboard Next.js
- Volumen compartido: `/data/logs`

## Servicio bot

- Repo: este repositorio
- Tipo: Dockerfile
- Dockerfile: `Dockerfile.bot`
- Puerto público: no requerido
- Volumen: montar almacenamiento persistente en `/data/logs`

### Variables del bot

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL=openai/gpt-4.1`
- `LOGS_DIR=/data/logs`
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
- `LOG_LEVEL=INFO`

## Servicio frontend

- Repo: este repositorio
- Tipo: Dockerfile
- Dockerfile: `web/Dockerfile.frontend`
- Base directory: `web`
- Puerto público: `3000`
- Dominio: el dominio que asignes en Coolify
- Volumen: montar el mismo almacenamiento persistente en `/data/logs`

### Variables del frontend

- `BOT_STATE_DIR=/data/logs`
- `NODE_ENV=production`

## Notas

- `Pausar` deja el proceso vivo pero bloquea nuevas operaciones.
- `Prender` reanuda el bot.
- `Detener` termina el proceso; Coolify debe relanzarlo si quieres volver a encenderlo.
- Mantén `DRY_RUN=true` hasta validar la key de Binance desde la IP del servidor.