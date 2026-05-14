import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/db";
import { sendTradeClosedEmail } from "@/lib/email";

// ─── Types ────────────────────────────────────────────────────────────────────

interface TradeClosedPayload {
  /** Trading pair, e.g. "ETH/USDT". */
  symbol: string;
  /** Net P&L as a percentage, e.g. 1.5 for +1.5%. */
  net_pnl_pct: number;
  /** "WIN" | "LOSS" */
  type: "WIN" | "LOSS";
}

// ─── Handler ──────────────────────────────────────────────────────────────────

/**
 * POST /api/webhooks/trade-closed
 *
 * Called by the Python trading bot after a trade closes. On WIN, sends an
 * email notification to every active client investor.
 *
 * Security:
 *   • Authorization: Bearer <WEBHOOK_SECRET> header required.
 *   • Returns 401 immediately on missing/invalid token.
 *   • Designed to respond 200 quickly; email dispatch is fire-and-forget
 *     (each send is individually wrapped in try/catch).
 *
 * Payload: { symbol: string, net_pnl_pct: number, type: "WIN" | "LOSS" }
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  // ── 1. Auth: Bearer token guard ───────────────────────────────────────────
  const webhookSecret = process.env.WEBHOOK_SECRET;
  if (!webhookSecret) {
    // Misconfiguration — fail closed.
    console.error("[trade-closed] WEBHOOK_SECRET env var is not set.");
    return NextResponse.json({ error: "Server misconfiguration." }, { status: 500 });
  }

  const authHeader = request.headers.get("authorization") ?? "";
  const [scheme, token] = authHeader.split(" ");
  if (scheme !== "Bearer" || token !== webhookSecret) {
    return NextResponse.json({ error: "Unauthorized." }, { status: 401 });
  }

  // ── 2. Parse body ──────────────────────────────────────────────────────────
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body." }, { status: 400 });
  }

  const payload = body as Partial<TradeClosedPayload>;
  const { symbol, net_pnl_pct, type } = payload;

  if (
    typeof symbol      !== "string" ||
    typeof net_pnl_pct !== "number" ||
    (type !== "WIN" && type !== "LOSS")
  ) {
    return NextResponse.json(
      { error: "Missing or invalid fields: symbol (string), net_pnl_pct (number), type ('WIN'|'LOSS')." },
      { status: 400 }
    );
  }

  // ── 3. Respond immediately — email dispatch is async, non-blocking ─────────
  // The bot should not wait for email delivery. We kick off the work and
  // return 200 right away so the bot's HTTP call doesn't time out.
  dispatchNotifications(symbol, net_pnl_pct, type).catch((err) =>
    console.error("[trade-closed] Uncaught error in dispatchNotifications:", err)
  );

  return NextResponse.json({ success: true, type, symbol, net_pnl_pct });
}

// ─── Async dispatch (fire-and-forget) ─────────────────────────────────────────

async function dispatchNotifications(
  symbol: string,
  net_pnl_pct: number,
  type: "WIN" | "LOSS"
): Promise<void> {
  // Only notify on WIN trades.
  if (type !== "WIN") {
    console.info(`[trade-closed] Skipping notification for LOSS trade on ${symbol}.`);
    return;
  }

  // Fetch all active clients.
  let clients: { id: string; email: string; name: string | null }[];
  try {
    clients = await prisma.user.findMany({
      where:  { isActive: true, role: "client" },
      select: { id: true, email: true, name: true },
    });
  } catch (err) {
    console.error("[trade-closed] DB query failed:", err);
    return;
  }

  if (clients.length === 0) {
    console.info("[trade-closed] No active clients to notify.");
    return;
  }

  console.info(
    `[trade-closed] Dispatching WIN notification for ${symbol} (+${net_pnl_pct}%) to ${clients.length} client(s).`
  );

  // Send each email individually so one failure does not block the rest.
  const results = await Promise.allSettled(
    clients.map((client) =>
      sendTradeClosedEmail(client.email, client.name, symbol, net_pnl_pct)
    )
  );

  let sent = 0;
  let failed = 0;
  for (const result of results) {
    if (result.status === "fulfilled") {
      sent++;
    } else {
      failed++;
      console.error("[trade-closed] Email send failed:", result.reason);
    }
  }

  console.info(`[trade-closed] Email dispatch complete: ${sent} sent, ${failed} failed.`);
}
