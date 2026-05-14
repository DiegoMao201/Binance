"use client";

import { useEffect, useState, useCallback } from "react";

const REFRESH_INTERVAL_MS = 30_000;
const WIN_RATE_TARGET = 55;

function fmt(val, decimals = 2, suffix = "") {
  if (val === null || val === undefined) return "—";
  if (val === Infinity) return "∞";
  return `${Number(val).toFixed(decimals)}${suffix}`;
}

function pnlColor(val) {
  if (val === null || val === undefined) return "#8899aa";
  return val > 0 ? "#4ade80" : val < 0 ? "#f87171" : "#8899aa";
}

function GaugeBar({ value, target = WIN_RATE_TARGET }) {
  const pct = Math.min(100, Math.max(0, value || 0));
  const onTarget = pct >= target;
  const barColor = onTarget ? "#4ade80" : pct >= target * 0.9 ? "#f4b942" : "#f87171";
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#8899aa", marginBottom: 4 }}>
        <span>0%</span>
        <span style={{ color: "#f4b942" }}>Target {target}%</span>
        <span>100%</span>
      </div>
      <div style={{ background: "rgba(255,255,255,0.07)", borderRadius: 6, height: 14, overflow: "hidden", position: "relative" }}>
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: barColor,
            borderRadius: 6,
            transition: "width 0.6s ease",
          }}
        />
        {/* target marker */}
        <div
          style={{
            position: "absolute",
            left: `${target}%`,
            top: 0,
            bottom: 0,
            width: 2,
            background: "#f4b942",
          }}
        />
      </div>
      <div style={{ textAlign: "center", marginTop: 6, fontSize: 22, fontWeight: 700, color: barColor }}>
        {fmt(value, 1, "%")}
      </div>
    </div>
  );
}

function KpiCard({ label, value, sub, color }) {
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.04)",
        border: "1px solid rgba(255,255,255,0.08)",
        borderRadius: 10,
        padding: "16px 20px",
        minWidth: 140,
        flex: "1 1 140px",
      }}
    >
      <div style={{ fontSize: 10, color: "#6b8299", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 700, color: color || "#e2e8f0" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: "#6b8299", marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function BreakdownTable({ data, title }) {
  if (!data || !Object.keys(data).length) return null;
  const rows = Object.entries(data).sort((a, b) => b[1].trades - a[1].trades);
  return (
    <div style={{ marginBottom: 28 }}>
      <div style={{ fontSize: 11, color: "#6b8299", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 10 }}>{title}</div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Trades</th>
              <th>Wins</th>
              <th>Win Rate</th>
              <th>Net PnL</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, d]) => (
              <tr key={key}>
                <td style={{ fontFamily: "monospace", color: "#94a3b8" }}>{key}</td>
                <td>{d.trades}</td>
                <td>{d.wins}</td>
                <td style={{ color: (d.win_rate_pct || 0) >= WIN_RATE_TARGET ? "#4ade80" : "#f87171" }}>
                  {fmt(d.win_rate_pct, 1, "%")}
                </td>
                <td style={{ color: pnlColor(d.pnl_usdt), fontFamily: "monospace" }}>
                  {fmt(d.pnl_usdt, 2)} USDT
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function CohortPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/cohort-analytics", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setLastUpdated(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [fetchData]);

  const styles = {
    page: {
      minHeight: "100vh",
      background: "#0d1117",
      color: "#e2e8f0",
      fontFamily: "'Inter', 'Segoe UI', system-ui, sans-serif",
      padding: "24px 20px",
      maxWidth: 960,
      margin: "0 auto",
    },
    header: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 28 },
    title: { fontSize: 20, fontWeight: 700, color: "#94a3b8" },
    subtitle: { fontSize: 12, color: "#6b8299", marginTop: 2 },
    badge: (ok) => ({
      display: "inline-block",
      padding: "3px 10px",
      borderRadius: 99,
      fontSize: 11,
      fontWeight: 700,
      background: ok ? "rgba(74,222,128,0.15)" : "rgba(248,113,113,0.15)",
      color: ok ? "#4ade80" : "#f87171",
      border: `1px solid ${ok ? "rgba(74,222,128,0.3)" : "rgba(248,113,113,0.3)"}`,
    }),
    section: {
      background: "rgba(255,255,255,0.03)",
      border: "1px solid rgba(255,255,255,0.07)",
      borderRadius: 12,
      padding: "20px 24px",
      marginBottom: 20,
    },
    sectionTitle: { fontSize: 12, color: "#6b8299", textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 16 },
    kpiRow: { display: "flex", gap: 12, flexWrap: "wrap" },
  };

  if (loading) {
    return (
      <div style={{ ...styles.page, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ color: "#6b8299" }}>Loading cohort data…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...styles.page, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ color: "#f87171" }}>Error: {error}</div>
      </div>
    );
  }

  const v3 = data?.v3 || {};
  const legacy = data?.legacy || {};
  const summary = data?.summary || {};
  const target = data?.target || {};

  return (
    <div style={styles.page}>
      {/* ── Header ── */}
      <div style={styles.header}>
        <div>
          <div style={styles.title}>V3 Cohort Analytics</div>
          <div style={styles.subtitle}>
            Regime-Aware AI Prompt · cutoff {data?.cutoff_iso?.slice(0, 10)} · refreshes every 30s
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          {target.on_track !== null && (
            <span style={styles.badge(target.on_track)}>
              {target.on_track ? "✓ ON TARGET" : `⚠ ${fmt(target.gap_to_target_pct, 1)}% below`}
            </span>
          )}
          {lastUpdated && (
            <div style={{ fontSize: 10, color: "#6b8299", marginTop: 6 }}>
              Updated {lastUpdated.toLocaleTimeString()}
            </div>
          )}
        </div>
      </div>

      {/* ── Win Rate gauge ── */}
      <div style={styles.section}>
        <div style={styles.sectionTitle}>Win Rate — Target {WIN_RATE_TARGET}%</div>
        <GaugeBar value={v3.win_rate_pct} target={WIN_RATE_TARGET} />
      </div>

      {/* ── Primary KPIs ── */}
      <div style={styles.section}>
        <div style={styles.sectionTitle}>V3 Cohort KPIs ({v3.total_trades ?? 0} trades)</div>
        <div style={styles.kpiRow}>
          <KpiCard
            label="Profit Factor"
            value={v3.profit_factor === Infinity ? "∞" : fmt(v3.profit_factor, 2)}
            sub="≥ 1.5 = good"
            color={(v3.profit_factor || 0) >= 1.5 ? "#4ade80" : "#f4b942"}
          />
          <KpiCard
            label="Net PnL"
            value={`${fmt(v3.net_pnl_usdt, 2)} USDT`}
            sub={`${v3.wins ?? 0}W / ${v3.losses ?? 0}L`}
            color={pnlColor(v3.net_pnl_usdt)}
          />
          <KpiCard
            label="Avg Win"
            value={`${fmt(v3.avg_win_usdt, 4)} USDT`}
            color="#4ade80"
          />
          <KpiCard
            label="Avg Loss"
            value={`-${fmt(v3.avg_loss_usdt, 4)} USDT`}
            color="#f87171"
          />
          <KpiCard
            label="EV / Trade"
            value={`${fmt(v3.ev_per_trade_usdt, 4)} USDT`}
            sub="Expected Value"
            color={pnlColor(v3.ev_per_trade_usdt)}
          />
          <KpiCard
            label="Micro-Gate Entries"
            value={summary.micro_gate_entries ?? "—"}
            sub={`of ${summary.total_v3 ?? 0} total`}
          />
        </div>
      </div>

      {/* ── Breakdowns ── */}
      <div style={styles.section}>
        <div style={styles.sectionTitle}>Breakdowns</div>
        <BreakdownTable data={v3.by_path} title="Entry Path (standard vs micro_gate)" />
        <BreakdownTable data={v3.by_regime} title="AI Regime" />
        <BreakdownTable data={v3.by_scenario} title="Scenario" />
        <BreakdownTable data={v3.by_exit_reason} title="Exit Reason" />
        <BreakdownTable data={v3.by_entry_logic} title="Entry Logic Tag" />
      </div>

      {/* ── Legacy comparison ── */}
      {legacy.total_trades > 0 && (
        <div style={styles.section}>
          <div style={styles.sectionTitle}>Legacy Cohort (pre-V3 · {legacy.total_trades} trades)</div>
          <div style={styles.kpiRow}>
            <KpiCard label="Win Rate" value={fmt(legacy.win_rate_pct, 1, "%")} color={pnlColor(legacy.win_rate_pct - 50)} />
            <KpiCard label="Profit Factor" value={fmt(legacy.profit_factor, 2)} color="#8899aa" />
            <KpiCard label="Net PnL" value={`${fmt(legacy.net_pnl_usdt, 2)} USDT`} color={pnlColor(legacy.net_pnl_usdt)} />
            <KpiCard label="EV / Trade" value={`${fmt(legacy.ev_per_trade_usdt, 4)} USDT`} color={pnlColor(legacy.ev_per_trade_usdt)} />
          </div>
        </div>
      )}

      {/* ── All-time totals ── */}
      <div style={{ fontSize: 11, color: "#4a5568", textAlign: "center", paddingTop: 8 }}>
        All-time trades: {summary.total_all_time ?? "—"} ·
        V3: {summary.total_v3 ?? "—"} ·
        Legacy: {summary.total_legacy ?? "—"}
      </div>
    </div>
  );
}
