"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";


const Plot = dynamic(() => import("react-plotly.js"), { ssr: false });

const BUY = "#0FD48A";
const SELL = "#E3475A";
const BG = "#071018";
const PANEL = "#0E1722";
const BORDER = "#182433";
const TEXT = "#DCE7F5";
const MUTED = "#7F92A8";


function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(2)}%`;
}


function formatNumber(value, digits = 2) {
  return Number(value || 0).toFixed(digits);
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
      line: { color: "#F4B942", width: 1.7 },
      name: "EMA 9",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.ema_slow),
      line: { color: "#55C1FF", width: 1.7 },
      name: "EMA 20",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.bb_upper),
      line: { color: "#5D728A", width: 1 },
      name: "BB Upper",
    },
    {
      type: "scatter",
      mode: "lines",
      x,
      y: market.map((item) => item.bb_lower),
      line: { color: "#5D728A", width: 1 },
      fill: "tonexty",
      fillcolor: "rgba(93,114,138,0.08)",
      name: "BB Lower",
    },
    {
      type: "scatter",
      mode: "markers",
      x: buySignals.map((item) => item.timestamp),
      y: buySignals.map((item) => item.technical_price),
      marker: { color: BUY, size: 11, symbol: "triangle-up" },
      name: "Buy",
    },
    {
      type: "scatter",
      mode: "markers",
      x: sellSignals.map((item) => item.timestamp),
      y: sellSignals.map((item) => item.technical_price),
      marker: { color: SELL, size: 11, symbol: "triangle-down" },
      name: "Sell",
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
  const decision = state?.decision || {};
  const ai = state?.ai_signal || {};
  const orders = payload?.orderHistory || [];
  const signalHistory = payload?.signalHistory || [];

  const isOnline = useMemo(() => {
    const heartbeat = status?.heartbeat_at;
    if (!heartbeat) {
      return false;
    }

    return Date.now() - new Date(heartbeat).getTime() < 120000;
  }, [status]);

  const chartData = useMemo(() => buildChartData(payload), [payload]);
  const latestSignal = signalHistory.length ? signalHistory[signalHistory.length - 1] : null;

  async function sendControl(desiredState) {
    setControlBusy(true);
    try {
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          desiredState,
          reason: `Cambio a ${desiredState} desde el panel operativo.`,
        }),
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
        <div className="sidebar-block">
          <span>Par</span>
          <strong>{status.symbol || "ETH/USDT"}</strong>
        </div>
        <div className="sidebar-block">
          <span>Heartbeat</span>
          <strong>{status.heartbeat_at || "sin datos"}</strong>
        </div>
        <div className="sidebar-block">
          <span>Bot</span>
          <strong className={isOnline ? "tone-buy" : "tone-sell"}>{isOnline ? "ONLINE" : "OFFLINE"}</strong>
        </div>
        <div className="sidebar-block">
          <span>Estado deseado</span>
          <strong>{control.desired_state || "running"}</strong>
        </div>
        <div className="sidebar-block">
          <span>Modelo IA</span>
          <strong>{ai?.model || "OpenRouter"}</strong>
        </div>
        <div className="sidebar-block">
          <span>Detalle</span>
          <strong>{status.detail || "n/d"}</strong>
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
            <h2>Panel limpio para ejecución prudente</h2>
          </div>
          <div className="timestamp">Servidor: {payload?.serverTime || "n/d"}</div>
        </header>

        <section className="metrics-grid">
          <MetricCard label="Balance USDT" value={formatNumber(risk.balance_usd)} subvalue={`Orden sugerida ${formatNumber(risk.recommended_trade_usd)}`} />
          <MetricCard label="PnL diario" value={formatPercent(risk.daily_pnl_pct)} tone={Number(risk.daily_pnl_pct) >= 0 ? "buy" : "sell"} />
          <MetricCard label="Kill Switch" value={risk.kill_switch_triggered ? "ACTIVO" : "SEGURO"} tone={risk.kill_switch_triggered ? "sell" : "buy"} />
          <MetricCard label="Decisión" value={decision.action || decision.side || "hold"} subvalue={decision.reason || decision.status || "n/d"} />
        </section>

        <section className="telemetry-grid">
          <div className="telemetry-card">
            <span>AI confidence</span>
            <strong>{formatPercent(ai.confidence)}</strong>
          </div>
          <div className="telemetry-card">
            <span>RSI</span>
            <strong>{formatNumber(state?.technical_signal?.rsi, 2)}</strong>
          </div>
          <div className="telemetry-card">
            <span>ATR %</span>
            <strong>{formatPercent(state?.technical_signal?.atr_pct)}</strong>
          </div>
          <div className="telemetry-card">
            <span>Volumen relativo</span>
            <strong>{formatNumber(state?.technical_signal?.volume_ratio, 2)}x</strong>
          </div>
        </section>

        <section className="panel chart-panel">
          <div className="panel-header">
            <h3>Precio y señales</h3>
            <span>Últimas 50 velas</span>
          </div>
          <Plot
            data={chartData}
            layout={{
              autosize: true,
              paper_bgcolor: BG,
              plot_bgcolor: BG,
              font: { color: TEXT, family: "IBM Plex Mono, Menlo, monospace" },
              margin: { l: 20, r: 20, t: 20, b: 30 },
              xaxis: { rangeslider: { visible: false }, gridcolor: "#16202C" },
              yaxis: { gridcolor: "#16202C" },
              legend: { orientation: "h", y: 1.1, x: 0 },
            }}
            config={{ displayModeBar: false, responsive: true }}
            style={{ width: "100%", height: "540px" }}
          />
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header">
              <h3>Filtro de decisión</h3>
              <span>Técnica + IA + guardrails</span>
            </div>
            <pre>{JSON.stringify(decision, null, 2)}</pre>
          </div>
          <div className="panel">
            <div className="panel-header">
              <h3>Opinión IA</h3>
              <span>Confirmación OpenRouter</span>
            </div>
            <pre>{JSON.stringify(ai, null, 2)}</pre>
          </div>
        </section>

        <section className="details-grid">
          <div className="panel">
            <div className="panel-header">
              <h3>Guardrails</h3>
              <span>Semáforos de entrada</span>
            </div>
            <div className="pill-grid">
              <StatusPill label={`Dirección ${decision.same_direction ? "OK" : "NO"}`} active={Boolean(decision.same_direction)} />
              <StatusPill label={`IA ${decision.ai_confident ? "OK" : "NO"}`} active={Boolean(decision.ai_confident)} />
              <StatusPill label={`Técnica ${decision.technical_confident ? "OK" : "NO"}`} active={Boolean(decision.technical_confident)} />
              <StatusPill label={`Volatilidad ${decision.volatility_ready ? "OK" : "NO"}`} active={Boolean(decision.volatility_ready)} />
              <StatusPill label={`Volumen ${decision.volume_ready ? "OK" : "NO"}`} active={Boolean(decision.volume_ready)} />
              <StatusPill label={`Tendencia ${decision.trend_ready ? "OK" : "NO"}`} active={Boolean(decision.trend_ready)} />
              <StatusPill label={`Cooldown ${decision.cooldown_active ? "ACTIVO" : "LIBRE"}`} active={!decision.cooldown_active} />
            </div>
          </div>
          <div className="panel">
            <div className="panel-header">
              <h3>Última señal</h3>
              <span>Evento más reciente</span>
            </div>
            <pre>{JSON.stringify(latestSignal || {}, null, 2)}</pre>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h3>Log de operaciones</h3>
            <span>{orders.length} registros</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Hora</th>
                  <th>Lado</th>
                  <th>Estado</th>
                  <th>Precio</th>
                  <th>Monto</th>
                  <th>Notional</th>
                  <th>SL</th>
                  <th>TP</th>
                </tr>
              </thead>
              <tbody>
                {orders.length === 0 ? (
                  <tr>
                    <td colSpan="8" className="empty">Sin operaciones todavía.</td>
                  </tr>
                ) : (
                  [...orders].reverse().map((order, index) => (
                    <tr key={`${order.timestamp}-${index}`}>
                      <td>{order.timestamp || "n/d"}</td>
                      <td className={order.side === "buy" ? "tone-buy" : "tone-sell"}>{order.side || "-"}</td>
                      <td>{order.status || "-"}</td>
                      <td>{formatNumber(order.price, 4)}</td>
                      <td>{formatNumber(order.amount, 6)}</td>
                      <td>{formatNumber(order.notional_usdt, 2)}</td>
                      <td>{formatNumber(order.stop_loss, 4)}</td>
                      <td>{formatNumber(order.take_profit, 4)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      </section>
    </main>
  );
}