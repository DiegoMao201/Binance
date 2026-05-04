"use client";

import dynamic from "next/dynamic";
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

  return new Date(value).toLocaleString("es-ES", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}


function buildChartData(payload) {
  const market = payload?.state?.market || [];
  const signalHistory = payload?.signalHistory || [];
  const x = market.map((item) => item.timestamp);
  const buySignals = signalHistory.filter((item) => item.technical_signal === "buy" || item.ai_signal === "buy");
  const sellSignals = signalHistory.filter((item) => item.technical_signal === "sell" || item.ai_signal === "sell");

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


function buildDecisionSummary(decision, technicalSignal) {
  const action = decision?.action || decision?.side || "hold";
  if (action === "buy") return `El bot disparo entrada por Escenario ${decision?.scenario || technicalSignal?.scenario || "?"}.`;
  if (action === "sell") return "El bot ve una salida defendible y ya consiguio validacion minima de tecnica, IA y riesgo.";
  if (decision?.reason?.includes("Kill Switch")) return "El bot se bloqueo para proteger capital porque la perdida acumulada supero el limite permitido.";

  const blockers = [];
  if (decision?.executable_signal === false) blockers.push("no se cumple Escenario A ni B");
  if (decision?.ai_confident === false) blockers.push(`la confianza de la IA esta por debajo del umbral (${formatPercent(decision?.ai_confidence)})`);
  if (decision?.volatility_ready === false) blockers.push("la volatilidad util no alcanza el rango deseado");
  if (decision?.volume_ready === false) blockers.push("el volumen no respalda la entrada");
  if (decision?.cooldown_active) blockers.push("el bot sigue en cooldown");
  if (!blockers.length) return "El bot esta observando y espera una ventaja mas clara antes de exponer capital.";
  return `El bot no entra porque ${blockers.join(", ")}.`;
}


function buildAiSummary(ai) {
  if (!ai?.signal) return "La IA aún no produjo una lectura utilizable.";
  if (ai.signal === "hold") return `La IA recomienda esperar. Su convicción actual es ${formatPercent(ai.confidence)}.${ai?.cached ? ` Lectura reutilizada desde caché hace ${formatNumber(ai.cached_age_seconds, 0)}s.` : ""}`;
  return `La IA está inclinada a ${ai.signal === "buy" ? "comprar" : "vender"} con convicción ${formatPercent(ai.confidence)}.`;
}


function buildModelHealth(ai, isOnline) {
  if (!isOnline) return { tone: "sell", title: "Salud degradada", detail: "No hay heartbeat reciente del bot; la lectura del modelo no es confiable." };
  const confidence = Number(ai?.confidence || 0);
  if (confidence >= 0.88) return { tone: "buy", title: "Modelo fuerte", detail: "La IA ya entra en el rango exigido por el bot para operar." };
  if (confidence >= 0.7) return { tone: "warn", title: "Modelo prudente", detail: "La IA responde bien, pero todavía no ve una ventaja estadística suficiente." };
  return { tone: "sell", title: "Modelo débil", detail: "La lectura del modelo es demasiado tibia; lo correcto es no tocar mercado." };
}


function buildAlerts({ status, control, risk, decision, ai, isOnline }) {
  const alerts = [];
  if (!isOnline) alerts.push({ tone: "sell", title: "Bot offline", detail: "No se está recibiendo heartbeat reciente del bot." });
  if (control?.desired_state === "paused") alerts.push({ tone: "warn", title: "Bot en pausa", detail: "El proceso sigue vivo, pero no abrirá nuevas operaciones hasta que lo reanudes." });
  if (control?.desired_state === "stopped") alerts.push({ tone: "sell", title: "Bot detenido", detail: "El proceso fue marcado para detenerse; Coolify tendrá que relanzarlo." });
  if (risk?.kill_switch_triggered) alerts.push({ tone: "sell", title: "Kill switch activo", detail: "La protección de capital se disparó y el bot dejó de operar." });
  if ((decision?.action === "buy" || decision?.action === "sell") && Number(ai?.confidence || 0) >= 0.88) alerts.push({ tone: "buy", title: "Entrada operable", detail: "El bot detectó una oportunidad compatible con sus filtros estrictos." });
  if (!alerts.length) alerts.push({ tone: "neutral", title: "Monitoreo normal", detail: status?.detail || "El bot está filtrando oportunidades y protegiendo capital." });
  return alerts;
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

  const isOnline = useMemo(() => {
    const heartbeat = status?.heartbeat_at;
    if (!heartbeat) return false;
    return Date.now() - new Date(heartbeat).getTime() < 120000;
  }, [status]);

  const chartData = useMemo(() => buildChartData(payload), [payload]);
  const latestSignal = signalHistory.length ? signalHistory[signalHistory.length - 1] : null;
  const decisionSummary = buildDecisionSummary(decision, technicalSignal);
  const aiSummary = buildAiSummary(ai);
  const modelHealth = buildModelHealth(ai, isOnline);
  const alerts = buildAlerts({ status, control, risk, decision, ai, isOnline });
  const timeline = buildTimeline(signalHistory);
  const executionAudit = buildExecutionAudit(orders, openPositions, closedTrades);
  const equityCurve = useMemo(() => buildEquityCurve(equityHistory), [equityHistory]);

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
        <div className="sidebar-block"><span>Par</span><strong>{status.symbol || "ETH/USDT"}</strong></div>
        <div className="sidebar-block"><span>Heartbeat</span><strong>{formatDate(status.heartbeat_at)}</strong></div>
        <div className="sidebar-block"><span>Bot</span><strong className={isOnline ? "tone-buy" : "tone-sell"}>{isOnline ? "ONLINE" : "OFFLINE"}</strong></div>
        <div className="sidebar-block"><span>Estado deseado</span><strong>{control.desired_state || "running"}</strong></div>
        <div className="sidebar-block"><span>Modelo IA</span><strong>{ai?.model || "OpenRouter"}</strong></div>
        <div className="sidebar-block"><span>Detalle</span><strong>{status.detail || "n/d"}</strong></div>
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
          <div className="timestamp">Servidor: {formatDate(payload?.serverTime)}</div>
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

        <section className="telemetry-grid">
          <div className="telemetry-card"><span>AI confidence</span><strong>{formatPercent(ai.confidence)}</strong></div>
          <div className="telemetry-card"><span>RSI</span><strong>{formatNumber(technicalSignal.rsi, 2)}</strong></div>
          <div className="telemetry-card"><span>ATR %</span><strong>{formatPercent(technicalSignal.atr_pct)}</strong></div>
          <div className="telemetry-card"><span>Volumen relativo</span><strong>{formatNumber(technicalSignal.volume_ratio, 2)}x</strong></div>
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
                <li>Precio observado: {formatNumber(technicalSignal.close, 4)}</li>
                <li>RSI actual: {formatNumber(technicalSignal.rsi, 2)}</li>
                <li>Volumen relativo: {formatNumber(technicalSignal.volume_ratio, 2)}x</li>
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
            <div className="panel-header"><h3>Guardrails (Logica OR)</h3><span>Semaforos de entrada</span></div>
            <div className="pill-grid">
              <StatusPill label={`Escenario A ${decision.scenario_a ? "OK" : "NO"}`} active={Boolean(decision.scenario_a)} />
              <StatusPill label={`Escenario B ${decision.scenario_b ? "OK" : "NO"}`} active={Boolean(decision.scenario_b)} />
              <StatusPill label={`IA >= 0.65 ${decision.ai_confident ? "OK" : "NO"}`} active={Boolean(decision.ai_confident)} />
              <StatusPill label={`Volatilidad ${decision.volatility_ready ? "OK" : "NO"}`} active={Boolean(decision.volatility_ready)} />
              <StatusPill label={`Volumen ${decision.volume_ready ? "OK" : "NO"}`} active={Boolean(decision.volume_ready)} />
              <StatusPill label={`Cooldown ${decision.cooldown_active ? "ACTIVO" : "LIBRE"}`} active={!decision.cooldown_active} />
              <StatusPill label={`Trigger ${decision.executable_signal ? "OK" : "NO"}`} active={Boolean(decision.executable_signal)} />
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
              <thead><tr><th>Hora</th><th>Lado</th><th>Estado</th><th>Precio</th><th>Monto</th><th>Notional</th><th>SL</th><th>TP</th></tr></thead>
              <tbody>
                {orders.length === 0 ? <tr><td colSpan="8" className="empty">Sin operaciones todavía.</td></tr> : [...orders].reverse().map((order, index) => (
                  <tr key={`${order.timestamp}-${index}`}>
                    <td>{formatDate(order.timestamp)}</td>
                    <td className={order.side === "buy" ? "tone-buy" : "tone-sell"}>{order.side || "-"}</td>
                    <td>{order.status || "-"}</td>
                    <td>{formatNumber(order.price, 4)}</td>
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
              <thead><tr><th>Apertura</th><th>Cierre</th><th>Esc</th><th>Lado</th><th>Entrada</th><th>Salida</th><th>Motivo</th><th>MAE %</th><th>MFE %</th><th>PnL USDT</th><th>PnL %</th></tr></thead>
              <tbody>
                {closedTrades.length === 0 ? <tr><td colSpan="11" className="empty">Sin cierres todavía. El bot solo mostrará ganancia o pérdida cuando una operación alcance TP o SL.</td></tr> : [...closedTrades].reverse().map((trade, index) => (
                  <tr key={`${trade.closed_at}-${index}`}>
                    <td>{formatDate(trade.opened_at)}</td>
                    <td>{formatDate(trade.closed_at)}</td>
                    <td>{trade.scenario || "-"}</td>
                    <td className={trade.side === "buy" ? "tone-buy" : "tone-sell"}>{trade.side || "-"}</td>
                    <td>{formatNumber(trade.entry_price, 4)}</td>
                    <td>{formatNumber(trade.exit_price, 4)}</td>
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
            <div className="panel-header"><h3>Telemetría por escenario</h3><span>Lógica OR (A vs B)</span></div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Escenario</th><th>Trades</th><th>W / L</th><th>Win rate</th><th>PnL USDT</th><th>MAE prom.</th><th>MFE prom.</th></tr></thead>
                <tbody>
                  {["A", "B"].map((label) => {
                    const stats = portfolio?.scenario_stats?.[label] || {};
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
                <thead><tr><th>Apertura</th><th>Esc</th><th>Lado</th><th>Entrada</th><th>SL</th><th>TP</th><th>MAE %</th><th>MFE %</th><th>PnL no real.</th></tr></thead>
                <tbody>
                  {openPositions.length === 0 ? <tr><td colSpan="9" className="empty">Sin posiciones abiertas.</td></tr> : openPositions.map((p, i) => (
                    <tr key={`${p.opened_at}-${i}`}>
                      <td>{formatDate(p.opened_at)}</td>
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