import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/db";
import { generateOtp, hashOtp } from "@/lib/auth";
import { sendOtpEmail } from "@/lib/email";
import { RequestOtpSchema } from "@/lib/zod";

// ─── Constants ────────────────────────────────────────────────────────────────

/** OTP validity window in milliseconds (10 minutes). */
const OTP_TTL_MS = 10 * 60 * 1_000;

/**
 * Minimum response time in milliseconds.
 *
 * Anti-enumeration defence: whether or not the email exists in the DB,
 * the endpoint always takes at least this long to respond.
 * An attacker probing for valid accounts cannot distinguish "found" from
 * "not found" via response timing.
 */
const CONSTANT_RESPONSE_DELAY_MS = 700;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function errorJson(message: string, status: number) {
  return NextResponse.json({ success: false, error: message }, { status });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── Handler ──────────────────────────────────────────────────────────────────

export async function POST(request: NextRequest) {
  const requestStart = Date.now();

  // ── 1. Parse & validate body ──────────────────────────────────────────────
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    await sleep(CONSTANT_RESPONSE_DELAY_MS);
    return errorJson("Request body inválido.", 400);
  }

  const parsed = RequestOtpSchema.safeParse(body);
  if (!parsed.success) {
    await sleep(CONSTANT_RESPONSE_DELAY_MS);
    return errorJson(
      parsed.error.errors.map((e) => e.message).join(" "),
      400
    );
  }

  const { email } = parsed.data;

  // ── 2. Core logic (time-constant regardless of outcome) ───────────────────
  try {
    const user = await prisma.user.findUnique({
      where: { email },
      select: { id: true, isActive: true },
    });

    if (user?.isActive) {
      // ── 2a. Expire any existing live OTPs for this user ──────────────────
      // Prevents session confusion if the user clicks "send again".
      await prisma.otpCode.updateMany({
        where: {
          userId: user.id,
          expiresAt: { gt: new Date() },
        },
        data: { expiresAt: new Date() },
      });

      // ── 2b. Generate, hash, and persist a new OTP ─────────────────────────
      const otp = generateOtp();
      const codeHash = hashOtp(otp);
      const expiresAt = new Date(Date.now() + OTP_TTL_MS);

      await prisma.otpCode.create({
        data: { userId: user.id, codeHash, expiresAt },
      });

      // ── 2c. Send email (errors bubble up to the catch below) ──────────────
      await sendOtpEmail(email, otp);
    }
    // If user not found or inactive: fall through silently (anti-enumeration).
  } catch (err) {
    // Log server-side but never reveal internal errors to the caller.
    console.error("[request-otp] Internal error:", err);

    // Still enforce minimum response time before returning 500.
    await enforceMinimumDelay(requestStart);
    return NextResponse.json(
      { success: false, error: "Error interno. Inténtalo más tarde." },
      { status: 500 }
    );
  }

  // ── 3. Constant-time response ─────────────────────────────────────────────
  await enforceMinimumDelay(requestStart);

  // Always return the same message regardless of whether the email exists.
  return NextResponse.json({
    success: true,
    message:
      "Si tu email está registrado, recibirás un código en los próximos segundos.",
  });
}

async function enforceMinimumDelay(startMs: number): Promise<void> {
  const elapsed = Date.now() - startMs;
  const remaining = CONSTANT_RESPONSE_DELAY_MS - elapsed;
  if (remaining > 0) await sleep(remaining);
}
