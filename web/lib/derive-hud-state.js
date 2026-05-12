// Derivadores puros para extraer las vistas que necesita el HUD del payload SSE.
// No hay side-effects ni I/O: 100% client-safe.

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
      active: Boolean(ts.scenario_a),
      checks: [
        { name: `RSI ≤ ${rsiMaxA}`, ok: rsi <= rsiMaxA, value: rsi.toFixed(1) },
        { name: "close > EMA20", ok: close > ema, value: close > ema ? "✓" : "✗" },
        { name: "slope ≥ -1.8", ok: slope >= -1.8, value: slope.toFixed(2) },
        { name: `Vol ≥ ${minVol}`, ok: vol >= minVol, value: vol.toFixed(2) },
      ],
    },
    {
      id: "B",
      label: "Sobreventa",
      active: Boolean(ts.scenario_b),
      checks: [
        { name: `RSI ≤ ${rsiMaxB} o ≤28`, ok: rsi <= rsiMaxB || rsi <= 28, value: rsi.toFixed(1) },
        { name: "Vela verde o deep", ok: green || rsi <= 28, value: green ? "green" : rsi <= 28 ? "deep" : "red" },
        { name: "slope ≥ -2.2", ok: rsi <= 28 || slope >= -2.2, value: slope.toFixed(2) },
        { name: `Vol ≥ ${minVol}`, ok: vol >= minVol, value: vol.toFixed(2) },
      ],
    },
    {
      id: "C",
      label: "Continuación",
      active: Boolean(ts.scenario_c),
      checks: [
        { name: `RSI ${rsiMaxA + 0.1}-70`, ok: rsi > rsiMaxA && rsi <= 70, value: rsi.toFixed(1) },
        { name: "Vela verde", ok: green, value: green ? "✓" : "✗" },
        { name: "Vol accel ≥ 0.80", ok: accel >= 0.80, value: accel.toFixed(2) },
        { name: "slope ≥ -1.5", ok: slope >= -1.5, value: slope.toFixed(2) },
      ],
    },
    {
      id: "D",
      label: "EMA Cross",
      active: Boolean(ts.scenario_d),
      checks: [
        { name: "Bullish cross", ok: bullCross, value: bullCross ? "✓" : "✗" },
        { name: "RSI 40-65", ok: rsi >= 40 && rsi <= 65, value: rsi.toFixed(1) },
        { name: "Vol ≥ 0.65", ok: vol >= 0.65, value: vol.toFixed(2) },
        { name: `Vol ≥ ${minVol}`, ok: vol >= minVol, value: vol.toFixed(2) },
      ],
    },
  ];
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
      level: s.signal === "buy" ? "info" : "muted",
      tag: "SCAN",
      symbol: s.symbol || "?",
      msg: `${(s.signal || "hold").toUpperCase()} · IA ${(s.confidence ?? 0).toFixed(2)} · setup=${s.scenario || "-"}`,
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
      msg: `${c.exit_reason || "?"} · ${pnl > 0 ? "+" : ""}${pnl.toFixed(4)} USDT`,
    });
  }
  for (const m of monitor.slice(-20)) {
    const t = Date.parse(m.evaluated_at || m.at || 0);
    events.push({
      time: Number.isFinite(t) ? t : Date.now(),
      level: m.action === "EMERGENCY_CLOSE" ? "warn" : m.action === "UPDATE_SL" ? "info" : "muted",
      tag: "AI-MON",
      symbol: m.symbol || "?",
      msg: `${m.action || "HOLD"} · ${m.rationale ? String(m.rationale).slice(0, 80) : ""}`,
    });
  }

  return events.sort((a, b) => b.time - a.time).slice(0, 60);
}
