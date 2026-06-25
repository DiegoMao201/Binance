import { verifyJWT, type PortalJWTPayload } from "@/lib/authEdge";

export const AUTH_COOKIE_LEGACY = "auth_token";
export const AUTH_COOKIE_ADMIN = "auth_token_admin";
export const AUTH_COOKIE_CLIENT = "auth_token_client";

export type SessionScope = "admin" | "client" | "any";

type CookieJar = {
  get(name: string): { value: string } | undefined;
};

const COOKIE_ORDER: Record<SessionScope, string[]> = {
  admin: [AUTH_COOKIE_ADMIN, AUTH_COOKIE_LEGACY],
  client: [AUTH_COOKIE_CLIENT, AUTH_COOKIE_LEGACY],
  any: [AUTH_COOKIE_LEGACY, AUTH_COOKIE_ADMIN, AUTH_COOKIE_CLIENT],
};

function roleMatchesScope(role: string, scope: SessionScope): boolean {
  if (scope === "any") return true;
  if (scope === "admin") return role === "admin";
  return role === "client" || role === "investor";
}

export function roleScopedCookieName(role: string): string {
  if (role === "admin") return AUTH_COOKIE_ADMIN;
  if (role === "client" || role === "investor") return AUTH_COOKIE_CLIENT;
  return AUTH_COOKIE_LEGACY;
}

export async function resolveSessionFromCookies(
  cookieJar: CookieJar,
  scope: SessionScope,
): Promise<{ payload: PortalJWTPayload; token: string; cookieName: string } | null> {
  for (const cookieName of COOKIE_ORDER[scope]) {
    const token = cookieJar.get(cookieName)?.value;
    if (!token) continue;

    const payload = await verifyJWT(token);
    if (!payload) continue;
    if (!roleMatchesScope(payload.role, scope)) continue;

    return { payload, token, cookieName };
  }

  return null;
}
