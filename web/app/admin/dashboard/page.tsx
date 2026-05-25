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
import fs from "node:fs/promises";
import path from "node:path";
import { verifyJWT } from "@/lib/auth";
import { getAdminStats } from "@/lib/pamm";
import { prisma } from "@/lib/db";
import { Prisma } from "@prisma/client";
import { OnboardingForm } from "./_components/OnboardingForm";
import { InvestorTable } from "./_components/InvestorTable";

export const dynamic = "force-dynamic";

type DerivOperationRow = {
  trade_id: string;
  symbol: string;
  side: string;
  exit_reason: string;
  allocated_at: Date;
  mirror_accounts: number;
  gross_pnl_total_usdt: Prisma.Decimal;
  net_pnl_total_usdt: Prisma.Decimal;
};

type MirrorRuntimeSnapshot = {
  mirrorEnabled: boolean;
  mirrorCount: number;
  openContracts: number;
};

// ─── Colour palette ───────────────────────────────────────────────────────────
const BG     = "#04070c";
const CARD   = "rgba(10,15,22,0.72)";
const BORD   = "rgba(63,87,114,0.28)";
const TEXT   = "#dce7f5";
const MUTE   = "#6b8299";
const GREEN  = "#10b981";

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

  // ── Fetch KPIs + compact Deriv operation summary ─────────────────────────────
  const [stats, derivOps, runtime] = await Promise.all([
    getAdminStats(),
    prisma.$queryRaw<DerivOperationRow[]>`
      SELECT
        uta.trade_id,
        MAX(uta.symbol)                                  AS symbol,
        MAX(uta.side)                                    AS side,
        MAX(uta.exit_reason)                             AS exit_reason,
        MAX(uta.allocated_at)                            AS allocated_at,
        COUNT(*)::int                                    AS mirror_accounts,
        COALESCE(SUM(uta.gross_pnl_usdt), 0)::numeric    AS gross_pnl_total_usdt,
        COALESCE(SUM(uta.net_pnl_usdt), 0)::numeric      AS net_pnl_total_usdt
      FROM user_trade_allocations uta
      WHERE COALESCE(uta.broker, 'binance') = 'deriv'
      GROUP BY uta.trade_id
      ORDER BY MAX(uta.allocated_at) DESC
      LIMIT 30
    `.catch(() => [] as DerivOperationRow[]),
    readMirrorRuntimeSnapshot(),
  ]);

  const latestOp = derivOps[0] ?? null;
  const latestGross = new Prisma.Decimal(latestOp?.gross_pnl_total_usdt ?? 0);
  const latestNet = new Prisma.Decimal(latestOp?.net_pnl_total_usdt ?? 0);
  const latestFee20 = latestGross.gt(0)
    ? latestGross.mul("0.20")
    : new Prisma.Decimal(0);
  const feePositive = latestFee20.gt(0);

  const kpis: KpiCardData[] = [
    {
      label:  "AUM",
      value:  fmtUSDT(stats.aum),
      sub:    "Capital activo total",
      accent: "#57c1ff",
    },
    {
      label:  "Cuentas Espejo Activas",
      value:  runtime.mirrorCount.toString(),
      sub:    runtime.openContracts > 0
        ? `Operando ahora (${runtime.openContracts} contrato/s abierto/s)`
        : "Sin contratos abiertos ahora",
      accent: runtime.openContracts > 0 ? GREEN : "#a78bfa",
    },
    {
      label:  "Fee Modelo 20% (Ult. Op)",
      value:  fmtUSDT(latestFee20.toFixed(2)),
      sub:    latestOp
        ? `${latestGross.gt(0) ? "Ganancia" : "Sin ganancia"} bruta: ${fmtUSDT(latestGross.toFixed(2))}`
        : "Sin operaciones Deriv cerradas",
      accent: feePositive ? GREEN : "#a78bfa",
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

      {/* ── Compact Deriv audit (one line per operation) ── */}
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
        <p
          style={{
            color: MUTE,
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            margin: "0 0 8px",
          }}
        >
          Auditoria Deriv Compacta
        </p>

        {latestOp ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "2fr 1fr 1fr 1fr",
              gap: 12,
              alignItems: "center",
              background: "rgba(4,7,12,0.62)",
              border: `1px solid ${BORD}`,
              borderRadius: 12,
              padding: "14px 16px",
            }}
          >
            <div>
              <p style={{ margin: 0, color: TEXT, fontSize: 14, fontWeight: 700 }}>
                {latestOp.symbol} [{latestOp.side}] - {latestOp.exit_reason}
              </p>
              <p style={{ margin: "4px 0 0", color: MUTE, fontSize: 11 }}>
                {new Date(latestOp.allocated_at).toLocaleString("es-ES", { timeZone: "UTC" })} UTC · Trade {latestOp.trade_id}
              </p>
            </div>

            <MetricPill
              label="Cuentas en espejo"
              value={String(latestOp.mirror_accounts)}
              accent="#57c1ff"
            />

            <MetricPill
              label="Ganancia bruta"
              value={fmtUSDT(latestGross.toFixed(2))}
              accent={latestGross.gte(0) ? GREEN : "#fb7185"}
            />

            <MetricPill
              label="Cobro 20%"
              value={fmtUSDT(latestFee20.toFixed(2))}
              accent={latestFee20.gt(0) ? "#f4b942" : MUTE}
            />
          </div>
        ) : (
          <div
            style={{
              background: "rgba(4,7,12,0.62)",
              border: `1px solid ${BORD}`,
              borderRadius: 12,
              padding: "14px 16px",
              color: MUTE,
              fontSize: 13,
            }}
          >
            Aun no hay operaciones Deriv para resumir.
          </div>
        )}

        <p style={{ margin: "10px 0 0", color: MUTE, fontSize: 11 }}>
          Neto ultima operacion: {fmtUSDT(latestNet.toFixed(2))} · Inversores activos: {stats.activeClients}
        </p>
      </section>
    </div>
  );
}

async function readMirrorRuntimeSnapshot(): Promise<MirrorRuntimeSnapshot> {
  const root = path.join(process.cwd(), "..");
  const logsDir = process.env.DERIV_STATE_DIR
    ?? process.env.BOT_STATE_DIR
    ?? path.join(root, "logs");

  let mirrorEnabled = false;
  let mirrorCount = 0;
  let openContracts = 0;

  try {
    const statusRaw = await fs.readFile(path.join(logsDir, "deriv_status.json"), "utf8");
    const status = JSON.parse(statusRaw) as Record<string, unknown>;
    const multi = (status.multi_account ?? {}) as Record<string, unknown>;
    mirrorEnabled = Boolean(multi.mirror_enabled);
    mirrorCount = Number(multi.mirror_count ?? 0) || 0;
  } catch {
    // keep defaults
  }

  try {
    const openRaw = await fs.readFile(path.join(logsDir, "deriv_open_contracts.json"), "utf8");
    const open = JSON.parse(openRaw);
    if (Array.isArray(open)) {
      openContracts = open.length;
    }
  } catch {
    // keep defaults
  }

  if (!mirrorEnabled) {
    mirrorCount = 0;
  }

  return { mirrorEnabled, mirrorCount, openContracts };
}

function MetricPill({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.02)",
        border: `1px solid ${BORD}`,
        borderRadius: 10,
        padding: "10px 12px",
      }}
    >
      <p
        style={{
          margin: 0,
          color: MUTE,
          fontSize: 10,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
        }}
      >
        {label}
      </p>
      <p
        style={{
          margin: "4px 0 0",
          color: accent,
          fontSize: 16,
          fontWeight: 700,
          fontFamily: "ui-monospace, Menlo, monospace",
        }}
      >
        {value}
      </p>
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
