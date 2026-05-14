"use client";
/**
 * components/client/EquityCurveChart.tsx
 *
 * Renders the investor's equity curve as a Recharts AreaChart.
 * Props are plain serialisable types (no Decimal, no Date) — safe for
 * Next.js Server → Client Component boundary.
 *
 * Design: dark premium — neon-green gradient fill, minimal axes.
 */

import { useMemo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  type TooltipProps,
} from "recharts";
import type { EquityPoint } from "./client-types";

// ─── Palette ──────────────────────────────────────────────────────────────────
const GREEN  = "#12d98b";
const MUTE   = "#6b8299";
const BORD   = "#1a2b3c";
const TEXT   = "#dce7f5";
const CARD   = "#0a1018";

// ─── Recharts data shape ──────────────────────────────────────────────────────
type ChartDatum = {
  date: string;
  value: number; // number for Recharts; converted from string prop
};

// ─── Custom Tooltip ───────────────────────────────────────────────────────────
function CustomTooltip({ active, payload, label }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const value = payload[0]?.value ?? 0;
  return (
    <div
      style={{
        background: CARD,
        border: `1px solid ${BORD}`,
        borderRadius: 10,
        padding: "10px 14px",
        fontSize: 12,
        color: TEXT,
        boxShadow: "0 4px 20px rgba(0,0,0,0.5)",
      }}
    >
      <p style={{ color: MUTE, marginBottom: 4 }}>{label}</p>
      <p style={{ color: GREEN, fontWeight: 700, fontFamily: "monospace", fontSize: 14 }}>
        ${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
      </p>
    </div>
  );
}

// ─── Component ────────────────────────────────────────────────────────────────
interface Props {
  points: EquityPoint[];
}

export function EquityCurveChart({ points }: Props) {
  const data: ChartDatum[] = useMemo(
    () =>
      points.map((p) => ({
        date:  p.date,
        value: parseFloat(p.balance),
      })),
    [points]
  );

  // Y-axis domain with 2% padding
  const values = data.map((d) => d.value);
  const minVal  = Math.min(...values);
  const maxVal  = Math.max(...values);
  const padding = (maxVal - minVal) * 0.08 || maxVal * 0.05;
  const yMin    = Math.max(0, minVal - padding);
  const yMax    = maxVal + padding;

  // X-axis: show every Nth label to avoid crowding
  const tickStep = Math.max(1, Math.ceil(data.length / 8));

  function fmtY(v: number): string {
    if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
    if (v >= 1_000)     return `$${(v / 1_000).toFixed(1)}K`;
    return `$${v.toFixed(0)}`;
  }

  return (
    <div style={{ width: "100%", overflowX: "auto" }}>
      <div style={{ minWidth: 320, height: 240 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%"   stopColor={GREEN} stopOpacity={0.35} />
                <stop offset="100%" stopColor={GREEN} stopOpacity={0.02} />
              </linearGradient>
            </defs>

            <CartesianGrid
              strokeDasharray="3 3"
              stroke={BORD}
              vertical={false}
            />

            <XAxis
              dataKey="date"
              tick={{ fill: MUTE, fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              interval={tickStep - 1}
              tickFormatter={(v: string) => {
                // "2026-05-14" → "May 14"
                const d = new Date(v + "T12:00:00Z");
                return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
              }}
            />

            <YAxis
              domain={[yMin, yMax]}
              tick={{ fill: MUTE, fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={fmtY}
              width={56}
            />

            <Tooltip content={<CustomTooltip />} cursor={{ stroke: BORD, strokeWidth: 1 }} />

            <Area
              type="monotone"
              dataKey="value"
              stroke={GREEN}
              strokeWidth={2}
              fill="url(#equityGradient)"
              dot={data.length <= 30 ? { r: 3, fill: GREEN, strokeWidth: 0 } : false}
              activeDot={{ r: 5, fill: GREEN, stroke: CARD, strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
