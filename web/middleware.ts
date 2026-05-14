import { NextResponse, type NextRequest } from "next/server";
import { verifyJWT } from "@/lib/auth";

// ─── Protected route prefixes ─────────────────────────────────────────────────
// Add portal routes here as the Fase 2+ modules are built.
const PROTECTED_PREFIXES = ["/portal", "/api/portfolio", "/api/admin"];

// ─── Public auth routes (never redirect-loop) ─────────────────────────────────
const AUTH_PATHS = [
  "/portal/login",
  "/api/auth/request-otp",
  "/api/auth/verify-otp",
];

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Skip middleware for static assets and Next.js internals.
  if (
    pathname.startsWith("/_next/") ||
    pathname.startsWith("/favicon") ||
    pathname === "/"
  ) {
    return NextResponse.next();
  }

  const isProtected = PROTECTED_PREFIXES.some((prefix) =>
    pathname.startsWith(prefix)
  );
  if (!isProtected) return NextResponse.next();

  // Auth routes are always accessible — avoid redirect loops.
  const isAuthRoute = AUTH_PATHS.some(
    (p) => pathname === p || pathname.startsWith(p)
  );
  if (isAuthRoute) return NextResponse.next();

  // ── Verify JWT from cookie ───────────────────────────────────────────────
  const token = request.cookies.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;

  if (!payload) {
    // API routes: return 401 JSON instead of redirecting.
    if (pathname.startsWith("/api/")) {
      return NextResponse.json(
        { success: false, error: "No autenticado." },
        { status: 401 }
      );
    }
    // Browser routes: redirect to login.
    const loginUrl = request.nextUrl.clone();
    loginUrl.pathname = "/portal/login";
    loginUrl.search = `?next=${encodeURIComponent(pathname)}`;
    return NextResponse.redirect(loginUrl);
  }

  // ── Admin-only guard ──────────────────────────────────────────────────────
  if (pathname.startsWith("/api/admin") && payload.role !== "admin") {
    return NextResponse.json(
      { success: false, error: "Acceso denegado." },
      { status: 403 }
    );
  }

  // ── Forward authenticated identity to downstream handlers ─────────────────
  // These headers are set on the internal request — never sent to the client.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-user-id", payload.sub);
  requestHeaders.set("x-user-role", payload.role);

  return NextResponse.next({ request: { headers: requestHeaders } });
}

export const config = {
  // Run middleware only on paths that could be protected.
  // Excludes static files and Next.js internals via the negative lookahead.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|webp)).*)",
  ],
};
