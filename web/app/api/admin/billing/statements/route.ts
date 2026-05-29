import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";
import { Prisma } from "@prisma/client";
import { z } from "zod";
import { prisma } from "@/lib/db";
import { resolveSessionFromCookies } from "@/lib/authSession";
import {
  listActiveClients,
  listClosedTradesSinceFechaInicio,
  type ClientCoreProfile,
} from "@/lib/clientData";
import {
  BILLING_DAVI_KEY,
  BILLING_NEQUI_NUMBER,
  buildDailySettlement,
  defaultBillingCutoffDayUtc,
  isValidDayKey,
  resolvePeriodWindow,
  summarizeBillingPeriod,
  type BillingMode,
  type BillingStatus,
} from "@/lib/billing";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type StatementDbRow = {
  id: bigint | number;
  user_id: string;
  period_start: Date | string;
  period_end: Date | string;
  mode: string;
  trades_count: number;
  pnl_usdt: Prisma.Decimal;
  service_due_usdt: Prisma.Decimal;
  client_net_usdt: Prisma.Decimal;
  capital_start_usdt: Prisma.Decimal;
  capital_end_usdt: Prisma.Decimal;
  status: string;
  paid_amount_usdt: Prisma.Decimal;
  paid_at: Date | string | null;
  payment_channel: string | null;
  payment_reference: string | null;
  notes: string | null;
  email_sent_at: Date | string | null;
  generated_at: Date | string;
  updated_at: Date | string;
  display_name: string | null;
  email: string;
  billing_whatsapp: string | null;
};

type StatementDto = {
  id: string;
  userId: string;
  displayName: string;
  email: string;
  billingWhatsApp: string | null;
  periodStart: string;
  periodEnd: string;
  mode: BillingMode;
  tradesCount: number;
  pnlUsdt: number;
  serviceDueUsdt: number;
  clientNetUsdt: number;
  capitalStartUsdt: number;
  capitalEndUsdt: number;
  status: BillingStatus;
  paidAmountUsdt: number;
  pendingAmountUsdt: number;
  paidAt: string | null;
  paymentChannel: string | null;
  paymentReference: string | null;
  notes: string | null;
  emailSentAt: string | null;
  generatedAt: string;
  updatedAt: string;
};

const STATUS_SET = new Set<BillingStatus>(["pending", "paid", "waived"]);
const MODE_SET = new Set<BillingMode>(["rolling_7d", "rolling_15d", "since_last_payment", "custom"]);

function toNumber(value: Prisma.Decimal | number | string | null | undefined): number {
  if (value == null) return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function toIsoString(value: Date | string): string {
  const d = value instanceof Date ? value : new Date(value);
  return d.toISOString();
}

function toDayKey(value: Date | string): string {
  return toIsoString(value).slice(0, 10);
}

function mapStatement(row: StatementDbRow): StatementDto {
  const serviceDue = toNumber(row.service_due_usdt);
  const paidAmount = toNumber(row.paid_amount_usdt);
  const pendingAmount = Math.max(serviceDue - paidAmount, 0);

  const mode = MODE_SET.has(row.mode as BillingMode) ? (row.mode as BillingMode) : "since_last_payment";
  const status = STATUS_SET.has(row.status as BillingStatus) ? (row.status as BillingStatus) : "pending";

  return {
    id: String(row.id),
    userId: row.user_id,
    displayName: row.display_name ?? row.email,
    email: row.email,
    billingWhatsApp: row.billing_whatsapp,
    periodStart: toDayKey(row.period_start),
    periodEnd: toDayKey(row.period_end),
    mode,
    tradesCount: row.trades_count,
    pnlUsdt: toNumber(row.pnl_usdt),
    serviceDueUsdt: serviceDue,
    clientNetUsdt: toNumber(row.client_net_usdt),
    capitalStartUsdt: toNumber(row.capital_start_usdt),
    capitalEndUsdt: toNumber(row.capital_end_usdt),
    status,
    paidAmountUsdt: paidAmount,
    pendingAmountUsdt: pendingAmount,
    paidAt: row.paid_at ? toIsoString(row.paid_at) : null,
    paymentChannel: row.payment_channel,
    paymentReference: row.payment_reference,
    notes: row.notes,
    emailSentAt: row.email_sent_at ? toIsoString(row.email_sent_at) : null,
    generatedAt: toIsoString(row.generated_at),
    updatedAt: toIsoString(row.updated_at),
  };
}

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  return Boolean(session?.payload);
}

async function listStatementsFromDb(limit = 500): Promise<StatementDto[]> {
  const rows = await prisma.$queryRaw<StatementDbRow[]>`
    SELECT
      s.id,
      s.user_id::text              AS user_id,
      s.period_start,
      s.period_end,
      s.mode,
      s.trades_count,
      s.pnl_usdt,
      s.service_due_usdt,
      s.client_net_usdt,
      s.capital_start_usdt,
      s.capital_end_usdt,
      s.status,
      s.paid_amount_usdt,
      s.paid_at,
      s.payment_channel,
      s.payment_reference,
      s.notes,
      s.email_sent_at,
      s.generated_at,
      s.updated_at,
      u.display_name,
      u.email,
      to_jsonb(u)->>'billing_whatsapp' AS billing_whatsapp
    FROM client_billing_statements s
    JOIN users u ON u.id = s.user_id
    ORDER BY s.period_end DESC, s.generated_at DESC
    LIMIT ${Math.max(1, Math.min(limit, 2000))}
  `;

  return rows.map(mapStatement);
}

function computeTotals(rows: StatementDto[]) {
  const pendingAmountUsdt = rows.reduce((acc, r) => acc + r.pendingAmountUsdt, 0);
  const serviceDueUsdt = rows.reduce((acc, r) => acc + r.serviceDueUsdt, 0);
  const paidAmountUsdt = rows.reduce((acc, r) => acc + r.paidAmountUsdt, 0);
  const pendingCount = rows.filter((r) => r.status === "pending" && r.pendingAmountUsdt > 0).length;
  const paidCount = rows.filter((r) => r.status === "paid").length;
  const waivedCount = rows.filter((r) => r.status === "waived").length;
  const clientsWithDebt = new Set(rows.filter((r) => r.pendingAmountUsdt > 0).map((r) => r.userId)).size;

  return {
    pendingAmountUsdt,
    serviceDueUsdt,
    paidAmountUsdt,
    pendingCount,
    paidCount,
    waivedCount,
    clientsWithDebt,
  };
}

async function lastPaidEndDayForUser(userId: string): Promise<string | null> {
  const rows = await prisma.$queryRaw<Array<{ last_paid_end: Date | string | null }>>`
    SELECT MAX(period_end)::date AS last_paid_end
    FROM client_billing_statements
    WHERE user_id = ${userId}::uuid
      AND status = 'paid'
  `;
  const value = rows[0]?.last_paid_end ?? null;
  if (!value) return null;
  return toDayKey(value);
}

async function upsertBillingStatement(input: {
  profile: ClientCoreProfile;
  mode: BillingMode;
  summary: ReturnType<typeof summarizeBillingPeriod>;
}): Promise<StatementDto | null> {
  const { profile, mode, summary } = input;
  const suggestedStatus: BillingStatus = summary.serviceDueUsdt > 0 ? "pending" : "waived";
  const payload = {
    partialStartDay: summary.partialStartDay,
    generatedBy: "admin_panel",
    generatedAt: new Date().toISOString(),
  };

  const rows = await prisma.$queryRaw<StatementDbRow[]>`
    WITH upserted AS (
      INSERT INTO client_billing_statements (
        user_id,
        period_start,
        period_end,
        mode,
        trades_count,
        pnl_usdt,
        service_due_usdt,
        client_net_usdt,
        capital_start_usdt,
        capital_end_usdt,
        status,
        paid_amount_usdt,
        statement_payload,
        generated_at,
        updated_at
      ) VALUES (
        ${profile.id}::uuid,
        ${summary.periodStart}::date,
        ${summary.periodEnd}::date,
        ${mode},
        ${summary.tradesCount},
        ${summary.pnlUsdt}::numeric,
        ${summary.serviceDueUsdt}::numeric,
        ${summary.clientNetUsdt}::numeric,
        ${summary.capitalStartUsdt}::numeric,
        ${summary.capitalEndUsdt}::numeric,
        ${suggestedStatus},
        0::numeric,
        ${JSON.stringify(payload)}::jsonb,
        NOW(),
        NOW()
      )
      ON CONFLICT (user_id, period_start, period_end)
      DO UPDATE SET
        mode = EXCLUDED.mode,
        trades_count = EXCLUDED.trades_count,
        pnl_usdt = EXCLUDED.pnl_usdt,
        service_due_usdt = EXCLUDED.service_due_usdt,
        client_net_usdt = EXCLUDED.client_net_usdt,
        capital_start_usdt = EXCLUDED.capital_start_usdt,
        capital_end_usdt = EXCLUDED.capital_end_usdt,
        statement_payload = EXCLUDED.statement_payload,
        generated_at = NOW(),
        updated_at = NOW(),
        status = CASE
          WHEN client_billing_statements.status = 'paid' THEN 'paid'
          WHEN EXCLUDED.service_due_usdt <= 0 THEN 'waived'
          ELSE 'pending'
        END,
        paid_amount_usdt = CASE
          WHEN client_billing_statements.status = 'paid' THEN client_billing_statements.paid_amount_usdt
          ELSE 0
        END,
        paid_at = CASE
          WHEN client_billing_statements.status = 'paid' THEN client_billing_statements.paid_at
          ELSE NULL
        END,
        payment_channel = CASE
          WHEN client_billing_statements.status = 'paid' THEN client_billing_statements.payment_channel
          ELSE NULL
        END,
        payment_reference = CASE
          WHEN client_billing_statements.status = 'paid' THEN client_billing_statements.payment_reference
          ELSE NULL
        END,
        notes = client_billing_statements.notes,
        email_sent_at = client_billing_statements.email_sent_at
      RETURNING *
    )
    SELECT
      s.id,
      s.user_id::text              AS user_id,
      s.period_start,
      s.period_end,
      s.mode,
      s.trades_count,
      s.pnl_usdt,
      s.service_due_usdt,
      s.client_net_usdt,
      s.capital_start_usdt,
      s.capital_end_usdt,
      s.status,
      s.paid_amount_usdt,
      s.paid_at,
      s.payment_channel,
      s.payment_reference,
      s.notes,
      s.email_sent_at,
      s.generated_at,
      s.updated_at,
      u.display_name,
      u.email,
      to_jsonb(u)->>'billing_whatsapp' AS billing_whatsapp
    FROM upserted s
    JOIN users u ON u.id = s.user_id
  `;

  if (!rows.length) return null;
  return mapStatement(rows[0]);
}

function parseAndFilterList(req: NextRequest, allRows: StatementDto[]): { rows: StatementDto[]; error?: string } {
  const status = req.nextUrl.searchParams.get("status");
  const userId = req.nextUrl.searchParams.get("userId");
  const fromDay = req.nextUrl.searchParams.get("fromDay");
  const toDay = req.nextUrl.searchParams.get("toDay");

  if (fromDay && !isValidDayKey(fromDay)) return { rows: [], error: "fromDay invalido (YYYY-MM-DD)." };
  if (toDay && !isValidDayKey(toDay)) return { rows: [], error: "toDay invalido (YYYY-MM-DD)." };
  if (status && status !== "all" && !STATUS_SET.has(status as BillingStatus)) {
    return { rows: [], error: "status invalido." };
  }

  let rows = allRows;

  if (status && status !== "all") {
    rows = rows.filter((r) => r.status === status);
  }
  if (userId && userId !== "all") {
    rows = rows.filter((r) => r.userId === userId);
  }
  if (fromDay) {
    rows = rows.filter((r) => r.periodEnd >= fromDay);
  }
  if (toDay) {
    rows = rows.filter((r) => r.periodStart <= toDay);
  }

  return { rows };
}

export async function GET(req: NextRequest) {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const limitParam = Number(req.nextUrl.searchParams.get("limit") ?? "500");
  const allRows = await listStatementsFromDb(Number.isFinite(limitParam) ? limitParam : 500);
  const { rows, error } = parseAndFilterList(req, allRows);
  if (error) {
    return NextResponse.json({ ok: false, error }, { status: 400 });
  }

  return NextResponse.json({
    ok: true,
    statements: rows,
    totals: computeTotals(rows),
    paymentMethods: {
      nequi: BILLING_NEQUI_NUMBER,
      daviKey: BILLING_DAVI_KEY,
    },
    count: rows.length,
  });
}

const GenerateSchema = z.object({
  mode: z.enum(["rolling_7d", "rolling_15d", "since_last_payment", "custom"]).default("since_last_payment"),
  cutoffDay: z.string().optional(),
  startDay: z.string().optional(),
  endDay: z.string().optional(),
  userId: z.string().uuid().optional(),
  includeZeroDue: z.boolean().optional().default(true),
});

export async function POST(req: NextRequest) {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "JSON invalido." }, { status: 400 });
  }

  const parsed = GenerateSchema.safeParse(body ?? {});
  if (!parsed.success) {
    return NextResponse.json({ ok: false, error: "Payload invalido.", issues: parsed.error.issues }, { status: 400 });
  }

  const data = parsed.data;
  const cutoffDay = data.cutoffDay ?? defaultBillingCutoffDayUtc();
  if (!isValidDayKey(cutoffDay)) {
    return NextResponse.json({ ok: false, error: "cutoffDay invalido (YYYY-MM-DD)." }, { status: 400 });
  }

  if (data.mode === "custom") {
    if (!data.startDay || !isValidDayKey(data.startDay)) {
      return NextResponse.json({ ok: false, error: "startDay invalido para modo custom." }, { status: 400 });
    }
    if (data.endDay && !isValidDayKey(data.endDay)) {
      return NextResponse.json({ ok: false, error: "endDay invalido para modo custom." }, { status: 400 });
    }
  }

  const allClients = await listActiveClients();
  const clients = data.userId ? allClients.filter((c) => c.id === data.userId) : allClients;

  const generated: StatementDto[] = [];
  const skipped: Array<{ userId: string; email: string; reason: string }> = [];

  for (const client of clients) {
    if (!client.fechaInicio) {
      skipped.push({ userId: client.id, email: client.email, reason: "missing_fecha_inicio" });
      continue;
    }

    const fechaInicioDay = client.fechaInicio.toISOString().slice(0, 10);
    const lastPaidEndDay = data.mode === "since_last_payment"
      ? await lastPaidEndDayForUser(client.id)
      : null;

    const period = resolvePeriodWindow({
      mode: data.mode,
      fechaInicioDay,
      cutoffDay,
      customStartDay: data.startDay,
      customEndDay: data.endDay,
      lastPaidEndDay,
    });

    if (!period) {
      skipped.push({ userId: client.id, email: client.email, reason: "empty_period" });
      continue;
    }

    const trades = await listClosedTradesSinceFechaInicio(client.fechaInicio, client.id);
    const dailySettlement = buildDailySettlement(client, trades);
    const summary = summarizeBillingPeriod({
      profile: client,
      dailySettlement,
      periodStart: period.periodStart,
      periodEnd: period.periodEnd,
    });

    if (!data.includeZeroDue && summary.serviceDueUsdt <= 0) {
      skipped.push({ userId: client.id, email: client.email, reason: "zero_due" });
      continue;
    }

    const row = await upsertBillingStatement({
      profile: client,
      mode: data.mode,
      summary,
    });

    if (row) generated.push(row);
  }

  const allRows = await listStatementsFromDb(1000);
  const rows = data.userId ? allRows.filter((r) => r.userId === data.userId) : allRows;

  return NextResponse.json({
    ok: true,
    generatedCount: generated.length,
    skippedCount: skipped.length,
    generated,
    skipped,
    statements: rows,
    totals: computeTotals(rows),
    paymentMethods: {
      nequi: BILLING_NEQUI_NUMBER,
      daviKey: BILLING_DAVI_KEY,
    },
    count: rows.length,
  });
}
