import { createHash, randomInt, timingSafeEqual } from "crypto";
import { SignJWT, jwtVerify, type JWTPayload as JosePayload } from "jose";

// ─── Environment guard ────────────────────────────────────────────────────────
// JWT_SECRET must be ≥ 32 bytes for HMAC-SHA256 security.
// Fail at startup rather than at runtime to surface misconfiguration early.
function getJwtSecret(): Uint8Array {
  const secret = process.env.JWT_SECRET;
  if (!secret || secret.length < 32) {
    throw new Error(
      "JWT_SECRET env var is missing or < 32 characters. " +
        "Generate one with: openssl rand -base64 48"
    );
  }
  return new TextEncoder().encode(secret);
}

const JWT_ISSUER = "optiferre-portal";
const JWT_AUDIENCE = "portal-investors";
const JWT_TTL = "24h";

// ─── Payload shape ────────────────────────────────────────────────────────────
export interface PortalJWTPayload {
  sub: string; // users.id (UUID)
  role: string; // 'admin' | 'client' | 'investor'
}

// ─── JWT helpers ──────────────────────────────────────────────────────────────

/**
 * Signs a JWT with HMAC-SHA256.
 * Returns a compact (string) token ready to be set in an HttpOnly cookie.
 */
export async function signJWT(payload: PortalJWTPayload): Promise<string> {
  return new SignJWT({ role: payload.role })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(payload.sub)
    .setIssuedAt()
    .setIssuer(JWT_ISSUER)
    .setAudience(JWT_AUDIENCE)
    .setExpirationTime(JWT_TTL)
    .sign(getJwtSecret());
}

/**
 * Verifies a JWT and returns the typed payload, or null if invalid / expired.
 * Safe to use in both Node.js API routes and Edge middleware.
 */
export async function verifyJWT(
  token: string
): Promise<PortalJWTPayload | null> {
  try {
    const { payload } = await jwtVerify(token, getJwtSecret(), {
      issuer: JWT_ISSUER,
      audience: JWT_AUDIENCE,
    });
    const p = payload as JosePayload & { role?: string };
    if (typeof p.sub !== "string" || typeof p.role !== "string") return null;
    return { sub: p.sub, role: p.role };
  } catch {
    return null;
  }
}

// ─── OTP helpers ──────────────────────────────────────────────────────────────

/** Generates a cryptographically random 6-digit OTP string ("100000"–"999999"). */
export function generateOtp(): string {
  // randomInt(min, max) → [min, max) → 100000 to 999999 inclusive.
  return String(randomInt(100000, 1_000_000));
}

/**
 * Returns the SHA-256 hex digest of the plaintext OTP.
 * Stored in otp_codes.code_hash; plaintext is NEVER persisted.
 */
export function hashOtp(otp: string): string {
  return createHash("sha256").update(otp, "utf8").digest("hex");
}

/**
 * Timing-safe comparison between a candidate OTP and a stored hash.
 * Both sides are SHA-256 digests (64 hex chars) → always equal length,
 * making timingSafeEqual safe to use directly.
 */
export function verifyOtp(candidateOtp: string, storedHash: string): boolean {
  const candidateHash = Buffer.from(hashOtp(candidateOtp), "utf8");
  const storedHashBuf = Buffer.from(storedHash, "utf8");
  // Lengths must match for timingSafeEqual; both are 64-char hex → always equal.
  if (candidateHash.length !== storedHashBuf.length) return false;
  return timingSafeEqual(candidateHash, storedHashBuf);
}
