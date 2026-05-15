"use client";
/**
 * web/components/dashboard-client.tsx
 *
 * OptiFerre Terminal v2.0 — Investor Dashboard (premium)
 *
 * Strict-TypeScript, render-safe, GPU-accelerated cyberpunk dashboard.
 *
 * Stack:
 *   • React 19 + Next.js 16 (Client Component)
 *   • framer-motion (variants declared OUTSIDE components)
 *   • recharts (ResponsiveContainer + AreaChart)
 *   • lucide-react (icons)
 *
 * Design language:
 *   Bloomberg Terminal × Cyberpunk — neon glow, glassmorphism, monospace.
 *
 * Project styling note:
 *   This project does NOT have TailwindCSS configured (verified). Each
 *   component therefore ships with explicit `style={{...}}` blocks using
 *   the EXACT colour tokens demanded by the design spec. `className`
 *   strings are still emitted alongside so that when Tailwind is added,
 *   the components light up automatically — zero refactor required.
 */

import {
  Component,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { motion, type Variants } from "framer-motion";
import {
  Activity,
  Radio,
  Shield,
  TrendingUp,
  Percent,
  Wallet,
  CircleDollarSign,
  Brain,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

/* ───────────────────────────────────────────────────────────────────────────
 * 1. PUBLIC TYPE CONTRACTS  (mandatory — must not be altered)
 * ─────────────────────────────────────────────────────────────────────────── */

export interface LiveRadarProps {
  ticker: string;
  entryTime: string;
  aiConfidence: number;
  isActive: boolean;
}

export interface MetricCardProps {
  title: string;
  value: string;
  type: "currency" | "percentage";
  glowColor: "emerald" | "purple" | "cyan" | "amber";
}

/** One point on the equity curve. `value` is a Prisma.Decimal stringified. */
export interface EquityPoint {
  date: string;
  value: string;
}

export interface DashboardClientProps {
  /** Live radar payload for the active position (or idle state). */
  radar: LiveRadarProps;
  /** AI sentiment score in [0..1]. Out-of-range values are clamped safely. */
  sentimentScore: number;
  /** Headline metrics. */
  metrics: {
    availableBalance: string;
    netReturnPct:     string;
    accumulatedProfit: string;
    paidFees:         string;
  };
  /** Historical equity curve. May be empty — component handles gracefully. */
  equityCurve: EquityPoint[];
}

/* ───────────────────────────────────────────────────────────────────────────
 * 2. COLOUR TOKENS  (Tailwind-equivalent hex map)
 * ─────────────────────────────────────────────────────────────────────────── */

const GLOW = {
  emerald: { stroke: "#10b981", fill: "rgba(16,185,129,0.15)", glow: "rgba(16,185,129,0.45)", text: "#34d399" },
  purple:  { stroke: "#a855f7", fill: "rgba(168,85,247,0.15)", glow: "rgba(168,85,247,0.45)", text: "#c084fc" },
  cyan:    { stroke: "#06b6d4", fill: "rgba(6,182,212,0.15)",  glow: "rgba(6,182,212,0.45)",  text: "#22d3ee" },
  amber:   { stroke: "#f59e0b", fill: "rgba(245,158,11,0.15)", glow: "rgba(245,158,11,0.45)", text: "#fbbf24" },
} as const;

const PALETTE = {
  bgRoot:     "#04070c",
  bgPanel:    "rgba(10,15,22,0.72)",
  bgPanelAlt: "rgba(13,20,30,0.72)",
  border:     "rgba(63,87,114,0.28)",
  borderHot:  "rgba(99,135,178,0.45)",
  text:       "#dce7f5",
  textMute:   "#6b8299",
  green:      "#10b981",
  red:        "#ef4444",
  cyan:       "#22d3ee",
} as const;

/* ───────────────────────────────────────────────────────────────────────────
 * 3. SAFE NUMERIC PARSING  (defensive — never throws, never returns NaN)
 * ─────────────────────────────────────────────────────────────────────────── */

/**
 * Parses a numeric input that may be a string, number, null, undefined, or
 * malformed garbage. Always returns a finite number; falls back to `fallback`
 * (default 0) when parsing fails. Strips currency symbols, thousand separators
 * and surrounding whitespace.
 */
function safeNumber(raw: unknown, fallback = 0): number {
  if (raw === null || raw === undefined) return fallback;
  if (typeof raw === "number") return Number.isFinite(raw) ? raw : fallback;
  if (typeof raw !== "string") return fallback;
  const cleaned = raw.replace(/[$,\s%]/g, "").trim();
  if (cleaned === "") return fallback;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : fallback;
}

/** Clamp `n` into [min, max]. Safe against NaN. */
function clamp(n: number, min: number, max: number): number {
  if (!Number.isFinite(n)) return min;
  return Math.min(max, Math.max(min, n));
}

const CURRENCY_FMT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const PERCENT_FMT = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const SIGNED_PERCENT_FMT = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "always",
});

function formatValue(value: string, type: MetricCardProps["type"]): string {
  const n = safeNumber(value);
  if (type === "currency") return CURRENCY_FMT.format(n);
  // Spec values for percentages are EXPRESSED as already-percent units (e.g. "12.34" → 12.34%).
  // Divide by 100 before passing to NumberFormat.
  return PERCENT_FMT.format(n / 100);
}

/* ───────────────────────────────────────────────────────────────────────────
 * 4. ANIMATION VARIANTS  (declared OUTSIDE components — perf requirement)
 * ─────────────────────────────────────────────────────────────────────────── */

const containerVariants: Variants = {
  hidden:  {},
  visible: { transition: { staggerChildren: 0.07, delayChildren: 0.05 } },
};

const cardVariants: Variants = {
  hidden:  { opacity: 0, y: 14, scale: 0.985 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: 0.45, ease: [0.22, 1, 0.36, 1] },
  },
};

const radarSweepVariants: Variants = {
  spinning: {
    rotate: 360,
    transition: { duration: 4, ease: "linear", repeat: Infinity },
  },
};

const blipPulseVariants: Variants = {
  pulse: {
    scale: [1, 2.2, 1],
    opacity: [0.85, 0, 0.85],
    transition: { duration: 1.6, ease: "easeInOut", repeat: Infinity },
  },
};

const idleScanVariants: Variants = {
  scan: {
    opacity: [0.35, 1, 0.35],
    transition: { duration: 2.2, ease: "easeInOut", repeat: Infinity },
  },
};

/* ───────────────────────────────────────────────────────────────────────────
 * 5. LIVE RADAR
 * ─────────────────────────────────────────────────────────────────────────── */

const RADAR_SIZE = 220;

export const LiveRadar = memo(function LiveRadar({
  ticker,
  entryTime,
  aiConfidence,
  isActive,
}: LiveRadarProps) {
  const safeConfidence = clamp(safeNumber(aiConfidence) * 100, 0, 100);
  const safeTicker     = (ticker ?? "").toString().toUpperCase().trim() || "—";
  const safeEntry      = (entryTime ?? "").toString().trim();

  const stateLabel = isActive ? "OPERACIÓN EN CURSO" : "SCANNING MARKET";
  const stateColor = isActive ? GLOW.emerald : GLOW.cyan;

  return (
    <motion.div
      variants={cardVariants}
      style={panelStyle({ accent: stateColor.glow, padding: "20px 22px" })}
      className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md"
      role="region"
      aria-label="Live operations radar"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <Activity size={14} color={stateColor.text} />
        <span
          style={{
            color: stateColor.text,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textShadow: `0 0 8px ${stateColor.glow}`,
          }}
          className="font-mono"
        >
          {stateLabel}
        </span>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: `${RADAR_SIZE}px 1fr`,
          gap: 24,
          alignItems: "center",
        }}
      >
        {/* ── SVG Radar (GPU-accelerated rotation) ── */}
        <div
          style={{
            position: "relative",
            width: RADAR_SIZE,
            height: RADAR_SIZE,
            willChange: "transform",
          }}
          className="will-change-transform"
        >
          {/* Static background rings + axes */}
          <svg
            viewBox="0 0 200 200"
            width={RADAR_SIZE}
            height={RADAR_SIZE}
            style={{ position: "absolute", inset: 0 }}
            aria-hidden="true"
          >
            <defs>
              <radialGradient id="radarBgGrad" cx="50%" cy="50%" r="50%">
                <stop offset="0%"   stopColor={stateColor.glow} stopOpacity="0.18" />
                <stop offset="80%"  stopColor="transparent" />
              </radialGradient>
              <linearGradient id="radarSweepGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%"   stopColor={stateColor.stroke} stopOpacity="0" />
                <stop offset="100%" stopColor={stateColor.stroke} stopOpacity="0.85" />
              </linearGradient>
            </defs>

            {/* Background glow */}
            <circle cx="100" cy="100" r="98" fill="url(#radarBgGrad)" />

            {/* Concentric rings */}
            {[30, 55, 80, 95].map((r) => (
              <circle
                key={r}
                cx="100"
                cy="100"
                r={r}
                fill="none"
                stroke={stateColor.stroke}
                strokeOpacity={0.18}
                strokeWidth={0.6}
              />
            ))}

            {/* Cross axes */}
            <line x1="100" y1="2"  x2="100" y2="198" stroke={stateColor.stroke} strokeOpacity="0.18" strokeWidth="0.6" />
            <line x1="2"   y1="100" x2="198" y2="100" stroke={stateColor.stroke} strokeOpacity="0.18" strokeWidth="0.6" />

            {/* Center dot */}
            <circle cx="100" cy="100" r="2.2" fill={stateColor.text} />
          </svg>

          {/* Rotating sweep — GPU-accelerated */}
          <motion.svg
            viewBox="0 0 200 200"
            width={RADAR_SIZE}
            height={RADAR_SIZE}
            style={{
              position: "absolute",
              inset: 0,
              willChange: "transform",
              transformOrigin: "50% 50%",
            }}
            className="will-change-transform"
            variants={radarSweepVariants}
            animate="spinning"
            aria-hidden="true"
          >
            <path
              d="M100,100 L100,5 A95,95 0 0,1 195,100 Z"
              fill="url(#radarSweepGrad)"
              opacity={0.55}
            />
            <line
              x1="100"
              y1="100"
              x2="100"
              y2="5"
              stroke={stateColor.text}
              strokeWidth="1.4"
              strokeLinecap="round"
              opacity={0.9}
            />
          </motion.svg>

          {/* Blip — only when an active position exists */}
          {isActive && (
            <motion.div
              style={{
                position: "absolute",
                top: "32%",
                left: "62%",
                width: 12,
                height: 12,
                borderRadius: "50%",
                background: GLOW.emerald.stroke,
                boxShadow: `0 0 10px ${GLOW.emerald.glow}, 0 0 20px ${GLOW.emerald.glow}`,
                willChange: "transform, opacity",
              }}
              className="will-change-transform"
              variants={blipPulseVariants}
              animate="pulse"
              aria-hidden="true"
            />
          )}
        </div>

        {/* ── Right column: metadata ── */}
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <RadarMetaRow
            icon={<Radio size={12} color={stateColor.text} />}
            label="TICKER"
            value={isActive ? safeTicker : "—"}
            valueColor={stateColor.text}
            mono
          />
          <RadarMetaRow
            icon={<Shield size={12} color={PALETTE.textMute} />}
            label="ENTRY TIME"
            value={isActive && safeEntry ? safeEntry : "—"}
            valueColor={PALETTE.text}
            mono
          />
          <RadarMetaRow
            icon={<Brain size={12} color={GLOW.purple.text} />}
            label="AI CONFIDENCE"
            value={`${safeConfidence.toFixed(1)}%`}
            valueColor={GLOW.purple.text}
            mono
          />

          {!isActive && (
            <motion.span
              variants={idleScanVariants}
              animate="scan"
              style={{
                color: PALETTE.textMute,
                fontSize: 10,
                letterSpacing: "0.2em",
                fontWeight: 600,
              }}
              className="font-mono"
            >
              ▸ ANALYZING ORDER FLOW…
            </motion.span>
          )}
        </div>
      </div>
    </motion.div>
  );
});

const RadarMetaRow = memo(function RadarMetaRow({
  icon,
  label,
  value,
  valueColor,
  mono,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  valueColor: string;
  mono?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <span style={{ display: "inline-flex", width: 16 }}>{icon}</span>
      <span
        style={{
          color: PALETTE.textMute,
          fontSize: 9,
          fontWeight: 700,
          letterSpacing: "0.16em",
          minWidth: 90,
        }}
        className="font-mono"
      >
        {label}
      </span>
      <span
        style={{
          color: valueColor,
          fontSize: 14,
          fontWeight: 700,
          fontFamily: mono ? "ui-monospace, Menlo, monospace" : undefined,
        }}
        className={mono ? "font-mono" : undefined}
      >
        {value}
      </span>
    </div>
  );
});

/* ───────────────────────────────────────────────────────────────────────────
 * 6. METRIC CARD  (neon glassmorphism)
 * ─────────────────────────────────────────────────────────────────────────── */

export const MetricCard = memo(function MetricCard({
  title,
  value,
  type,
  glowColor,
}: MetricCardProps) {
  const glow      = GLOW[glowColor];
  const formatted = useMemo(() => formatValue(value, type), [value, type]);
  const Icon      = useMemo(() => pickIconForGlow(glowColor), [glowColor]);

  return (
    <motion.div
      variants={cardVariants}
      whileHover={{ y: -3, transition: { duration: 0.18, ease: "easeOut" } }}
      style={panelStyle({
        accent: glow.glow,
        padding: "20px 22px",
        accentBorderLeft: glow.stroke,
      })}
      className={`rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md border-l-2 border-l-${glowColor}-400`}
      role="figure"
      aria-label={`${title}: ${formatted}`}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
        <span
          style={{
            color: PALETTE.textMute,
            fontSize: 9.5,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textTransform: "uppercase",
          }}
          className="font-mono"
        >
          {title}
        </span>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 26,
            height: 26,
            borderRadius: 8,
            background: glow.fill,
            border: `1px solid ${glow.glow}`,
            boxShadow: `0 0 10px ${glow.glow}`,
          }}
        >
          <Icon size={13} color={glow.text} />
        </span>
      </div>

      <div
        style={{
          color: glow.text,
          fontSize: 28,
          fontWeight: 800,
          letterSpacing: "-0.02em",
          fontFamily: "ui-monospace, Menlo, monospace",
          textShadow: `0 0 12px ${glow.glow}`,
          lineHeight: 1.1,
        }}
        className="font-mono"
      >
        {formatted}
      </div>
    </motion.div>
  );
});

function pickIconForGlow(glow: MetricCardProps["glowColor"]) {
  switch (glow) {
    case "emerald": return Wallet;
    case "purple":  return Percent;
    case "cyan":    return TrendingUp;
    case "amber":   return CircleDollarSign;
  }
}

/* ───────────────────────────────────────────────────────────────────────────
 * 7. AI SENTIMENT GAUGE  (semi-circular, animated needle)
 * ─────────────────────────────────────────────────────────────────────────── */

const GAUGE_W = 280;
const GAUGE_H = 160;
const GAUGE_CX = GAUGE_W / 2;
const GAUGE_CY = GAUGE_H - 10;
const GAUGE_R  = 108;

/** Polar→Cartesian for a half-disc starting at 180° (left) → 0° (right). */
function polarToCartesian(angleDeg: number): { x: number; y: number } {
  // angleDeg: 0..180 ; 0 = bearish (left), 180 = bullish (right).
  // In SVG, X grows right, Y grows down. Map angle to cos/sin in radians.
  // We parametrise so 0 → (cx-r, cy), 90 → (cx, cy-r), 180 → (cx+r, cy).
  const rad = ((180 - angleDeg) * Math.PI) / 180;
  return {
    x: GAUGE_CX + GAUGE_R * Math.cos(rad),
    y: GAUGE_CY - GAUGE_R * Math.sin(rad),
  };
}

const sentimentSpring = { type: "spring" as const, stiffness: 90, damping: 18, mass: 0.7 };

export interface AiSentimentGaugeProps {
  /** Score in [0..1]. Out-of-range values are clamped. */
  score: number;
}

export const AiSentimentGauge = memo(function AiSentimentGauge({
  score,
}: AiSentimentGaugeProps) {
  const safeScore  = clamp(safeNumber(score), 0, 1);
  const angleDeg   = safeScore * 180;          // 0 → bearish, 180 → bullish
  const tip        = polarToCartesian(angleDeg);

  // Label + colour zone
  const zone = useMemo(() => {
    if (safeScore < 0.34) return { label: "BEARISH", color: GLOW.amber };
    if (safeScore < 0.66) return { label: "NEUTRAL", color: GLOW.cyan };
    return { label: "BULLISH", color: GLOW.emerald };
  }, [safeScore]);

  // Static arc path (semi-circle)
  const arcStart = polarToCartesian(0);
  const arcEnd   = polarToCartesian(180);
  const arcD     = `M ${arcStart.x} ${arcStart.y} A ${GAUGE_R} ${GAUGE_R} 0 0 1 ${arcEnd.x} ${arcEnd.y}`;

  return (
    <motion.div
      variants={cardVariants}
      style={panelStyle({ accent: zone.color.glow, padding: "20px 22px" })}
      className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md"
      role="figure"
      aria-label={`AI sentiment: ${zone.label}, score ${safeScore.toFixed(2)}`}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <Brain size={13} color={zone.color.text} />
        <span
          style={{
            color: PALETTE.textMute,
            fontSize: 9.5,
            fontWeight: 700,
            letterSpacing: "0.18em",
          }}
          className="font-mono"
        >
          AI MARKET SENTIMENT
        </span>
      </div>

      <div style={{ display: "flex", justifyContent: "center", marginTop: 4 }}>
        <svg
          viewBox={`0 0 ${GAUGE_W} ${GAUGE_H}`}
          width="100%"
          height={GAUGE_H + 4}
          style={{ maxWidth: GAUGE_W }}
          aria-hidden="true"
        >
          <defs>
            <linearGradient id="gaugeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%"   stopColor={GLOW.amber.stroke} />
              <stop offset="50%"  stopColor={GLOW.cyan.stroke} />
              <stop offset="100%" stopColor={GLOW.emerald.stroke} />
            </linearGradient>
            <filter id="gaugeGlow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="3.2" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>

          {/* Track */}
          <path
            d={arcD}
            fill="none"
            stroke={PALETTE.border}
            strokeWidth={14}
            strokeLinecap="round"
          />
          {/* Neon zones */}
          <path
            d={arcD}
            fill="none"
            stroke="url(#gaugeGrad)"
            strokeWidth={14}
            strokeLinecap="round"
            opacity={0.85}
            filter="url(#gaugeGlow)"
          />

          {/* Tick marks at 0 / 90 / 180 */}
          {[0, 90, 180].map((deg) => {
            const inner = polarToCartesianAt(deg, GAUGE_R - 22);
            const outer = polarToCartesianAt(deg, GAUGE_R + 8);
            return (
              <line
                key={deg}
                x1={inner.x}
                y1={inner.y}
                x2={outer.x}
                y2={outer.y}
                stroke={PALETTE.textMute}
                strokeWidth={1.2}
                opacity={0.6}
              />
            );
          })}

          {/* Animated needle (spring) */}
          <motion.line
            x1={GAUGE_CX}
            y1={GAUGE_CY}
            x2={tip.x}
            y2={tip.y}
            stroke={zone.color.text}
            strokeWidth={2.6}
            strokeLinecap="round"
            initial={false}
            animate={{ x2: tip.x, y2: tip.y }}
            transition={sentimentSpring}
            style={{
              filter: `drop-shadow(0 0 6px ${zone.color.glow})`,
            }}
          />

          {/* Hub */}
          <circle cx={GAUGE_CX} cy={GAUGE_CY} r={6} fill={zone.color.text} />
          <circle cx={GAUGE_CX} cy={GAUGE_CY} r={3} fill={PALETTE.bgRoot} />
        </svg>
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          marginTop: 6,
        }}
      >
        <span
          style={{
            color: zone.color.text,
            fontSize: 24,
            fontWeight: 800,
            fontFamily: "ui-monospace, Menlo, monospace",
            textShadow: `0 0 10px ${zone.color.glow}`,
          }}
          className="font-mono"
        >
          {safeScore.toFixed(2)}
        </span>
        <span
          style={{
            color: zone.color.text,
            fontSize: 11,
            letterSpacing: "0.22em",
            fontWeight: 700,
          }}
          className="font-mono"
        >
          {zone.label}
        </span>
      </div>
    </motion.div>
  );
});

function polarToCartesianAt(angleDeg: number, r: number): { x: number; y: number } {
  const rad = ((180 - angleDeg) * Math.PI) / 180;
  return {
    x: GAUGE_CX + r * Math.cos(rad),
    y: GAUGE_CY - r * Math.sin(rad),
  };
}

/* ───────────────────────────────────────────────────────────────────────────
 * 8. EQUITY CURVE  (recharts AreaChart, render-safe)
 * ─────────────────────────────────────────────────────────────────────────── */

interface NormalizedEquityPoint {
  date:   string;
  value:  number;
  /** % change vs previous point. 0 for the first point. */
  delta:  number;
}

interface EquityTooltipPayloadEntry {
  payload?: NormalizedEquityPoint;
  value?:   number;
}

interface EquityTooltipProps {
  active?:  boolean;
  payload?: ReadonlyArray<EquityTooltipPayloadEntry>;
  label?:   string | number;
}

function EquityTooltip({
  active,
  payload,
  label,
}: EquityTooltipProps) {
  if (!active || !payload || !payload.length) return null;
  const entry = payload[0];
  const point = entry?.payload;
  if (!point) return null;

  const positive = point.delta >= 0;
  const deltaTxt = `${positive ? "+" : ""}${point.delta.toFixed(2)}%`;

  return (
    <div
      style={{
        background: "rgba(4,7,12,0.92)",
        border: `1px solid ${PALETTE.borderHot}`,
        borderRadius: 10,
        padding: "10px 14px",
        fontFamily: "ui-monospace, Menlo, monospace",
        boxShadow: `0 8px 24px rgba(0,0,0,0.55), 0 0 0 1px ${GLOW.cyan.glow}`,
        backdropFilter: "blur(8px)",
      }}
      className="font-mono"
    >
      <p style={{ color: PALETTE.textMute, fontSize: 10, marginBottom: 4, letterSpacing: "0.12em" }}>
        {label ?? point.date}
      </p>
      <p style={{ color: GLOW.cyan.text, fontSize: 14, fontWeight: 700, marginBottom: 2 }}>
        {CURRENCY_FMT.format(point.value)}
      </p>
      <p style={{ color: positive ? PALETTE.green : PALETTE.red, fontSize: 11, fontWeight: 700 }}>
        Δ {deltaTxt}
      </p>
    </div>
  );
}

export interface EquityCurveAreaChartProps {
  points: EquityPoint[];
  height?: number;
}

export const EquityCurveAreaChart = memo(function EquityCurveAreaChart({
  points,
  height = 260,
}: EquityCurveAreaChartProps) {
  // Defensive normalisation: coerce strings to numbers, compute % deltas.
  const normalized: NormalizedEquityPoint[] = useMemo(() => {
    if (!Array.isArray(points)) return [];
    const out: NormalizedEquityPoint[] = [];
    let prev: number | null = null;
    for (const p of points) {
      if (!p || typeof p !== "object") continue;
      const date = String(p.date ?? "").trim();
      if (!date) continue;
      const value = safeNumber(p.value);
      const delta = prev !== null && prev !== 0 ? ((value - prev) / Math.abs(prev)) * 100 : 0;
      out.push({ date, value, delta });
      prev = value;
    }
    return out;
  }, [points]);

  if (normalized.length < 2) {
    return (
      <motion.div
        variants={cardVariants}
        style={panelStyle({ accent: GLOW.cyan.glow, padding: "20px 22px", minHeight: 200 })}
        className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md flex items-center justify-center"
      >
        <p
          style={{ color: PALETTE.textMute, fontSize: 12, letterSpacing: "0.2em", textAlign: "center" }}
          className="font-mono"
        >
          ▸ INSUFFICIENT DATA — AWAITING TRADE HISTORY
        </p>
      </motion.div>
    );
  }

  return (
    <motion.div
      variants={cardVariants}
      style={panelStyle({ accent: GLOW.cyan.glow, padding: "20px 22px 12px" })}
      className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md"
      role="figure"
      aria-label="Equity curve"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <TrendingUp size={13} color={GLOW.cyan.text} />
        <span
          style={{
            color: GLOW.cyan.text,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textShadow: `0 0 8px ${GLOW.cyan.glow}`,
          }}
          className="font-mono"
        >
          EQUITY CURVE — REAL TIME
        </span>
      </div>

      <div style={{ width: "100%", height }}>
        <ChartErrorBoundary>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={normalized} margin={{ top: 6, right: 12, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="equityCyanGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%"   stopColor={GLOW.cyan.stroke} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={GLOW.cyan.stroke} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                stroke={PALETTE.border}
                strokeDasharray="2 4"
                vertical={false}
                opacity={0.5}
              />
              <XAxis
                dataKey="date"
                stroke={PALETTE.textMute}
                tick={{ fontSize: 10, fill: PALETTE.textMute, fontFamily: "ui-monospace, Menlo, monospace" }}
                axisLine={false}
                tickLine={false}
                minTickGap={28}
              />
              <YAxis
                stroke={PALETTE.textMute}
                tick={{ fontSize: 10, fill: PALETTE.textMute, fontFamily: "ui-monospace, Menlo, monospace" }}
                axisLine={false}
                tickLine={false}
                width={56}
                tickFormatter={(v: number) =>
                  Number.isFinite(v) ? `$${Math.round(v).toLocaleString("en-US")}` : "—"
                }
                domain={["dataMin - 5", "dataMax + 5"]}
              />
              <Tooltip
                content={<EquityTooltip />}
                cursor={{ stroke: GLOW.cyan.glow, strokeDasharray: "3 3", strokeWidth: 1 }}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke={GLOW.cyan.stroke}
                strokeWidth={2}
                fill="url(#equityCyanGrad)"
                fillOpacity={1}
                isAnimationActive={false}
                activeDot={{ r: 4, fill: GLOW.cyan.text, stroke: PALETTE.bgRoot, strokeWidth: 2 }}
              />
            </AreaChart>
          </ResponsiveContainer>
        </ChartErrorBoundary>
      </div>
    </motion.div>
  );
});

/* React error boundary base — implemented inline to avoid extra files. */
interface ErrorBoundaryProps { children: ReactNode }
interface ErrorBoundaryState { hasError: boolean }

class ErrorBoundaryBase extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false };
  static getDerivedStateFromError(): ErrorBoundaryState { return { hasError: true }; }
  componentDidCatch(err: Error, info: ErrorInfo): void {
    if (typeof console !== "undefined") {
      console.error("[ChartErrorBoundary]", err, info);
    }
  }
  render(): ReactNode { return this.props.children; }
}

/** Crash-safe wrapper: any chart-render error renders a neutral fallback. */
class ChartErrorBoundary extends ErrorBoundaryBase {
  override render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            color: PALETTE.textMute,
            fontSize: 12,
            letterSpacing: "0.18em",
          }}
          className="font-mono"
        >
          ▸ CHART RENDER ERROR
        </div>
      );
    }
    return this.props.children;
  }
}

/* ───────────────────────────────────────────────────────────────────────────
 * 9. PANEL STYLE HELPER (consistent glassmorphism)
 * ─────────────────────────────────────────────────────────────────────────── */

function panelStyle(opts: {
  accent: string;
  padding?: string;
  minHeight?: number;
  accentBorderLeft?: string;
}): CSSProperties {
  return {
    position: "relative",
    background: PALETTE.bgPanel,
    backdropFilter: "blur(10px)",
    WebkitBackdropFilter: "blur(10px)",
    border: `1px solid ${PALETTE.border}`,
    borderLeft: opts.accentBorderLeft ? `2px solid ${opts.accentBorderLeft}` : `1px solid ${PALETTE.border}`,
    borderRadius: 18,
    padding: opts.padding ?? "18px 20px",
    boxShadow: `0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px ${opts.accent}`,
    minHeight: opts.minHeight,
    overflow: "hidden",
  };
}

/* ───────────────────────────────────────────────────────────────────────────
 * 10. ROOT — DashboardClient
 * ─────────────────────────────────────────────────────────────────────────── */

export default function DashboardClient({
  radar,
  sentimentScore,
  metrics,
  equityCurve,
}: DashboardClientProps) {
  // Mark mounted on client to defer animation start (prevents hydration
  // mismatch warnings & unnecessary first-paint cost).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const handleRetry = useCallback(() => {
    if (typeof window !== "undefined") window.location.reload();
  }, []);

  const gridMetrics: MetricCardProps[] = useMemo(
    () => [
      { title: "AVAILABLE BALANCE",   value: metrics.availableBalance ?? "0", type: "currency",   glowColor: "emerald" },
      { title: "NET RETURN",          value: metrics.netReturnPct     ?? "0", type: "percentage", glowColor: "purple"  },
      { title: "ACCUMULATED PROFIT",  value: metrics.accumulatedProfit ?? "0", type: "currency",  glowColor: "cyan"    },
      { title: "PAID FEES",           value: metrics.paidFees         ?? "0", type: "currency",   glowColor: "amber"   },
    ],
    [metrics],
  );

  return (
    <main
      style={{
        minHeight: "100vh",
        background:
          `radial-gradient(900px 600px at 12% -10%, rgba(34,211,238,0.08), transparent 60%),` +
          `radial-gradient(900px 600px at 100% 0%, rgba(168,85,247,0.07), transparent 65%),` +
          `${PALETTE.bgRoot}`,
        color: PALETTE.text,
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        padding: "32px 28px 56px",
      }}
      className="bg-[#04070c] text-slate-200"
      data-component="DashboardClient"
      onError={handleRetry}
    >
      <header style={{ marginBottom: 28, display: "flex", alignItems: "baseline", gap: 14 }}>
        <h1
          style={{
            margin: 0,
            fontSize: 22,
            fontWeight: 800,
            letterSpacing: "-0.02em",
            color: PALETTE.text,
          }}
        >
          ◈ OptiFerre <span style={{ color: GLOW.cyan.text }}>Terminal</span>
        </h1>
        <span
          style={{
            color: PALETTE.textMute,
            fontSize: 10,
            letterSpacing: "0.22em",
            fontWeight: 700,
          }}
          className="font-mono"
        >
          INVESTOR · v2.0
        </span>
      </header>

      <motion.section
        variants={containerVariants}
        initial="hidden"
        animate={mounted ? "visible" : "hidden"}
        style={{ display: "grid", gap: 18, maxWidth: 1280, margin: "0 auto" }}
      >
        {/* Row 1: Radar + Sentiment */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0,1.4fr) minmax(0,1fr)",
            gap: 18,
          }}
        >
          <LiveRadar
            ticker={radar.ticker}
            entryTime={radar.entryTime}
            aiConfidence={radar.aiConfidence}
            isActive={radar.isActive}
          />
          <AiSentimentGauge score={sentimentScore} />
        </div>

        {/* Row 2: Metric grid (4 columns, responsive) */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: 18,
          }}
        >
          {gridMetrics.map((m) => (
            <MetricCard key={m.title} {...m} />
          ))}
        </div>

        {/* Row 3: Equity curve */}
        <EquityCurveAreaChart points={equityCurve} />
      </motion.section>
    </main>
  );
}
