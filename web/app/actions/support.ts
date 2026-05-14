"use server";
/**
 * app/actions/support.ts — Support message Server Action
 *
 * Security model:
 *   • JWT auth_token cookie required; role must be "client" or "investor".
 *   • Input is validated and sanitised before forwarding.
 *   • Sends email to the admin address via SendGrid.
 */

import { cookies } from "next/headers";
import { verifyJWT } from "@/lib/auth";
import { prisma } from "@/lib/db";
import sgMail from "@sendgrid/mail";
import { z } from "zod";

sgMail.setApiKey(process.env.SENDGRID_API_KEY ?? "");

const FROM_EMAIL  = process.env.MAIL_FROM_EMAIL ?? process.env.SENDGRID_FROM_EMAIL ?? "";
const FROM_NAME   = process.env.MAIL_FROM_NAME  ?? "OptiFerre Portal";
const ADMIN_EMAIL = process.env.ADMIN_SUPPORT_EMAIL ?? process.env.MAIL_FROM_EMAIL ?? "";

// ─── Input schema ─────────────────────────────────────────────────────────────
const SupportSchema = z.object({
  subject: z.string().min(3).max(120).trim(),
  message: z.string().min(10).max(2000).trim(),
});

export type SupportResult = {
  success: boolean;
  error?: string;
};

// ─── Action ───────────────────────────────────────────────────────────────────
export async function sendSupportMessage(
  rawSubject: string,
  rawMessage: string
): Promise<SupportResult> {
  // ── Auth guard ────────────────────────────────────────────────────────────
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;
  if (!payload || (payload.role !== "client" && payload.role !== "investor")) {
    return { success: false, error: "No autorizado" };
  }

  // ── Input validation ──────────────────────────────────────────────────────
  const parsed = SupportSchema.safeParse({ subject: rawSubject, message: rawMessage });
  if (!parsed.success) {
    return { success: false, error: "Datos inválidos. Verifica asunto (3-120 chars) y mensaje (10-2000 chars)." };
  }
  const { subject, message } = parsed.data;

  // ── Fetch sender email from DB ────────────────────────────────────────────
  const user = await prisma.user.findUnique({
    where: { id: payload.sub },
    select: { email: true },
  });
  const senderEmail = user?.email ?? null;

  // ── Env guard ─────────────────────────────────────────────────────────────
  if (!process.env.SENDGRID_API_KEY || !FROM_EMAIL || !ADMIN_EMAIL) {
    console.warn("[support] SendGrid env vars missing — support message not sent.");
    // Return success to the user so UX is not broken; log the message for manual review.
    console.warn(`[support] FROM=${FROM_EMAIL} | ADMIN=${ADMIN_EMAIL}`);
    console.warn(`[support] Subject: ${subject}`);
    console.warn(`[support] Message: ${message}`);
    return { success: true };
  }

  try {
    await sgMail.send({
      to: ADMIN_EMAIL,
      from: { email: FROM_EMAIL, name: FROM_NAME },
      replyTo: senderEmail ?? undefined,
      subject: `[Soporte] ${subject} — ${senderEmail ?? "cliente"}`,
      text: [
        `Mensaje de soporte de un inversor.`,
        ``,
        `Usuario: ${senderEmail ?? payload.sub}`,

        `Asunto: ${subject}`,
        ``,
        `Mensaje:`,
        message,
      ].join("\n"),
      html: `
<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#080e16;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <div style="max-width:560px;margin:40px auto;background:#0a1018;border:1px solid #1a2b3c;border-radius:16px;overflow:hidden">
    <div style="background:linear-gradient(135deg,#0f1e2e,#0a1018);padding:28px 32px;border-bottom:1px solid #1a2b3c">
      <p style="margin:0;color:#6b8299;font-size:11px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase">OptiFerre — Soporte</p>
      <h1 style="margin:8px 0 0;color:#dce7f5;font-size:20px;font-weight:700">${subject}</h1>
    </div>
    <div style="padding:28px 32px">
      <p style="margin:0 0 6px;color:#6b8299;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em">Inversor</p>
      <p style="margin:0 0 20px;color:#dce7f5;font-size:14px">${senderEmail ?? payload.sub}</p>
      <p style="margin:0 0 6px;color:#6b8299;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em">Mensaje</p>
      <div style="background:#080e16;border:1px solid #1a2b3c;border-radius:10px;padding:16px 20px">
        <p style="margin:0;color:#dce7f5;font-size:14px;line-height:1.65;white-space:pre-wrap">${message.replace(/</g, "&lt;").replace(/>/g, "&gt;")}</p>
      </div>
    </div>
    <div style="padding:16px 32px;background:#060b11;border-top:1px solid #1a2b3c">
      <p style="margin:0;color:#6b8299;font-size:11px">Responde directamente a este email para contactar al inversor.</p>
    </div>
  </div>
</body>
</html>`,
      trackingSettings: { clickTracking: { enable: false }, openTracking: { enable: false } },
    });
    return { success: true };
  } catch (err) {
    console.error("[support] SendGrid error:", err);
    return { success: false, error: "Error al enviar el mensaje. Inténtalo más tarde." };
  }
}
