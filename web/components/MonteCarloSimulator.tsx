"use client";

/**
 * MonteCarloSimulator.tsx
 * Interactive Monte Carlo equity projector for OptiFerre-Trader dashboard.
 *
 * Architecture:
 *  - All 10,000+ simulation paths run in a Web Worker (/workers/monte-carlo-worker.js)
 *  - Main thread stays at 60 fps — no blocking during calculation
 *  - Recharts ComposedChart renders percentile bands via stacked Areas + median Line
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from "recharts";
import type { SimParams, SimResults } from "./monteCarloEngine";

// ─── Palette (matches OptiFerre dashboard) ────────────────────────────────────
const G    = "#12d98b";
const R    = "#eb4b61";
const Y    = "#f4b942";
const B    = "#57c1ff";
const BG   = "#080e16";
const CARD = "rgba(10,18,28,0.96)";
const BORD = "#1a2b3c";
const TEXT = "#dce7f5";
const MUTE = "#6b8299";

// ─── Formatters ───────────────────────────────────────────────────────────────
const dollar = (v: number) => `$${v.toFixed(2)}`;
const pctFmt = (v: number) => `${(v * 100).toFixed(1)}%`;
const sign   = (v: number) => `${v >= 0 ? "+" : ""}${pctFmt(v)}`;

// ─── Bot constants (mirrors main_loop.py Phase 9–11) ─────────────────────────
const BOT_SL_PCT    = 0.012;
const BOT_RISK_PCT  = 0.02;
const NOTIONAL_MULT = (BOT_RISK_PCT / BOT_SL_PCT).toFixed(4); // 1.6667

const DEFAULT_ITERATIONS = 10_000;
const DAYS = 30;

// ─── Default simulation parameters ───────────────────────────────────────────
const DEFAULT_PARAMS: SimParams = {
  initialCapital: 40,
  winRate:        0.49,
  tradesPerDay:   3,
  days:           DAYS,
  iterations:     DEFAULT_ITERATIONS,
};

// ─── Chart data shape ─────────────────────────────────────────────────────────
interface ChartPoint {
  day:          number;
  /** Transparent base from 0 → P5 (sets stacking origin at P5) */
  base_p5:      number;
  /** Height of P5→P25 band (pessimistic) */
  band_p5_p25:  number;
  /** Height of P25→P75 band (core confidence interval) */
  band_p25_p75: number;
  /** Height of P75→P95 band (optimistic) */
  band_p75_p95: number;
  /** Absolute median for the non-stacked Line */
  p50:          number;
}

function buildChartData(r: SimResults): ChartPoint[] {
  return r.days.map((d, i) => ({
    day:          d,
    base_p5:      r.p5[i],
    band_p5_p25:  r.p25[i] - r.p5[i],
    band_p25_p75: r.p75[i] - r.p25[i],
    band_p75_p95: r.p95[i] - r.p75[i],
    p50:          r.p50[i],
  }));
}

// ─── Custom Tooltip ───────────────────────────────────────────────────────────
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function MCTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;

  const base = payload.find((p: any) => p.dataKey === "base_p5");
  const b1   = payload.find((p: any) => p.dataKey === "band_p5_p25");
  const b2   = payload.find((p: any) => p.dataKey === "band_p25_p75");
  const b3   = payload.find((p: any) => p.dataKey === "band_p75_p95");
  const med  = payload.find((p: any) => p.dataKey === "p50");

  if (!base || !b1 || !b2 || !b3) return null;

  const p5  = base.value  as number;
  const p25 = p5  + (b1.value as number);
  const p75 = p25 + (b2.value as number);
  const p95 = p75 + (b3.value as number);
  const p50 = med?.value as number | undefined;

  const rows = [
    { label: "P95 optimista",   val: p95,   color: G },
    { label: "P75 cuartil sup", val: p75,   color: `${G}bb` },
    { label: "P50 mediana",     val: p50,   color: G,          bold: true },
    { label: "P25 cuartil inf", val: p25,   color: `${Y}bb` },
    { label: "P5 pesimista",    val: p5,    color: R },
  ];

  return (
    <div style={{
      background: CARD, border: `1px solid ${BORD}`, borderRadius: 10,
      padding: "10px 14px", fontSize: 11, color: TEXT, minWidth: 170,
      boxShadow: "0 4px 20px rgba(0,0,0,0.5)",
    }}>
      <div style={{ color: MUTE, marginBottom: 8, fontWeight: 600, fontSize: 10, textTransform: "uppercase" }}>
        Día {label}
      </div>
      {rows.map(({ label: l, val, color, bold }) => (
        <div key={l} style={{
          display: "flex", justifyContent: "space-between", gap: 14,
          marginBottom: 3, fontWeight: bold ? 700 : 400,
        }}>
          <span style={{ color }}>{l}</span>
          <span style={{ fontFamily: "monospace", color }}>
            {val !== undefined ? dollar(val) : "–"}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─── Stat Card ────────────────────────────────────────────────────────────────
function StatCard({
  label, value, sub, color = TEXT,
}: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <div style={{
      background: CARD, border: `1px solid ${BORD}`, borderRadius: 12,
      padding: "12px 16px", flex: 1, minWidth: 110,
    }}>
      <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 17, fontWeight: 700, fontFamily: "monospace", color }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 10, color: MUTE, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

// ─── Labeled Slider Row ───────────────────────────────────────────────────────
function ParamRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
      <div style={{ fontSize: 11, color: MUTE, width: 145, flexShrink: 0, lineHeight: 1.3 }}>{label}</div>
      {children}
    </div>
  );
}

// ─── Loading Spinner (CSS-based, no extra deps) ───────────────────────────────
function Spinner({ elapsed }: { elapsed: number }) {
  return (
    <>
      <style>{`
        @keyframes mc-rotate { to { transform: rotate(360deg); } }
        @keyframes mc-pulse  { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
        .mc-spin  { animation: mc-rotate 1s linear infinite; display:inline-block; }
        .mc-pulse { animation: mc-pulse 2s ease-in-out infinite; }
      `}</style>
      <div style={{
        background: CARD, border: `1px solid ${BORD}`, borderRadius: 16,
        height: 360, display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center", gap: 14,
      }}>
        <div className="mc-spin" style={{ fontSize: 36, color: G }}>◌</div>
        <div style={{ fontSize: 14, color: TEXT, fontWeight: 600 }}>
          Simulando paths Monte Carlo…
        </div>
        <div style={{ fontSize: 12, color: MUTE, fontFamily: "monospace" }}>
          {elapsed.toFixed(1)} s transcurridos
        </div>
        <div className="mc-pulse" style={{
          fontSize: 10, color: MUTE, maxWidth: 300, textAlign: "center", lineHeight: 1.6,
        }}>
          El cálculo corre en un Web Worker separado.
          <br />El dashboard mantiene 60 fps sin interrupciones.
        </div>
      </div>
    </>
  );
}

// ─── Main Export ──────────────────────────────────────────────────────────────
export interface MonteCarloSimulatorProps {
  /** Pre-fill from live bot equity (from risk.equity_usd) */
  initialEquity?: number;
  /** Pre-fill win rate from portfolio.win_rate_pct (0–100 scale) */
  liveWinRatePct?: number;
}

export default function MonteCarloSimulator({
  initialEquity  = 40,
  liveWinRatePct,
}: MonteCarloSimulatorProps) {
  const liveWR = liveWinRatePct
    ? Math.min(0.60, Math.max(0.40, liveWinRatePct / 100))
    : DEFAULT_PARAMS.winRate;

  const [params, setParams] = useState<SimParams>({
    ...DEFAULT_PARAMS,
    initialCapital: initialEquity,
    winRate:        liveWR,
  });

  const [results, setResults] = useState<SimResults | null>(null);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  const workerRef   = useRef<Worker | null>(null);
  const timerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const startRef    = useRef(0);

  // ── Worker lifecycle ────────────────────────────────────────────────────────
  const runSimulation = useCallback((overrideParams?: SimParams) => {
    const p = overrideParams ?? params;

    // Kill previous worker if still running
    if (workerRef.current) { workerRef.current.terminate(); workerRef.current = null; }
    if (timerRef.current)  { clearInterval(timerRef.current); timerRef.current = null; }

    setRunning(true);
    setResults(null);
    setElapsed(0);
    startRef.current = performance.now();

    // Elapsed counter update every 100 ms
    timerRef.current = setInterval(() => {
      setElapsed(Math.round((performance.now() - startRef.current) / 100) / 10);
    }, 100);

    const worker = new Worker("/workers/monte-carlo-worker.js");
    workerRef.current = worker;

    worker.onmessage = (e: MessageEvent<SimResults>) => {
      setResults(e.data);
      setRunning(false);
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
      worker.terminate();
      workerRef.current = null;
    };

    worker.onerror = (err) => {
      console.error("[MonteCarloWorker] error:", err);
      setRunning(false);
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    };

    worker.postMessage(p);
  }, [params]);

  // Auto-run on first mount
  useEffect(() => {
    runSimulation();
    return () => {
      workerRef.current?.terminate();
      if (timerRef.current) clearInterval(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Chart data ──────────────────────────────────────────────────────────────
  const chartData = results ? buildChartData(results) : null;

  const yMin = chartData
    ? Math.floor(Math.min(...chartData.map(d => d.base_p5)) * 0.96)
    : 0;
  const yMax = chartData
    ? Math.ceil(Math.max(...chartData.map(d => d.base_p5 + d.band_p5_p25 + d.band_p25_p75 + d.band_p75_p95)) * 1.02)
    : 100;

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, color: TEXT, fontFamily: "system-ui, sans-serif" }}>

      {/* ── Control Panel ────────────────────────────────────────────────────── */}
      <div style={{ background: CARD, border: `1px solid ${BORD}`, borderRadius: 16, padding: 20 }}>

        {/* Header row */}
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 18, flexWrap: "wrap", gap: 10 }}>
          <div>
            <div style={{ fontSize: 17, fontWeight: 700 }}>Monte Carlo · Proyección 30 días</div>
            <div style={{ fontSize: 11, color: MUTE, marginTop: 3 }}>
              Hasta {params.iterations.toLocaleString()} iteraciones · Web Worker off-main-thread
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {running && (
              <span style={{ fontSize: 11, color: Y, fontFamily: "monospace" }}>
                ⏳ {elapsed.toFixed(1)}s…
              </span>
            )}
            <button
              onClick={() => runSimulation()}
              disabled={running}
              style={{
                background:  running ? `${G}18` : `${G}22`,
                border:      `1px solid ${running ? G + "44" : G + "77"}`,
                borderRadius: 8,
                color:        running ? `${G}66` : G,
                cursor:       running ? "not-allowed" : "pointer",
                fontSize:     12,
                fontWeight:   700,
                padding:      "7px 18px",
                transition:   "all 0.2s",
                letterSpacing: "0.04em",
              }}
            >
              {running ? "⏳ Simulando…" : "▶ Ejecutar Simulación"}
            </button>
          </div>
        </div>

        {/* Parameter grid */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 40px" }}>

          {/* Capital inicial */}
          <ParamRow label="Capital inicial (USDT)">
            <input
              type="number"
              min={10} max={100000} step={1}
              value={params.initialCapital}
              onChange={e => setParams(p => ({ ...p, initialCapital: Math.max(1, Number(e.target.value)) }))}
              style={{
                background: "#0a1422", border: `1px solid ${BORD}`, borderRadius: 6,
                color: TEXT, fontSize: 13, fontFamily: "monospace",
                padding: "5px 10px", width: 100, outline: "none",
              }}
            />
            <span style={{ fontSize: 11, color: MUTE }}>USDT</span>
          </ParamRow>

          {/* Trades por día */}
          <ParamRow label={`Trades/día · ${params.tradesPerDay}`}>
            <input
              type="range" min={1} max={10} step={1}
              value={params.tradesPerDay}
              onChange={e => setParams(p => ({ ...p, tradesPerDay: Number(e.target.value) }))}
              style={{ width: 120, accentColor: B, cursor: "pointer" }}
            />
            <span style={{ fontSize: 11, color: B, fontWeight: 700, fontFamily: "monospace" }}>
              {params.tradesPerDay}
            </span>
          </ParamRow>

          {/* Win Rate */}
          <ParamRow label={`Win Rate · ${(params.winRate * 100).toFixed(0)}%`}>
            <input
              type="range" min={40} max={60} step={1}
              value={Math.round(params.winRate * 100)}
              onChange={e => setParams(p => ({ ...p, winRate: Number(e.target.value) / 100 }))}
              style={{ width: 120, accentColor: params.winRate >= 0.5 ? G : Y, cursor: "pointer" }}
            />
            <span style={{
              fontSize: 12, fontWeight: 700, fontFamily: "monospace",
              color: params.winRate >= 0.5 ? G : params.winRate >= 0.46 ? Y : R,
            }}>
              {(params.winRate * 100).toFixed(0)}%
            </span>
          </ParamRow>

          {/* Iteraciones */}
          <ParamRow label="Iteraciones">
            <select
              value={params.iterations}
              onChange={e => setParams(p => ({ ...p, iterations: Number(e.target.value) }))}
              style={{
                background: "#0a1422", border: `1px solid ${BORD}`, borderRadius: 6,
                color: TEXT, fontSize: 11, padding: "5px 8px", outline: "none", cursor: "pointer",
              }}
            >
              <option value={1_000}>1,000 (rápido)</option>
              <option value={5_000}>5,000</option>
              <option value={10_000}>10,000 (default)</option>
              <option value={25_000}>25,000 (preciso)</option>
              <option value={50_000}>50,000 (máx)</option>
            </select>
          </ParamRow>
        </div>

        {/* Bot constraints info bar */}
        <div style={{
          marginTop: 14, padding: "8px 12px",
          background: `${B}08`, border: `1px solid ${B}22`,
          borderRadius: 8, fontSize: 10, color: MUTE,
          display: "flex", gap: 16, flexWrap: "wrap",
        }}>
          <span>⚙ SL fijo: <b style={{ color: R }}>-1.20%</b></span>
          <span>⚙ Fee: <b style={{ color: Y }}>0.20%</b> RT por trade</span>
          <span>⚙ Riesgo: <b style={{ color: G }}>2%</b> equity / trade</span>
          <span>⚙ Notional: <b style={{ color: B }}>{NOTIONAL_MULT}× capital</b></span>
          <span>⚙ TP range: <b style={{ color: G }}>+1.80–2.40%</b> (60% wins)</span>
          <span>⚙ Trailing T4: <b style={{ color: G }}>+1.20%</b> (15% wins)</span>
        </div>
      </div>

      {/* ── Loading State ─────────────────────────────────────────────────────── */}
      {running && !results && <Spinner elapsed={elapsed} />}

      {/* ── Results ───────────────────────────────────────────────────────────── */}
      {results && chartData && (
        <>
          {/* ── KPI Summary Cards ──────────────────────────────────────────── */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <StatCard
              label="Capital mediano (P50)"
              value={dollar(results.finalP50)}
              sub={`${sign(results.expectedReturnMedian)} vs inicial`}
              color={results.expectedReturnMedian >= 0 ? G : R}
            />
            <StatCard
              label="P95 Optimista"
              value={dollar(results.finalP95)}
              sub={sign((results.finalP95 - params.initialCapital) / params.initialCapital)}
              color={G}
            />
            <StatCard
              label="P5 Pesimista"
              value={dollar(results.finalP5)}
              sub={sign((results.finalP5 - params.initialCapital) / params.initialCapital)}
              color={results.finalP5 >= params.initialCapital ? G : R}
            />
            <StatCard
              label="Prob. ganancia"
              value={pctFmt(results.probProfit)}
              sub={`${Math.round(results.probProfit * results.iterationsRun).toLocaleString()} / ${results.iterationsRun.toLocaleString()}`}
              color={results.probProfit >= 0.5 ? G : R}
            />
            <StatCard
              label="Max DD mediano"
              value={pctFmt(results.maxDrawdownMedian)}
              sub="Camino P50"
              color={results.maxDrawdownMedian > 0.07 ? R : results.maxDrawdownMedian > 0.04 ? Y : G}
            />
          </div>

          {/* ── Chart ─────────────────────────────────────────────────────────── */}
          <div style={{
            background: CARD, border: `1px solid ${BORD}`,
            borderRadius: 16, padding: "20px 8px 14px 8px",
          }}>
            {/* Legend */}
            <div style={{
              fontSize: 10, color: MUTE, paddingLeft: 16, marginBottom: 10,
              display: "flex", gap: 16, flexWrap: "wrap",
            }}>
              <span style={{ color: G, fontWeight: 700 }}>— Mediana P50</span>
              <span style={{ color: `${G}99` }}>▓ Banda P25–P75 (confianza)</span>
              <span style={{ color: `${G}55` }}>░ Banda P5–P95 (extremos)</span>
              <span style={{ color: `${R}88` }}>░ Zona pesimista P5–P25</span>
              <span style={{ color: MUTE }}>- - Capital inicial</span>
            </div>

            <ResponsiveContainer width="100%" height={340}>
              <ComposedChart data={chartData} margin={{ top: 8, right: 24, bottom: 16, left: 54 }}>
                <defs>
                  {/* Band gradients */}
                  <linearGradient id="gradOptimist" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%"   stopColor={G} stopOpacity={0.10} />
                    <stop offset="100%" stopColor={G} stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="gradCore" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%"   stopColor={G} stopOpacity={0.32} />
                    <stop offset="100%" stopColor={G} stopOpacity={0.10} />
                  </linearGradient>
                  <linearGradient id="gradPessimist" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%"   stopColor={R} stopOpacity={0.22} />
                    <stop offset="100%" stopColor={R} stopOpacity={0.04} />
                  </linearGradient>
                </defs>

                <CartesianGrid strokeDasharray="2 5" stroke={BORD} />

                <XAxis
                  dataKey="day"
                  tick={{ fill: MUTE, fontSize: 10 }}
                  tickLine={false}
                  axisLine={{ stroke: BORD }}
                  label={{ value: "Días", position: "insideBottom", offset: -4, fill: MUTE, fontSize: 10 }}
                />
                <YAxis
                  domain={[yMin, yMax]}
                  tick={{ fill: MUTE, fontSize: 10 }}
                  tickLine={false}
                  axisLine={{ stroke: BORD }}
                  tickFormatter={v => `$${(v as number).toFixed(0)}`}
                />

                <Tooltip content={MCTooltip} />

                {/* ── Stacked areas create the percentile bands ── */}

                {/* Transparent base: 0 → P5 (invisible, anchors the stack) */}
                <Area
                  type="monotone" dataKey="base_p5" stackId="mc"
                  fill="none" stroke="none" isAnimationActive={false}
                />

                {/* P5 → P25: pessimistic red band */}
                <Area
                  type="monotone" dataKey="band_p5_p25" stackId="mc"
                  fill="url(#gradPessimist)" stroke="none"
                  isAnimationActive={false}
                />

                {/* P25 → P75: core confidence band (green) */}
                <Area
                  type="monotone" dataKey="band_p25_p75" stackId="mc"
                  fill="url(#gradCore)" stroke="none"
                  isAnimationActive={false}
                />

                {/* P75 → P95: optimistic band (lighter green) */}
                <Area
                  type="monotone" dataKey="band_p75_p95" stackId="mc"
                  fill="url(#gradOptimist)" stroke="none"
                  isAnimationActive={false}
                />

                {/* ── Median line (absolute, not stacked) ── */}
                <Line
                  type="monotone" dataKey="p50"
                  stroke={G} strokeWidth={2.5}
                  dot={false} isAnimationActive={false}
                  activeDot={{ r: 5, fill: G, stroke: BG, strokeWidth: 2 }}
                />

                {/* ── Initial capital reference ── */}
                <ReferenceLine
                  y={params.initialCapital}
                  stroke={MUTE}
                  strokeDasharray="6 4"
                  label={{
                    value: `$${params.initialCapital.toFixed(0)}`,
                    position: "insideTopRight",
                    fill: MUTE, fontSize: 9,
                  }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          {/* ── Percentile Distribution Table ────────────────────────────────── */}
          <div style={{
            background: CARD, border: `1px solid ${BORD}`,
            borderRadius: 16, padding: 20,
          }}>
            <div style={{
              fontSize: 10, color: MUTE, textTransform: "uppercase",
              letterSpacing: "0.1em", marginBottom: 14, fontWeight: 600,
            }}>
              Distribución final · Día {params.days} · {results.iterationsRun.toLocaleString()} paths
            </div>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              {([
                { label: "P5 Pesimista",  val: results.finalP5,  color: R },
                { label: "P25",           val: results.finalP25, color: Y },
                { label: "P50 Mediana",   val: results.finalP50, color: G },
                { label: "P75",           val: results.finalP75, color: G },
                { label: "P95 Optimista", val: results.finalP95, color: `${G}cc` },
              ] as const).map(({ label, val, color }) => (
                <div key={label} style={{ flex: 1, minWidth: 100, textAlign: "center" }}>
                  <div style={{ fontSize: 9, color: MUTE, marginBottom: 4 }}>{label}</div>
                  <div style={{ fontSize: 15, fontWeight: 700, fontFamily: "monospace", color }}>
                    {dollar(val)}
                  </div>
                  <div style={{
                    fontSize: 10, marginTop: 3,
                    color: val >= params.initialCapital ? G : R,
                  }}>
                    {sign((val - params.initialCapital) / params.initialCapital)}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* ── Assumptions footer ───────────────────────────────────────────── */}
          <div style={{
            fontSize: 10, color: MUTE, lineHeight: 1.7,
            borderTop: `1px solid ${BORD}`, paddingTop: 10,
          }}>
            <b style={{ color: `${TEXT}88` }}>Supuestos del modelo · </b>
            Fee {(0.002 * 100).toFixed(2)}% RT obligatorio ·{" "}
            SL -{(0.012 * 100).toFixed(2)}% · Riesgo 2% equity/trade ·{" "}
            Notional ≈ {NOTIONAL_MULT}× capital · Distribución ganancias: T1(+0.20%, 5%) +
            T2(+0.40%, 10%) + T3(+0.80%, 10%) + T4(+1.20%, 15%) + TP(+1.80–2.40%, 60%) ·{" "}
            OptiFerre-Trader Phase 9–11 · {results.iterationsRun.toLocaleString()} iteraciones · PRNG Mulberry32
          </div>
        </>
      )}
    </div>
  );
}
