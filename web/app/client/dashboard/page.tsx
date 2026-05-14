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
};

// ─── Colour tokens (Server-side layout) ──────────────────────────────────────
const BG    = "#080e16";
const CARD  = "#0a1018";
const BORD  = "#1a2b3c";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#12d98b";
const RED   = "#eb4b61";
const BLUE  = "#57c1ff";

function fmtUSDT(v: string, showSign = false): string {
  const n = Number(v);
  const sign = showSign && n > 0 ? "+" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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

  // ── 2. Parallel data fetching ───────────────────────────────────────────────
  const [userProfile, grossDepositResult, rawTrades, openPositions] =
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
          uta.allocated_at
        FROM user_trade_allocations uta
        JOIN master_trades mt ON mt.id = uta.master_trade_id
        WHERE uta.user_id = ${userId}::uuid
        ORDER BY uta.allocated_at DESC
        LIMIT 500
      `.catch(() => [] as TradeQueryRow[]),

      // D. Bot active: check open_positions.json on the shared volume.
      //    Returns [] on any read/parse error (safe fallback).
      (async () => {
        try {
          const logsDir = process.env.BOT_STATE_DIR
            ?? path.join(process.cwd(), "..", "logs");
          const raw = await fs.readFile(path.join(logsDir, "open_positions.json"), "utf8");
          return JSON.parse(raw) as unknown[];
        } catch {
          return [] as unknown[];
        }
      })(),
    ]);

  // ── 3. Guard: user must exist and be active ─────────────────────────────────
  if (!userProfile) {
    redirect("/portal/login");
  }

  // ── 4. Decimal serialisation ────────────────────────────────────────────────
  const balance      = new Prisma.Decimal(userProfile.balanceUsdt);
  const grossDeposit = new Prisma.Decimal(grossDepositResult._sum.amountUsdt ?? 0);
  const netDeposited = grossDeposit.mul("0.98"); // entry fee already deducted
  const perfFeePct   = new Prisma.Decimal(userProfile.performanceFeePct);

  // Net ROI: ((balance - netDeposited) / netDeposited) × 100
  const roi = netDeposited.gt(0)
    ? balance.sub(netDeposited).div(netDeposited).mul(100)
    : new Prisma.Decimal(0);

  // ── 5. Bot status ────────────────────────────────────────────────────────────
  const isBotActive = Array.isArray(openPositions) && openPositions.length > 0;

  // ── 6. Trade rows for the table ─────────────────────────────────────────────
  // bot already computes user_net_pnl_usdt correctly (asymmetric fee).
  // We expose the breakdown transparently: gross, fee, net.
  const trades: TradeRow[] = rawTrades.map((row) => ({
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
  }));

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

  return (
    <div
      style={{
        minHeight: "100vh",
        background: BG,
        color: TEXT,
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace",
        boxSizing: "border-box",
      }}
    >
      {/* ── Top navigation bar ── */}
      <nav
        style={{
          background: CARD,
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
        <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: "-0.02em" }}>
          ◈ OptiFerre
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <LiveBadge active={isBotActive} />
          <span style={{ color: MUTE, fontSize: 12 }}>
            {userProfile.name ?? userProfile.email}
          </span>
        </div>
      </nav>

      {/* ── Main content ── */}
      <main style={{ padding: "28px 24px", maxWidth: 1100, margin: "0 auto" }}>

        {/* ── KPI hero row ── */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: 16,
            marginBottom: 28,
          }}
        >
          {/* Balance hero card */}
          <div
            style={{
              background: CARD,
              border: `1px solid ${BORD}`,
              borderRadius: 20,
              padding: "24px 24px 20px",
              gridColumn: "span 2",
              borderLeft: `3px solid ${GREEN}`,
              minWidth: 0,
            }}
          >
            <p style={{ color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 10 }}>
              Balance Actual
            </p>
            <p style={{ color: TEXT, fontSize: 36, fontWeight: 800, fontFamily: "monospace", letterSpacing: "-0.03em", marginBottom: 4 }}>
              {kpiBalance}
            </p>
            <p style={{ color: MUTE, fontSize: 12 }}>
              Capital neto inicial:{" "}
              <span style={{ color: TEXT }}>{kpiDeposited}</span>
            </p>
          </div>

          {/* ROI card */}
          <div
            style={{
              background: CARD,
              border: `1px solid ${BORD}`,
              borderRadius: 20,
              padding: "24px 24px 20px",
              borderLeft: `3px solid ${roiPositive ? GREEN : RED}`,
            }}
          >
            <p style={{ color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 10 }}>
              Retorno Neto Total
            </p>
            <p
              style={{
                color: roiPositive ? GREEN : RED,
                fontSize: 32,
                fontWeight: 800,
                fontFamily: "monospace",
                letterSpacing: "-0.03em",
                marginBottom: 4,
              }}
            >
              {kpiRoi}
            </p>
            <p style={{ color: MUTE, fontSize: 12 }}>
              Comisión de gestión: <span style={{ color: TEXT }}>{kpiPerfFee}</span> sobre ganancias
            </p>
          </div>

          {/* Trades count card */}
          <div
            style={{
              background: CARD,
              border: `1px solid ${BORD}`,
              borderRadius: 20,
              padding: "24px 24px 20px",
              borderLeft: `3px solid ${BLUE}`,
            }}
          >
            <p style={{ color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 10 }}>
              Operaciones
            </p>
            <p style={{ color: TEXT, fontSize: 32, fontWeight: 800, fontFamily: "monospace", letterSpacing: "-0.03em", marginBottom: 4 }}>
              {trades.length}
            </p>
            <p style={{ color: MUTE, fontSize: 12 }}>
              Historial completo de asignaciones
            </p>
          </div>
        </div>

        {/* ── Equity curve ── */}
        {chartPoints.length > 1 && (
          <div
            style={{
              background: CARD,
              border: `1px solid ${BORD}`,
              borderRadius: 20,
              padding: "24px 24px 16px",
              marginBottom: 24,
            }}
          >
            <p style={{ color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 20 }}>
              Curva de Capital
            </p>
            <EquityCurveChart points={chartPoints} />
          </div>
        )}

        {/* ── Trade history ── */}
        <div
          style={{
            background: CARD,
            border: `1px solid ${BORD}`,
            borderRadius: 20,
            padding: "24px 24px 8px",
          }}
        >
          <p style={{ color: MUTE, fontSize: 10, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", marginBottom: 20 }}>
            Historial de Operaciones
          </p>
          <TradeHistoryTable trades={trades} />
        </div>

      </main>
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
