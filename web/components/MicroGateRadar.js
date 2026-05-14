"use client";

/**
 * MicroGateRadar
 * ==============
 * Shows "Trigger Proximity" for every V3 AI-prompt gate on a focused symbol.
 * Read-only — does NOT interact with main_loop.py.
 *
 * Gates reflected (from ai_client.py v3 prompt):
 *
 *  ALL REGIMES — Hard Vetoes (must pass):
 *    V3  · flow_score >= 0.40  OR  ob_imbalance >= 0.50
 *    V5  · volume_acceleration >= 0.60
 *
 *  NORMAL / HIGH_VOL — Conditional Vetoes (must pass):
 *    C1  · atr_pct >= 0.003
 *    C2  · bb_width_pct >= 0.005
 *
 *  LOW_VOL only — Micro-Gates (replaces C1+C2, BOTH must pass):
 *    GATE-A  · flow_score >= 0.62  AND  ob_imbalance >= 0.57
 *    GATE-B  · rsi_slope > 1.00   AND  candle_body_pct >= 0.45
 */

import { memo, useMemo } from "react";

// ── Palette (mirrors dashboard-client.js) ──────────────────────────────────
const G    = "#12d98b";
const R    = "#eb4b61";
const Y    = "#f4b942";
const OR   = "#f97316";
const BORD = "#1a2b3c";
const MUTE = "#6b8299";
const TEXT = "#dce7f5";
const CARD = "rgba(10,18,28,0.96)";

// ── Gate thresholds (single source of truth) ───────────────────────────────
const THRESHOLDS = {
  // Hard vetoes (all regimes)
  flow_v3:     { label: "Flow Score (V-Veto V3)",     key: "trade_flow_score",     required: 0.40, fmt: "pct" },
  ob_v3:       { label: "OB Imbalance (V-Veto V3)",   key: "orderbook_imbalance",  required: 0.50, fmt: "pct" },
  vol_accel:   { label: "Vol Acceleration (V-Veto V5)",key: "volume_acceleration", required: 0.60, fmt: "x"   },
  // Conditional vetoes — NORMAL / HIGH_VOL only
  atr_c1:      { label: "ATR % (C-Veto C1)",          key: "atr_pct",              required: 0.003, fmt: "pct4" },
  bb_c2:       { label: "BB Width (C-Veto C2)",        key: "bb_width_pct",         required: 0.005, fmt: "pct4" },
  // Micro-Gate A — LOW_VOL only
  flow_mga:    { label: "Flow Score (Micro-Gate A)",   key: "trade_flow_score",     required: 0.62, fmt: "pct" },
  ob_mga:      { label: "OB Imbalance (Micro-Gate A)", key: "orderbook_imbalance",  required: 0.57, fmt: "pct" },
  // Micro-Gate B — LOW_VOL only
  rsi_mgb:     { label: "RSI Slope (Micro-Gate B)",    key: "rsi_slope",            required: 1.00, fmt: "raw2" },
  body_mgb:    { label: "Candle Body (Micro-Gate B)",  key: "candle_body_pct",      required: 0.45, fmt: "pct" },
};

// ── Regime classifier (mirrors ai_client.py v3 thresholds) ─────────────────
function classifyRegime(scan) {
  const atr = Number(scan.atr_pct ?? 0);
  const bb  = Number(scan.bb_width_pct ?? 0.01);
  if (atr < 0.0025 && bb < 0.005) return "LOW_VOL";
  if (atr >= 0.005)                return "HIGH_VOL";
  return "NORMAL";
}

// ── Value formatters ───────────────────────────────────────────────────────
function fmtVal(v, fmt) {
  if (v === null || v === undefined) return "—";
  switch (fmt) {
    case "pct":   return `${Math.round(v * 100)}%`;
    case "pct4":  return `${(v * 100).toFixed(2)}%`;
    case "x":     return `${Number(v).toFixed(2)}×`;
    case "raw2":  return Number(v).toFixed(2);
    default:      return String(v);
  }
}

// ── Proximity helpers ──────────────────────────────────────────────────────
/**
 * Returns a 0–1 score representing how close `value` is to `required`.
 * rsi_slope can be negative so we handle that case specially.
 */
function proximity(value, required) {
  if (value === null || value === undefined) return 0;
  if (required <= 0) return value >= required ? 1 : Math.max(0, 1 + value / Math.abs(required));
  return Math.min(1, Math.max(0, value / required));
}

/**
 * Bar fill color based on proximity ratio:
 *  < 0.50  → faint red
 *  < 0.70  → orange
 *  < 0.88  → yellow
 *  >= 0.88 and not met → pulsing amber (CSS class added separately)
 *  met     → green
 */
function gateColor(ratio, met) {
  if (met)          return G;
  if (ratio >= 0.88) return Y;
  if (ratio >= 0.70) return OR;
  return R;
}

// ── CSS keyframes injected once ──────────────────────────────────────────
const PULSE_STYLE_ID = "mgr-pulse-style";
if (typeof document !== "undefined" && !document.getElementById(PULSE_STYLE_ID)) {
  const s = document.createElement("style");
  s.id = PULSE_STYLE_ID;
  s.textContent = `
    @keyframes mgr-pulse {
      0%, 100% { opacity: 1; }
      50%       { opacity: 0.45; }
    }
    .mgr-pulsing { animation: mgr-pulse 1s ease-in-out infinite; }
  `;
  document.head.appendChild(s);
}

// ── Sub-components ─────────────────────────────────────────────────────────

function GateRow({ spec, value, groupMet }) {
  const v    = value === null || value === undefined ? null : Number(value);
  const met  = v !== null && v >= spec.required;
  const ratio = v !== null ? proximity(v, spec.required) : 0;
  const barColor = gateColor(ratio, met);
  const pulsing  = !met && ratio >= 0.88;
  const delta    = v !== null ? v - spec.required : null;

  return (
    <div style={{ marginBottom: 10 }}>
      {/* Label row */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 3 }}>
        <span style={{ fontSize: 10, color: met ? barColor : MUTE, letterSpacing: "0.03em" }}>
          {spec.label}
        </span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {/* Current value */}
          <span
            className={pulsing ? "mgr-pulsing" : undefined}
            style={{ fontSize: 11, fontFamily: "monospace", fontWeight: 700, color: barColor }}
          >
            {v !== null ? fmtVal(v, spec.fmt) : "—"}
          </span>
          {/* Required threshold */}
          <span style={{ fontSize: 10, color: MUTE }}>
            / {fmtVal(spec.required, spec.fmt)}
          </span>
          {/* Delta badge */}
          {delta !== null && (
            <span style={{
              fontSize: 10, fontFamily: "monospace",
              color: met ? G : R,
              minWidth: 38, textAlign: "right",
            }}>
              {met ? "✓ MET" : `${fmtVal(delta, spec.fmt)}`}
            </span>
          )}
        </div>
      </div>

      {/* Progress bar */}
      <div style={{ position: "relative", background: "rgba(255,255,255,0.06)", borderRadius: 4, height: 5, overflow: "hidden" }}>
        <div
          className={pulsing ? "mgr-pulsing" : undefined}
          style={{
            position: "absolute", left: 0, top: 0, bottom: 0,
            width: `${Math.round(Math.min(1, ratio) * 100)}%`,
            background: barColor,
            borderRadius: 4,
            transition: "width 0.5s ease, background 0.3s ease",
          }}
        />
        {/* 100% = threshold marker */}
        {!met && (
          <div style={{
            position: "absolute", top: 0, bottom: 0,
            left: "100%",   // always at the far edge since bar is capped at 100%
            width: 1,
            background: "rgba(255,255,255,0.18)",
          }} />
        )}
      </div>
    </div>
  );
}

function GateGroup({ title, gates, scan, allMet, dimmed }) {
  return (
    <div style={{
      marginBottom: 14,
      opacity: dimmed ? 0.40 : 1,
      transition: "opacity 0.3s ease",
    }}>
      {/* Group header */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        borderBottom: `1px solid ${BORD}`, paddingBottom: 6, marginBottom: 8,
      }}>
        <span style={{ fontSize: 10, color: MUTE, textTransform: "uppercase", letterSpacing: "0.07em" }}>
          {title}
        </span>
        <span style={{
          fontSize: 10, fontWeight: 700,
          color: allMet ? G : R,
          background: allMet ? "rgba(18,217,139,0.12)" : "rgba(235,75,97,0.10)",
          padding: "1px 7px", borderRadius: 99,
        }}>
          {allMet ? "ALL CLEAR" : "BLOCKED"}
        </span>
      </div>

      {gates.map((gk) => (
        <GateRow
          key={gk}
          spec={THRESHOLDS[gk]}
          value={scan[THRESHOLDS[gk].key]}
          groupMet={allMet}
        />
      ))}
    </div>
  );
}

// ── Proximity score chip (0–100%) ──────────────────────────────────────────
function ProximityChip({ score }) {
  const color = score >= 90 ? G : score >= 70 ? Y : score >= 50 ? OR : R;
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      background: `${color}12`, border: `1px solid ${color}30`,
      borderRadius: 8, padding: "6px 14px", minWidth: 72,
    }}>
      <span style={{ fontSize: 18, fontWeight: 800, fontFamily: "monospace", color }}>{score}%</span>
      <span style={{ fontSize: 9, color: MUTE, textTransform: "uppercase", letterSpacing: "0.06em" }}>proximity</span>
    </div>
  );
}

// ── Regime badge ───────────────────────────────────────────────────────────
const REGIME_META = {
  LOW_VOL:  { label: "LOW VOL",  color: "#a78bfa", bg: "rgba(167,139,250,0.12)", detail: "Micro-Gates A+B activos" },
  NORMAL:   { label: "NORMAL",   color: "#57c1ff", bg: "rgba(87,193,255,0.10)",  detail: "Vetoes C1+C2 activos" },
  HIGH_VOL: { label: "HIGH VOL", color: "#f4b942", bg: "rgba(244,185,66,0.12)",  detail: "Volatilidad elevada" },
};

function RegimeBadge({ regime }) {
  const meta = REGIME_META[regime] || REGIME_META.NORMAL;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{
        fontSize: 10, fontWeight: 700, color: meta.color,
        background: meta.bg, border: `1px solid ${meta.color}44`,
        padding: "2px 9px", borderRadius: 99, letterSpacing: "0.07em",
      }}>
        {meta.label}
      </span>
      <span style={{ fontSize: 10, color: MUTE }}>{meta.detail}</span>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────
const MicroGateRadar = memo(function MicroGateRadar({ scan }) {
  const s = scan || {};

  const regime = useMemo(() => classifyRegime(s), [s.atr_pct, s.bb_width_pct]);
  const isLowVol = regime === "LOW_VOL";

  // ── Evaluate each gate group ─────────────────────────────────────────
  const groups = useMemo(() => {
    // Hard vetoes — all regimes
    // V3: flow OR ob must pass (union condition)
    const flowV3 = Number(s.trade_flow_score ?? 0);
    const obV3   = Number(s.orderbook_imbalance ?? 0);
    const hardMet = (flowV3 >= 0.40 || obV3 >= 0.50) && Number(s.volume_acceleration ?? 0) >= 0.60;

    // Conditional vetoes — NORMAL/HIGH_VOL
    const condMet = Number(s.atr_pct ?? 0) >= 0.003 && Number(s.bb_width_pct ?? 0) >= 0.005;

    // Micro-Gate A — LOW_VOL
    const mgaMet = Number(s.trade_flow_score ?? 0) >= 0.62 && Number(s.orderbook_imbalance ?? 0) >= 0.57;

    // Micro-Gate B — LOW_VOL
    const mgbMet = Number(s.rsi_slope ?? -99) > 1.00 && Number(s.candle_body_pct ?? 0) >= 0.45;

    return { hardMet, condMet, mgaMet, mgbMet };
  }, [
    s.trade_flow_score, s.orderbook_imbalance, s.volume_acceleration,
    s.atr_pct, s.bb_width_pct, s.rsi_slope, s.candle_body_pct,
  ]);

  // ── Overall proximity score (avg of relevant gates) ──────────────────
  const proximityScore = useMemo(() => {
    const keys = isLowVol
      ? ["flow_mga", "ob_mga", "rsi_mgb", "body_mgb", "flow_v3", "ob_v3", "vol_accel"]
      : ["atr_c1", "bb_c2", "flow_v3", "ob_v3", "vol_accel"];

    const ratios = keys.map((k) => {
      const spec = THRESHOLDS[k];
      const v = s[spec.key];
      return v !== null && v !== undefined ? proximity(Number(v), spec.required) : 0;
    });
    const avg = ratios.reduce((a, b) => a + b, 0) / ratios.length;
    return Math.round(avg * 100);
  }, [isLowVol, s.trade_flow_score, s.orderbook_imbalance, s.volume_acceleration,
      s.atr_pct, s.bb_width_pct, s.rsi_slope, s.candle_body_pct]);

  const symbol = s.symbol || "–";
  const hasData = s.atr_pct !== undefined;

  return (
    <div style={{
      background: CARD,
      border: `1px solid ${BORD}`,
      borderRadius: 12,
      padding: "16px 18px",
    }}>
      {/* ── Header ── */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, color: TEXT, marginBottom: 4 }}>
            Micro-Gate Radar
            <span style={{ marginLeft: 8, fontSize: 11, color: MUTE, fontWeight: 400 }}>
              {symbol}
            </span>
          </div>
          <RegimeBadge regime={regime} />
        </div>
        <ProximityChip score={proximityScore} />
      </div>

      {!hasData ? (
        <div style={{ fontSize: 11, color: MUTE, textAlign: "center", padding: "16px 0" }}>
          Sin datos de microestructura — esperando scan…
        </div>
      ) : (
        <>
          {/* ── Hard Vetoes (all regimes) ── */}
          <GateGroup
            title="Vetos Duros — todos los regímenes"
            gates={["flow_v3", "ob_v3", "vol_accel"]}
            scan={s}
            allMet={groups.hardMet}
            dimmed={false}
          />

          {/* ── Regime-conditional block ── */}
          {isLowVol ? (
            <>
              {/* LOW_VOL: Micro-Gate A */}
              <GateGroup
                title="Micro-Gate A (LOW_VOL · reemplaza C1)"
                gates={["flow_mga", "ob_mga"]}
                scan={s}
                allMet={groups.mgaMet}
                dimmed={false}
              />
              {/* LOW_VOL: Micro-Gate B */}
              <GateGroup
                title="Micro-Gate B (LOW_VOL · reemplaza C2)"
                gates={["rsi_mgb", "body_mgb"]}
                scan={s}
                allMet={groups.mgbMet}
                dimmed={false}
              />
            </>
          ) : (
            /* NORMAL / HIGH_VOL: conditional vetoes C1+C2 */
            <GateGroup
              title="Vetos Condicionales C1+C2 (NORMAL / HIGH_VOL)"
              gates={["atr_c1", "bb_c2"]}
              scan={s}
              allMet={groups.condMet}
              dimmed={false}
            />
          )}

          {/* ── Final verdict ── */}
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            borderTop: `1px solid ${BORD}`, paddingTop: 10, marginTop: 4,
          }}>
            <div style={{
              flex: 1, fontSize: 10, fontWeight: 700,
              color: (groups.hardMet && (isLowVol ? groups.mgaMet && groups.mgbMet : groups.condMet)) ? G : R,
            }}>
              {groups.hardMet && (isLowVol ? groups.mgaMet && groups.mgbMet : groups.condMet)
                ? "✓ Todos los gates abiertos — AI puede aprobar"
                : "✗ Gates bloqueados — AI rechazará sin importar confidence"}
            </div>
            {isLowVol && (
              <span style={{ fontSize: 9, color: MUTE, fontStyle: "italic" }}>
                B8 boost activo si A+B met
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
});

export default MicroGateRadar;

// ── Usage example (mock data) ─────────────────────────────────────────────
//
// import MicroGateRadar from "../components/MicroGateRadar";
//
// // LOW_VOL scenario — near MICRO-GATE-A trigger:
// const mockScanLowVol = {
//   symbol:              "SOLUSDT",
//   atr_pct:             0.0018,   // < 0.0025 → LOW_VOL
//   bb_width_pct:        0.0038,   // < 0.005  → confirms LOW_VOL
//   trade_flow_score:    0.59,     // needs 0.62 (gate A) → 95% proximity → pulsing yellow
//   orderbook_imbalance: 0.53,     // needs 0.57 (gate A) → 93% proximity → pulsing yellow
//   rsi_slope:           0.70,     // needs > 1.00 (gate B) → 70%
//   candle_body_pct:     0.48,     // needs 0.45 (gate B) → MET ✓
//   volume_acceleration: 0.72,     // needs 0.60 (veto V5) → MET ✓
// };
//
// // NORMAL scenario — C1/C2 active:
// const mockScanNormal = {
//   symbol:              "BTCUSDT",
//   atr_pct:             0.0035,   // >= 0.0025, < 0.005 → NORMAL
//   bb_width_pct:        0.0055,   // > 0.005 → C2 MET
//   trade_flow_score:    0.44,     // >= 0.40 → V3 MET
//   orderbook_imbalance: 0.51,     // >= 0.50 → V3 MET
//   rsi_slope:           1.20,
//   candle_body_pct:     0.38,
//   volume_acceleration: 0.58,     // < 0.60 → V5 BLOCKED
// };
//
// <MicroGateRadar scan={mockScanLowVol} />
// <MicroGateRadar scan={mockScanNormal} />
