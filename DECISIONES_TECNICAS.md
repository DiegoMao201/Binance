# DECISIONES TÉCNICAS — Bot Binance

> **Guarda este fichero en la raíz del repo.** Es el dossier permanente del proyecto: cada sesión futura de Claude Code debe leerlo antes de tocar nada.
> No es un plan de trabajo — es la lista de decisiones ya tomadas, con su motivo, para que no se re-discutan ni se reviertan por accidente.
>
> Última actualización: 17-ago-2026 (v2 — liquidationSnapshot confirmado ELIMINADO del bucket S3)

---

# 0 · Reglas que no se negocian

1. **Aislamiento total respecto al bot de Deriv.** Deriv opera 24/7 con dinero real. Ningún cambio en Binance puede tocarlo. Contenedor Deriv: `o4w1ns4cceccmn2ozqt7sol2`. Estrategia `entrada_diego.py`, estado en `/data/deriv-logs/`.
2. **`DRY_RUN=true` siempre**, hasta que exista una estrategia que haya pasado el protocolo de validación completo.
3. **Ningún cambio va a producción sin evidencia.** Toda afirmación sobre el comportamiento del sistema se acompaña de log con timestamp. Lo no verificado ejecutando se declara como hipótesis.
4. **El simulador nunca regala fills.** Ante ambigüedad, elige siempre el resultado peor para nosotros.

---

# 1 · Arquitectura

**Señal desde el mercado de FUTUROS (lectura pública, gratis) → ejecución en SPOT contra FDUSD con `LIMIT_MAKER`.**

Motivo: es el único camino con 0% de comisión en Binance a agosto 2026. No hace falta operar futuros para leer sus datos.

**Universo operable:** `ETHFDUSD SOLFDUSD XRPFDUSD DOGEFDUSD LINKFDUSD BNBFDUSD`

- `BTCFDUSD` excluido: `stepSize` de 0.00001 BTC ≈ $0.63, el 6.3% de una orden de $10. Imposible dimensionar.
- ADA y AVAX excluidos: no tienen par FDUSD.
- `BTCUSDT` de futuros se graba **solo como factor de mercado**, no es operable.

---

# 2 · Hechos verificados (no re-investigar)

| Hecho | Valor | Verificado |
|---|---|---|
| Maker en los 7 pares `/FDUSD` | **0%** | Anuncio Binance 29-ene-2026 |
| Taker en los mismos pares | 0.1% (0.075% con BNB), sin excepción de VIP | ídem |
| Maker/taker VIP0 en pares `/USDT` | 0.1% / 0.1% — ser maker **no ahorra nada** fuera de FDUSD | Fee schedule |
| `minNotional` pares FDUSD | 5.00 → una orden de $10 es válida | `exchangeInfo` en vivo |
| Post-only en spot | `GTX` **no existe**. Se usa el tipo de orden **`LIMIT_MAKER`** | Docs API |
| `LIMIT_MAKER` que cruzaría el spread | Rechazo `-2010`. **Es flujo normal, no excepción** | Docs API |
| Cancelar una orden | **NO devuelve cupo** al contador. Solo se decrementa al llenarse | FAQ order count |
| Límites | 6.000 weight/min · 50 órdenes/10s · 160.000/día | Docs API |
| Spreads reales (16-ago-2026) | BTC y ETH clavados en **1 tick** · LINK 4.25 bps (el mejor) | `bookTicker` en vivo |
| **Market making** | **Descartado por aritmética**: techo de $0.004 por round-trip perfecto de $10 | cálculo propio |
| FDUSD | ~0.9979 vs USDT. Se despegó a $0.87 en abr-2025. Cap −90% desde el pico | mercado + reportes |

**Riesgo FDUSD:** nunca dejar saldo parado fuera de horas de operación. Monitorizar `FDUSDUSDT` en cada ciclo. **Liquidar todo a USDT y parar si cae por debajo de 0.9950.**

---

# 3 · Base de datos y credenciales

- DB propia: **`optiferre_binance`** en `10.0.1.8`.
- Rol propio: **`binance_bot`**, con `CONNECT` **revocado explícitamente** sobre `optiferre_pamm`.
- Nunca conectar como `bot_admin` desde el bot de Binance: esa credencial tiene acceso a la DB de Deriv.
- Migraciones en `db/migrations_binance/`, numeradas **desde 001**. No continuar la serie 018+ de Deriv.
- Si alguna vez hace falta un dato de la DB de Deriv: **se exporta a fichero. La frontera no se abre ampliando permisos.**

---

# 4 · Librerías: qué usar y qué no

## Adoptadas

| Librería | Para qué | Licencia |
|---|---|---|
| **`binance-sdk-spot`** (oficial) | **Toda la ejecución.** Único cliente con `amend/keepPriority` y pegged orders. Usar su **WebSocket API** (asyncio nativo), no REST. Auth Ed25519 para límites altos | MIT |
| **`cryptofeed`** | Recorder. Su `_check_update_id` es la sincronización de libro correcta y probada | XFree86 |
| **`hftbacktest`** | Modelos de posición en cola. Define también el formato de captura | MIT |
| **`purgedcv`** | Purged K-fold, embargo, CPCV, PSR, **Deflated Sharpe Ratio**, PBO, `min_track_record_length` | MIT |
| **`arch`** | **Block bootstrap** + `SPA` (White's Reality Check y test de Hansen en una clase) | libre |
| **`savvi`** | Anytime-valid inference para el kill-switch | MIT |
| `pyarrow` | Parquet. **Nunca `pandas` en el hot path** del recorder | Apache-2.0 |

## Prohibidas, con motivo

| Qué | Por qué |
|---|---|
| **`ccxt` para enviar o modificar órdenes** | Su `edit_order()` en spot enruta a `POST /api/v3/order/cancelReplace` (`ccxt/binance.py:5765`) → **destruye la posición en cola**. Se queda solo donde ya está, leyendo datos REST |
| **`mlfinlab`** | **Ya no es open source.** Licencia "all rights reserved", requiere pago. Riesgo legal |
| **Backtesters de vela** (Jesse, backtesting.py, backtrader, el de Freqtrade) | Fills asumidos sobre velas. Para una estrategia maker **sobreestiman el edge sistemáticamente** |
| **`vectorbt` como validador** | Sin cola, sin latencia, sin post-only. Y hace trivial correr 10.000 backtests, lo que **agrava** el problema de comparaciones múltiples. Solo screening |
| **`ArcticDB`** | Licencia BSL 1.1: exige licencia comercial en producción |
| **`nautilus_trader` para ejecución spot** | Rechaza explícitamente modificar órdenes en spot (`adapters/binance/execution.py:1264`) |

## Copiar de Hummingbot (Apache-2.0 — uso comercial libre, solo atribución)

| Archivo | Qué aporta |
|---|---|
| `core/api_throttler/async_throttler.py` + `binance_constants.py` | El mejor throttler del ecosistema, con la tabla de pesos de Binance ya mantenida |
| `pure_market_making.pyx` → `c_is_within_tolerance` | **10 líneas.** Si el precio propuesto está dentro de la tolerancia de la orden viva, no cancela → preserva cola |
| `inventory_skew_calculator.pyx` | 55 líneas, puro `np.interp`. ~80% del beneficio de Avellaneda-Stoikov con el 5% del riesgo |
| `liquidations_feed/binance/binance_liquidations.py` | La única implementación decente de feed de liquidaciones en GitHub |
| `client_order_tracker.py` | Patrón "lost orders": no asumir que una orden murió porque el exchange no la encuentra |
| `exchange_py_base.py:1129` | Detector de websocket muerto en silencio, en 6 líneas |

## Leer, aunque no se adopte

- **`nautilus_trader/docs/concepts/reconciliation.md`** — 425 líneas que son la especificación completa de divergencias entre estado local y exchange. Regla de oro a copiar: **si la reconciliación de arranque falla, el sistema no arranca** (fail-closed).
- **FreqAI `freqai_interface.py:558`** — el **Dissimilarity Index**: gate de out-of-distribution. Es el sustituto correcto del filtro LLM.

---

# 5 · Captura de datos

## Streams

**Futuros (`fstream.binance.com`)**
- `!forceOrder@arr` — liquidaciones. **Sin histórico recuperable después de hoy.**
- `<symbol>@markPrice@1s` — mark price y funding.

**Spot (`stream.binance.com`)**
- `<symbol>@depth@100ms` — **diff stream, NO `depth20`.** Los snapshots destruyen la información de cola.
- `<symbol>@aggTrade` — eventos de ejecución para decrementar la cola.
- `<symbol>@bookTicker` — validación cruzada del libro reconstruido.
- `fdusdusdt@bookTicker` — monitor de peg.

**REST cada 5 min:** `openInterestHist`, `topLongShortPositionRatio`, `topLongShortAccountRatio`, `takerlongshortRatio`.
**REST diario:** `exchangeInfo` → `tickSize`, `stepSize`, `minNotional`, `amendAllowed`, `pegInstructionsAllowed`. **Nunca hardcodear estos valores.**

## Doble escritura, obligatoria

1. **Crudo:** gzip, una línea por mensaje, `timestamp_local_microsegundos + espacio + JSON sin tocar`. El timestamp local es lo único que permite modelar la latencia real; el JSON crudo permite re-derivar cualquier campo futuro.
2. **Normalizado:** Parquet + ZSTD particionado por día, para consulta con DuckDB.

Volumen estimado: ~60–90 GB/mes con ambas copias. En DO Spaces son unos $5/mes. **El coste es irrelevante; la información perdida es permanente.**

## Validación (Binance NO publica checksum de libro)

- Continuidad de secuencia `U`/`u`, persistiendo **cada gap** detectado.
- Diff periódico contra snapshot REST `GET /api/v3/depth?limit=1000`.
- Contraste del top del libro contra `bookTicker` — el test continuo más barato, detecta el 90% de los errores.

---

# 6 · La trampa del feed de liquidaciones

Documentación oficial, literal: *"For each symbol, only the latest one liquidation order within 1000ms will be pushed as the snapshot."*

Desde el **27-abr-2021** el feed está throttleado a **1 evento/segundo/símbolo**. Durante una cascada hay cientos por segundo y el stream da uno. **El dato está truncado exactamente cuando más lo necesitamos**, y el sesgo no es constante: en calma el feed es casi completo, en cascada captura quizá el 1–5%.

**Esto es universal.** CoinGlass, Coinalyze, Velo, Hyblock, Tardis y Amberdata consumen todos el mismo stream público. Pagar no compra datos más completos.

## Consecuencia de diseño

```
saturación = fracción de segundos con ≥1 evento de liquidación
             en una ventana de 60 s
```

Monótona con la intensidad real, independiente del sesgo de muestreo. **Los USD reportados son solo cota inferior.** Esta decisión importa más que la elección de proveedor.

## Detector-proxy, que resuelve el problema gratis

Caída brusca de OI + burst de `aggTrades` unidireccionales + expansión de rango = firma de la cascada, con datos **gratuitos, completos y sin huecos desde 2019**. El dataset `metrics` de `data.binance.vision` (OI histórico) **sí sigue actualizándose**, y los `aggTrades` nunca se truncaron.

**Construirlo y correlacionarlo con las muestras de Tardis** (día 1 de cada mes sin coste, event-level desde 2020). El archivo de liquidaciones de Binance `liquidationSnapshot` fue **eliminado del bucket S3** sin aviso (confirmado 2026-08-17 con S3 listing; cero `<Key>` en todos los prefijos). Las muestras de Tardis sustituyen esa fuente de corroboración. Si el detector correlaciona con Tardis, hay siete años de backtest gratis.

---

# 7 · Protocolo de validación (los gates)

Ninguna estrategia llega a producción sin atravesar los siete. Si falla **cualquiera**, no se despliega. Ajustar el umbral hasta que pase *es* el sobreajuste que el DSR detecta.

| # | Prueba | Criterio |
|---|---|---|
| 1 | Potencia estadística | `n ≥ (z_{α/2}+z_β)²σ²/δ²`. Con EV de 0.085% y σ de 0.6% → **~400 trades**. El bot anterior tenía 113 |
| 2 | Bootstrap por bloques (10.000 remuestreos) | **Límite inferior del CI95 > 0.** Por bloques, no iid |
| 3 | Walk-forward con purga y embargo | Positivo en **≥3 ventanas OOS no solapadas**. Embargo ≥ horizonte máximo de hold |
| 4 | Deflated Sharpe Ratio | **DSR > 0.95**, contando **todas** las configuraciones probadas |
| 5 | Sensibilidad a costes | Sigue positivo con el doble de slippage y 40% de salidas taker |
| 6 | Viabilidad de ejecución | `fill_rate ≥ 30%` y consumo de order count dentro de 160k/día |
| 7 | Paper trading en vivo | ≥2 semanas; el EV realizado cae dentro del CI95 predicho |

**Registro de hipótesis:** fichero append-only `hypotheses.jsonl` donde **cada backtest se registra antes de ver el resultado**. El contador total alimenta el DSR. Sin ese contador, el DSR es un número inventado.

**Modelo de cola:** correr primero con el modelo **pesimista** (tipo `RiskAdverse`: la cola solo avanza con trades). Si el EV no sobrevive ahí, no se despliega. Después repetir con los modelos probabilísticos — el EV debe ser positivo en **todos**, no solo en el más benévolo.

---

# 8 · Orden de los tests de investigación

**El test que puede matar el proyecto es el más barato. Va primero.**

### Test 0 — Markout de fills pasivos propios ← **antes que cualquier señal**

```
markout_h = signo · (mid_{t+h} − precio_fill)   para h ∈ {1s, 5s, 30s, 60s}
```

Estratificar por quintil de σ realizada y por quintil de VPIN. Muestra mínima: 5.000 fills.

**Criterio de parada:** si `E[markout_60s] + medio_spread < 0` en régimen de alta volatilidad, la estrategia pasiva durante cascadas es **inviable** y hay que replantear el diseño entero.

**Por qué:** la mejor descomposición publicada del P&L de un maker pasivo (Gatto 2026, 353.387 fills reales) da spread capturado +0.654 bps, deriva adversa −0.661 bps, comisión −0.015 bps → **neto −0.022 bps**. La ventaja de comisión 0% vale **44× menos** que la selección adversa. Y en crisis la selección adversa excede el spread capturado en **105–111%** — precisamente el régimen donde nuestra señal diría que operemos.

Este dato no lo vende nadie. Solo sale de nuestro paper trading.

### Test 1 — VPIN como veto
La señal mejor respaldada y más explotable. No genera alfa: evita pérdidas. Se calcula desde el taker buy/sell volume que ya leemos. **VPIN alto → no cotizar.**

### Test 2 — Modulación horaria
Slippage con pico a las **15:00 y 20:00 UTC**, mínimo hacia las **23:00 UTC** (Kaiko). Es un patrón de *coste*, no de retorno — mucho más estable. Coste marginal cero: mismos datos del Test 0.

### Test 3 — Reversión post-cascada *(solo si el Test 0 pasa)*
Entrar **tras el agotamiento verificado** de la venta forzada, nunca durante. El BIS documentó que el retail compra las caídas y pierde mientras las ballenas venden.

### Test 4 — Endógeno vs exógeno
Probablemente el filtro que decide el signo del P&L. Exógeno = hay salto macro en la ventana. **Predicción: solo las cascadas endógenas revierten.**

### Test 5 — Decaimiento del edge *(obligatorio sobre cualquier resultado positivo)*
Las ineficiencias en perpetuos decrecen **~11%/año** (He et al. 2024). Un backtest de muestra completa puede sobreestimar el rendimiento futuro en ~100%.

---

# 9 · Evidencia que va en contra (leerla antes de ilusionarse)

- **Zaremba et al. (2021)**, >3.600 criptos: el efecto de reversión viene de la **iliquidez**. Las monedas más grandes y líquidas muestran **momentum, no reversión**. Nuestros pares son los más líquidos que existen.
- **Meng et al. (2023)**: los inversores **infrarreaccionan** a shocks → drift, no reversión.
- **Nadie ha publicado la medición de reversión post-cascada** en perps cripto. Puede haber alfa; también puede no haber nada, y no habrá paper al que agarrarse.
- Las cascadas son **subcríticas** (λ=0.1–0.2) y el venue absorbe el **63% off-book**: el flujo forzado que golpea el libro es mucho menor de lo que sugieren los datos públicos.
- El slippage **se triplica y permanece elevado** tras el sell-off → los fills post-cascada son de peor calidad, no mejor.
- Durante la cascada **todo correlaciona a 0.85–0.90**. Diversificar entre pares no protege.

**A favor:** Galati (2024, *Journal of Banking & Finance*), estudiando el evento casi idéntico (Binance elimina comisiones en BTC, jul-2022), encuentra que **el maker gana a costa del taker**. Estamos en el lado bueno de la mesa. Y en cripto la selección adversa se come **~10% del spread efectivo**, no el ~100% del benchmark CME.

---

# 10 · Rol de la IA

**Fuera de la ruta crítica de decisión. Siempre.**

Medición propia en el bot de Deriv: el LLM Entry Gate aprobaba el **87% con confianza 0.94** y daba **WR de 39%**, mientras las compuertas matemáticas hacían el 96% del filtrado real. Conclusión: **el LLM es narrador, no filtro.**

Está respaldado por la literatura: un survey de **77 estudios** sobre agentes LLM en mercados encuentra que, de los 19 que ejecutan de verdad, **1 modela costes de transacción y 0 son reproduciblesa alto nivel**. Conclusión textual: *"No measured win rates survive scrutiny."*

**Causa mecánica:** la confianza de un LLM no está calibrada — 0.94 es un token generado, no una probabilidad posterior. Y el sesgo de complacencia de RLHF empuja a aprobar.

**Sustituto correcto:** un gate de out-of-distribution tipo **Dissimilarity Index** sobre las features de futuros. Responde la misma pregunta ("¿estoy en un régimen que conozco?") de forma calibrada, determinista, barata y auditable.

**Donde la IA sí rinde:** forense de logs y postmortems, generación de hipótesis (que luego pasan por el harness como cualquier otra), vigilancia de configuración, consolidación de alertas, redacción de informes.

**`deepseek-harness`:** no meterlo en el bot. Es un agent harness de codificación en TypeScript; su SDK Python lanza Node como subproceso con API bloqueante, consume 3–10× más tokens, y está en preview `0.1.0-rc.5` con breaking changes anunciados. Vale como sidecar headless para análisis forense, nunca en la ruta de trading. La API de DeepSeek se sigue llamando directo por HTTP OpenAI-compatible.

---

# 11 · Métricas que el bot debe emitir

| Métrica | Por qué |
|---|---|
| `adverse_selection_bps` | **La más importante.** Movimiento del mid a T+30s tras cada fill, por lado. Si se mueve sistemáticamente en contra, el spread es demasiado estrecho por muy 0% que sea la comisión |
| `queue_position_estimate` | Sin ella, la decisión "¿cancelo o aguanto?" es a ciegas. ~40 líneas portadas de hftbacktest, **corriendo en vivo** |
| `quote_uptime_ratio` | % del tiempo con órdenes en ambos lados. El KPI real de un maker |
| `orders_rejected_total{reason}` | Separar los `-2010` post-only del resto |
| `fill_rate` | Por símbolo y nivel. Si <30%, no es implementable |
| `ws_last_message_age_seconds{stream}` | Detector de websocket muerto en silencio |
| `reconciliation_discrepancies_total{type}` | Divergencias entre estado local y exchange |
| `rate_limit_weight_used_ratio` | Cancelar no devuelve cupo: este es el presupuesto real |

---

# 12 · Mapa de adopción por fase

Para no sobre-construir. **Nada de esto se implementa antes de su fase.**

| Fase | Se incorpora |
|---|---|
| **Recorder + shadow** (actual) | `cryptofeed` (referencia de sync), `pyarrow`, formato crudo de hftbacktest, markout desde el día 1 |
| **Datos históricos** (paralelo, gratis) | `binance-public-data`, muestras de Tardis, cron de Coinalyze, detector-proxy OI+aggTrades |
| **Harness de validación** | `hftbacktest`, `purgedcv`, `arch`, registro de hipótesis |
| **Ejecución real** | `binance-sdk-spot`, `amend/keepPriority`, pegged orders, throttler + inventory skew + refresh tolerance de Hummingbot, reconciliación estilo Nautilus |
| **Monitorización** | Métricas de la sección 11, kill-switch con `savvi`, lost-orders y detector de WS muerto |
| **Capa IA** | Dissimilarity Index, DeepSeek para forense y propuesta de hipótesis |

---

# ANEXO — Estado de los datasets históricos (verificado 2026-08-17)

> Esta tarea **no toca el bot ni la base de datos**. Es descarga y análisis puro. Se puede correr en paralelo al recorder.

## Disponibilidad verificada por S3 listing (BTCUSDT, futures um)

| Dataset | monthly | daily | Estado |
|---|---|---|---|
| `aggTrades` | ✅ | ✅ | **Pilar del detector-proxy. Confirmado vivo.** |
| `metrics` (OI) | ❌ | ✅ | Solo daily. Descargando activamente. |
| `bookDepth` | ❌ | ✅ | Solo daily. |
| `bookTicker` | ✅ | ✅ | Vivo. |
| `trades` | ✅ | ✅ | Vivo. |
| `klines` | ❌ | ❌ (top) | Existe bajo `klines/{sym}/{interval}/`. |
| `liquidationSnapshot` | ❌ | ❌ | **ELIMINADO DEL BUCKET S3.** S3 listing devuelve cero `<Key>` en todos los prefijos diario y mensual. Nunca planificar sobre este dataset. |

## Tarea

`scripts/download_historical.py` implementado. `data/historical/` en uso.

**1. `liquidationSnapshot` — GONE.** Dataset eliminado de `data.binance.vision` sin aviso. No se puede descargar. Ninguna sesión futura debe intentarlo.

**2. `metrics`** (open interest histórico, daily) — descarga activa. **Este dataset sigue actualizándose** hasta hoy. Disponible desde 2020-09-01.

**3. `aggTrades`** mensuales — confirmado vivo desde 2020-01. Pilar del detector-proxy OI+aggTrades. Descargar con `--crisis-only` primero (9 meses, ~5% del espacio).

**4. Muestras gratuitas de Tardis** — **sustituto directo de `liquidationSnapshot` como fuente de validación.**
```
https://datasets.tardis.dev/v1/binance-futures/liquidations/{YYYY}/{MM}/01/{SYMBOL}.csv.gz
```
Día 1 de cada mes, sin API key, event-level, desde 2020-01 hasta hoy → ~80 días. Sirve para correlacionar el detector-proxy contra liquidaciones reales independientes. Descargado con `python scripts/download_historical.py --tardis`.

**5. Cron diario contra Coinalyze** `/liquidation-history` a 1 minuto (gratis, API key en su web, 40 req/min). Retiene ~33 horas a 1m, así que un job diario nunca pierde nada. Red de seguridad multi-exchange mientras el recorder madura.

## Entregable

Un informe con:
- Rango real de fechas disponible por símbolo y por dataset.
- Número de eventos de liquidación descargados, por año.
- **Distribución de la métrica de saturación** (segundos-con-evento por ventana de 60s) — para calibrar el umbral de cascada.
- Número de eventos de cascada candidatos identificados, aplicando la definición de onset: *el minuto que cierra el retorno logarítmico de 60 minutos más negativo del día*.
- Volumen en GB y tiempo de descarga.
- **Comparación Binance vs Tardis** en las fechas solapadas: ¿coinciden los eventos? ¿coinciden los notionals?

**No construyas ninguna estrategia con estos datos todavía.** Esta tarea es solo conseguir y verificar el dataset.
