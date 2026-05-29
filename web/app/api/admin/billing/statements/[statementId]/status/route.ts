import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { z } from "zod";
import { prisma } from "@/lib/db";
import { resolveSessionFromCookies } from "@/lib/authSession";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  return Boolean(session?.payload);
}

const BodySchema = z.object({
  status: z.enum(["pending", "paid", "waived"]),
  paidAmountUsdt: z.coerce.number().min(0).optional(),
  paymentChannel: z.string().max(32).optional(),
  paymentReference: z.string().max(160).optional(),
  notes: z.string().max(2000).optional(),
});

export async function PATCH(
  req: Request,
  ctx: { params: Promise<{ statementId: string }> },
) {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const { statementId } = await ctx.params;
  if (!/^\d+$/.test(statementId)) {
    return NextResponse.json({ ok: false, error: "statementId invalido." }, { status: 400 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "JSON invalido." }, { status: 400 });
  }

  const parsed = BodySchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({ ok: false, error: "Payload invalido.", issues: parsed.error.issues }, { status: 400 });
  }

  const data = parsed.data;

  const current = await prisma.$queryRaw<Array<{ service_due_usdt: number | string }>>`
    SELECT service_due_usdt
    FROM client_billing_statements
    WHERE id = ${statementId}::bigint
    LIMIT 1
  `;

  if (!current.length) {
    return NextResponse.json({ ok: false, error: "Estado de cuenta no encontrado." }, { status: 404 });
  }

  const serviceDue = Number(current[0].service_due_usdt ?? 0);
  const paidAmount = data.status === "paid"
    ? (data.paidAmountUsdt ?? serviceDue)
    : 0;

  const rows = await prisma.$queryRaw<Array<{
    id: bigint | number;
    status: string;
    paid_amount_usdt: number | string;
    paid_at: Date | null;
    payment_channel: string | null;
    payment_reference: string | null;
    notes: string | null;
    updated_at: Date;
  }>>`
    UPDATE client_billing_statements
       SET status = ${data.status},
           paid_amount_usdt = ${paidAmount}::numeric,
           paid_at = CASE WHEN ${data.status} = 'paid' THEN NOW() ELSE NULL END,
           payment_channel = CASE WHEN ${data.status} = 'paid' THEN ${data.paymentChannel ?? null} ELSE NULL END,
           payment_reference = CASE WHEN ${data.status} = 'paid' THEN ${data.paymentReference ?? null} ELSE NULL END,
           notes = ${data.notes ?? null},
           updated_at = NOW()
     WHERE id = ${statementId}::bigint
     RETURNING
       id,
       status,
       paid_amount_usdt,
       paid_at,
       payment_channel,
       payment_reference,
       notes,
       updated_at
  `;

  if (!rows.length) {
    return NextResponse.json({ ok: false, error: "No se pudo actualizar el estado de cuenta." }, { status: 500 });
  }

  const row = rows[0];
  return NextResponse.json({
    ok: true,
    statement: {
      id: String(row.id),
      status: row.status,
      paidAmountUsdt: Number(row.paid_amount_usdt ?? 0),
      paidAt: row.paid_at?.toISOString() ?? null,
      paymentChannel: row.payment_channel,
      paymentReference: row.payment_reference,
      notes: row.notes,
      updatedAt: row.updated_at.toISOString(),
    },
  });
}
