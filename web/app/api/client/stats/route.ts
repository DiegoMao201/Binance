/**
 * GET /api/client/stats
 * Estadisticas resumidas del cliente desde su fecha_inicio:
 *  - total trades, win rate, mejor dia, dias positivos, promedio diario
 *  - serie diaria de PnL agregada (para el grafico de barras)
 *
 * Aplica la regla absoluta: NUNCA datos anteriores a fecha_inicio.
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { resolveSessionFromCookies } from "@/lib/authSession";
import {
  getClientProfile,
  listClosedTradesSinceFechaInicio,
  type ClosedTradeRow,
} from "@/lib/clientData";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

function addDaysUtc(dayKeyUtc: string, deltaDays: number): string {
  const d = new Date(`${dayKeyUtc}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

function serviceFromNetPnl(pnl: number): number {
  return Math.max(pnl, 0) * 0.20;
}

export async function GET() {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "client");
  const payload = session?.payload ?? null;
  if (!payload) {
    return NextResponse.json({ ok: false, error: "No autenticado." }, { status: 401 });
  }
  const profile = await getClientProfile(payload.sub);
  if (!profile?.fechaInicio) {
    return NextResponse.json({
      ok: false,
      error: "Cliente sin fecha_inicio configurada.",
    }, { status: 409 });
  }

  const trades: ClosedTradeRow[] = await listClosedTradesSinceFechaInicio(
    profile.fechaInicio,
    profile.id,
  );

  const totalTrades = trades.length;
  const wins = trades.filter((t) => t.realizedPnl > 0).length;
  const winRate = totalTrades > 0 ? (wins / totalTrades) * 100 : 0;

  // Agregado diario
  const byDay = new Map<string, number>();
  const tradesByDay = new Map<string, number>();
  for (const t of trades) {
    const k = dayKey(t.closedAt);
    byDay.set(k, (byDay.get(k) ?? 0) + t.realizedPnl);
    tradesByDay.set(k, (tradesByDay.get(k) ?? 0) + 1);
  }
  const dailyPnl = Array.from(byDay.entries())
    .map(([date, pnl]) => ({ date, pnl }))
    .sort((a, b) => (a.date < b.date ? -1 : 1));

  const bestDay = dailyPnl.reduce((mx, d) => (d.pnl > mx ? d.pnl : mx), 0);
  const positiveDays = dailyPnl.filter((d) => d.pnl > 0).length;
  const totalDays = dailyPnl.length;

  const inicio = profile.fechaInicio;
  const diasActivo = Math.max(
    1,
    Math.floor((Date.now() - inicio.getTime()) / 86_400_000),
  );
  const totalPnl = dailyPnl.reduce((acc, d) => acc + d.pnl, 0);
  const promedioDiario = diasActivo > 0 ? totalPnl / diasActivo : 0;
  const latestDay = dailyPnl.length ? dailyPnl[dailyPnl.length - 1] : null;
  const todayKeyUtc = new Date().toISOString().slice(0, 10);
  const todayDay = dailyPnl.find((d) => d.date === todayKeyUtc) ?? null;

  const latestDayPnl = latestDay?.pnl ?? 0;
  const todayPnlUtc = todayDay?.pnl ?? 0;
  const estimatedServiceLatestDay = serviceFromNetPnl(latestDayPnl);
  const estimatedServiceTodayUtc = serviceFromNetPnl(todayPnlUtc);
  const estimatedServiceTotal = serviceFromNetPnl(totalPnl);

  const dailySettlement = [] as Array<{
    dayKey: string;
    pnl: number;
    service: number;
    clientNet: number;
    capitalStart: number;
    capitalEnd: number;
    trades: number;
    partialDay: boolean;
  }>;
  let runningCapital = profile.capitalInicial;
  const inicioDayKey = inicio.toISOString().slice(0, 10);
  for (const d of dailyPnl) {
    const capitalStart = runningCapital;
    const service = serviceFromNetPnl(d.pnl);
    const clientNet = d.pnl - service;
    const capitalEnd = capitalStart + d.pnl;
    dailySettlement.push({
      dayKey: d.date,
      pnl: d.pnl,
      service,
      clientNet,
      capitalStart,
      capitalEnd,
      trades: tradesByDay.get(d.date) ?? 0,
      partialDay: d.date === inicioDayKey,
    });
    runningCapital = capitalEnd;
  }

  const pnlBeforeTodayUtc = dailyPnl
    .filter((d) => d.date < todayKeyUtc)
    .reduce((acc, d) => acc + d.pnl, 0);
  const operationalCapitalToday = profile.capitalInicial + pnlBeforeTodayUtc;
  const projectedNextDayCapital = operationalCapitalToday + todayPnlUtc;
  const clientNetTodayUtc = todayPnlUtc - estimatedServiceTodayUtc;
  const firstDayPartial = inicio.toISOString().slice(0, 10) === todayKeyUtc;

  const settledDays = dailyPnl.filter((d) => d.date < todayKeyUtc);
  const lastSettledDay = settledDays.length ? settledDays[settledDays.length - 1] : null;
  const lastSettledDayService = serviceFromNetPnl(lastSettledDay?.pnl ?? 0);

  const yesterdayKeyUtc = addDaysUtc(todayKeyUtc, -1);
  const yesterdayPnlUtc = byDay.get(yesterdayKeyUtc) ?? 0;
  const serviceDueYesterdayUtc = serviceFromNetPnl(yesterdayPnlUtc);
  const clientNetYesterdayUtc = yesterdayPnlUtc - serviceDueYesterdayUtc;
  const pnlBeforeYesterdayUtc = dailyPnl
    .filter((d) => d.date < yesterdayKeyUtc)
    .reduce((acc, d) => acc + d.pnl, 0);
  const operationalCapitalYesterday = profile.capitalInicial + pnlBeforeYesterdayUtc;
  const projectedAfterYesterdayCapital = operationalCapitalYesterday + yesterdayPnlUtc;
  const tradesYesterdayUtc = tradesByDay.get(yesterdayKeyUtc) ?? 0;

  return NextResponse.json({
    ok: true,
    activeSince: inicio.toISOString(),
    diasActivo,
    totalTrades,
    winRate,
    wins,
    losses: totalTrades - wins,
    bestDay,
    positiveDays,
    totalDays,
    promedioDiario,
    totalPnl,
    dailyPnl,
    latestDayKey: latestDay?.date ?? null,
    latestDayPnl,
    todayKeyUtc,
    todayPnlUtc,
    estimatedServiceLatestDay,
    estimatedServiceTodayUtc,
    estimatedServiceTotal,
    estimatedClientShareLatestDay: latestDayPnl - estimatedServiceLatestDay,
    estimatedClientShareTotal: totalPnl - estimatedServiceTotal,
    operationalCapitalToday,
    serviceDueTodayUtc: estimatedServiceTodayUtc,
    clientNetTodayUtc,
    projectedNextDayCapital,
    yesterdayKeyUtc,
    yesterdayPnlUtc,
    serviceDueYesterdayUtc,
    clientNetYesterdayUtc,
    operationalCapitalYesterday,
    projectedAfterYesterdayCapital,
    tradesYesterdayUtc,
    firstDayPartial,
    lastSettledDayKey: lastSettledDay?.date ?? null,
    lastSettledDayPnl: lastSettledDay?.pnl ?? 0,
    lastSettledDayService,
    dailySettlement,
  });
}
