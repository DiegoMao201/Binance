"use client";
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ResponsiveContainer, AreaChart, Area, LineChart, Line, BarChart, Bar,
  ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine,
  Cell, Legend,
} from "recharts";
import { SpikeProbabilityWidget, WaitZScoreWidget, RarityPercentileWidget, AnalyticsGrid } from "./analytics-widgets";

/* ════════════════════════════════════════════════════════════════════════
   DESIGN TOKENS — premium quant terminal
   ════════════════════════════════════════════════════════════════════════ */
const T = {
  bg:       "#06080d",
  bg2:      "#0a0d14",
  panel:    "#0c1018",
  panel2:   "#10151f",
  border:   "rgba(255,255,255,0.06)",
  borderH:  "rgba(255,255,255,0.14)",
  text:     "#e6ebf2",
  textD:    "#aab1bd",
  mute:     "#5b6473",
  green:    "#22d3a3",
  greenD:   "#0e8f6e",
  red:      "#ff5d6c",
  redD:     "#c43747",
  cyan:     "#62d4ff",
  violet:   "#a78bfa",
  amber:    "#f5c43c",
  blue:     "#5b8cff",
  glow:     "0 0 24px rgba(98,212,255,0.10)",
};
const FONT_MONO = "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace";

const REGIME_COLORS = {
  TRENDING: T.green, RANGE: T.cyan, VOLATILE: T.amber, CALM: T.violet,
  SCALP: T.blue, SPIKE: T.red, MEAN_REV: T.cyan, UNKNOWN: T.mute,
};

/* ════════════════════════════════════════════════════════════════════════
   UTILS
   ════════════════════════════════════════════════════════════════════════ */
const n = (v, d = 2) => (v == null || !Number.isFinite(Number(v))) ? "–" : Number(v).toFixed(d);
const nC = (v, d = 0) => (v == null || !Number.isFinite(Number(v))) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const pct = (v, d = 1) => (v == null || !Number.isFinite(Number(v))) ? "–" : `${(Number(v) * 100).toFixed(d)}%`;
const sign = v => (Number(v) > 0 ? "+" : "");
const tone = v => Number(v) > 0 ? T.green : Number(v) < 0 ? T.red : T.textD;
const fmtT = ts => {
  if (!ts) return "–";
  const d = new Date(typeof ts === "number" ? (ts > 1e12 ? ts : ts * 1000) : ts);
  return isNaN(d) ? "–" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
};
const fmtDT = ts => {
  if (!ts) return "–";
  const d = new Date(typeof ts === "number" ? (ts > 1e12 ? ts : ts * 1000) : ts);
  return isNaN(d) ? "–" : d.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
};
const dur = s => {
  if (!s || !Number.isFinite(Number(s))) return "–";
  s = Number(s);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
};

const btnStyle = (color = T.cyan) => ({
  background: `${color}14`, color, border: `1px solid ${color}44`,
  borderRadius: 5, padding: "5px 10px", cursor: "pointer",
  fontFamily: FONT_MONO, fontSize: 10, fontWeight: 700,
  letterSpacing: "0.1em", textTransform: "uppercase",
  transition: "all 120ms ease",
});

const inputStyle = {
  background: T.bg, color: T.text, border: `1px solid ${T.border}`,
  borderRadius: 5, padding: "4px 8px", fontFamily: FONT_MONO,
  fontSize: 10, outline: "none", minWidth: 140,
};

/* ════════════════════════════════════════════════════════════════════════
   PRIMITIVES
   ════════════════════════════════════════════════════════════════════════ */
function Panel({ title, right, children, pad = true, glow = false }) {
  return (
    <div style={{
      background: T.panel, border: `1px solid ${T.border}`, borderRadius: 10,
      boxShadow: glow ? T.glow : "none",
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {(title || right) && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "10px 14px",
          borderBottom: `1px solid ${T.border}`,
          background: "linear-gradient(180deg, rgba(255,255,255,0.015), rgba(0,0,0,0))",
        }}>
          {title && <span style={{
            fontSize: 10, fontWeight: 700, color: T.textD,
            textTransform: "uppercase", letterSpacing: "0.16em",
            fontFamily: FONT_MONO,
          }}>{title}</span>}
          {right && <span style={{ fontSize: 10, color: T.mute, fontFamily: FONT_MONO }}>{right}</span>}
        </div>
      )}
      <div style={{ padding: pad ? 14 : 0, display: "flex", flexDirection: "column", gap: 10 }}>
        {children}
      </div>
    </div>
  );
}

function KPI({ label, value, sub, color = T.text, accent = null, big = false, trend = null }) {
  return (
    <div style={{
      background: T.panel2,
      border: `1px solid ${accent ? accent + "33" : T.border}`,
      borderRadius: 8,
      padding: big ? "14px 16px" : "10px 12px",
      display: "flex", flexDirection: "column", gap: 4,
      position: "relative", overflow: "hidden",
    }}>
      {accent && <div style={{
        position: "absolute", top: 0, left: 0, right: 0, height: 2,
        background: `linear-gradient(90deg, ${accent}, transparent)`,
      }} />}
      <span style={{
        fontSize: 9, color: T.mute, fontFamily: FONT_MONO,
        textTransform: "uppercase", letterSpacing: "0.14em", fontWeight: 700,
      }}>{label}</span>
      <span style={{
        fontSize: big ? 22 : 16, color, fontFamily: FONT_MONO,
        fontWeight: 700, lineHeight: 1.05,
      }}>{value}</span>
      {sub != null && <span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>{sub}</span>}
      {trend != null && (
        <span style={{ fontSize: 9, color: tone(trend), fontFamily: FONT_MONO, fontWeight: 700 }}>
          {trend > 0 ? "▲" : trend < 0 ? "▼" : "■"} {pct(Math.abs(trend))}
        </span>
      )}
    </div>
  );
}

function Pill({ children, color = T.cyan, size = "md" }) {
  const pad = size === "sm" ? "1px 6px" : "3px 8px";
  const fs = size === "sm" ? 9 : 10;
  return (
    <span style={{
      background: `${color}1a`, color, border: `1px solid ${color}44`,
      borderRadius: 3, padding: pad, fontFamily: FONT_MONO, fontSize: fs,
      fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase",
      whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

function MiniBar({ value, max = 1, color = T.cyan, height = 4 }) {
  const w = Math.max(0, Math.min(1, value / max));
  return (
    <div style={{ width: "100%", height, background: T.bg, borderRadius: height / 2, overflow: "hidden" }}>
      <div style={{ width: `${w * 100}%`, height: "100%", background: color, transition: "width 200ms ease" }} />
    </div>
  );
}

function TabBar({ tabs, active, onChange }) {
  return (
    <div style={{
      display: "flex", gap: 2, padding: 4,
      background: T.panel, border: `1px solid ${T.border}`,
      borderRadius: 8, overflowX: "auto",
    }}>
      {tabs.map(t => {
        const a = t.id === active;
        return (
          <button key={t.id} onClick={() => onChange(t.id)} style={{
            background: a ? T.panel2 : "transparent",
            color: a ? T.cyan : T.textD,
            border: `1px solid ${a ? T.cyan + "44" : "transparent"}`,
            borderRadius: 6, padding: "8px 14px", cursor: "pointer",
            fontFamily: FONT_MONO, fontSize: 10, fontWeight: 700,
            letterSpacing: "0.12em", textTransform: "uppercase",
            display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap",
            transition: "all 120ms ease",
            boxShadow: a ? `0 0 14px ${T.cyan}22 inset` : "none",
          }}>
            {t.icon && <span>{t.icon}</span>}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TOOLTIP HINT — quant term explanations (modo simple)
   ════════════════════════════════════════════════════════════════════════ */
function TooltipHint({ tip }) {
  const [show, setShow] = useState(false);
  return (
    <span style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
      <span
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        style={{
          cursor: "help", color: T.mute, fontSize: 8, marginLeft: 3, fontWeight: 700,
          border: `1px solid ${T.mute}55`, borderRadius: "50%",
          width: 12, height: 12, display: "inline-flex", alignItems: "center",
          justifyContent: "center", lineHeight: 1, flexShrink: 0,
        }}>?</span>
      {show && (
        <span style={{
          position: "absolute", bottom: "130%", left: "50%",
          transform: "translateX(-50%)",
          background: T.panel2, border: `1px solid ${T.borderH}`,
          color: T.textD, fontSize: 10, padding: "7px 10px",
          borderRadius: 7, width: 220, zIndex: 300, pointerEvents: "none",
          boxShadow: "0 6px 24px rgba(0,0,0,0.6)",
          whiteSpace: "normal", lineHeight: 1.55, fontFamily: FONT_MONO,
        }}>{tip}</span>
      )}
    </span>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   SIDE BADGE — dirección de la posición
   ════════════════════════════════════════════════════════════════════════ */
function SideBadge({ side }) {
  const isUp   = String(side || "").toUpperCase().includes("UP");
  const isDown = String(side || "").toUpperCase().includes("DOWN");
  return (
    <span style={{
      fontSize: 9, fontWeight: 700, padding: "2px 7px", borderRadius: 3,
      background: isUp ? `${T.green}22` : isDown ? `${T.red}22` : `${T.mute}22`,
      color: isUp ? T.green : isDown ? T.red : T.mute,
      letterSpacing: "0.1em",
      border: `1px solid ${isUp ? T.green + "44" : isDown ? T.red + "44" : T.mute + "22"}`,
    }}>
      {isUp ? "▲ SUBE" : isDown ? "▼ BAJA" : side || "–"}
    </span>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   CHART HELPERS
   ════════════════════════════════════════════════════════════════════════ */
const CHART_GRID = "rgba(255,255,255,0.04)";
const CHART_AXIS = { fill: T.mute, fontSize: 9, fontFamily: FONT_MONO };

function chartTooltip(formatter) {
  return ({ active, payload, label }) => {
    if (!active || !payload?.length) return null;
    const rows = formatter ? formatter(payload, label) : payload.map(p => ({
      name: p.name, value: p.value, color: p.color || p.stroke || p.fill || T.text,
    }));
    return (
      <div style={{
        background: T.panel2, border: `1px solid ${T.borderH}`, borderRadius: 6,
        padding: "8px 10px", fontFamily: FONT_MONO, fontSize: 10,
        boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
      }}>
        {label != null && <div style={{ color: T.mute, marginBottom: 4 }}>{typeof label === "number" && label > 1e10 ? fmtDT(label) : label}</div>}
        {rows.map((r, i) => (
          <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 12, color: r.color || T.text }}>
            <span>{r.name}</span>
            <span style={{ fontWeight: 700 }}>{typeof r.value === "number" ? n(r.value, 2) : String(r.value)}</span>
          </div>
        ))}
      </div>
    );
  };
}

/* ════════════════════════════════════════════════════════════════════════
   ROOT
   ════════════════════════════════════════════════════════════════════════ */
const TABS = [
  { id: "operaciones", label: "Operaciones", icon: "◎" },
  { id: "overview",    label: "Overview",    icon: "◉" },
  { id: "analytics",   label: "Analytics",   icon: "▤" },
  { id: "telemetry",   label: "Telemetry",   icon: "◈" },
  { id: "decisions",   label: "Decisions",   icon: "▸" },
  { id: "symbols",     label: "Symbols",     icon: "✦" },
  { id: "spikes",      label: "Spikes",      icon: "⚡" },
  { id: "logs",        label: "Logs",        icon: "≡" },
  { id: "export",      label: "Export",      icon: "↓" },
];

export default function DerivAnalyticsClient({ initialState }) {
  const [tab, setTab] = useState("overview");
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(true);
  const [lastUpdate, setLastUpdate] = useState(null);

  const fetchOnce = useCallback(async () => {
    try {
      const r = await fetch("/api/deriv-analytics", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setData(j);
      setErr(null);
      setLastUpdate(Date.now());
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchOnce(); }, [fetchOnce]);
  useEffect(() => {
    if (!live) return;
    const id = setInterval(fetchOnce, 5000);
    return () => clearInterval(id);
  }, [live, fetchOnce]);

  if (loading && !data) return <LoadingShell />;
  if (err && !data) return <ErrorShell err={err} onRetry={fetchOnce} />;

  const kpiTop = {
    balance: data?.status?.balance,
    floating: data?.status?.floating_pnl,
    session: data?.global?.session_pnl,
    today: data?.global?.today_pnl,
    winrate: data?.global?.winrate,
    pf: data?.global?.profit_factor,
    trades: data?.global?.total_trades,
  };

  return (
<div style={{
  minHeight: "100vh", background: `radial-gradient(ellipse at top, ${T.bg2} 0%, ${T.bg} 60%)`,
  color: T.text, fontFamily: FONT_MONO, padding: "16px 18px",
  display: "flex", flexDirection: "column", gap: 14,
}}>
      <TopBar status={data?.status} live={live} setLive={setLive} lastUpdate={lastUpdate} loading={loading} error={err} onRefresh={fetchOnce} kpi={kpiTop} />
      <TabBar tabs={TABS} active={tab} onChange={setTab} />
      <AnimatePresence mode="wait">
        <motion.div key={tab}
          initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18 }}>
          {tab === "operaciones" && <OperacionesTab data={data} />}
          {tab === "overview"  && <OverviewTab data={data} />}
          {tab === "analytics" && <AnalyticsTab data={data} />}
          {tab === "telemetry" && <TelemetryTab data={data} />}
          {tab === "decisions" && <DecisionsTab data={data} />}
          {tab === "symbols"   && <SymbolsTab data={data} />}
          {tab === "spikes"    && <SpikesTab />}
          {tab === "logs"      && <LogsTab data={data} />}
          {tab === "export"    && <ExportTab data={data} />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function TopBar({ status, live, setLive, lastUpdate, loading, error, onRefresh, kpi }) {
  const ws = String(status?.ws_state || "—").toLowerCase();
  const wsColor = ws === "open" || ws === "connected" ? T.green : ws === "closed" || ws === "error" ? T.red : T.amber;
  return (
    <div style={{
      display: "flex", justifyContent: "space-between", alignItems: "center",
      padding: "12px 16px", background: T.panel,
      border: `1px solid ${T.border}`, borderRadius: 10,
      boxShadow: T.glow,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <motion.div
            animate={{ scale: [1, 1.3, 1], opacity: [0.6, 1, 0.6] }}
            transition={{ duration: 2, repeat: Infinity }}
            style={{ width: 8, height: 8, background: wsColor, borderRadius: "50%", boxShadow: `0 0 12px ${wsColor}` }}
          />
          <div style={{ display: "flex", flexDirection: "column" }}>
            <span style={{ fontSize: 14, fontWeight: 700, letterSpacing: "0.16em", color: T.text }}>DERIV · QUANT TERMINAL</span>
            <span style={{ fontSize: 9, color: T.mute, letterSpacing: "0.1em" }}>ws={ws.toUpperCase()} · last={fmtT(lastUpdate)} {loading ? "· syncing…" : ""}{error ? ` · ${error}` : ""}</span>
          </div>
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <Stat lbl="BAL"      val={`$${n(kpi.balance, 2)}`} />
        <Stat lbl="FLOAT"    val={`${sign(kpi.floating)}$${n(kpi.floating, 2)}`} color={tone(kpi.floating)} />
        <Stat lbl="SESSION"  val={`${sign(kpi.session)}$${n(kpi.session, 2)}`} color={tone(kpi.session)} />
        <Stat lbl="TODAY"    val={`${sign(kpi.today)}$${n(kpi.today, 2)}`} color={tone(kpi.today)} />
        <Stat lbl="WIN%"     val={pct(kpi.winrate)} color={kpi.winrate >= 0.5 ? T.green : T.red} />
        <Stat lbl="PF"       val={kpi.pf != null ? n(kpi.pf, 2) : "–"} color={T.violet} />
        <Stat lbl="TRADES"   val={kpi.trades || 0} />
        <a href="/deriv/operar" style={{ ...btnStyle(T.violet), textDecoration: "none", padding: "5px 12px", fontWeight: 800 }} title="Consola de operación manual: todas las señales en una pantalla">🎯 OPERAR MANUAL</a>
        <button onClick={() => setLive(!live)} style={btnStyle(live ? T.green : T.amber)}>{live ? "● LIVE" : "❚❚ PAUSED"}</button>
        <button onClick={onRefresh} style={btnStyle(T.cyan)}>↻ SYNC</button>
        {/* Phase 32: session reset — resets visual KPIs only, bot keeps running */}
        <button
          onClick={async () => {
            try {
              const r = await fetch("/api/session", { method: "POST" });
              if (r.ok) onRefresh();
            } catch { /* noop */ }
          }}
          style={{ ...btnStyle(T.amber), minWidth: 70 }}
          title="Resetear estadísticas de sesión (visual, bot no se interrumpe)"
        >
          ↺ RESET·S
        </button>
      </div>
    </div>
  );
}

function Stat({ lbl, val, color = T.text }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 1, padding: "0 8px", borderLeft: `1px solid ${T.border}` }}>
      <span style={{ fontSize: 8, color: T.mute, letterSpacing: "0.16em", fontWeight: 700 }}>{lbl}</span>
      <span style={{ fontSize: 12, color, fontWeight: 700 }}>{val}</span>
    </div>
  );
}

function LoadingShell() {
  return (
    <div style={{ minHeight: "100vh", background: T.bg, color: T.text, fontFamily: FONT_MONO, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
        <motion.div animate={{ rotate: 360 }} transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
          style={{ width: 40, height: 40, border: `2px solid ${T.cyan}`, borderTopColor: "transparent", borderRadius: "50%" }} />
        <span style={{ fontSize: 10, color: T.mute, letterSpacing: "0.2em" }}>LOADING TELEMETRY…</span>
      </div>
    </div>
  );
}

function ErrorShell({ err, onRetry }) {
  return (
    <div style={{ minHeight: "100vh", background: T.bg, color: T.text, fontFamily: FONT_MONO, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: T.panel, border: `1px solid ${T.red}44`, borderRadius: 10, padding: 24, display: "flex", flexDirection: "column", gap: 10 }}>
        <span style={{ color: T.red, fontSize: 12, letterSpacing: "0.16em" }}>ERROR · UNABLE TO LOAD</span>
        <span style={{ color: T.textD, fontSize: 11 }}>{err}</span>
        <button onClick={onRetry} style={btnStyle(T.cyan)}>↻ RETRY</button>
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 0 — OPERACIONES
   ════════════════════════════════════════════════════════════════════════ */
function DpmTierPanel({ c }) {
  // DPM Tier Visualizer — 2026-05-30 tier-staircase v1.
  // Prefiere tier_state nuevo (staircase + tier % dinámico + spike quota)
  // y mantiene fallback al ratchet legacy si tier_state aún no se publica.
  const stake   = Number(c.stake_usdt || 0);
  const peak    = Number(c.peak_profit ?? 0);
  const trailSl = Number(c.trail_sl ?? c.trail_sl_locked ?? -1);
  const beLocked = Boolean(c.broker_be_locked);
  const durSec  = Number(c.duration_sec ?? 0);
  const ts = c.tier_state || {};
  const hasTierState = ts && typeof ts === "object" && Object.keys(ts).length > 0;

  const tierPct      = Number(ts.tier_pct ?? 0);
  const dollarFloor  = Number(ts.dollar_floor ?? 0);
  const tierFloorVal = Number(ts.tier_floor ?? 0);
  const slFloorVal   = ts.sl_floor != null ? Number(ts.sl_floor) : (trailSl >= 0 ? trailSl : null);
  const spikes1h     = Number(ts.spikes_1h ?? 0);
  const spikesAvg    = Number(ts.spikes_avg_h ?? 0);
  const quotaTarget  = Number(ts.quota_target ?? 0);
  const quotaRemaining = Number(ts.quota_remaining ?? Math.max(0, quotaTarget - spikes1h));
  const quotaDone    = Boolean(ts.quota_done);
  const waitForQuota = Boolean(ts.wait_for_quota);
  const tierActive   = Boolean(ts.tier_active);
  const tierGatedBy  = String(ts.tier_gated_by ?? "");
  const postSpikeConf = Number(ts.post_spike_confirmations ?? 0);
  const lookbackHrs  = Number(ts.lookback_hours ?? 12);
  const profitLock   = Number(ts.profit_lock_usdt ?? 2);
  const profitLockedUntil = Number(c.profit_lock_unlock_at ?? 0);
  const isPhase2 = slFloorVal != null && slFloorVal >= 0 && peak > 0;
  const phasePct = stake > 0
    ? Math.min(1, peak / (stake * 0.65))
    : 0;
  const slDisplay   = slFloorVal != null ? `$${n(slFloorVal, 3)}` : "–";
  const peakDisplay = peak > 0 ? `+$${n(peak, 3)}` : "–";
  const tierLabel   = hasTierState && tierPct > 0
    ? `${Math.round(tierPct * 1000) / 10}%`
    : null;

  const phase1Color = "#60a5fa";
  const phase2Color = T.green;
  const phaseColor  = isPhase2 ? phase2Color : phase1Color;
  const tierBadgeColor = tierPct <= 0.05 ? T.green
    : tierPct <= 0.10 ? T.cyan
    : T.amber;

  // Mini staircase: ticks por cada dólar alcanzado (max 6 escalones).
  const stairSteps = [];
  for (let i = 1; i <= 6; i += 1) {
    stairSteps.push({ usd: i, reached: peak >= i });
  }

  return (
    <div style={{
      borderTop: `1px solid ${T.border}`,
      paddingTop: 8,
      display: "flex",
      flexDirection: "column",
      gap: 6,
    }}>
      {/* Phase label + BE lock + tier badge */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{
            fontSize: 9, fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase",
            color: phaseColor, background: `${phaseColor}18`,
            border: `1px solid ${phaseColor}44`, borderRadius: 4, padding: "2px 6px",
          }}>
            {isPhase2 ? "▲ TIER ARMADO" : "● TIER OFF"}
          </span>
          {tierLabel && (
            <span title="Giveback máximo desde el peak" style={{
              fontSize: 9, fontWeight: 700, letterSpacing: "0.06em",
              color: tierBadgeColor, background: `${tierBadgeColor}18`,
              border: `1px solid ${tierBadgeColor}44`, borderRadius: 4, padding: "2px 6px",
              fontFamily: FONT_MONO,
            }}>
              TIER {tierLabel}
            </span>
          )}
          {quotaDone && (
            <span title={`Spikes 1h (${spikes1h}) ≥ cuota (${quotaTarget}) — tier estricto, recogiendo en próxima caída`} style={{
              fontSize: 9, fontWeight: 700, letterSpacing: "0.06em",
              color: T.amber, background: `${T.amber}18`,
              border: `1px solid ${T.amber}44`, borderRadius: 4, padding: "2px 6px",
            }}>
              QUOTA ✓
            </span>
          )}
          {waitForQuota && (
            <span title={`Esperando ${quotaRemaining} spikes más para activar tier %. Solo escalón $ duro protege.`} style={{
              fontSize: 9, fontWeight: 700, letterSpacing: "0.06em",
              color: T.cyan, background: `${T.cyan}14`,
              border: `1px solid ${T.cyan}44`, borderRadius: 4, padding: "2px 6px",
              fontFamily: FONT_MONO,
            }}>
              ⏳ ESPERA {quotaRemaining}
            </span>
          )}
          {beLocked && (
            <span style={{
              fontSize: 9, fontWeight: 700, letterSpacing: "0.06em",
              color: T.green, background: `${T.green}14`,
              border: `1px solid ${T.green}44`, borderRadius: 4, padding: "2px 6px",
            }}>
              🔒 BE
            </span>
          )}
        </div>
        <span style={{ fontSize: 9, color: T.mute }}>
          {durSec > 0 ? dur(durSec) : "–"}
        </span>
      </div>

      {/* Staircase visual + ratchet bar */}
      {hasTierState && (
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          {stairSteps.map((s) => (
            <div key={s.usd} title={`Piso $${s.usd}`} style={{
              flex: 1,
              height: 10,
              borderRadius: 2,
              background: s.reached ? T.green : `${T.border}`,
              border: `1px solid ${s.reached ? T.green : T.border}`,
              fontSize: 7,
              fontWeight: 700,
              color: s.reached ? "#062" : T.mute,
              fontFamily: FONT_MONO,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              letterSpacing: "0.02em",
            }}>
              ${s.usd}
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <span style={{ fontSize: 9, color: T.mute }}>Progreso ratchet</span>
          <span style={{ fontSize: 9, color: phaseColor, fontFamily: FONT_MONO, fontWeight: 700 }}>
            {Math.round(phasePct * 100)}%
          </span>
        </div>
        <div style={{ height: 4, borderRadius: 2, background: `${T.border}`, overflow: "hidden" }}>
          <div style={{
            height: "100%",
            width: `${Math.round(phasePct * 100)}%`,
            borderRadius: 2,
            background: isPhase2
              ? `linear-gradient(90deg, ${T.green}, #34d399)`
              : `linear-gradient(90deg, ${phase1Color}, #93c5fd)`,
            transition: "width 1s ease",
          }} />
        </div>
      </div>

      {/* SL floor + Peak + Stake */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6 }}>
        <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 5, padding: "4px 6px" }}>
          <div style={{ fontSize: 8, color: T.mute, textTransform: "uppercase", letterSpacing: "0.06em" }}>SL Floor</div>
          <div style={{
            fontSize: 11, fontWeight: 700, fontFamily: FONT_MONO,
            color: isPhase2 ? T.green : T.mute,
          }}>
            {slDisplay}
          </div>
        </div>
        <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 5, padding: "4px 6px" }}>
          <div style={{ fontSize: 8, color: T.mute, textTransform: "uppercase", letterSpacing: "0.06em" }}>Peak P&L</div>
          <div style={{
            fontSize: 11, fontWeight: 700, fontFamily: FONT_MONO,
            color: peak > 0 ? T.green : T.mute,
          }}>
            {peakDisplay}
          </div>
        </div>
        <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 5, padding: "4px 6px" }}>
          <div style={{ fontSize: 8, color: T.mute, textTransform: "uppercase", letterSpacing: "0.06em" }}>Stake</div>
          <div style={{ fontSize: 11, fontWeight: 700, fontFamily: FONT_MONO, color: T.amber }}>
            ${n(stake, 2)}
          </div>
        </div>
      </div>

      {/* Spike quota + profit lock telemetry */}
      {hasTierState && (
        <>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6 }}>
          <div
            title={`Spikes vistos en la hora UTC actual (${spikes1h}) vs cuota objetivo ${quotaTarget} (avg rolling ${lookbackHrs}h = ${n(spikesAvg, 2)}). Si faltan, NO cierro por tier %, solo por piso $.`}
            style={{ background: "rgba(255,255,255,0.03)", borderRadius: 5, padding: "4px 6px" }}>
            <div style={{ fontSize: 8, color: T.mute, textTransform: "uppercase", letterSpacing: "0.06em" }}>Cuota hora</div>
            <div style={{ fontSize: 11, fontWeight: 700, fontFamily: FONT_MONO, color: quotaDone ? T.green : T.cyan }}>
              {spikes1h}<span style={{ color: T.mute, fontWeight: 500 }}>{` / ${quotaTarget}`}</span>
              {!quotaDone && quotaRemaining > 0 && (
                <span style={{ fontSize: 9, color: T.amber, fontWeight: 600 }}>{` (faltan ${quotaRemaining})`}</span>
              )}
            </div>
          </div>
          <div
            title={`Piso duro en dólares enteros. Se activa cada vez que peak cruza $1, $2, $3... — el bot NUNCA cerrará por debajo de este nivel.`}
            style={{ background: "rgba(255,255,255,0.03)", borderRadius: 5, padding: "4px 6px" }}>
            <div style={{ fontSize: 8, color: T.mute, textTransform: "uppercase", letterSpacing: "0.06em" }}>Piso $</div>
            <div style={{ fontSize: 11, fontWeight: 700, fontFamily: FONT_MONO, color: dollarFloor > 0 ? T.green : T.mute }}>
              {dollarFloor > 0 ? `$${n(dollarFloor, 0)}` : "–"}
            </div>
          </div>
          <div
            title={`Profit-lock: si este contrato cierra con PnL ≥ $${n(profitLock, 0)}, el símbolo queda inactivo hasta el cambio de hora UTC. Evita re-entradas perdedoras tras cumplir cuota.`}
            style={{ background: "rgba(255,255,255,0.03)", borderRadius: 5, padding: "4px 6px" }}>
            <div style={{ fontSize: 8, color: T.mute, textTransform: "uppercase", letterSpacing: "0.06em" }}>Lock ≥ ${n(profitLock, 0)}</div>
            <div style={{ fontSize: 11, fontWeight: 700, fontFamily: FONT_MONO, color: profitLockedUntil > 0 ? T.amber : T.mute }}>
              {profitLockedUntil > 0
                ? `${Math.max(0, Math.round((profitLockedUntil - Date.now()/1000) / 60))}m`
                : "–"}
            </div>
          </div>
        </div>
        {/* Plain-language status line */}
        <div style={{
          fontSize: 9, color: T.mute, lineHeight: 1.4,
          background: "rgba(255,255,255,0.02)", padding: "4px 6px", borderRadius: 4,
          border: `1px dashed ${T.border}`,
        }}>
          {peak < 1.0 && !tierActive && tierGatedBy === "post_spike_confirmation_in_flight" && (
            <span>🔄 Spike llegó, pero hay <b style={{ color: T.cyan }}>{postSpikeConf} confirmación{postSpikeConf===1?"":"es"} post-spike</b> en vuelo — esperando otro pico antes de armar tier.</span>
          )}
          {peak < 1.0 && !tierActive && tierGatedBy !== "post_spike_confirmation_in_flight" && (
            <span>📊 Esperando que llegue el spike (peak aún bajo el umbral).</span>
          )}
          {peak < 1.0 && tierActive && slFloorVal != null && (
            <span>
              🛡️ <b style={{ color: T.green }}>Tier {Math.round(tierPct*1000)/10}% activo</b> (sin confirmaciones post-spike) — protegiendo peak
              {" "}<b style={{ color: T.green }}>+${n(peak, 2)}</b>. Cerrará si PnL cae bajo
              {" "}<b style={{ color: T.green }}>${n(slFloorVal, 2)}</b>.
            </span>
          )}
          {peak >= 1.0 && waitForQuota && (
            <span>
              ⏳ <b style={{ color: T.cyan }}>Esperando {quotaRemaining} spikes</b> más en esta hora UTC ({spikes1h}/{quotaTarget}).
              {dollarFloor > 0 && <> Mientras tanto, piso duro en <b style={{ color: T.green }}>${n(dollarFloor, 0)}</b>.</>}
            </span>
          )}
          {peak >= 1.0 && quotaDone && (
            <span>
              ✓ <b style={{ color: T.amber }}>Cuota cumplida</b> ({spikes1h}/{quotaTarget}). Tier {Math.round(tierPct*1000)/10}% activo —
              cerrará si PnL cae por debajo de <b style={{ color: T.green }}>${n(slFloorVal ?? 0, 2)}</b>.
            </span>
          )}
        </div>
        </>
      )}
    </div>
  );
}

function OpenContractCard({ c }) {
  const pnl = Number(c.floating_pnl ?? c.pnl ?? 0);
  const pnlColor = pnl > 0.001 ? T.green : pnl < -0.001 ? T.red : T.textD;
  const sym = c.symbol || c.underlying || "–";
  const openedTs = c.opened_at_ts;
  const openedSec = openedTs ? Number(openedTs > 1e12 ? openedTs / 1000 : openedTs) : null;
  const duration = openedSec ? Math.round(Date.now() / 1000 - openedSec) : null;
  const ticksSinceSpikeRaw = Number(c.ticks_since_last_spike_now);
  const ticksSinceSpike = Number.isFinite(ticksSinceSpikeRaw) ? Math.max(0, Math.round(ticksSinceSpikeRaw)) : null;
  const tickTargetRaw = Number(c.spike_pre_filter_target_live ?? c.spike_pre_filter_target);
  const tickTarget = Number.isFinite(tickTargetRaw) ? Math.max(0, Math.round(tickTargetRaw)) : null;
  const ticksToTargetRaw = Number(c.ticks_to_prefilter_target);
  const ticksToTarget = Number.isFinite(ticksToTargetRaw)
    ? Math.max(0, Math.round(ticksToTargetRaw))
    : (ticksSinceSpike != null && tickTarget != null ? Math.max(0, tickTarget - ticksSinceSpike) : null);
  const accelRatioRaw = Number(c.tick_acceleration_ratio_2h_vs_6h);
  const accelRatio = Number.isFinite(accelRatioRaw) ? accelRatioRaw : null;
  const accelColor = accelRatio == null ? T.textD : accelRatio > 1.2 ? T.red : accelRatio < 0.8 ? T.green : T.cyan;
  const multispikeBufferRaw = Number(c.multispike_buffer_ticks_live);
  const multispikeBuffer = Number.isFinite(multispikeBufferRaw) ? Math.max(0, Math.round(multispikeBufferRaw)) : null;
  const multispikeMode = String(c.multispike_mode_live || "").toUpperCase();
  const multispikeModeColor = multispikeMode === "FAST" ? T.green : multispikeMode === "SLOW" ? T.red : T.cyan;
  const tickProgressMax = tickTarget != null ? Math.max(1, tickTarget) : Math.max(1, ticksSinceSpike || 0);
  const tickProgress = ticksSinceSpike != null ? Math.min(tickProgressMax, ticksSinceSpike) : 0;
  // DPM panel always shows — stake_usdt is always present in deriv_open_contracts.json.
  // Note: the JSON key is "trail_sl_locked" (from _persist_open), not "trail_sl".
  // DpmTierPanel handles both names via c.trail_sl ?? c.trail_sl_locked fallback.
  const hasDpmData = c.stake_usdt != null;
  const scoreBreakdown = c.score_breakdown || {};
  const scoreVal = c.score ?? scoreBreakdown.score_raw ?? null;
  const hurstVal = c.hurst ?? scoreBreakdown.hurst ?? null;
  const regimeVal = c.regime ?? scoreBreakdown.regime ?? scoreBreakdown.regime_class ?? null;
  const aiApproved = c.ai_approved ?? scoreBreakdown.ai_approved ?? null;
  const aiConfidence = c.ai_confidence ?? scoreBreakdown.ai_confidence ?? null;
  const aiModel = c.ai_model ?? scoreBreakdown.ai_model ?? null;

  // New state for analytics data
  const [spikeProbData, setSpikeProbData] = useState(null);
  const [waitZScoreData, setWaitZScoreData] = useState(null);
  const [rarityPercentileData, setRarityPercentileData] = useState(null);

  const fetchAnalyticsData = useCallback(async () => {
    if (!sym || sym === "–") return;
    try {
      const [spikeProbRes, waitZScoreRes, rarityPercentileRes] = await Promise.all([
        fetch(`/api/deriv/analytics/spike-probability?symbol=${sym}&window=100,200,500`),
        fetch(`/api/deriv/analytics/wait-zscore?symbol=${sym}`),
        fetch(`/api/deriv/analytics/rarity-percentile?symbol=${sym}&lookback=72`),
      ]);

      if (spikeProbRes.ok) {
        const json = await spikeProbRes.json();
        setSpikeProbData(json);
      } else {
        console.error(`Error fetching spike probability for ${sym}: ${spikeProbRes.status}`);
      }

      if (waitZScoreRes.ok) {
        const json = await waitZScoreRes.json();
        setWaitZScoreData(json);
      } else {
        console.error(`Error fetching wait z-score for ${sym}: ${waitZScoreRes.status}`);
      }

      if (rarityPercentileRes.ok) {
        const json = await rarityPercentileRes.json();
        setRarityPercentileData(json);
      } else {
        console.error(`Error fetching rarity percentile for ${sym}: ${rarityPercentileRes.status}`);
      }
    } catch (err) {
      console.error(`Error fetching analytics data for ${sym}: ${err}`);
    }
  }, [sym]);

  useEffect(() => {
    fetchAnalyticsData();
    const interval = setInterval(fetchAnalyticsData, 10000); // Poll every 10 seconds
    return () => clearInterval(interval);
  }, [fetchAnalyticsData]);

  const maxProb = Math.max(...Object.values(spikeProbData?.probabilities || {}).filter(v => v != null));
  const zscore = waitZScoreData?.zscore;
  const rarity = rarityPercentileData?.metrics?.price_change_percentile;

  return (
    <div style={{
      background: T.panel2, borderRadius: 10,
      border: `1px solid ${pnl > 0.001 ? T.green + "55" : pnl < -0.001 ? T.red + "55" : T.border}`,
      padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8,
      boxShadow: pnl > 0.001 ? `0 0 20px ${T.green}14` : pnl < -0.001 ? `0 0 20px ${T.red}14` : "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 14, fontWeight: 700, color: T.text }}>{sym}</span>
          <SideBadge side={c.side} />
        </div>
        <span style={{ fontSize: 18, fontWeight: 700, color: pnlColor, fontFamily: FONT_MONO }}>
          {pnl > 0 ? "+" : ""}{n(pnl, 2)}
        </span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
        <MicroStat lbl="STAKE"   val={`$${n(c.stake_usdt, 2)}`} />
        <MicroStat lbl="MULT"    val={`×${c.multiplier || "–"}`} color={T.violet} />
        <MicroStat lbl="ENTRY"   val={n(c.entry_price, 4)} color={T.cyan} />
        <MicroStat lbl="DUR."    val={dur(duration)} color={T.amber} />
      </div>
      {ticksSinceSpike != null && (
        <div style={{
          border: `1px solid ${T.border}`,
          borderRadius: 7,
          background: "rgba(255,255,255,0.02)",
          padding: "7px 8px",
          display: "flex",
          flexDirection: "column",
          gap: 6,
        }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
            <MicroStat lbl="TICKS SIN SPIKE" val={nC(ticksSinceSpike, 0)} color={T.amber} />
            <MicroStat lbl="TARGET IA" val={tickTarget != null ? nC(tickTarget, 0) : "–"} color={T.cyan} />
            <MicroStat lbl="RATIO 2H/6H" val={accelRatio != null ? n(accelRatio, 2) : "–"} color={accelColor} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
            <MicroStat lbl="MODO" val={multispikeMode || "–"} color={multispikeModeColor} />
            <MicroStat lbl="BUFFER" val={multispikeBuffer != null ? `${nC(multispikeBuffer, 0)}t` : "–"} color={T.violet} />
            <MicroStat lbl="A TARGET" val={ticksToTarget != null ? `${nC(ticksToTarget, 0)}t` : "–"} color={T.textD} />
          </div>
          <MiniBar value={tickProgress} max={tickProgressMax} color={ticksSinceSpike >= (tickTarget || Infinity) ? T.green : T.amber} height={5} />
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: T.mute }}>
            <span>{ticksToTarget != null ? `faltan ${nC(ticksToTarget, 0)} ticks` : "target IA no disponible"}</span>
            <span>{tickTarget != null && ticksSinceSpike >= tickTarget ? "prefiltro listo" : "acumulando"}</span>
          </div>
        </div>
      )}
      {(scoreVal != null || regimeVal != null) && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
          <MicroStat
            lbl={<>SCORE <TooltipHint tip="Puntuación compuesta de la señal (0–10). Mayor score = entrada de mayor calidad." /></>}
            val={scoreVal != null ? n(scoreVal, 2) : "–"}
            color={scoreVal >= 7 ? T.green : scoreVal >= 5 ? T.amber : T.red}
          />
          <MicroStat
            lbl={<>HURST <TooltipHint tip="Exponente Hurst: >0.55 = tendencia persistente, <0.45 = mean-reversion." /></>}
            val={hurstVal != null ? n(hurstVal, 3) : "–"}
            color={T.violet}
          />
          <MicroStat lbl="REGIME" val={regimeVal || "–"} color={REGIME_COLORS[regimeVal] || T.cyan} />
        </div>
      )}
      {/* New analytics data injection */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
        <MicroStat
          lbl={<>PROB. SPIKE <TooltipHint tip="Probabilidad de que ocurra un spike en las próximas ventanas de ticks." /></>}
          val={maxProb != null ? pct(maxProb) : "–"}
          color={maxProb > 0.7 ? T.green : maxProb > 0.5 ? T.amber : T.textD}
        />
        <MicroStat
          lbl={<>Z-SCORE <TooltipHint tip="Medida de cuán inusual es el tiempo de espera actual para un spike." /></>}
          val={zscore != null ? n(zscore, 2) : "–"}
          color={zscore > 2 || zscore < -2 ? T.red : zscore > 1 || zscore < -1 ? T.amber : T.green}
        />
        <MicroStat
          lbl={<>RARITY % <TooltipHint tip="Percentil de rareza del movimiento de precio actual en comparación con el historial." /></>}
          val={rarity != null ? pct(rarity, 0) : "–"}
          color={rarity > 0.9 ? T.red : rarity > 0.75 ? T.amber : T.textD}
        />
      </div>
      {aiApproved != null && (
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <Pill color={aiApproved ? T.green : T.red} size="sm">
            {aiApproved ? "IA ✓" : "IA ✗"}{aiConfidence != null ? ` ${pct(aiConfidence, 0)}` : ""}
          </Pill>
          {aiModel && <span style={{ fontSize: 9, color: T.mute }}>{aiModel}</span>}
        </div>
      )}
      {hasDpmData && <DpmTierPanel c={c} />}
      <div style={{ fontSize: 9, color: T.mute, display: "flex", justifyContent: "space-between" }}>
        <span>#{String(c.contract_id || "–").slice(-8)}</span>
        <span>{fmtT(openedTs)}</span>
      </div>
    </div>
  );
}

function EvalStateBadge({ evalData, prefix = "" }) {
  const hasEval = evalData && Object.keys(evalData).length > 0;
  const shouldClose = Boolean(evalData?.should_close);
  const shadow = Boolean(evalData?.shadow);

  let label = "N/A";
  let color = T.mute;
  if (shouldClose) {
    label = "CLOSE";
    color = T.red;
  } else if (shadow) {
    label = "SHADOW";
    color = T.amber;
  } else if (hasEval) {
    label = "TRACK";
    color = T.green;
  }

  return <Pill color={color} size="sm">{prefix ? `${prefix}:${label}` : label}</Pill>;
}

function buildAuditNarrative({ c, dep, prob, lastDecision }) {
  const pnl = Number(c.floating_pnl ?? c.pnl ?? 0);
  const peak = Number(c.peak_profit ?? 0);
  const gaveBack = Number.isFinite(peak) ? Math.max(0, peak - pnl) : 0;
  const depClose = Boolean(dep?.should_close);
  const probClose = Boolean(prob?.should_close);
  const forcedClose = depClose || probClose || Boolean(c.pending_close_reason);
  const probRaw = Number(prob?.probability);
  const probVal = Number.isFinite(probRaw) ? probRaw : null;
  const blocked = lastDecision?.allowed === false;

  const usd = (v) => `${Number(v) >= 0 ? "+" : ""}$${n(v, 2)}`;

  if (forcedClose && pnl <= 0) {
    return {
      title: "SALIMOS DEL DESASTRE",
      badge: "DEFENSIVO",
      color: T.red,
      message: `Perdida live ${usd(pnl)}. Se activa salida para cortar dano antes de ampliar drawdown.`,
    };
  }

  if (forcedClose && pnl > 0) {
    return {
      title: "ASEGURAMOS GANANCIA",
      badge: "PROTECCION",
      color: T.amber,
      message: `Vamos en verde ${usd(pnl)} y el motor sugiere cobrar para no devolver beneficio.`,
    };
  }

  if (probVal != null && probVal >= 0.70 && pnl >= 0) {
    return {
      title: "PINTA A GANAR",
      badge: "BULLISH",
      color: T.green,
      message: `Probabilidad fuerte (${pct(probVal, 0)}). Seguimos bien mientras no se deteriore DEP.`,
    };
  }

  if (pnl >= 0.10 && (probVal == null || probVal >= 0.45)) {
    return {
      title: "SEGUIMOS BIEN",
      badge: "SALUDABLE",
      color: T.green,
      message: gaveBack > 0.05
        ? `Ganando ${usd(pnl)} pero ya cedio $${n(gaveBack, 2)} desde el pico; vigilar sin panico.`
        : `Ganando ${usd(pnl)} con estructura estable; de momento mantenemos posicion.`,
    };
  }

  if (pnl <= -0.10 && !forcedClose) {
    return {
      title: "ESTAMOS EN RIESGO",
      badge: "AGUANTAMOS",
      color: T.red,
      message: blocked
        ? `Va en rojo ${usd(pnl)} y la ultima decision estuvo bloqueada. Aun aguantamos, alta vigilancia.`
        : `Va en rojo ${usd(pnl)}. Aun aguantamos porque no hay cierre forzado, pero la zona es fragil.`,
    };
  }

  if (blocked) {
    return {
      title: "EN OBSERVACION",
      badge: "BLOQUEADA",
      color: T.amber,
      message: `La ultima decision fue BLOCK. Esperando mejor ventana antes de escalar riesgo.`,
    };
  }

  return {
    title: "SEGUIMIENTO ACTIVO",
    badge: "NEUTRO",
    color: T.cyan,
    message: `Sin alerta critica ahora. Monitoreando DEP, probabilidad y contexto de ticks en vivo.`,
  };
}

function OpenOrderAuditCard({ c, lastDecision }) {
  const dep = c.dep_last_eval || {};
  const prob = c.prob_last_eval || {};
  const sym = c.symbol || c.underlying || "–";
  const pendingReason = c.pending_close_reason || dep.reason || prob.reason || "";

  const lastAllowed = lastDecision?.allowed;
  const lastStateLabel = lastAllowed == null ? "NO DEC" : (lastAllowed ? "GO" : "BLOCK");
  const lastStateColor = lastAllowed == null ? T.mute : (lastAllowed ? T.green : T.red);

  const decisionScoreRaw = Number(lastDecision?.score ?? lastDecision?.score_breakdown?.score_raw);
  const decisionScore = Number.isFinite(decisionScoreRaw) ? decisionScoreRaw : null;
  const decisionRegime = lastDecision?.regime || lastDecision?.score_breakdown?.regime || "–";
  const decisionTs = lastDecision?.ts || lastDecision?.at || lastDecision?.timestamp || null;

  const ticksSince = Number.isFinite(Number(c.ticks_since_last_spike_now))
    ? Math.round(Number(c.ticks_since_last_spike_now))
    : null;
  const targetTicks = Number.isFinite(Number(c.spike_pre_filter_target_live))
    ? Math.round(Number(c.spike_pre_filter_target_live))
    : null;
  const ticksToTarget = Number.isFinite(Number(c.ticks_to_prefilter_target))
    ? Math.round(Number(c.ticks_to_prefilter_target))
    : null;
  const pnlLive = Number(c.floating_pnl ?? c.pnl ?? 0);
  const peakPnl = Number(c.peak_profit ?? 0);
  const gaveBack = Number.isFinite(peakPnl) ? Math.max(0, peakPnl - pnlLive) : 0;
  const probVal = Number.isFinite(Number(prob.probability)) ? Number(prob.probability) : null;
  const hero = buildAuditNarrative({ c, dep, prob, lastDecision });
  const heroGlow = `${hero.color}22`;
  const heroBg = `linear-gradient(130deg, ${hero.color}2f, rgba(8,10,16,0.72) 55%, rgba(8,10,16,0.95))`;

  return (
    <div style={{
      background: T.panel2,
      border: `1px solid ${hero.color}55`,
      borderRadius: 10,
      padding: "10px 12px",
      display: "flex",
      flexDirection: "column",
      gap: 8,
      boxShadow: `0 0 0 1px ${heroGlow} inset, 0 8px 24px rgba(0,0,0,0.28)`,
    }}>
      <div style={{
        border: `1px solid ${hero.color}66`,
        borderRadius: 8,
        background: heroBg,
        padding: "9px 10px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <span style={{
            fontSize: 14,
            fontWeight: 800,
            color: hero.color,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
          }}>{hero.title}</span>
          <Pill color={hero.color} size="sm">{hero.badge}</Pill>
        </div>
        <div style={{ fontSize: 10, color: T.textD, lineHeight: 1.45 }}>
          {hero.message}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 6 }}>
          <MicroStat lbl="PNL LIVE" val={`${pnlLive >= 0 ? "+" : ""}$${n(pnlLive, 2)}`} color={tone(pnlLive)} />
          <MicroStat lbl="PERDIDO PICO" val={`$${n(gaveBack, 2)}`} color={gaveBack > 0.05 ? T.amber : T.textD} />
          <MicroStat lbl="PROB HOY" val={probVal != null ? pct(probVal, 0) : "–"} color={probVal != null ? (probVal >= 0.65 ? T.green : probVal <= 0.35 ? T.red : T.amber) : T.mute} />
          <MicroStat lbl="ULTIMA" val={lastStateLabel} color={lastStateColor} />
        </div>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.text }}>{sym}</span>
          <SideBadge side={c.side} />
          <span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>#{String(c.contract_id || "–").slice(-8)}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <EvalStateBadge evalData={dep} prefix="DEP" />
          <EvalStateBadge evalData={prob} prefix="PROB" />
          <Pill color={lastStateColor} size="sm">{lastStateLabel}</Pill>
        </div>
      </div>

      {pendingReason && (
        <div style={{
          fontSize: 9,
          color: T.red,
          background: "rgba(255,93,108,0.08)",
          border: `1px solid ${T.red}44`,
          borderRadius: 5,
          padding: "5px 7px",
        }}>
          pending: {pendingReason}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
        <div style={{ border: `1px solid ${T.border}`, borderRadius: 6, padding: "6px 7px", display: "flex", flexDirection: "column", gap: 5 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 9, color: T.textD, letterSpacing: "0.09em" }}>DEP</span>
            <span style={{ fontSize: 9, color: T.cyan, fontFamily: FONT_MONO }}>{String(dep.policy || "PASSIVE").toUpperCase()}</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 5 }}>
            <MicroStat lbl="ATR RATIO" val={dep.atr_ratio != null ? n(dep.atr_ratio, 3) : "–"} color={T.cyan} />
            <MicroStat lbl="THR" val={dep.decay_threshold != null ? n(dep.decay_threshold, 3) : "–"} color={T.amber} />
            <MicroStat lbl="PNL" val={dep.current_pnl != null ? n(dep.current_pnl, 3) : "–"} color={tone(dep.current_pnl)} />
            <MicroStat lbl="FLOOR" val={dep.loss_floor_usdt != null ? n(dep.loss_floor_usdt, 3) : "–"} color={T.redD} />
          </div>
        </div>

        <div style={{ border: `1px solid ${T.border}`, borderRadius: 6, padding: "6px 7px", display: "flex", flexDirection: "column", gap: 5 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 9, color: T.textD, letterSpacing: "0.09em" }}>OPEN PROB</span>
            <span style={{ fontSize: 9, color: T.cyan, fontFamily: FONT_MONO }}>{prob.probability != null ? pct(prob.probability, 0) : "–"}</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 5 }}>
            <MicroStat lbl="THR" val={prob.close_threshold != null ? pct(prob.close_threshold, 0) : "–"} color={T.amber} />
            <MicroStat lbl="CYCLE" val={prob.cycle_progress != null ? n(prob.cycle_progress, 2) : "–"} color={T.violet} />
            <MicroStat lbl="ATR RATIO" val={prob.atr_ratio != null ? n(prob.atr_ratio, 3) : "–"} color={T.cyan} />
            <MicroStat lbl="PNL CEIL" val={prob.close_pnl_ceil != null ? n(prob.close_pnl_ceil, 3) : "–"} color={T.redD} />
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
        <MicroStat lbl="TICKS" val={ticksSince != null ? nC(ticksSince, 0) : "–"} color={T.amber} />
        <MicroStat lbl="TARGET" val={targetTicks != null ? nC(targetTicks, 0) : "–"} color={T.cyan} />
        <MicroStat lbl="A TARGET" val={ticksToTarget != null ? nC(ticksToTarget, 0) : "–"} color={T.textD} />
        <MicroStat lbl="ACCEL 2H/6H" val={c.tick_acceleration_ratio_2h_vs_6h != null ? n(c.tick_acceleration_ratio_2h_vs_6h, 2) : "–"} color={T.blue} />
      </div>

      <div style={{
        borderTop: `1px solid ${T.border}`,
        paddingTop: 6,
        display: "grid",
        gridTemplateColumns: "repeat(4, 1fr)",
        gap: 6,
      }}>
        <MicroStat lbl="LAST SCORE" val={decisionScore != null ? n(decisionScore, 2) : "–"} color={decisionScore != null ? (decisionScore >= 7 ? T.green : decisionScore >= 5 ? T.amber : T.red) : T.mute} />
        <MicroStat lbl="LAST REGIME" val={decisionRegime} color={REGIME_COLORS[decisionRegime] || T.cyan} />
        <MicroStat lbl="LAST DEC" val={lastStateLabel} color={lastStateColor} />
        <MicroStat lbl="EVAL TS" val={fmtT(decisionTs)} color={T.textD} />
      </div>

      {lastDecision?.reason && (
        <div style={{
          fontSize: 9,
          color: lastAllowed ? T.textD : T.redD,
          background: lastAllowed ? "rgba(255,255,255,0.03)" : "rgba(255,93,108,0.06)",
          border: `1px solid ${lastAllowed ? T.border : T.red + "33"}`,
          borderRadius: 5,
          padding: "5px 7px",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}>
          {lastDecision.reason}
        </div>
      )}
    </div>
  );
}

function OperacionesTab({ data }) {
  const open   = data.open_contracts   || [];
  const closed = data.closed_contracts || [];
  const decisionBySymbol = data?.decisions?.last_by_symbol || {};

  const [symFilter,    setSymFilter]    = useState("all");
  const [sideFilter,   setSideFilter]   = useState("all");
  const [resultFilter, setResultFilter] = useState("all");
  const [sortBy,       setSortBy]       = useState("date");
  const [sortDir,      setSortDir]      = useState("desc");
  const [search,       setSearch]       = useState("");
  const [page,         setPage]         = useState(0);
  const PAGE_SIZE = 25;

  const syms = useMemo(
    () => [...new Set(closed.map(c => c.symbol || c.underlying || "–"))].sort(),
    [closed]
  );

  const openAuditRows = useMemo(() => {
    return open.map(c => {
      const key = String(c.symbol || c.underlying || "").toUpperCase();
      const dec = decisionBySymbol[key] || decisionBySymbol[c.symbol] || null;
      return { c, dec };
    });
  }, [open, decisionBySymbol]);

  const filteredClosed = useMemo(() => {
    const rows = closed.filter(c => {
      const sym = c.symbol || c.underlying || "–";
      const pnl = Number(c.realized_pnl_usdt ?? c.pnl ?? c.profit ?? 0);
      if (symFilter !== "all" && sym !== symFilter) return false;
      if (sideFilter !== "all") {
        const s = String(c.side || "").toUpperCase();
        if (sideFilter === "up"   && !s.includes("UP"))   return false;
        if (sideFilter === "down" && !s.includes("DOWN")) return false;
      }
      if (resultFilter === "win"  && pnl <= 0) return false;
      if (resultFilter === "loss" && pnl >= 0) return false;
      if (search) {
        const q = `${sym} ${c.side || ""} ${c.exit_reason || ""}`.toLowerCase();
        if (!q.includes(search.toLowerCase())) return false;
      }
      return true;
    });
    rows.sort((a, b) => {
      let va = 0, vb = 0;
      if (sortBy === "date")  { va = Number(a.closed_at_ts || a.opened_at_ts || 0); vb = Number(b.closed_at_ts || b.opened_at_ts || 0); }
      if (sortBy === "pnl")   { va = Number(a.realized_pnl_usdt ?? a.pnl ?? 0); vb = Number(b.realized_pnl_usdt ?? b.pnl ?? 0); }
      if (sortBy === "score") { va = Number(a.score || 0); vb = Number(b.score || 0); }
      if (sortBy === "dur") {
        const d1 = a.closed_at_ts && a.opened_at_ts ? Math.abs(Number(a.closed_at_ts) - Number(a.opened_at_ts)) : 0;
        const d2 = b.closed_at_ts && b.opened_at_ts ? Math.abs(Number(b.closed_at_ts) - Number(b.opened_at_ts)) : 0;
        va = d1; vb = d2;
      }
      return sortDir === "desc" ? vb - va : va - vb;
    });
    return rows;
  }, [closed, symFilter, sideFilter, resultFilter, search, sortBy, sortDir]);

  const totalPages  = Math.ceil(filteredClosed.length / PAGE_SIZE);
  const paginated   = filteredClosed.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const wins        = filteredClosed.filter(c => Number(c.realized_pnl_usdt ?? c.pnl ?? c.profit ?? 0) > 0).length;
  const totalPnl    = filteredClosed.reduce((s, c) => s + Number(c.realized_pnl_usdt ?? c.pnl ?? c.profit ?? 0), 0);
  const winRate     = filteredClosed.length > 0 ? wins / filteredClosed.length : null;

  const SortBtn = ({ id, label }) => (
    <button
      onClick={() => {
        if (sortBy === id) setSortDir(d => d === "desc" ? "asc" : "desc");
        else { setSortBy(id); setSortDir("desc"); setPage(0); }
      }}
      style={{
        background: sortBy === id ? `${T.cyan}18` : "transparent",
        color: sortBy === id ? T.cyan : T.mute,
        border: `1px solid ${sortBy === id ? T.cyan + "44" : "transparent"}`,
        borderRadius: 4, padding: "3px 8px", cursor: "pointer",
        fontFamily: FONT_MONO, fontSize: 9, fontWeight: 700, letterSpacing: "0.08em",
      }}
    >{label}{sortBy === id ? (sortDir === "desc" ? " ↓" : " ↑") : ""}</button>
  );

  const exportUrl = (fmt) => {
    const p = new URLSearchParams({ dataset: "trades", format: fmt });
    if (symFilter    !== "all") p.set("symbol", symFilter);
    if (sideFilter   === "up")   p.set("side", "MULTUP");
    if (sideFilter   === "down") p.set("side", "MULTDOWN");
    if (resultFilter !== "all") p.set("result", resultFilter);
    return `/api/deriv-analytics/export?${p.toString()}`;
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>

      {/* ── POSICIONES ABIERTAS ── */}
      <Panel
        title={`posiciones abiertas · ${open.length}`}
        right={open.length > 0 ? <span style={{ color: T.amber, fontWeight: 700, fontSize: 10 }}>● LIVE</span> : null}
        glow
      >
        {open.length === 0
          ? <div style={{ color: T.mute, fontSize: 11, padding: "6px 0" }}>sin posiciones abiertas en este momento</div>
          : <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 10 }}>
              {open.map((c, i) => <OpenContractCard key={c.contract_id || i} c={c} />)}
            </div>
        }
      </Panel>

      {open.length > 0 && (
        <Panel
          title={`auditoria live de ordenes abiertas · ${open.length}`}
          right={<span style={{ color: T.cyan, fontWeight: 700, fontSize: 10 }}>DEP + OPEN_PROB + LAST_DECISION</span>}
        >
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(360px, 1fr))", gap: 10 }}>
            {openAuditRows.map(({ c, dec }, i) => (
              <OpenOrderAuditCard key={`${c.contract_id || i}-audit`} c={c} lastDecision={dec} />
            ))}
          </div>
        </Panel>
      )}

      {/* ── KPI STRIP ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 10 }}>
        <KPI label="Trades (filtro)" value={nC(filteredClosed.length)} accent={T.cyan} />
        <KPI label="Ganadas"  value={wins}  color={T.green} accent={T.green} />
        <KPI label="Perdidas" value={filteredClosed.length - wins} color={T.red} accent={T.red} />
        <KPI label="Winrate" value={winRate != null ? pct(winRate) : "–"} color={winRate != null ? (winRate >= 0.5 ? T.green : T.red) : T.textD} accent={T.violet} />
        <KPI label="PnL filtrado" value={`${totalPnl >= 0 ? "+" : ""}$${n(totalPnl, 2)}`} color={tone(totalPnl)} accent={tone(totalPnl)} />
      </div>

      {/* ── TABLA HISTÓRICA ── */}
      <Panel
        title={`historial de operaciones · ${filteredClosed.length} trades`}
        pad={false}
        right={
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <input
              value={search} onChange={e => { setSearch(e.target.value); setPage(0); }}
              placeholder="buscar…" style={inputStyle}
            />
            <select value={symFilter} onChange={e => { setSymFilter(e.target.value); setPage(0); }}
              style={{ ...inputStyle, minWidth: 110 }}>
              <option value="all">Todos los símbolos</option>
              {syms.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <select value={sideFilter} onChange={e => { setSideFilter(e.target.value); setPage(0); }}
              style={{ ...inputStyle, minWidth: 90 }}>
              <option value="all">SUBE + BAJA</option>
              <option value="up">▲ Solo SUBE</option>
              <option value="down">▼ Solo BAJA</option>
            </select>
            <select value={resultFilter} onChange={e => { setResultFilter(e.target.value); setPage(0); }}
              style={{ ...inputStyle, minWidth: 100 }}>
              <option value="all">Todos resultados</option>
              <option value="win">✓ Solo wins</option>
              <option value="loss">✗ Solo losses</option>
            </select>
          </div>
        }
      >
        {/* Sort + Export row */}
        <div style={{ display: "flex", gap: 6, padding: "10px 14px 6px", borderBottom: `1px solid ${T.border}`, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 9, color: T.mute, letterSpacing: "0.12em", fontWeight: 700 }}>ORDEN:</span>
          <SortBtn id="date"  label="FECHA" />
          <SortBtn id="pnl"   label="PNL" />
          <SortBtn id="score" label="SCORE" />
          <SortBtn id="dur"   label="DUR." />
          <div style={{ flex: 1 }} />
          <a href={exportUrl("csv")}  download style={{ ...btnStyle(T.green), textDecoration: "none", padding: "3px 10px" }}>↓ CSV</a>
          <a href={exportUrl("json")} download style={{ ...btnStyle(T.cyan),  textDecoration: "none", padding: "3px 10px" }}>↓ JSON</a>
          <a href={exportUrl("csv") + "&all=1"} download style={{ ...btnStyle(T.amber), textDecoration: "none", padding: "3px 10px" }}>↓ MUESTRA COMPLETA</a>
        </div>

        {/* Column headers */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "28px 100px 72px 54px 44px 60px 54px 84px 84px 1fr 70px",
          gap: 4, padding: "6px 14px",
          fontSize: 9, color: T.mute, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase",
          borderBottom: `1px solid ${T.border}`,
        }}>
          <span>#</span>
          <span>SÍMBOLO</span>
          <span>DIRECCIÓN</span>
          <span>STAKE</span>
          <span>MULT</span>
          <span>
            SCORE{" "}
            <TooltipHint tip="Puntuación compuesta de la señal de entrada (0–10). Mayor score = mayor calidad de entrada." />
          </span>
          <span>DUR.</span>
          <span>APERTURA</span>
          <span>CIERRE</span>
          <span>RAZÓN</span>
          <span>PNL</span>
        </div>

        {/* Rows */}
        <div style={{ maxHeight: 560, overflowY: "auto" }}>
          {paginated.length === 0 && (
            <div style={{ color: T.mute, fontSize: 11, padding: "16px 14px" }}>sin trades con los filtros seleccionados</div>
          )}
          {paginated.map((c, i) => {
            const pnl    = Number(c.realized_pnl_usdt ?? c.pnl ?? c.profit ?? 0);
            const isWin  = pnl > 0;
            const sym    = c.symbol || c.underlying || "–";
            const oTs    = c.opened_at_ts;
            const cTs    = c.closed_at_ts;
            const sec1   = Number(oTs > 1e12 ? oTs / 1000 : oTs);
            const sec2   = Number(cTs > 1e12 ? cTs / 1000 : cTs);
            const holdSec = oTs && cTs ? Math.abs(sec2 - sec1) : null;
            const rowIdx  = page * PAGE_SIZE + i;
            return (
              <div key={c.contract_id || i} style={{
                display: "grid",
                gridTemplateColumns: "28px 100px 72px 54px 44px 60px 54px 84px 84px 1fr 70px",
                gap: 4, padding: "5px 14px",
                background: rowIdx % 2 === 0 ? T.panel2 : T.panel,
                alignItems: "center", fontSize: 10,
                borderLeft: `3px solid ${isWin ? T.green + "66" : T.red + "66"}`,
              }}>
                <span style={{ color: T.mute, fontSize: 9 }}>{rowIdx + 1}</span>
                <span style={{ fontWeight: 700, color: T.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sym}</span>
                <SideBadge side={c.side} />
                <span style={{ color: T.textD }}>{n(c.stake_usdt, 2)}</span>
                <span style={{ color: T.violet }}>×{c.multiplier || "–"}</span>
                <span style={{ color: c.score != null ? (c.score >= 7 ? T.green : c.score >= 5 ? T.amber : T.red) : T.mute }}>
                  {c.score != null ? n(c.score, 1) : "–"}
                </span>
                <span style={{ color: T.textD }}>{dur(holdSec)}</span>
                <span style={{ color: T.mute, fontSize: 9 }}>{fmtDT(oTs)}</span>
                <span style={{ color: T.mute, fontSize: 9 }}>{fmtDT(cTs)}</span>
                <span style={{ color: T.textD, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 9 }}>
                  {c.exit_reason || "–"}
                </span>
                <span style={{ fontWeight: 700, color: isWin ? T.green : T.red }}>
                  {pnl > 0 ? "+" : ""}{n(pnl, 2)}
                </span>
              </div>
            );
          })}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div style={{ display: "flex", gap: 8, padding: "10px 14px", borderTop: `1px solid ${T.border}`, alignItems: "center" }}>
            <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
              style={{ ...btnStyle(T.cyan), opacity: page === 0 ? 0.3 : 1 }}>← PREV</button>
            <span style={{ fontSize: 10, color: T.mute, flex: 1, textAlign: "center" }}>
              Página {page + 1} de {totalPages} · {filteredClosed.length} trades
            </span>
            <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}
              style={{ ...btnStyle(T.cyan), opacity: page >= totalPages - 1 ? 0.3 : 1 }}>NEXT →</button>
          </div>
        )}
      </Panel>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 1 — OVERVIEW
   ════════════════════════════════════════════════════════════════════════ */
function OverviewTab({ data }) {
  const g = data?.global || {};
  const dd = g.max_drawdown || {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        <KPI label="Total trades" value={nC(g.total_trades)} accent={T.cyan} big />
        <KPI label="Winrate" value={pct(g.winrate)} color={g.winrate >= 0.5 ? T.green : T.red} accent={g.winrate >= 0.5 ? T.green : T.red} big />
        <KPI label="Profit factor" value={g.profit_factor != null ? n(g.profit_factor, 2) : "–"} accent={T.violet} big />
        <KPI label="Expectancy" value={`$${n(g.expectancy, 3)}`} color={tone(g.expectancy)} accent={T.green} big />
        <KPI label="Avg duration" value={dur(g.avg_duration_sec)} accent={T.amber} />
        <KPI label="Sharpe-like" value={n(g.sharpe_like, 2)} accent={T.violet} />
        <KPI label="Max drawdown" value={`$${n(dd.max_dd, 2)}`} sub={pct(dd.max_dd_pct)} color={T.red} accent={T.red} />
        <KPI label="Streak" value={`${g.streaks?.current?.type || "–"} ${g.streaks?.current?.n || 0}`} sub={`max W:${g.streaks?.max_win || 0} / L:${g.streaks?.max_loss || 0}`} accent={T.cyan} />
      </div>

      <Panel title="equity curve · cumulative pnl" right={`${(data.equity_curve || []).length} pts`} glow>
        <div style={{ height: 280 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data.equity_curve || []}>
              <defs>
                <linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={T.cyan} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={T.cyan} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
              <XAxis dataKey="ts" tickFormatter={fmtDT} tick={CHART_AXIS} stroke={T.mute} />
              <YAxis tick={CHART_AXIS} stroke={T.mute} width={50} />
              <Tooltip content={chartTooltip()} />
              <ReferenceLine y={0} stroke={T.mute} strokeDasharray="3 3" />
              <Area type="monotone" dataKey="pnl" stroke={T.cyan} strokeWidth={1.6} fill="url(#eqg)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12 }}>
        <Panel title="daily pnl">
          <div style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.daily_pnl || []}>
                <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
                <XAxis dataKey="date" tick={CHART_AXIS} stroke={T.mute} />
                <YAxis tick={CHART_AXIS} stroke={T.mute} width={42} />
                <Tooltip content={chartTooltip()} />
                <ReferenceLine y={0} stroke={T.mute} />
                <Bar dataKey="pnl" radius={[2, 2, 0, 0]}>
                  {(data.daily_pnl || []).map((d, i) => <Cell key={i} fill={d.pnl >= 0 ? T.green : T.red} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>
        <Panel title="day×hour heatmap (pnl)">
          <HeatmapDayHour data={data.heatmap_pnl} counts={data.heatmap_count} />
        </Panel>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        <Panel title="by symbol"><BucketTable data={data.by_symbol} /></Panel>
        <Panel title="by strategy"><BucketTable data={data.by_strategy} /></Panel>
        <Panel title="by regime"><BucketTable data={data.by_regime} colorMap={k => REGIME_COLORS[k] || T.cyan} /></Panel>
      </div>
    </div>
  );
}

function HeatmapDayHour({ data, counts }) {
  const grid = data || Array(7).fill(0).map(() => Array(24).fill(0));
  const cnt = counts || Array(7).fill(0).map(() => Array(24).fill(0));
  let maxAbs = 0;
  for (const row of grid) for (const v of row) maxAbs = Math.max(maxAbs, Math.abs(v));
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const cellOf = (v) => {
    if (maxAbs === 0) return "rgba(255,255,255,0.03)";
    const a = Math.min(1, Math.abs(v) / maxAbs);
    if (v > 0) return `rgba(34,211,163,${0.1 + 0.7 * a})`;
    if (v < 0) return `rgba(255,93,108,${0.1 + 0.7 * a})`;
    return "rgba(255,255,255,0.03)";
  };
  return (
    <div style={{ display: "grid", gridTemplateColumns: "32px repeat(24, 1fr)", gap: 1, fontFamily: FONT_MONO }}>
      <span />
      {Array.from({ length: 24 }, (_, h) => (
        <span key={h} style={{ fontSize: 7, color: T.mute, textAlign: "center" }}>{h % 2 === 0 ? h : ""}</span>
      ))}
      {days.map((d, di) => [
        <span key={`d-${di}`} style={{ fontSize: 8, color: T.mute, fontWeight: 700, padding: "2px 0" }}>{d}</span>,
        ...Array.from({ length: 24 }, (_, h) => (
          <div key={`${di}-${h}`} title={`${d} ${h}:00 · pnl=${n(grid[di][h], 2)} · n=${cnt[di][h] || 0}`} style={{
            background: cellOf(grid[di][h]),
            height: 16, borderRadius: 2,
          }} />
        )),
      ])}
    </div>
  );
}

function BucketTable({ data, keyName = "key", colorMap }) {
  if (!data || Object.keys(data).length === 0) return <div style={{ color: T.mute, fontSize: 11 }}>no data</div>;
  const rows = Object.entries(data).map(([k, v]) => ({ ...v, [keyName]: k })).sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 10 }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 38px 50px 48px 70px", gap: 6, padding: "4px 6px", color: T.mute, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", fontSize: 8 }}>
        <span>{keyName}</span><span style={{ textAlign: "right" }}>N</span><span style={{ textAlign: "right" }}>Win%</span><span style={{ textAlign: "right" }}>PF</span><span style={{ textAlign: "right" }}>PnL</span>
      </div>
      {rows.map(r => (
        <div key={r[keyName]} style={{ display: "grid", gridTemplateColumns: "1fr 38px 50px 48px 70px", gap: 6, padding: "5px 6px", background: T.panel2, borderRadius: 4, alignItems: "center" }}>
          <span style={{ color: colorMap ? colorMap(r[keyName]) : T.text, fontWeight: 700, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r[keyName]}</span>
          <span style={{ textAlign: "right", color: T.textD }}>{r.n || 0}</span>
          <span style={{ textAlign: "right", color: r.winrate >= 0.5 ? T.green : T.red }}>{pct(r.winrate, 0)}</span>
          <span style={{ textAlign: "right", color: T.violet }}>{r.profit_factor != null ? n(r.profit_factor, 1) : "–"}</span>
          <span style={{ textAlign: "right", color: tone(r.pnl), fontWeight: 700 }}>{sign(r.pnl)}{n(r.pnl, 2)}</span>
        </div>
      ))}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   AUTO-INSIGHTS — genera análisis automáticos en español
   ════════════════════════════════════════════════════════════════════════ */
function AutoInsights({ data }) {
  const insights = useMemo(() => {
    const g         = data?.global        || {};
    const bySymbol  = data?.by_symbol     || {};
    const byRegime  = data?.by_regime     || {};
    const byScore   = data?.by_score_band || {};
    const byHurst   = data?.by_hurst_zone || {};
    const decisions = data?.decisions?.recent || [];
    const N_MIN = 5;
    const res = [];

    if ((g.total_trades || 0) < N_MIN) {
      res.push({ icon: "◷", c: T.mute, txt: `Necesitas al menos ${N_MIN} trades completados para generar insights automáticos.` });
      return res;
    }

    // Winrate
    const wr = g.winrate;
    if (wr >= 0.6)      res.push({ icon: "✓", c: T.green, txt: `Winrate de ${pct(wr, 0)} en ${g.total_trades} trades — rendimiento sólido.` });
    else if (wr < 0.4)  res.push({ icon: "⚠", c: T.red,   txt: `Winrate en ${pct(wr, 0)} — por debajo del 50%. Considera revisar los filtros de entrada.` });
    else                res.push({ icon: "◉", c: T.amber,  txt: `Winrate de ${pct(wr, 0)} — dentro del rango neutro. Hay margen para optimizar.` });

    // Profit factor
    if (g.profit_factor != null) {
      if (g.profit_factor >= 1.5)     res.push({ icon: "★", c: T.green, txt: `Profit Factor ${n(g.profit_factor, 2)}: cada $1 arriesgado genera $${n(g.profit_factor, 2)} en ganancias brutas.` });
      else if (g.profit_factor < 1.0) res.push({ icon: "✗", c: T.red,   txt: `Profit Factor de ${n(g.profit_factor, 2)}: el sistema está perdiendo en términos brutos. Revisa los filtros de entrada.` });
    }

    // Best / worst symbol
    const symRows = Object.entries(bySymbol).filter(([, v]) => (v.n || 0) >= N_MIN);
    if (symRows.length > 0) {
      const [bestSym, bestV]   = symRows.reduce((a, b) => (b[1].pnl || 0) > (a[1].pnl || 0) ? b : a);
      const [worstSym, worstV] = symRows.reduce((a, b) => (b[1].pnl || 0) < (a[1].pnl || 0) ? b : a);
      res.push({ icon: "◉", c: T.cyan, txt: `${bestSym} es el símbolo más rentable: $${n(bestV.pnl, 2)} PnL, ${pct(bestV.winrate, 0)} WR.` });
      if (worstSym !== bestSym)
        res.push({ icon: "⚡", c: T.amber, txt: `${worstSym} arrastra el PnL con $${n(worstV.pnl, 2)}. Considera ajustar su perfil de entrada.` });
    }

    // Best / worst regime
    const regRows = Object.entries(byRegime).filter(([, v]) => (v.n || 0) >= N_MIN);
    if (regRows.length >= 2) {
      const [bestReg, bestRV]   = regRows.reduce((a, b) => (b[1].winrate || 0) > (a[1].winrate || 0) ? b : a);
      const [worstReg, worstRV] = regRows.reduce((a, b) => (b[1].winrate || 0) < (a[1].winrate || 0) ? b : a);
      res.push({ icon: "◈", c: REGIME_COLORS[bestReg] || T.cyan, txt: `Mejor régimen de mercado: ${bestReg} con ${pct(bestRV.winrate, 0)} WR y PF ${n(bestRV.profit_factor, 2)}.` });
      if (worstReg !== bestReg && (worstRV.winrate || 0) < 0.4)
        res.push({ icon: "⚠", c: T.amber, txt: `Régimen ${worstReg} tiene WR de ${pct(worstRV.winrate, 0)} — candidato a filtrar o reducir exposición.` });
    }

    // Score band efficiency
    const scoreBands = Object.entries(byScore).filter(([, v]) => (v.n || 0) >= N_MIN);
    if (scoreBands.length >= 2) {
      const [bestBand, bestBV] = scoreBands.reduce((a, b) =>
        ((b[1].pnl || 0) / (b[1].n || 1)) > ((a[1].pnl || 0) / (a[1].n || 1)) ? b : a
      );
      res.push({ icon: "◎", c: T.violet, txt: `Zona de score más eficiente: ${bestBand} con $${n((bestBV.pnl || 0) / (bestBV.n || 1), 3)} PnL/trade.` });
    }

    // AI filter ratio
    if (decisions.length >= 20) {
      const aiBlocked = decisions.filter(d => !d.allowed && String(d.reason || "").toUpperCase().includes("AI")).length;
      const aiRate    = aiBlocked / decisions.length;
      if (aiRate > 0.3)
        res.push({ icon: "🤖", c: T.violet, txt: `La IA está bloqueando el ${pct(aiRate, 0)} de las señales. Si parece excesivo, revisa el umbral AI en la config.` });
      else if (aiRate < 0.05 && decisions.length > 50)
        res.push({ icon: "🤖", c: T.cyan, txt: `La IA aprueba casi todo (${pct(1 - aiRate, 0)}). El filtro AI podría estar demasiado permisivo — verifica la conectividad.` });
    }

    // Hurst zone
    const hurstRows = Object.entries(byHurst).filter(([, v]) => (v.n || 0) >= N_MIN);
    if (hurstRows.length >= 2) {
      const [bestH, bestHV] = hurstRows.reduce((a, b) => (b[1].winrate || 0) > (a[1].winrate || 0) ? b : a);
      res.push({ icon: "〰", c: T.blue, txt: `Zona Hurst más rentable: ${bestH} (WR ${pct(bestHV.winrate, 0)}). Prioriza entradas en esta zona.` });
    }

    // Drawdown warning
    const ddPct = g.max_drawdown?.max_dd_pct;
    if (ddPct != null && ddPct > 0.15)
      res.push({ icon: "▼", c: T.red, txt: `Max drawdown de ${pct(ddPct, 1)}: zona de riesgo elevado. Revisa los límites del risk manager.` });

    // Expectancy
    if (g.expectancy != null) {
      if (g.expectancy > 0)       res.push({ icon: "→", c: T.green, txt: `Expectancy positiva de $${n(g.expectancy, 3)} por trade — el sistema es matemáticamente rentable.` });
      else if (g.expectancy < 0)  res.push({ icon: "→", c: T.red,   txt: `Expectancy negativa ($${n(g.expectancy, 3)}/trade) — el sistema pierde dinero en promedio. Revisa ratio riesgo/recompensa.` });
    }

    return res;
  }, [data]);

  return (
    <Panel
      title="insights automáticos"
      right={<span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>basado en datos reales · español</span>}
      glow
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        {insights.map((ins, i) => (
          <div key={i} style={{
            display: "flex", gap: 10, alignItems: "flex-start",
            padding: "8px 12px", borderRadius: 6,
            background: `${ins.c}0d`, border: `1px solid ${ins.c}22`,
          }}>
            <span style={{ fontSize: 13, color: ins.c, flexShrink: 0, marginTop: 1 }}>{ins.icon}</span>
            <span style={{ fontSize: 11, color: T.textD, lineHeight: 1.55, fontFamily: FONT_MONO }}>{ins.txt}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 2 — ANALYTICS
   ════════════════════════════════════════════════════════════════════════ */
function AnalyticsTab({ data }) {
  const [w, setW] = useState(20);
  const rolling = w === 20 ? data.rolling_20 : data.rolling_50;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <AutoInsights data={data} />
      <Panel
        title={`rolling metrics · window=${w}`}
        right={<div style={{ display: "flex", gap: 4 }}>{[20, 50].map(x => <button key={x} onClick={() => setW(x)} style={{ ...btnStyle(w === x ? T.cyan : T.mute), padding: "3px 8px", fontSize: 9 }}>{x}</button>)}</div>}
        glow>
        <div style={{ height: 260 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rolling || []}>
              <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
              <XAxis dataKey="i" tick={CHART_AXIS} stroke={T.mute} />
              <YAxis yAxisId="L" tick={CHART_AXIS} stroke={T.mute} width={42} />
              <YAxis yAxisId="R" orientation="right" tick={CHART_AXIS} stroke={T.mute} width={42} />
              <Tooltip content={chartTooltip()} />
              <Legend wrapperStyle={{ fontSize: 9, fontFamily: FONT_MONO, color: T.mute }} />
              <Line yAxisId="L" type="monotone" dataKey="winrate" stroke={T.cyan} strokeWidth={1.5} dot={false} />
              <Line yAxisId="L" type="monotone" dataKey="expectancy" stroke={T.green} strokeWidth={1.5} dot={false} />
              <Line yAxisId="R" type="monotone" dataKey="profit_factor" stroke={T.violet} strokeWidth={1.5} dot={false} name="PF" />
              <ReferenceLine yAxisId="L" y={0.5} stroke={T.mute} strokeDasharray="3 3" />
              <ReferenceLine yAxisId="R" y={1} stroke={T.mute} strokeDasharray="3 3" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Panel title="performance by score band"><BucketBars data={data.by_score_band} order={["<4","4-5","5-6","6-7","7-8","8-9","9+"]} /></Panel>
        <Panel title="performance by hurst zone"><BucketBars data={data.by_hurst_zone} colorMap={k => String(k).includes("A") ? T.red : String(k).includes("B") ? T.amber : String(k).includes("C") ? T.green : T.cyan} /></Panel>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Panel title="performance by regime"><BucketBars data={data.by_regime} colorMap={k => REGIME_COLORS[k] || T.cyan} /></Panel>
        <Panel title="performance by strategy mode"><BucketBars data={data.by_strategy} /></Panel>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        <ScatterPanel title="score vs pnl" data={data.scatter} xKey="score" yKey="pnl" xLabel="Score" />
        <ScatterPanel title="hurst vs pnl" data={data.scatter} xKey="hurst" yKey="pnl" xLabel="Hurst" />
        <ScatterPanel title="atr% vs pnl"  data={data.scatter} xKey="atr_pct" yKey="pnl" xLabel="ATR %" />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Panel title="correlation matrix (pearson)"><CorrelationMatrix data={data.correlations} /></Panel>
        <Panel title="score / hurst distribution"><Histograms scoreHist={data.histograms?.score} hurstHist={data.histograms?.hurst} /></Panel>
      </div>

      <Panel title="drawdown timeline" right={`max DD $${n(data.global?.max_drawdown?.max_dd, 2)}`}>
        <DrawdownChart equity={data.equity_curve} />
      </Panel>
    </div>
  );
}

function BucketBars({ data, order, colorMap }) {
  if (!data || Object.keys(data).length === 0) return <div style={{ color: T.mute, fontSize: 11 }}>no data</div>;
  let rows = Object.entries(data).map(([k, v]) => ({ ...v, key: k }));
  if (order) rows.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  else rows.sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
  return (
    <div style={{ height: 220 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} layout="vertical">
          <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
          <XAxis type="number" tick={CHART_AXIS} stroke={T.mute} />
          <YAxis dataKey="key" type="category" tick={CHART_AXIS} stroke={T.mute} width={80} />
          <Tooltip content={chartTooltip((p) => [
            { name: "n", value: p[0].payload.n, color: T.mute },
            { name: "pnl", value: p[0].payload.pnl, color: tone(p[0].payload.pnl) },
            { name: "winrate", value: pct(p[0].payload.winrate), color: T.cyan },
            { name: "PF", value: p[0].payload.profit_factor, color: T.violet },
          ])} />
          <ReferenceLine x={0} stroke={T.mute} />
          <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
            {rows.map((r, i) => <Cell key={i} fill={colorMap ? colorMap(r.key) : (r.pnl >= 0 ? T.green : T.red)} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function ScatterPanel({ title, data, xKey, yKey, xLabel }) {
  const pts = (data || []).filter(p => Number.isFinite(p[xKey]) && Number.isFinite(p[yKey]));
  return (
    <Panel title={title} right={`${pts.length} pts`}>
      <div style={{ height: 220 }}>
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart>
            <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
            <XAxis dataKey={xKey} type="number" tick={CHART_AXIS} stroke={T.mute} name={xLabel} />
            <YAxis dataKey={yKey} type="number" tick={CHART_AXIS} stroke={T.mute} width={42} />
            <Tooltip content={chartTooltip((p) => [
              { name: "symbol", value: p[0].payload.symbol, color: T.cyan },
              { name: xKey, value: p[0].payload[xKey], color: T.text },
              { name: "pnl", value: p[0].payload[yKey], color: tone(p[0].payload[yKey]) },
              { name: "regime", value: p[0].payload.regime, color: REGIME_COLORS[p[0].payload.regime] || T.cyan },
            ])} cursor={{ stroke: T.borderH }} />
            <ReferenceLine y={0} stroke={T.mute} strokeDasharray="3 3" />
            <Scatter data={pts}>
              {pts.map((p, i) => <Cell key={i} fill={p[yKey] >= 0 ? T.green : T.red} fillOpacity={0.55} />)}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  );
}

function CorrelationMatrix({ data }) {
  if (!data) return <div style={{ color: T.mute, fontSize: 11 }}>no data</div>;
  const keys = Object.keys(data);
  const colorOf = v => {
    const a = Math.abs(v);
    if (v > 0) return `rgba(34,211,163,${Math.min(0.85, a)})`;
    if (v < 0) return `rgba(255,93,108,${Math.min(0.85, a)})`;
    return "rgba(255,255,255,0.04)";
  };
  const cells = [];
  cells.push(<span key="hdr-empty" />);
  keys.forEach(k => cells.push(
    <span key={`hdr-${k}`} style={{ fontSize: 9, color: T.mute, textAlign: "center", padding: "4px 0", fontWeight: 700 }}>{k}</span>
  ));
  keys.forEach(rk => {
    cells.push(<span key={`r-${rk}`} style={{ fontSize: 9, color: T.mute, textAlign: "right", padding: "4px 8px", fontWeight: 700 }}>{rk}</span>);
    keys.forEach(ck => {
      const v = data[rk]?.[ck] ?? 0;
      cells.push(
        <div key={`${rk}-${ck}`} style={{
          background: colorOf(v), padding: "10px 4px", textAlign: "center",
          borderRadius: 3, fontSize: 10, fontWeight: 700,
          color: Math.abs(v) > 0.4 ? T.text : T.textD,
        }}>{n(v, 2)}</div>
      );
    });
  });
  return (
    <div style={{ display: "grid", gridTemplateColumns: `90px repeat(${keys.length}, 1fr)`, gap: 2, fontFamily: FONT_MONO }}>
      {cells}
    </div>
  );
}

function Histograms({ scoreHist, hurstHist }) {
  const sData = (scoreHist || []).map((c, i) => ({ bin: String(i), n: c }));
  const hData = (hurstHist || []).map((c, i) => ({ bin: (i / 20).toFixed(2), n: c }));
  return (
    <div style={{ display: "grid", gridTemplateRows: "1fr 1fr", gap: 8, height: 220 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={sData}>
          <XAxis dataKey="bin" tick={CHART_AXIS} stroke={T.mute} />
          <YAxis tick={CHART_AXIS} stroke={T.mute} width={30} />
          <Tooltip content={chartTooltip()} />
          <Bar dataKey="n" fill={T.cyan} name="score" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={hData}>
          <XAxis dataKey="bin" tick={CHART_AXIS} stroke={T.mute} />
          <YAxis tick={CHART_AXIS} stroke={T.mute} width={30} />
          <Tooltip content={chartTooltip()} />
          <Bar dataKey="n" fill={T.violet} name="hurst" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function DrawdownChart({ equity }) {
  const pts = equity || [];
  let peak = -Infinity;
  const dd = pts.map(p => {
    if (p.pnl > peak) peak = p.pnl;
    return { ts: p.ts, drawdown: peak - p.pnl };
  });
  return (
    <div style={{ height: 200 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={dd}>
          <defs>
            <linearGradient id="ddgrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={T.red} stopOpacity={0.5} />
              <stop offset="100%" stopColor={T.red} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
          <XAxis dataKey="ts" tickFormatter={fmtDT} tick={CHART_AXIS} stroke={T.mute} />
          <YAxis tick={CHART_AXIS} stroke={T.mute} width={42} reversed />
          <Tooltip content={chartTooltip()} />
          <Area type="monotone" dataKey="drawdown" stroke={T.red} strokeWidth={1.4} fill="url(#ddgrad)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   DECISION FUNNEL — embudo visual del pipeline de señales
   ════════════════════════════════════════════════════════════════════════ */
function DecisionFunnel({ decisions = [], pipelineCounts }) {
  const N = decisions.length;
  if (N === 0 && !pipelineCounts) return <div style={{ color: T.mute, fontSize: 11 }}>sin datos de pipeline</div>;

  // Derive counts from decisions log
  const blocked = { cooldown: 0, score: 0, hurst: 0, regime: 0, ai: 0, other: 0 };
  for (const d of decisions) {
    if (d.allowed) continue;
    const r = String(d.reason || "").toUpperCase();
    if      (r.includes("COOLDOWN"))                                           blocked.cooldown++;
    else if (r.includes("SCORE") || r.includes("PROFILE") || r.includes("MIN_SCORE")) blocked.score++;
    else if (r.includes("HURST"))                                              blocked.hurst++;
    else if (r.includes("AI") || r.includes("VETO"))                          blocked.ai++;
    else if (r.includes("REGIME") || r.includes("STRATEGY"))                  blocked.regime++;
    else                                                                       blocked.other++;
  }
  const executed = decisions.filter(d => d.allowed).length;

  // Override with backend counters if present
  const pc = pipelineCounts || {};
  const stagesRaw = [
    { label: "SEÑALES EVALUADAS", color: T.cyan,   n: pc.total    || N,
      desc: "Total de setups evaluados por el pipeline" },
    { label: "COOLDOWN OK",       color: T.blue,   n: pc.pass_cd  || Math.max(0, N - blocked.cooldown),
      blocked: pc.block_cd  != null ? pc.block_cd  : blocked.cooldown,
      desc: "Señales que pasan el filtro de cooldown entre trades" },
    { label: "SCORE PASS",        color: T.violet, n: pc.pass_sc  || Math.max(0, N - blocked.cooldown - blocked.score),
      blocked: pc.block_sc  != null ? pc.block_sc  : blocked.score,
      desc: "Señales con score ≥ umbral mínimo configurado" },
    { label: "HURST PASS",        color: T.amber,  n: pc.pass_h   || Math.max(0, N - blocked.cooldown - blocked.score - blocked.hurst),
      blocked: pc.block_h   != null ? pc.block_h   : blocked.hurst,
      desc: "Señales en zona Hurst favorable para la estrategia" },
    { label: "RÉGIMEN OK",        color: T.green,  n: pc.pass_reg || Math.max(0, N - blocked.cooldown - blocked.score - blocked.hurst - blocked.regime),
      blocked: pc.block_reg != null ? pc.block_reg : blocked.regime,
      desc: "Señales cuyo régimen de mercado es compatible con la estrategia" },
    { label: "APROBADO IA",       color: T.cyan,   n: pc.pass_ai  || Math.max(0, N - blocked.cooldown - blocked.score - blocked.hurst - blocked.regime - blocked.ai),
      blocked: pc.block_ai  != null ? pc.block_ai  : blocked.ai,
      desc: "Señales que superan el gate de análisis de la IA" },
    { label: "EJECUTADO",         color: T.green,  n: pc.exec     || executed,
      blocked: 0,
      desc: "Contratos efectivamente abiertos" },
  ].map(s => ({ ...s, n: Math.max(0, s.n ?? 0), blocked: Math.max(0, s.blocked ?? 0) }));

  const maxN = stagesRaw[0].n || 1;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {stagesRaw.map((s, i) => {
        const w = (s.n / maxN) * 100;
        const dropPct = i > 0 && stagesRaw[i - 1].n > 0 ? 1 - s.n / stagesRaw[i - 1].n : 0;
        return (
          <div key={i} style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {/* Stage label */}
            <div style={{ width: 148, textAlign: "right", flexShrink: 0 }}>
              <span style={{ fontSize: 9, color: s.color, fontWeight: 700, letterSpacing: "0.09em" }}>{s.label}</span>
            </div>
            {/* Bar */}
            <div title={s.desc} style={{
              flex: 1, height: 28, background: T.bg, borderRadius: 5,
              overflow: "hidden", position: "relative", cursor: "help",
            }}>
              <div style={{
                width: `${w}%`, height: "100%",
                background: `linear-gradient(90deg, ${s.color}55, ${s.color}28)`,
                border: `1px solid ${s.color}66`, borderRadius: 5,
                transition: "width 500ms ease",
              }} />
              <span style={{
                position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
                fontSize: 12, fontWeight: 700, color: s.color,
              }}>{nC(s.n, 0)}</span>
              <span style={{
                position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)",
                fontSize: 9, color: T.mute,
              }}>{pct(s.n / maxN, 0)}</span>
            </div>
            {/* Drop-off indicator */}
            <div style={{ width: 80, flexShrink: 0, textAlign: "left" }}>
              {s.blocked > 0
                ? <span style={{ fontSize: 10, color: T.red, fontWeight: 700 }}>−{nC(s.blocked, 0)}
                    {dropPct > 0 && <span style={{ fontSize: 8, color: T.mute }}> ({pct(dropPct, 0)})</span>}
                  </span>
                : i === stagesRaw.length - 1
                  ? <span style={{ fontSize: 9, color: T.green, fontWeight: 700 }}>EXEC ✓</span>
                  : <span />
              }
            </div>
          </div>
        );
      })}
      <div style={{ marginTop: 4, padding: "8px 12px", background: `${T.cyan}0a`, borderRadius: 6, border: `1px solid ${T.cyan}22` }}>
        <span style={{ fontSize: 10, color: T.mute }}>
          Tasa de ejecución:{" "}
          <span style={{ color: T.cyan, fontWeight: 700 }}>
            {maxN > 0 ? pct((stagesRaw[stagesRaw.length - 1].n) / maxN, 1) : "–"}
          </span>
          {" "}· Bloqueadas por IA:{" "}
          <span style={{ color: T.violet, fontWeight: 700 }}>{nC(blocked.ai, 0)}</span>
          {" "}· Por score:{" "}
          <span style={{ color: T.amber, fontWeight: 700 }}>{nC(blocked.score, 0)}</span>
          {" "}· Por Hurst:{" "}
          <span style={{ color: T.red, fontWeight: 700 }}>{nC(blocked.hurst, 0)}</span>
        </span>
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 3 — TELEMETRY
   ════════════════════════════════════════════════════════════════════════ */
function TelemetryTab({ data }) {
  const last = data.decisions?.last_by_symbol || {};
  const symbols = Object.keys(last).sort();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Panel title="embudo de decisión · señal → ejecución" glow right={
        <span style={{ fontSize: 9, color: T.mute }}>{data.decisions?.recent?.length || 0} evaluaciones recientes</span>
      }>
        <DecisionFunnel decisions={data.decisions?.recent || []} pipelineCounts={data.decisions?.pipeline_counts} />
      </Panel>
      <Panel title={`per-symbol live state · ${symbols.length} symbols`}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10 }}>
          {symbols.map(s => <SymbolStateCard key={s} sym={s} dec={last[s]} />)}
          {symbols.length === 0 && <div style={{ color: T.mute, fontSize: 11 }}>no recent decisions</div>}
        </div>
      </Panel>
      <Panel title="rejection reasons (recent decisions)">
        <RejectionBars counts={data.decisions?.rejection_count || {}} />
      </Panel>
    </div>
  );
}

function PipelineFlow({ decisions }) {
  const stages = [
    { id: "signal",   label: "SIGNAL",    color: T.cyan },
    { id: "cooldown", label: "COOLDOWN",  color: T.amber },
    { id: "score",    label: "SCORE",     color: T.blue },
    { id: "hurst",    label: "HURST",     color: T.violet },
    { id: "regime",   label: "REGIME",    color: T.green },
    { id: "ai",       label: "AI",        color: T.cyan },
    { id: "exec",     label: "EXECUTION", color: T.green },
  ];
  const blockedAt = { signal: 0, cooldown: 0, score: 0, hurst: 0, regime: 0, ai: 0 };
  let passedAll = 0;
  for (const d of decisions) {
    if (d.allowed) { passedAll++; continue; }
    const r = String(d.reason || "").toUpperCase();
    if (r.includes("COOLDOWN")) blockedAt.cooldown++;
    else if (r.includes("HURST")) blockedAt.hurst++;
    else if (r.includes("SCORE") || r.includes("PROFILE")) blockedAt.score++;
    else if (r.includes("STRATEGY") || r.includes("REGIME") || r.includes("VETO")) blockedAt.regime++;
    else if (r.includes("AI")) blockedAt.ai++;
    else blockedAt.signal++;
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 8 }}>
      {stages.map((s, i) => (
        <div key={s.id} style={{
          position: "relative", background: T.panel2,
          border: `1px solid ${s.color}33`, borderRadius: 8,
          padding: "12px 8px", textAlign: "center",
          boxShadow: `0 0 12px ${s.color}22 inset`,
        }}>
          <div style={{ fontSize: 9, color: s.color, fontWeight: 700, letterSpacing: "0.14em" }}>{s.label}</div>
          {s.id === "exec"
            ? <div style={{ fontSize: 18, color: T.green, fontWeight: 700, marginTop: 4 }}>{passedAll}</div>
            : <div style={{ fontSize: 18, color: T.red, fontWeight: 700, marginTop: 4 }}>−{blockedAt[s.id] || 0}</div>}
          {i < stages.length - 1 && <div style={{ position: "absolute", right: -10, top: "50%", transform: "translateY(-50%)", color: s.color, fontSize: 14 }}>→</div>}
        </div>
      ))}
    </div>
  );
}

function SymbolStateCard({ sym, dec }) {
  const bd = dec?.score_breakdown || {};
  const regColor = REGIME_COLORS[dec?.regime] || T.cyan;
  const scoreColor = (dec?.score || 0) >= 7 ? T.green : (dec?.score || 0) >= 5 ? T.amber : T.red;
  // geo_gate colour: +1.0 = green, 0 = muted, <0 = red scale
  const geoGate = bd.geo_gate != null ? Number(bd.geo_gate) : null;
  const geoGateColor = geoGate == null ? T.mute
    : geoGate >= 1.0 ? T.green
    : geoGate === 0  ? T.mute
    : geoGate >= -1.5 ? T.amber
    : T.red;
  const geoGateLabel = geoGate == null ? "n/a"
    : geoGate >= 1.0 ? `+${geoGate.toFixed(1)} ✓`
    : geoGate === 0  ? "±0.0"
    : `${geoGate.toFixed(1)}`;
  const geoPos = bd.geo_channel_pos != null ? Number(bd.geo_channel_pos) : null;
  const spikeFamilyInterval = bd.spike_family_interval || 0;
  return (
    <div style={{
      background: T.panel2, border: `1px solid ${dec?.allowed ? T.green + "44" : T.border}`,
      borderRadius: 8, padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6,
      boxShadow: dec?.allowed ? `0 0 12px ${T.green}22` : "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: T.text }}>{sym}</span>
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          {spikeFamilyInterval > 0 && (
            <Pill color={sym.includes("BOOM") ? T.green : T.red} size="sm">
              {spikeFamilyInterval}
            </Pill>
          )}
          <Pill color={dec?.allowed ? T.green : T.red} size="sm">{dec?.allowed ? "GO" : "BLOCK"}</Pill>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
        <MicroStat lbl="SCORE" val={n(dec?.score, 2)} color={scoreColor} />
        <MicroStat lbl="HURST" val={bd.hurst != null ? n(bd.hurst, 2) : "–"} color={T.violet} />
        <MicroStat lbl="REGIME" val={dec?.regime || "–"} color={regColor} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
        <MicroStat lbl="SIZE×R" val={bd.regime_size_mult != null ? n(bd.regime_size_mult, 2) : "–"} />
        <MicroStat lbl="SIZE×S" val={bd.score_size_mult != null ? n(bd.score_size_mult, 2) : "–"} />
        <MicroStat lbl="ZONE" val={bd.hurst_zone || "—"} color={String(bd.hurst_zone).includes("C") ? T.green : String(bd.hurst_zone).includes("B") ? T.amber : T.textD} />
      </div>
      {/* Geo gate row — always shown when geo_channel_pos is present */}
      {(geoGate != null || geoPos != null) && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
          <MicroStat lbl="GEO GATE" val={geoGateLabel} color={geoGateColor} />
          <MicroStat lbl="GEO POS" val={geoPos != null ? n(geoPos, 3) : "–"} color={geoPos != null ? (geoPos < -0.5 ? T.green : geoPos < 0 ? T.amber : T.red) : T.mute} />
          <MicroStat lbl="HD" val={bd.hd_bonus != null ? (bd.hd_bonus >= 0 ? `+${n(bd.hd_bonus, 2)}` : n(bd.hd_bonus, 2)) : "–"} color={bd.hd_bonus > 0 ? T.green : bd.hd_bonus < 0 ? T.red : T.mute} />
        </div>
      )}
      {dec?.reason && !dec.allowed && (
        <div style={{
          fontSize: 9, color: T.redD, background: "rgba(255,93,108,0.06)",
          padding: "4px 6px", borderRadius: 4, maxHeight: 32, overflow: "hidden",
          textOverflow: "ellipsis",
        }}>{dec.reason}</div>
      )}
      {dec?.allowed && (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {bd.fvg_active && <Pill color={T.violet} size="sm">FVG</Pill>}
          {bd.spike_entry && <Pill color={T.amber} size="sm">SPIKE</Pill>}
          {bd.mean_rev_mode && <Pill color={T.cyan} size="sm">MR</Pill>}
          {bd.micro_scalp && <Pill color={T.blue} size="sm">SCALP</Pill>}
          {bd.spike_family && <Pill color={T.textD} size="sm">{bd.spike_family}</Pill>}
        </div>
      )}
      <div style={{ fontSize: 9, color: T.mute, textAlign: "right" }}>{fmtT(dec?.ts)}</div>
    </div>
  );
}

function MicroStat({ lbl, val, color = T.text }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
      <span style={{ fontSize: 8, color: T.mute, letterSpacing: "0.12em", fontWeight: 700 }}>{lbl}</span>
      <span style={{ fontSize: 11, color, fontWeight: 700 }}>{val}</span>
    </div>
  );
}

function RejectionBars({ counts }) {
  const rows = Object.entries(counts).map(([k, v]) => ({ key: k, n: v })).sort((a, b) => b.n - a.n);
  if (rows.length === 0) return <div style={{ color: T.mute, fontSize: 11 }}>no rejections</div>;
  return (
    <div style={{ height: Math.max(160, rows.length * 26) }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} layout="vertical">
          <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
          <XAxis type="number" tick={CHART_AXIS} stroke={T.mute} />
          <YAxis dataKey="key" type="category" tick={CHART_AXIS} stroke={T.mute} width={140} />
          <Tooltip content={chartTooltip()} />
          <Bar dataKey="n" fill={T.red} radius={[0, 3, 3, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 4 — DECISIONS
   ════════════════════════════════════════════════════════════════════════ */
function DecisionsTab({ data }) {
  const all = data.decisions?.recent || [];
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const filtered = all
    .filter(d => filter === "all" ? true : filter === "allowed" ? d.allowed : !d.allowed)
    .filter(d => !search || String(d.symbol || "").toLowerCase().includes(search.toLowerCase()) || String(d.reason || "").toLowerCase().includes(search.toLowerCase()))
    .slice().reverse();
  return (
    <Panel title={`decision timeline · ${filtered.length} / ${all.length}`} right={
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="filter…" style={inputStyle} />
        {["all", "allowed", "blocked"].map(f => (
          <button key={f} onClick={() => setFilter(f)} style={{ ...btnStyle(filter === f ? T.cyan : T.mute), padding: "3px 8px", fontSize: 9 }}>{f}</button>
        ))}
      </div>
    }>
      <div style={{ maxHeight: 680, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 }}>
        {filtered.slice(0, 300).map((d, i) => <DecisionRow key={i} d={d} />)}
        {filtered.length === 0 && <div style={{ color: T.mute, fontSize: 11, padding: 12 }}>no decisions match</div>}
      </div>
    </Panel>
  );
}

function DecisionRow({ d }) {
  const [open, setOpen] = useState(false);
  const bd = d.score_breakdown || {};
  const c = d.allowed ? T.green : T.red;
  return (
    <div style={{ borderLeft: `3px solid ${c}`, background: "rgba(255,255,255,0.012)", borderRadius: 5, padding: "6px 10px", fontSize: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }} onClick={() => setOpen(!open)}>
        <span style={{ color: T.mute, width: 70 }}>{fmtT(d.ts)}</span>
        <span style={{ color: T.text, fontWeight: 700, width: 90 }}>{d.symbol || "–"}</span>
        <span style={{ color: tone(d.side === "MULTUP" ? 1 : d.side === "MULTDOWN" ? -1 : 0), width: 70 }}>{d.side || "–"}</span>
        <span style={{ color: T.text, width: 55 }}>{d.score != null ? n(d.score, 2) : "–"}</span>
        <span style={{ color: REGIME_COLORS[d.regime] || T.cyan, width: 90 }}>{d.regime || "–"}</span>
        <Pill color={c} size="sm">{d.allowed ? "GO" : "X"}</Pill>
        <span style={{ flex: 1, color: T.textD, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.reason}</span>
        <span style={{ color: T.mute }}>{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <div style={{ marginTop: 8, padding: "8px 10px", background: T.bg, borderRadius: 4, display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
          {Object.entries(bd).map(([k, v]) => (
            <div key={k} style={{ display: "flex", flexDirection: "column", gap: 1 }}>
              <span style={{ fontSize: 8, color: T.mute, letterSpacing: "0.1em", textTransform: "uppercase" }}>{k}</span>
              <span style={{ fontSize: 10, color: T.text, fontWeight: 700, wordBreak: "break-all" }}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 5 — SYMBOLS
   ════════════════════════════════════════════════════════════════════════ */
function SymbolsTab({ data }) {
  const syms = Object.keys(data.by_symbol || {}).sort();
  const [sel, setSel] = useState(syms[0] || null);
  if (syms.length === 0) return <Panel title="symbols"><div style={{ color: T.mute, fontSize: 11 }}>no symbol data</div></Panel>;
  const s = data.by_symbol[sel] || {};
  const sTrades = (data.closed_contracts || []).filter(c => (c.symbol || c.underlying) === sel);
  const sScatter = (data.scatter || []).filter(p => p.symbol === sel);
  let eq = 0;
  const sEquity = sTrades
    .slice()
    .sort((a, b) => Number(a.closed_at_ts || 0) - Number(b.closed_at_ts || 0))
    .map(c => {
      eq += Number(c.realized_pnl_usdt ?? c.pnl ?? 0);
      const ts = Number(c.closed_at_ts || c.opened_at_ts || 0);
      return { ts: ts > 1e12 ? ts : ts * 1000, pnl: eq };
    });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {syms.map(sy => {
          const a = sy === sel;
          const c = sy.includes("BOOM") ? T.green : sy.includes("CRASH") ? T.red : T.cyan;
          return (
            <button key={sy} onClick={() => setSel(sy)} style={{
              background: a ? `${c}1f` : T.panel,
              color: a ? c : T.textD,
              border: `1px solid ${a ? c + "55" : T.border}`,
              borderRadius: 6, padding: "6px 12px", cursor: "pointer",
              fontFamily: FONT_MONO, fontSize: 10, fontWeight: 700,
              letterSpacing: "0.1em",
              boxShadow: a ? `0 0 12px ${c}22` : "none",
            }}>{sy}</button>
          );
        })}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
        <KPI label="Trades" value={s.n || 0} accent={T.cyan} />
        <KPI label="Winrate" value={pct(s.winrate)} accent={s.winrate >= 0.5 ? T.green : T.red} color={s.winrate >= 0.5 ? T.green : T.red} />
        <KPI label="PF" value={s.profit_factor != null ? n(s.profit_factor, 2) : "–"} accent={T.violet} />
        <KPI label="Expectancy" value={`$${n(s.expectancy, 3)}`} accent={T.green} color={tone(s.expectancy)} />
        <KPI label="PnL" value={`$${n(s.pnl, 2)}`} accent={tone(s.pnl)} color={tone(s.pnl)} />
        <KPI label="Avg hold" value={dur(s.avg_hold_sec)} accent={T.amber} />
        <KPI label="Avg win" value={`$${n(s.avg_win, 2)}`} accent={T.green} />
        <KPI label="Avg loss" value={`$${n(s.avg_loss, 2)}`} accent={T.red} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12 }}>
        <Panel title={`${sel} · equity curve`} glow>
          <div style={{ height: 240 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={sEquity}>
                <defs>
                  <linearGradient id="syg" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={T.cyan} stopOpacity={0.45} />
                    <stop offset="100%" stopColor={T.cyan} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
                <XAxis dataKey="ts" tickFormatter={fmtT} tick={CHART_AXIS} stroke={T.mute} />
                <YAxis tick={CHART_AXIS} stroke={T.mute} width={42} />
                <Tooltip content={chartTooltip()} />
                <ReferenceLine y={0} stroke={T.mute} strokeDasharray="3 3" />
                <Area type="monotone" dataKey="pnl" stroke={T.cyan} strokeWidth={1.5} fill="url(#syg)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Panel>
        <Panel title="long vs short"><SideSplit long={s.long} short={s.short} /></Panel>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.5fr", gap: 12 }}>
        <Panel title="regime mix"><RegimeMix counts={s.regime_counts || {}} /></Panel>
        <Panel title="score vs pnl">
          <div style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart>
                <CartesianGrid stroke={CHART_GRID} strokeDasharray="2 4" />
                <XAxis dataKey="score" type="number" tick={CHART_AXIS} stroke={T.mute} />
                <YAxis dataKey="pnl" type="number" tick={CHART_AXIS} stroke={T.mute} width={42} />
                <Tooltip content={chartTooltip()} cursor={{ stroke: T.borderH }} />
                <ReferenceLine y={0} stroke={T.mute} strokeDasharray="3 3" />
                <Scatter data={sScatter}>
                  {sScatter.map((p, i) => <Cell key={i} fill={p.pnl >= 0 ? T.green : T.red} fillOpacity={0.6} />)}
                </Scatter>
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function SideSplit({ long, short }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <SideRow label="LONG (UP)" b={long || {}} color={T.green} />
      <SideRow label="SHORT (DN)" b={short || {}} color={T.red} />
    </div>
  );
}
function SideRow({ label, b, color }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "8px 10px", background: `${color}0c`, borderRadius: 6, border: `1px solid ${color}22` }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <span style={{ fontSize: 10, color, fontWeight: 700, letterSpacing: "0.12em" }}>{label}</span>
        <span style={{ fontSize: 11, color: T.text, fontWeight: 700 }}>{b.n || 0} trades</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        <MicroStat lbl="WIN%" val={pct(b.winrate)} color={b.winrate >= 0.5 ? T.green : T.red} />
        <MicroStat lbl="PF" val={b.profit_factor != null ? n(b.profit_factor, 2) : "–"} color={T.violet} />
        <MicroStat lbl="PNL" val={`$${n(b.pnl, 2)}`} color={tone(b.pnl)} />
      </div>
    </div>
  );
}

function RegimeMix({ counts }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (total === 0) return <div style={{ color: T.mute, fontSize: 11 }}>no data</div>;
  const rows = Object.entries(counts).map(([k, v]) => ({ key: k, n: v, p: v / total }));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.map(r => (
        <div key={r.key} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10 }}>
            <span style={{ color: REGIME_COLORS[r.key] || T.cyan, fontWeight: 700 }}>{r.key}</span>
            <span style={{ color: T.textD }}>{r.n} · {pct(r.p, 0)}</span>
          </div>
          <MiniBar value={r.p} max={1} color={REGIME_COLORS[r.key] || T.cyan} height={6} />
        </div>
      ))}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 6 — LOGS
   ════════════════════════════════════════════════════════════════════════ */
function LogsTab({ data }) {
  const [search,      setSearch]      = useState("");
  const [autoScroll,  setAutoScroll]  = useState(true);
  const [severity,    setSeverity]    = useState("all");
  const [groupBySym,  setGroupBySym]  = useState(false);
  const all = (data.decisions?.recent || []).slice().reverse();
  const ref = useRef(null);
  useEffect(() => { if (autoScroll && ref.current) ref.current.scrollTop = 0; }, [all.length, autoScroll]);

  const severityOf = d => {
    if (d.allowed) return "info";
    const r = String(d.reason || "").toUpperCase();
    if (r.includes("HARD") || r.includes("ERROR"))                      return "err";
    if (r.includes("AI") || r.includes("VETO"))                         return "ai";
    if (r.includes("HURST"))                                            return "hurst";
    if (r.includes("SCORE") || r.includes("PROFILE"))                   return "score";
    return "warn";
  };

  const SEVER = {
    info:  { color: T.green,  label: "ENTRY" },
    ai:    { color: T.violet, label: "AI-VETO" },
    hurst: { color: T.blue,   label: "HURST" },
    score: { color: T.amber,  label: "SCORE" },
    warn:  { color: T.amber,  label: "BLOCK" },
    err:   { color: T.red,    label: "ERROR" },
  };
  const SEV_FILTER_MAP = { info: "info", ai: "ai", hurst: "hurst", score: "score", warn: "warn", err: "err" };

  const severityFilters = ["all", "info", "ai", "hurst", "score", "warn", "err"];

  const filtered = all
    .filter(d => severity === "all" || severityOf(d) === severity)
    .filter(d => !search || JSON.stringify(d).toLowerCase().includes(search.toLowerCase()));

  const LogRow = ({ d, i }) => {
    const sev = severityOf(d);
    const meta = SEVER[sev] || { color: T.mute, label: "LOG" };
    const isEntry = sev === "info";
    return (
      <div style={{
        display: "flex", gap: 10, padding: "4px 2px",
        borderBottom: `1px solid rgba(255,255,255,0.03)`,
        background: i % 2 === 0 ? "rgba(255,255,255,0.008)" : "transparent",
        borderLeft: `3px solid ${meta.color}55`,
        paddingLeft: 8,
      }}>
        <span style={{ color: T.mute, width: 68, flexShrink: 0, fontSize: 9 }}>{fmtT(d.ts)}</span>
        <span style={{
          flexShrink: 0, fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
          background: `${meta.color}22`, color: meta.color,
          border: `1px solid ${meta.color}44`, letterSpacing: "0.08em",
          width: 66, textAlign: "center",
        }}>{meta.label}</span>
        <span style={{ color: T.cyan, width: 90, flexShrink: 0, fontWeight: 700 }}>{d.symbol || "—"}</span>
        <SideBadge side={d.side} />
        <span style={{ color: T.textD, width: 52, flexShrink: 0, fontSize: 9 }}>
          s={d.score != null ? n(d.score, 1) : "–"}
        </span>
        <span style={{ color: REGIME_COLORS[d.regime] || T.mute, width: 80, flexShrink: 0, fontSize: 9 }}>
          {d.regime || "–"}
        </span>
        <span style={{ color: isEntry ? T.green : T.textD, flex: 1, fontSize: 10 }}>{d.reason || (isEntry ? "ENTRY ALLOWED" : "–")}</span>
      </div>
    );
  };

  const renderGrouped = () => {
    const bySymbol = {};
    for (const d of filtered) {
      const s = d.symbol || "—";
      if (!bySymbol[s]) bySymbol[s] = [];
      bySymbol[s].push(d);
    }
    return Object.entries(bySymbol)
      .sort((a, b) => b[1].length - a[1].length)
      .map(([sym, rows]) => {
        const wins   = rows.filter(d => d.allowed).length;
        const blocks = rows.length - wins;
        const symColor = sym.includes("BOOM") ? T.green : sym.includes("CRASH") ? T.red : T.cyan;
        return (
          <div key={sym} style={{ marginBottom: 10 }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 8, padding: "5px 6px",
              background: `${symColor}0d`, borderRadius: 5,
              borderLeft: `3px solid ${symColor}66`, marginBottom: 4,
            }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: symColor }}>{sym}</span>
              <Pill color={T.green} size="sm">{wins} entries</Pill>
              <Pill color={T.red}   size="sm">{blocks} blocked</Pill>
              <span style={{ fontSize: 9, color: T.mute, marginLeft: 4 }}>{rows.length} total</span>
            </div>
            {rows.slice(0, 50).map((d, i) => <LogRow key={i} d={d} i={i} />)}
            {rows.length > 50 && <div style={{ fontSize: 9, color: T.mute, padding: "4px 8px" }}>+ {rows.length - 50} más…</div>}
          </div>
        );
      });
  };

  return (
    <Panel
      title={`decision log · ${filtered.length}/${all.length}`}
      right={
        <div style={{ display: "flex", gap: 5, alignItems: "center", flexWrap: "wrap" }}>
          <input placeholder="buscar…" value={search} onChange={e => setSearch(e.target.value)} style={inputStyle} />
          {severityFilters.map(s => {
            const meta = SEVER[s] || { color: T.cyan, label: "ALL" };
            return (
              <button key={s} onClick={() => setSeverity(s)} style={{
                ...btnStyle(severity === s ? (meta.color || T.cyan) : T.mute),
                padding: "3px 7px", fontSize: 9,
              }}>{s === "all" ? "ALL" : meta.label}</button>
            );
          })}
          <button onClick={() => setGroupBySym(!groupBySym)} style={{
            ...btnStyle(groupBySym ? T.cyan : T.mute), padding: "3px 8px", fontSize: 9,
          }}>⊞ GRUPO</button>
          <button onClick={() => setAutoScroll(!autoScroll)} style={{
            ...btnStyle(autoScroll ? T.green : T.mute), padding: "3px 8px", fontSize: 9,
          }}>{autoScroll ? "AUTO" : "PIN"}</button>
        </div>
      }
      pad={false}>
      <div ref={ref} style={{
        maxHeight: 740, overflowY: "auto", fontSize: 11, padding: "10px 12px",
        background: "linear-gradient(180deg, #07090f 0%, #0a0c12 100%)",
      }}>
        {groupBySym
          ? renderGrouped()
          : filtered.slice(0, 400).map((d, i) => <LogRow key={i} d={d} i={i} />)
        }
        {filtered.length === 0 && <div style={{ color: T.mute, padding: 16, textAlign: "center" }}>— sin resultados —</div>}
      </div>
    </Panel>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 7 — EXPORT
   ════════════════════════════════════════════════════════════════════════ */
/* ════════════════════════════════════════════════════════════════════════
   SPIKES TAB — real-time spike event explorer
   ════════════════════════════════════════════════════════════════════════ */
function SpikesTab() {
  const [spikes, setSpikes]       = useState([]);
  const [total, setTotal]         = useState(0);
  const [loading, setLoading]     = useState(true);
  const [filterSym, setFilterSym] = useState("");
  const [limit, setLimit]         = useState(300);
  const [sortDesc, setSortDesc]   = useState(true);
  const [liveMode, setLiveMode]   = useState(true);

  const fetchSpikes = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit });
      if (filterSym) params.set("symbol", filterSym);
      const r = await fetch(`/api/spikes?${params}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setSpikes(j.spikes || []);
      setTotal(j.total || 0);
    } catch { /* ignore */ } finally {
      setLoading(false);
    }
  }, [filterSym, limit]);

  useEffect(() => { fetchSpikes(); }, [fetchSpikes]);
  useEffect(() => {
    if (!liveMode) return;
    const id = setInterval(fetchSpikes, 5000);
    return () => clearInterval(id);
  }, [liveMode, fetchSpikes]);

  const sorted = useMemo(() => {
    const s = [...spikes];
    return sortDesc ? s.reverse() : s;
  }, [spikes, sortDesc]);

  // ── Stats by symbol ───────────────────────────────────────────────────
  const bySym = useMemo(() => {
    const m = {};
    for (const e of spikes) {
      const s = e.symbol || "?";
      if (!m[s]) m[s] = { n: 0, entered: 0, missed: 0, blocked: 0, win: 0, loss: 0, openPos: 0, ratioSum: 0, ratioMax: 0 };
      m[s].n++;
      m[s].ratioSum += e.ratio || 0;
      if ((e.ratio || 0) > m[s].ratioMax) m[s].ratioMax = e.ratio;
      if (e.bot_entered === true) {
        m[s].entered++;
        if (e.trade_result === "win")  m[s].win++;
        else if (e.trade_result === "loss") m[s].loss++;
        else if (e.trade_result === "open") m[s].openPos++;
      } else if (e.missed_exit === true) {
        m[s].missed++;
      } else if (e.bot_entered === false) {
        m[s].blocked++;
      }
    }
    return Object.entries(m).sort((a, b) => b[1].n - a[1].n);
  }, [spikes]);

  const entered   = spikes.filter(e => e.bot_entered === true).length;
  const missed    = spikes.filter(e => e.missed_exit === true).length;
  const blocked   = spikes.filter(e => e.bot_entered === false && !e.missed_exit).length;
  const unknown   = spikes.filter(e => e.bot_entered == null).length;
  // WIN/LOSS/OPEN breakdown of entered spikes
  const spikeWins   = spikes.filter(e => e.bot_entered === true && e.trade_result === "win").length;
  const spikeLosses = spikes.filter(e => e.bot_entered === true && e.trade_result === "loss").length;
  const spikeOpen   = spikes.filter(e => e.bot_entered === true && e.trade_result === "open").length;
  const spikeClosedEntered = spikeWins + spikeLosses;
  // Effective capture rate: spikes where bot entered AND won / all known spikes
  const entryDenom = entered + missed + blocked;
  const efectivaDenom = entryDenom; // same base = total meaningful spikes
  const capturaEfectiva = efectivaDenom > 0 ? ((spikeWins / efectivaDenom) * 100).toFixed(1) : "–";
  const winRateEntered  = spikeClosedEntered > 0 ? ((spikeWins / spikeClosedEntered) * 100).toFixed(1) : "–";
  const allSymbols = [...new Set(spikes.map(e => e.symbol).filter(Boolean))].sort();

  const dirColor = d => d === "UP" ? T.green : T.red;

  // ── Timing analysis ─────────────────────────────────────────────────
  const tardeSamples   = spikes.filter(e => e.bot_entered === true && e.post_spike_entry === true);
  const trueSamples    = spikes.filter(e => e.bot_entered === true && !e.post_spike_entry);
  const perdidoSamples = spikes.filter(e => e.missed_exit === true);

  const avgTardeLag  = tardeSamples.length > 0
    ? Math.round(tardeSamples.reduce((s, e) => s + (e.entry_lag_sec || 0), 0) / tardeSamples.length)
    : null;
  const maxTardeLag  = tardeSamples.length > 0
    ? Math.round(Math.max(...tardeSamples.map(e => e.entry_lag_sec || 0)))
    : null;
  const closedTarde  = tardeSamples.filter(e => e.trade_result === "win" || e.trade_result === "loss");
  const tardeWins    = tardeSamples.filter(e => e.trade_result === "win").length;
  const tardeLosses  = tardeSamples.filter(e => e.trade_result === "loss").length;
  const tardeWinRate = closedTarde.length > 0
    ? ((tardeWins / closedTarde.length) * 100).toFixed(0) + "%"
    : "–";

  const avgMissedSec = perdidoSamples.length > 0
    ? Math.round(perdidoSamples.reduce((s, e) => s + (e.missed_exit_sec || 0), 0) / perdidoSamples.length)
    : null;
  const minMissedSec = perdidoSamples.length > 0
    ? Math.min(...perdidoSamples.map(e => e.missed_exit_sec || 0))
    : null;
  const exitReasonBreakdown = perdidoSamples.reduce((acc, e) => {
    const r = e.missed_exit_reason || "?";
    acc[r] = (acc[r] || 0) + 1;
    return acc;
  }, {});

  const closedTrue  = trueSamples.filter(e => e.trade_result === "win" || e.trade_result === "loss");
  const trueWins    = trueSamples.filter(e => e.trade_result === "win").length;
  const trueLosses  = trueSamples.filter(e => e.trade_result === "loss").length;
  const trueWinRate = closedTrue.length > 0
    ? ((trueWins / closedTrue.length) * 100).toFixed(0) + "%"
    : "–";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* ── Top KPIs ─────────────────────────────────────────────────── */}
      {/* FILA 1: contadores brutos */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 8 }}>
        <KPI label="Total Spikes" value={total} accent={T.cyan} />
        <KPI label="En pantalla" value={spikes.length} accent={T.violet} />
        <KPI label="Bot entró" value={entered} color={T.textD} accent={T.textD}
          sub={`${spikeWins}W · ${spikeLosses}L · ${spikeOpen} abiertas`} />
        <KPI label="↩ Salió antes" value={missed} color={T.amber} accent={T.amber} sub="spike_timeout prematura" />
        <KPI label="✗ Bloqueado" value={blocked} color={T.red} accent={T.red} sub="filtros/lockout" />
        <KPI label="Sin dato" value={unknown} color={T.mute} />
      </div>
      {/* FILA 2: métricas reales */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8 }}>
        <KPI label="✓ Spike WIN" value={spikeWins} color={T.green} accent={T.green} big
          sub={`de ${entered} entradas`} />
        <KPI label="✗ Spike LOSS" value={spikeLosses} color={T.red} accent={T.red} big
          sub={`de ${entered} entradas`} />
        <KPI label="Win% (de entradas cerradas)" value={`${winRateEntered}%`}
          color={Number(winRateEntered) >= 50 ? T.green : T.red} accent={T.cyan} big
          sub={`${spikeClosedEntered} spikes cerrados`} />
        <KPI label="Captura efectiva" value={`${capturaEfectiva}%`}
          color={Number(capturaEfectiva) >= 30 ? T.green : Number(capturaEfectiva) >= 15 ? T.amber : T.red}
          accent={T.violet} big
          sub={`WIN / total spikes (${spikeWins}/${efectivaDenom})`} />
      </div>

      {/* ── Controls ─────────────────────────────────────────────────── */}
      <Panel title="Filtros">
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Label>Símbolo</Label>
            <select value={filterSym} onChange={e => setFilterSym(e.target.value)}
              style={{ ...inputStyle, padding: "5px 8px", minWidth: 120 }}>
              <option value="">Todos</option>
              {allSymbols.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Label>Mostrar</Label>
            <select value={limit} onChange={e => setLimit(Number(e.target.value))}
              style={{ ...inputStyle, padding: "5px 8px" }}>
              <option value={50}>Últimos 50</option>
              <option value={100}>Últimos 100</option>
              <option value={300}>Últimos 300</option>
              <option value={1000}>Últimos 1000</option>
              <option value={2000}>Todos (2000)</option>
            </select>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Label>Orden</Label>
            <button onClick={() => setSortDesc(v => !v)} style={{ ...btnStyle(T.cyan), padding: "6px 10px" }}>
              {sortDesc ? "↓ Más reciente" : "↑ Más antiguo"}
            </button>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Label>Live</Label>
            <button onClick={() => setLiveMode(v => !v)} style={{ ...btnStyle(liveMode ? T.green : T.mute), padding: "6px 10px" }}>
              {liveMode ? "◉ LIVE" : "◎ PAUSE"}
            </button>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Label>&nbsp;</Label>
            <button onClick={fetchSpikes} style={{ ...btnStyle(T.amber), padding: "6px 10px" }}>↻ Refresh</button>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <Label>Descargar</Label>
            <a
              href={`/api/spikes?limit=2000${filterSym ? `&symbol=${filterSym}` : ""}&format=json`}
              download="deriv_spike_events.json"
              style={{ ...btnStyle(T.violet), padding: "6px 10px", textDecoration: "none", display: "inline-block" }}
            >↓ JSON</a>
          </div>
        </div>
      </Panel>

      {/* ── Stats por símbolo ─────────────────────────────────────────── */}
      {bySym.length > 0 && (
        <Panel title="Stats por símbolo">
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 10, fontFamily: FONT_MONO }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                  {["Símbolo","Spikes","WIN","LOSS","Open","↩Perdido","✗Bloq.","Win%","Cap.Efect.","Ratio avg","Ratio max"].map(h => (
                    <th key={h} style={{ textAlign: "left", padding: "4px 8px", color: T.mute, fontWeight: 700, letterSpacing: "0.12em", textTransform: "uppercase", fontSize: 9, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bySym.map(([sym, st]) => {
                  const closed = st.win + st.loss;
                  const winPct   = closed > 0 ? ((st.win / closed) * 100).toFixed(0) : "–";
                  const capDenom = st.entered + (st.missed||0) + st.blocked;
                  const capPct   = capDenom > 0 ? ((st.win / capDenom) * 100).toFixed(0) : "–";
                  const winColor = closed > 0 && st.win >= st.loss ? T.green : T.red;
                  return (
                    <tr key={sym}
                      style={{ cursor: "pointer", borderBottom: `1px solid ${T.border}22` }}
                      onClick={() => setFilterSym(filterSym === sym ? "" : sym)}
                      onMouseEnter={e => e.currentTarget.style.background = T.panel2}
                      onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                      <td style={{ padding: "5px 8px" }}><Pill color={sym.includes("BOOM") ? T.green : T.red} size="sm">{sym}</Pill></td>
                      <td style={{ padding: "5px 8px", color: T.cyan, fontWeight: 700 }}>{st.n}</td>
                      <td style={{ padding: "5px 8px", color: T.green, fontWeight: 700 }}>{st.win}</td>
                      <td style={{ padding: "5px 8px", color: T.red, fontWeight: 700 }}>{st.loss}</td>
                      <td style={{ padding: "5px 8px", color: T.cyan }}>{st.openPos||0}</td>
                      <td style={{ padding: "5px 8px", color: T.amber }}>{st.missed||0}</td>
                      <td style={{ padding: "5px 8px", color: T.red }}>{st.blocked}</td>
                      <td style={{ padding: "5px 8px", color: winColor, fontWeight: 700 }}>{winPct}{winPct !== "–" ? "%" : ""}</td>
                      <td style={{ padding: "5px 8px", color: Number(capPct) >= 30 ? T.green : Number(capPct) >= 15 ? T.amber : T.red, fontWeight: 700 }}>{capPct}{capPct !== "–" ? "%" : ""}</td>
                      <td style={{ padding: "5px 8px", color: T.textD }}>{st.n > 0 ? (st.ratioSum / st.n).toFixed(0) : "–"}×</td>
                      <td style={{ padding: "5px 8px", color: T.violet }}>{st.ratioMax.toFixed(0)}×</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {/* ── Análisis de Timing ─────────────────────────────────────── */}
      {(tardeSamples.length > 0 || perdidoSamples.length > 0) && (
        <Panel title="Análisis de Timing — Estrategia">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>

            {/* TARDE: late entries */}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Pill color={T.cyan}>⚡ TARDE</Pill>
                <span style={{ fontSize: 9, color: T.mute }}>Entró &gt;5s después del spike (persiguió el movimiento)</span>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6 }}>
                <KPI label="Total" value={tardeSamples.length} color={T.cyan} />
                <KPI label="Lag prom." value={avgTardeLag != null ? avgTardeLag + "s" : "–"} color={T.cyan} />
                <KPI label="Win rate" value={tardeWinRate} color={closedTarde.length > 0 && tardeWins >= tardeLosses ? T.green : T.red} />
              </div>
              {tardeSamples.length > 0 && (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 9, fontFamily: FONT_MONO }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                        {["Hora Spike", "Símbolo", "Lag entrada", "Resultado"].map(h => (
                          <th key={h} style={{ textAlign: "left", padding: "3px 6px", color: T.mute, fontSize: 8, letterSpacing: "0.1em", fontWeight: 700 }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {tardeSamples.slice(-10).reverse().map((e, i) => (
                        <tr key={i} style={{ borderBottom: `1px solid ${T.border}18` }}>
                          <td style={{ padding: "3px 6px", color: T.mute }}>{fmtDT(e.ts)}</td>
                          <td style={{ padding: "3px 6px" }}><Pill color={(e.symbol||"").includes("BOOM") ? T.green : T.red} size="sm">{e.symbol}</Pill></td>
                          <td style={{ padding: "3px 6px", color: T.cyan, fontWeight: 700 }}>+{e.entry_lag_sec}s</td>
                          <td style={{ padding: "3px 6px" }}>
                            {e.trade_result === "win"  && <Pill color={T.green} size="sm">WIN</Pill>}
                            {e.trade_result === "loss" && <Pill color={T.red}   size="sm">LOSS</Pill>}
                            {e.trade_result === "open" && <Pill color={T.cyan}  size="sm">OPEN</Pill>}
                            {!e.trade_result           && <span style={{ color: T.mute }}>–</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div style={{ fontSize: 9, color: T.amber, background: `${T.amber}0a`, border: `1px solid ${T.amber}33`, borderRadius: 6, padding: "7px 10px", lineHeight: 1.6 }}>
                💡 Si lag &gt;15s con win rate bajo → reducir ventana case-1 de 180s. Si lag 5-15s con buenos resultados → el bot sigue el impulso correctamente.
              </div>
            </div>

            {/* PERDIDO: missed exits */}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Pill color={T.amber}>↩ PERDIDO</Pill>
                <span style={{ fontSize: 9, color: T.mute }}>Trade cerrado antes de que llegara el spike</span>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
                <KPI label="Total" value={perdidoSamples.length} color={T.amber} />
                <KPI label="Salida prom." value={avgMissedSec != null ? avgMissedSec + "s antes" : "–"} color={T.amber} />
              </div>
              {Object.keys(exitReasonBreakdown).length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  <span style={{ fontSize: 8, color: T.mute, letterSpacing: "0.12em", textTransform: "uppercase", fontWeight: 700 }}>Razón de salida</span>
                  {Object.entries(exitReasonBreakdown).sort((a, b) => b[1] - a[1]).map(([reason, count]) => (
                    <div key={reason} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                      <span style={{ fontSize: 9, color: T.textD }}>{reason}</span>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <div style={{ width: Math.max(16, Math.round((count / perdidoSamples.length) * 80)) + "px", height: 5, background: T.amber, borderRadius: 3 }} />
                        <span style={{ fontSize: 9, color: T.amber, fontWeight: 700, minWidth: 16, textAlign: "right" }}>{count}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {perdidoSamples.length > 0 && (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 9, fontFamily: FONT_MONO }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                        {["Hora Spike", "Símbolo", "Salió X antes", "Razón"].map(h => (
                          <th key={h} style={{ textAlign: "left", padding: "3px 6px", color: T.mute, fontSize: 8, letterSpacing: "0.1em", fontWeight: 700 }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {perdidoSamples.slice(-10).reverse().map((e, i) => (
                        <tr key={i} style={{ borderBottom: `1px solid ${T.border}18` }}>
                          <td style={{ padding: "3px 6px", color: T.mute }}>{fmtDT(e.ts)}</td>
                          <td style={{ padding: "3px 6px" }}><Pill color={(e.symbol||"").includes("BOOM") ? T.green : T.amber} size="sm">{e.symbol}</Pill></td>
                          <td style={{ padding: "3px 6px", color: T.amber, fontWeight: 700 }}>-{e.missed_exit_sec}s</td>
                          <td style={{ padding: "3px 6px", color: T.mute, fontSize: 8 }}>{e.missed_exit_reason || "?"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div style={{ fontSize: 9, color: T.amber, background: `${T.amber}0a`, border: `1px solid ${T.amber}33`, borderRadius: 6, padding: "7px 10px", lineHeight: 1.6 }}>
                💡 Si mayoría sale por <code style={{ color: T.amber }}>spike_timeout</code>: considerar aumentar timeout o añadir hold-for-spike si ratio ATR sube bruscamente. Si sale por SL/trailing: re-entrada rápida &lt;30s post-spike es la estrategia correcta.
              </div>
            </div>
          </div>

          {/* Comparativa TRUE vs TARDE */}
          {(closedTrue.length > 0 || closedTarde.length > 0) && (
            <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 12, marginTop: 4 }}>
              <span style={{ fontSize: 9, color: T.mute, letterSpacing: "0.12em", textTransform: "uppercase", fontWeight: 700 }}>Comparativa: Captura Real vs. Entrada Tarde</span>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 8 }}>
                <div style={{ background: `${T.green}08`, border: `1px solid ${T.green}22`, borderRadius: 6, padding: "10px 12px" }}>
                  <div style={{ fontSize: 9, color: T.green, fontWeight: 700, marginBottom: 8, letterSpacing: "0.1em" }}>✓ CAPTURA REAL ({trueSamples.length} spikes / {closedTrue.length} cerrados)</div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
                    <div><div style={{ fontSize: 8, color: T.mute }}>WIN RATE</div><div style={{ fontSize: 14, color: T.green, fontWeight: 700, fontFamily: FONT_MONO }}>{trueWinRate}</div></div>
                    <div><div style={{ fontSize: 8, color: T.mute }}>WINS</div><div style={{ fontSize: 14, color: T.green, fontWeight: 700, fontFamily: FONT_MONO }}>{trueWins}</div></div>
                    <div><div style={{ fontSize: 8, color: T.mute }}>LOSSES</div><div style={{ fontSize: 14, color: T.red, fontWeight: 700, fontFamily: FONT_MONO }}>{trueLosses}</div></div>
                  </div>
                </div>
                <div style={{ background: `${T.cyan}08`, border: `1px solid ${T.cyan}22`, borderRadius: 6, padding: "10px 12px" }}>
                  <div style={{ fontSize: 9, color: T.cyan, fontWeight: 700, marginBottom: 8, letterSpacing: "0.1em" }}>⚡ ENTRADA TARDE ({tardeSamples.length} spikes / {closedTarde.length} cerrados) · lag máx {maxTardeLag != null ? maxTardeLag+"s" : "–"}</div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
                    <div><div style={{ fontSize: 8, color: T.mute }}>WIN RATE</div><div style={{ fontSize: 14, color: T.cyan, fontWeight: 700, fontFamily: FONT_MONO }}>{tardeWinRate}</div></div>
                    <div><div style={{ fontSize: 8, color: T.mute }}>WINS</div><div style={{ fontSize: 14, color: T.green, fontWeight: 700, fontFamily: FONT_MONO }}>{tardeWins}</div></div>
                    <div><div style={{ fontSize: 8, color: T.mute }}>LOSSES</div><div style={{ fontSize: 14, color: T.red, fontWeight: 700, fontFamily: FONT_MONO }}>{tardeLosses}</div></div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </Panel>
      )}

      {/* ── Tabla de eventos ─────────────────────────────────────────── */}
      <Panel title={`Spike events · ${sorted.length} shown`} pad={false}>
        {loading ? (
          <div style={{ padding: 20, textAlign: "center", color: T.mute, fontSize: 11 }}>Cargando spikes…</div>
        ) : sorted.length === 0 ? (
          <div style={{ padding: 20, textAlign: "center", color: T.mute, fontSize: 11 }}>No hay spikes registrados aún.</div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 10, fontFamily: FONT_MONO }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${T.border}`, background: T.panel2 }}>
                  {["Hora","Símbolo","Dir","Jump","ATR","Ratio","Price","Streak","Cooldown","Lockout","Entró","Timing","Trade","Razón"].map(h => (
                    <th key={h} style={{ textAlign: "left", padding: "5px 10px", color: T.mute, fontWeight: 700, letterSpacing: "0.12em", textTransform: "uppercase", fontSize: 9, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sorted.map((e, i) => {
                  const isEntered = e.bot_entered === true;
                  const isMissed  = e.missed_exit === true;
                  const isBlocked = e.bot_entered === false && !isMissed;
                  const rowBg = isEntered ? `${T.green}08` : isMissed ? `${T.amber}08` : isBlocked ? `${T.red}06` : "transparent";
                  return (
                    <tr key={`${e.ts}-${i}`} style={{ background: rowBg, borderBottom: `1px solid ${T.border}18` }}>
                      <td style={{ padding: "4px 10px", color: T.mute, whiteSpace: "nowrap" }}>
                        {fmtDT(e.ts)}
                      </td>
                      <td style={{ padding: "4px 10px" }}>
                        <Pill color={(e.symbol || "").includes("BOOM") ? T.green : T.red} size="sm">{e.symbol || "?"}</Pill>
                      </td>
                      <td style={{ padding: "4px 10px", color: dirColor(e.direction), fontWeight: 700 }}>
                        {e.direction === "UP" ? "▲" : "▼"} {e.direction}
                      </td>
                      <td style={{ padding: "4px 10px", color: dirColor(e.direction), fontWeight: 700 }}>
                        {e.jump != null ? (e.jump > 0 ? "+" : "") + e.jump.toFixed(3) : "–"}
                      </td>
                      <td style={{ padding: "4px 10px", color: T.textD }}>{e.atr != null ? e.atr.toFixed(4) : "–"}</td>
                      <td style={{ padding: "4px 10px" }}>
                        <span style={{ color: (e.ratio || 0) > 500 ? T.red : (e.ratio || 0) > 100 ? T.amber : T.textD, fontWeight: 700 }}>
                          {e.ratio != null ? e.ratio.toFixed(0) + "×" : "–"}
                        </span>
                      </td>
                      <td style={{ padding: "4px 10px", color: T.textD }}>{e.price != null ? e.price.toFixed(2) : "–"}</td>
                      <td style={{ padding: "4px 10px", color: (e.loss_streak || 0) > 0 ? T.amber : T.textD }}>
                        {e.loss_streak != null ? e.loss_streak : "–"}
                      </td>
                      <td style={{ padding: "4px 10px", color: T.textD }}>
                        {e.since_last_trade_s != null ? `${e.since_last_trade_s}s` : "–"}
                      </td>
                      <td style={{ padding: "4px 10px" }}>
                        {e.lockout_active ? <Pill color={T.red} size="sm">SÍ</Pill> : <span style={{ color: T.mute }}>–</span>}
                      </td>
                      <td style={{ padding: "4px 10px" }}>
                        {e.bot_entered === true  && !e.post_spike_entry && <Pill color={T.green} size="sm">✓ SÍ</Pill>}
                        {e.bot_entered === true  && e.post_spike_entry  && <Pill color={T.cyan}  size="sm">⚡ TARDE</Pill>}
                        {e.missed_exit === true  && <Pill color={T.amber} size="sm">↩ PERDIDO</Pill>}
                        {e.bot_entered === false && !e.missed_exit       && <Pill color={T.red}   size="sm">✗ NO</Pill>}
                        {e.bot_entered == null   && <span style={{ color: T.mute }}>–</span>}
                        {e.score != null && <span style={{ color: T.textD, marginLeft: 4, fontSize: 9 }}>s={e.score}</span>}
                      </td>
                      <td style={{ padding: "4px 10px", whiteSpace: "nowrap" }}>
                        {e.post_spike_entry && e.entry_lag_sec != null && (
                          <span style={{ color: T.cyan, fontWeight: 700 }}>+{e.entry_lag_sec}s</span>
                        )}
                        {e.missed_exit === true && e.missed_exit_sec != null && (
                          <span style={{ color: T.amber, fontWeight: 700 }}>-{e.missed_exit_sec}s</span>
                        )}
                        {e.bot_entered === true && !e.post_spike_entry && e.entry_lag_sec != null && (
                          <span style={{ color: T.mute, fontSize: 9 }}>{e.entry_lag_sec > 0 ? '+' : ''}{e.entry_lag_sec}s</span>
                        )}
                      </td>
                      <td style={{ padding: "4px 10px", whiteSpace: "nowrap" }}>
                        {e.trade_result === "win"  && <Pill color={T.green} size="sm">WIN</Pill>}
                        {e.trade_result === "loss" && <Pill color={T.red}   size="sm">LOSS</Pill>}
                        {e.trade_result === "open" && <Pill color={T.cyan}  size="sm">OPEN</Pill>}
                        {e.trade_exit_reason && e.trade_result !== "open" && (
                          <span style={{ color: T.mute, marginLeft: 4, fontSize: 8 }}>{e.trade_exit_reason}</span>
                        )}
                      </td>
                      <td style={{ padding: "4px 10px", color: T.mute, maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {e.block_reason || (e.had_open_pos ? "pos_open" : "–")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function ExportTab({ data }) {
  const [dataset, setDataset] = useState("trades");
  const [format, setFormat] = useState("csv");
  const [filters, setFilters] = useState({});
  const [lastN, setLastN] = useState("");
  const [sinceId, setSinceId] = useState("");
  const [sinceDate, setSinceDate] = useState("");
  const syms = Object.keys(data.by_symbol || {}).sort();
  const regimes = Object.keys(data.by_regime || {});
  const strategies = Object.keys(data.by_strategy || {});

  // Phase quick-presets — click to pre-fill the since_date field
  const PHASES = [
    { label: "Phase 18-19", date: "2026-05-19", desc: "AI gate + MTBS timeout" },
    { label: "Phase 20", date: "2026-05-19", desc: "Spike capture" },
    { label: "Phase 21", date: "2026-05-20", desc: "R_75 FVG off + micro-TP" },
  ];

  const buildUrl = () => {
    const u = new URLSearchParams({ dataset, format });
    for (const [k, v] of Object.entries(filters)) {
      if (v != null && v !== "") u.set(k, v);
    }
    if (lastN && Number(lastN) > 0)  u.set("last_n", lastN);
    if (sinceId && sinceId !== "")   u.set("since_id", sinceId);
    if (sinceDate && sinceDate !== "") u.set("since_date", sinceDate);
    return `/api/deriv-analytics/export?${u.toString()}`;
  };

  const datasets = [
    { id: "trades",    label: "Closed trades",  desc: "all_trades" },
    { id: "open",      label: "Open contracts", desc: "live_positions" },
    { id: "decisions", label: "Decision log",   desc: "telemetry_logs" },
    { id: "rejected",  label: "Rejected only",  desc: "blocked_trades" },
    { id: "equity",    label: "Equity history", desc: "pnl_history" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Panel title="dataset export · csv / json" glow>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <Label>Dataset</Label>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {datasets.map(d => (
                <button key={d.id} onClick={() => setDataset(d.id)} style={{
                  background: dataset === d.id ? T.panel2 : T.panel,
                  border: `1px solid ${dataset === d.id ? T.cyan + "55" : T.border}`,
                  color: dataset === d.id ? T.cyan : T.textD,
                  borderRadius: 6, padding: "8px 12px", cursor: "pointer",
                  textAlign: "left", display: "flex", flexDirection: "column", gap: 2,
                  fontFamily: FONT_MONO,
                }}>
                  <span style={{ fontSize: 11, fontWeight: 700 }}>{d.label}</span>
                  <span style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em" }}>{d.desc}</span>
                </button>
              ))}
            </div>
            <Label>Format</Label>
            <div style={{ display: "flex", gap: 6 }}>
              {["csv", "json"].map(f => (
                <button key={f} onClick={() => setFormat(f)} style={{ ...btnStyle(format === f ? T.green : T.mute), flex: 1, padding: "8px" }}>{f.toUpperCase()}</button>
              ))}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <Label>Filters</Label>
            <FilterField label="Symbol" value={filters.symbol || ""} onChange={v => setFilters({ ...filters, symbol: v })} options={["", ...syms]} />
            <FilterField label="Regime" value={filters.regime || ""} onChange={v => setFilters({ ...filters, regime: v })} options={["", ...regimes]} />
            <FilterField label="Strategy" value={filters.strategy || ""} onChange={v => setFilters({ ...filters, strategy: v })} options={["", ...strategies]} />
            <FilterField label="Side" value={filters.side || ""} onChange={v => setFilters({ ...filters, side: v })} options={["", "MULTUP", "MULTDOWN"]} />
            <FilterField label="Result" value={filters.result || ""} onChange={v => setFilters({ ...filters, result: v })} options={["", "win", "loss"]} />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              <FilterNumber label="Min score" value={filters.min_score} onChange={v => setFilters({ ...filters, min_score: v })} />
              <FilterNumber label="Max score" value={filters.max_score} onChange={v => setFilters({ ...filters, max_score: v })} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              <FilterDate label="From" value={filters.from} onChange={v => setFilters({ ...filters, from: v })} />
              <FilterDate label="To" value={filters.to} onChange={v => setFilters({ ...filters, to: v })} />
            </div>

            {/* ── Phase slice — new ─────────────────────────────────────── */}
            <div style={{ marginTop: 4, padding: "10px 12px", background: T.bg, borderRadius: 6, border: `1px solid ${T.border}` }}>
              <Label>Phase slice (exportar solo desde una fase)</Label>
              <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                {PHASES.map(ph => (
                  <button key={ph.label} onClick={() => { setSinceDate(ph.date); setLastN(""); setSinceId(""); }}
                    title={ph.desc}
                    style={{ ...btnStyle(sinceDate === ph.date && lastN === "" && sinceId === "" ? T.cyan : T.mute), fontSize: 9, padding: "4px 8px" }}>
                    {ph.label}
                  </button>
                ))}
                <button onClick={() => { setSinceDate(""); setLastN("50"); setSinceId(""); }}
                  style={{ ...btnStyle(lastN === "50" ? T.violet : T.mute), fontSize: 9, padding: "4px 8px" }}>last 50</button>
                <button onClick={() => { setSinceDate(""); setLastN("100"); setSinceId(""); }}
                  style={{ ...btnStyle(lastN === "100" ? T.violet : T.mute), fontSize: 9, padding: "4px 8px" }}>last 100</button>
                <button onClick={() => { setSinceDate(""); setLastN(""); setSinceId(""); }}
                  style={{ ...btnStyle(T.mute), fontSize: 9, padding: "4px 8px" }}>todo</button>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6, marginTop: 8 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <Label>last N</Label>
                  <input type="number" value={lastN} onChange={e => { setLastN(e.target.value); setSinceId(""); setSinceDate(""); }}
                    placeholder="ej: 50" style={{ ...inputStyle, padding: "5px 8px" }} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <Label>since contract_id</Label>
                  <input type="number" value={sinceId} onChange={e => { setSinceId(e.target.value); setLastN(""); setSinceDate(""); }}
                    placeholder="ej: 248735491" style={{ ...inputStyle, padding: "5px 8px" }} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <Label>since date</Label>
                  <input type="date" value={sinceDate} onChange={e => { setSinceDate(e.target.value); setLastN(""); setSinceId(""); }}
                    style={{ ...inputStyle, padding: "5px 8px" }} />
                </div>
              </div>
            </div>
          </div>
        </div>

        <div style={{
          marginTop: 16, padding: "12px 14px", background: T.bg, borderRadius: 6,
          border: `1px solid ${T.border}`, display: "flex", alignItems: "center",
          justifyContent: "space-between", gap: 12,
        }}>
          <div style={{ fontSize: 10, color: T.textD, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            <span style={{ color: T.mute }}>GET </span>{buildUrl()}
          </div>
          <a href={buildUrl()} download style={{ ...btnStyle(T.green), textDecoration: "none" }}>↓ DOWNLOAD</a>
        </div>
      </Panel>

      <Panel title="schemas">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 10 }}>
          <SchemaCard title="Closed trades" fields={["symbol", "side", "entry_price", "exit_price", "realized_pnl_usdt", "opened_at_ts", "closed_at_ts", "exit_reason", "score", "score_breakdown.*"]} />
          <SchemaCard title="Decisions" fields={["ts", "symbol", "side", "allowed", "score", "regime", "reason", "score_breakdown.*"]} />
          <SchemaCard title="Equity" fields={["ts", "pnl", "trade_pnl", "symbol"]} />
        </div>
      </Panel>
    </div>
  );
}

function Label({ children }) {
  return <span style={{ fontSize: 9, color: T.mute, letterSpacing: "0.16em", textTransform: "uppercase", fontWeight: 700 }}>{children}</span>;
}
function FilterField({ label, value, onChange, options }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <Label>{label}</Label>
      <select value={value} onChange={e => onChange(e.target.value)} style={{ ...inputStyle, padding: "6px 8px", width: "100%" }}>
        {options.map(o => <option key={o} value={o}>{o || "(any)"}</option>)}
      </select>
    </div>
  );
}
function FilterNumber({ label, value, onChange }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <Label>{label}</Label>
      <input type="number" step="0.1" value={value || ""} onChange={e => onChange(e.target.value)} style={{ ...inputStyle, padding: "6px 8px", width: "100%" }} />
    </div>
  );
}
function FilterDate({ label, value, onChange }) {
  const local = value ? new Date(Number(value)).toISOString().slice(0, 16) : "";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <Label>{label}</Label>
      <input type="datetime-local" value={local} onChange={e => onChange(e.target.value ? new Date(e.target.value).getTime() : "")} style={{ ...inputStyle, padding: "6px 8px", width: "100%" }} />
    </div>
  );
}
function SchemaCard({ title, fields }) {
  return (
    <div style={{ background: T.panel2, border: `1px solid ${T.border}`, borderRadius: 8, padding: "10px 12px" }}>
      <div style={{ fontSize: 10, color: T.cyan, fontWeight: 700, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 6 }}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {fields.map(f => <span key={f} style={{ fontSize: 9, color: T.textD, background: T.bg, padding: "2px 6px", borderRadius: 3 }}>{f}</span>)}
      </div>
    </div>
  );
}
