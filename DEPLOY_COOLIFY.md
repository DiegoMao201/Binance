# Deploy en Coolify

Arquitectura recomendada:

- `optiferre-bot`: proceso Python continuo que ejecuta `main_loop.py`
- `optiferre-frontend`: dashboard Next.js
- volumen compartido: `/data/logs`
- despliegue desde el mismo repo en dos servicios separados

## Objetivo operativo

Mover el bot y el dashboard fuera del portátil para que:

- el bot siga activo 24/7
- el dashboard lea el mismo estado persistido por el bot
- Coolify pueda reiniciar servicios caídos con healthchecks reales

Este documento describe el entorno live canónico. El bot local no debe correr en paralelo con este despliegue cuando ambos apunten a la misma cuenta de Binance.

## Servicio bot

- tipo: `Dockerfile`
- base directory: raíz del repo
- dockerfile: `Dockerfile.bot`
- puerto público: no requerido
- volumen persistente: montar en `/data/logs`
- healthcheck: usa `scripts/bot_healthcheck.py` y valida `logs/status.json`

### Variables del bot

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `BINANCE_PROXY_URL`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL=openai/gpt-4.1`
- `TELEGRAM_ENABLED=true`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `LOGS_DIR=/data/logs`
- `DRY_RUN=true` para el primer despliegue; luego pasar a `false`
- `TARGET_SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,AVAX/USDT,LINK/USDT,ADA/USDT`
- `TRADING_SYMBOL=BTC/USDT`
- `MAX_GLOBAL_OPEN_POSITIONS=1`
- `SYMBOL_SCAN_PAUSE_SECONDS=1.0`
- `TIMEFRAME=5m`
- `POLL_INTERVAL_SECONDS=60`
- `INITIAL_CAPITAL_USD=20`
- `MINIMUM_TRADE_USDT=10.1`
- `POSITION_SIZE_PCT=0.60`
- `MAX_RISK_PER_TRADE=1.0`
- `AI_CONFIDENCE_THRESHOLD=0.65`
- `TECHNICAL_CONFIDENCE_THRESHOLD=0.0`
- `MIN_BB_WIDTH_PCT=0.0`
- `MIN_ATR_PCT=0.0010`
- `MAX_ATR_PCT=0.030`
- `MIN_VOLUME_RATIO=0.20`
- `SCENARIO_A_RSI_MAX=45`
- `SCENARIO_B_RSI_MAX=32`
- `TRADE_COOLDOWN_MINUTES=10`
- `KILL_SWITCH_DRAWDOWN=0.10`
- `STOP_LOSS_PCT=0.01`
- `TAKE_PROFIT_PCT=0.02`
- `BOT_HEALTH_MAX_AGE_SECONDS=180`
- `LOG_LEVEL=INFO`

### Ajuste live recomendado tras los 3 paros por kill switch (2026-05-16)

El bot se detuvo 3 veces porque `KILL_SWITCH_DRAWDOWN=0.07` (7%) con un HWM histórico de ~$41.47 dejaba el umbral real de disparo en $38.57. Con equity real de $35.38 el drawdown era 14.58% >> 7%, por lo que el kill switch se disparaba inmediatamente al reiniciar.

**Raíz de los bucles de stop:**
- HWM nunca se reseteaba: solo se reiniciaba en exits `external_reconcile`, no en stop-loss normales.
- Al reiniciar manualmente el bot vía dashboard, HWM seguía en el pico histórico → kill switch disparaba de nuevo en el primer ciclo.
- Con `desired_state=stopped`, `run_cycle()` nunca era llamado → posiciones abiertas quedaban huérfanas sin gestión de SL/TP.

**Cambios aplicados en el código (commit kill-switch-hardening):**
- `KILL_SWITCH_DRAWDOWN` por defecto: 5% → **10%** (código)
- Eliminado el override agresivo de cuentas pequeñas (<$25 → 4%): era contraproducente y causaba disparos falsos.
- HWM se resetea al equity actual cuando: (a) `_clear_stale_kill_switch_stop` auto-recupera, (b) operador reanuda manualmente desde el dashboard tras un kill switch.
- Bot en estado `stopped` por kill switch ahora ejecuta un ciclo de gestión si hay posiciones abiertas → OCO/trailing se actualizan aunque el bot esté detenido.

**Env var en Coolify a actualizar:**
- `KILL_SWITCH_DRAWDOWN=0.10` (era 0.07)

## Servicio frontend

- tipo: `Dockerfile`
- base directory: `web`
- dockerfile: `Dockerfile.frontend`
- puerto público: `3000`
- dominio: el que asignes en Coolify
- volumen persistente: montar el mismo volumen en `/data/logs`
- healthcheck: `GET /api/state`

### Variables del frontend

- `BOT_STATE_DIR=/data/logs`
- `NODE_ENV=production`

## Secuencia recomendada

1. Crear el volumen persistente compartido en Coolify.
2. Desplegar primero `optiferre-bot` con `DRY_RUN=true`.
3. Validar que en `/data/logs` aparezcan `status.json`, `bot_state.json`, `scan_history.json` y `control.json`.
4. Desplegar `optiferre-frontend` montando el mismo volumen.
5. Confirmar que el dashboard muestra estado en vivo.
6. Validar que la IP del servidor esté autorizada en Binance.
7. Recién después cambiar `DRY_RUN=false` en el bot.

## Criterios de aceptación

- si el contenedor del bot se cae, Coolify lo reinicia
- si el bot deja de escribir heartbeat por más de 180 segundos, el healthcheck falla
- el frontend sigue mostrando el último estado persistido aunque el bot se reinicie
- el control remoto sigue usando `/data/logs/control.json`
- no existe otro proceso live local operando en paralelo sobre la misma cuenta
- cada arranque deja trazabilidad en `/data/logs/recovery_status.json`
- si Binance conserva una posición live y `open_positions.json` está vacío o viejo, el bot debe reconstruirla antes de seguir operando
- los errores transitorios de red, timeout o rate limit no deben dejar el control en `stopped`

## SRE: monitor activo y release blocker

### 1) Monitor activo (cron cada 5m)

Instalar watchdog en servidor:

```bash
REMOTE_PASSWORD='TU_PASS_SSH' bash scripts/install_deriv_watchdog_cron_remote.sh
```

Comportamiento:

- ejecuta `validate_deriv_runtime.py` cada 5 minutos
- si pierde `PASS`, dispara alerta critica con detalle de checks fallidos
- si se recupera, envia alerta de recovery
- escribe trazabilidad en `/data/deriv-logs/deriv_runtime_watchdog.log`

### 2) Release blocker (gate obligatorio)

Antes de marcar un release como exitoso, ejecutar:

```bash
bash scripts/deriv_release_gate.sh
```

En pipelines que disparan despliegue remoto, usar:

```bash
REMOTE_PASSWORD='TU_PASS_SSH' bash scripts/deriv_release_gate_remote.sh
```

Regla operacional:

- si el gate devuelve exit code `!= 0`, el despliegue se considera bloqueado
- no promover a produccion mientras haya checks en `FAIL`

Adicionalmente, el contenedor Deriv ya puede exponer healthcheck runtime via:

```bash
python /app/scripts/validate_deriv_runtime.py --logs-dir "${LOGS_DIR:-/data/logs}"
```

Eso endurece la señal de salud del servicio para reinicio/observabilidad en plataforma.

## Recovery cuando el panel vuelve pero el frontend está desalineado

Síntomas típicos:

- `panel.datovatenexuspro.com` abre, pero `tradingdiegomao.datovatenexuspro.com` muestra estado viejo
- el frontend responde, pero el heartbeat está congelado
- la posición BTC del exchange no coincide con `open_positions.json`
- Telegram deja de avisar aunque el bot parece arriba

Checklist dentro de Coolify:

1. Abrir el servicio del bot y comprobar que el contenedor esté `running` y `healthy`.
2. Abrir el servicio del frontend y comprobar que el contenedor esté `running`.
3. Verificar que ambos servicios monten exactamente el mismo volumen en `/data/logs`.
4. Verificar que el bot tenga `LOGS_DIR=/data/logs`.
5. Verificar que el frontend tenga `BOT_STATE_DIR=/data/logs`.
6. Verificar que el bot tenga `BINANCE_PROXY_URL` configurado si la salida a Binance depende del proxy europeo.
7. Verificar que el bot tenga `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
8. Revisar `status.json` en el volumen y confirmar que `heartbeat_at` avance cada ciclo.
9. Revisar `bot_state.json` y confirmar que `serverTime`, `status` y `portfolio` no estén congelados.
10. Revisar `open_positions.json` y compararlo con la posición real en Binance antes de relanzar el bot.

## Revisión específica para retomar la posición BTC sin desincronización

1. Confirmar en Binance si la posición BTC sigue abierta.
2. Confirmar en `/data/logs/open_positions.json` que exista una única posición BTC abierta.
3. Confirmar en `/data/logs/closed_trades.json` que no haya cierres falsos `external_reconcile` de esa BTC.
4. Confirmar en `/data/logs/equity_history.json` que no exista una muestra espuria de equity cercana al balance libre de USDT solamente.
5. Si el estado del volumen no coincide con Binance, corregir primero los JSON persistidos y solo después reiniciar el bot.
6. No arrancar el bot local mientras se hace esta reconciliación en Coolify.

## Contrato de reanudación tras caída de servidor o contenedor

Objetivo: que una posición abierta no quede abandonada por reinicio del host o del proceso.

Secuencia esperada:

1. Coolify reinicia el contenedor del bot.
2. `main_loop.py` vuelve a cargar `open_positions.json`, `closed_trades.json` y `equity_history.json`.
3. El arranque ejecuta una reconciliación de holdings live por cada símbolo objetivo.
4. Si encuentra holdings spot suficientes que no están en `open_positions.json`, reconstruye la posición desde los trades recientes del exchange.
5. Persiste el resultado en `open_positions.json` y en `/data/logs/recovery_status.json`.
6. El frontend vuelve a leer esos mismos JSON desde `/data/logs`.
7. El siguiente ciclo gestiona SL/TP o reconciliación externa de esa posición ya reatada al estado del exchange.

Qué no debe ocurrir:

- reiniciar con una BTC abierta en Binance y `open_positions.json` vacío sin intentar recuperación
- dejar `control.json` en `stopped` por un timeout transitorio de Binance
- operar local live mientras Coolify está reanudando la misma cuenta

## Limpieza segura de logs y artefactos en el servidor

No borres a ciegas todo `/data/logs`.

Limpieza segura:

1. Mantener: `status.json`, `control.json`, `bot_state.json`, `open_positions.json`, `closed_trades.json`, `equity_history.json`, `order_history.json`, `signal_history.json`, `scan_history.json`.
2. Limpiar solo rotados o snapshots no críticos si ocupan espacio: `bot.log.*`, archivos temporales y dumps manuales.
3. Antes de limpiar historiales, descargar copia del volumen o exportar los JSON críticos.
4. Si el problema es tamaño de disco, limpiar primero imágenes Docker antiguas y logs rotados antes que borrar estado operativo.

Comandos orientativos dentro del host si tienes consola:

```bash
df -h
docker ps
docker system df
docker image prune -af
find /data/logs -maxdepth 1 -type f -name 'bot.log.*' -delete
```

No ejecutes limpieza destructiva sobre `open_positions.json`, `closed_trades.json` o `equity_history.json` sin comparar primero contra Binance.

## Checklist de Telegram en Coolify

1. `TELEGRAM_ENABLED=true`
2. `TELEGRAM_BOT_TOKEN` presente
3. `TELEGRAM_CHAT_ID` presente
4. Revisar logs del bot buscando errores `Telegram devolvio` o `No se pudo enviar notificacion Telegram`
5. Forzar un evento de prueba solo cuando el bot ya esté estable y sincronizado

## Checklist de frontend en Coolify

1. `BOT_STATE_DIR=/data/logs`
2. El volumen montado debe ser exactamente el mismo que usa el bot
3. `GET /api/state` debe devolver JSON fresco
4. El `serverTime` del API puede cambiar aunque los archivos estén congelados; lo autoritativo es el `heartbeat_at` dentro de `status.json`

## Nota importante

Hoy el `stop loss` y el `take profit` siguen siendo gestionados localmente por el bot, no por órdenes residentes en Binance. Migrar a Coolify elimina la dependencia del portátil, pero no elimina todavía el riesgo de que una caída del contenedor deje una posición abierta sin protección nativa en exchange.