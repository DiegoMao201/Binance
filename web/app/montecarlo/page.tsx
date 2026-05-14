/**
 * app/montecarlo/page.tsx
 * Route: /montecarlo
 *
 * Server component — reads live bot state and passes equity + win rate
 * to the client-side Monte Carlo simulator as seed values.
 */

import Link from "next/link";
import { readDashboardState } from "../../lib/read-dashboard-state";
import MonteCarloSimulator from "../../components/MonteCarloSimulator";

export const dynamic = "force-dynamic";

export default async function MonteCarloPage() {
  const data = await readDashboardState();

  // readDashboardState() returns: { state: bot_state.json, status, ... }
  // Portfolio and risk live inside state.state (bot_state.json contents)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const botState  = (data?.state  ?? {}) as Record<string, any>;
  const portfolio = botState?.portfolio ?? {};
  const risk      = botState?.risk      ?? {};

  const liveEquity   = Number(risk?.equity_usd    || portfolio?.equity_usd    || 40);
  const liveWinRate  = Number(portfolio?.win_rate_pct || 0);  // 0–100 scale

  return (
    <div style={{
      minHeight: "100vh",
      background: "#080e16",
      fontFamily: "system-ui, sans-serif",
      color: "#dce7f5",
    }}>
      {/* ── Sticky Header ───────────────────────────────────────────────────── */}
      <header style={{
        position: "sticky", top: 0, zIndex: 50,
        background: "rgba(8,14,22,0.95)",
        backdropFilter: "blur(12px)",
        borderBottom: "1px solid #1a2b3c",
        padding: "12px 20px",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Link href="/" style={{
            color: "#6b8299", textDecoration: "none", fontSize: 12,
            padding: "4px 10px", borderRadius: 6,
            border: "1px solid #1a2b3c", background: "rgba(255,255,255,0.02)",
          }}>
            ← Dashboard
          </Link>
          <Link href="/matriz" style={{
            color: "#6b8299", textDecoration: "none", fontSize: 12,
            padding: "4px 10px", borderRadius: 6,
            border: "1px solid #1a2b3c", background: "rgba(255,255,255,0.02)",
          }}>
            Matriz →
          </Link>
          <div>
            <span style={{ fontSize: 14, fontWeight: 700 }}>Monte Carlo</span>
            <span style={{ fontSize: 11, color: "#6b8299", marginLeft: 10 }}>
              Proyección estocástica · OptiFerre-Trader v2
            </span>
          </div>
        </div>

        {/* Live seed indicator */}
        {liveEquity > 0 && (
          <div style={{
            fontSize: 10, color: "#6b8299",
            background: "rgba(18,217,139,0.06)",
            border: "1px solid rgba(18,217,139,0.2)",
            borderRadius: 6, padding: "4px 10px",
          }}>
            Semilla: equity en vivo{" "}
            <span style={{ color: "#12d98b", fontFamily: "monospace", fontWeight: 700 }}>
              ${liveEquity.toFixed(2)}
            </span>
            {liveWinRate > 0 && (
              <span style={{ marginLeft: 8 }}>
                · WR live{" "}
                <span style={{ color: "#f4b942", fontFamily: "monospace", fontWeight: 700 }}>
                  {liveWinRate.toFixed(1)}%
                </span>
              </span>
            )}
          </div>
        )}
      </header>

      {/* ── Page body ───────────────────────────────────────────────────────── */}
      <main style={{ maxWidth: 1200, margin: "0 auto", padding: "24px 16px 60px" }}>
        <MonteCarloSimulator
          initialEquity={liveEquity > 0 ? liveEquity : 40}
          liveWinRatePct={liveWinRate > 0 ? liveWinRate : undefined}
        />
      </main>
    </div>
  );
}
