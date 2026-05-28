/**
 * GET /api/client/open-positions
 * Lee posiciones abiertas Deriv filtradas (si el archivo trae user_id)
 * para el cliente autenticado.
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { verifyJWT } from "@/lib/auth";
import { listOpenContractsFiltered, getClientProfile } from "@/lib/clientData";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  if (!payload) {
    return NextResponse.json({ ok: false, error: "No autenticado." }, { status: 401 });
  }
  const profile = await getClientProfile(payload.sub);
  if (!profile) {
    return NextResponse.json({ ok: false, error: "Cliente no encontrado." }, { status: 404 });
  }

  const raw = await listOpenContractsFiltered(profile.id);
  const positions = raw.map((r) => {
    const sym = typeof r.symbol === "string" ? r.symbol : "DERIV";
    const stake = typeof r.stake === "number"
      ? r.stake
      : Number(r.buy_price ?? r.stake_usdt ?? 0);
    const pnl = typeof r.current_pnl === "number"
      ? r.current_pnl
      : Number(r.profit ?? r.realized_pnl_usdt ?? 0);
    const openedAt = typeof r.opened_at === "string"
      ? r.opened_at
      : typeof r.opened_at_ts === "number"
        ? new Date(r.opened_at_ts > 1e12 ? r.opened_at_ts : r.opened_at_ts * 1000).toISOString()
        : null;
    return {
      symbol: sym,
      stake: Number.isFinite(stake) ? stake : 0,
      currentPnl: Number.isFinite(pnl) ? pnl : 0,
      status: "Activo",
      openedAt,
    };
  });

  return NextResponse.json({ ok: true, positions });
}
