/**
 * GET /api/admin/bot-status
 * Lee deriv_status.json del volumen compartido y agrega conteo de open contracts.
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { resolveSessionFromCookies } from "@/lib/authSession";
import { readBotStatus, listOpenContractsFiltered } from "@/lib/clientData";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  if (!session?.payload) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const [status, open] = await Promise.all([
    readBotStatus(),
    listOpenContractsFiltered(null),
  ]);

  const heartbeatAt = (status?.heartbeat_at as string | undefined) ?? null;
  const heartbeatAgeSec = heartbeatAt
    ? Math.round((Date.now() - new Date(heartbeatAt).getTime()) / 1000)
    : null;

  return NextResponse.json({
    ok: true,
    status: status?.status ?? "unknown",
    connected: Boolean(status?.connected),
    heartbeatAt,
    heartbeatAgeSec,
    ordersSent: Number(status?.orders_sent ?? 0),
    ordersOk: Number(status?.orders_ok ?? 0),
    ticksTotal: Number(status?.ticks_total ?? 0),
    activeSymbols: Array.isArray(status?.active_symbols)
      ? (status?.active_symbols as unknown[]).length
      : Number(status?.active_symbols_count ?? 0),
    openContracts: open.length,
    raw: status,
  });
}
