import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/db";
import { signJWT, verifyOtp } from "@/lib/auth";
import { AUTH_COOKIE_LEGACY, roleScopedCookieName } from "@/lib/authSession";
import { VerifyOtpSchema } from "@/lib/zod";

// ─── Constants ────────────────────────────────────────────────────────────────

/** Lock the OTP after this many failed attempts. */
const MAX_ATTEMPTS = 3;

/** JWT lifetime in seconds (matches the 24 h token TTL). */
const COOKIE_MAX_AGE = 86_400;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function errorJson(message: string, status: number) {
  return NextResponse.json({ success: false, error: message }, { status });
}

// ─── Handler ──────────────────────────────────────────────────────────────────

export async function POST(request: NextRequest) {
  // ── 1. Parse & validate body ──────────────────────────────────────────────
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorJson("Request body inválido.", 400);
  }

  const parsed = VerifyOtpSchema.safeParse(body);
  if (!parsed.success) {
    return errorJson(
      parsed.error.errors.map((e) => e.message).join(" "),
      400
    );
  }

  const { email, code } = parsed.data;

  // ── 2. Resolve user ────────────────────────────────────────────────────────
  // Use a generic error for all failure cases to prevent user enumeration.
  const user = await prisma.user.findUnique({
    where: { email },
    select: { id: true, role: true, isActive: true },
  });

  if (!user || !user.isActive) {
    // Deliberate generic message — do NOT reveal whether the email exists.
    return errorJson("Código inválido o expirado.", 401);
  }

  // ── 3. Find active OTP & verify inside a transaction ─────────────────────
  // The transaction guarantees that attempt-increment and expiry update are
  // atomic — no race condition between concurrent verify requests.
  type TxResult =
    | { status: "no_otp" }
    | { status: "locked" }
    | { status: "invalid" }
    | { status: "success" };

  let txResult: TxResult;

  try {
    txResult = await prisma.$transaction<TxResult>(async (tx) => {
      // Find the most recent unexpired OTP with attempts remaining.
      const otp = await tx.otpCode.findFirst({
        where: {
          userId: user.id,
          expiresAt: { gt: new Date() },
          attempts: { lt: MAX_ATTEMPTS },
        },
        orderBy: { createdAt: "desc" },
      });

      if (!otp) return { status: "no_otp" };

      // Atomically increment the attempt counter.
      const updated = await tx.otpCode.update({
        where: { id: otp.id },
        data: { attempts: { increment: 1 } },
        select: { attempts: true },
      });

      // Timing-safe comparison AFTER incrementing (prevents brute-force by
      // consuming attempts regardless of the comparison result).
      const isMatch = verifyOtp(code, otp.codeHash);

      if (!isMatch) {
        if (updated.attempts >= MAX_ATTEMPTS) {
          // Lock out: expire the OTP immediately.
          await tx.otpCode.update({
            where: { id: otp.id },
            data: { expiresAt: new Date() },
          });
          return { status: "locked" };
        }
        return { status: "invalid" };
      }

      // Success — invalidate the OTP so it cannot be replayed.
      await tx.otpCode.update({
        where: { id: otp.id },
        data: { expiresAt: new Date() },
      });

      return { status: "success" };
    });
  } catch (err) {
    console.error("[verify-otp] Transaction error:", err);
    return NextResponse.json(
      { success: false, error: "Error interno. Inténtalo más tarde." },
      { status: 500 }
    );
  }

  // ── 4. Handle transaction outcome ─────────────────────────────────────────
  if (txResult.status === "no_otp" || txResult.status === "invalid") {
    // Same message for both — prevents distinguishing "no OTP requested" from
    // "wrong code entered".
    return errorJson("Código inválido o expirado.", 401);
  }

  if (txResult.status === "locked") {
    return errorJson(
      "Demasiados intentos fallidos. Solicita un nuevo código.",
      429
    );
  }

  // ── 5. Issue JWT session cookie ────────────────────────────────────────────
  let token: string;
  try {
    token = await signJWT({ sub: user.id, role: user.role });
  } catch (err) {
    console.error("[verify-otp] JWT signing error:", err);
    return NextResponse.json(
      { success: false, error: "Error al generar la sesión." },
      { status: 500 }
    );
  }

  const response = NextResponse.json({ success: true });
  const scopedCookie = roleScopedCookieName(user.role);
  const cookieOptions = {
    httpOnly: true,                                       // not accessible via JS
    secure: process.env.NODE_ENV === "production",        // HTTPS-only in prod
    sameSite: "strict",                                   // CSRF protection
    maxAge: COOKIE_MAX_AGE,
    path: "/",
  } as const;

  // Legacy cookie keeps backward compatibility with old routes/actions.
  response.cookies.set(AUTH_COOKIE_LEGACY, token, cookieOptions);
  // Scoped cookie prevents admin/client sessions from overriding each other.
  response.cookies.set(scopedCookie, token, cookieOptions);

  return response;
}
