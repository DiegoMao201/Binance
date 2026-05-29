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
  for (const t of trades) {
    const k = dayKey(t.closedAt);
    byDay.set(k, (byDay.get(k) ?? 0) + t.realizedPnl);
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
  });
}
