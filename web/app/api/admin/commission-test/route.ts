/**
 * GET /api/admin/commission-test
 * Ejecuta los 6 escenarios obligatorios contra lib/commission.ts
 * y devuelve los resultados con su valor esperado para auditoria.
 */

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { verifyJWT } from "@/lib/auth";
import { calcularComisionPosicion } from "@/lib/commission";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface Case {
  name: string;
  args: [number, number, number];
  expected: { comisionAdmin: number; gananciaCliente: number };
}

const CASES: Case[] = [
  { name: "1) ganancia sobre umbral",
    args: [100, 105, 2.00],
    expected: { comisionAdmin: 0.40, gananciaCliente: 1.60 } },
  { name: "2) perdida sobre umbral",
    args: [100, 105, -1.50],
    expected: { comisionAdmin: 0, gananciaCliente: -1.50 } },
  { name: "3) ganancia en recuperacion sin cruzar",
    args: [100, 96, 1.20],
    expected: { comisionAdmin: 0, gananciaCliente: 1.20 } },
  { name: "4) cruce exacto del umbral",
    args: [100, 98, 4.00],
    expected: { comisionAdmin: 0.40, gananciaCliente: 3.60 } },
  { name: "5) ganancia exactamente en el umbral",
    args: [100, 100, 1.50],
    expected: { comisionAdmin: 0.30, gananciaCliente: 1.20 } },
  { name: "6) ganancia se queda exactamente en umbral",
    args: [100, 97, 3.00],
    expected: { comisionAdmin: 0, gananciaCliente: 3.00 } },
];

function nearlyEqual(a: number, b: number, eps = 1e-9): boolean {
  return Math.abs(a - b) < eps;
}

export async function GET() {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  if (payload?.role !== "admin") {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const results = CASES.map((c) => {
    const out = calcularComisionPosicion(...c.args);
    const passComision = nearlyEqual(out.comisionAdmin, c.expected.comisionAdmin);
    const passCliente  = nearlyEqual(out.gananciaCliente, c.expected.gananciaCliente);
    return {
      name: c.name,
      args: { capitalInicial: c.args[0], balancePrevio: c.args[1], pnl: c.args[2] },
      expected: c.expected,
      got: {
        comisionAdmin: Number(out.comisionAdmin.toFixed(6)),
        gananciaCliente: Number(out.gananciaCliente.toFixed(6)),
        enModoRecuperacion: out.enModoRecuperacion,
        balanceNuevo: out.balanceNuevo,
      },
      pass: passComision && passCliente,
    };
  });

  const allPass = results.every((r) => r.pass);
  return NextResponse.json({
    ok: allPass,
    summary: `${results.filter((r) => r.pass).length}/${results.length} casos OK`,
    results,
  });
}
