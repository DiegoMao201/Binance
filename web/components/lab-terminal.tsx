"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ── Palette (matches operator-terminal.js) ──────────────────────────────────
const BG   = "#080e16";
const BG2  = "#0d1622";
const CARD = "rgba(10,18,28,0.96)";
const BORD = "#1a2b3c";
const TEXT = "#dce7f5";
const MUTE = "#6b8299";
const G    = "#12d98b";  // green  — CANDIDATA / positive
const R    = "#eb4b61";  // red    — DESCARTADA / negative
const Y    = "#f4b942";  // yellow — INSUFICIENTE / warning
const B    = "#57c1ff";  // blue   — info

// Categorical palette for strategy lines (never reciclada)
const CAT_COLORS = ["#57c1ff","#f4b942","#a78bfa","#ff7c5c","#39d0ff","#ffb3e6","#7bc67e","#ffd166"];

const STATUS_COLOR: Record<string,string> = {
  CANDIDATA:    G,
  INSUFICIENTE: Y,
  DESCARTADA:   R,
  CONTROL:      MUTE,
};

const STATUS_ICON: Record<string,string> = {
  CANDIDATA:    "●",
  INSUFICIENTE: "◐",
  DESCARTADA:   "○",
  CONTROL:      "—",
};

// ── Types ───────────────────────────────────────────────────────────────────
interface MarkoutCurve {
  "1s":  number|null; "5s":  number|null; "30s": number|null; "60s": number|null;
  "5m":  number|null; "15m": number|null; "30m": number|null; "60m": number|null;
}

interface Strategy {
  name: string; display_name: string; mode: string;
  signals: number; fills: number; n_ef: number;
  neto_bps: number|null; ci95_inf: number|null; vs_ctrl: number|null;
  status: string;
  first_signal: string|null; last_fill: string|null;
  markout_curve: MarkoutCurve;
  utc_hours: number[];       // 24 values
  markout_hist: number[];    // bps samples
}

interface SystemHealth {
  db_ok: boolean;
  streams: { name: string; status: string; gap_count: number; seconds_ago: number }[];
  total_fills_24h: number;
  last_fill_ts: string|null;
}

interface LabData {
  generated_at: string;
  lab_started_at: string|null;
  strategies: Strategy[];
  system: SystemHealth;
}

// ── Helpers ─────────────────────────────────────────────────────────────────
const n2 = (v: number|null|undefined, d=2) =>
  v == null ? "—" : v.toFixed(d);

function elapsed(from: string|null): string {
  if (!from) return "—";
  const s = (Date.now() - new Date(from).getTime()) / 1000;
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s/60).toFixed(0)}m`;
  if (s < 86400) return `${(s/3600).toFixed(1)}h`;
  return `${(s/86400).toFixed(1)}d`;
}

function labProgress(from: string|null, totalDays=7): number {
  if (!from) return 0;
  const s = (Date.now() - new Date(from).getTime()) / 1000;
  return Math.min(1, s / (totalDays * 86400));
}

function uniqueHours(hours: number[]): number {
  return hours.filter(v => v > 0).length;
}

// ── Primitives ───────────────────────────────────────────────────────────────

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{
      background: CARD, border: `1px solid ${BORD}`, borderRadius: 10,
      padding: "16px 20px", ...style
    }}>
      {children}
    </div>
  );
}

function CardTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, fontWeight: 700, color: MUTE, letterSpacing: "0.1em",
      textTransform: "uppercase", marginBottom: 12 }}>
      {children}
    </div>
  );
}

function BarH({ value, max, color=G, height=6 }: { value: number; max: number; color?: string; height?: number }) {
  const p = max > 0 ? Math.min(100, value/max*100) : 0;
  return (
    <div style={{ background: "rgba(255,255,255,0.06)", borderRadius: 4, height, overflow:"hidden" }}>
      <div style={{ background: color, width:`${p}%`, height:"100%", transition:"width 0.4s ease" }} />
    </div>
  );
}

// ── Block 1 — Lab status header ───────────────────────────────────────────────
function BlockStatus({ data }: { data: LabData }) {
  const { strategies, lab_started_at, system } = data;
  const progress = labProgress(lab_started_at);
  const totalSig = strategies.reduce((a, s) => a + s.signals, 0);
  const totalFills = strategies.reduce((a, s) => a + s.fills, 0);
  const totalNef = strategies.reduce((a, s) => a + s.n_ef, 0);
  const maxHours = Math.max(...strategies.map(s => uniqueHours(s.utc_hours)));

  return (
    <Card>
      <div style={{ display:"flex", flexWrap:"wrap", gap:24, alignItems:"flex-start" }}>
        {/* Progress */}
        <div style={{ minWidth:180, flex:"1 1 180px" }}>
          <div style={{ fontSize:11, color:MUTE, marginBottom:4 }}>TIEMPO CORRIENDO</div>
          <div style={{ fontSize:20, fontWeight:700, color:TEXT, marginBottom:6 }}>
            {elapsed(lab_started_at)} <span style={{fontSize:13, color:MUTE}}>/ 7 días</span>
          </div>
          <BarH value={progress*100} max={100} color={B} />
          <div style={{ fontSize:10, color:MUTE, marginTop:3 }}>{(progress*100).toFixed(1)}%</div>
        </div>
        {/* Stats */}
        {[
          { label:"ESTRATEGIAS", value: strategies.filter(s=>s.status!=="CONTROL").length + " activas" },
          { label:"SEÑALES", value: totalSig.toLocaleString() },
          { label:"FILLS", value: totalFills.toLocaleString() },
          { label:"n_ef TOTAL", value: totalNef.toLocaleString() },
          { label:"COBERTURA TEMPORAL", value: `${maxHours} / 24 h` },
          { label:"ÚLTIMO FILL", value: elapsed(system.last_fill_ts) + " atrás" },
        ].map(({ label, value }) => (
          <div key={label} style={{ minWidth:110 }}>
            <div style={{ fontSize:11, color:MUTE, marginBottom:2 }}>{label}</div>
            <div style={{ fontSize:18, fontWeight:700, color:TEXT }}>{value}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ── Block 2 — Ranking table ───────────────────────────────────────────────────
function BlockTable({ strategies }: { strategies: Strategy[] }) {
  const controls  = strategies.filter(s => s.status === "CONTROL");
  const rest      = strategies.filter(s => s.status !== "CONTROL")
    .sort((a, b) => (b.neto_bps ?? -9999) - (a.neto_bps ?? -9999));

  const Row = ({ s }: { s: Strategy }) => {
    const col = STATUS_COLOR[s.status];
    return (
      <tr style={{ borderBottom:`1px solid ${BORD}` }}>
        <td style={{ padding:"10px 12px", color:TEXT, fontSize:13, fontFamily:"monospace" }}>
          {s.display_name}
        </td>
        <td style={{ padding:"10px 8px", color:MUTE, fontSize:12 }}>{s.mode}</td>
        <td style={{ padding:"10px 8px", color:TEXT, fontSize:13, textAlign:"right", fontVariantNumeric:"tabular-nums" }}>
          {s.signals.toLocaleString()}
        </td>
        <td style={{ padding:"10px 8px", color:TEXT, fontSize:13, textAlign:"right", fontVariantNumeric:"tabular-nums" }}>
          {s.fills.toLocaleString()}
        </td>
        <td style={{ padding:"10px 8px", color:TEXT, fontSize:13, textAlign:"right", fontVariantNumeric:"tabular-nums" }}>
          {s.n_ef.toLocaleString()}
        </td>
        <td style={{ padding:"10px 8px", textAlign:"right", fontVariantNumeric:"tabular-nums",
          color: s.neto_bps == null ? MUTE : s.neto_bps >= 0 ? G : R, fontSize:13 }}>
          {n2(s.neto_bps)}
        </td>
        <td style={{ padding:"10px 8px", textAlign:"right", fontVariantNumeric:"tabular-nums",
          color: s.ci95_inf == null ? MUTE : s.ci95_inf > 0 ? G : MUTE, fontSize:13 }}>
          {n2(s.ci95_inf)}
        </td>
        <td style={{ padding:"10px 8px", textAlign:"right", fontVariantNumeric:"tabular-nums",
          color: s.vs_ctrl == null ? MUTE : s.vs_ctrl >= 10 ? G : s.vs_ctrl >= 0 ? TEXT : R, fontSize:13 }}>
          {s.vs_ctrl == null ? "—" : (s.vs_ctrl >= 0 ? "+" : "") + n2(s.vs_ctrl)}
        </td>
        <td style={{ padding:"10px 12px", fontSize:13 }}>
          <span style={{ color: col, fontWeight:600 }}>
            {STATUS_ICON[s.status]} {s.status}
          </span>
        </td>
      </tr>
    );
  };

  const cols = ["ESTRATEGIA","MODO","SEÑALES","FILLS","n_ef","NETO bps","CI95 inf","vs CTRL","ESTADO"];

  return (
    <Card style={{ padding:0 }}>
      <div style={{ overflowX:"auto" }}>
        <table style={{ width:"100%", borderCollapse:"collapse" }}>
          <thead>
            <tr style={{ borderBottom:`1px solid ${BORD}` }}>
              {cols.map(c => (
                <th key={c} style={{ padding:"10px 12px", textAlign: c==="ESTRATEGIA"||c==="ESTADO" ? "left" : "right",
                  fontSize:10, fontWeight:700, color:MUTE, letterSpacing:"0.08em", whiteSpace:"nowrap" }}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {controls.map(s => <Row key={s.name} s={s} />)}
            <tr><td colSpan={9} style={{ borderBottom:`1px solid ${BORD}`, padding:0 }} /></tr>
            {rest.map(s => <Row key={s.name} s={s} />)}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ── Block 3 — Markout curves (SVG line chart) ────────────────────────────────
const HORIZONS = ["1s","5s","30s","60s","5m","15m","30m","60m"] as const;

function MarkoutSvg({ strategies }: { strategies: Strategy[] }) {
  const W = 680, H = 220, PAD = { top:16, right:16, bottom:36, left:52 };
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;

  // Collect all non-null values
  const allVals = strategies.flatMap(s => HORIZONS.map(h => s.markout_curve[h]).filter(v => v != null)) as number[];
  if (allVals.length === 0) {
    return (
      <div style={{ height:220, display:"flex", alignItems:"center", justifyContent:"center", color:MUTE, fontSize:12 }}>
        Esperando datos de markout…
      </div>
    );
  }

  const yMin = Math.min(...allVals, 0) * 1.15;
  const yMax = Math.max(...allVals, 0) * 1.15 || 5;

  const xScale = (i: number) => PAD.left + (i / (HORIZONS.length - 1)) * innerW;
  const yScale = (v: number) => PAD.top + innerH - ((v - yMin) / (yMax - yMin)) * innerH;
  const y0 = yScale(0);

  // Assign colors: controls get index determined by their position in CAT_COLORS
  const colorMap: Record<string,string> = {};
  let ci = 0;
  for (const s of strategies) {
    colorMap[s.name] = CAT_COLORS[ci++ % CAT_COLORS.length];
  }

  const makePoints = (s: Strategy) =>
    HORIZONS.map((h, i) => {
      const v = s.markout_curve[h];
      if (v == null) return null;
      return `${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`;
    }).filter(Boolean).join(" ");

  const yTicks = 5;
  const yStep = (yMax - yMin) / yTicks;

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ overflow:"visible" }}>
      {/* Grid */}
      {Array.from({length: yTicks+1}, (_, i) => {
        const v = yMin + i * yStep;
        const y = yScale(v);
        return (
          <g key={i}>
            <line x1={PAD.left} y1={y} x2={W-PAD.right} y2={y}
              stroke={BORD} strokeWidth={1} />
            <text x={PAD.left-6} y={y+4} textAnchor="end" fill={MUTE}
              fontSize={10}>{v.toFixed(1)}</text>
          </g>
        );
      })}
      {/* Zero line */}
      <line x1={PAD.left} y1={y0} x2={W-PAD.right} y2={y0}
        stroke={TEXT} strokeWidth={1.5} strokeDasharray="4 2" opacity={0.4} />
      {/* X axis labels */}
      {HORIZONS.map((h, i) => (
        <text key={h} x={xScale(i)} y={H-PAD.bottom+18}
          textAnchor="middle" fill={MUTE} fontSize={10}>{h}</text>
      ))}
      {/* Strategy lines */}
      {strategies.map(s => {
        const pts = makePoints(s);
        if (!pts) return null;
        const isCtrl = s.status === "CONTROL";
        const col = colorMap[s.name];
        return (
          <polyline key={s.name}
            points={pts}
            fill="none"
            stroke={col}
            strokeWidth={isCtrl ? 1.5 : 2}
            strokeDasharray={isCtrl ? "6 3" : undefined}
            opacity={0.9}
          />
        );
      })}
    </svg>
  );
}

function BlockMarkoutCurves({ strategies }: { strategies: Strategy[] }) {
  const [showAll, setShowAll] = useState(false);
  const colorMap: Record<string,string> = {};
  let ci = 0;
  for (const s of strategies) colorMap[s.name] = CAT_COLORS[ci++ % CAT_COLORS.length];

  const visible = showAll
    ? strategies
    : [...strategies.filter(s=>s.status==="CONTROL"), ...strategies.filter(s=>s.status==="CANDIDATA")];

  return (
    <Card>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:12 }}>
        <CardTitle>CURVA DE MARKOUT — Eje X: horizonte · Eje Y: bps</CardTitle>
        <button onClick={() => setShowAll(v=>!v)} style={{
          background: "transparent", border:`1px solid ${BORD}`, color:TEXT,
          padding:"4px 10px", borderRadius:6, cursor:"pointer", fontSize:11
        }}>
          {showAll ? "Solo candidatas" : "Todas"}
        </button>
      </div>
      <MarkoutSvg strategies={visible} />
      {/* Legend below chart */}
      <div style={{ display:"flex", flexWrap:"wrap", gap:"6px 16px", marginTop:10 }}>
        {strategies.map(s => (
          <div key={s.name} style={{ display:"flex", alignItems:"center", gap:5, fontSize:11, color:TEXT }}>
            <svg width={20} height={8}>
              <line x1={0} y1={4} x2={20} y2={4}
                stroke={colorMap[s.name]} strokeWidth={2}
                strokeDasharray={s.status==="CONTROL" ? "4 2" : undefined} />
            </svg>
            <span style={{ color: s.status==="CONTROL" ? MUTE : TEXT }}>{s.display_name}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ── Block 4 — Progress toward significance ───────────────────────────────────
function BlockProgress({ strategies }: { strategies: Strategy[] }) {
  const lab = strategies
    .filter(s => s.status !== "CONTROL")
    .sort((a, b) => b.n_ef - a.n_ef);

  return (
    <Card>
      <CardTitle>PROGRESO HACIA SIGNIFICANCIA (n_ef ≥ 30)</CardTitle>
      <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
        {lab.map(s => {
          const pct = Math.min(1, s.n_ef / 30);
          const col = s.n_ef >= 30 ? G : s.n_ef >= 15 ? Y : MUTE;
          return (
            <div key={s.name} style={{ display:"grid", gridTemplateColumns:"220px 1fr 48px", alignItems:"center", gap:10 }}>
              <div style={{ fontSize:11, color:TEXT, fontFamily:"monospace", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
                {s.display_name}
              </div>
              <BarH value={s.n_ef} max={30} color={col} height={8} />
              <div style={{ fontSize:11, color:col, textAlign:"right", fontVariantNumeric:"tabular-nums" }}>
                {s.n_ef}/30
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ── Block 5 — Markout histogram ───────────────────────────────────────────────
function mkHist(vals: number[], bins=20): { lo:number; hi:number; count:number }[] {
  if (vals.length === 0) return [];
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  if (min === max) return [{ lo:min, hi:max, count:vals.length }];
  const step = (max - min) / bins;
  const counts = Array.from({length:bins}, (_, i) => ({
    lo: min + i * step,
    hi: min + (i + 1) * step,
    count: 0,
  }));
  for (const v of vals) {
    const i = Math.min(bins-1, Math.floor((v - min) / step));
    counts[i].count++;
  }
  return counts;
}

function BlockHistogram({ strategies }: { strategies: Strategy[] }) {
  const [selected, setSelected] = useState(strategies[0]?.name ?? "");
  const s = strategies.find(x => x.name === selected) ?? strategies[0];
  if (!s) return null;

  const vals = s.markout_hist;
  const mean = vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : null;
  const sorted = [...vals].sort((a,b)=>a-b);
  const median = sorted.length ? sorted[Math.floor(sorted.length/2)] : null;
  const hist = mkHist(vals);
  const maxCount = Math.max(...hist.map(b => b.count), 1);

  return (
    <Card>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:12 }}>
        <CardTitle>DISTRIBUCIÓN DE MARKOUT (60s bps)</CardTitle>
        <select
          value={selected}
          onChange={e => setSelected(e.target.value)}
          style={{ background:BG2, border:`1px solid ${BORD}`, color:TEXT,
            padding:"4px 8px", borderRadius:6, fontSize:11, cursor:"pointer" }}
        >
          {strategies.map(s => (
            <option key={s.name} value={s.name}>{s.display_name}</option>
          ))}
        </select>
      </div>
      {vals.length === 0 ? (
        <div style={{ color:MUTE, fontSize:12 }}>Sin fills todavía</div>
      ) : (
        <>
          <div style={{ display:"flex", alignItems:"flex-end", gap:2, height:120, marginBottom:8 }}>
            {hist.map((b, i) => {
              const h = maxCount > 0 ? (b.count / maxCount) * 100 : 0;
              return (
                <div key={i} title={`${b.lo.toFixed(1)} → ${b.hi.toFixed(1)} bps: ${b.count}`}
                  style={{ flex:1, height:`${h}%`, background:b.lo < 0 ? R+"88" : G+"88",
                    borderRadius:"2px 2px 0 0", minHeight: b.count > 0 ? 2 : 0 }}
                />
              );
            })}
          </div>
          <div style={{ display:"flex", gap:16, fontSize:11 }}>
            <span style={{ color:TEXT }}>Media: <b style={{color:B}}>{n2(mean)}</b> bps</span>
            <span style={{ color:TEXT }}>Mediana: <b style={{color:Y}}>{n2(median)}</b> bps</span>
            <span style={{ color:MUTE }}>n = {vals.length}</span>
            {mean !== null && median !== null && median > mean + 1 &&
              <span style={{ color:R }}>⚠ Mediana &gt; Media → cola izquierda activa (selección adversa)</span>
            }
          </div>
        </>
      )}
    </Card>
  );
}

// ── Block 6 — Temporal coverage ───────────────────────────────────────────────
function BlockCoverage({ strategies }: { strategies: Strategy[] }) {
  const [selected, setSelected] = useState(strategies[0]?.name ?? "");
  const s = strategies.find(x => x.name === selected) ?? strategies[0];
  if (!s) return null;
  const hours = s.utc_hours;
  const maxH = Math.max(...hours, 1);

  return (
    <Card>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:12 }}>
        <CardTitle>COBERTURA TEMPORAL (fills por hora UTC)</CardTitle>
        <select
          value={selected}
          onChange={e => setSelected(e.target.value)}
          style={{ background:BG2, border:`1px solid ${BORD}`, color:TEXT,
            padding:"4px 8px", borderRadius:6, fontSize:11, cursor:"pointer" }}
        >
          {strategies.map(s => (
            <option key={s.name} value={s.name}>{s.display_name}</option>
          ))}
        </select>
      </div>
      <div style={{ display:"flex", alignItems:"flex-end", gap:2, height:80 }}>
        {hours.map((cnt, h) => {
          const height = maxH > 0 ? (cnt / maxH) * 100 : 0;
          return (
            <div key={h} style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center" }}>
              <div title={`${h}:00 UTC — ${cnt} fills`}
                style={{ width:"100%", height:`${height}%`, background: cnt > 0 ? B+"99" : "transparent",
                  borderRadius:"2px 2px 0 0", minHeight: cnt > 0 ? 2 : 0 }}
              />
            </div>
          );
        })}
      </div>
      <div style={{ display:"flex", justifyContent:"space-between", fontSize:9, color:MUTE, marginTop:4 }}>
        {[0,6,12,18,23].map(h => <span key={h}>{h}h</span>)}
      </div>
      <div style={{ fontSize:11, color:MUTE, marginTop:6 }}>
        Horas cubiertas: <span style={{color:TEXT}}>{uniqueHours(hours)} / 24</span>
        {uniqueHours(hours) < 24 &&
          <span style={{color:Y}}> — muestra insuficiente para concluir</span>}
      </div>
    </Card>
  );
}

// ── Block 7 — System health ───────────────────────────────────────────────────
function BlockHealth({ system }: { system: SystemHealth }) {
  const { db_ok, streams, last_fill_ts, total_fills_24h } = system;
  return (
    <Card>
      <CardTitle>SALUD DEL SISTEMA</CardTitle>
      <div style={{ display:"flex", flexWrap:"wrap", gap:20 }}>
        <div>
          <div style={{ fontSize:11, color:MUTE, marginBottom:4 }}>BASE DE DATOS</div>
          <span style={{ fontSize:14, color: db_ok ? G : R, fontWeight:600 }}>
            {db_ok ? "● OK" : "● CAÍDA"}
          </span>
        </div>
        <div>
          <div style={{ fontSize:11, color:MUTE, marginBottom:4 }}>ÚLTIMO FILL</div>
          <span style={{ fontSize:14, color:TEXT }}>{elapsed(last_fill_ts)} atrás</span>
        </div>
        <div>
          <div style={{ fontSize:11, color:MUTE, marginBottom:4 }}>FILLS 24h</div>
          <span style={{ fontSize:14, color:TEXT }}>{total_fills_24h.toLocaleString()}</span>
        </div>
        <div>
          <div style={{ fontSize:11, color:MUTE, marginBottom:8 }}>STREAMS</div>
          <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
            {streams.length === 0
              ? <span style={{ color:MUTE, fontSize:12 }}>Sin datos de salud</span>
              : streams.map(st => {
                  const ok = st.status === "connected" && st.seconds_ago < 60;
                  const col = ok ? G : st.seconds_ago < 300 ? Y : R;
                  return (
                    <div key={st.name} style={{ display:"flex", gap:10, alignItems:"center", fontSize:12 }}>
                      <span style={{ color:col }}>●</span>
                      <span style={{ color:TEXT, fontFamily:"monospace", minWidth:160 }}>{st.name}</span>
                      <span style={{ color:MUTE }}>{st.seconds_ago}s</span>
                      <span style={{ color:MUTE }}>{st.gap_count} gaps</span>
                    </div>
                  );
                })
            }
          </div>
        </div>
        <div style={{ fontSize:11, color:MUTE }}>
          <div style={{ marginBottom:4 }}>DISCO / RAM</div>
          <span>Ver Coolify UI</span>
        </div>
      </div>
    </Card>
  );
}

// ── Root component ─────────────────────────────────────────────────────────────
export default function LabTerminal() {
  const [data, setData] = useState<LabData|null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string|null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date|null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval>|null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await window.fetch("/api/lab");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setLastRefresh(new Date());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    timerRef.current = setInterval(refresh, 30_000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [refresh]);

  const style: React.CSSProperties = {
    minHeight:"100vh", background:BG,
    color:TEXT, fontFamily:"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace",
    padding:"20px 24px",
  };

  if (loading) return (
    <div style={style}>
      <div style={{ color:MUTE, fontSize:13, paddingTop:60, textAlign:"center" }}>
        Conectando con la base de datos del laboratorio…
      </div>
    </div>
  );

  if (error && !data) return (
    <div style={style}>
      <div style={{ color:R, fontSize:13, paddingTop:60, textAlign:"center" }}>
        Error: {error}<br/>
        <span style={{ color:MUTE, fontSize:11 }}>
          ¿Está BINANCE_DB_URL configurado en el contenedor?
        </span>
      </div>
    </div>
  );

  if (!data) return null;

  return (
    <div style={style}>
      {/* Header */}
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:20 }}>
        <div>
          <div style={{ fontSize:18, fontWeight:700, color:TEXT }}>Laboratorio de Señales</div>
          <div style={{ fontSize:11, color:MUTE }}>OptiFerre Binance · {data.strategies.length} estrategias en paralelo</div>
        </div>
        <div style={{ textAlign:"right" }}>
          <div style={{ fontSize:11, color:MUTE }}>
            Última actualización: {lastRefresh?.toLocaleTimeString("es-ES")} UTC
          </div>
          {error && <div style={{ fontSize:11, color:Y }}>⚠ {error}</div>}
          <div style={{ fontSize:10, color:MUTE }}>Auto-refresco cada 30s</div>
        </div>
      </div>

      <div style={{ display:"flex", flexDirection:"column", gap:16 }}>
        <BlockStatus data={data} />
        <BlockTable strategies={data.strategies} />
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16 }}>
          <BlockProgress strategies={data.strategies.filter(s=>s.status!=="CONTROL")} />
          <BlockCoverage strategies={data.strategies} />
        </div>
        <BlockMarkoutCurves strategies={data.strategies} />
        <BlockHistogram strategies={data.strategies} />
        <BlockHealth system={data.system} />
      </div>
    </div>
  );
}
