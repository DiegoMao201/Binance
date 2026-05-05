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

## Restricciones de seguridad incorporadas

- `DRY_RUN=true` por defecto
- `OPENROUTER_MODEL=openai/gpt-4.1` por defecto para priorizar calidad analítica y salida JSON estable
- capital inicial limitado a 20 USD
- máximo 10% del balance por operación
- kill switch al 5% de pérdida acumulada

## Visibilidad operativa

- El panel canónico es `https://tradingdiegomao.datovatenexuspro.com/`.
- El panel local en `http://localhost:3000` se considera auxiliar y no debe gobernar el entorno live remoto salvo mantenimiento explícito.
- El dashboard ya muestra `PnL realizado`, `PnL flotante` y una tabla de `Resultado por operación` para las operaciones simuladas que cierren por take profit o stop loss.

## Nota

El sistema se ha diseñado para priorizar preservación de capital y validación incremental. No elimina el riesgo de mercado ni sustituye pruebas extensas en paper trading.
