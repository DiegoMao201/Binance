"use client";
import { useState, useEffect, useRef, useCallback } from "react";

// ── Design tokens (same palette as operator-terminal) ─────────────────────
const BG   = "#0a0c10";
const BG2  = "#0f1117";
const CARD = "#13161e";
const BORD = "rgba(255,255,255,0.07)";
const TEXT = "#e2e8f0";
const MUTE = "#64748b";
const G    = "#12d98b";
const R    = "#eb4b61";
const B    = "#57c1ff";
const Y    = "#f5a623";
const P    = "#a78bfa";

// ── Utilities ─────────────────────────────────────────────────────────────
const n = (v, d = 2) => (v == null || isNaN(Number(v)) ? "–" : Number(v).toFixed(d));
const pct = (v, d = 2) => (v == null || isNaN(Number(v)) ? "–" : (Number(v) * 100).toFixed(d) + "%");
const tone = v => Number(v) > 0 ? G : Number(v) < 0 ? R : MUTE;
const fmtTs = ts => {
  if (!ts) return "–";
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
  return isNaN(d) ? "–" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
};
const fmtDt = ts => {
  if (!ts) return "–";
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
  return isNaN(d) ? "–" : d.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
};

// ── Base card ─────────────────────────────────────────────────────────────
function Card({ title, right, children, noPad = false }) {
  return (
    <div style={{
      background: CARD, border: `1px solid ${BORD}`, borderRadius: 12,
      padding: noPad ? 0 : "14px 18px",
      display: "flex", flexDirection: "column", gap: 12,
    }}>
      {(title || right) && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: noPad ? "14px 18px 0" : 0 }}>
          {title && <span style={{ fontSize: 11, fontWeight: 700, color: MUTE, textTransform: "uppercase", letterSpacing: "0.1em" }}>{title}</span>}
          {right && <span style={{ fontSize: 11 }}>{right}</span>}
        </div>
      )}
      {children}
    </div>
  );
}

// ── Mini sparkline (SVG) ───────────────────────────────────────────────────
function Sparkline({ data, color = G, height = 40, width = 120 }) {
  if (!data || data.length < 2) {
    return <div style={{ width, height, display: "flex", alignItems: "center", justifyContent: "center", color: MUTE, fontSize: 10 }}>sin datos</div>;
  }
  const vals = data.map(Number).filter(v => !isNaN(v));
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * width},${height - ((v - min) / range) * height}`).join(" ");
  const lastUp = vals[vals.length - 1] >= vals[0];
  const lineColor = color || (lastUp ? G : R);
  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      <polyline points={pts} fill="none" stroke={lineColor} strokeWidth={1.5} strokeLinejoin="round" />
      <circle cx={(vals.length - 1) / (vals.length - 1) * width} cy={height - ((vals[vals.length - 1] - min) / range) * height} r={2.5} fill={lineColor} />
    </svg>
  );
}

// ── Progress bar ──────────────────────────────────────────────────────────
function Bar({ value, max = 1, color = G, height = 5 }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div style={{ height, background: "rgba(255,255,255,0.05)", borderRadius: 4, overflow: "hidden" }}>
      <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 4, transition: "width 0.4s ease" }} />
    </div>
  );
}

// ── Score ring ─────────────────────────────────────────────────────────────
function ScoreRing({ score, size = 64 }) {
  const norm = Math.min(1, Math.max(0, (score || 0) / 10));
  const r = (size / 2) - 5;
  const circ = 2 * Math.PI * r;
  const offset = circ * (1 - norm);
  const col = norm >= 0.75 ? G : norm >= 0.5 ? Y : R;
  return (
    <svg width={size} height={size}>
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={5} />
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={col} strokeWidth={5}
        strokeDasharray={circ} strokeDashoffset={offset}
        strokeLinecap="round" transform={`rotate(-90 ${size/2} ${size/2})`}
        style={{ transition: "stroke-dashoffset 0.5s ease" }} />
      <text x={size/2} y={size/2 + 1} textAnchor="middle" dominantBaseline="middle"
        fill={col} fontSize={14} fontWeight={700} fontFamily="monospace">
        {Number(score || 0).toFixed(1)}
      </text>
    </svg>
  );
}

// ── Main component ────────────────────────────────────────────────────────
export default function DerivAnalyticsClient({ derivStatus: _initStatus, derivOpenContracts: _initOpen, derivClosedContracts: _initClosed }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [tab, setTab] = useState("overview");
  const timerRef = useRef(null);

  const fetch_ = useCallback(async () => {
    try {
      const res = await fetch("/api/deriv-analytics", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      setData(d);
      setLastUpdate(new Date().toLocaleTimeString());
    } catch (e) {
      console.error("[deriv-analytics] fetch error:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetch_();
    timerRef.current = setInterval(fetch_, 8000);
    return () => clearInterval(timerRef.current);
  }, [fetch_]);

  if (loading || !data) {
    return (
      <div style={{ minHeight: "100vh", background: BG, display: "flex", alignItems: "center", justifyContent: "center", color: MUTE, fontFamily: "IBM Plex Mono, monospace" }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>◌</div>
          <div>Cargando Deriv Analytics…</div>
        </div>
      </div>
    );
  }

  const { status = {}, open_contracts = [], closed_contracts = [], analytics = {} } = data;
  const { by_symbol = {}, score_distribution = {}, exit_reasons = {}, streaks = {}, equity_history = [], multiplier_dist = {} } = analytics;
  const symRows = Object.values(by_symbol).sort((a, b) => b.trades - a.trades);
  const allClosed = [...closed_contracts].reverse();
  const openContracts = Array.isArray(status.open_contracts_live) ? status.open_contracts_live : open_contracts;
  const decisions = Array.isArray(status.last_decisions) ? [...status.last_decisions].reverse() : [];
  const ticks = status.last_ticks || {};
  const counters = status.counters || {};
  const isUp = status.status === "running" || status.connected === true;
  const balance = status.balance != null ? Number(status.balance).toFixed(2) : "–";
  const currency = status.balance_currency || "USD";

  const TABS = ["overview", "trades", "análisis", "mercado", "decisiones"];

  return (
    <div style={{
      minHeight: "100vh", background: BG, color: TEXT,
      fontFamily: "IBM Plex Mono, monospace",
      padding: "0 0 40px",
    }}>
      {/* ── Top Bar ───────────────────────────────────────────────────────── */}
      <div style={{
        background: BG2, borderBottom: `1px solid ${BORD}`,
        padding: "14px 24px",
        display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap",
      }}>
        <a href="/" style={{ color: MUTE, fontSize: 11, textDecoration: "none", letterSpacing: "0.05em" }}>← Dashboard</a>
        <div style={{ width: 1, height: 20, background: BORD }} />
        <span style={{ fontWeight: 800, fontSize: 15, letterSpacing: "0.08em", color: TEXT }}>DERIV ANALYTICS</span>
        <span style={{ fontSize: 10, color: MUTE }}>·</span>
        <span style={{ fontSize: 11, color: MUTE }}>{status.account_id || "–"}</span>
        <span style={{
          fontSize: 9, fontWeight: 700, padding: "2px 7px", borderRadius: 4,
          background: isUp ? `${G}22` : `${MUTE}15`,
          border: `1px solid ${isUp ? G : MUTE}44`,
          color: isUp ? G : MUTE,
        }}>{isUp ? "LIVE" : "OFFLINE"}</span>
        {status.dry_run && (
          <span style={{ fontSize: 9, fontWeight: 700, padding: "2px 7px", borderRadius: 4, background: `${Y}22`, border: `1px solid ${Y}44`, color: Y }}>DRY-RUN</span>
        )}
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 10, color: MUTE }}>upd: {lastUpdate || "–"}</span>
      </div>

      {/* ── Tab nav ───────────────────────────────────────────────────────── */}
      <div style={{ padding: "12px 24px 0", display: "flex", gap: 4, borderBottom: `1px solid ${BORD}`, background: BG2 }}>
        {TABS.map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            background: tab === t ? `${B}18` : "transparent",
            border: "none", borderBottom: tab === t ? `2px solid ${B}` : "2px solid transparent",
            color: tab === t ? B : MUTE, fontFamily: "IBM Plex Mono, monospace",
            fontWeight: 700, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em",
            padding: "8px 14px", cursor: "pointer", transition: "all 0.2s",
          }}>{t}</button>
        ))}
      </div>

      <div style={{ padding: "20px 24px", display: "flex", flexDirection: "column", gap: 16 }}>

        {/* ══════════════════════════════════════════════════════════════════
            TAB: OVERVIEW
           ══════════════════════════════════════════════════════════════════ */}
        {tab === "overview" && (
          <>
            {/* KPI Strip */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 10 }}>
              {[
                { label: "SALDO", val: `${balance} ${currency}`, color: B, icon: "◈" },
                { label: "PnL SESIÓN", val: `${analytics.session_pnl >= 0 ? "+" : ""}${n(analytics.session_pnl, 4)} USD`, color: tone(analytics.session_pnl), icon: "▲" },
                { label: "WIN RATE", val: `${Math.round((analytics.win_rate || 0) * 100)}%`, color: analytics.win_rate >= 0.5 ? G : analytics.win_rate >= 0.4 ? Y : R, icon: "%" },
                { label: "TRADES", val: analytics.total_trades || 0, color: TEXT, icon: "≡" },
                { label: "GANANCIAS", val: analytics.total_wins || 0, color: G, icon: "+" },
                { label: "PÉRDIDAS", val: analytics.total_losses || 0, color: R, icon: "-" },
                { label: "ABIERTOS", val: openContracts.length, color: openContracts.length > 0 ? G : MUTE, icon: "●" },
                { label: "RACHA ACT", val: streaks.current ? `${streaks.current}×` : "–", color: MUTE, icon: "~" },
              ].map(k => (
                <div key={k.label} style={{ background: BG2, borderRadius: 10, padding: "12px 14px", border: `1px solid ${BORD}` }}>
                  <div style={{ fontSize: 9, color: MUTE, marginBottom: 5, textTransform: "uppercase", letterSpacing: "0.07em" }}>{k.icon} {k.label}</div>
                  <div style={{ fontWeight: 800, fontFamily: "monospace", color: k.color, fontSize: 16 }}>{k.val}</div>
                </div>
              ))}
            </div>

            {/* 2-col: equity curve + per-symbol matrix */}
            <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: 14 }}>
              {/* Equity sparkline */}
              <Card title="Curva de Saldo (Deriv Demo)" right={<span style={{ fontSize: 12, color: B, fontFamily: "monospace" }}>{balance} {currency}</span>}>
                {equity_history.length > 1 ? (
                  <div style={{ padding: "0 0 4px" }}>
                    <Sparkline data={equity_history.map(e => e.balance)} width={500} height={80} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: MUTE, marginTop: 4 }}>
                      <span>{equity_history[0]?.ts ? new Date(equity_history[0].ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–"}</span>
                      <span>{equity_history[equity_history.length - 1]?.ts ? new Date(equity_history[equity_history.length - 1].ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–"}</span>
                    </div>
                  </div>
                ) : (
                  <div style={{ height: 80, display: "flex", alignItems: "center", justifyContent: "center", color: MUTE, fontSize: 12 }}>
                    Acumulando datos de saldo (30 s/snapshot)…
                  </div>
                )}
              </Card>

              {/* Counters */}
              <Card title="Telemetría de motor">
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  {[
                    { label: "Ticks recibidos", val: counters.ticks_total || 0 },
                    { label: "Decisiones", val: counters.decisions_total || 0 },
                    { label: "Órdenes enviadas", val: counters.orders_sent || 0 },
                    { label: "Órdenes OK", val: counters.orders_ok || 0 },
                    { label: "Órdenes fail", val: counters.orders_failed || 0 },
                    { label: "Símbolos activos", val: (status.symbols || []).length },
                  ].map(r => (
                    <div key={r.label} style={{ background: "rgba(255,255,255,0.025)", borderRadius: 8, padding: "8px 10px" }}>
                      <div style={{ fontSize: 9, color: MUTE }}>{r.label}</div>
                      <div style={{ fontSize: 16, fontWeight: 700, fontFamily: "monospace", color: TEXT, marginTop: 2 }}>{r.val}</div>
                    </div>
                  ))}
                </div>
                <div style={{ paddingTop: 4, borderTop: `1px solid ${BORD}` }}>
                  <div style={{ fontSize: 9, color: MUTE, marginBottom: 4 }}>Éxito de ejecución</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <div style={{ flex: 1 }}>
                      <Bar
                        value={counters.orders_ok || 0}
                        max={Math.max(1, (counters.orders_ok || 0) + (counters.orders_failed || 0))}
                        color={G} height={6}
                      />
                    </div>
                    <span style={{ fontSize: 11, color: G, fontWeight: 700, fontFamily: "monospace" }}>
                      {counters.orders_sent > 0 ? Math.round(((counters.orders_ok || 0) / counters.orders_sent) * 100) : 0}%
                    </span>
                  </div>
                </div>
              </Card>
            </div>

            {/* Per-symbol matrix */}
            {symRows.length > 0 && (
              <Card title={`Rendimiento por Símbolo · ${symRows.length} mercados`}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                    <thead>
                      <tr style={{ color: MUTE, fontSize: 9, textTransform: "uppercase", letterSpacing: "0.07em" }}>
                        {["Símbolo", "Trades", "Win%", "PnL Total", "PnL Avg", "Mejor", "Peor", "Hold avg", "SUBE", "BAJA", "Salidas"].map(h => (
                          <th key={h} style={{ textAlign: h === "Símbolo" ? "left" : "center", padding: "6px 8px", borderBottom: `1px solid ${BORD}`, whiteSpace: "nowrap" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {symRows.map(r => {
                        const wr = Math.round((r.win_rate || 0) * 100);
                        const wrCol = wr >= 50 ? G : wr >= 40 ? Y : R;
                        const upSide = r.by_side?.MULTUP || {};
                        const downSide = r.by_side?.MULTDOWN || {};
                        const topExit = Object.entries(r.by_exit || {}).sort((a, b) => b[1] - a[1])[0];
                        return (
                          <tr key={r.symbol} style={{ borderTop: `1px solid ${BORD}` }}>
                            <td style={{ padding: "8px 8px", fontWeight: 800, color: TEXT }}>{r.symbol}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace" }}>{r.trades}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: wrCol, fontWeight: 700 }}>{wr}%</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: tone(r.pnl), fontWeight: 700 }}>{r.pnl >= 0 ? "+" : ""}{n(r.pnl, 4)}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: tone(r.avg_pnl) }}>{r.avg_pnl >= 0 ? "+" : ""}{n(r.avg_pnl, 4)}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: G }}>{r.best != null ? (r.best >= 0 ? "+" : "") + n(r.best, 4) : "–"}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: R }}>{r.worst != null ? n(r.worst, 4) : "–"}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: MUTE }}>{r.avg_hold_sec > 60 ? `${Math.round(r.avg_hold_sec / 60)}m` : `${Math.round(r.avg_hold_sec)}s`}</td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: G, fontSize: 11 }}>
                              {upSide.trades || 0}<span style={{ color: MUTE, fontSize: 9 }}> ({upSide.trades ? Math.round((upSide.wins || 0) / upSide.trades * 100) : 0}%)</span>
                            </td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: R, fontSize: 11 }}>
                              {downSide.trades || 0}<span style={{ color: MUTE, fontSize: 9 }}> ({downSide.trades ? Math.round((downSide.wins || 0) / downSide.trades * 100) : 0}%)</span>
                            </td>
                            <td style={{ padding: "8px 8px", textAlign: "center", fontSize: 10, color: MUTE }}>{topExit ? `${topExit[0]} (${topExit[1]})` : "–"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}

            {/* Score distribution + exit reasons + streaks */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
              <Card title="Distribución de Score">
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {Object.entries(score_distribution).map(([range, count]) => {
                    const total = Object.values(score_distribution).reduce((a, b) => a + b, 0) || 1;
                    const pct2 = (count / total) * 100;
                    const rangeNum = parseFloat(range.split("-")[0]);
                    const col = rangeNum >= 8 ? G : rangeNum >= 6 ? Y : rangeNum >= 4 ? B : MUTE;
                    return (
                      <div key={range}>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE, marginBottom: 2 }}>
                          <span style={{ color: col }}>{range}</span>
                          <span style={{ fontFamily: "monospace", color: TEXT }}>{count} ({Math.round(pct2)}%)</span>
                        </div>
                        <Bar value={pct2} max={100} color={col} height={6} />
                      </div>
                    );
                  })}
                </div>
              </Card>

              <Card title="Razones de Salida">
                {Object.keys(exit_reasons).length === 0 ? (
                  <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "14px 0" }}>Sin cierres registrados</div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {Object.entries(exit_reasons).sort((a, b) => b[1].count - a[1].count).map(([reason, stats]) => {
                      const total = Object.values(exit_reasons).reduce((a, b) => a + b.count, 0) || 1;
                      const pct2 = (stats.count / total) * 100;
                      const col = reason === "take_profit" ? G : reason === "stop_loss" ? R : Y;
                      return (
                        <div key={reason}>
                          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, marginBottom: 2 }}>
                            <span style={{ color: col, textTransform: "uppercase" }}>{reason.replace("_", " ")}</span>
                            <span style={{ fontFamily: "monospace", color: tone(stats.pnl) }}>{stats.pnl >= 0 ? "+" : ""}{n(stats.pnl, 3)} ({stats.count})</span>
                          </div>
                          <Bar value={pct2} max={100} color={col} height={5} />
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>

              <Card title="Rachas y Estadísticas">
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {[
                    { label: "Racha ganadora record", val: `${streaks.best_win || 0}×`, color: G },
                    { label: "Racha perdedora record", val: `${streaks.worst_loss || 0}×`, color: R },
                    { label: "Racha actual", val: streaks.current ? `${streaks.current}×` : "–", color: TEXT },
                    { label: "Multiplicador + usado", val: Object.entries(multiplier_dist).sort((a, b) => b[1] - a[1])[0]?.[0] || "–", color: B },
                  ].map(r => (
                    <div key={r.label} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: 11, color: MUTE }}>{r.label}</span>
                      <span style={{ fontSize: 14, fontWeight: 700, fontFamily: "monospace", color: r.color }}>{r.val}</span>
                    </div>
                  ))}
                </div>
              </Card>
            </div>
          </>
        )}

        {/* ══════════════════════════════════════════════════════════════════
            TAB: TRADES
           ══════════════════════════════════════════════════════════════════ */}
        {tab === "trades" && (
          <>
            {/* Open contracts */}
            <Card title={`Contratos Abiertos · ${openContracts.length}`}>
              {openContracts.length === 0 ? (
                <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "20px 0" }}>Sin contratos abiertos</div>
              ) : (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                  {openContracts.map((c, i) => {
                    const isUp2 = c.side === "MULTUP";
                    const sideColor = isUp2 ? G : R;
                    const heldSec = c.opened_at_ts ? Math.round(Date.now() / 1000 - c.opened_at_ts) : 0;
                    return (
                      <div key={c.contract_id || i} style={{
                        flex: "1 1 220px", minWidth: 200,
                        background: `${sideColor}0a`, border: `1px solid ${sideColor}33`,
                        borderRadius: 10, padding: "12px 14px",
                      }}>
                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                          <span style={{ fontWeight: 700, fontSize: 14 }}>{c.symbol}</span>
                          <span style={{ fontSize: 11, fontWeight: 700, color: sideColor }}>{isUp2 ? "▲ SUBE" : "▼ BAJA"}</span>
                        </div>
                        <div style={{ fontSize: 11, display: "flex", flexDirection: "column", gap: 4 }}>
                          <div style={{ display: "flex", justifyContent: "space-between" }}>
                            <span style={{ color: MUTE }}>Contrato</span>
                            <span style={{ fontFamily: "monospace", color: B }}>#{c.contract_id}</span>
                          </div>
                          <div style={{ display: "flex", justifyContent: "space-between" }}>
                            <span style={{ color: MUTE }}>Entrada</span>
                            <span style={{ fontFamily: "monospace" }}>{n(c.entry_price, 5)}</span>
                          </div>
                          <div style={{ display: "flex", justifyContent: "space-between" }}>
                            <span style={{ color: MUTE }}>Stake</span>
                            <span style={{ fontFamily: "monospace", color: B }}>{n(c.stake_usdt, 2)} USD × {c.multiplier}x</span>
                          </div>
                          <div style={{ display: "flex", justifyContent: "space-between" }}>
                            <span style={{ color: MUTE }}>Hold</span>
                            <span style={{ fontFamily: "monospace", color: heldSec > 240 ? Y : MUTE }}>
                              {heldSec > 60 ? `${Math.floor(heldSec / 60)}m ${heldSec % 60}s` : `${heldSec}s`}
                            </span>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>

            {/* Full closed contracts table */}
            <Card title={`Historial de Contratos Cerrados · ${allClosed.length} total`}>
              {allClosed.length === 0 ? (
                <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "20px 0" }}>Sin cierres registrados aún</div>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                    <thead>
                      <tr style={{ color: MUTE, fontSize: 9, textTransform: "uppercase", letterSpacing: "0.07em" }}>
                        {["#", "Símbolo", "Lado", "Stake", "×", "Entrada", "Salida", "PnL", "Razón", "Hold", "Abierto", "Cerrado"].map(h => (
                          <th key={h} style={{ textAlign: "center", padding: "5px 6px", borderBottom: `1px solid ${BORD}`, whiteSpace: "nowrap" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {allClosed.slice(0, 100).map((c, i) => {
                        const pnl = Number(c.realized_pnl_usdt ?? c.pnl ?? 0);
                        const isUp2 = c.side === "MULTUP";
                        const heldSec = c.opened_at_ts && c.closed_at_ts ? Math.round(c.closed_at_ts - c.opened_at_ts) : 0;
                        const exitCol = c.exit_reason === "take_profit" ? G : c.exit_reason === "stop_loss" ? R : Y;
                        return (
                          <tr key={c.contract_id || i} style={{ borderTop: `1px solid ${BORD}`, background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.01)" }}>
                            <td style={{ padding: "5px 6px", textAlign: "center", color: MUTE, fontSize: 9 }}>{allClosed.length - i}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontWeight: 700 }}>{c.symbol}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", color: isUp2 ? G : R, fontWeight: 700 }}>{isUp2 ? "▲" : "▼"}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontFamily: "monospace", color: B }}>{n(c.stake_usdt, 2)}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontFamily: "monospace", color: MUTE }}>{c.multiplier}×</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontFamily: "monospace", color: MUTE }}>{n(c.entry_price, 4)}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontFamily: "monospace", color: MUTE }}>{n(c.exit_price, 4)}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontFamily: "monospace", fontWeight: 700, color: tone(pnl) }}>{pnl >= 0 ? "+" : ""}{n(pnl, 4)}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontSize: 9 }}>
                              <span style={{ color: exitCol, background: `${exitCol}15`, borderRadius: 3, padding: "1px 5px" }}>
                                {c.exit_reason || "–"}
                              </span>
                            </td>
                            <td style={{ padding: "5px 6px", textAlign: "center", fontFamily: "monospace", color: MUTE, fontSize: 10 }}>
                              {heldSec > 60 ? `${Math.floor(heldSec / 60)}m` : `${heldSec}s`}
                            </td>
                            <td style={{ padding: "5px 6px", textAlign: "center", color: MUTE, fontSize: 9 }}>{fmtTs(c.opened_at_ts)}</td>
                            <td style={{ padding: "5px 6px", textAlign: "center", color: MUTE, fontSize: 9 }}>{fmtTs(c.closed_at_ts)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {allClosed.length > 100 && (
                    <div style={{ textAlign: "center", padding: "8px", fontSize: 10, color: MUTE }}>
                      Mostrando los últimos 100 de {allClosed.length} contratos
                    </div>
                  )}
                </div>
              )}
            </Card>
          </>
        )}

        {/* ══════════════════════════════════════════════════════════════════
            TAB: ANÁLISIS (MATRIX MATEMATICA)
           ══════════════════════════════════════════════════════════════════ */}
        {tab === "análisis" && (
          <>
            <Card title="Matriz Matemática · Estadísticas por Símbolo y Lado" right={
              <span style={{ fontSize: 10, color: MUTE }}>{analytics.total_trades || 0} contratos analizados</span>
            }>
              <div style={{ fontSize: 11, color: MUTE, marginBottom: 4 }}>
                Esta matriz muestra los factores de eficiencia por símbolo: esperanza matemática, eficiencia de PnL, eficiencia por minuto.
                <br />Los índices sintéticos son procesos aleatorios — busca sesgo estadístico real (n mínimo &gt; 10 trades).
              </div>
              {symRows.length === 0 ? (
                <div style={{ color: MUTE, textAlign: "center", padding: "20px", fontSize: 12 }}>Sin datos suficientes — opera más contratos para construir la matriz estadística.</div>
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 14 }}>
                  {symRows.map(r => {
                    const wr = r.win_rate || 0;
                    const avgPnl = r.avg_pnl || 0;
                    const expectancy = wr * avgPnl;  // simplified EV
                    const pnlPerMin = r.avg_hold_sec > 0 ? avgPnl / (r.avg_hold_sec / 60) : 0;
                    const wrCol = wr >= 0.55 ? G : wr >= 0.45 ? Y : R;
                    return (
                      <div key={r.symbol} style={{
                        background: "rgba(255,255,255,0.02)", border: `1px solid ${BORD}`,
                        borderRadius: 10, padding: 14,
                        display: "flex", flexDirection: "column", gap: 10,
                      }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <span style={{ fontSize: 16, fontWeight: 800 }}>{r.symbol}</span>
                          <div style={{ textAlign: "right" }}>
                            <div style={{ fontSize: 9, color: MUTE }}>n={r.trades}</div>
                            <div style={{ fontSize: 11, color: wrCol, fontWeight: 700 }}>{Math.round(wr * 100)}% WR</div>
                          </div>
                        </div>

                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                          <div style={{ background: "rgba(0,0,0,0.2)", borderRadius: 6, padding: "6px 8px" }}>
                            <div style={{ fontSize: 9, color: MUTE }}>Esperanza matemática</div>
                            <div style={{ fontSize: 13, fontWeight: 700, fontFamily: "monospace", color: tone(expectancy) }}>
                              {expectancy >= 0 ? "+" : ""}{n(expectancy, 5)}
                            </div>
                          </div>
                          <div style={{ background: "rgba(0,0,0,0.2)", borderRadius: 6, padding: "6px 8px" }}>
                            <div style={{ fontSize: 9, color: MUTE }}>PnL por minuto</div>
                            <div style={{ fontSize: 13, fontWeight: 700, fontFamily: "monospace", color: tone(pnlPerMin) }}>
                              {pnlPerMin >= 0 ? "+" : ""}{n(pnlPerMin, 5)}
                            </div>
                          </div>
                          <div style={{ background: "rgba(0,0,0,0.2)", borderRadius: 6, padding: "6px 8px" }}>
                            <div style={{ fontSize: 9, color: MUTE }}>Mejor trade</div>
                            <div style={{ fontSize: 13, fontWeight: 700, fontFamily: "monospace", color: G }}>{r.best != null ? `+${n(r.best, 4)}` : "–"}</div>
                          </div>
                          <div style={{ background: "rgba(0,0,0,0.2)", borderRadius: 6, padding: "6px 8px" }}>
                            <div style={{ fontSize: 9, color: MUTE }}>Peor trade</div>
                            <div style={{ fontSize: 13, fontWeight: 700, fontFamily: "monospace", color: R }}>{r.worst != null ? n(r.worst, 4) : "–"}</div>
                          </div>
                        </div>

                        <div>
                          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: MUTE, marginBottom: 3 }}>
                            <span>Win rate</span>
                            <span style={{ color: wrCol }}>{Math.round(wr * 100)}%</span>
                          </div>
                          <Bar value={wr} max={1} color={wrCol} height={6} />
                        </div>

                        <div style={{ fontSize: 10, color: MUTE, paddingTop: 4, borderTop: `1px solid ${BORD}` }}>
                          SUBE {r.by_side?.MULTUP?.trades || 0} ({r.by_side?.MULTUP?.trades ? Math.round((r.by_side.MULTUP.wins || 0) / r.by_side.MULTUP.trades * 100) : 0}%)
                          <span style={{ margin: "0 8px", color: BORD }}>|</span>
                          BAJA {r.by_side?.MULTDOWN?.trades || 0} ({r.by_side?.MULTDOWN?.trades ? Math.round((r.by_side.MULTDOWN.wins || 0) / r.by_side.MULTDOWN.trades * 100) : 0}%)
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>

            {/* Score breakdown of last decisions */}
            <Card title="Desglose de Scoring · Últimas Decisiones" right={
              <span style={{ fontSize: 10, color: MUTE }}>linear regression · momentum · ATR adaptivo · estabilidad</span>
            }>
              <div style={{ fontSize: 11, color: MUTE, marginBottom: 4 }}>
                v2 del motor de riesgo: trend con regresión lineal (R²), momentum de aceleración,
                ATR adaptivo por percentil histórico, detección de régimen (trending/ranging/volatile/calm).
              </div>
              {decisions.length === 0 ? (
                <div style={{ color: MUTE, textAlign: "center", padding: "20px", fontSize: 12 }}>Sin decisiones registradas aún</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  {decisions.slice(0, 20).map((d, i) => {
                    const col = d.allowed ? G : MUTE;
                    const breakdown = d.score_breakdown || {};
                    return (
                      <div key={i} style={{
                        padding: "8px 10px", borderRadius: 6,
                        background: `${col}07`, border: `1px solid ${col}1e`,
                        display: "flex", flexDirection: "column", gap: 4,
                      }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
                          <ScoreRing score={d.score || 0} size={36} />
                          <div style={{ flex: 1 }}>
                            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                              <span style={{ fontWeight: 700, color: TEXT }}>{d.symbol}</span>
                              {d.side && <span style={{ fontSize: 9, color: d.side === "MULTUP" ? G : R, fontWeight: 700 }}>{d.side === "MULTUP" ? "▲ SUBE" : "▼ BAJA"}</span>}
                              {d.regime && <span style={{ fontSize: 9, color: MUTE, background: "rgba(255,255,255,0.04)", padding: "1px 5px", borderRadius: 3 }}>{d.regime}</span>}
                              <span style={{ color: d.allowed ? G : MUTE, fontSize: 10, fontStyle: d.allowed ? "normal" : "italic", flex: 1 }}>{d.reason}</span>
                              <span style={{ fontSize: 9, color: MUTE, fontFamily: "monospace" }}>{fmtTs(d.ts)}</span>
                            </div>
                          </div>
                        </div>
                        {Object.keys(breakdown).length > 0 && (
                          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginLeft: 44 }}>
                            {Object.entries(breakdown).filter(([k]) => k !== "regime").map(([k, v]) => {
                              const val = Number(v || 0);
                              const bCol = val > 0 ? G : val < 0 ? R : MUTE;
                              return (
                                <span key={k} style={{ fontSize: 9, color: bCol, background: `${bCol}12`, padding: "1px 5px", borderRadius: 3 }}>
                                  {k}:{val > 0 ? "+" : ""}{val.toFixed(2)}
                                </span>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          </>
        )}

        {/* ══════════════════════════════════════════════════════════════════
            TAB: MERCADO
           ══════════════════════════════════════════════════════════════════ */}
        {tab === "mercado" && (
          <>
            <Card title={`Precios en Vivo · ${Object.keys(ticks).length} símbolos`} right={
              <span style={{ fontSize: 10, color: MUTE, fontFamily: "monospace" }}>
                ticks/total {counters.ticks_total || 0}
              </span>
            }>
              {Object.keys(ticks).length === 0 ? (
                <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "20px" }}>
                  Sin ticks — el stream WebSocket de Deriv no ha enviado datos todavía
                </div>
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12 }}>
                  {Object.entries(ticks).map(([sym, t]) => {
                    const age = t.ts ? Math.max(0, Math.round((Date.now() - new Date(t.ts).getTime()) / 1000)) : null;
                    const alive = age != null && age < 10;
                    const spread = Number(t.spread || 0);
                    return (
                      <div key={sym} style={{
                        background: "rgba(255,255,255,0.025)", border: `1px solid ${BORD}`,
                        borderRadius: 10, padding: 14,
                      }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <span style={{ width: 7, height: 7, borderRadius: "50%", background: alive ? G : MUTE, display: "inline-block", boxShadow: alive ? `0 0 6px ${G}` : "none" }} />
                            <span style={{ fontWeight: 800, fontSize: 14 }}>{sym}</span>
                          </div>
                          <span style={{ fontSize: 10, color: MUTE, fontFamily: "monospace" }}>{age != null ? `${age}s ago` : "–"}</span>
                        </div>
                        <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "monospace", marginBottom: 6 }}>
                          {Number(t.price || 0).toFixed(5)}
                        </div>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE }}>
                          <span>Spread</span>
                          <span style={{ color: spread > 0.0008 ? R : G, fontFamily: "monospace" }}>
                            {spread > 0 ? `${(spread * 100).toFixed(4)}%` : "–"}
                          </span>
                        </div>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE, marginTop: 3 }}>
                          <span>Tick ID</span>
                          <span style={{ fontFamily: "monospace" }}>{t.tick_id || "–"}</span>
                        </div>
                        {/* Symbol-level stats */}
                        {by_symbol[sym] && (
                          <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${BORD}`, display: "flex", justifyContent: "space-between", fontSize: 10 }}>
                            <span style={{ color: MUTE }}>WR histórico</span>
                            <span style={{ fontWeight: 700, color: by_symbol[sym].win_rate >= 0.5 ? G : R, fontFamily: "monospace" }}>
                              {Math.round((by_symbol[sym].win_rate || 0) * 100)}%
                            </span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>

            {/* Per-symbol regime from status */}
            <Card title="Régimen de Mercado · Deriv Risk Engine v2">
              <div style={{ fontSize: 11, color: MUTE, marginBottom: 8 }}>
                El motor clasifica automáticamente el régimen por símbolo usando regresión lineal (R²) y ATR adaptivo.
                <br />
                <b style={{ color: G }}>trending</b> → señal fuerte, convicción alta ·
                <b style={{ color: B, marginLeft: 8 }}>ranging</b> → ruido, señal débil ·
                <b style={{ color: R, marginLeft: 8 }}>volatile</b> → stake reducido ·
                <b style={{ color: MUTE, marginLeft: 8 }}>calm</b> → ATR bajo, spreads normales
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 10 }}>
                {(status.symbols || ["R_100", "R_75", "R_50"]).map(sym => {
                  const lastDec = [...(status.last_decisions || [])].reverse().find(d => d.symbol === sym);
                  const regime = lastDec?.regime || lastDec?.score_breakdown?.regime || "unknown";
                  const regimeCol = regime === "trending" ? G : regime === "volatile" ? R : regime === "calm" ? MUTE : B;
                  const lastScore = lastDec?.score || 0;
                  return (
                    <div key={sym} style={{ background: "rgba(255,255,255,0.02)", border: `1px solid ${BORD}`, borderRadius: 10, padding: 12 }}>
                      <div style={{ fontWeight: 800, fontSize: 14, marginBottom: 6 }}>{sym}</div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                        <span style={{ fontSize: 10, fontWeight: 700, color: regimeCol, background: `${regimeCol}15`, padding: "2px 8px", borderRadius: 4, textTransform: "uppercase" }}>
                          {regime}
                        </span>
                        <ScoreRing score={lastScore} size={36} />
                      </div>
                      {lastDec && (
                        <div style={{ fontSize: 10, color: MUTE }}>
                          Último: {lastDec.side || "–"} · {fmtTs(lastDec.ts)}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>
          </>
        )}

        {/* ══════════════════════════════════════════════════════════════════
            TAB: DECISIONES
           ══════════════════════════════════════════════════════════════════ */}
        {tab === "decisiones" && (
          <Card
            title={`Log de Decisiones · últimas ${decisions.length}`}
            right={
              <span style={{ fontSize: 9, color: MUTE, fontFamily: "monospace" }}>
                ok {counters.orders_ok ?? 0} · fail {counters.orders_failed ?? 0} · decisiones {counters.decisions_total ?? 0}
              </span>
            }
          >
            {decisions.length === 0 ? (
              <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "20px" }}>Sin decisiones registradas</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                {decisions.map((d, i) => {
                  const c = d.allowed ? G : MUTE;
                  const breakdown = d.score_breakdown || {};
                  return (
                    <div key={i} style={{
                      padding: "8px 12px", borderRadius: 6,
                      background: `${c}07`, border: `1px solid ${c}1e`,
                    }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, flexWrap: "wrap" }}>
                        <span style={{ width: 6, height: 6, borderRadius: "50%", background: c, flexShrink: 0 }} />
                        <span style={{ fontWeight: 700, color: TEXT, minWidth: 56 }}>{d.symbol}</span>
                        {d.side && (
                          <span style={{ fontSize: 9, color: d.side === "MULTUP" ? G : R, fontWeight: 700 }}>
                            {d.side === "MULTUP" ? "▲ SUBE" : "▼ BAJA"}
                          </span>
                        )}
                        <span style={{ fontFamily: "monospace", color: B, fontSize: 10 }}>
                          score {Number(d.score || 0).toFixed(2)}
                        </span>
                        {breakdown.regime && (
                          <span style={{ fontSize: 9, color: MUTE, background: "rgba(255,255,255,0.04)", padding: "1px 5px", borderRadius: 3 }}>
                            {breakdown.regime}
                          </span>
                        )}
                        <span style={{ flex: 1, color: d.allowed ? G : MUTE, fontSize: 10, fontStyle: d.allowed ? "normal" : "italic" }}>
                          {d.reason}
                        </span>
                        <span style={{ fontSize: 9, color: MUTE, fontFamily: "monospace" }}>
                          {d.ts ? new Date(d.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–"}
                        </span>
                      </div>
                      {Object.keys(breakdown).length > 0 && (
                        <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginTop: 5, paddingLeft: 14 }}>
                          {[
                            { k: "trend", v: breakdown.trend },
                            { k: "momentum", v: breakdown.momentum },
                            { k: "atr", v: breakdown.atr },
                            { k: "stability", v: breakdown.stability },
                            { k: "streak", v: breakdown.streak_penalty },
                            { k: "cooldown", v: breakdown.cooldown },
                            { k: "headroom", v: breakdown.headroom },
                          ].filter(x => x.v != null).map(({ k, v }) => {
                            const val = Number(v || 0);
                            const bCol = val > 0 ? G : val < 0 ? R : MUTE;
                            return (
                              <span key={k} title={k} style={{ fontSize: 9, color: bCol, background: `${bCol}12`, padding: "1px 5px", borderRadius: 3, fontFamily: "monospace" }}>
                                {k.slice(0, 5)}:{val > 0 ? "+" : ""}{val.toFixed(2)}
                              </span>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}
