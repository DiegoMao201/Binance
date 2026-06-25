// Edge Runtime compatible subset — no Node.js crypto.
// Used by middleware.ts → authSession.ts. API routes use lib/auth.ts directly.
import { jwtVerify, type JWTPayload as JosePayload } from "jose";

export interface PortalJWTPayload {
  sub: string;
  role: string;
}

function getJwtSecret(): Uint8Array {
  const secret = process.env.JWT_SECRET;
  if (!secret || secret.length < 32) {
    throw new Error("JWT_SECRET env var is missing or < 32 characters.");
  }
  return new TextEncoder().encode(secret);
}

export async function verifyJWT(
  token: string
): Promise<PortalJWTPayload | null> {
  try {
    const { payload } = await jwtVerify(token, getJwtSecret(), {
      issuer: "optiferre-portal",
      audience: "portal-investors",
    });
    const p = payload as JosePayload & { role?: string };
    if (typeof p.sub !== "string" || typeof p.role !== "string") return null;
    return { sub: p.sub, role: p.role };
  } catch {
    return null;
  }
}
