/**
 * GET /api/client/account-state
 * Devuelve el estado financiero del cliente autenticado:
 *  - capital_inicial, balance_actual (Deriv WS), ganancia_neta, comision_total
 *  - bandera de modo_recuperacion + mensaje listo para UI
 *
 * Zero-Trust: user_id viene SOLO del JWT cookie verificado por middleware.
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { verifyJWT } from "@/lib/auth";
import {
  getClientProfile,
  updateBalanceCache,
} from "@/lib/clientData";
import { fetchDerivBalance } from "@/lib/derivBalance";
import { calcularEstadoCuenta } from "@/lib/commission";

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
  if (!profile.capitalInicial || !profile.fechaInicio) {
    return NextResponse.json({
      ok: false,
      error: "Cuenta no configurada (faltan capital_inicial o fecha_inicio).",
    }, { status: 409 });
  }

  // Balance en tiempo real desde Deriv si tenemos token.
  let balanceActual = profile.balanceActualCache ?? profile.capitalInicial;
  let balanceSource: "deriv_ws" | "cache" | "fallback" = "fallback";
  let derivError: string | undefined;
  let currency: string | undefined;
  let loginid: string | undefined;

  if (profile.derivToken) {
    const snap = await fetchDerivBalance(profile.derivToken);
    if (snap.ok && typeof snap.balance === "number") {
      balanceActual = snap.balance;
      balanceSource = "deriv_ws";
      currency = snap.currency;
      loginid = snap.loginid;
      // best-effort cache update
      try { await updateBalanceCache(profile.id, balanceActual); } catch { /* noop */ }
    } else {
      derivError = snap.error;
      if (profile.balanceActualCache != null) balanceSource = "cache";
    }
  } else if (profile.balanceActualCache != null) {
    balanceSource = "cache";
  }

  const estado = calcularEstadoCuenta(
    profile.capitalInicial,
    balanceActual,
    profile.comisionTotalCobrada,
  );

  // Desglose para la UI (cliente recibe 80% de la ganancia neta):
  const gananciaBrutaSobreUmbral = Math.max(estado.gananciaNeta, 0);
  const comisionEstimadaSobreUmbral = gananciaBrutaSobreUmbral * 0.20;
  const parteClienteSobreUmbral = gananciaBrutaSobreUmbral * 0.80;

  return NextResponse.json({
    ok: true,
    profile: {
      id: profile.id,
      displayName: profile.displayName,
      email: profile.email,
      derivAccountId: profile.derivAccountId,
      fechaInicio: profile.fechaInicio?.toISOString() ?? null,
    },
    estado: {
      capitalInicial: estado.capitalInicial,
      balanceActual: estado.balanceActual,
      gananciaNeta: estado.gananciaNeta,
      rendimientoPct: estado.rendimientoPct,
      enModoRecuperacion: estado.enModoRecuperacion,
      mensajeEstado: estado.mensajeEstado,
      comisionTotalCobrada: estado.comisionTotalCobrada,
      parteClienteSobreUmbral,
      comisionEstimadaSobreUmbral,
    },
    balanceMeta: {
      source: balanceSource,
      currency,
      loginid,
      cachedAt: profile.balanceActualAt?.toISOString() ?? null,
      derivError,
    },
  });
}
