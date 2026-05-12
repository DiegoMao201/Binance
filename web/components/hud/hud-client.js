"use client";
import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Cpu, Wifi, WifiOff, Activity, TrendingUp, TrendingDown, Pause, Play, Square, RotateCcw } from "lucide-react";

import LiveChart from "./live-chart";
import NeuralPulse from "./neural-pulse";
import DecisionMatrix from "./decision-matrix";
import AuditTerminal from "./audit-terminal";

import { deriveCandles, deriveMarkers, deriveDecisionMatrix, deriveAuditEvents } from "../../lib/derive-hud-state";

function fmtUsd(n, dp = 2) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function fmtPct(n, dp = 2) {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  return `${(Number(n) * 100).toFixed(dp)}%`;
}

function StatPill({ label, value, accent = "#22d3ee", trend = 0 }) {
  return (
    <div className="hud-stat">
      <div className="hud-stat-label">{label}</div>
      <div className="hud-stat-value" style={{ color: accent, textShadow: `0 0 12px ${accent}40` }}>
        {value}
        {trend !== 0 && (trend > 0 ? <TrendingUp size={12} /> : <TrendingDown size={12} />)}
      </div>
    </div>
  );
}

export default function HudClient({ initialData }) {
  const [payload, setPayload] = useState(initialData);
  const [connected, setConnected] = useState(false);
  const [focusSymbol, setFocusSymbol] = useState("");
  const [controlBusy, setControlBusy] = useState(false);

  // SSE event-driven (chokidar push). Si falla, retry exponencial.
  useEffect(() => {
    let es;
    let retry = 1000;
    let cancelled = false;

    function open() {
      if (cancelled) return;
      try {
        es = new EventSource("/api/stream");
      } catch {
        setTimeout(open, retry); retry = Math.min(retry * 2, 15000); return;
      }
      es.addEventListener("state", (e) => {
        try { setPayload(JSON.parse(e.data)); setConnected(true); retry = 1000; } catch { /* noop */ }
      });
      es.onmessage = (e) => {
        try { setPayload(JSON.parse(e.data)); setConnected(true); retry = 1000; } catch { /* noop */ }
      };
      es.onerror = () => {
        setConnected(false);
        try { es.close(); } catch { /* noop */ }
        setTimeout(open, retry);
        retry = Math.min(retry * 2, 15000);
      };
    }
    open();
    return () => { cancelled = true; try { es?.close(); } catch { /* noop */ } };
  }, []);

  const state = payload?.state || {};
  const status = payload?.status || {};
  const control = payload?.control || {};
  const portfolio = state?.portfolio || {};
  const ai = state?.ai_signal || {};
  const ts = state?.technical_signal || {};
  const openPositions = payload?.openPositions || state?.open_positions || [];
  const closedTrades = payload?.closedTrades || state?.closed_trades || [];
  const lastScans = state?.last_scans || [];
  const targetSymbols = state?.target_symbols || [];

  // Symbol focus
  useEffect(() => {
    if (focusSymbol) return;
    const pref = state?.active_symbol || openPositions[0]?.symbol || lastScans[0]?.symbol || targetSymbols[0] || "";
    if (pref) setFocusSymbol(pref);
  }, [state?.active_symbol, openPositions, lastScans, targetSymbols, focusSymbol]);

  const focusScan = useMemo(
    () => lastScans.find((s) => s.symbol === focusSymbol) || lastScans[0] || null,
    [lastScans, focusSymbol]
  );
  const focusAi = focusScan?.ai_signal || ai;
  const focusTechnical = focusScan?.technical_signal || ts;
  const focusOpen = openPositions.find((p) => p.symbol === focusSymbol) || openPositions[0] || null;

  // Si la pantalla foco es la del símbolo activo, usamos las velas de bot_state.market.
  const candles = useMemo(() => deriveCandles(state), [state]);
  const markers = useMemo(() => deriveMarkers(closedTrades, openPositions, focusSymbol), [closedTrades, openPositions, focusSymbol]);
  const matrix = useMemo(() => deriveDecisionMatrix({ technical_signal: focusTechnical, settings: state?.settings || {} }), [focusTechnical, state?.settings]);
  const events = useMemo(() => deriveAuditEvents(payload), [payload]);

  // Live position info
  const trailingSL = focusOpen?.stop_loss ?? null;
  const takeProfit = focusOpen?.take_profit ?? null;
  const entryPrice = focusOpen?.entry_price ?? null;

  // Portfolio stats
  const equity = portfolio.equity_usdt ?? portfolio.balance_usdt ?? null;
  const realized = portfolio.realized_pnl_usdt ?? null;
  const winRate = portfolio.win_rate_pct ?? portfolio.win_rate ?? null;
  const wins = portfolio.wins ?? null;
  const losses = portfolio.losses ?? null;
  const totalTrades = portfolio.total_trades ?? closedTrades.length ?? 0;
  const drawdown = portfolio.current_drawdown_pct ?? null;
  const isOnline = useMemo(() => {
    const hb = status?.heartbeat_at;
    if (!hb) return false;
    const ref = payload?.serverTime ? Date.parse(payload.serverTime) : Date.now();
    return ref - Date.parse(hb) < 120000;
  }, [payload?.serverTime, status?.heartbeat_at]);

  const desired = control?.desired_state || "running";

  async function sendControl(desired_state) {
    setControlBusy(true);
    try {
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ desired_state }),
      });
    } catch { /* noop */ }
    finally { setControlBusy(false); }
  }

  return (
    <div className="hud-shell">
      {/* TOP BAR */}
      <header className="hud-topbar">
        <div className="hud-brand">
          <div className="hud-brand-mark"><Cpu size={16} /></div>
          <div>
            <div className="hud-brand-title">OptiFerre <span>HUD</span></div>
            <div className="hud-brand-sub">Real-Time Trading Terminal · {targetSymbols.length || "—"} mercados</div>
          </div>
        </div>

        <div className="hud-topbar-stats">
          <StatPill label="EQUITY" value={`${fmtUsd(equity)} USDT`} accent="#22d3ee" />
          <StatPill label="PnL REAL" value={`${realized != null && realized >= 0 ? "+" : ""}${fmtUsd(realized, 4)}`} accent={realized >= 0 ? "#22c55e" : "#ef4444"} trend={realized > 0 ? 1 : realized < 0 ? -1 : 0} />
          <StatPill label="WIN RATE" value={winRate != null ? `${Number(winRate).toFixed(1)}%` : "—"} accent="#facc15" />
          <StatPill label="TRADES" value={`${totalTrades}`} accent="#a78bfa" />
          <StatPill label="DD" value={drawdown != null ? fmtPct(drawdown) : "—"} accent="#f97316" />
          <StatPill label="W/L" value={wins != null && losses != null ? `${wins}/${losses}` : "—"} accent="#22d3ee" />
        </div>

        <div className="hud-topbar-conn">
          <div className={`hud-conn ${connected ? "ok" : "bad"}`}>
            {connected ? <Wifi size={14} /> : <WifiOff size={14} />}
            <span>{connected ? "STREAM" : "RECONECTANDO"}</span>
          </div>
          <div className={`hud-conn ${isOnline ? "ok" : "bad"}`}>
            <Activity size={14} />
            <span>{isOnline ? "BOT LIVE" : "BOT STALE"}</span>
          </div>
        </div>
      </header>

      {/* CONTROL BAR */}
      <div className="hud-controlbar">
        <div className="hud-controlbar-left">
          <span className="hud-control-label">SYMBOL</span>
          <div className="hud-symbol-tabs">
            {(targetSymbols.length ? targetSymbols : lastScans.map(s => s.symbol)).map((sym) => (
              <button key={sym} onClick={() => setFocusSymbol(sym)} className={`hud-symbol-tab ${sym === focusSymbol ? "active" : ""}`}>
                {sym}
              </button>
            ))}
          </div>
        </div>
        <div className="hud-controlbar-right">
          <button disabled={controlBusy} onClick={() => sendControl("running")} className={`hud-btn hud-btn-go ${desired === "running" ? "active" : ""}`}>
            <Play size={12} /> RUN
          </button>
          <button disabled={controlBusy} onClick={() => sendControl("paused")} className={`hud-btn hud-btn-pause ${desired === "paused" ? "active" : ""}`}>
            <Pause size={12} /> PAUSE
          </button>
          <button disabled={controlBusy} onClick={() => sendControl("stopped")} className={`hud-btn hud-btn-stop ${desired === "stopped" ? "active" : ""}`}>
            <Square size={12} /> STOP
          </button>
          <a href="/" className="hud-btn hud-btn-link"><RotateCcw size={12} /> Clásico</a>
        </div>
      </div>

      {/* MAIN GRID — 3 columnas */}
      <main className="hud-main">
        {/* COL IZQ: Neural Pulse + Position */}
        <section className="hud-col-left">
          <NeuralPulse
            confidence={focusAi?.confidence ?? 0}
            threshold={state?.settings?.ai_confidence_threshold ?? 0.55}
            signal={focusAi?.signal ?? "hold"}
            model={focusAi?.model ?? "lazy_gate"}
            approved={Boolean(focusAi?.approved)}
            fallbackMode={Boolean(focusAi?.risk_flags?.includes?.("technical_fallback_mode"))}
          />

          <motion.div className="hud-position" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            <div className="hud-position-head">POSITION · {focusOpen ? focusOpen.symbol : "—"}</div>
            {focusOpen ? (
              <div className="hud-position-grid">
                <div><span className="muted">Entry</span><b>{fmtUsd(focusOpen.entry_price, 4)}</b></div>
                <div><span className="muted">Mark</span><b>{fmtUsd(focusOpen.mark_price, 4)}</b></div>
                <div><span className="muted">SL</span><b style={{ color: "#ef4444" }}>{fmtUsd(focusOpen.stop_loss, 4)}</b></div>
                <div><span className="muted">TP</span><b style={{ color: "#22c55e" }}>{fmtUsd(focusOpen.take_profit, 4)}</b></div>
                <div><span className="muted">PnL</span><b style={{ color: (focusOpen.unrealized_pnl_usdt ?? 0) >= 0 ? "#22c55e" : "#ef4444" }}>
                  {focusOpen.unrealized_pnl_usdt != null ? `${focusOpen.unrealized_pnl_usdt >= 0 ? "+" : ""}${fmtUsd(focusOpen.unrealized_pnl_usdt, 4)}` : "—"}
                </b></div>
                <div><span className="muted">Scenario</span><b style={{ color: "#22d3ee" }}>{focusOpen.scenario || "—"}</b></div>
              </div>
            ) : (
              <div className="hud-position-empty">// sin posición abierta · escaneando…</div>
            )}
          </motion.div>
        </section>

        {/* COL CENTRO: Chart + Audit */}
        <section className="hud-col-center">
          <LiveChart
            candles={candles}
            markers={markers}
            trailingSL={trailingSL}
            takeProfit={takeProfit}
            entryPrice={entryPrice}
            symbol={focusSymbol}
            height={460}
          />
          <AuditTerminal events={events} />
        </section>

        {/* COL DERECHA: Decision Matrix + Scanner */}
        <section className="hud-col-right">
          <DecisionMatrix matrix={matrix} focus={focusSymbol} />

          <div className="hud-scanner">
            <div className="hud-scanner-head">SCANNER · {lastScans.length} símbolos</div>
            <div className="hud-scanner-list">
              {lastScans.map((s) => {
                const sig = s.ai_signal?.signal || "hold";
                const conf = s.ai_signal?.confidence ?? 0;
                const rsi = s.technical_signal?.rsi ?? null;
                const cand = !!s.candidate;
                return (
                  <button key={s.symbol} className={`hud-scanner-row ${s.symbol === focusSymbol ? "focus" : ""} ${cand ? "candidate" : ""}`} onClick={() => setFocusSymbol(s.symbol)}>
                    <span className="hud-scanner-sym">{s.symbol}</span>
                    <span className="hud-scanner-rsi">{rsi != null ? rsi.toFixed(1) : "—"}</span>
                    <span className={`hud-scanner-sig hud-scanner-sig-${sig}`}>{sig.toUpperCase()}</span>
                    <span className="hud-scanner-conf">{(conf * 100).toFixed(0)}%</span>
                  </button>
                );
              })}
              {!lastScans.length && <div className="hud-scanner-empty">// scanner inicializando…</div>}
            </div>
          </div>
        </section>
      </main>

      <footer className="hud-foot">
        OptiFerre HUD · event-driven SSE · {payload?.serverTime ? new Date(payload.serverTime).toISOString().slice(11, 19) : "--:--:--"} UTC
      </footer>
    </div>
  );
}
