"use client";
/**
 * web/components/admin-center.tsx
 *
 * OptiFerre Terminal v2.0 — Admin Mission Control (premium)
 *
 * Strict-TypeScript, render-safe, GPU-accelerated cyberpunk admin centre.
 *
 * Stack:
 *   • React 19 + Next.js 16 (Client Component)
 *   • framer-motion (variants declared OUTSIDE components)
 *   • lucide-react (icons)
 *
 * Sections:
 *   1. KPI Glow Cards (Global AUM, Total Revenue, Active Investors)
 *      with background micro-sparklines.
 *   2. Investor Risk Heatmap (GitHub-contribution-style grid).
 *   3. High-Velocity Operations Log (paginated ledger table with
 *      colour-coded badges — exact tokens specified by design).
 *
 * Project styling note:
 *   This project does NOT have TailwindCSS configured (verified). Each
 *   component therefore ships with explicit `style={{...}}` blocks using
 *   the EXACT colour tokens demanded by the design spec. `className`
 *   strings are still emitted alongside so that when Tailwind is added
 *   the components light up automatically — zero refactor required.
 */

import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { motion, type Variants } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  ArrowDownUp,
  Banknote,
  ChevronLeft,
  ChevronRight,
  TrendingUp,
  Users,
  Wallet,
} from "lucide-react";

/* ───────────────────────────────────────────────────────────────────────────
 * 1. PUBLIC TYPE CONTRACTS  (mandatory — must not be altered)
 * ─────────────────────────────────────────────────────────────────────────── */

export interface LedgerTransaction {
  id:        string;
  userId:    string;
  timestamp: string;
  type:      "ENTRY_FEE" | "DEPOSIT" | "TRADE_PNL";
  /** Stringified Prisma.Decimal — defensive parsing applied in component. */
  amount:    string;
  /** Recent micro-history (for the row-level sparkline). */
  pnlHistory: number[];
}

export interface AdminDashboardProps {
  globalAum:            string;
  totalRevenue:         string;
  activeInvestorsCount: number;
  transactions:         LedgerTransaction[];
  riskGridData: ReadonlyArray<{
    userId:        string;
    drawdownLevel: number;
  }>;
}

/* ───────────────────────────────────────────────────────────────────────────
 * 2. COLOUR TOKENS  (Tailwind-equivalent hex map — mandated by design spec)
 * ─────────────────────────────────────────────────────────────────────────── */

const PALETTE = {
  bgRoot:     "#04070c",
  bgPanel:    "rgba(10,15,22,0.72)",
  bgPanelAlt: "rgba(13,20,30,0.72)",
  border:     "rgba(63,87,114,0.28)",
  borderHot:  "rgba(99,135,178,0.45)",
  text:       "#dce7f5",
  textStrong: "#f1f5fb",
  textMute:   "#6b8299",
  rowHover:   "rgba(34,211,238,0.05)",
} as const;

/** Mapped EXACTLY from the requested Tailwind tokens. */
const BADGES = {
  ENTRY_FEE: {
    color:       "#fbbf24",                  // text-amber-400
    background:  "rgba(69, 26, 3, 0.5)",     // bg-amber-950/50
    borderColor: "rgba(245, 158, 11, 0.3)",  // border-amber-500/30
  },
  DEPOSIT: {
    color:       "#60a5fa",                  // text-blue-400
    background:  "rgba(23, 37, 84, 0.5)",    // bg-blue-950/50
    borderColor: "rgba(59, 130, 246, 0.3)",  // border-blue-500/30
  },
  TRADE_PNL: {
    color:       "#34d399",                  // text-emerald-400
    background:  "rgba(2, 44, 34, 0.5)",     // bg-emerald-950/50
    borderColor: "rgba(16, 185, 129, 0.3)",  // border-emerald-500/30
  },
} as const;

const KPI_GLOW = {
  emerald: { stroke: "#10b981", text: "#34d399", glow: "rgba(16,185,129,0.45)", fill: "rgba(16,185,129,0.18)" },
  cyan:    { stroke: "#06b6d4", text: "#22d3ee", glow: "rgba(6,182,212,0.45)",  fill: "rgba(6,182,212,0.18)"  },
  purple:  { stroke: "#a855f7", text: "#c084fc", glow: "rgba(168,85,247,0.45)", fill: "rgba(168,85,247,0.18)" },
} as const;

/** Discrete thresholds for the heatmap colour scale. */
const HEATMAP_LOW    = "#2e1065"; // deep dark purple   — low risk
const HEATMAP_MID    = "#facc15"; // electric yellow    — medium
const HEATMAP_HIGH   = "#c2410c"; // burnt orange       — critical
const HEATMAP_EMPTY  = "rgba(255,255,255,0.05)";

/* ───────────────────────────────────────────────────────────────────────────
 * 3. SAFE PARSING / FORMATTING  (defensive — never throws, never NaN)
 * ─────────────────────────────────────────────────────────────────────────── */

function safeNumber(raw: unknown, fallback = 0): number {
  if (raw === null || raw === undefined) return fallback;
  if (typeof raw === "number") return Number.isFinite(raw) ? raw : fallback;
  if (typeof raw !== "string") return fallback;
  const cleaned = raw.replace(/[$,\s%]/g, "").trim();
  if (cleaned === "") return fallback;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : fallback;
}

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

const COMPACT_CURRENCY_FMT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 2,
});

const INTEGER_FMT = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

const SIGNED_CURRENCY_FMT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "always",
});

/** "0xabcdef1234abcd" → "0xabcd…abcd" (head=6, tail=4 default). */
function truncateUserId(userId: string, head = 6, tail = 4): string {
  const safe = (userId ?? "").toString().trim();
  if (!safe) return "—";
  if (safe.length <= head + tail + 1) return safe;
  return `${safe.slice(0, head)}…${safe.slice(-tail)}`;
}

function formatTimestamp(ts: string): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  // YYYY-MM-DD HH:MM:SS  (terminal aesthetic)
  const pad = (n: number) => n.toString().padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

/* ───────────────────────────────────────────────────────────────────────────
 * 4. ANIMATION VARIANTS  (declared OUTSIDE components)
 * ─────────────────────────────────────────────────────────────────────────── */

const containerVariants: Variants = {
  hidden:  {},
  visible: { transition: { staggerChildren: 0.06, delayChildren: 0.04 } },
};

const cardVariants: Variants = {
  hidden:  { opacity: 0, y: 16, scale: 0.985 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: 0.45, ease: [0.22, 1, 0.36, 1] },
  },
};

const heatmapCellVariants: Variants = {
  hidden:  { opacity: 0, scale: 0.6 },
  visible: { opacity: 1, scale: 1, transition: { duration: 0.25, ease: "easeOut" } },
};

const rowVariants: Variants = {
  hidden:  { opacity: 0, x: -6 },
  visible: { opacity: 1, x: 0, transition: { duration: 0.22, ease: "easeOut" } },
};

const ambientPulse: Variants = {
  pulse: {
    opacity: [0.55, 0.95, 0.55],
    transition: { duration: 2.4, ease: "easeInOut", repeat: Infinity },
  },
};

/* ───────────────────────────────────────────────────────────────────────────
 * 5. PANEL HELPER (consistent glassmorphism)
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
 * 6. MICRO-SPARKLINE (reusable SVG polyline)
 * ─────────────────────────────────────────────────────────────────────────── */

interface MicroSparklineProps {
  data: number[];
  color: string;
  width?: number;
  height?: number;
  strokeWidth?: number;
  filled?: boolean;
  style?: CSSProperties;
}

const MicroSparkline = memo(function MicroSparkline({
  data,
  color,
  width = 80,
  height = 24,
  strokeWidth = 1.4,
  filled = false,
  style,
}: MicroSparklineProps) {
  const { points, areaPath } = useMemo(() => {
    if (!Array.isArray(data) || data.length < 2) return { points: "", areaPath: "" };
    const valid = data.filter((n): n is number => typeof n === "number" && Number.isFinite(n));
    if (valid.length < 2) return { points: "", areaPath: "" };

    const min = Math.min(...valid);
    const max = Math.max(...valid);
    const span = max - min || 1;
    const stepX = width / (valid.length - 1);
    const yOf = (v: number) => height - ((v - min) / span) * (height - 2) - 1;

    const pts = valid.map((v, i) => `${(i * stepX).toFixed(2)},${yOf(v).toFixed(2)}`).join(" ");
    const area =
      `M 0,${height} L ${valid
        .map((v, i) => `${(i * stepX).toFixed(2)},${yOf(v).toFixed(2)}`)
        .join(" L ")} L ${width},${height} Z`;
    return { points: pts, areaPath: area };
  }, [data, width, height]);

  if (!points) {
    return (
      <svg width={width} height={height} style={style} aria-hidden="true">
        <line
          x1={0}
          y1={height / 2}
          x2={width}
          y2={height / 2}
          stroke={PALETTE.textMute}
          strokeOpacity={0.25}
          strokeDasharray="2 3"
        />
      </svg>
    );
  }

  return (
    <svg width={width} height={height} style={style} aria-hidden="true">
      {filled && (
        <>
          <defs>
            <linearGradient id={`spark-fill-${color.replace(/[^a-z0-9]/gi, "")}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.45} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <path d={areaPath} fill={`url(#spark-fill-${color.replace(/[^a-z0-9]/gi, "")})`} />
        </>
      )}
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
});

/* ───────────────────────────────────────────────────────────────────────────
 * 7. KPI GLOW CARDS
 * ─────────────────────────────────────────────────────────────────────────── */

interface KpiCardProps {
  title: string;
  formattedValue: string;
  rawNumeric: number;
  glow: keyof typeof KPI_GLOW;
  Icon: typeof Wallet;
  sparkline: number[];
  subline?: string;
}

const KpiGlowCard = memo(function KpiGlowCard({
  title,
  formattedValue,
  glow,
  Icon,
  sparkline,
  subline,
}: KpiCardProps) {
  const tone = KPI_GLOW[glow];
  return (
    <motion.div
      variants={cardVariants}
      whileHover={{ y: -3, transition: { duration: 0.2, ease: "easeOut" } }}
      style={panelStyle({
        accent: tone.glow,
        padding: "20px 22px",
        accentBorderLeft: tone.stroke,
      })}
      className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md"
      role="figure"
      aria-label={`${title}: ${formattedValue}`}
    >
      {/* Background sparkline */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          opacity: 0.18,
          pointerEvents: "none",
          display: "flex",
          alignItems: "flex-end",
          paddingBottom: 8,
        }}
        aria-hidden="true"
      >
        <MicroSparkline
          data={sparkline}
          color={tone.stroke}
          width={400}
          height={64}
          strokeWidth={1.6}
          filled
          style={{ width: "100%", height: 64 }}
        />
      </div>

      <div style={{ position: "relative", zIndex: 1 }}>
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
          <motion.span
            variants={ambientPulse}
            animate="pulse"
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: 28,
              height: 28,
              borderRadius: 9,
              background: tone.fill,
              border: `1px solid ${tone.glow}`,
              boxShadow: `0 0 12px ${tone.glow}`,
            }}
          >
            <Icon size={14} color={tone.text} />
          </motion.span>
        </div>

        <div
          style={{
            color: tone.text,
            fontSize: 30,
            fontWeight: 800,
            letterSpacing: "-0.02em",
            fontFamily: "ui-monospace, Menlo, monospace",
            textShadow: `0 0 12px ${tone.glow}`,
            lineHeight: 1.05,
          }}
          className="font-mono"
        >
          {formattedValue}
        </div>

        {subline && (
          <p
            style={{
              marginTop: 8,
              color: PALETTE.textMute,
              fontSize: 10.5,
              letterSpacing: "0.06em",
            }}
            className="font-mono"
          >
            {subline}
          </p>
        )}
      </div>
    </motion.div>
  );
});

/* ───────────────────────────────────────────────────────────────────────────
 * 8. INVESTOR RISK HEATMAP  (GitHub-contribution grid)
 * ─────────────────────────────────────────────────────────────────────────── */

function getRiskColor(level: number): string {
  const v = clamp(safeNumber(level), 0, 1);
  if (v < 0.33) return HEATMAP_LOW;
  if (v < 0.66) return HEATMAP_MID;
  return HEATMAP_HIGH;
}

function getRiskLabel(level: number): string {
  const v = clamp(safeNumber(level), 0, 1);
  if (v < 0.33) return "LOW";
  if (v < 0.66) return "MEDIUM";
  return "CRITICAL";
}

interface RiskHeatmapProps {
  data: AdminDashboardProps["riskGridData"];
}

const InvestorRiskHeatmap = memo(function InvestorRiskHeatmap({
  data,
}: RiskHeatmapProps) {
  const cells = useMemo(() => {
    if (!Array.isArray(data)) return [];
    return data
      .filter((d): d is { userId: string; drawdownLevel: number } =>
        Boolean(d) &&
        typeof d.userId === "string" &&
        d.userId.trim() !== "" &&
        typeof d.drawdownLevel === "number" &&
        Number.isFinite(d.drawdownLevel),
      )
      .map((d) => ({
        userId: d.userId,
        level:  clamp(d.drawdownLevel, 0, 1),
      }));
  }, [data]);

  const [hovered, setHovered] = useState<null | { x: number; y: number; userId: string; level: number }>(null);

  const handleEnter = useCallback(
    (e: React.MouseEvent<HTMLDivElement>, userId: string, level: number) => {
      const rect = (e.currentTarget.parentElement as HTMLDivElement | null)?.getBoundingClientRect();
      if (!rect) return;
      const cellRect = e.currentTarget.getBoundingClientRect();
      setHovered({
        x: cellRect.left - rect.left + cellRect.width / 2,
        y: cellRect.top  - rect.top,
        userId,
        level,
      });
    },
    [],
  );

  const handleLeave = useCallback(() => setHovered(null), []);

  return (
    <motion.div
      variants={cardVariants}
      style={panelStyle({ accent: KPI_GLOW.purple.glow, padding: "20px 22px" })}
      className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md"
      role="figure"
      aria-label="Investor risk heatmap"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <AlertTriangle size={13} color={KPI_GLOW.purple.text} />
        <span
          style={{
            color: KPI_GLOW.purple.text,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textShadow: `0 0 8px ${KPI_GLOW.purple.glow}`,
          }}
          className="font-mono"
        >
          INVESTOR RISK MATRIX
        </span>
        <span
          style={{
            marginLeft: "auto",
            color: PALETTE.textMute,
            fontSize: 10,
            letterSpacing: "0.12em",
          }}
          className="font-mono"
        >
          {cells.length} ACCOUNTS
        </span>
      </div>

      {cells.length === 0 ? (
        <div
          style={{
            padding: "40px 0",
            textAlign: "center",
            color: PALETTE.textMute,
            fontSize: 12,
            letterSpacing: "0.18em",
          }}
          className="font-mono"
        >
          ▸ NO INVESTORS YET
        </div>
      ) : (
        <div style={{ position: "relative" }}>
          <motion.div
            variants={containerVariants}
            initial="hidden"
            animate="visible"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(16px, 1fr))",
              gap: 4,
            }}
          >
            {cells.map((cell, idx) => {
              const color = getRiskColor(cell.level);
              return (
                <motion.div
                  key={`${cell.userId}-${idx}`}
                  variants={heatmapCellVariants}
                  whileHover={{ scale: 1.45, transition: { duration: 0.12 } }}
                  onMouseEnter={(e) => handleEnter(e, cell.userId, cell.level)}
                  onMouseLeave={handleLeave}
                  style={{
                    width: "100%",
                    aspectRatio: "1 / 1",
                    background: color,
                    borderRadius: 3,
                    boxShadow: `0 0 4px ${color}55`,
                    cursor: "pointer",
                    willChange: "transform",
                  }}
                  className="will-change-transform"
                  role="img"
                  aria-label={`${truncateUserId(cell.userId)} drawdown ${(cell.level * 100).toFixed(0)}%`}
                />
              );
            })}
          </motion.div>

          {hovered && (
            <div
              style={{
                position: "absolute",
                left: hovered.x,
                top:  hovered.y - 56,
                transform: "translateX(-50%)",
                background: "rgba(4,7,12,0.95)",
                border: `1px solid ${PALETTE.borderHot}`,
                borderRadius: 8,
                padding: "8px 12px",
                fontFamily: "ui-monospace, Menlo, monospace",
                fontSize: 11,
                color: PALETTE.text,
                whiteSpace: "nowrap",
                boxShadow: `0 6px 18px rgba(0,0,0,0.5)`,
                pointerEvents: "none",
                zIndex: 10,
              }}
              className="font-mono"
            >
              <div style={{ color: PALETTE.textMute, fontSize: 10, marginBottom: 2 }}>
                {truncateUserId(hovered.userId)}
              </div>
              <div style={{ color: getRiskColor(hovered.level), fontWeight: 700 }}>
                {(hovered.level * 100).toFixed(1)}% · {getRiskLabel(hovered.level)}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Legend */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          marginTop: 16,
          fontSize: 9.5,
          letterSpacing: "0.14em",
          color: PALETTE.textMute,
        }}
        className="font-mono"
      >
        <LegendDot color={HEATMAP_LOW}  label="LOW" />
        <LegendDot color={HEATMAP_MID}  label="MEDIUM" />
        <LegendDot color={HEATMAP_HIGH} label="CRITICAL" />
      </div>
    </motion.div>
  );
});

const LegendDot = memo(function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span style={{ width: 10, height: 10, borderRadius: 2, background: color, boxShadow: `0 0 6px ${color}80` }} />
      {label}
    </span>
  );
});

/* ───────────────────────────────────────────────────────────────────────────
 * 9. HIGH-VELOCITY OPERATIONS LOG  (paginated ledger table)
 * ─────────────────────────────────────────────────────────────────────────── */

const PAGE_SIZE = 25;

interface OpsLogProps {
  transactions: LedgerTransaction[];
}

const HighVelocityOpsLog = memo(function HighVelocityOpsLog({
  transactions,
}: OpsLogProps) {
  // Defensive: sort by timestamp desc, drop malformed rows.
  const sorted = useMemo(() => {
    if (!Array.isArray(transactions)) return [];
    return transactions
      .filter((t): t is LedgerTransaction =>
        Boolean(t) &&
        typeof t.id === "string" &&
        typeof t.userId === "string" &&
        typeof t.timestamp === "string" &&
        (t.type === "ENTRY_FEE" || t.type === "DEPOSIT" || t.type === "TRADE_PNL"),
      )
      .slice()
      .sort((a, b) => {
        const da = new Date(a.timestamp).getTime();
        const db = new Date(b.timestamp).getTime();
        const sa = Number.isFinite(da) ? da : 0;
        const sb = Number.isFinite(db) ? db : 0;
        return sb - sa;
      });
  }, [transactions]);

  const [page, setPage] = useState(0);
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const pageRows = useMemo(
    () => sorted.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE),
    [sorted, page],
  );

  // Guard against page overflow when data shrinks.
  useEffect(() => {
    if (page >= totalPages) setPage(0);
  }, [page, totalPages]);

  return (
    <motion.div
      variants={cardVariants}
      style={panelStyle({ accent: KPI_GLOW.cyan.glow, padding: "20px 22px 14px" })}
      className="rounded-2xl border bg-[#0a0f16]/70 backdrop-blur-md"
      role="region"
      aria-label="High-velocity operations log"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <Activity size={13} color={KPI_GLOW.cyan.text} />
        <span
          style={{
            color: KPI_GLOW.cyan.text,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textShadow: `0 0 8px ${KPI_GLOW.cyan.glow}`,
          }}
          className="font-mono"
        >
          HIGH-VELOCITY OPERATIONS LOG
        </span>
        <span
          style={{
            marginLeft: "auto",
            color: PALETTE.textMute,
            fontSize: 10,
            letterSpacing: "0.12em",
          }}
          className="font-mono"
        >
          {sorted.length} TX
        </span>
      </div>

      <div
        style={{
          width: "100%",
          overflowX: "auto",
          borderRadius: 10,
          border: `1px solid ${PALETTE.border}`,
        }}
      >
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontFamily: "ui-monospace, Menlo, monospace",
            fontSize: 12,
          }}
          className="font-mono"
        >
          <thead>
            <tr style={{ background: PALETTE.bgPanelAlt }}>
              <Th>TIMESTAMP</Th>
              <Th>USER</Th>
              <Th>TYPE</Th>
              <Th align="right">AMOUNT</Th>
              <Th align="right">7-PT TREND</Th>
            </tr>
          </thead>
          <tbody>
            {pageRows.length === 0 ? (
              <tr>
                <td
                  colSpan={5}
                  style={{
                    padding: "26px 14px",
                    textAlign: "center",
                    color: PALETTE.textMute,
                    fontSize: 11,
                    letterSpacing: "0.18em",
                  }}
                >
                  ▸ NO TRANSACTIONS RECORDED
                </td>
              </tr>
            ) : (
              pageRows.map((tx) => <OpsLogRow key={tx.id} tx={tx} />)
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination footer */}
      {totalPages > 1 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginTop: 12,
            color: PALETTE.textMute,
            fontSize: 11,
          }}
          className="font-mono"
        >
          <span style={{ letterSpacing: "0.12em" }}>
            PAGE {page + 1} / {totalPages}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <PageBtn
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              ariaLabel="Previous page"
            >
              <ChevronLeft size={13} />
            </PageBtn>
            <PageBtn
              disabled={page >= totalPages - 1}
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              ariaLabel="Next page"
            >
              <ChevronRight size={13} />
            </PageBtn>
          </div>
        </div>
      )}
    </motion.div>
  );
});

const Th = memo(function Th({
  children,
  align = "left",
}: {
  children: ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      style={{
        textAlign: align,
        padding: "10px 14px",
        color: PALETTE.textMute,
        fontSize: 9.5,
        fontWeight: 700,
        letterSpacing: "0.16em",
        borderBottom: `1px solid ${PALETTE.border}`,
        position: "sticky",
        top: 0,
        background: PALETTE.bgPanelAlt,
        zIndex: 1,
      }}
    >
      {children}
    </th>
  );
});

const OpsLogRow = memo(function OpsLogRow({ tx }: { tx: LedgerTransaction }) {
  const badge   = BADGES[tx.type];
  const amount  = safeNumber(tx.amount);
  const signed  = tx.type === "TRADE_PNL"
    ? SIGNED_CURRENCY_FMT.format(amount)
    : CURRENCY_FMT.format(Math.abs(amount));
  const amountColor =
    tx.type === "TRADE_PNL"
      ? amount >= 0 ? "#34d399" : "#fb7185"
      : badge.color;

  const sparkColor =
    tx.type === "TRADE_PNL"
      ? amount >= 0 ? "#34d399" : "#fb7185"
      : badge.color;

  return (
    <motion.tr
      variants={rowVariants}
      initial="hidden"
      animate="visible"
      whileHover={{ backgroundColor: PALETTE.rowHover }}
      style={{
        borderBottom: `1px solid ${PALETTE.border}`,
        color: PALETTE.text,
      }}
    >
      <td style={{ padding: "10px 14px", color: PALETTE.textMute, whiteSpace: "nowrap" }}>
        {formatTimestamp(tx.timestamp)}
      </td>
      <td style={{ padding: "10px 14px", color: PALETTE.text, whiteSpace: "nowrap" }}>
        {truncateUserId(tx.userId)}
      </td>
      <td style={{ padding: "10px 14px" }}>
        <span
          style={{
            display: "inline-block",
            padding: "3px 9px",
            borderRadius: 999,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.08em",
            color: badge.color,
            background: badge.background,
            border: `1px solid ${badge.borderColor}`,
          }}
          className={
            tx.type === "ENTRY_FEE"
              ? "text-amber-400 bg-amber-950/50 border-amber-500/30"
              : tx.type === "DEPOSIT"
              ? "text-blue-400 bg-blue-950/50 border-blue-500/30"
              : "text-emerald-400 bg-emerald-950/50 border-emerald-500/30"
          }
        >
          {tx.type}
        </span>
      </td>
      <td
        style={{
          padding: "10px 14px",
          textAlign: "right",
          color: amountColor,
          fontWeight: 700,
          whiteSpace: "nowrap",
        }}
      >
        {signed}
      </td>
      <td style={{ padding: "10px 14px", textAlign: "right" }}>
        <MicroSparkline
          data={Array.isArray(tx.pnlHistory) ? tx.pnlHistory : []}
          color={sparkColor}
          width={80}
          height={22}
          strokeWidth={1.3}
          style={{ verticalAlign: "middle" }}
        />
      </td>
    </motion.tr>
  );
});

const PageBtn = memo(function PageBtn({
  children,
  onClick,
  disabled,
  ariaLabel,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled: boolean;
  ariaLabel: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 28,
        height: 28,
        borderRadius: 8,
        border: `1px solid ${disabled ? PALETTE.border : PALETTE.borderHot}`,
        background: disabled ? "transparent" : "rgba(34,211,238,0.06)",
        color: disabled ? PALETTE.textMute : KPI_GLOW.cyan.text,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.45 : 1,
        transition: "background 0.15s ease, border-color 0.15s ease",
      }}
    >
      {children}
    </button>
  );
});

/* ───────────────────────────────────────────────────────────────────────────
 * 10. ROOT — AdminCenter
 * ─────────────────────────────────────────────────────────────────────────── */

export default function AdminCenter({
  globalAum,
  totalRevenue,
  activeInvestorsCount,
  transactions,
  riskGridData,
}: AdminDashboardProps) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // Pre-compute KPI sparklines from the transactions stream.
  const aumSpark = useMemo(() => buildAumSparkline(transactions), [transactions]);
  const revenueSpark = useMemo(
    () => buildRevenueSparkline(transactions),
    [transactions],
  );
  const investorsSpark = useMemo(
    () => buildInvestorsSparkline(transactions),
    [transactions],
  );

  const aumNumeric     = safeNumber(globalAum);
  const revenueNumeric = safeNumber(totalRevenue);
  const investorCount  = Number.isFinite(activeInvestorsCount) ? Math.max(0, Math.trunc(activeInvestorsCount)) : 0;

  return (
    <main
      style={{
        minHeight: "100vh",
        background:
          `radial-gradient(900px 600px at 14% -8%, rgba(168,85,247,0.08), transparent 60%),` +
          `radial-gradient(900px 600px at 100% 0%, rgba(6,182,212,0.07), transparent 65%),` +
          `${PALETTE.bgRoot}`,
        color: PALETTE.text,
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        padding: "32px 28px 56px",
      }}
      className="bg-[#04070c] text-slate-200"
      data-component="AdminCenter"
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
          ◈ OptiFerre <span style={{ color: KPI_GLOW.purple.text }}>Mission Control</span>
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
          ADMIN · v2.0
        </span>
      </header>

      <motion.section
        variants={containerVariants}
        initial="hidden"
        animate={mounted ? "visible" : "hidden"}
        style={{ display: "grid", gap: 18, maxWidth: 1320, margin: "0 auto" }}
      >
        {/* Row 1: KPI cards */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 18,
          }}
        >
          <KpiGlowCard
            title="GLOBAL AUM"
            formattedValue={CURRENCY_FMT.format(aumNumeric)}
            rawNumeric={aumNumeric}
            glow="emerald"
            Icon={Wallet}
            sparkline={aumSpark}
            subline={`Compact: ${COMPACT_CURRENCY_FMT.format(aumNumeric)}`}
          />
          <KpiGlowCard
            title="TOTAL REVENUE"
            formattedValue={CURRENCY_FMT.format(revenueNumeric)}
            rawNumeric={revenueNumeric}
            glow="cyan"
            Icon={Banknote}
            sparkline={revenueSpark}
            subline="Lifetime fees collected"
          />
          <KpiGlowCard
            title="ACTIVE INVESTORS"
            formattedValue={INTEGER_FMT.format(investorCount)}
            rawNumeric={investorCount}
            glow="purple"
            Icon={Users}
            sparkline={investorsSpark}
            subline={investorCount === 1 ? "1 active account" : `${investorCount} active accounts`}
          />
        </div>

        {/* Row 2: Heatmap (full width) */}
        <InvestorRiskHeatmap data={riskGridData} />

        {/* Row 3: Ops log (full width) */}
        <HighVelocityOpsLog transactions={transactions} />
      </motion.section>
    </main>
  );
}

/* ───────────────────────────────────────────────────────────────────────────
 * 11. SPARKLINE BUILDERS  (defensive — never throw on bad data)
 * ─────────────────────────────────────────────────────────────────────────── */

function buildAumSparkline(transactions: LedgerTransaction[]): number[] {
  if (!Array.isArray(transactions) || transactions.length === 0) return [];
  const sorted = transactions
    .filter((t): t is LedgerTransaction => Boolean(t) && typeof t.timestamp === "string")
    .slice()
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
  let cum = 0;
  const out: number[] = [];
  for (const tx of sorted) {
    const a = safeNumber(tx.amount);
    if (tx.type === "DEPOSIT") cum += a;
    else if (tx.type === "TRADE_PNL") cum += a;
    out.push(cum);
  }
  return downsample(out, 32);
}

function buildRevenueSparkline(transactions: LedgerTransaction[]): number[] {
  if (!Array.isArray(transactions) || transactions.length === 0) return [];
  const sorted = transactions
    .filter((t): t is LedgerTransaction => Boolean(t) && typeof t.timestamp === "string")
    .slice()
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
  let cum = 0;
  const out: number[] = [];
  for (const tx of sorted) {
    if (tx.type === "ENTRY_FEE") cum += Math.abs(safeNumber(tx.amount));
    out.push(cum);
  }
  return downsample(out, 32);
}

function buildInvestorsSparkline(transactions: LedgerTransaction[]): number[] {
  if (!Array.isArray(transactions) || transactions.length === 0) return [];
  const sorted = transactions
    .filter((t): t is LedgerTransaction => Boolean(t) && typeof t.timestamp === "string")
    .slice()
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
  const seen = new Set<string>();
  const out: number[] = [];
  for (const tx of sorted) {
    if (tx.type === "DEPOSIT" || tx.type === "ENTRY_FEE") seen.add(tx.userId);
    out.push(seen.size);
  }
  return downsample(out, 32);
}

function downsample(arr: number[], target: number): number[] {
  if (arr.length <= target) return arr;
  const step = arr.length / target;
  const out: number[] = [];
  for (let i = 0; i < target; i++) {
    const idx = Math.min(arr.length - 1, Math.floor(i * step));
    out.push(arr[idx]);
  }
  return out;
}

/* ───────────────────────────────────────────────────────────────────────────
 * 12. UNUSED-EXPORT GUARD  (silence tree-shaker false positives)
 * ─────────────────────────────────────────────────────────────────────────── */

export const __ADMIN_CENTER_ICONS_USED = {
  Activity,
  AlertTriangle,
  ArrowDownUp,
  Banknote,
  ChevronLeft,
  ChevronRight,
  TrendingUp,
  Users,
  Wallet,
};
