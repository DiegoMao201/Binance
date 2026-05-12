// Derivadores puros para extraer las vistas que necesita el HUD del payload SSE.
// No hay side-effects ni I/O: 100% client-safe.

// ── Extractor de keywords del rationale de la IA ──────────────────────────────
const KEYWORD_PATTERNS = [
  { rx: /momentum\s+(?:fuerte|strong|positivo|alcista|creciente)/i, tag: "Momentum fuerte", color: "#22c55e" },
  { rx: /momentum\s+(?:d[ée]bil|débil|negativo|bajista|decreciente)/i, tag: "Momentum débil", color: "#ef4444" },
  { rx: /soporte\s+(?:detectado|confirmado|activo|clave)/i, tag: "Soporte detectado", color: "#22d3ee" },
  { rx: /resistencia\s+(?:detectada|confirmada|activa|clave)/i, tag: "Resistencia cerca", color: "#f97316" },
  { rx: /volumen\s+(?:seco|bajo|escaso|reducido|insuficiente)/i, tag: "Volumen seco", color: "#f97316" },
  { rx: /volumen\s+(?:alto|fuerte|creciente|elevado|acelerando)/i, tag: "Volumen fuerte", color: "#22c55e" },
  { rx: /oversold|sobreventa|sobrevendido/i, tag: "Zona sobreventa", color: "#a78bfa" },
  { rx: /overbought|sobrecompra|sobrecomprado/i, tag: "Zona sobrecompra", color: "#ef4444" },
  { rx: /reversal|reversión|giro\s+bajista/i, tag: "Reversión detectada", color: "#ef4444" },
  { rx: /continuaci[oó]n|trending|tendencia\s+alcista/i, tag: "Continuación alcista", color: "#22c55e" },
  { rx: /order\s*book|imbalance|desequilibrio/i, tag: "Orderbook sesgado", color: "#facc15" },
  { rx: /neutral|equilibrado|indeciso|ranging/i, tag: "Mercado neutro", color: "#6b8299" },
  { rx: /break\s*out|ruptura|rompimiento/i, tag: "Breakout potencial", color: "#22d3ee" },
  { rx: /bearish|bajista|sell\s+pressure/i, tag: "Presión bajista", color: "#ef4444" },
  { rx: /bullish|alcista|buy\s+pressure/i, tag: "Presión alcista", color: "#22c55e" },
];

export function extractAiKeywords(rationale = "") {
  if (!rationale || typeof rationale !== "string") return [];
  const results = [];
  for (const { rx, tag, color } of KEYWORD_PATTERNS) {
    if (rx.test(rationale)) {
      results.push({ tag, color });
      if (results.length >= 3) break; // máximo 3 para no saturar
    }
  }
  // fallback: si el rationale existe pero no matchea, mostrar fragmento limpio
  if (results.length === 0 && rationale.length > 0) {
    const clean = rationale.replace(/[{}\[\]"]/g, "").slice(0, 60);
    results.push({ tag: clean, color: "#9fb6cc" });
  }
  return results;
}

export function deriveCandles(state) {
  const market = state?.market;
  if (!Array.isArray(market) || market.length === 0) return [];
  const out = [];
  for (const row of market) {
    const t = new Date(row.timestamp).getTime() / 1000;
    if (!Number.isFinite(t)) continue;
    out.push({
      time: Math.floor(t),
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume) || 0,
      rsi: Number(row.rsi) || null,
      ema_slow: Number(row.ema_slow) || null,
    });
  }
  // dedupe por time conservando el último
  const dedup = new Map();
  for (const c of out) dedup.set(c.time, c);
  return Array.from(dedup.values()).sort((a, b) => a.time - b.time);
}

export function deriveMarkers(closedTrades, openPositions, focusSymbol) {
  const markers = [];
  const closed = Array.isArray(closedTrades) ? closedTrades : [];
  const open = Array.isArray(openPositions) ? openPositions : [];

  for (const t of closed) {
    if (focusSymbol && t.symbol !== focusSymbol) continue;
    const opened = Date.parse(t.opened_at || t.entry_time || 0);
    const closedAt = Date.parse(t.closed_at || t.exit_time || 0);
    if (Number.isFinite(opened)) {
      markers.push({
        time: Math.floor(opened / 1000),
        position: "belowBar",
        color: "#22d3ee",
        shape: "arrowUp",
        text: `IN ${t.scenario || "?"}`,
      });
    }
    if (Number.isFinite(closedAt)) {
      const win = (t.pnl_usdt || 0) > 0;
      markers.push({
        time: Math.floor(closedAt / 1000),
        position: "aboveBar",
        color: win ? "#22c55e" : "#ef4444",
        shape: "arrowDown",
        text: `${win ? "+" : ""}${(t.pnl_usdt ?? 0).toFixed(3)}`,
      });
    }
  }

  for (const p of open) {
    if (focusSymbol && p.symbol !== focusSymbol) continue;
    const opened = Date.parse(p.opened_at || 0);
    if (Number.isFinite(opened)) {
      markers.push({
        time: Math.floor(opened / 1000),
        position: "belowBar",
        color: "#facc15",
        shape: "arrowUp",
        text: `LIVE ${p.scenario || ""}`,
      });
    }
  }

  return markers.sort((a, b) => a.time - b.time);
}

export function deriveDecisionMatrix(state) {
  const ts = state?.technical_signal || {};
  const settings = state?.settings || {};
  const rsi = Number(ts.rsi) || 0;
  const slope = Number(ts.rsi_slope) || 0;
  const vol = Number(ts.volume_ratio) || 0;
  const accel = Number(ts.volume_acceleration) || 0;
  const green = Boolean(ts.green_candle);
  const close = Number(ts.close) || 0;
  const ema = Number(ts.ema_slow) || 0;
  const bullCross = Boolean(ts.bullish_cross);
  const minVol = Number(settings.min_volume_ratio ?? 0.15);
  const rsiMaxA = Number(settings.scenario_a_rsi_max ?? 52);
  const rsiMaxB = Number(settings.scenario_b_rsi_max ?? 36);

  return [
    {
      id: "A",
      label: "Pullback",
      description: "Rebote sobre EMA20 tras retroceso. Compra momentum renovado.",
      active: Boolean(ts.scenario_a),
      checks: [
        {
          name: `RSI ≤ ${rsiMaxA}`, ok: rsi <= rsiMaxA, value: rsi.toFixed(1),
          tooltip: rsi <= rsiMaxA
            ? `✅ RSI ${rsi.toFixed(1)} en zona de pullback sano (≤ ${rsiMaxA}). Hay momentum disponible para el rebote sin sobreextensión.`
            : `⛔ RSI ${rsi.toFixed(1)} supera ${rsiMaxA}. El impulso está agotado: comprar aquí implica perseguir un movimiento ya maduro.`,
        },
        {
          name: "close > EMA20", ok: close > ema, value: close > ema ? "✓" : "✗",
          tooltip: close > ema
            ? `✅ Precio (${close.toFixed(2)}) sobre EMA20 (${ema.toFixed(2)}). La media actúa como soporte dinámico — estamos en zona de re-entrada válida.`
            : `⛔ Precio por debajo de la EMA20. La tendencia primaria está comprometida; un pullback sin soporte EMA puede convertirse en continuación bajista.`,
        },
        {
          name: "slope ≥ -1.8", ok: slope >= -1.8, value: slope.toFixed(2),
          tooltip: slope >= -1.8
            ? `✅ RSI slope ${slope.toFixed(2)}: la caída se está frenando o revirtiendo. (rsi[-1] − rsi[-3]). Entramos cuando el RSI frena, no cuando aún cae.`
            : `⛔ RSI slope ${slope.toFixed(2)} < -1.8: el RSI aún cae con fuerza. Compramos antes de que el cuchillo toque el suelo — riesgo de pérdida inmediata.`,
        },
        {
          name: `Vol ≥ ${minVol}`, ok: vol >= minVol, value: vol.toFixed(2),
          tooltip: vol >= minVol
            ? `✅ Volumen ${vol.toFixed(2)}x el promedio 20v. El rebote tiene participantes reales detrás.`
            : `⛔ Volumen ${vol.toFixed(2)}x es insuficiente (mín ${minVol}x). Sin volumen, el rebote puede ser una trampa de baja liquidez.`,
        },
      ],
    },
    {
      id: "B",
      label: "Sobreventa",
      description: "Zona de pánico vendedor. Rebote explosivo inminente.",
      active: Boolean(ts.scenario_b),
      checks: [
        {
          name: `RSI ≤ ${rsiMaxB} o ≤28`, ok: rsi <= rsiMaxB || rsi <= 28, value: rsi.toFixed(1),
          tooltip: rsi <= 28
            ? `🔥 Deep-Oversold activo: RSI ${rsi.toFixed(1)} < 28. Zona de pánico extremo — los rebotes más explosivos ocurren aquí. Override activo: se ignora green_candle.`
            : rsi <= rsiMaxB
            ? `✅ RSI ${rsi.toFixed(1)} en sobreventa (≤ ${rsiMaxB}). Históricamente, niveles bajos preceden reversiones técnicas significativas.`
            : `⛔ RSI ${rsi.toFixed(1)} no está en zona de sobreventa (necesita ≤ ${rsiMaxB} o ≤ 28). Sin señal de agotamiento vendedor.`,
        },
        {
          name: "Vela verde o deep", ok: green || rsi <= 28, value: green ? "verde" : rsi <= 28 ? "deep-OB" : "roja",
          tooltip: rsi <= 28
            ? `✅ Deep-Oversold override: RSI < 28 ignora el color de la vela. Los knife-catches más rentables tienen vela roja inicial que luego explota al alza.`
            : green
            ? `✅ Vela verde confirma el freno vendedor. Los compradores están absorbiendo la oferta.`
            : `⛔ Vela roja sin deep-oversold. Sin confirmación de agotamiento bajista, el precio puede continuar cayendo.`,
        },
        {
          name: "slope ≥ -2.2", ok: rsi <= 28 || slope >= -2.2, value: slope.toFixed(2),
          tooltip: rsi <= 28
            ? `✅ Deep-Oversold activo: en RSI < 28 el slope se ignora — el agotamiento es tan extremo que cualquier rebote está justificado.`
            : slope >= -2.2
            ? `✅ Slope ${slope.toFixed(2)}: la caída no es vertical. El sell-off está perdiendo fuerza.`
            : `⛔ Slope ${slope.toFixed(2)} < -2.2: caída violenta activa. Esperar estabilización antes de entrar.`,
        },
        {
          name: `Vol ≥ ${minVol}`, ok: vol >= minVol, value: vol.toFixed(2),
          tooltip: vol >= minVol
            ? `✅ Volumen de capitulación presente (${vol.toFixed(2)}x). El pánico tiene cuerpo — ideal para rebote.`
            : `⛔ Pánico sin volumen (${vol.toFixed(2)}x < ${minVol}x). La sobreventa puede extenderse sin catalizador de demanda.`,
        },
      ],
    },
    {
      id: "C",
      label: "Continuación",
      description: "Tendencia alcista sostenida con momentum sano. Ride the wave.",
      active: Boolean(ts.scenario_c),
      checks: [
        {
          name: `RSI ${rsiMaxA + 0.1}-70`, ok: rsi > rsiMaxA && rsi <= 70, value: rsi.toFixed(1),
          tooltip: rsi > rsiMaxA && rsi <= 70
            ? `✅ Zona de momentum sostenible (${(rsiMaxA + 0.1).toFixed(0)}-70). RSI ${rsi.toFixed(1)} = impulso activo sin sobreextensión. El rally tiene recorrido.`
            : rsi > 70
            ? `⛔ RSI ${rsi.toFixed(1)} > 70: sobrecompra. Comprar aquí = comprar la cima. Esperar retroceso.`
            : `⛔ RSI ${rsi.toFixed(1)} ≤ ${rsiMaxA}: el mercado no tiene momentum suficiente para continuación. Más apto para escenario A.`,
        },
        {
          name: "Vela verde", ok: green, value: green ? "✓" : "✗",
          tooltip: green
            ? `✅ Vela de cierre verde: los compradores mantienen control al cierre. Continuación validada.`
            : `⛔ Vela roja en tendencia alcista = posible distribución. Sin confirmación alcista al cierre, el momentum puede estar girando.`,
        },
        {
          name: "Vol accel ≥ 0.80", ok: accel >= 0.80, value: accel.toFixed(2),
          tooltip: accel >= 0.80
            ? `✅ Aceleración de volumen ${accel.toFixed(2)}x (vs últimas 3 velas). El dinero fluye hacia esta dirección — continuación respaldada.`
            : `⛔ Volumen decelerando (${accel.toFixed(2)}x < 0.80). Momentum sin combustible = rally que se agota. Riesgo de trampa alcista.`,
        },
        {
          name: "slope ≥ -1.5", ok: slope >= -1.5, value: slope.toFixed(2),
          tooltip: slope >= -1.5
            ? `✅ RSI slope ${slope.toFixed(2)}: el indicador mantiene dirección alcista o neutral. La tendencia no está girando.`
            : `⛔ RSI slope ${slope.toFixed(2)} < -1.5: el RSI ya está girando hacia abajo dentro de la tendencia. Señal temprana de agotamiento.`,
        },
      ],
    },
    {
      id: "D",
      label: "EMA Cross",
      description: "Cruce alcista EMA9/EMA20. Captura el giro exacto de momentum.",
      active: Boolean(ts.scenario_d),
      checks: [
        {
          name: "Bullish cross", ok: bullCross, value: bullCross ? "✓" : "✗",
          tooltip: bullCross
            ? `✅ EMA rápida (9) cruzó por encima de EMA lenta (20) en esta vela. Este cruce captura el momento exacto del cambio de momentum — señal técnica clásica y de alta fidelidad.`
            : `⛔ Sin cruce activo. EMA9 y EMA20 no han cruzado en la vela actual. Esperando el evento exacto.`,
        },
        {
          name: "RSI 40-65", ok: rsi >= 40 && rsi <= 65, value: rsi.toFixed(1),
          tooltip: rsi >= 40 && rsi <= 65
            ? `✅ RSI ${rsi.toFixed(1)} en zona de aceleración (40-65). El movimiento tiene espacio para correr. Ideal para capturar la primera ola tras el cruce.`
            : rsi > 65
            ? `⛔ RSI ${rsi.toFixed(1)} > 65: el cruce llega tarde — el movimiento ya está avanzado. Riesgo de comprar el pico de la ola.`
            : `⛔ RSI ${rsi.toFixed(1)} < 40: cruce prematuro en debilidad. Esperar más confirmación de que el momentum está cambiando.`,
        },
        {
          name: "Vol ≥ 0.65", ok: vol >= 0.65, value: vol.toFixed(2),
          tooltip: vol >= 0.65
            ? `✅ Volumen ${vol.toFixed(2)}x. El cruce tiene participación de mercado — no es un cruce en el vacío.`
            : `⛔ Volumen ${vol.toFixed(2)}x < 0.65x. Cruce sin respaldo de mercado = señal falsa probable. Esperar confirmación de volumen.`,
        },
        {
          name: `Vol ≥ ${minVol}`, ok: vol >= minVol, value: vol.toFixed(2),
          tooltip: vol >= minVol
            ? `✅ Liquidez mínima presente (${vol.toFixed(2)}x ≥ ${minVol}x). Spread controlado, ejecución eficiente posible.`
            : `⛔ Volumen base insuficiente (${vol.toFixed(2)}x < ${minVol}x). Mercado sin actividad — slippage elevado en ejecución.`,
        },
      ],
    },
  ];
}

// ── Narrativa para eventos de auditoría ───────────────────────────────────────
function narrativeScan(s) {
  const conf = (s.confidence ?? 0).toFixed(2);
  const sym = s.symbol || "?";
  const scenario = s.scenario || "-";
  const rsi = s.rsi != null ? ` · RSI ${Number(s.rsi).toFixed(1)}` : "";

  if (s.signal === "buy") {
    if (s.approved === false) {
      return `⚠️ Setup ${scenario} detectado en ${sym} — IA descarta (conf ${conf} bajo umbral)${rsi}. Filtro activo.`;
    }
    return `🎯 Entrada ${scenario} confirmada en ${sym} — IA aprueba con conf ${conf}${rsi}.`;
  }
  if (s.signal === "hold") {
    const rsiVal = s.rsi != null ? Number(s.rsi) : null;
    if (rsiVal != null && rsiVal > 70) {
      return `⛔ ${sym} sobrecomprado (RSI ${rsiVal.toFixed(1)}). Esperando retroceso a zona de valor.`;
    }
    return `⏸ ${sym} sin escenario ejecutable${rsi}. Mercado neutral o en consolidación.`;
  }
  return `ℹ️ ${sym} SCAN · conf ${conf}${rsi}`;
}

function narrativeExit(c) {
  const pnl = Number(c.pnl_usdt || 0);
  const sym = c.symbol || "?";
  const reason = (c.exit_reason || "").toLowerCase();
  const pnlStr = `${pnl > 0 ? "+" : ""}${pnl.toFixed(4)} USDT`;

  if (pnl > 0) {
    if (reason.includes("take_profit") || reason.includes("tp")) {
      return `🏆 Target alcanzado en ${sym}: ${pnlStr}. TP ejecutado limpiamente.`;
    }
    if (reason.includes("trailing") || reason.includes("trail")) {
      return `📈 Trailing SL ejecutado en ${sym}: ${pnlStr}. Ganancia protegida con tendencia.`;
    }
    return `✅ Trade ganador en ${sym}: ${pnlStr} · motivo: ${c.exit_reason || "?"}.`;
  }
  if (reason.includes("emergency") || reason.includes("emerg")) {
    return `🚨 Cierre de emergencia en ${sym}: IA detectó capitulación. ${pnlStr}.`;
  }
  if (reason.includes("stop_loss") || reason.includes("sl")) {
    return `🛡 Stop Loss activado en ${sym}: pérdida controlada. ${pnlStr} — dentro del riesgo definido.`;
  }
  return `🔴 Salida ${sym}: ${pnlStr} · ${c.exit_reason || "?"}`;
}

function narrativeMonitor(m) {
  const sym = m.symbol || "?";
  const rationale = m.rationale ? String(m.rationale).slice(0, 90) : "";
  switch (m.action) {
    case "EMERGENCY_CLOSE":
      return `🚨 Emergencia ${sym}: ${rationale || "señales de reversión detectadas por IA."}`;
    case "UPDATE_SL":
      return `🔒 SL movido a break-even en ${sym}. Ganancia asegurada. ${rationale ? `(${rationale.slice(0, 60)})` : ""}`;
    case "HOLD":
      return `🔍 Monitor ${sym}: posición saludable — momentum y flujo estables. ${rationale ? `(${rationale.slice(0, 55)})` : ""}`;
    default:
      return `ℹ️ AI-MON ${sym}: ${m.action || "?"} · ${rationale.slice(0, 80)}`;
  }
}

export function deriveAuditEvents(payload) {
  const events = [];
  const sigs = payload?.signalHistory || [];
  const closed = payload?.closedTrades || [];
  const monitor = payload?.tradeMonitorLog || [];

  for (const s of sigs.slice(-30)) {
    const t = Date.parse(s.timestamp || s.at || 0);
    events.push({
      time: Number.isFinite(t) ? t : Date.now(),
      level: s.signal === "buy" ? (s.approved === false ? "warn" : "info") : "muted",
      tag: "SCAN",
      symbol: s.symbol || "?",
      msg: narrativeScan(s),
      rawSignal: s.signal,
      confidence: s.confidence,
      rationale: s.rationale,
    });
  }
  for (const c of closed.slice(-20)) {
    const t = Date.parse(c.closed_at || c.exit_time || 0);
    const pnl = Number(c.pnl_usdt || 0);
    events.push({
      time: Number.isFinite(t) ? t : Date.now(),
      level: pnl > 0 ? "win" : "loss",
      tag: "EXIT",
      symbol: c.symbol || "?",
      msg: narrativeExit(c),
    });
  }
  for (const m of monitor.slice(-20)) {
    const t = Date.parse(m.evaluated_at || m.at || 0);
    events.push({
      time: Number.isFinite(t) ? t : Date.now(),
      level: m.action === "EMERGENCY_CLOSE" ? "warn" : m.action === "UPDATE_SL" ? "info" : "muted",
      tag: "AI-MON",
      symbol: m.symbol || "?",
      msg: narrativeMonitor(m),
      rationale: m.rationale,
    });
  }

  return events.sort((a, b) => b.time - a.time).slice(0, 60);
}
