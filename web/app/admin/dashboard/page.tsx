/**
 * app/admin/dashboard/page.tsx — Admin Backoffice Dashboard
 *
 * Server Component: data is fetched server-side and rendered as HTML.
 * Auth is verified server-side (double-layer after middleware).
 * Client Components are used ONLY for interactive elements.
 *
 * force-dynamic prevents Next.js from caching this page — financial KPIs
 * must always reflect the current database state.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { verifyJWT } from "@/lib/auth";
import { getAdminStats } from "@/lib/pamm";
import { prisma } from "@/lib/db";
import { OnboardingForm } from "./_components/OnboardingForm";
import { InvestorTable } from "./_components/InvestorTable";
import { GlobalOpsLog, type LedgerRow } from "./_components/GlobalOpsLog";

export const dynamic = "force-dynamic";

// ─── Colour palette ───────────────────────────────────────────────────────────
const BG     = "#04070c";
const CARD   = "rgba(10,15,22,0.72)";
const BORD   = "rgba(63,87,114,0.28)";
const TEXT   = "#dce7f5";
const MUTE   = "#6b8299";
const GREEN  = "#10b981";
const GLASSB = "rgba(4,7,12,0.72)";

function fmtUSDT(v: string): string {
  return "$" + Number(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// ─── KPI card data ────────────────────────────────────────────────────────────
type KpiCardData = {
  label: string;
  value: string;
  sub: string;
  accent: string;
};

// ─── Page ─────────────────────────────────────────────────────────────────────
export default async function AdminDashboardPage() {
  // ── Server-side auth guard (role: admin required) ────────────────────────────
  const cookieStore = await cookies();
  const token = cookieStore.get("auth_token")?.value;
  const payload = token ? await verifyJWT(token) : null;

  if (!payload || payload.role !== "admin") {
    redirect("/portal/login?next=/admin/dashboard");
  }

  // ── Fetch KPIs, investor list, and full ledger log ────────────────────────────
  const [stats, rawLedger] = await Promise.all([
    getAdminStats(),
    // Fetch last 2 000 ledger entries (all users, DESC) for the Ops Log.
    // Joined with users to get a display alias without a second query.
    prisma.ledgerTransaction.findMany({
      orderBy: { createdAt: "desc" },
      take: 2000,
      include: { user: { select: { email: true, name: true } } },
    }),
  ]);

  // Serialise Decimal + Date + BigInt → plain strings (Server → Client boundary).
  const ledgerRows: LedgerRow[] = rawLedger.map((row) => ({
    id:          row.id.toString(),
    userId:      row.userId,
    userAlias:   row.user.name ?? row.user.email,
    type:        row.type,
    amount:      row.amountUsdt.toFixed(8),
    description: row.description ?? null,
    createdAt:   row.createdAt instanceof Date
                   ? row.createdAt.toISOString()
                   : String(row.createdAt),
    broker:      ((row as { broker?: string | null }).broker ?? "binance").toLowerCase(),
  }));

  const kpis: KpiCardData[] = [
    {
      label:  "AUM",
      value:  fmtUSDT(stats.aum),
      sub:    "Capital operativo total",
      accent: "#57c1ff",
    },
    {
      label:  "Total Revenue",
      value:  fmtUSDT(stats.totalRevenue),
      sub:    `Entry ${fmtUSDT(stats.entryFeeRevenue)} + Perf ${fmtUSDT(stats.performanceFeeRevenue)}`,
      accent: GREEN,
    },
    {
      label:  "Active Clients",
      value:  stats.activeClients.toString(),
      sub:    "Inversores activos",
      accent: "#a78bfa",
    },
  ];

  return (
    <div
      style={{
        minHeight: "100vh",
        background:
          `radial-gradient(900px 600px at 12% -10%, rgba(168,85,247,0.09), transparent 60%),` +
          `radial-gradient(900px 600px at 100% 0%, rgba(6,182,212,0.07), transparent 65%),` +
          BG,
        color: TEXT,
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        padding: "28px 24px",
        boxSizing: "border-box",
      }}
    >
      {/* ── Header ── */}
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 32,
        }}
      >
        <div>
          <h1
            style={{
              fontSize: 22,
              fontWeight: 800,
              margin: 0,
              letterSpacing: "-0.02em",
            }}
          >
            ◈ OptiFerre{" "}
            <span style={{ color: "#c084fc" }}>Mission Control</span>
          </h1>
          <p style={{ color: MUTE, fontSize: 11, margin: "4px 0 0", letterSpacing: "0.08em" }}>
            PAMM ADMIN · v2.0
          </p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span
            style={{
              background: `rgba(16,185,129,0.12)`,
              border: `1px solid rgba(16,185,129,0.35)`,
              color: GREEN,
              fontSize: 10,
              fontWeight: 700,
              padding: "5px 14px",
              borderRadius: 20,
              letterSpacing: "0.14em",
              boxShadow: `0 0 10px rgba(16,185,129,0.25)`,
            }}
          >
            ● LIVE
          </span>
          <span style={{ color: MUTE, fontSize: 12, fontFamily: "ui-monospace, Menlo, monospace" }}>
            {payload.sub.slice(0, 8)}…
          </span>
        </div>
      </header>

      {/* ── KPI Cards ── */}
      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: 16,
          marginBottom: 28,
        }}
      >
        {kpis.map((kpi) => (
          <KpiCard key={kpi.label} {...kpi} />
        ))}
      </section>

      {/* ── Onboarding + Investor Grid ── */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "320px 1fr",
          gap: 20,
          alignItems: "start",
          marginBottom: 32,
        }}
      >
        <OnboardingForm />
        <InvestorTable investors={stats.investors} />
      </div>

      {/* ── Global Operations Log ── */}
      <section
        style={{
          background: CARD,
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          border: `1px solid ${BORD}`,
          borderRadius: 20,
          padding: "24px 22px",
          boxShadow: "0 0 0 1px rgba(255,255,255,0.02) inset, 0 16px 40px -20px rgba(6,182,212,0.25)",
        }}
      >
        <GlobalOpsLog rows={ledgerRows} />
      </section>
    </div>
  );
}

// ─── KPI Card (glassmorphism neon — cyberpunk terminal) ──────────────────────
function KpiCard({ label, value, sub, accent }: KpiCardData) {
  return (
    <div
      style={{
        position: "relative",
        background: CARD,
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
        border: `1px solid ${BORD}`,
        borderLeft: `2px solid ${accent}`,
        borderRadius: 18,
        padding: "20px 22px",
        boxShadow: `0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px ${accent}66`,
        overflow: "hidden",
      }}
    >
      {/* Ambient glow blob */}
      <div
        style={{
          position: "absolute",
          top: -30,
          right: -30,
          width: 100,
          height: 100,
          borderRadius: "50%",
          background: `radial-gradient(circle, ${accent}18, transparent 70%)`,
          pointerEvents: "none",
        }}
      />
      <p
        style={{
          position: "relative",
          color: MUTE,
          fontSize: 9.5,
          fontWeight: 700,
          letterSpacing: "0.18em",
          textTransform: "uppercase",
          marginBottom: 12,
          fontFamily: "ui-monospace, Menlo, monospace",
        }}
      >
        {label}
      </p>
      <p
        style={{
          position: "relative",
          color: accent,
          fontSize: 30,
          fontWeight: 800,
          fontFamily: "ui-monospace, Menlo, monospace",
          letterSpacing: "-0.02em",
          marginBottom: 8,
          textShadow: `0 0 14px ${accent}88`,
          lineHeight: 1.05,
        }}
      >
        {value}
      </p>
      <p style={{ position: "relative", color: MUTE, fontSize: 11, letterSpacing: "0.04em" }}>{sub}</p>
    </div>
  );
}
