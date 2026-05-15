"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";

const REFRESH_INTERVAL_MS = 30_000;
const WIN_RATE_TARGET = 55;

// ── Tokens ────────────────────────────────────────────────────────
const BG       = "#0a0e14";
const SURFACE  = "rgba(255,255,255,0.03)";
const SURFACE2 = "rgba(255,255,255,0.05)";
const BORD     = "rgba(255,255,255,0.08)";
const TEXT     = "#e2e8f0";
const MUTE     = "#6b8299";
const SUB      = "#94a3b8";
const G        = "#4ade80";
const R        = "#f87171";
const Y        = "#f4b942";
const B        = "#60a5fa";
const P        = "#a78bfa";

const CADENCE_COLORS = {
  turbo:         { bg: "rgba(248,113,113,0.16)", fg: "#f87171", border: "rgba(248,113,113,0.35)" },
  rapida:        { bg: "rgba(74,222,128,0.16)",  fg: "#4ade80", border: "rgba(74,222,128,0.35)" },
  estandar:      { bg: "rgba(244,185,66,0.16)",  fg: "#f4b942", border: "rgba(244,185,66,0.35)" },
  institucional: { bg: "rgba(96,165,250,0.16)",  fg: "#60a5fa", border: "rgba(96,165,250,0.35)" },
  unknown:       { bg: "rgba(120,120,120,0.16)", fg: "#9ca3af", border: "rgba(120,120,120,0.35)" },
};

const SEVERITY_COLORS = {
  high:   { bg: "rgba(248,113,113,0.10)", fg: R,  border: "rgba(248,113,113,0.35)", icon: "▲" },
  medium: { bg: "rgba(244,185,66,0.10)",  fg: Y,  border: "rgba(244,185,66,0.35)",  icon: "●" },
  info:   { bg: "rgba(96,165,250,0.10)",  fg: B,  border: "rgba(96,165,250,0.35)",  icon: "ℹ" },
  low:    { bg: "rgba(148,163,184,0.10)", fg: SUB, border: "rgba(148,163,184,0.35)", icon: "·" },
};

// ── Helpers ───────────────────────────────────────────────────────
function fmt(val, decimals = 2, suffix = "") {
  if (val === null || val === undefined) return "—";
  if (val === Infinity) return "∞";
  return `${Number(val).toFixed(decimals)}${suffix}`;
}
function pnlColor(val) {
  if (val === null || val === undefined) return MUTE;
  return val > 0 ? G : val < 0 ? R : MUTE;
}
function wrColor(wr) {
  if (wr === null || wr === undefined) return MUTE;
  if (wr >= WIN_RATE_TARGET) return G;
  if (wr >= WIN_RATE_TARGET * 0.85) return Y;
  return R;
}
function heatColor(wr) {
  // Heatmap cell color (transparent green→red)
  if (wr === null || wr === undefined) return "rgba(255,255,255,0.02)";
  const clamped = Math.min(100, Math.max(0, wr));
  if (clamped >= WIN_RATE_TARGET) {
    const t = (clamped - WIN_RATE_TARGET) / (100 - WIN_RATE_TARGET);
    return `rgba(74,222,128,${0.18 + t * 0.32})`;
  }
  const t = clamped / WIN_RATE_TARGET;
  return `rgba(248,113,113,${0.40 - t * 0.22})`;
}

// ── Components ────────────────────────────────────────────────────
function GaugeBar({ value, target = WIN_RATE_TARGET }) {
  const pct = Math.min(100, Math.max(0, value || 0));
  const onTarget = pct >= target;
  const barColor = onTarget ? G : pct >= target * 0.85 ? Y : R;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: MUTE, marginBottom: 4 }}>
        <span>0%</span>
        <span style={{ color: Y }}>Target {target}%</span>
        <span>100%</span>
      </div>
      <div style={{ background: "rgba(255,255,255,0.07)", borderRadius: 6, height: 14, overflow: "hidden", position: "relative" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: barColor, borderRadius: 6, transition: "width 0.6s ease" }} />
        <div style={{ position: "absolute", left: `${target}%`, top: 0, bottom: 0, width: 2, background: Y }} />
      </div>
      <div style={{ textAlign: "center", marginTop: 6, fontSize: 22, fontWeight: 700, color: barColor, fontFamily: "monospace" }}>
        {fmt(value, 1, "%")}
      </div>
    </div>
  );
}

function KpiCard({ label, value, sub, color, accent }) {
  return (
    <div style={{
      background: SURFACE,
      border: `1px solid ${accent || BORD}`,
      borderRadius: 10,
      padding: "14px 16px",
      flex: "1 1 140px",
      minWidth: 140,
    }}>
      <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase", letterSpacing: "0.07em", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || TEXT, fontFamily: "monospace" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: MUTE, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function CadencePill({ tag }) {
  const c = CADENCE_COLORS[tag] || CADENCE_COLORS.unknown;
  return (
    <span style={{
      display: "inline-block", padding: "2px 8px", borderRadius: 99,
      fontSize: 9, fontWeight: 700, letterSpacing: "0.08em",
      background: c.bg, color: c.fg, border: `1px solid ${c.border}`,
      textTransform: "uppercase",
    }}>
      {tag}
    </span>
  );
}

function SectionTitle({ children, hint }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 14 }}>
      <div style={{ fontSize: 12, color: MUTE, textTransform: "uppercase", letterSpacing: "0.08em", fontWeight: 700 }}>
        {children}
      </div>
      {hint && <div style={{ fontSize: 11, color: MUTE }}>{hint}</div>}
    </div>
  );
}

// ── Hero: V3 vs All-time side-by-side ─────────────────────────────
function PerformanceHero({ summary, target, v3, allTime }) {
  const at = allTime || {};
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 16, marginBottom: 18 }}>
      {/* V3 cohort card */}
      <div style={{ background: SURFACE, border: `1px solid ${BORD}`, borderRadius: 14, padding: "20px 22px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 4 }}>
          <div>
            <div style={{ fontSize: 11, color: P, textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 700 }}>V3 Cohort</div>
            <div style={{ fontSize: 22, fontWeight: 700, color: TEXT }}>Regime-Aware Era</div>
            <div style={{ fontSize: 11, color: MUTE, marginTop: 2 }}>
              {summary?.total_v3 ?? 0} trades · prompt v3 estricto
            </div>
          </div>
          {target?.on_track !== null && target?.on_track !== undefined && (
            <span style={{
              padding: "4px 10px", borderRadius: 99, fontSize: 10, fontWeight: 700,
              background: target.on_track ? "rgba(74,222,128,0.15)" : "rgba(248,113,113,0.15)",
              color: target.on_track ? G : R,
              border: `1px solid ${target.on_track ? "rgba(74,222,128,0.35)" : "rgba(248,113,113,0.35)"}`,
            }}>
              {target.on_track ? "✓ ON TARGET" : `${fmt(target.gap_to_target_pct, 1)}% bajo`}
            </span>
          )}
        </div>
        <GaugeBar value={v3?.win_rate_pct} target={WIN_RATE_TARGET} />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginTop: 14 }}>
          <MiniStat label="Net PnL" value={`${fmt(v3?.net_pnl_usdt, 2)} USDT`} color={pnlColor(v3?.net_pnl_usdt)} />
          <MiniStat label="Profit F." value={fmt(v3?.profit_factor, 2)} color={(v3?.profit_factor ?? 0) >= 1.5 ? G : (v3?.profit_factor ?? 0) >= 1 ? Y : R} />
          <MiniStat label="W / L" value={`${v3?.wins ?? 0} / ${v3?.losses ?? 0}`} color={SUB} />
          <MiniStat label="Avg Win" value={fmt(v3?.avg_win_usdt, 4)} color={G} />
          <MiniStat label="Avg Loss" value={`-${fmt(v3?.avg_loss_usdt, 4)}`} color={R} />
          <MiniStat label="EV/Trade" value={fmt(v3?.ev_per_trade_usdt, 4)} color={pnlColor(v3?.ev_per_trade_usdt)} />
        </div>
      </div>

      {/* All-time card */}
      <div style={{ background: SURFACE, border: `1px solid ${BORD}`, borderRadius: 14, padding: "20px 22px" }}>
        <div style={{ marginBottom: 4 }}>
          <div style={{ fontSize: 11, color: SUB, textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 700 }}>All-time</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: TEXT }}>Histórico completo</div>
          <div style={{ fontSize: 11, color: MUTE, marginTop: 2 }}>
            {summary?.total_all_time ?? 0} trades · V3 ({summary?.total_v3 ?? 0}) + Legacy ({summary?.total_legacy ?? 0})
          </div>
        </div>
        <GaugeBar value={at.win_rate_pct} target={WIN_RATE_TARGET} />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginTop: 14 }}>
          <MiniStat label="Net PnL" value={`${fmt(at.net_pnl_usdt, 2)} USDT`} color={pnlColor(at.net_pnl_usdt)} />
          <MiniStat label="Profit F." value={fmt(at.profit_factor, 2)} color={(at.profit_factor ?? 0) >= 1.5 ? G : (at.profit_factor ?? 0) >= 1 ? Y : R} />
          <MiniStat label="W / L" value={`${at.wins ?? 0} / ${at.losses ?? 0}`} color={SUB} />
          <MiniStat label="Avg Win" value={fmt(at.avg_win_usdt, 4)} color={G} />
          <MiniStat label="Avg Loss" value={`-${fmt(at.avg_loss_usdt, 4)}`} color={R} />
          <MiniStat label="EV/Trade" value={fmt(at.ev_per_trade_usdt, 4)} color={pnlColor(at.ev_per_trade_usdt)} />
        </div>
      </div>
    </div>
  );
}

function MiniStat({ label, value, color }) {
  return (
    <div>
      <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase", letterSpacing: "0.07em" }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: color || TEXT, fontFamily: "monospace", marginTop: 2 }}>{value}</div>
    </div>
  );
}

// ── Insights panel (actionable refinement cards) ──────────────────
function InsightsPanel({ insights }) {
  if (!insights || insights.length === 0) {
    return (
      <div style={{ ...sectionStyle, padding: "20px 24px" }}>
        <SectionTitle hint="auto-generadas desde la cohorte V3">Refinement Insights</SectionTitle>
        <div style={{ fontSize: 13, color: MUTE, padding: "16px 0" }}>
          Sin insights críticos por el momento. Sigue acumulando trades.
        </div>
      </div>
    );
  }
  return (
    <div style={{ ...sectionStyle, padding: "20px 24px" }}>
      <SectionTitle hint={`${insights.length} señales accionables`}>Refinement Insights</SectionTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
        {insights.map((ins, i) => {
          const c = SEVERITY_COLORS[ins.severity] || SEVERITY_COLORS.low;
          return (
            <div key={i} style={{
              background: c.bg, border: `1px solid ${c.border}`, borderRadius: 10, padding: "12px 14px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                <span style={{ fontSize: 11, color: c.fg, fontWeight: 700 }}>{c.icon}</span>
                <span style={{ fontSize: 9, color: c.fg, fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}>
                  {ins.severity} · {ins.dimension}
                </span>
              </div>
              <div style={{ fontSize: 13, color: TEXT, fontWeight: 600, marginBottom: 6, lineHeight: 1.35 }}>
                {ins.headline}
              </div>
              <div style={{ fontSize: 11, color: SUB, lineHeight: 1.45 }}>
                <span style={{ color: c.fg, fontWeight: 700 }}>Acción: </span>{ins.action}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Per-market grid with cadence + scenario sub-stats ─────────────
function PerMarketGrid({ bySymbol, title }) {
  const symbols = Object.values(bySymbol || {}).sort((a, b) => a.cadence_priority - b.cadence_priority);
  if (!symbols.length) return null;
  return (
    <div style={{ ...sectionStyle, padding: "20px 24px" }}>
      <SectionTitle hint={`${symbols.length} mercados activos · ordenados por prioridad de scaneo`}>{title}</SectionTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
        {symbols.map((s) => (
          <div key={s.symbol} style={{
            background: SURFACE, border: `1px solid ${BORD}`, borderRadius: 10, padding: "14px 16px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 700, color: TEXT, fontFamily: "monospace" }}>{s.symbol}</div>
                <div style={{ fontSize: 10, color: MUTE, marginTop: 2 }}>{s.cadence_label} · IA {s.cadence_ttl_s}s · prio {s.cadence_priority}</div>
              </div>
              <CadencePill tag={s.cadence_tag} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 10 }}>
              <MiniStat label="Trades" value={String(s.trades)} color={TEXT} />
              <MiniStat label="WR" value={fmt(s.win_rate_pct, 1, "%")} color={wrColor(s.win_rate_pct)} />
              <MiniStat label="PnL" value={fmt(s.pnl, 2)} color={pnlColor(s.pnl)} />
              <MiniStat label="PF" value={fmt(s.profit_factor, 2)} color={(s.profit_factor ?? 0) >= 1.5 ? G : (s.profit_factor ?? 0) >= 1 ? Y : R} />
            </div>
            {/* Per-scenario mini-row */}
            <div style={{ display: "flex", gap: 6, fontSize: 10 }}>
              {["A", "B", "C"].map((sc) => {
                const cell = s.by_scenario[sc];
                if (!cell || cell.trades === 0) {
                  return (
                    <div key={sc} style={{
                      flex: 1, padding: "6px 8px", borderRadius: 6,
                      background: "rgba(255,255,255,0.02)", border: `1px solid ${BORD}`,
                      color: MUTE, textAlign: "center",
                    }}>
                      {sc}<br /><span style={{ fontSize: 9 }}>—</span>
                    </div>
                  );
                }
                return (
                  <div key={sc} style={{
                    flex: 1, padding: "6px 8px", borderRadius: 6,
                    background: heatColor(cell.win_rate_pct), border: `1px solid ${BORD}`,
                    textAlign: "center", color: TEXT, fontFamily: "monospace",
                  }}>
                    <div style={{ fontSize: 10, opacity: 0.9 }}>{sc}</div>
                    <div style={{ fontWeight: 700 }}>{fmt(cell.win_rate_pct, 0, "%")}</div>
                    <div style={{ fontSize: 9, opacity: 0.75 }}>{cell.wins}/{cell.trades}</div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Scenario × Symbol heatmap matrix ──────────────────────────────
function ScenarioHeatmap({ matrix }) {
  if (!matrix || !matrix.matrix?.length) return null;
  return (
    <div style={{ ...sectionStyle, padding: "20px 24px" }}>
      <SectionTitle hint="celdas coloreadas por win-rate · número en monospace = WR%">
        Heatmap Escenario × Mercado
      </SectionTitle>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: 4, minWidth: 480 }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", padding: "8px 10px", fontSize: 10, color: MUTE, letterSpacing: "0.08em", textTransform: "uppercase" }}>Mercado</th>
              {matrix.scenarios.map((sc) => (
                <th key={sc} style={{ textAlign: "center", padding: "8px 10px", fontSize: 10, color: MUTE, letterSpacing: "0.08em", textTransform: "uppercase" }}>
                  Esc. {sc}
                </th>
              ))}
              <th style={{ textAlign: "center", padding: "8px 10px", fontSize: 10, color: MUTE, letterSpacing: "0.08em", textTransform: "uppercase" }}>Total</th>
            </tr>
          </thead>
          <tbody>
            {matrix.matrix.map((row) => {
              let totT = 0, totW = 0;
              for (const sc of matrix.scenarios) {
                if (row[sc]) { totT += row[sc].trades; totW += row[sc].wins; }
              }
              const totWR = totT > 0 ? (totW / totT) * 100 : null;
              return (
                <tr key={row.symbol}>
                  <td style={{
                    padding: "10px 12px", background: SURFACE, borderRadius: 8,
                    fontFamily: "monospace", fontSize: 12, color: TEXT, fontWeight: 600,
                    display: "flex", alignItems: "center", gap: 8,
                  }}>
                    <CadencePill tag={row.cadence_tag} />
                    {row.symbol}
                  </td>
                  {matrix.scenarios.map((sc) => {
                    const cell = row[sc];
                    if (!cell || cell.trades === 0) {
                      return (
                        <td key={sc} style={{
                          padding: "10px 12px", textAlign: "center",
                          background: "rgba(255,255,255,0.02)", borderRadius: 8,
                          color: MUTE, fontSize: 11,
                        }}>—</td>
                      );
                    }
                    return (
                      <td key={sc} style={{
                        padding: "10px 12px", textAlign: "center",
                        background: heatColor(cell.win_rate_pct), borderRadius: 8,
                      }}>
                        <div style={{ fontFamily: "monospace", fontSize: 13, fontWeight: 700, color: TEXT }}>
                          {fmt(cell.win_rate_pct, 0, "%")}
                        </div>
                        <div style={{ fontSize: 9, color: SUB, marginTop: 2 }}>
                          {cell.wins}/{cell.trades} · {fmt(cell.pnl, 2)}
                        </div>
                      </td>
                    );
                  })}
                  <td style={{
                    padding: "10px 12px", textAlign: "center",
                    background: heatColor(totWR), borderRadius: 8,
                  }}>
                    <div style={{ fontFamily: "monospace", fontSize: 13, fontWeight: 700, color: TEXT }}>
                      {totWR !== null ? fmt(totWR, 0, "%") : "—"}
                    </div>
                    <div style={{ fontSize: 9, color: SUB, marginTop: 2 }}>{totW}/{totT}</div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Cadence aggregate panel ───────────────────────────────────────
function CadencePanel({ byCadence }) {
  const entries = Object.entries(byCadence || {});
  if (!entries.length) return null;
  // Order: turbo, rapida, estandar, institucional
  const order = ["turbo", "rapida", "estandar", "institucional"];
  entries.sort((a, b) => (order.indexOf(a[0]) - order.indexOf(b[0])));
  return (
    <div style={{ ...sectionStyle, padding: "20px 24px" }}>
      <SectionTitle hint="agregado por tier de velocidad IA">Performance por Cadencia</SectionTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
        {entries.map(([tag, d]) => {
          const c = CADENCE_COLORS[tag] || CADENCE_COLORS.unknown;
          return (
            <div key={tag} style={{
              background: c.bg, border: `1px solid ${c.border}`, borderRadius: 10, padding: "14px 16px",
            }}>
              <div style={{ fontSize: 10, color: c.fg, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 6 }}>
                {tag}
              </div>
              <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "monospace", color: wrColor(d.win_rate_pct) }}>
                {fmt(d.win_rate_pct, 1, "%")}
              </div>
              <div style={{ fontSize: 10, color: SUB, marginTop: 4 }}>
                {d.wins}W / {d.losses}L · PnL {fmt(d.pnl, 2)}
              </div>
              <div style={{ fontSize: 9, color: MUTE, marginTop: 6, lineHeight: 1.4 }}>
                {d.symbols.join(" · ")}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Generic breakdown table ───────────────────────────────────────
function BreakdownTable({ data, title, hint }) {
  if (!data || !Object.keys(data).length) return null;
  const rows = Object.entries(data).sort((a, b) => b[1].trades - a[1].trades);
  return (
    <div style={{ marginBottom: 22 }}>
      <SectionTitle hint={hint}>{title}</SectionTitle>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${BORD}` }}>
              {["Key", "Trades", "Wins", "Win Rate", "Net PnL"].map((h) => (
                <th key={h} style={{
                  textAlign: h === "Key" ? "left" : "right",
                  padding: "8px 12px", fontSize: 10, color: MUTE, letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 700,
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, d]) => (
              <tr key={key} style={{ borderBottom: `1px solid ${BORD}` }}>
                <td style={{ padding: "10px 12px", fontFamily: "monospace", fontSize: 12, color: TEXT }}>{key}</td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontFamily: "monospace", color: SUB }}>{d.trades}</td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontFamily: "monospace", color: SUB }}>{d.wins}</td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontFamily: "monospace", color: wrColor(d.win_rate_pct) }}>
                  {fmt(d.win_rate_pct, 1, "%")}
                </td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontFamily: "monospace", color: pnlColor(d.pnl) }}>
                  {fmt(d.pnl, 2)} USDT
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Section style helper ──────────────────────────────────────────
const sectionStyle = {
  background: SURFACE2,
  border: `1px solid ${BORD}`,
  borderRadius: 14,
  padding: "20px 24px",
  marginBottom: 18,
};

// ── Main page component ──────────────────────────────────────────
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
      background: BG,
      color: TEXT,
      fontFamily: "'Inter', 'Segoe UI', system-ui, sans-serif",
      padding: "24px 20px",
      maxWidth: 1280,
      margin: "0 auto",
    },
  };

  if (loading) {
    return (
      <div style={{ ...styles.page, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ color: MUTE }}>Loading cohort data…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...styles.page, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <div style={{ color: R }}>Error: {error}</div>
      </div>
    );
  }

  const v3      = data?.v3 || {};
  const allTime = data?.all_time || {};
  const legacy  = data?.legacy || {};
  const summary = data?.summary || {};
  const target  = data?.target || {};
  const insights = data?.insights || [];

  return (
    <div style={styles.page}>
      {/* ── Header ── */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 22, flexWrap: "wrap", gap: 12 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
            <h1 style={{ fontSize: 22, fontWeight: 700, color: TEXT, margin: 0 }}>OptiFerre · Cohort Analytics</h1>
            <span style={{
              padding: "3px 9px", borderRadius: 99, fontSize: 9, fontWeight: 700,
              background: "rgba(167,139,250,0.15)", color: P, border: "1px solid rgba(167,139,250,0.35)",
              letterSpacing: "0.08em",
            }}>
              {data?.source === "db_cache" ? "DB-CACHE" : "JSON-LIVE"}
            </span>
          </div>
          <div style={{ fontSize: 12, color: MUTE }}>
            Refresca cada 30s · {data?.filter || "tag:ai_prompt_version=v3"}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Link href="/" style={{ padding: "6px 12px", borderRadius: 8, border: `1px solid ${BORD}`, fontSize: 11, color: SUB, textDecoration: "none" }}>
            ← Dashboard
          </Link>
          <Link href="/admin/dashboard" style={{ padding: "6px 12px", borderRadius: 8, border: `1px solid ${BORD}`, fontSize: 11, color: P, textDecoration: "none", background: "rgba(167,139,250,0.06)", fontWeight: 600 }}>
            Admin →
          </Link>
          {lastUpdated && (
            <div style={{ fontSize: 10, color: MUTE }}>
              Updated {lastUpdated.toLocaleTimeString()}
            </div>
          )}
        </div>
      </div>

      {/* ── Hero: V3 vs All-time ── */}
      <PerformanceHero summary={summary} target={target} v3={v3} allTime={allTime} />

      {/* ── Refinement Insights ── */}
      <InsightsPanel insights={insights} />

      {/* ── Per-market cards (V3 cohort) ── */}
      <PerMarketGrid bySymbol={data?.v3_by_symbol} title="Mercados (V3 Cohort)" />

      {/* ── Scenario × Symbol heatmap ── */}
      <ScenarioHeatmap matrix={data?.v3_scenario_matrix} />

      {/* ── Cadence aggregate ── */}
      <CadencePanel byCadence={data?.v3_by_cadence} />

      {/* ── Breakdown tables ── */}
      <div style={{ ...sectionStyle }}>
        <SectionTitle hint="distribución por dimensión">Breakdowns Adicionales</SectionTitle>
        <BreakdownTable data={v3.by_path} title="Entry Path (standard vs micro_gate)" />
        <BreakdownTable data={v3.by_regime} title="AI Regime detectado" />
        <BreakdownTable data={v3.by_scenario} title="Scenario (A / B / C)" />
        <BreakdownTable data={v3.by_exit_reason} title="Exit Reason" />
      </div>

      {/* ── Legacy comparison ── */}
      {legacy.total_trades > 0 && (
        <div style={{ ...sectionStyle, padding: "20px 24px" }}>
          <SectionTitle hint="pre-V3 prompt · referencia histórica">
            Cohorte Legacy ({legacy.total_trades} trades)
          </SectionTitle>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
            <KpiCard label="Win Rate" value={fmt(legacy.win_rate_pct, 1, "%")} color={wrColor(legacy.win_rate_pct)} />
            <KpiCard label="Profit Factor" value={fmt(legacy.profit_factor, 2)} color={(legacy.profit_factor ?? 0) >= 1.5 ? G : Y} />
            <KpiCard label="Net PnL" value={`${fmt(legacy.net_pnl_usdt, 2)} USDT`} color={pnlColor(legacy.net_pnl_usdt)} />
            <KpiCard label="EV / Trade" value={fmt(legacy.ev_per_trade_usdt, 4)} color={pnlColor(legacy.ev_per_trade_usdt)} />
          </div>
        </div>
      )}

      <div style={{ fontSize: 10, color: "#4a5568", textAlign: "center", paddingTop: 8 }}>
        Trades all-time: {summary?.total_all_time ?? "—"} · V3: {summary?.total_v3 ?? "—"} · Legacy: {summary?.total_legacy ?? "—"} · Source: {data?.source}
      </div>
    </div>
  );
}
