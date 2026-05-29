/**
 * Admin clients API
 *   GET  /api/admin/clients          -> lista clientes activos con KPIs vivos
 *   POST /api/admin/clients          -> crea nuevo cliente
 *
 * Calcula Mi 20% por cliente leyendo balance Deriv en vivo (cuando hay token)
 * y aplicando lib/commission.ts.
 *
 * Solo admin (verificado por JWT + middleware).
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { z } from "zod";
import { prisma } from "@/lib/db";
import { resolveSessionFromCookies } from "@/lib/authSession";
import {
  listActiveClients,
  listClosedTradesSinceFechaInicio,
  updateBalanceCache,
  type ClientCoreProfile,
} from "@/lib/clientData";
import { fetchDerivBalance } from "@/lib/derivBalance";
import { calcularEstadoCuenta } from "@/lib/commission";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  return Boolean(session?.payload);
}

async function resolveLiveBalance(
  p: ClientCoreProfile,
): Promise<{ balance: number | null; source: "deriv_ws" | "cache" | "unavailable"; error?: string }> {
  if (p.derivToken) {
    const snap = await fetchDerivBalance(p.derivToken, p.derivAccountId);
    if (snap.ok && typeof snap.balance === "number") {
      try { await updateBalanceCache(p.id, snap.balance); } catch { /* noop */ }
      return { balance: snap.balance, source: "deriv_ws" };
    }
    if (p.balanceActualCache != null) {
      return { balance: p.balanceActualCache, source: "cache", error: snap.error };
    }
    return { balance: null, source: "unavailable", error: snap.error ?? "Sin lectura Deriv." };
  }
  if (p.balanceActualCache != null) {
    return { balance: p.balanceActualCache, source: "cache" };
  }
  return { balance: null, source: "unavailable", error: "Cliente sin token Deriv." };
}

function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

type PnlSnapshot = {
  totalTrades: number;
  realizedPnlTotal: number;
  realizedPnlTodayUtc: number;
  latestDayKey: string | null;
  latestDayPnl: number;
  serviceEstimatedTotal: number;
  serviceEstimatedLatestDay: number;
  todayKeyUtc: string;
  pnlBeforeTodayUtc: number;
  operationalCapitalToday: number;
  serviceDueTodayUtc: number;
  clientNetTodayUtc: number;
  projectedNextDayCapital: number;
  firstDayPartial: boolean;
  lastSettledDayKey: string | null;
  lastSettledDayPnl: number;
  lastSettledDayService: number;
};

async function resolvePnlSnapshot(p: ClientCoreProfile): Promise<PnlSnapshot> {
  if (!p.fechaInicio) {
    return {
      totalTrades: 0,
      realizedPnlTotal: 0,
      realizedPnlTodayUtc: 0,
      latestDayKey: null,
      latestDayPnl: 0,
      serviceEstimatedTotal: 0,
      serviceEstimatedLatestDay: 0,
      todayKeyUtc: new Date().toISOString().slice(0, 10),
      pnlBeforeTodayUtc: 0,
      operationalCapitalToday: p.capitalInicial,
      serviceDueTodayUtc: 0,
      clientNetTodayUtc: 0,
      projectedNextDayCapital: p.capitalInicial,
      firstDayPartial: false,
      lastSettledDayKey: null,
      lastSettledDayPnl: 0,
      lastSettledDayService: 0,
    };
  }

  const trades = await listClosedTradesSinceFechaInicio(p.fechaInicio, p.id);
  if (!trades.length) {
    const todayKeyUtc = new Date().toISOString().slice(0, 10);
    return {
      totalTrades: 0,
      realizedPnlTotal: 0,
      realizedPnlTodayUtc: 0,
      latestDayKey: null,
      latestDayPnl: 0,
      serviceEstimatedTotal: 0,
      serviceEstimatedLatestDay: 0,
      todayKeyUtc,
      pnlBeforeTodayUtc: 0,
      operationalCapitalToday: p.capitalInicial,
      serviceDueTodayUtc: 0,
      clientNetTodayUtc: 0,
      projectedNextDayCapital: p.capitalInicial,
      firstDayPartial: p.fechaInicio.toISOString().slice(0, 10) === todayKeyUtc,
      lastSettledDayKey: null,
      lastSettledDayPnl: 0,
      lastSettledDayService: 0,
    };
  }

  const byDay = new Map<string, number>();
  for (const t of trades) {
    const k = dayKey(t.closedAt);
    byDay.set(k, (byDay.get(k) ?? 0) + t.realizedPnl);
  }

  const daily = Array.from(byDay.entries()).sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const latest = daily[daily.length - 1] ?? null;
  const latestDayKey = latest?.[0] ?? null;
  const latestDayPnl = latest?.[1] ?? 0;
  const todayKeyUtc = new Date().toISOString().slice(0, 10);
  const realizedPnlTodayUtc = byDay.get(todayKeyUtc) ?? 0;
  const realizedPnlTotal = trades.reduce((acc, t) => acc + t.realizedPnl, 0);

  const pnlBeforeTodayUtc = daily
    .filter(([k]) => k < todayKeyUtc)
    .reduce((acc, [, pnl]) => acc + pnl, 0);
  const operationalCapitalToday = p.capitalInicial + pnlBeforeTodayUtc;
  const serviceDueTodayUtc = Math.max(realizedPnlTodayUtc, 0) * 0.20;
  const clientNetTodayUtc = realizedPnlTodayUtc - serviceDueTodayUtc;
  const projectedNextDayCapital = operationalCapitalToday + realizedPnlTodayUtc;
  const firstDayPartial = p.fechaInicio.toISOString().slice(0, 10) === todayKeyUtc;

  const settled = daily.filter(([k]) => k < todayKeyUtc);
  const lastSettled = settled.length ? settled[settled.length - 1] : null;
  const lastSettledDayKey = lastSettled?.[0] ?? null;
  const lastSettledDayPnl = lastSettled?.[1] ?? 0;
  const lastSettledDayService = Math.max(lastSettledDayPnl, 0) * 0.20;

  return {
    totalTrades: trades.length,
    realizedPnlTotal,
    realizedPnlTodayUtc,
    latestDayKey,
    latestDayPnl,
    serviceEstimatedTotal: Math.max(realizedPnlTotal, 0) * 0.20,
    serviceEstimatedLatestDay: Math.max(latestDayPnl, 0) * 0.20,
    todayKeyUtc,
    pnlBeforeTodayUtc,
    operationalCapitalToday,
    serviceDueTodayUtc,
    clientNetTodayUtc,
    projectedNextDayCapital,
    firstDayPartial,
    lastSettledDayKey,
    lastSettledDayPnl,
    lastSettledDayService,
  };
}

export async function GET() {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const clients = await listActiveClients();
  const enriched = await Promise.all(
    clients.map(async (c) => {
      const [live, pnl] = await Promise.all([
        resolveLiveBalance(c),
        resolvePnlSnapshot(c),
      ]);
      const estado = live.balance == null
        ? null
        : calcularEstadoCuenta(
          c.capitalInicial,
          live.balance,
          c.comisionTotalCobrada,
        );
      const adminFeeLatestDay = estado?.enModoRecuperacion ? 0 : pnl.serviceDueTodayUtc;
      const adminFeeTotalEstimated = estado?.enModoRecuperacion ? 0 : pnl.serviceEstimatedTotal;
      const serviceDueTodayUtc = estado?.enModoRecuperacion ? 0 : pnl.serviceDueTodayUtc;

      return {
        id: c.id,
        displayName: c.displayName,
        email: c.email,
        derivAccountId: c.derivAccountId,
        hasDerivToken: Boolean(c.derivToken),
        fechaInicio: c.fechaInicio?.toISOString() ?? null,
        capitalInicial: c.capitalInicial,
        balanceActual: live.balance,
        balanceSource: live.source,
        balanceError: live.error,
        gananciaNeta: estado?.gananciaNeta ?? null,
        rendimientoPct: estado?.rendimientoPct ?? null,
        enModoRecuperacion: estado?.enModoRecuperacion ?? null,
        mensajeEstado: estado?.mensajeEstado ?? "Saldo no disponible.",
        adminFee20: adminFeeLatestDay,
        adminFee20LatestDay: adminFeeLatestDay,
        adminFee20TotalEstimated: adminFeeTotalEstimated,
        realizedPnlTotal: pnl.realizedPnlTotal,
        realizedPnlTodayUtc: pnl.realizedPnlTodayUtc,
        latestDayKey: pnl.latestDayKey,
        latestDayPnl: pnl.latestDayPnl,
        tradesSinceStart: pnl.totalTrades,
        todayKeyUtc: pnl.todayKeyUtc,
        pnlBeforeTodayUtc: pnl.pnlBeforeTodayUtc,
        operationalCapitalToday: pnl.operationalCapitalToday,
        serviceDueTodayUtc,
        clientNetTodayUtc: pnl.clientNetTodayUtc,
        projectedNextDayCapital: pnl.projectedNextDayCapital,
        firstDayPartial: pnl.firstDayPartial,
        lastSettledDayKey: pnl.lastSettledDayKey,
        lastSettledDayPnl: pnl.lastSettledDayPnl,
        lastSettledDayService: pnl.lastSettledDayService,
      };
    }),
  );

  const totalAdminFee20LatestDay = enriched.reduce((acc, c) => acc + (c.adminFee20LatestDay ?? 0), 0);
  const totalAdminFee20Estimated = enriched.reduce((acc, c) => acc + (c.adminFee20TotalEstimated ?? 0), 0);
  const totalRealizedPnl = enriched.reduce((acc, c) => acc + (c.realizedPnlTotal ?? 0), 0);
  const totalServiceDueTodayUtc = enriched.reduce((acc, c) => acc + (c.serviceDueTodayUtc ?? 0), 0);
  const totalProjectedNextDayCapital = enriched.reduce((acc, c) => acc + (c.projectedNextDayCapital ?? 0), 0);

  return NextResponse.json({
    ok: true,
    clients: enriched,
    totalAdminFee20: totalAdminFee20LatestDay,
    totalAdminFee20LatestDay,
    totalAdminFee20Estimated,
    totalRealizedPnl,
    totalServiceDueTodayUtc,
    totalProjectedNextDayCapital,
    count: enriched.length,
  });
}

const CreateClientSchema = z.object({
  nombre: z.string().min(2).max(120).trim(),
  email: z.string().email().max(320).toLowerCase().trim(),
  capitalInicial: z.coerce.number().positive().max(10_000_000),
  fechaInicio: z.string().min(8), // ISO date or datetime
  derivToken: z.string().min(8).max(512),
  derivAccountId: z.string().min(2).max(64),
});

export async function POST(req: Request) {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  let body: unknown;
  try { body = await req.json(); } catch {
    return NextResponse.json({ ok: false, error: "JSON invalido." }, { status: 400 });
  }
  const parsed = CreateClientSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({
      ok: false,
      error: "Validacion fallida.",
      fieldErrors: parsed.error.flatten().fieldErrors,
    }, { status: 400 });
  }
  const data = parsed.data;

  const fechaInicio = new Date(data.fechaInicio);
  if (Number.isNaN(fechaInicio.getTime())) {
    return NextResponse.json({ ok: false, error: "fechaInicio invalida." }, { status: 400 });
  }

  // Upsert: si ya existe el email, actualiza; si no, crea.
  try {
    const existing = await prisma.user.findUnique({
      where: { email: data.email },
      select: { id: true },
    });

    if (existing) {
      await prisma.$executeRaw`
        UPDATE users
           SET display_name        = ${data.nombre},
               role                = 'client',
               is_active           = TRUE,
               capital_inicial     = ${data.capitalInicial}::numeric,
               fecha_inicio        = ${fechaInicio}::timestamptz,
               deriv_token         = ${data.derivToken},
               deriv_account_id    = ${data.derivAccountId},
               comision_total_cobrada = COALESCE(comision_total_cobrada, 0),
               updated_at          = NOW()
         WHERE id = ${existing.id}::uuid
      `;
      return NextResponse.json({ ok: true, id: existing.id, action: "updated" });
    }

    const created = await prisma.user.create({
      data: {
        email: data.email,
        name: data.nombre,
        role: "client",
        isActive: true,
        entryFeePct: "0",
        performanceFeePct: "0.20",
        balanceUsdt: data.capitalInicial,
      },
      select: { id: true },
    });

    await prisma.$executeRaw`
      UPDATE users
         SET capital_inicial     = ${data.capitalInicial}::numeric,
             fecha_inicio        = ${fechaInicio}::timestamptz,
             deriv_token         = ${data.derivToken},
             deriv_account_id    = ${data.derivAccountId},
             comision_total_cobrada = 0
       WHERE id = ${created.id}::uuid
    `;
    return NextResponse.json({ ok: true, id: created.id, action: "created" });
  } catch (err) {
    console.error("[POST /api/admin/clients]", err);
    return NextResponse.json({ ok: false, error: "Error interno." }, { status: 500 });
  }
}
