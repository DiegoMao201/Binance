/**
 * POST /api/webhooks/trade-closed
 *
 * ─── PAMM Allocation Engine ──────────────────────────────────────────────────
 *
 * Called by the Python trading bot after every trade closes. Performs the full
 * PAMM (Percentage Allocation Management Module) distribution atomically:
 *
 *   1. Auth guard — Bearer <WEBHOOK_SECRET> required.
 *   2. Zod validation — typed payload from the Python bot.
 *   3. Prisma $transaction — for every active CLIENT:
 *        gross_pnl   = balance_before × pnl_pct          (can be negative)
 *        commission  = balance_before × fees_pct          (always ≥ 0, recovered by admin)
 *        perf_fee    = max(gross_pnl, 0) × performance_fee_pct  (5%, only on wins)
 *        admin_fee   = commission + perf_fee
 *        net_pnl     = gross_pnl − admin_fee              (credited to client)
 *        new_balance = balance_before + net_pnl
 *
 *      Writes atomically:
 *        • users.balance_usdt          ← new_balance
 *        • user_trade_allocations      ← immutable allocation record
 *        • ledger_transactions         ← PERFORMANCE_FEE row (if fee > 0)
 *        • ledger_transactions         ← BINANCE_COMMISSION row (always)
 *
 *   4. Fire-and-forget email to clients whose net_pnl > 0.
 *
 * Rule: The bot NEVER waits for email delivery. HTTP 200 is returned as soon
 * as the DB transaction commits, typically < 300 ms.
 *
 * Security:
 *   • Timing-safe token comparison via crypto.timingSafeEqual.
 *   • Returns 401 on any auth failure without leaking which check failed.
 */

import { NextResponse, type NextRequest } from "next/server";
import { timingSafeEqual, createHash } from "crypto";
import { z } from "zod";
import { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { sendPammAllocationEmail } from "@/lib/email";

// ─── Performance fee constant ─────────────────────────────────────────────────
// Matches the default in prisma/schema.prisma (User.performanceFeePct = 0.05).
// Per-user fee schedules are read from the DB; this constant is only used to
// label the ledger row description.
const PERF_FEE_LABEL = "5% performance fee";

// ─── Payload schema ───────────────────────────────────────────────────────────
const TradeClosedSchema = z.object({
  /** Trading pair, e.g. "SOL/USDT". */
  symbol: z.string().min(3).max(32),
  /**
   * Net P&L as a decimal fraction of the position size.
   * e.g. 0.015 = +1.5%   |  -0.008 = -0.8%
   */
  pnl_pct: z.number(),
  /** "BUY" | "SELL" (Binance Spot convention). */
  side: z.string().min(2).max(8),
  /** Human-readable close trigger, e.g. "trailing_stop". */
  exit_reason: z.string().min(1).max(64).default("unknown"),
  /**
   * Binance commission as a fraction of position notional.
   * e.g. 0.001 = 0.1% standard maker/taker fee.
   * Defaults to 0.001 when not supplied.
   */
  fees_pct: z.number().min(0).max(0.02).default(0.001),
});

type TradeClosedPayload = z.infer<typeof TradeClosedSchema>;

// ─── Per-client allocation result (for email dispatch) ────────────────────────
interface ClientAllocation {
  userId:     string;
  email:      string;
  name:       string | null;
  symbol:     string;
  netPnlUsdt: Prisma.Decimal;
  balanceBefore: Prisma.Decimal;
  balanceAfter:  Prisma.Decimal;
}

// ─── Auth helper ──────────────────────────────────────────────────────────────
/** Constant-time token comparison — prevents timing-oracle attacks. */
function verifyBearer(authHeader: string, secret: string): boolean {
  const parts = authHeader.split(" ");
  if (parts.length !== 2 || parts[0] !== "Bearer") return false;
  try {
    // Pad both to the same length before comparing to avoid length leaks.
    const a = createHash("sha256").update(parts[1]).digest();
    const b = createHash("sha256").update(secret).digest();
    return timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

// ─── Route handler ────────────────────────────────────────────────────────────
export async function POST(request: NextRequest): Promise<NextResponse> {
  // ── 1. Auth: constant-time Bearer token guard ──────────────────────────────
  const webhookSecret = process.env.WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.error("[pamm-webhook] WEBHOOK_SECRET env var is not set — fail closed.");
    return NextResponse.json({ error: "Server misconfiguration." }, { status: 500 });
  }

  const authHeader = request.headers.get("authorization") ?? "";
  if (!verifyBearer(authHeader, webhookSecret)) {
    return NextResponse.json({ error: "Unauthorized." }, { status: 401 });
  }

  // ── 2. Parse + validate body ───────────────────────────────────────────────
  let rawBody: unknown;
  try {
    rawBody = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body." }, { status: 400 });
  }

  const parsed = TradeClosedSchema.safeParse(rawBody);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid payload.", details: parsed.error.flatten() },
      { status: 422 },
    );
  }
  const payload: TradeClosedPayload = parsed.data;

  // ── 3. PAMM Allocation Transaction ────────────────────────────────────────
  // All Decimal arithmetic uses Prisma.Decimal (decimal.js) for exact
  // fixed-point math. No `number` type is used for monetary values.
  let allocations: ClientAllocation[];
  try {
    allocations = await runPammTransaction(payload);
  } catch (err) {
    console.error("[pamm-webhook] Transaction failed:", err);
    return NextResponse.json({ error: "PAMM transaction failed." }, { status: 500 });
  }

  console.info(
    "[pamm-webhook] Allocated %s (%s%%) to %d clients.",
    payload.symbol,
    (payload.pnl_pct * 100).toFixed(4),
    allocations.length,
  );

  // ── 4. Fire-and-forget emails for net-positive clients ─────────────────────
  dispatchWinEmails(allocations, payload.symbol).catch((err) =>
    console.error("[pamm-webhook] Email dispatch error:", err),
  );

  return NextResponse.json({
    success:    true,
    symbol:     payload.symbol,
    pnl_pct:    payload.pnl_pct,
    fees_pct:   payload.fees_pct,
    allocated:  allocations.length,
  });
}

// ─── PAMM Transaction ─────────────────────────────────────────────────────────
/**
 * Executes the full PAMM allocation atomically.
 *
 * Uses Prisma.$transaction with a Prisma.Decimal-only arithmetic pipeline.
 * If the server crashes mid-transaction, Postgres rolls back automatically —
 * no client balance is partially updated.
 *
 * @returns The list of per-client allocation results (for email dispatch).
 */
async function runPammTransaction(
  payload: TradeClosedPayload,
): Promise<ClientAllocation[]> {
  const pnlPct  = new Prisma.Decimal(payload.pnl_pct);
  const feesPct = new Prisma.Decimal(payload.fees_pct);

  // Fetch all active clients with their individual fee schedules.
  const clients = await prisma.user.findMany({
    where:  { role: "client", isActive: true },
    select: { id: true, email: true, name: true, balanceUsdt: true, performanceFeePct: true },
  });

  if (clients.length === 0) {
    console.info("[pamm-webhook] No active clients — skipping allocation.");
    return [];
  }

  // ── Build all DB operations in Decimal arithmetic ──────────────────────────
  type AllocationOp = {
    client:      typeof clients[number];
    grossPnl:    Prisma.Decimal;
    commission:  Prisma.Decimal;
    perfFee:     Prisma.Decimal;
    adminFee:    Prisma.Decimal;
    netPnl:      Prisma.Decimal;
    newBalance:  Prisma.Decimal;
  };

  const ops: AllocationOp[] = clients.map((client) => {
    const balance    = new Prisma.Decimal(client.balanceUsdt);
    const perfFeePct = new Prisma.Decimal(client.performanceFeePct);

    // ── PAMM Math ────────────────────────────────────────────────────────────
    // grossPnl can be negative (loss trade).
    const grossPnl   = balance.mul(pnlPct);
    // Commission is always positive — admin recovers Binance overhead per trade.
    const commission = balance.mul(feesPct).abs();
    // Performance fee is charged ONLY on wins. Zero on losses.
    const perfFee    = grossPnl.gt(0) ? grossPnl.mul(perfFeePct) : new Prisma.Decimal(0);
    const adminFee   = commission.add(perfFee);
    const netPnl     = grossPnl.sub(adminFee);
    const newBalance = balance.add(netPnl);

    return { client, grossPnl, commission, perfFee, adminFee, netPnl, newBalance };
  });

  // ── Execute atomically ─────────────────────────────────────────────────────
  await prisma.$transaction(
    ops.flatMap(({ client, grossPnl, commission, perfFee, adminFee, netPnl, newBalance }) => {
      const base = {
        userId:      client.id,
        symbol:      payload.symbol,
        side:        payload.side,
        exitReason:  payload.exit_reason,
        pnlPct:      pnlPct,
        grossPnlUsdt:   grossPnl,
        commissionUsdt: commission,
        perfFeeUsdt:    perfFee,
        adminFeeUsdt:   adminFee,
        netPnlUsdt:     netPnl,
        balanceBefore:  new Prisma.Decimal(client.balanceUsdt),
        balanceAfter:   newBalance,
      };

      const writes: Prisma.PrismaPromise<unknown>[] = [
        // 1. Update client balance.
        prisma.user.update({
          where: { id: client.id },
          data:  { balanceUsdt: newBalance },
        }),
        // 2. Immutable allocation record.
        prisma.userTradeAllocation.create({ data: base }),
        // 3. Binance commission ledger row (always, even on losses).
        prisma.ledgerTransaction.create({
          data: {
            userId:      client.id,
            type:        "BINANCE_COMMISSION",
            amountUsdt:  commission,
            description: `Binance commission share — ${payload.symbol} (${(payload.fees_pct * 100).toFixed(3)}% of position)`,
          },
        }),
      ];

      // 4. Performance fee ledger row — only when a fee was charged.
      if (perfFee.gt(0)) {
        writes.push(
          prisma.ledgerTransaction.create({
            data: {
              userId:      client.id,
              type:        "PERFORMANCE_FEE",
              amountUsdt:  perfFee,
              description: `${PERF_FEE_LABEL} — ${payload.symbol} gross +${grossPnl.toFixed(4)} USDT`,
            },
          }),
        );
      }

      return writes;
    }),
  );

  // Return allocation results for email dispatch.
  return ops.map(({ client, netPnl, newBalance }) => ({
    userId:        client.id,
    email:         client.email,
    name:          client.name,
    symbol:        payload.symbol,
    netPnlUsdt:    netPnl,
    balanceBefore: new Prisma.Decimal(client.balanceUsdt),
    balanceAfter:  newBalance,
  }));
}

// ─── Fire-and-forget email dispatch ───────────────────────────────────────────
/**
 * Sends "trade closed — WIN" emails to every client whose net PnL > 0.
 * Each send is individually wrapped so one failure does not block the rest.
 * This function is intentionally NOT awaited by the route handler.
 */
async function dispatchWinEmails(
  allocations: ClientAllocation[],
  symbol: string,
): Promise<void> {
  const winners = allocations.filter((a) => a.netPnlUsdt.gt(0));
  if (winners.length === 0) return;

  console.info(
    "[pamm-webhook] Dispatching win emails for %s to %d client(s).",
    symbol,
    winners.length,
  );

  const results = await Promise.allSettled(
    winners.map((a) =>
      sendPammAllocationEmail(
        a.email,
        a.name,
        a.symbol,
        parseFloat(a.netPnlUsdt.toFixed(4)),
        parseFloat(a.balanceAfter.toFixed(2)),
      ),
    ),
  );

  let sent = 0;
  let failed = 0;
  for (const r of results) {
    if (r.status === "fulfilled") { sent++; }
    else { failed++; console.error("[pamm-webhook] Email failed:", r.reason); }
  }
  console.info("[pamm-webhook] Emails: %d sent, %d failed.", sent, failed);
}
