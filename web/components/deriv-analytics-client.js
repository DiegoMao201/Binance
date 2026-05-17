"use client";
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ResponsiveContainer, AreaChart, Area, LineChart, Line, BarChart, Bar,
  ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine,
  Cell, Legend,
} from "recharts";

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
  { id: "overview",  label: "Overview",  icon: "◉" },
  { id: "analytics", label: "Analytics", icon: "▤" },
  { id: "telemetry", label: "Telemetry", icon: "◈" },
  { id: "decisions", label: "Decisions", icon: "▸" },
  { id: "symbols",   label: "Symbols",   icon: "✦" },
  { id: "logs",      label: "Logs",      icon: "≡" },
  { id: "export",    label: "Export",    icon: "↓" },
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
          {tab === "overview"  && <OverviewTab data={data} />}
          {tab === "analytics" && <AnalyticsTab data={data} />}
          {tab === "telemetry" && <TelemetryTab data={data} />}
          {tab === "decisions" && <DecisionsTab data={data} />}
          {tab === "symbols"   && <SymbolsTab data={data} />}
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
        <button onClick={() => setLive(!live)} style={btnStyle(live ? T.green : T.amber)}>{live ? "● LIVE" : "❚❚ PAUSED"}</button>
        <button onClick={onRefresh} style={btnStyle(T.cyan)}>↻ SYNC</button>
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
   TAB 2 — ANALYTICS
   ════════════════════════════════════════════════════════════════════════ */
function AnalyticsTab({ data }) {
  const [w, setW] = useState(20);
  const rolling = w === 20 ? data.rolling_20 : data.rolling_50;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
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
   TAB 3 — TELEMETRY
   ════════════════════════════════════════════════════════════════════════ */
function TelemetryTab({ data }) {
  const last = data.decisions?.last_by_symbol || {};
  const symbols = Object.keys(last).sort();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Panel title="signal → execution pipeline" glow>
        <PipelineFlow decisions={data.decisions?.recent || []} />
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
  return (
    <div style={{
      background: T.panel2, border: `1px solid ${dec?.allowed ? T.green + "44" : T.border}`,
      borderRadius: 8, padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6,
      boxShadow: dec?.allowed ? `0 0 12px ${T.green}22` : "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: T.text }}>{sym}</span>
        <Pill color={dec?.allowed ? T.green : T.red} size="sm">{dec?.allowed ? "GO" : "BLOCK"}</Pill>
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
  const [search, setSearch] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [severity, setSeverity] = useState("all");
  const all = (data.decisions?.recent || []).slice().reverse();
  const ref = useRef(null);
  useEffect(() => { if (autoScroll && ref.current) ref.current.scrollTop = 0; }, [all.length, autoScroll]);

  const severityOf = d => {
    if (d.allowed) return "info";
    const r = String(d.reason || "").toUpperCase();
    if (r.includes("HARD") || r.includes("VETO") || r.includes("ERROR")) return "err";
    return "warn";
  };
  const tagColor = { info: T.green, warn: T.amber, err: T.red };

  const filtered = all
    .filter(d => severity === "all" || severityOf(d) === severity)
    .filter(d => !search || JSON.stringify(d).toLowerCase().includes(search.toLowerCase()));

  return (
    <Panel
      title={`live decision log · ${filtered.length}/${all.length}`}
      right={
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input placeholder="search…" value={search} onChange={e => setSearch(e.target.value)} style={inputStyle} />
          {["all", "info", "warn", "err"].map(s => (
            <button key={s} onClick={() => setSeverity(s)} style={{ ...btnStyle(severity === s ? (tagColor[s] || T.cyan) : T.mute), padding: "3px 8px", fontSize: 9 }}>{s}</button>
          ))}
          <button onClick={() => setAutoScroll(!autoScroll)} style={{ ...btnStyle(autoScroll ? T.green : T.mute), padding: "3px 8px", fontSize: 9 }}>{autoScroll ? "AUTO" : "PIN"}</button>
        </div>
      }
      pad={false}>
      <div ref={ref} style={{
        maxHeight: 720, overflowY: "auto", fontSize: 11, padding: 12,
        background: "linear-gradient(180deg, #07090f 0%, #0a0c12 100%)",
      }}>
        {filtered.map((d, i) => {
          const sev = severityOf(d);
          const sc = tagColor[sev];
          return (
            <div key={i} style={{ display: "flex", gap: 10, padding: "3px 0", borderBottom: `1px solid ${T.border}` }}>
              <span style={{ color: T.mute, width: 70 }}>{fmtT(d.ts)}</span>
              <Pill color={sc} size="sm">{sev.toUpperCase()}</Pill>
              <span style={{ color: T.cyan, width: 90 }}>{d.symbol || "—"}</span>
              <span style={{ color: tone(d.side === "MULTUP" ? 1 : d.side === "MULTDOWN" ? -1 : 0), width: 70 }}>{d.side || "—"}</span>
              <span style={{ color: T.text, width: 60 }}>s={n(d.score, 2)}</span>
              <span style={{ color: T.textD, flex: 1 }}>{d.reason}</span>
            </div>
          );
        })}
        {filtered.length === 0 && <div style={{ color: T.mute, padding: 16, textAlign: "center" }}>— empty —</div>}
      </div>
    </Panel>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   TAB 7 — EXPORT
   ════════════════════════════════════════════════════════════════════════ */
function ExportTab({ data }) {
  const [dataset, setDataset] = useState("trades");
  const [format, setFormat] = useState("csv");
  const [filters, setFilters] = useState({});
  const syms = Object.keys(data.by_symbol || {}).sort();
  const regimes = Object.keys(data.by_regime || {});
  const strategies = Object.keys(data.by_strategy || {});

  const buildUrl = () => {
    const u = new URLSearchParams({ dataset, format });
    for (const [k, v] of Object.entries(filters)) {
      if (v != null && v !== "") u.set(k, v);
    }
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
