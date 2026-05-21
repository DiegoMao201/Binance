/**
 * POST /api/webhooks/trade-closed
 *
 * ─── PAMM Allocation Engine v2 — Asymmetric Single-Client ────────────────────
 *
 * Called by the Python trading bot after every trade closes. Performs an
 * exact, auditable PAMM distribution for one specific client atomically.
 *
 * ── Business Rules ─────────────────────────────────────────────────────────
 *
 *   Input:  tradeId, userId, rawPnl (USDT), binanceFee (USDT), symbol,
 *           side, exitReason
 *
 *   Step 1 — Idempotency guard:
 *     If (tradeId, userId) already exists in user_trade_allocations →
 *     return 200 OK { status: "skipped", reason: "duplicate" }.
 *     This prevents double-charging/crediting on network retries.
 *
 *   Step 2 — PAMM Math (Decimal-only, no floats):
 *     netBaseline     = rawPnl − binanceFee
 *     performanceFee  = netBaseline > 0  ?  netBaseline × userPerfFeePct  : 0
 *     clientNetShare  = rawPnl − binanceFee − performanceFee
 *                     = netBaseline × (1 − userPerfFeePct)   [if WIN]
 *                     = netBaseline                           [if LOSS]
 *     adminTotalShare = binanceFee + performanceFee
 *
 *   Step 3 — ACID $transaction:
 *     (a) users.balance_usdt  += clientNetShare        (client row)
 *     (b) users.balance_usdt  += adminTotalShare       (admin row)
 *     (c) user_trade_allocations  ← allocation record
 *     (d) ledger_transactions ← TRADE_PNL             (client)
 *     (e) ledger_transactions ← BINANCE_FEE_REIMBURSEMENT  (client)
 *     (f) ledger_transactions ← PERFORMANCE_FEE       (client, only on wins)
 *
 * Security:
 *   • Timing-safe Bearer token via crypto.timingSafeEqual.
 *   • Returns 401 on auth failure without leaking which check failed.
 *   • Idempotency prevents replay-attack double-credits.
 */

import { NextResponse, type NextRequest } from "next/server";
import { timingSafeEqual, createHash } from "crypto";
import { z } from "zod";
import { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { sendPammAllocationEmail } from "@/lib/email";

// ─── Payload schema ───────────────────────────────────────────────────────────
const TradeClosedSchema = z.object({
  /** Bot-generated opaque trade identifier. Used for idempotency. */
  tradeId: z.string().min(1).max(128),
  /** UUID of the client user this allocation belongs to. */
  userId: z.string().uuid(),
  /**
   * Raw (gross) PnL in absolute USDT.
   * Positive = WIN, negative = LOSS. Precision up to 8 decimal places.
   */
  rawPnl: z.number(),
  /**
   * Binance execution fee in absolute USDT (always >= 0).
   * Admin paid this upfront at the exchange level; it is deducted from the
   * client and reimbursed to the admin on every trade, WIN or LOSS.
   */
  binanceFee: z.number().min(0),
  /** Trading pair, e.g. "SOL/USDT". */
  symbol: z.string().min(3).max(32),
  /** "BUY" | "SELL" (Binance Spot convention). */
  side: z.string().min(2).max(8),
  /** Human-readable close trigger, e.g. "trailing_stop". */
  exitReason: z.string().min(1).max(64).default("unknown"),
  /**
   * Originating broker. Defaults to 'binance' for backwards compatibility
   * with the existing Binance Spot pipeline. The Deriv async daemon sends
   * 'deriv' so the audit trail can attribute PnL per broker.
   */
  broker: z.enum(["binance", "deriv"]).default("binance"),
});

type TradeClosedPayload = z.infer<typeof TradeClosedSchema>;

// ─── Auth helper ──────────────────────────────────────────────────────────────
/** Constant-time token comparison — prevents timing-oracle attacks. */
function verifyBearer(authHeader: string, secret: string): boolean {
  const parts = authHeader.split(" ");
  if (parts.length !== 2 || parts[0] !== "Bearer") return false;
  try {
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
  const p: TradeClosedPayload = parsed.data;

  // ── 2b. Ghost / zero-allocation short-circuit ──────────────────────────────
  // Reconciliation-purged or otherwise zero-money trades have no PAMM math to
  // perform: rawPnl=0 and binanceFee=0 means every allocation slice is 0, so
  // the transaction would only burn DB locks and emit empty ledger rows. We
  // acknowledge with 200 so the Python bot does not retry/log warnings.
  if (
    p.exitReason === "lost_or_ghost_closed" &&
    Math.abs(p.rawPnl) < 1e-9 &&
    Math.abs(p.binanceFee) < 1e-9
  ) {
    return NextResponse.json(
      { status: "skipped", reason: "ghost_zero_allocation", tradeId: p.tradeId },
      { status: 200 },
    );
  }

  // ── 3. Idempotency guard ───────────────────────────────────────────────────
  // Check BEFORE opening the transaction to avoid unnecessary DB locks.
  let existing: { id: bigint } | null;
  try {
    existing = await prisma.userTradeAllocation.findFirst({
      where: { tradeId: p.tradeId, userId: p.userId },
      select: { id: true },
    });
  } catch (err) {
    console.error("[pamm-webhook] Idempotency check failed:", err);
    return NextResponse.json({ error: "Database error during idempotency check." }, { status: 500 });
  }
  if (existing) {
    return NextResponse.json(
      { status: "skipped", reason: "duplicate" },
      { status: 200 },
    );
  }

  // ── 4. Resolve client + admin users ───────────────────────────────────────
  let client: { id: string; email: string; name: string | null; balanceUsdt: Prisma.Decimal; performanceFeePct: Prisma.Decimal; isActive: boolean } | null = null;
  let admin: { id: string; balanceUsdt: Prisma.Decimal } | null = null;
  try {
    [client, admin] = await Promise.all([
      prisma.user.findUnique({
        where: { id: p.userId },
        select: { id: true, email: true, name: true, balanceUsdt: true, performanceFeePct: true, isActive: true },
      }),
      prisma.user.findFirst({
        where: { role: "admin", isActive: true },
        select: { id: true, balanceUsdt: true },
      }),
    ]);
  } catch (err) {
    console.error("[pamm-webhook] User lookup failed:", err);
    return NextResponse.json({ error: "Database error during user lookup." }, { status: 500 });
  }

  if (!client || !client.isActive) {
    return NextResponse.json({ error: "Client user not found or inactive." }, { status: 404 });
  }
  if (!admin) {
    console.error("[pamm-webhook] No active admin user found in DB.");
    return NextResponse.json({ error: "Admin user not configured." }, { status: 500 });
  }

  // ── 5. PAMM Math — Decimal-only, zero float contamination ─────────────────
  //
  //   netBaseline     = rawPnl − binanceFee
  //   ┌─ WIN  (netBaseline > 0) ──────────────────────────────────────────────
  //   │   performanceFee  = netBaseline × performanceFeePct      (e.g. 5%)
  //   │   clientNetShare  = netBaseline − performanceFee          (95%)
  //   └─ LOSS (netBaseline ≤ 0) ──────────────────────────────────────────────
  //       performanceFee  = 0            (admin takes NOTHING on losses)
  //       clientNetShare  = netBaseline  (client absorbs 100% of the loss)
  //
  //   adminTotalShare = binanceFee + performanceFee  (always ≥ binanceFee)
  //
  const rawPnlD      = new Prisma.Decimal(p.rawPnl);
  const binanceFeeD  = new Prisma.Decimal(p.binanceFee);
  const perfFeePct   = new Prisma.Decimal(client.performanceFeePct);

  const netBaseline     = rawPnlD.sub(binanceFeeD);
  const isWin           = netBaseline.gt(0);
  const performanceFee  = isWin ? netBaseline.mul(perfFeePct) : new Prisma.Decimal(0);
  const clientNetShare  = netBaseline.sub(performanceFee);
  const adminTotalShare = binanceFeeD.add(performanceFee);

  const clientBalanceBefore = new Prisma.Decimal(client.balanceUsdt);
  const adminBalanceBefore  = new Prisma.Decimal(admin.balanceUsdt);
  const clientBalanceAfter  = clientBalanceBefore.add(clientNetShare);
  const adminBalanceAfter   = adminBalanceBefore.add(adminTotalShare);

  const symbolLabel = `${p.symbol} [${p.side.toUpperCase()}] — ${p.exitReason}`;

  // ── 6. ACID $transaction — all-or-nothing ──────────────────────────────────
  // A DB crash at any point rolls back every write automatically (Postgres).
  // Order matters: balance updates first, then immutable audit records.
  try {
    await prisma.$transaction([
      // (a) Update CLIENT balance
      prisma.user.update({
        where: { id: client.id },
        data:  { balanceUsdt: clientBalanceAfter },
      }),

      // (b) Update ADMIN balance (Binance fee reimbursement + performance fee)
      prisma.user.update({
        where: { id: admin.id },
        data:  { balanceUsdt: adminBalanceAfter },
      }),

      // (c) Immutable allocation record — idempotency key stored here
      prisma.userTradeAllocation.create({
        data: {
          tradeId:        p.tradeId,
          userId:         client.id,
          symbol:         p.symbol,
          side:           p.side,
          exitReason:     p.exitReason,
          broker:         p.broker,
          // Map absolute USDT amounts to schema columns:
          //   pnlPct         → 0 (not percentage-based in this model)
          //   grossPnlUsdt   → rawPnl (pre-fee gross)
          //   commissionUsdt → binanceFee
          //   perfFeeUsdt    → performanceFee
          //   adminFeeUsdt   → adminTotalShare
          //   netPnlUsdt     → clientNetShare
          pnlPct:         new Prisma.Decimal(0),
          grossPnlUsdt:   rawPnlD,
          commissionUsdt: binanceFeeD,
          perfFeeUsdt:    performanceFee,
          adminFeeUsdt:   adminTotalShare,
          netPnlUsdt:     clientNetShare,
          balanceBefore:  clientBalanceBefore,
          balanceAfter:   clientBalanceAfter,
        },
      }),

      // (d) TRADE_PNL — net amount credited/debited to the client
      //     Skipped if clientNetShare is exactly 0 (satisfies ledger_amount_positive constraint).
      ...(clientNetShare.abs().gt(0)
        ? [
            prisma.ledgerTransaction.create({
              data: {
                userId:      client.id,
                type:        "TRADE_PNL",
                broker:      p.broker,
                amountUsdt:  clientNetShare.abs(),
                description: `Net PnL (${clientNetShare.gte(0) ? "WIN" : "LOSS"}) — ${symbolLabel} | raw: ${rawPnlD.toFixed(8)} USDT`,
              },
            }),
          ]
        : []),

      // (e) BINANCE_FEE_REIMBURSEMENT — only when fee > 0 (Deriv trades have 0 fee)
      //     Skipped when binanceFee=0 to satisfy the ledger_amount_positive constraint.
      ...(binanceFeeD.gt(0)
        ? [
            prisma.ledgerTransaction.create({
              data: {
                userId:      client.id,
                type:        "BINANCE_FEE_REIMBURSEMENT",
                broker:      p.broker,
                amountUsdt:  binanceFeeD,
                description: `${p.broker === "deriv" ? "Deriv" : "Binance"} execution fee — ${symbolLabel} | reimbursed to admin`,
              },
            }),
          ]
        : []),

      // (f) PERFORMANCE_FEE — only charged on wins (netBaseline > 0)
      //     Not created on LOSS trades.
      ...(isWin
        ? [
            prisma.ledgerTransaction.create({
              data: {
                userId:      client.id,
                type:        "PERFORMANCE_FEE",
                broker:      p.broker,
                amountUsdt:  performanceFee,
                description: `${(perfFeePct.mul(100).toFixed(1))}% performance fee — ${symbolLabel} | net baseline: ${netBaseline.toFixed(8)} USDT`,
              },
            }),
          ]
        : []),
    ]);
  } catch (err) {
    // If the unique constraint fires (race condition after idempotency check),
    // treat it as a duplicate rather than an internal error.
    if (
      err instanceof Prisma.PrismaClientKnownRequestError &&
      err.code === "P2002"
    ) {
      return NextResponse.json(
        { status: "skipped", reason: "duplicate" },
        { status: 200 },
      );
    }
    console.error("[pamm-webhook] Transaction failed:", err);
    return NextResponse.json({ error: "PAMM transaction failed." }, { status: 500 });
  }

  console.info(
    "[pamm-webhook] Allocated %s | rawPnl %s | fee %s | clientNet %s | adminGain %s",
    p.symbol,
    rawPnlD.toFixed(8),
    binanceFeeD.toFixed(8),
    clientNetShare.toFixed(8),
    adminTotalShare.toFixed(8),
  );

  // ── 7. Fire-and-forget email for client (WIN only) ─────────────────────────
  if (isWin) {
    dispatchWinEmail(
      client.email,
      client.name,
      p.symbol,
      clientNetShare,
      clientBalanceAfter,
    ).catch((err) =>
      console.error("[pamm-webhook] Email dispatch error:", err),
    );
  }

  return NextResponse.json({
    success:        true,
    tradeId:        p.tradeId,
    symbol:         p.symbol,
    rawPnl:         rawPnlD.toFixed(8),
    binanceFee:     binanceFeeD.toFixed(8),
    performanceFee: performanceFee.toFixed(8),
    clientNetShare: clientNetShare.toFixed(8),
    adminTotalShare: adminTotalShare.toFixed(8),
  });
}

// ─── Fire-and-forget win email ─────────────────────────────────────────────────
async function dispatchWinEmail(
  email: string,
  name: string | null,
  symbol: string,
  netPnlUsdt: Prisma.Decimal,
  balanceAfter: Prisma.Decimal,
): Promise<void> {
  await sendPammAllocationEmail(
    email,
    name,
    symbol,
    parseFloat(netPnlUsdt.toFixed(8)),
    parseFloat(balanceAfter.toFixed(2)),
  );
}

