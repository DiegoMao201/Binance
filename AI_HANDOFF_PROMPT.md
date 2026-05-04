# Prompt de Handoff Completo: OptiFerre-Trader

Usa este documento como contexto base para continuar el desarrollo de este proyecto con otra IA. La meta es que puedas retomar el trabajo sin perder decisiones previas, sin romper la arquitectura actual y sin reabrir problemas ya resueltos.

## Rol esperado de la otra IA

Actúa como arquitecto y desarrollador principal de una plataforma de trading algorítmico local-first para Binance llamada OptiFerre-Trader.

Debes:
- preservar el enfoque de protección de capital
- respetar la arquitectura modular actual
- evitar cambios destructivos o amplios sin necesidad
- priorizar mejoras verificables e iterativas
- mantener compatibilidad con la operación local en Mac
- asumir que el panel Next.js local es el centro de control maestro
- no exponer secretos ni tocar `.env` salvo que se pida explícitamente
- no activar trading real sin endurecer primero la lógica live

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
- el bot corre localmente en el Mac del usuario y esa modalidad es actualmente la preferida

Estado operativo real del proyecto:
- el frontend local funciona en `http://localhost:3000`
- el bot Python local funciona desde el Mac del usuario
- Binance responde correctamente desde la IP residencial del usuario
- Coolify se intentó pero quedó descartado como camino principal porque la IP/región del servidor devolvía `451 restricted location`

## Objetivo estratégico actual

El proyecto no debe enfocarse ahora en despliegue remoto. Debe enfocarse en:
- operación local estable
- observabilidad clara del bot
- reducción de coste de IA
- validación prolongada en simulación
- evolución posterior hacia spot live real, pero solo cuando el control de posiciones sea robusto

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
- desde el servidor remoto usado en Coolify, Binance respondía `451 restricted location`
- desde la IP residencial del usuario sí funcionó
- por eso el proyecto se movió a estrategia local-first

## Infraestructura y despliegue

### Local actual
Es la infraestructura preferida ahora mismo.

Componentes:
- proceso Python del bot en la Mac
- frontend Next.js local en puerto `3000`
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
- no es el camino principal ahora mismo
- tuvo problemas de espacio en disco y luego de restricción geográfica de Binance
- puede mantenerse la documentación, pero no debe asumirse como entorno operativo actual

## Configuración importante actual

Archivo `.env` local del usuario contiene credenciales reales. No debe exponerse.

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
