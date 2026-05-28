/**
 * GET /api/client/trades?limit=200
 * Devuelve trades cerrados del cliente desde fecha_inicio.
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { resolveSessionFromCookies } from "@/lib/authSession";
import {
  getClientProfile,
  listClosedTradesSinceFechaInicio,
} from "@/lib/clientData";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request) {
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

  const url = new URL(req.url);
  const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? 200), 1), 1000);

  const trades = await listClosedTradesSinceFechaInicio(profile.fechaInicio, profile.id);
  return NextResponse.json({
    ok: true,
    trades: trades.slice(0, limit),
    total: trades.length,
  });
}
