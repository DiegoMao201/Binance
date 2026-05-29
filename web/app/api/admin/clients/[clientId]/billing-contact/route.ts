import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { z } from "zod";
import { resolveSessionFromCookies } from "@/lib/authSession";
import { prisma } from "@/lib/db";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function requireAdmin(): Promise<boolean> {
  const cookieStore = await cookies();
  const session = await resolveSessionFromCookies(cookieStore, "admin");
  return Boolean(session?.payload);
}

const BodySchema = z.object({
  billingWhatsApp: z.string().max(32).optional(),
});

function normalizeWhatsapp(raw: string | undefined): string | null {
  const value = (raw ?? "").trim();
  if (!value) return null;
  return value.replace(/\s+/g, "");
}

export async function PATCH(
  req: Request,
  ctx: { params: Promise<{ clientId: string }> },
) {
  if (!(await requireAdmin())) {
    return NextResponse.json({ ok: false, error: "Acceso denegado." }, { status: 403 });
  }

  const { clientId } = await ctx.params;
  if (!/^[0-9a-fA-F-]{36}$/.test(clientId)) {
    return NextResponse.json({ ok: false, error: "clientId invalido." }, { status: 400 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "JSON invalido." }, { status: 400 });
  }

  const parsed = BodySchema.safeParse(body ?? {});
  if (!parsed.success) {
    return NextResponse.json({ ok: false, error: "Payload invalido.", issues: parsed.error.issues }, { status: 400 });
  }

  const billingWhatsApp = normalizeWhatsapp(parsed.data.billingWhatsApp);

  const rows = await prisma.$queryRaw<Array<{ id: string; billing_whatsapp: string | null }>>`
    UPDATE users
       SET billing_whatsapp = ${billingWhatsApp},
           updated_at = NOW()
     WHERE id = ${clientId}::uuid
       AND role IN ('client', 'investor')
     RETURNING id::text AS id, to_jsonb(users)->>'billing_whatsapp' AS billing_whatsapp
  `;

  if (!rows.length) {
    return NextResponse.json({ ok: false, error: "Cliente no encontrado." }, { status: 404 });
  }

  return NextResponse.json({ ok: true, clientId: rows[0].id, billingWhatsApp: rows[0].billing_whatsapp });
}
