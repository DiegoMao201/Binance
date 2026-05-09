"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";


const Plot = dynamic(() => import("react-plotly.js"), { ssr: false });

const BUY = "#12d98b";
const SELL = "#eb4b61";
const BG = "#071018";
const TEXT = "#dce7f5";


function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(2)}%`;
}


function formatNumber(value, digits = 2) {
  return Number(value || 0).toFixed(digits);
}


function formatDate(value) {
  if (!value) {
    return "sin dato";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "sin dato";
  }

  const day = String(date.getUTCDate()).padStart(2, "0");
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const hour = String(date.getUTCHours()).padStart(2, "0");
  const minute = String(date.getUTCMinutes()).padStart(2, "0");
  const second = String(date.getUTCSeconds()).padStart(2, "0");
  return `${day}/${month}, ${hour}:${minute}:${second} UTC`;
}


function buildChartData(payload, focusSymbol) {
  const market = payload?.state?.market || [];
  const signalHistory = payload?.signalHistory || [];
  const x = market.map((item) => item.timestamp);
  const scopedSignals = focusSymbol
    ? signalHistory.filter((item) => item.symbol === focusSymbol)
    : signalHistory;
  const buySignals = scopedSignals.filter((item) => item.technical_signal === "buy" || item.ai_signal === "buy");
  const sellSignals = scopedSignals.filter((item) => item.technical_signal === "sell" || item.ai_signal === "sell");

  return [
    {
      type: "candlestick",
      x,
      open: market.map((item) => item.open),
      high: market.map((item) => item.high),
      low: market.map((item) => item.low),
      close: market.map((item) => item.close),
      increasing: { line: { color: BUY } },
      decreasing: { line: { color: SELL } },
      name: "Precio",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.ema_fast),
      line: { color: "#f4b942", width: 1.6 },
      name: "EMA 9",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.ema_slow),
      line: { color: "#57c1ff", width: 1.6 },
      name: "EMA 20",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.bb_upper),
      line: { color: "#68809b", width: 1 },
      name: "BB Upper",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.bb_lower),
      line: { color: "#68809b", width: 1 },
      fill: "tonexty",
      fillcolor: "rgba(104,128,155,0.08)",
      name: "BB Lower",
    },
    {
      type: "scatter",
      mode: "markers",
      x: buySignals.map((item) => item.timestamp),
      y: buySignals.map((item) => item.technical_price),
      marker: { color: BUY, size: 10, symbol: "triangle-up" },
      name: "Buy",
    },
    {
      type: "scatter",
      mode: "markers",
      x: sellSignals.map((item) => item.timestamp),
      y: sellSignals.map((item) => item.technical_price),
      marker: { color: SELL, size: 10, symbol: "triangle-down" },
      name: "Sell",
    },
  ];
}


function buildDecisionSummary(decision, technicalSignal, guardrails, focusScan) {
  const action = decision?.action || decision?.side || "hold";
  if (action === "buy") return `El bot disparo entrada por Escenario ${decision?.scenario || technicalSignal?.scenario || "?"}.`;
  if (action === "sell") return "El bot ve una salida defendible y ya consiguio validacion minima de tecnica, IA y riesgo.";
  if (decision?.reason?.includes("Kill Switch")) return "El bot se bloqueo para proteger capital porque la perdida acumulada supero el limite permitido.";

  const g = guardrails || decision || {};
  const prefix = focusScan?.symbol ? `${focusScan.symbol}: ` : "";
  const blockers = [];
  if (g.executable_signal === false) blockers.push("no se cumple Escenario A ni B");
  if (g.ai_confident === false) blockers.push(`la confianza de la IA esta por debajo del umbral (${formatPercent(g.ai_confidence)})`);
  if (g.ai_approved === false) blockers.push("la IA no aprobaria la entrada");
  if (g.ai_alignment === false) blockers.push("IA no esta alineada con la tecnica");
  if (g.ai_setup_ready === false) blockers.push("calidad de setup insuficiente para IA");
  if (g.ai_risk_clear === false) blockers.push("IA marca banderas de riesgo");
  if (g.volatility_ready === false) blockers.push("la volatilidad util no alcanza el rango deseado");
  if (g.volume_ready === false) blockers.push("el volumen no respalda la entrada");
  if (g.cooldown_active) blockers.push("el bot sigue en cooldown");
  if (!blockers.length) return `${prefix}El bot esta observando y espera una ventaja mas clara antes de exponer capital.`;
  return `${prefix}El bot no entra porque ${blockers.join(", ")}.`;
}


function buildAiSummary(ai) {
  if (!ai?.signal) return "La IA aún no produjo una lectura utilizable.";
  if (ai.signal === "hold") return `La IA recomienda esperar. Su convicción actual es ${formatPercent(ai.confidence)}.${ai?.cached ? ` Lectura reutilizada desde caché hace ${formatNumber(ai.cached_age_seconds, 0)}s.` : ""}`;
  return `La IA está inclinada a ${ai.signal === "buy" ? "comprar" : "vender"} con convicción ${formatPercent(ai.confidence)}.`;
}


function buildModelHealth(ai, isOnline) {
  if (!isOnline) return { tone: "sell", title: "Salud degradada", detail: "No hay heartbeat reciente del bot; la lectura del modelo no es confiable." };
  const confidence = Number(ai?.confidence || 0);
  if (confidence >= 0.65) return { tone: "buy", title: "Modelo fuerte", detail: "La IA ya entra en el rango exigido por el bot para operar." };
  if (confidence >= 0.5) return { tone: "warn", title: "Modelo prudente", detail: "La IA responde bien, pero todavía no ve una ventaja estadística suficiente." };
  return { tone: "sell", title: "Modelo débil", detail: "La lectura del modelo es demasiado tibia; lo correcto es no tocar mercado." };
}


function buildAlerts({ status, control, risk, decision, ai, isOnline }) {
  const alerts = [];
  if (!isOnline) alerts.push({ tone: "sell", title: "Bot offline", detail: "No se está recibiendo heartbeat reciente del bot." });
  if (control?.desired_state === "paused") alerts.push({ tone: "warn", title: "Bot en pausa", detail: "El proceso sigue vivo, pero no abrirá nuevas operaciones hasta que lo reanudes." });
  if (control?.desired_state === "stopped") alerts.push({ tone: "sell", title: "Bot detenido", detail: "El proceso fue marcado para detenerse; Coolify tendrá que relanzarlo." });
  if (risk?.kill_switch_triggered) alerts.push({ tone: "sell", title: "Kill switch activo", detail: "La protección de capital se disparó y el bot dejó de operar." });
  if ((decision?.action === "buy" || decision?.action === "sell") && Number(ai?.confidence || 0) >= 0.65) alerts.push({ tone: "buy", title: "Entrada operable", detail: "El bot detectó una oportunidad compatible con sus filtros estrictos." });
  if (!alerts.length) alerts.push({ tone: "neutral", title: "Monitoreo normal", detail: status?.detail || "El bot está filtrando oportunidades y protegiendo capital." });
  return alerts;
}


function buildScanBlockTelemetry(scan) {
  const guardrails = scan?.guardrails || {};
  const ai = scan?.ai_signal || {};
  const consulted = Boolean(scan?.ia_consulted);
  const flags = Array.isArray(ai.risk_flags) ? ai.risk_flags : [];

  if (scan?.status === "locked") return { tone: "tone-sell", text: "mutex global activo" };
  if (scan?.status === "waiting") return { tone: "", text: scan?.candidate_reason || scan?.rejection_reason || "sin escenario A/B" };
  if (scan?.status === "scenario_only") return { tone: "", text: scan?.candidate_reason || "falta volumen o ATR para ser candidato" };
  if (!consulted) return { tone: "", text: "pendiente de evaluación IA" };

  const blockers = [];
  if (guardrails.executable_signal === false) blockers.push("señal no ejecutable");
  if (guardrails.ai_confident === false) blockers.push(`confianza baja (${formatPercent(guardrails.ai_confidence)})`);
  if (guardrails.ai_approved === false) blockers.push("IA no aprueba");
  if (guardrails.ai_alignment === false) blockers.push("IA desalineada");
  if (guardrails.ai_setup_ready === false) blockers.push("setup insuficiente");
  if (guardrails.ai_risk_clear === false) blockers.push(flags.length ? `flags: ${flags.join(", ")}` : "riesgo IA");
  if (guardrails.volume_ready === false) blockers.push(`volumen bajo (${formatNumber(scan?.volume_ratio, 2)}x)`);
  if (guardrails.volatility_ready === false) blockers.push(`ATR fuera de rango (${formatPercent(scan?.atr_pct)})`);
  if (guardrails.cooldown_active) blockers.push("cooldown activo");

  if (!blockers.length && guardrails.ai_gate_ready) return { tone: "tone-buy", text: "sin bloqueo: listo para ejecutar" };
  return { tone: blockers.length ? "tone-sell" : "", text: blockers.join(" | ") || "esperando confirmación" };
}


function buildTimeline(signalHistory) {
  return [...signalHistory].slice(-8).reverse().map((item) => ({
    ...item,
    action: item.decision_action === "hold" ? "Sin entrada" : item.decision_action.toUpperCase(),
    summary: item.decision_action === "hold"
      ? `Se descartó la entrada. Técnica=${item.technical_signal}, IA=${item.ai_signal}, convicción IA ${formatPercent(item.ai_confidence)}.`
      : `El bot marcó ${item.decision_action} a ${formatNumber(item.technical_price, 4)}.`,
  }));
}


// Tabla de tiers para mostrar el progreso break-even en vivo. Debe coincidir
// con `trailing_tiers` en main_loop.py para que la barra refleje exactamente
// lo que el backend va a ejecutar.
const TRAILING_TIERS = [
  { tier: 1, trigger: 0.005, offset: 0.002, label: "Break-even +0.2%" },
  { tier: 2, trigger: 0.008, offset: 0.004, label: "Lock +0.4%" },
  { tier: 3, trigger: 0.010, offset: 0.006, label: "Lock +0.6%" },
  { tier: 4, trigger: 0.014, offset: 0.008, label: "Lock +0.8%" },
  { tier: 5, trigger: 0.018, offset: 0.010, label: "Lock +1.0%" },
];


function resolveTierFromMfe(mfePct) {
  let tier = 0;
  for (const candidate of TRAILING_TIERS) {
    if (Number(mfePct || 0) >= candidate.trigger) {
      tier = candidate.tier;
    }
  }
  return tier;
}


function buildExecutionAudit(orders, openPositions, closedTrades) {
  const latestOrder = orders.length ? orders[orders.length - 1] : null;
  const latestOpenPosition = openPositions.length ? openPositions[openPositions.length - 1] : null;
  const latestClosedTrade = closedTrades.length ? closedTrades[closedTrades.length - 1] : null;

  return {
    latestOrder,
    latestOpenPosition,
    latestClosedTrade,
    latestOrderStatus: latestOrder?.status || "sin ordenes",
    latestOrderMode: latestOrder?.mode || latestOpenPosition?.mode || "n/d",
    latestOrderReason: latestOrder?.reason || latestClosedTrade?.exit_reason || "Sin incidencias recientes.",
  };
}


function buildEquityCurve(equityHistory) {
  const points = Array.isArray(equityHistory) ? equityHistory.slice(-60) : [];
  return [
    {
      type: "scatter",
      mode: "lines",
      x: points.map((item) => item.timestamp),
      y: points.map((item) => item.equity_usdt),
      line: { color: BUY, width: 2 },
      name: "Equity",
    },
    {
      type: "scatter",
      mode: "lines",
      x: points.map((item) => item.timestamp),
      y: points.map((item) => item.high_water_mark),
      line: { color: "#f4b942", width: 1.5, dash: "dot" },
      name: "HWM",
    },
  ];
}


function MetricCard({ label, value, tone = "neutral", subvalue }) {
  return (
    <div className={`metric-card ${tone}`}>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {subvalue ? <span className="metric-subvalue">{subvalue}</span> : null}
    </div>
  );
}


function StatusPill({ label, active }) {
  return <span className={`status-pill ${active ? "active" : "inactive"}`}>{label}</span>;
}


function ProgressBar({ value, max = 1, tone = "buy", height = 8, label }) {
  const pct = Math.max(0, Math.min(1, max > 0 ? value / max : 0));
  const fillColor = tone === "buy" ? "#12d98b" : tone === "warn" ? "#f4b942" : tone === "sell" ? "#eb4b61" : "#57c1ff";
  return (
    <div style={{ width: "100%" }}>
      {label ? <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 4 }}>{label}</div> : null}
      <div style={{ background: "rgba(255,255,255,0.06)", borderRadius: 6, height, overflow: "hidden" }}>
        <div style={{ background: fillColor, width: `${pct * 100}%`, height: "100%", transition: "width 0.5s ease" }} />
      </div>
    </div>
  );
}


function buildLivePositionView(openPosition, openPositions) {
  // Si tenemos una position completa en openPositions (incluye trailing_tier) la
  // preferimos sobre el snapshot summary (open_position).
  const summary = openPosition || null;
  if (!summary) return null;
  const fullPos = openPositions?.find((p) => p.symbol === summary.symbol) || summary;
  const entry = Number(fullPos.entry_price || summary.entry_price || 0);
  const mark = Number(fullPos.mark_price || summary.mark_price || entry);
  const sl = Number(fullPos.stop_loss || summary.stop_loss || 0);
  const tp = Number(fullPos.take_profit || summary.take_profit || 0);
  const mfePct = Number(fullPos.mfe_pct || summary.mfe_pct || 0);
  const maePct = Number(fullPos.mae_pct || summary.mae_pct || 0);
  const unrealizedPct = entry > 0 ? (mark - entry) / entry : 0;
  const tier = Number(fullPos.trailing_tier || summary.trailing_tier || 0);
  const achievedTier = resolveTierFromMfe(mfePct);
  const initialSL = Number(fullPos.initial_stop_loss || sl);
  const slLockedAboveEntry = sl > entry && entry > 0;

  const currentTier = TRAILING_TIERS.find((t) => t.tier === tier);
  const currentAchievedTier = TRAILING_TIERS.find((t) => t.tier === achievedTier);
  const nextTier = TRAILING_TIERS.find((t) => t.tier === achievedTier + 1);
  const distanceToNextTier = nextTier ? nextTier.trigger - mfePct : 0;

  return {
    symbol: fullPos.symbol,
    side: fullPos.side,
    entry,
    mark,
    sl,
    tp,
    mfePct,
    maePct,
    unrealizedPct,
    unrealizedUsdt: Number(fullPos.unrealized_pnl_usdt || summary.unrealized_pnl_usdt || 0),
    tier,
    achievedTier,
    currentTier,
    currentAchievedTier,
    nextTier,
    distanceToNextTier,
    initialSL,
    slLockedAboveEntry,
    tierSyncPending: achievedTier > tier,
    holdMinutes: Number(fullPos.hold_minutes || 0),
    scenario: fullPos.scenario || summary.scenario,
  };
}


export default function DashboardClient({ initialData }) {
  const [payload, setPayload] = useState(initialData);
  const [controlBusy, setControlBusy] = useState(false);

  async function refreshState() {
    const response = await fetch("/api/state", { cache: "no-store" });
    const next = await response.json();
    setPayload(next);
  }

  useEffect(() => {
    const intervalId = window.setInterval(async () => {
      await refreshState();
    }, 10000);
    return () => window.clearInterval(intervalId);
  }, []);

  const state = payload?.state || {};
  const status = payload?.status || {};
  const control = payload?.control || {};
  const risk = state?.risk || {};
  const portfolio = state?.portfolio || {};
  const decision = state?.decision || {};
  const ai = state?.ai_signal || {};
  const technicalSignal = state?.technical_signal || {};
  const orders = payload?.orderHistory || [];
  const signalHistory = payload?.signalHistory || [];
  const openPositions = payload?.openPositions || state?.open_positions || [];
  const closedTrades = payload?.closedTrades || state?.closed_trades || [];
  const equityHistory = payload?.equityHistory || [];
  const preFlight = payload?.preFlight || {};
  const lastScans = state?.last_scans || [];
  const targetSymbols = state?.target_symbols || [];
  const globalLock = Boolean(state?.global_lock);
  const activeSymbol = state?.active_symbol || null;
  const openPosition = state?.open_position || null;
  const perSymbolStats = portfolio?.per_symbol_stats || {};
  const symbolUniverse = useMemo(() => {
    const all = new Set();
    targetSymbols.forEach((s) => all.add(s));
    Object.keys(perSymbolStats).forEach((s) => all.add(s));
    return Array.from(all);
  }, [targetSymbols, perSymbolStats]);
  const [statsFilter, setStatsFilter] = useState("ALL");
  const [focusSymbol, setFocusSymbol] = useState("");

  useEffect(() => {
    const preferredSymbol = activeSymbol || openPosition?.symbol || lastScans[0]?.symbol || targetSymbols[0] || "";
    if (!preferredSymbol) return;
    setFocusSymbol((current) => (current && current === preferredSymbol) || current ? current : preferredSymbol);
  }, [activeSymbol, openPosition, lastScans, targetSymbols]);

  const isOnline = useMemo(() => {
    const heartbeat = status?.heartbeat_at;
    if (!heartbeat) return false;
    const referenceTime = payload?.serverTime ? new Date(payload.serverTime).getTime() : Number.NaN;
    if (Number.isNaN(referenceTime)) return false;
    return referenceTime - new Date(heartbeat).getTime() < 120000;
  }, [payload?.serverTime, status?.heartbeat_at]);

  const focusScan = useMemo(() => {
    if (!lastScans.length) return null;
    return lastScans.find((scan) => scan.symbol === focusSymbol) || lastScans.find((scan) => scan.symbol === activeSymbol) || lastScans[0];
  }, [lastScans, focusSymbol, activeSymbol]);
  const chartData = useMemo(() => buildChartData(payload, focusScan?.symbol), [payload, focusScan]);
  const latestSignal = signalHistory.length ? signalHistory[signalHistory.length - 1] : null;
  const focusGuardrails = focusScan?.guardrails || decision;
  const focusAi = focusScan?.ai_signal || ai;
  const decisionSummary = buildDecisionSummary(decision, technicalSignal, focusGuardrails, focusScan);
  const aiSummary = buildAiSummary(focusAi);
  const modelHealth = buildModelHealth(focusAi, isOnline);
  const alerts = buildAlerts({ status, control, risk, decision, ai, isOnline });
  const timeline = buildTimeline(signalHistory);
  const executionAudit = buildExecutionAudit(orders, openPositions, closedTrades);
  const equityCurve = useMemo(() => buildEquityCurve(equityHistory), [equityHistory]);
  const livePos = useMemo(() => buildLivePositionView(openPosition, openPositions), [openPosition, openPositions]);

  async function sendControl(desiredState) {
    setControlBusy(true);
    try {
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ desiredState, reason: `Cambio a ${desiredState} desde el panel operativo.` }),
      });
      await refreshState();
    } finally {
      setControlBusy(false);
    }
  }

  return (
    <main className="page-shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">OptiFerre Terminal</p>
          <h1>Control operativo</h1>
        </div>
        <div className="sidebar-block"><span>Universo</span><strong>{(targetSymbols.length ? targetSymbols : [status.symbol || "ETH/USDT"]).join(" · ")}</strong></div>
        <div className="sidebar-block"><span>Mutex global</span><strong className={globalLock ? "tone-sell" : "tone-buy"}>{globalLock ? `LOCK · ${activeSymbol}` : "LIBRE"}</strong></div>
        <div className="sidebar-block"><span>Heartbeat</span><strong>{formatDate(status.heartbeat_at)}</strong></div>
        <div className="sidebar-block"><span>Bot</span><strong className={isOnline ? "tone-buy" : "tone-sell"}>{isOnline ? "ONLINE" : "OFFLINE"}</strong></div>
        <div className="sidebar-block"><span>Estado deseado</span><strong>{control.desired_state || "running"}</strong></div>
        <div className="sidebar-block"><span>Modelo IA</span><strong>{ai?.model || "OpenRouter"}</strong></div>
        <div className="sidebar-block"><span>Detalle</span><strong>{status.detail || "n/d"}</strong></div>
        <div className="sidebar-block nav-block">
          <span>Navegación</span>
          <div className="nav-pills">
            <Link href="/" className="nav-pill active">Panel</Link>
            <Link href="/matriz" className="nav-pill">Matriz</Link>
          </div>
        </div>
        <div className="sidebar-block control-panel">
          <span>Mandos</span>
          <div className="button-stack">
            <button disabled={controlBusy} className="control-btn run" onClick={() => sendControl("running")}>Prender</button>
            <button disabled={controlBusy} className="control-btn pause" onClick={() => sendControl("paused")}>Pausar</button>
            <button disabled={controlBusy} className="control-btn stop" onClick={() => sendControl("stopped")}>Detener</button>
          </div>
        </div>
      </aside>

      <section className="content">
        <header className="hero">
          <div>
            <p className="eyebrow">Bloomberg-style monitor</p>
            <h2>Centro de mando del bot</h2>
          </div>
          <div className="hero-actions">
            <Link href="/matriz" className="hero-link">Abrir matriz completa</Link>
            <div className="timestamp">Servidor: {formatDate(payload?.serverTime)}</div>
          </div>
        </header>

        <section className="alerts-grid">
          {alerts.map((alert, index) => (
            <article key={`${alert.title}-${index}`} className={`alert-card ${alert.tone}`}>
              <span className="alert-kicker">Alerta</span>
              <h3>{alert.title}</h3>
              <p>{alert.detail}</p>
            </article>
          ))}
        </section>

        <section className="metrics-grid">
          <MetricCard label="Modo" value={portfolio.mode || (status?.dry_run ? "dry_run" : "live")} tone={(portfolio.mode || "live") === "live" ? "buy" : "warn"} subvalue={`Control ${control.desired_state || "running"}`} />
          <MetricCard label="Balance USDT" value={formatNumber(risk.balance_usd)} subvalue={`Equity ${formatNumber(risk.equity_usd)}`} />
          <MetricCard label="PnL acumulado" value={`${formatNumber(portfolio.realized_pnl_usdt)} USDT`} tone={Number(portfolio.realized_pnl_usdt) >= 0 ? "buy" : "sell"} subvalue={formatPercent(portfolio.accumulated_pnl_pct)} />
          <MetricCard label="Win rate" value={`${formatNumber(portfolio.win_rate_pct, 2)}%`} subvalue={`${portfolio.wins || 0} W / ${portfolio.losses || 0} L`} />
          <MetricCard label="Max drawdown" value={formatPercent(portfolio.max_drawdown_pct)} tone={Number(portfolio.max_drawdown_pct) >= 0.02 ? "sell" : "buy"} subvalue={`HWM ${formatNumber(portfolio.high_water_mark_usdt)}`} />
          <MetricCard label="Velocidad DD" value={`${formatNumber((Number(portfolio.drawdown_velocity_seconds) || 0) / 60, 1)} min`} tone={Number(portfolio.max_drawdown_pct) >= 0.02 ? "sell" : "neutral"} subvalue={`Ultimo HWM ${formatDate(portfolio.last_hwm_at)}`} />
          <MetricCard label="Inventario spot" value={`${formatNumber(portfolio?.asset_holdings?.free, 6)} ${portfolio?.asset_holdings?.asset || ""}`} subvalue={`Total ${formatNumber(portfolio?.asset_holdings?.total, 6)}`} />
          <MetricCard label="Pre-flight" value={preFlight.ok ? "VERDE" : "BLOQUEADO"} tone={preFlight.ok ? "buy" : "sell"} subvalue={preFlight.detail || "Sin chequeo"} />
          <MetricCard label="Kill Switch" value={risk.kill_switch_triggered ? "ACTIVO" : "SEGURO"} tone={risk.kill_switch_triggered ? "sell" : "buy"} />
          <MetricCard label="Decisión" value={decision.action || decision.side || "hold"} subvalue={decision.reason || decision.status || "n/d"} />
        </section>

        <section className="panel" style={{ marginBottom: "1rem" }}>
          <div className="panel-header">
            <h3>Radar enfocado</h3>
            <span>
              <select
                value={focusScan?.symbol || ""}
                onChange={(e) => setFocusSymbol(e.target.value)}
                style={{ background: "transparent", color: "inherit", border: "1px solid #2a3744", borderRadius: 4, padding: "2px 6px", font: "inherit" }}
              >
                {(targetSymbols.length ? targetSymbols : lastScans.map((scan) => scan.symbol)).map((symbol) => (
                  <option key={symbol} value={symbol}>{symbol}</option>
                ))}
              </select>
            </span>
          </div>
          <section className="telemetry-grid">
            <div className="telemetry-card"><span>Símbolo</span><strong>{focusScan?.symbol || activeSymbol || technicalSignal.symbol || "sin dato"}</strong></div>
            <div className="telemetry-card"><span>Estado backend</span><strong className={focusScan?.status === "candidate" ? "tone-buy" : focusScan?.status === "locked" ? "tone-sell" : ""}>{focusScan?.status || "waiting"}</strong></div>
            <div className="telemetry-card"><span>Escenarios</span><strong>{focusScan?.scenario_a ? "A" : "·"} / {focusScan?.scenario_b ? "B" : "·"}</strong></div>
            <div className="telemetry-card"><span>RSI</span><strong>{formatNumber(focusScan?.rsi ?? technicalSignal.rsi, 2)}</strong></div>
            <div className="telemetry-card"><span>ATR %</span><strong>{formatPercent(focusScan?.atr_pct ?? technicalSignal.atr_pct)}</strong></div>
            <div className="telemetry-card"><span>Volumen relativo</span><strong>{formatNumber(focusScan?.volume_ratio ?? technicalSignal.volume_ratio, 2)}x</strong></div>
            <div className="telemetry-card"><span>Precio</span><strong>{formatNumber(focusScan?.close ?? technicalSignal.close, 4)}</strong></div>
            <div className="telemetry-card">
              <span>AI confidence</span>
              <strong>{formatPercent(focusAi?.confidence)}</strong>
              <span className="metric-subvalue">
                {focusScan?.symbol || "símbolo"} · {focusAi?.cached ? `cache ${formatNumber(focusAi.cached_age_seconds, 0)}s` : focusAi?.consulted ? "evaluación fresca" : "no consultada"}
              </span>
            </div>
          </section>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h3>Scanner multi-moneda</h3>
            <span>{globalLock ? `Mutex activo · ${activeSymbol}` : `${lastScans.length || targetSymbols.length} tickers vigilados`}</span>
          </div>
          {openPosition ? (
            <div className="narrative-card compact" style={{ marginBottom: "0.75rem" }}>
              <strong>Posición abierta · {openPosition.symbol} {openPosition.side?.toUpperCase()}</strong>
              <p>
                Escenario {openPosition.scenario || "?"} · Entrada {formatNumber(openPosition.entry_price, 4)} · SL {formatNumber(openPosition.stop_loss, 4)} · TP {formatNumber(openPosition.take_profit, 4)}
              </p>
              <p>
                Mark {formatNumber(openPosition.mark_price, 4)} · PnL {formatNumber(openPosition.unrealized_pnl_usdt, 4)} USDT · MAE {formatPercent(openPosition.mae_pct)} · MFE {formatPercent(openPosition.mfe_pct)}
              </p>
            </div>
          ) : null}
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Símbolo</th>
                  <th>Estado backend</th>
                  <th>Esc A</th>
                  <th>Esc B</th>
                  <th>RSI</th>
                  <th>Precio</th>
                  <th>ATR %</th>
                  <th>Vol x</th>
                  <th>Motivo (backend)</th>
                </tr>
              </thead>
              <tbody>
                {lastScans.length === 0 ? (
                  <tr><td colSpan={9} style={{ opacity: 0.6 }}>Sin escaneos registrados aún.</td></tr>
                ) : lastScans.map((scan) => {
                  // Estado autoritativo: viene crudo del backend (_build_scan_summary en Python).
                  // Cero lógica derivada en el cliente. Si el backend dice "waiting", se muestra "waiting".
                  const isActive = scan.symbol === activeSymbol;
                  const status = scan.status || "waiting";
                  const tone =
                    status === "candidate" ? "tone-buy" :
                    status === "locked" ? "tone-sell" :
                    status === "scenario_only" ? "tone-warning" : "";
                  const renderBool = (val) => val === true ? <span className="tone-buy">✓</span> : <span style={{ opacity: 0.4 }}>·</span>;
                  return (
                    <tr key={scan.symbol} style={isActive ? { background: "rgba(244,185,66,0.08)" } : undefined}>
                      <td><strong>{scan.symbol}</strong>{isActive ? " ★" : ""}</td>
                      <td className={tone}>{status}</td>
                      <td>{renderBool(scan.scenario_a)}</td>
                      <td>{renderBool(scan.scenario_b)}</td>
                      <td>{formatNumber(scan.rsi, 2)}</td>
                      <td>{formatNumber(scan.close, 4)}</td>
                      <td>{formatPercent(scan.atr_pct)}</td>
                      <td>{formatNumber(scan.volume_ratio, 2)}x</td>
                      <td style={{ opacity: 0.7 }}>{scan.candidate_reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        {livePos ? (
          <section className="panel">
            <div className="panel-header">
              <h3>Posición viva · {livePos.symbol}</h3>
              <span>
                {livePos.slLockedAboveEntry ? "GANANCIA ASEGURADA" : "PROTECCIÓN INICIAL"} · Tier confirmado {livePos.tier}
                {livePos.tierSyncPending ? ` · MFE ya alcanzó Tier ${livePos.achievedTier}` : ""}
              </span>
            </div>
            <section className="telemetry-grid">
              <div className="telemetry-card"><span>Lado · Esc</span><strong>{livePos.side?.toUpperCase()} · {livePos.scenario || "?"}</strong></div>
              <div className="telemetry-card"><span>Entrada</span><strong>{formatNumber(livePos.entry, 4)}</strong></div>
              <div className="telemetry-card"><span>Mark</span><strong>{formatNumber(livePos.mark, 4)}</strong></div>
              <div className="telemetry-card">
                <span>PnL no realizado</span>
                <strong className={livePos.unrealizedUsdt >= 0 ? "tone-buy" : "tone-sell"}>{formatNumber(livePos.unrealizedUsdt, 4)} USDT</strong>
                <span className="metric-subvalue">{formatPercent(livePos.unrealizedPct)}</span>
              </div>
              <div className="telemetry-card"><span>Stop Loss</span><strong className={livePos.slLockedAboveEntry ? "tone-buy" : "tone-sell"}>{formatNumber(livePos.sl, 4)}</strong><span className="metric-subvalue">{livePos.slLockedAboveEntry ? "por encima de entry" : "inicial"}</span></div>
              <div className="telemetry-card"><span>Take Profit</span><strong>{formatNumber(livePos.tp, 4)}</strong></div>
              <div className="telemetry-card"><span>MFE alcanzado</span><strong className="tone-buy">{formatPercent(livePos.mfePct)}</strong></div>
              <div className="telemetry-card"><span>MAE</span><strong className="tone-sell">{formatPercent(livePos.maePct)}</strong></div>
              <div className="telemetry-card"><span>Hold</span><strong>{formatNumber(livePos.holdMinutes, 0)} min</strong></div>
            </section>
            <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
              <ProgressBar
                value={livePos.mfePct}
                max={Math.max(0.02, (livePos.nextTier?.trigger ?? livePos.currentAchievedTier?.trigger ?? 0.02))}
                tone="buy"
                label={`MFE ${formatPercent(livePos.mfePct)} · ${livePos.nextTier ? `próximo tier en +${formatPercent(livePos.distanceToNextTier)}` : "tier máximo alcanzado"}`}
              />
              {livePos.tierSyncPending ? (
                <span className="metric-subvalue">
                  El precio ya alcanzó Tier {livePos.achievedTier}, pero la protección persistida aún refleja Tier {livePos.tier}.
                </span>
              ) : null}
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {TRAILING_TIERS.map((t) => (
                  <span key={t.tier} className={`status-pill ${livePos.achievedTier >= t.tier ? "active" : "inactive"}`}>
                    T{t.tier} · ≥{formatPercent(t.trigger)} → SL +{formatPercent(t.offset)}
                  </span>
                ))}
              </div>
            </div>
          </section>
        ) : null}

        <section className="panel">
          <div className="panel-header">
            <h3>Mesa de IA · todos los símbolos</h3>
            <span>{lastScans.filter((s) => s.ia_consulted).length} consultados · {lastScans.length} vigilados</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Símbolo</th><th>Estado</th><th>Esc</th><th>IA Signal</th><th>Confidence</th><th>Approved</th><th>Setup</th><th>Risk flags</th><th>Bloqueo</th><th>Cache</th></tr></thead>
              <tbody>
                {lastScans.length === 0 ? (
                  <tr><td colSpan="10" className="empty">Sin escaneos.</td></tr>
                ) : lastScans.map((scan) => {
                  const ai = scan.ai_signal || {};
                  const consulted = scan.ia_consulted;
                  const conf = Number(ai.confidence || 0);
                  const confTone = conf >= 0.65 ? "tone-buy" : conf >= 0.5 ? "" : "tone-sell";
                  const flags = Array.isArray(ai.risk_flags) ? ai.risk_flags : [];
                  const block = buildScanBlockTelemetry(scan);
                  return (
                    <tr key={`mesa-${scan.symbol}`}>
                      <td><strong>{scan.symbol}</strong></td>
                      <td className={scan.status === "candidate" ? "tone-buy" : scan.status === "locked" ? "tone-sell" : ""}>{scan.status}</td>
                      <td>{scan.scenario || "·"}</td>
                      <td className={ai.signal === "buy" ? "tone-buy" : ai.signal === "sell" ? "tone-sell" : ""}>{consulted ? (ai.signal || "—") : "—"}</td>
                      <td className={confTone}>{consulted ? formatPercent(conf) : "—"}</td>
                      <td>{consulted ? (ai.approved ? <span className="tone-buy">✓</span> : <span className="tone-sell">✗</span>) : "—"}</td>
                      <td>{consulted ? (ai.setup_quality || "low") : "—"}</td>
                      <td style={{ opacity: 0.85 }}>{flags.length === 0 ? (consulted ? "—" : "") : flags.join(", ")}</td>
                      <td className={block.tone} style={{ minWidth: 260, opacity: 0.92 }}>{block.text}</td>
                      <td style={{ opacity: 0.7 }}>{consulted ? (ai.cached ? `${formatNumber(ai.cached_age_seconds, 0)}s` : "fresca") : "no consultada"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header"><h3>Profundidad orderbook · {focusScan?.symbol || "—"}</h3><span>Top 20 niveles spot</span></div>
            {focusScan?.orderbook && focusScan.orderbook.imbalance !== undefined ? (
              <>
                <section className="telemetry-grid">
                  <div className="telemetry-card"><span>Bid</span><strong className="tone-buy">{formatNumber(focusScan.orderbook.bid, 4)}</strong></div>
                  <div className="telemetry-card"><span>Ask</span><strong className="tone-sell">{formatNumber(focusScan.orderbook.ask, 4)}</strong></div>
                  <div className="telemetry-card"><span>Spread</span><strong>{formatPercent(focusScan.orderbook.spread_pct)}</strong></div>
                  <div className="telemetry-card">
                    <span>Imbalance</span>
                    <strong className={focusScan.orderbook.imbalance > 0.55 ? "tone-buy" : focusScan.orderbook.imbalance < 0.45 ? "tone-sell" : ""}>
                      {formatPercent(focusScan.orderbook.imbalance)}
                    </strong>
                    <span className="metric-subvalue">{focusScan.orderbook.imbalance > 0.55 ? "presión compradora" : focusScan.orderbook.imbalance < 0.45 ? "presión vendedora" : "neutro"}</span>
                  </div>
                </section>
                <div style={{ marginTop: 12 }}>
                  <ProgressBar
                    value={Number(focusScan.orderbook.bid_volume || 0)}
                    max={Number(focusScan.orderbook.bid_volume || 0) + Number(focusScan.orderbook.ask_volume || 0)}
                    tone="buy"
                    label={`Bid volume ${formatNumber(focusScan.orderbook.bid_volume, 2)} vs Ask volume ${formatNumber(focusScan.orderbook.ask_volume, 2)}`}
                  />
                </div>
              </>
            ) : (
              <p className="secondary-text">Orderbook se lee solo cuando el símbolo es candidato real (ahorro de rate-limit). {focusScan?.orderbook?.error ? `Error: ${focusScan.orderbook.error}` : ""}</p>
            )}
          </div>
          <div className="panel">
            <div className="panel-header"><h3>Régimen macro 15m · {focusScan?.symbol || "—"}</h3><span>EMA20/EMA50 + slope</span></div>
            {focusScan?.macro_regime && focusScan.macro_regime.trend ? (
              <section className="telemetry-grid">
                <div className="telemetry-card">
                  <span>Tendencia</span>
                  <strong className={focusScan.macro_regime.trend === "bullish" ? "tone-buy" : focusScan.macro_regime.trend === "bearish" ? "tone-sell" : ""}>
                    {focusScan.macro_regime.trend.toUpperCase()}
                  </strong>
                </div>
                <div className="telemetry-card"><span>Slope EMA50</span><strong className={Number(focusScan.macro_regime.slope_pct) >= 0 ? "tone-buy" : "tone-sell"}>{formatPercent(focusScan.macro_regime.slope_pct)}</strong></div>
                <div className="telemetry-card"><span>Close vs EMA50</span><strong className={focusScan.macro_regime.close_above_ema50 ? "tone-buy" : "tone-sell"}>{focusScan.macro_regime.close_above_ema50 ? "ARRIBA" : "ABAJO"}</strong></div>
                <div className="telemetry-card"><span>EMA20</span><strong>{formatNumber(focusScan.macro_regime.ema20, 4)}</strong></div>
                <div className="telemetry-card"><span>EMA50</span><strong>{formatNumber(focusScan.macro_regime.ema50, 4)}</strong></div>
                <div className="telemetry-card"><span>Close 15m</span><strong>{formatNumber(focusScan.macro_regime.close, 4)}</strong></div>
              </section>
            ) : (
              <p className="secondary-text">Régimen macro se lee solo cuando el símbolo es candidato real. {focusScan?.macro_regime?.error ? `Error: ${focusScan.macro_regime.error}` : ""}</p>
            )}
          </div>
        </section>

        <section className="panel chart-panel">
          <div className="panel-header"><h3>Precio y señales</h3><span>Últimas 50 velas</span></div>
          <Plot data={chartData} layout={{ autosize: true, paper_bgcolor: BG, plot_bgcolor: BG, font: { color: TEXT, family: "IBM Plex Mono, Menlo, monospace" }, margin: { l: 20, r: 20, t: 20, b: 30 }, xaxis: { rangeslider: { visible: false }, gridcolor: "#16202c" }, yaxis: { gridcolor: "#16202c" }, legend: { orientation: "h", y: 1.08, x: 0 } }} config={{ displayModeBar: false, responsive: true }} style={{ width: "100%", height: "540px" }} />
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header"><h3>Auditoría live</h3><span>Última ejecución</span></div>
            <div className="narrative-card compact">
              <strong>{executionAudit.latestOrderStatus}</strong>
              <p>{executionAudit.latestOrderReason}</p>
              <ul className="fact-list">
                <li>Modo de orden: {executionAudit.latestOrderMode}</li>
                <li>Última orden: {executionAudit.latestOrder ? formatDate(executionAudit.latestOrder.timestamp) : "sin ordenes"}</li>
                <li>Posición abierta: {executionAudit.latestOpenPosition ? `${formatNumber(executionAudit.latestOpenPosition.amount, 6)} @ ${formatNumber(executionAudit.latestOpenPosition.entry_price, 4)}` : "ninguna"}</li>
                <li>Último cierre: {executionAudit.latestClosedTrade ? `${formatNumber(executionAudit.latestClosedTrade.pnl_usdt, 4)} USDT` : "sin cierres"}</li>
              </ul>
            </div>
          </div>
          <div className="panel chart-panel">
            <div className="panel-header"><h3>Curva de equity</h3><span>Equity vs HWM</span></div>
            <Plot data={equityCurve} layout={{ autosize: true, paper_bgcolor: BG, plot_bgcolor: BG, font: { color: TEXT, family: "IBM Plex Mono, Menlo, monospace" }, margin: { l: 30, r: 20, t: 20, b: 30 }, xaxis: { gridcolor: "#16202c" }, yaxis: { gridcolor: "#16202c" }, legend: { orientation: "h", y: 1.08, x: 0 } }} config={{ displayModeBar: false, responsive: true }} style={{ width: "100%", height: "260px" }} />
          </div>
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header"><h3>Qué está haciendo el bot</h3><span>Decisión traducida</span></div>
            <div className="narrative-card">
              <strong>{decision.action === "hold" ? "En observación" : `Acción ${decision.action}`}</strong>
              <p>{decisionSummary}</p>
              <ul className="fact-list">
                <li>Símbolo evaluado: {focusScan?.symbol || technicalSignal.symbol || activeSymbol || "n/d"}</li>
                <li>Precio observado: {formatNumber(focusScan?.close ?? technicalSignal.close, 4)}</li>
                <li>RSI actual: {formatNumber(focusScan?.rsi ?? technicalSignal.rsi, 2)}</li>
                <li>Volumen relativo: {formatNumber(focusScan?.volume_ratio ?? technicalSignal.volume_ratio, 2)}x</li>
                <li>IA consultada: {focusScan?.ia_consulted ? `sí (${formatPercent(focusScan?.ia_confidence)})` : "no en este ciclo"}</li>
                <li>Posiciones abiertas: {openPositions.length}</li>
                <li>Heartbeat: {formatDate(status.heartbeat_at)}</li>
              </ul>
            </div>
          </div>
          <div className="panel">
            <div className="panel-header"><h3>Salud del modelo IA</h3><span>Lectura operativa</span></div>
            <div className={`narrative-card ${modelHealth.tone}`}>
              <strong>{modelHealth.title}</strong>
              <p>{aiSummary}</p>
              <p className="secondary-text">{modelHealth.detail}</p>
              <p className="secondary-text">Motivo principal: {ai?.rationale || "Sin explicación adicional."}</p>
            </div>
          </div>
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header"><h3>Guardrails (Logica OR)</h3><span>{focusScan?.symbol ? `Semaforos · ${focusScan.symbol}` : "Semaforos de entrada"}</span></div>
            <div className="pill-grid">
              <StatusPill label={`Escenario A ${focusGuardrails.scenario_a ? "OK" : "NO"}`} active={Boolean(focusGuardrails.scenario_a)} />
              <StatusPill label={`Escenario B ${focusGuardrails.scenario_b ? "OK" : "NO"}`} active={Boolean(focusGuardrails.scenario_b)} />
              <StatusPill label={`IA confianza ${focusGuardrails.ai_confident ? "OK" : "NO"}`} active={Boolean(focusGuardrails.ai_confident)} />
              <StatusPill label={`IA approved ${focusGuardrails.ai_approved ? "OK" : "NO"}`} active={Boolean(focusGuardrails.ai_approved)} />
              <StatusPill label={`IA alignment ${focusGuardrails.ai_alignment ? "OK" : "NO"}`} active={Boolean(focusGuardrails.ai_alignment)} />
              <StatusPill label={`IA setup ${focusGuardrails.ai_setup_ready ? "OK" : "NO"}`} active={Boolean(focusGuardrails.ai_setup_ready)} />
              <StatusPill label={`IA risk ${focusGuardrails.ai_risk_clear ? "OK" : "NO"}`} active={Boolean(focusGuardrails.ai_risk_clear)} />
              <StatusPill label={`Volatilidad ${focusGuardrails.volatility_ready ? "OK" : "NO"}`} active={Boolean(focusGuardrails.volatility_ready)} />
              <StatusPill label={`Volumen ${focusGuardrails.volume_ready ? "OK" : "NO"}`} active={Boolean(focusGuardrails.volume_ready)} />
              <StatusPill label={`Cooldown ${focusGuardrails.cooldown_active ? "ACTIVO" : "LIBRE"}`} active={!focusGuardrails.cooldown_active} />
              <StatusPill label={`Trigger ${focusGuardrails.executable_signal ? "OK" : "NO"}`} active={Boolean(focusGuardrails.executable_signal)} />
            </div>
          </div>
          <div className="panel">
            <div className="panel-header"><h3>Última evaluación</h3><span>Evento más reciente</span></div>
            <div className="narrative-card compact">
              <strong>{latestSignal ? latestSignal.decision_action : "Sin eventos"}</strong>
              <p>{latestSignal ? `A las ${formatDate(latestSignal.timestamp)} el bot vio ${latestSignal.technical_signal} por técnica y ${latestSignal.ai_signal} por IA. La convicción de la IA fue ${formatPercent(latestSignal.ai_confidence)}.` : "Todavía no hay eventos suficientes para construir esta explicación."}</p>
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header"><h3>Timeline de decisiones</h3><span>Últimos 8 eventos</span></div>
          <div className="timeline-list">
            {timeline.length === 0 ? <div className="timeline-empty">Todavía no hay suficiente actividad para construir la línea de tiempo.</div> : timeline.map((item, index) => (
              <article key={`${item.timestamp}-${index}`} className="timeline-item">
                <div className={`timeline-dot ${item.decision_action === "buy" ? "buy" : item.decision_action === "sell" ? "sell" : "hold"}`} />
                <div className="timeline-content">
                  <div className="timeline-head"><strong>{item.action}</strong><span>{formatDate(item.timestamp)}</span></div>
                  <p>{item.summary}</p>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header"><h3>Log de operaciones</h3><span>{orders.length} registros</span></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Hora</th><th>Lado</th><th>Estado</th><th>Señal</th><th>Fill</th><th>Slippage</th><th>Monto</th><th>Notional</th><th>SL</th><th>TP</th></tr></thead>
              <tbody>
                {orders.length === 0 ? <tr><td colSpan="10" className="empty">Sin operaciones todavía.</td></tr> : [...orders].reverse().map((order, index) => (
                  <tr key={`${order.timestamp}-${index}`}>
                    <td>{formatDate(order.timestamp)}</td>
                    <td className={order.side === "buy" ? "tone-buy" : "tone-sell"}>{order.side || "-"}</td>
                    <td>{order.status || "-"}</td>
                    <td>{formatNumber(order.signal_price ?? order.price, 4)}</td>
                    <td>{formatNumber(order.fill_price ?? order.avg_price ?? order.price, 4)}</td>
                    <td className={Number(order.slippage_pct) > 0.0015 ? "tone-sell" : ""}>{formatPercent(order.slippage_pct)}</td>
                    <td>{formatNumber(order.amount, 6)}</td>
                    <td>{formatNumber(order.notional_usdt, 2)}</td>
                    <td>{formatNumber(order.stop_loss, 4)}</td>
                    <td>{formatNumber(order.take_profit, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header"><h3>Resultado por operación</h3><span>{closedTrades.length} cierres</span></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Apertura</th><th>Cierre</th><th>Esc</th><th>Lado</th><th>Entrada</th><th>Salida</th><th>Slippage</th><th>Motivo</th><th>MAE %</th><th>MFE %</th><th>PnL USDT</th><th>PnL %</th></tr></thead>
              <tbody>
                {closedTrades.length === 0 ? <tr><td colSpan="12" className="empty">Sin cierres todavía. El bot solo mostrará ganancia o pérdida cuando una operación alcance TP o SL.</td></tr> : [...closedTrades].reverse().map((trade, index) => (
                  <tr key={`${trade.closed_at}-${index}`}>
                    <td>{formatDate(trade.opened_at)}</td>
                    <td>{formatDate(trade.closed_at)}</td>
                    <td>{trade.scenario || "-"}</td>
                    <td className={trade.side === "buy" ? "tone-buy" : "tone-sell"}>{trade.side || "-"}</td>
                    <td>{formatNumber(trade.entry_price, 4)}</td>
                    <td>{formatNumber(trade.exit_price, 4)}</td>
                    <td className={Number(trade.slippage_pct) > 0.0015 ? "tone-sell" : ""}>{formatPercent(trade.slippage_pct)}</td>
                    <td>{trade.exit_reason || "-"}</td>
                    <td className="tone-sell">{formatPercent(trade.mae_pct)}</td>
                    <td className="tone-buy">{formatPercent(trade.mfe_pct)}</td>
                    <td className={Number(trade.pnl_usdt) >= 0 ? "tone-buy" : "tone-sell"}>{formatNumber(trade.pnl_usdt, 4)}</td>
                    <td className={Number(trade.pnl_pct) >= 0 ? "tone-buy" : "tone-sell"}>{formatPercent(trade.pnl_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header">
              <h3>Telemetría por escenario</h3>
              <span>
                <select
                  value={statsFilter}
                  onChange={(e) => setStatsFilter(e.target.value)}
                  style={{ background: "transparent", color: "inherit", border: "1px solid #2a3744", borderRadius: 4, padding: "2px 6px", font: "inherit" }}
                >
                  <option value="ALL">Todos los símbolos</option>
                  {symbolUniverse.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </span>
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Escenario</th><th>Trades</th><th>W / L</th><th>Win rate</th><th>PnL USDT</th><th>MAE prom.</th><th>MFE prom.</th></tr></thead>
                <tbody>
                  {["A", "B"].map((label) => {
                    const stats = statsFilter === "ALL"
                      ? (portfolio?.scenario_stats?.[label] || {})
                      : (perSymbolStats?.[statsFilter]?.[label] || {});
                    return (
                      <tr key={label}>
                        <td><strong>Escenario {label}</strong> <span className="secondary-text">{label === "A" ? "Pullback" : "Sobreventa extrema"}</span></td>
                        <td>{stats.trades || 0}</td>
                        <td>{stats.wins || 0} / {stats.losses || 0}</td>
                        <td>{formatNumber(stats.win_rate_pct, 1)}%</td>
                        <td className={Number(stats.pnl_usdt) >= 0 ? "tone-buy" : "tone-sell"}>{formatNumber(stats.pnl_usdt, 4)}</td>
                        <td className="tone-sell">{formatPercent(stats.avg_mae_pct)}</td>
                        <td className="tone-buy">{formatPercent(stats.avg_mfe_pct)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
          <div className="panel">
            <div className="panel-header"><h3>Posiciones abiertas</h3><span>{openPositions.length} en curso</span></div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Apertura</th><th>Símbolo</th><th>Esc</th><th>Lado</th><th>Entrada</th><th>SL</th><th>TP</th><th>MAE %</th><th>MFE %</th><th>PnL no real.</th></tr></thead>
                <tbody>
                  {openPositions.length === 0 ? <tr><td colSpan="10" className="empty">Sin posiciones abiertas.</td></tr> : openPositions.map((p, i) => (
                    <tr key={`${p.opened_at}-${i}`}>
                      <td>{formatDate(p.opened_at)}</td>
                      <td><strong>{p.symbol || "—"}</strong></td>
                      <td>{p.scenario || "-"}</td>
                      <td className={p.side === "buy" ? "tone-buy" : "tone-sell"}>{p.side}</td>
                      <td>{formatNumber(p.entry_price, 4)}</td>
                      <td>{formatNumber(p.stop_loss, 4)}</td>
                      <td>{formatNumber(p.take_profit, 4)}</td>
                      <td className="tone-sell">{formatPercent(p.mae_pct)}</td>
                      <td className="tone-buy">{formatPercent(p.mfe_pct)}</td>
                      <td className={Number(p.unrealized_pnl_usdt) >= 0 ? "tone-buy" : "tone-sell"}>{formatNumber(p.unrealized_pnl_usdt, 4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </section>
    </main>
  );
}