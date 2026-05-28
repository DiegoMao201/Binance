/**
 * GET /api/admin/open-positions
 * Posiciones abiertas del bot maestro (global, sin filtro user_id).
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { resolveSessionFromCookies } from "@/lib/authSession";
import { listOpenContractsFiltered } from "@/lib/clientData";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  if (!session?.payload) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const raw = await listOpenContractsFiltered(null);
  const positions = raw.map((r) => {
    const sym = typeof r.symbol === "string" ? r.symbol : "DERIV";
    const stake = Number(r.stake ?? r.buy_price ?? r.stake_usdt ?? 0);
    const pnl = Number(r.current_pnl ?? r.profit ?? r.realized_pnl_usdt ?? 0);
    const openedAt = typeof r.opened_at === "string"
      ? r.opened_at
      : typeof r.opened_at_ts === "number"
        ? new Date(r.opened_at_ts > 1e12 ? r.opened_at_ts : r.opened_at_ts * 1000).toISOString()
        : null;
    return {
      symbol: sym,
      stake: Number.isFinite(stake) ? stake : 0,
      currentPnl: Number.isFinite(pnl) ? pnl : 0,
      openedAt,
      contractId: r.contract_id ?? null,
    };
  });

  return NextResponse.json({ ok: true, positions, count: positions.length });
}
