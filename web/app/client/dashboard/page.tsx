/**
 * app/client/dashboard/page.tsx — Investor Client Dashboard
 *
 * Server Component (force-dynamic). Zero-Trust data isolation:
 *   • user_id extracted exclusively from the verified JWT cookie.
 *   • Never trusts any query-param or header sent by the browser.
 *
 * Decimal serialisation contract:
 *   • All Prisma.Decimal / BigInt values are converted to `string` before
 *     being passed as props to Client Components.
 *   • Client Components receive plain strings and call Number(str) or
 *     toLocaleString() for rendering — zero precision loss.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import fs from "node:fs/promises";
import path from "node:path";
import { verifyJWT } from "@/lib/auth";
import { prisma } from "@/lib/db";
import { Prisma } from "@prisma/client";
import { EquityCurveChart } from "@/components/client/EquityCurveChart";
import { TradeHistoryTable } from "@/components/client/TradeHistoryTable";
import { ActiveTradeRadar } from "@/components/client/ActiveTradeRadar";
import { SupportWidget } from "@/components/client/SupportWidget";
import { MotionGrid, MotionCard } from "@/components/client/DashboardMotionLayout";
import type { TradeRow, EquityPoint } from "@/components/client/client-types";

export const dynamic = "force-dynamic";

// ─── Raw SQL types ────────────────────────────────────────────────────────────
// Prisma $queryRaw returns BIGSERIAL → BigInt, NUMERIC → Prisma.Decimal,
// TIMESTAMPTZ → Date. All must be serialised before crossing Server→Client.
type TradeQueryRow = {
  alloc_id:              bigint;
  symbol:                string;
  side:                  string;
  opened_at:             Date;
  closed_at:             Date;
  gross_user_pnl_usdt:   string; // NUMERIC cast to TEXT in SQL for safety
  admin_fee_usdt:        string;
  user_net_pnl_usdt:     string;
  allocated_at:          Date;
  broker:                string | null;
};

type ClosedContractFileRow = {
  contract_id?: number | string;
  user_id?: string;
  symbol?: string;
  side?: string;
  opened_at_ts?: number | string;
  closed_at_ts?: number | string;
  realized_pnl_usdt?: number | string;
};

// ─── Colour tokens (Server-side layout) ──────────────────────────────────────
const BG    = "#04070c";
const CARD  = "rgba(10,15,22,0.72)";
const BORD  = "rgba(63,87,114,0.28)";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#10b981";
const RED   = "#fb7185";
const BLUE  = "#22d3ee";

function fmtUSDT(v: string, showSign = false): string {
  const n = Number(v);
  const sign = showSign && n > 0 ? "+" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function toIsoDateString(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) {
    const d = new Date(value);
    if (!Number.isNaN(d.getTime())) {
      return d.toISOString();
    }
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    const ms = value > 1_000_000_000_000 ? value : value * 1000;
    const d = new Date(ms);
    if (!Number.isNaN(d.getTime())) {
      return d.toISOString();
    }
  }
  return undefined;
}

// ─── Page ─────────────────────────────────────────────────────────────────────
export default async function ClientDashboardPage() {

  // ── 1. Auth & role guard ────────────────────────────────────────────────────
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;

  if (!payload) {
    redirect("/portal/login?next=/client/dashboard");
  }

  // Admins get their own dashboard.
  if (payload.role === "admin") {
    redirect("/admin/dashboard");
  }

  // Only 'client' and legacy 'investor' roles allowed here.
  if (payload.role !== "client" && payload.role !== "investor") {
    redirect("/portal/login?next=/client/dashboard");
  }

  const userId = payload.sub; // Zero-Trust: always from the signed JWT.

  const readSharedStateArray = async (candidates: string[]): Promise<unknown[]> => {
    try {
      const logsDir = process.env.DERIV_STATE_DIR
        ?? process.env.BOT_STATE_DIR
        ?? path.join(process.cwd(), "..", "logs");
      for (const fileName of candidates) {
        try {
          const raw = await fs.readFile(path.join(logsDir, fileName), "utf8");
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed)) {
            return parsed as unknown[];
          }
        } catch {
          // Try next candidate file.
        }
      }
      return [];
    } catch {
      return [];
    }
  };

  // ── 2. Parallel data fetching ───────────────────────────────────────────────
  const [userProfile, grossDepositResult, rawTrades, openPositions, closedContracts] =
    await Promise.all([
      // A. User profile: balance + fee schedule
      prisma.user.findUnique({
        where: { id: userId },
        select: {
          name:             true,
          email:            true,
          balanceUsdt:      true,
          performanceFeePct: true,
          entryFeePct:      true,
        },
      }),

      // B. Initial deposit: sum of DEPOSIT ledger entries
      prisma.ledgerTransaction.aggregate({
        _sum: { amountUsdt: true },
        where: { userId, type: "DEPOSIT" },
      }),

      // C. Trade history: user_trade_allocations ⟕ master_trades
      //    NUMERIC columns are cast to TEXT in SQL to guarantee string output.
      //    This is the safest way to avoid Prisma.Decimal boundary issues.
      prisma.$queryRaw<TradeQueryRow[]>`
        SELECT
          uta.id::bigint                                AS alloc_id,
          mt.symbol,
          mt.side,
          mt.opened_at,
          mt.closed_at,
          uta.gross_user_pnl_usdt::text                AS gross_user_pnl_usdt,
          uta.admin_fee_usdt::text                     AS admin_fee_usdt,
          uta.user_net_pnl_usdt::text                  AS user_net_pnl_usdt,
          uta.allocated_at,
          COALESCE(uta.broker, 'deriv')                AS broker
        FROM user_trade_allocations uta
        JOIN master_trades mt ON mt.id = uta.master_trade_id
        WHERE uta.user_id = ${userId}::uuid
          AND COALESCE(uta.broker, 'binance') = 'deriv'
        ORDER BY uta.allocated_at DESC
        LIMIT 500
      `.catch(() => [] as TradeQueryRow[]),

      // D. Bot active: check Deriv open contracts on the shared volume.
      readSharedStateArray(["deriv_open_contracts.json", "open_positions.json"]),

      // E. Live closed contracts fallback while webhook allocations catch up.
      readSharedStateArray(["deriv_closed_contracts.json"]),
    ]);

  // ── 3. Guard: user must exist and be active ─────────────────────────────────
  if (!userProfile) {
    redirect("/portal/login");
  }

  // ── 4. Decimal serialisation ────────────────────────────────────────────────
  const balance      = new Prisma.Decimal(userProfile.balanceUsdt);
  const grossDeposit = new Prisma.Decimal(grossDepositResult._sum.amountUsdt ?? 0);
  const entryFeePct  = new Prisma.Decimal(userProfile.entryFeePct ?? 0);
  const netDeposited = grossDeposit.mul(new Prisma.Decimal(1).sub(entryFeePct));
  const perfFeePct   = new Prisma.Decimal(userProfile.performanceFeePct);

  // Net ROI: ((balance - netDeposited) / netDeposited) × 100
  const roi = netDeposited.gt(0)
    ? balance.sub(netDeposited).div(netDeposited).mul(100)
    : new Prisma.Decimal(0);

  // ── 5. Bot status ────────────────────────────────────────────────────────────
  const openContractsCount = Array.isArray(openPositions) ? openPositions.length : 0;
  const isBotActive = openContractsCount > 0;
  const firstPosition = isBotActive
    ? (openPositions[0] as Record<string, unknown>)
    : null;
  const liveSymbol  = typeof firstPosition?.symbol  === "string" ? firstPosition.symbol  : undefined;
  const liveOpenedAt = toIsoDateString(firstPosition?.opened_at ?? firstPosition?.opened_at_ts);

  // ── 6. Trade rows for the table ─────────────────────────────────────────────
  // bot already computes user_net_pnl_usdt correctly (asymmetric fee).
  // We expose the breakdown transparently: gross, fee, net.
  const tradesFromAllocations: TradeRow[] = rawTrades.map((row) => ({
    id:          row.alloc_id.toString(),
    symbol:      row.symbol,
    side:        row.side,
    openedAt:    row.opened_at instanceof Date
                   ? row.opened_at.toISOString()
                   : String(row.opened_at),
    closedAt:    row.closed_at instanceof Date
                   ? row.closed_at.toISOString()
                   : String(row.closed_at),
    allocatedAt: row.allocated_at instanceof Date
                   ? row.allocated_at.toISOString()
                   : String(row.allocated_at),
    grossPnl:    new Prisma.Decimal(row.gross_user_pnl_usdt).toFixed(2),
    adminFee:    new Prisma.Decimal(row.admin_fee_usdt).toFixed(2),
    netPnl:      new Prisma.Decimal(row.user_net_pnl_usdt).toFixed(2),
    broker:      (row.broker ?? "deriv").toLowerCase(),
  }));

  const parsedClosedContracts = Array.isArray(closedContracts)
    ? (closedContracts as ClosedContractFileRow[])
    : [];
  const hasScopedRows = parsedClosedContracts.some((row) => typeof row.user_id === "string");
  const scopedClosedContracts = hasScopedRows
    ? parsedClosedContracts.filter((row) => row.user_id === userId)
    : parsedClosedContracts;

  const fallbackLiveTrades: TradeRow[] = scopedClosedContracts
    .map((row, idx) => {
      const symbol = typeof row.symbol === "string" && row.symbol.trim()
        ? row.symbol
        : "DERIV";
      const closedAt = toIsoDateString(row.closed_at_ts) ?? new Date().toISOString();
      const openedAt = toIsoDateString(row.opened_at_ts) ?? closedAt;

      let pnl = new Prisma.Decimal(0);
      try {
        pnl = new Prisma.Decimal(String(row.realized_pnl_usdt ?? 0));
      } catch {
        pnl = new Prisma.Decimal(0);
      }

      const sideRaw = String(row.side ?? "").toLowerCase();
      const side = sideRaw === "multup" || sideRaw === "buy" ? "buy" : "sell";
      const contractId = String(row.contract_id ?? idx);

      return {
        id: `live-deriv-${contractId}-${idx}`,
        symbol,
        side,
        openedAt,
        closedAt,
        allocatedAt: closedAt,
        grossPnl: pnl.toFixed(2),
        adminFee: "0.00",
        netPnl: pnl.toFixed(2),
        broker: "deriv",
      };
    })
    .sort((a, b) => new Date(b.closedAt).getTime() - new Date(a.closedAt).getTime())
    .slice(0, 500);

  const usingLiveFallback = tradesFromAllocations.length === 0 && fallbackLiveTrades.length > 0;
  const trades: TradeRow[] = usingLiveFallback ? fallbackLiveTrades : tradesFromAllocations;

  // ── 7. Equity curve: running balance from oldest trade to newest ─────────────
  // Reverse to get ASC order for accumulation.
  const tradesAsc = [...trades].reverse();
  const equityPoints: EquityPoint[] = [];

  // Anchor point: initial net deposit (before any trades)
  equityPoints.push({
    date:    grossDeposit.gt(0)
               ? new Date(Date.now() - tradesAsc.length * 86_400_000).toISOString().slice(0, 10)
               : new Date().toISOString().slice(0, 10),
    balance: netDeposited.toFixed(2),
  });

  let running = new Prisma.Decimal(netDeposited);
  for (const t of tradesAsc) {
    running = running.add(new Prisma.Decimal(t.netPnl));
    equityPoints.push({
      date:    t.closedAt.slice(0, 10),
      balance: running.toFixed(2),
    });
  }

  // Deduplicate same-day points (keep last — max per day)
  const dateMap = new Map<string, string>();
  for (const p of equityPoints) dateMap.set(p.date, p.balance);
  const chartPoints: EquityPoint[] = Array.from(dateMap.entries()).map(
    ([date, balance]) => ({ date, balance })
  );

  // ─── Serialised KPI strings ──────────────────────────────────────────────────
  const kpiBalance      = fmtUSDT(balance.toFixed(2));
  const kpiRoi          = (roi.gte(0) ? "+" : "") + roi.toFixed(2) + "%";
  const kpiDeposited    = fmtUSDT(netDeposited.toFixed(2));
  const kpiPerfFee      = (perfFeePct.mul(100).toFixed(1)) + "%";
  const roiPositive     = roi.gte(0);

  // ── Metrics: cumulative profits + total fees paid ───────────────────────────
  // Cumulative Profits = sum of every net-positive allocation (closed wins only).
  // Total Fees Paid    = sum of all admin_fee_usdt entries (Binance fee + perf fee).
  const cumulativeProfits = trades.reduce(
    (acc, t) => {
      const n = new Prisma.Decimal(t.netPnl);
      return n.gt(0) ? acc.add(n) : acc;
    },
    new Prisma.Decimal(0),
  );
  const totalFeesPaid = trades.reduce(
    (acc, t) => acc.add(new Prisma.Decimal(t.adminFee)),
    new Prisma.Decimal(0),
  );
  const kpiCumProfits  = fmtUSDT(cumulativeProfits.toFixed(2));
  const kpiTotalFees   = fmtUSDT(totalFeesPaid.toFixed(2));

  return (
    <div
      style={{
        minHeight: "100vh",
        background:
          `radial-gradient(900px 600px at 8% -8%, rgba(16,185,129,0.09), transparent 60%),` +
          `radial-gradient(900px 600px at 100% 0%, rgba(6,182,212,0.07), transparent 65%),` +
          BG,
        color: TEXT,
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        boxSizing: "border-box",
      }}
    >
      {/* ── Top navigation bar ── */}
      <nav
        style={{
          background: "rgba(4,7,12,0.88)",
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          borderBottom: `1px solid ${BORD}`,
          padding: "0 24px",
          height: 56,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        <span style={{ fontWeight: 800, fontSize: 16, letterSpacing: "-0.02em" }}>
          ◈ OptiFerre
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <LiveBadge active={isBotActive} />
          <span style={{ color: MUTE, fontSize: 12, fontFamily: "ui-monospace, Menlo, monospace" }}>
            {userProfile.name ?? userProfile.email}
          </span>
        </div>
      </nav>

      {/* ── Main content ── */}
      <main style={{ padding: "28px 24px", maxWidth: 1100, margin: "0 auto" }}>

        {/* ── Staggered grid ── */}
        <MotionGrid style={{ display: "flex", flexDirection: "column", gap: 20 }}>

          {/* ── Live Radar ── */}
          <MotionCard>
            <ActiveTradeRadar
              active={isBotActive}
              symbol={liveSymbol}
              openedAt={liveOpenedAt}
            />
          </MotionCard>

          {/* ── KPI hero row ── */}
          <MotionCard>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                gap: 16,
              }}
            >
              {/* Balance hero card */}
              <div
                style={{
                  position: "relative",
                  overflow: "hidden",
                  background: CARD,
                  backdropFilter: "blur(12px)",
                  WebkitBackdropFilter: "blur(12px)",
                  border: `1px solid ${BORD}`,
                  borderLeft: "2px solid #22d3ee",
                  borderRadius: 20,
                  padding: "24px 24px 20px",
                  gridColumn: "span 2",
                  minWidth: 0,
                  boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px rgba(34,211,238,0.35)",
                }}
              >
                <div style={{ position: "absolute", top: -30, right: -30, width: 120, height: 120, borderRadius: "50%", background: "radial-gradient(circle, rgba(34,211,238,0.13), transparent 70%)", pointerEvents: "none" }} />
                <p style={{ position: "relative", color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 10, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  Balance Actual
                </p>
                <p style={{ position: "relative", color: "#22d3ee", fontSize: 36, fontWeight: 800, fontFamily: "ui-monospace, Menlo, monospace", letterSpacing: "-0.03em", marginBottom: 4, textShadow: "0 0 14px rgba(34,211,238,0.5)", lineHeight: 1.05 }}>
                  {kpiBalance}
                </p>
                <p style={{ position: "relative", color: MUTE, fontSize: 12 }}>
                  Capital neto inicial:{" "}
                  <span style={{ color: TEXT }}>{kpiDeposited}</span>
                </p>
              </div>

              {/* ROI card */}
              <div
                style={{
                  position: "relative",
                  overflow: "hidden",
                  background: CARD,
                  backdropFilter: "blur(12px)",
                  WebkitBackdropFilter: "blur(12px)",
                  border: `1px solid ${BORD}`,
                  borderLeft: `2px solid ${roiPositive ? "#10b981" : "#fb7185"}`,
                  borderRadius: 20,
                  padding: "24px 24px 20px",
                  boxShadow: `0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px ${roiPositive ? "rgba(16,185,129,0.35)" : "rgba(251,113,133,0.35)"}`,
                }}
              >
                <div style={{ position: "absolute", top: -30, right: -30, width: 100, height: 100, borderRadius: "50%", background: `radial-gradient(circle, ${roiPositive ? "rgba(16,185,129,0.13)" : "rgba(251,113,133,0.13)"}, transparent 70%)`, pointerEvents: "none" }} />
                <p style={{ position: "relative", color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 10, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  Retorno Neto Total
                </p>
                <p style={{ position: "relative", color: roiPositive ? "#34d399" : "#fb7185", fontSize: 32, fontWeight: 800, fontFamily: "ui-monospace, Menlo, monospace", letterSpacing: "-0.03em", marginBottom: 4, textShadow: `0 0 14px ${roiPositive ? "rgba(52,211,153,0.5)" : "rgba(251,113,133,0.5)"}`, lineHeight: 1.05 }}>
                  {kpiRoi}
                </p>
                <p style={{ position: "relative", color: MUTE, fontSize: 12 }}>
                  Comisión de gestión: <span style={{ color: TEXT }}>{kpiPerfFee}</span> sobre ganancias
                </p>
              </div>

              {/* Trades count card */}
              <div
                style={{
                  position: "relative",
                  overflow: "hidden",
                  background: CARD,
                  backdropFilter: "blur(12px)",
                  WebkitBackdropFilter: "blur(12px)",
                  border: `1px solid ${BORD}`,
                  borderLeft: "2px solid #c084fc",
                  borderRadius: 20,
                  padding: "24px 24px 20px",
                  boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px rgba(192,132,252,0.35)",
                }}
              >
                <div style={{ position: "absolute", top: -30, right: -30, width: 100, height: 100, borderRadius: "50%", background: "radial-gradient(circle, rgba(192,132,252,0.13), transparent 70%)", pointerEvents: "none" }} />
                <p style={{ position: "relative", color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 10, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  Operaciones Cerradas
                </p>
                <p style={{ position: "relative", color: "#c084fc", fontSize: 32, fontWeight: 800, fontFamily: "ui-monospace, Menlo, monospace", letterSpacing: "-0.03em", marginBottom: 4, textShadow: "0 0 14px rgba(192,132,252,0.5)", lineHeight: 1.05 }}>
                  {trades.length}
                </p>
                <p style={{ position: "relative", color: MUTE, fontSize: 12 }}>
                  Deriv cerradas · Abiertas ahora: {openContractsCount}
                  {usingLiveFallback ? " · modo reconciliacion en vivo" : ""}
                </p>
              </div>

              {/* ── Cumulative Profits card ── */}
              {/* Sum of every net-positive allocation in absolute USDT. */}
              <div
                style={{
                  position: "relative",
                  overflow: "hidden",
                  background: CARD,
                  backdropFilter: "blur(12px)",
                  WebkitBackdropFilter: "blur(12px)",
                  border: `1px solid ${BORD}`,
                  borderLeft: `2px solid #10b981`,
                  borderRadius: 20,
                  padding: "24px 24px 20px",
                  boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px rgba(16,185,129,0.35)",
                }}
              >
                <div style={{ position: "absolute", top: -30, right: -30, width: 100, height: 100, borderRadius: "50%", background: "radial-gradient(circle, rgba(16,185,129,0.15), transparent 70%)", pointerEvents: "none" }} />
                <p style={{ position: "relative", color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 10, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  Ganancias Acumuladas
                </p>
                <p style={{ position: "relative", color: "#34d399", fontSize: 32, fontWeight: 800, fontFamily: "ui-monospace, Menlo, monospace", letterSpacing: "-0.03em", marginBottom: 4, textShadow: "0 0 14px rgba(52,211,153,0.5)", lineHeight: 1.05 }}>
                  {kpiCumProfits}
                </p>
                <p style={{ position: "relative", color: MUTE, fontSize: 12 }}>
                  Solo operaciones ganadoras (neto post-comisión)
                </p>
              </div>

              {/* ── Total Fees Paid card ── */}
              {/* adminFee = binanceFee + performanceFee across all trades. */}
              <div
                style={{
                  position: "relative",
                  overflow: "hidden",
                  background: CARD,
                  backdropFilter: "blur(12px)",
                  WebkitBackdropFilter: "blur(12px)",
                  border: `1px solid ${BORD}`,
                  borderLeft: "2px solid #fbbf24",
                  borderRadius: 20,
                  padding: "24px 24px 20px",
                  boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px rgba(251,191,36,0.3)",
                }}
              >
                <div style={{ position: "absolute", top: -30, right: -30, width: 100, height: 100, borderRadius: "50%", background: "radial-gradient(circle, rgba(251,191,36,0.15), transparent 70%)", pointerEvents: "none" }} />
                <p style={{ position: "relative", color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 10, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  Comisiones Pagadas
                </p>
                <p style={{ position: "relative", color: "#fbbf24", fontSize: 32, fontWeight: 800, fontFamily: "ui-monospace, Menlo, monospace", letterSpacing: "-0.03em", marginBottom: 4, textShadow: "0 0 14px rgba(251,191,36,0.5)", lineHeight: 1.05 }}>
                  {kpiTotalFees}
                </p>
                <p style={{ position: "relative", color: MUTE, fontSize: 12 }}>
                  Execution fee + comisión de rendimiento
                </p>
              </div>
            </div>
          </MotionCard>

          {/* ── Equity curve ── */}
          {chartPoints.length > 1 && (
            <MotionCard>
              <div
                style={{
                  position: "relative",
                  overflow: "hidden",
                  background: CARD,
                  backdropFilter: "blur(12px)",
                  WebkitBackdropFilter: "blur(12px)",
                  border: `1px solid ${BORD}`,
                  borderLeft: "2px solid #06b6d4",
                  borderRadius: 20,
                  padding: "24px 24px 16px",
                  boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px rgba(6,182,212,0.35)",
                }}
              >
                <p style={{ color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 20, fontFamily: "ui-monospace, Menlo, monospace" }}>
                  Curva de Capital
                </p>
                <EquityCurveChart points={chartPoints} />
              </div>
            </MotionCard>
          )}

          {/* ── Trade history ── */}
          <MotionCard>
            <div
              style={{
                position: "relative",
                overflow: "hidden",
                background: CARD,
                backdropFilter: "blur(12px)",
                WebkitBackdropFilter: "blur(12px)",
                border: `1px solid ${BORD}`,
                borderLeft: "2px solid rgba(99,135,178,0.5)",
                borderRadius: 20,
                padding: "24px 24px 8px",
                marginBottom: 80,
                boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset",
              }}
            >
              <p style={{ color: MUTE, fontSize: 9.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 20, fontFamily: "ui-monospace, Menlo, monospace" }}>
                Historial de Operaciones
              </p>
              {usingLiveFallback && (
                <p style={{ color: "#fbbf24", fontSize: 11, marginTop: -8, marginBottom: 14 }}>
                  Mostrando cierres Deriv en vivo mientras finaliza la conciliacion por usuario.
                </p>
              )}
              <TradeHistoryTable trades={trades} />
            </div>
          </MotionCard>

        </MotionGrid>

      </main>

      {/* ── Support FAB (client component) ── */}
      <SupportWidget />
    </div>
  );
}
// ─── Live status badge (server-rendered, no interactivity) ───────────────────
function LiveBadge({ active }: { active: boolean }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        background: active ? `${GREEN}14` : `${MUTE}14`,
        border: `1px solid ${active ? `${GREEN}44` : `${MUTE}33`}`,
        borderRadius: 20,
        padding: "4px 12px 4px 8px",
        fontSize: 11,
        fontWeight: 600,
        color: active ? GREEN : MUTE,
        letterSpacing: "0.04em",
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: active ? GREEN : MUTE,
          flexShrink: 0,
          boxShadow: active ? `0 0 6px ${GREEN}` : "none",
          animation: active ? "pulse 2s infinite" : "none",
        }}
      />
      {active ? "Operación en Curso" : "Buscando Oportunidades 📡"}
    </span>
  );
}
