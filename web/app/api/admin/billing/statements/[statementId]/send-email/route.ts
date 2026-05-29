import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { resolveSessionFromCookies } from "@/lib/authSession";
import { prisma } from "@/lib/db";
import {
  BILLING_DAVI_KEY,
  BILLING_NEQUI_NUMBER,
} from "@/lib/billing";
import { sendBillingStatementEmail } from "@/lib/email";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  return Boolean(session?.payload);
}

export async function POST(
  _req: Request,
  ctx: { params: Promise<{ statementId: string }> },
) {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const { statementId } = await ctx.params;
  if (!/^\d+$/.test(statementId)) {
    return NextResponse.json({ ok: false, error: "statementId invalido." }, { status: 400 });
  }

  const rows = await prisma.$queryRaw<Array<{
    id: bigint | number;
    period_start: Date | string;
    period_end: Date | string;
    trades_count: number;
    pnl_usdt: number | string;
    service_due_usdt: number | string;
    paid_amount_usdt: number | string;
    client_net_usdt: number | string;
    capital_start_usdt: number | string;
    capital_end_usdt: number | string;
    status: string;
    display_name: string | null;
    email: string;
  }>>`
    SELECT
      s.id,
      s.period_start,
      s.period_end,
      s.trades_count,
      s.pnl_usdt,
      s.service_due_usdt,
      s.paid_amount_usdt,
      s.client_net_usdt,
      s.capital_start_usdt,
      s.capital_end_usdt,
      s.status,
      u.display_name,
      u.email
    FROM client_billing_statements s
    JOIN users u ON u.id = s.user_id
    WHERE s.id = ${statementId}::bigint
    LIMIT 1
  `;

  if (!rows.length) {
    return NextResponse.json({ ok: false, error: "Estado de cuenta no encontrado." }, { status: 404 });
  }

  const row = rows[0];
  const periodStart = (row.period_start instanceof Date ? row.period_start : new Date(row.period_start)).toISOString().slice(0, 10);
  const periodEnd = (row.period_end instanceof Date ? row.period_end : new Date(row.period_end)).toISOString().slice(0, 10);

  await sendBillingStatementEmail(
    row.email,
    row.display_name,
    {
      periodStart,
      periodEnd,
      tradesCount: row.trades_count,
      pnlUsdt: Number(row.pnl_usdt ?? 0),
      serviceDueUsdt: Number(row.service_due_usdt ?? 0),
      paidAmountUsdt: Number(row.paid_amount_usdt ?? 0),
      clientNetUsdt: Number(row.client_net_usdt ?? 0),
      capitalStartUsdt: Number(row.capital_start_usdt ?? 0),
      capitalEndUsdt: Number(row.capital_end_usdt ?? 0),
      status: row.status,
      paymentNequi: BILLING_NEQUI_NUMBER,
      paymentDaviKey: BILLING_DAVI_KEY,
    },
  );

  await prisma.$executeRaw`
    UPDATE client_billing_statements
       SET email_sent_at = NOW(),
           updated_at = NOW()
     WHERE id = ${statementId}::bigint
  `;

  return NextResponse.json({ ok: true, statementId: String(row.id), sentTo: row.email });
}
