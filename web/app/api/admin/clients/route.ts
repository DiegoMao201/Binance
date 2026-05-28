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
import { verifyJWT } from "@/lib/auth";
import {
  listActiveClients,
  updateBalanceCache,
  type ClientCoreProfile,
} from "@/lib/clientData";
import { fetchDerivBalance } from "@/lib/derivBalance";
import { calcularEstadoCuenta } from "@/lib/commission";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  return payload?.role === "admin";
}

async function resolveLiveBalance(p: ClientCoreProfile): Promise<{ balance: number; source: string; error?: string }> {
  if (p.derivToken) {
    const snap = await fetchDerivBalance(p.derivToken);
    if (snap.ok && typeof snap.balance === "number") {
      try { await updateBalanceCache(p.id, snap.balance); } catch { /* noop */ }
      return { balance: snap.balance, source: "deriv_ws" };
    }
    if (p.balanceActualCache != null) {
      return { balance: p.balanceActualCache, source: "cache", error: snap.error };
    }
    return { balance: p.capitalInicial, source: "fallback", error: snap.error };
  }
  if (p.balanceActualCache != null) {
    return { balance: p.balanceActualCache, source: "cache" };
  }
  return { balance: p.capitalInicial, source: "fallback" };
}

export async function GET() {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const clients = await listActiveClients();
  const enriched = await Promise.all(
    clients.map(async (c) => {
      const live = await resolveLiveBalance(c);
      const estado = calcularEstadoCuenta(
        c.capitalInicial,
        live.balance,
        c.comisionTotalCobrada,
      );
      // "Mi 20%" sobre la ganancia que esta por encima del capital_inicial.
      const gananciaSobreUmbral = Math.max(estado.gananciaNeta, 0);
      const adminFee20 = gananciaSobreUmbral * 0.20;

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
        gananciaNeta: estado.gananciaNeta,
        rendimientoPct: estado.rendimientoPct,
        enModoRecuperacion: estado.enModoRecuperacion,
        mensajeEstado: estado.mensajeEstado,
        adminFee20,
      };
    }),
  );

  const totalAdminFee20 = enriched.reduce((acc, c) => acc + (c.enModoRecuperacion ? 0 : c.adminFee20), 0);

  return NextResponse.json({
    ok: true,
    clients: enriched,
    totalAdminFee20,
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
