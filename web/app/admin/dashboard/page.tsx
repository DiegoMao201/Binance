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
import { OnboardingForm } from "./_components/OnboardingForm";
import { InvestorTable } from "./_components/InvestorTable";

export const dynamic = "force-dynamic";

// ─── Colour palette ───────────────────────────────────────────────────────────
const BG    = "#080e16";
const CARD  = "#0a1018";
const BORD  = "#1a2b3c";
const TEXT  = "#dce7f5";
const MUTE  = "#6b8299";
const GREEN = "#12d98b";

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

  // ── Fetch KPIs and investor list ─────────────────────────────────────────────
  const stats = await getAdminStats();

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
        background: BG,
        color: TEXT,
        fontFamily:
          "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace",
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
              fontSize: 20,
              fontWeight: 700,
              margin: 0,
              letterSpacing: "-0.02em",
            }}
          >
            Admin Backoffice
          </h1>
          <p style={{ color: MUTE, fontSize: 12, margin: "4px 0 0" }}>
            OptiFerre PAMM — Panel de Control
          </p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span
            style={{
              background: `${GREEN}1a`,
              border: `1px solid ${GREEN}44`,
              color: GREEN,
              fontSize: 11,
              fontWeight: 600,
              padding: "4px 12px",
              borderRadius: 20,
              letterSpacing: "0.04em",
            }}
          >
            ● LIVE
          </span>
          <span style={{ color: MUTE, fontSize: 12 }}>
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
        }}
      >
        <OnboardingForm />
        <InvestorTable investors={stats.investors} />
      </div>
    </div>
  );
}

// ─── KPI Card (pure presentational — no interactivity needed) ─────────────────
function KpiCard({ label, value, sub, accent }: KpiCardData) {
  return (
    <div
      style={{
        background: CARD,
        border: `1px solid ${BORD}`,
        borderRadius: 16,
        padding: "20px 22px",
        borderLeft: `3px solid ${accent}`,
      }}
    >
      <p
        style={{
          color: MUTE,
          fontSize: 10,
          fontWeight: 600,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          marginBottom: 8,
        }}
      >
        {label}
      </p>
      <p
        style={{
          color: TEXT,
          fontSize: 26,
          fontWeight: 700,
          fontFamily: "monospace",
          letterSpacing: "-0.02em",
          marginBottom: 6,
        }}
      >
        {value}
      </p>
      <p style={{ color: MUTE, fontSize: 11 }}>{sub}</p>
    </div>
  );
}
