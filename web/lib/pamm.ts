/**
 * lib/pamm.ts — PAMM Aggregation Engine
 *
 * All monetary values are computed with Prisma.Decimal (decimal.js under the
 * hood) for exact arbitrary-precision arithmetic. They are converted to
 * fixed-precision strings BEFORE leaving this module so that Server Components
 * can safely pass them as props to Client Components (JSON-serializable).
 *
 * RULE: No `number` type is used for monetary values. Only `Decimal` →
 *       serialised as `string` via `.toFixed(2)`.
 */

import { prisma } from "@/lib/db";
import { Prisma } from "@prisma/client";

// ─── Public serialisable types ────────────────────────────────────────────────
// These types are safe to pass as Next.js Server → Client Component props.

export type InvestorRow = {
  id: string;
  name: string;
  email: string;
  /** Gross amount the client wired in (before entry fee). */
  grossDeposit: string;
  /** Net capital that entered trading (grossDeposit × 0.98). */
  netDeposited: string;
  /** Current balance_usdt from the users table. */
  balance: string;
  /** Net ROI % relative to netDeposited. Positive or negative. */
  roi: string;
};

export type AdminStats = {
  /** Sum of all active client balances. */
  aum: string;
  /** Sum of all ENTRY_FEE ledger rows. */
  entryFeeRevenue: string;
  /** 5% of every positive net_pnl_usdt allocation, per user's fee schedule. */
  performanceFeeRevenue: string;
  /** entryFeeRevenue + performanceFeeRevenue. */
  totalRevenue: string;
  activeClients: number;
  investors: InvestorRow[];
};

// ─── Internal raw-query types ─────────────────────────────────────────────────
type PerfFeeRow = { perf_fees: Prisma.Decimal | null };

type InvestorQueryRow = {
  id: string;
  name: string | null;
  email: string;
  balance_usdt: Prisma.Decimal;
  gross_deposit: Prisma.Decimal;
};

// ─── Main function ────────────────────────────────────────────────────────────

/**
 * Fetches all admin KPIs and the full investor grid in a single round-trip
 * using parallel Prisma queries. All Decimal values are serialised to strings.
 *
 * Throws on unrecoverable DB errors; callers (Server Components) can wrap in
 * try/catch and render an error boundary.
 */
export async function getAdminStats(): Promise<AdminStats> {
  // ── 1. AUM + active-client count (Prisma aggregate — type-safe) ─────────────
  const [aumResult, activeClients] = await Promise.all([
    prisma.user.aggregate({
      _sum: { balanceUsdt: true },
      where: { role: "client", isActive: true },
    }),
    prisma.user.count({
      where: { role: "client", isActive: true },
    }),
  ]);

  // ── 2. Entry-fee revenue (Prisma aggregate — type-safe) ─────────────────────
  const entryFeeResult = await prisma.ledgerTransaction.aggregate({
    _sum: { amountUsdt: true },
    where: { type: "ENTRY_FEE" },
  });

  // ── 3. Performance-fee revenue (raw SQL — reads user_trade_allocations) ──────
  // Sums the pre-calculated perf_fee_usdt stored by the PAMM webhook.
  // RULE: Read-only here. The PAMM webhook is the sole writer.
  const perfFeeRevenue: Prisma.Decimal = await prisma
    .$queryRaw<PerfFeeRow[]>`
      SELECT
        COALESCE(SUM(uta.perf_fee_usdt), 0)::numeric AS perf_fees
      FROM user_trade_allocations uta
      WHERE uta.perf_fee_usdt > 0
    `
    .then((rows) => new Prisma.Decimal(rows[0]?.perf_fees ?? 0))
    .catch(() => new Prisma.Decimal(0)); // table is empty on a fresh deploy

  // ── 4. Investor grid (raw SQL for the DEPOSIT aggregate per user) ────────────
  const investorRows = await prisma.$queryRaw<InvestorQueryRow[]>`
    SELECT
      u.id::text,
      u.display_name AS name,
      u.email,
      u.balance_usdt,
      COALESCE(
        SUM(lt.amount_usdt) FILTER (WHERE lt.type = 'DEPOSIT'),
        0
      )::numeric AS gross_deposit
    FROM users u
    LEFT JOIN ledger_transactions lt ON lt.user_id = u.id
    WHERE u.role = 'client'
    GROUP BY u.id, u.display_name, u.email, u.balance_usdt
    ORDER BY u.created_at DESC
  `.catch(() => [] as InvestorQueryRow[]);

  // ── Decimal arithmetic ────────────────────────────────────────────────────────
  const aum       = new Prisma.Decimal(aumResult._sum.balanceUsdt       ?? 0);
  const entryFees = new Prisma.Decimal(entryFeeResult._sum.amountUsdt   ?? 0);
  const totalRev  = entryFees.add(perfFeeRevenue);

  const investors: InvestorRow[] = investorRows.map((row) => {
    const balance      = new Prisma.Decimal(row.balance_usdt);
    const gross        = new Prisma.Decimal(row.gross_deposit);
    // Net invested = what actually went into the trading account (98% of gross)
    const netDeposited = gross.mul("0.98");
    // ROI relative to net invested capital; 0 when no deposit yet
    const roi = netDeposited.gt(0)
      ? balance.sub(netDeposited).div(netDeposited).mul(100)
      : new Prisma.Decimal(0);

    return {
      id:           row.id,
      name:         row.name ?? "—",
      email:        row.email,
      grossDeposit: gross.toFixed(2),
      netDeposited: netDeposited.toFixed(2),
      balance:      balance.toFixed(2),
      roi:          roi.toFixed(2),
    };
  });

  return {
    aum:                    aum.toFixed(2),
    entryFeeRevenue:        entryFees.toFixed(2),
    performanceFeeRevenue:  perfFeeRevenue.toFixed(2),
    totalRevenue:           totalRev.toFixed(2),
    activeClients,
    investors,
  };
}
