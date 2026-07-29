"use client";
import React, { useState, useEffect, useRef, useCallback } from "react";

/* ════════════════════════════════════════════════════════════════════════
   CONSOLA DE OPERACIÓN MANUAL — Deriv CRASH
   Todo lo que el bot calcula en una pantalla para operar a mano:
   confirmaciones de posibles spikes (hora · score · por qué), tiempo y
   ticks desde el último spike, cadencia, semáforo "loaded gun" y la tabla
   de spikes enriquecida (ratio · atr · nº en la hora · gap · ticks).
   ════════════════════════════════════════════════════════════════════════ */

const T = {
  bg: "#06080d", bg2: "#0a0d14", panel: "#0c1018", panel2: "#10151f",
  border: "rgba(255,255,255,0.06)", borderH: "rgba(255,255,255,0.14)",
  text: "#e6ebf2", textD: "#aab1bd", mute: "#5b6473",
  green: "#22d3a3", red: "#ff5d6c", cyan: "#62d4ff", violet: "#a78bfa",
  amber: "#f5c43c", blue: "#5b8cff", orange: "#ff9f43",
};
const FONT_MONO = "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace";

const READY_COLOR = {
  FRESCO: T.mute, CARGANDO: T.cyan, LISTO: T.amber,
  VENCIDO: T.orange, SECO: T.violet, SIN_DATOS: T.mute,
};
const READY_LABEL = {
  FRESCO: "Recién disparó", CARGANDO: "Cargando",
  LISTO: "LISTO — vigilar", VENCIDO: "VENCIDO — alta prob.",
  SECO: "Seco / lento", SIN_DATOS: "Sin datos",
};

// Confirmation kind → color/label.
const KIND = {
  CONFIRMADO: { color: T.green, label: "CONFIRMADO" },
  SPIKE: { color: T.red, label: "SPIKE" },
  SCORE: { color: T.amber, label: "SCORE" },
  CARGANDO: { color: T.cyan, label: "CARGANDO" },
  SECO: { color: "#f59e0b", label: "SECO" },
  TREND_GATE: { color: T.mute, label: "TREND sin FVG" },
  AI_VETO: { color: "#a78bfa", label: "IA VETÓ" },
  BLOQUEADO: { color: T.mute, label: "BLOQUEADO" },
  INFO: { color: T.mute, label: "INFO" },
};

/* ── helpers ─────────────────────────────────────────────── */
const num = (v, d = 2) => (v == null || !Number.isFinite(Number(v)) ? "–" : Number(v).toFixed(d));
const intC = (v) => (v == null || !Number.isFinite(Number(v)) ? "–" : Number(v).toLocaleString());
const fmtClock = (ts) => {
  if (!ts) return "–";
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return isNaN(d) ? "–" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
};
const ago = (s) => {
  if (s == null || !Number.isFinite(Number(s))) return "–";
  s = Math.max(0, Number(s));
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};
const dur = (s) => {
  if (s == null || !Number.isFinite(Number(s))) return "–";
  s = Number(s);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
};
const sideLabel = (side) =>
  side === "MULTDOWN" ? "CRASH↓" : side === "MULTUP" ? "BOOM↑" : side || "–";
const sideColor = (side) => (side === "MULTDOWN" ? T.red : side === "MULTUP" ? T.green : T.textD);
const pnlColor = (v) => (Number(v) > 0 ? T.green : Number(v) < 0 ? T.red : T.textD);
const ord = (n) => (n == null ? "–" : `#${n}`);

/* ── primitives ──────────────────────────────────────────── */
function Panel({ title, right, children, accent }) {
  return (
    <div style={{
      background: T.panel, border: `1px solid ${accent ? accent + "55" : T.border}`,
      borderRadius: 10, display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {(title || right) && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "9px 12px", borderBottom: `1px solid ${T.border}`, background: T.panel2,
        }}>
          <span style={{
            fontFamily: FONT_MONO, fontSize: 10.5, fontWeight: 800, letterSpacing: "0.12em",
            textTransform: "uppercase", color: accent || T.textD,
          }}>{title}</span>
          {right}
        </div>
      )}
      <div style={{ padding: 12 }}>{children}</div>
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ fontFamily: FONT_MONO, fontSize: 9, letterSpacing: "0.08em", textTransform: "uppercase", color: T.mute }}>{label}</span>
      <span style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 800, color: color || T.text }}>{value}</span>
    </div>
  );
}

/* ── D.6.5 Post-Racha Cooldown Bar ──────────────────────────── */
function CooldownBar({ cooldown }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (!cooldown?.active) return null;

  const nowSec    = now / 1000;
  const remaining = Math.max(0, cooldown.until_ts - nowSec);
  const total     = cooldown.until_ts - cooldown.started_ts;
  const elapsed   = Math.max(0, total - remaining);
  const pct       = total > 0 ? Math.min(100, Math.max(0, (remaining / total) * 100)) : 0;
  const spikes    = cooldown.spike_count ?? 0;

  const label = spikes >= 5 ? "5+ spikes" : `${spikes} spikes`;
  const color = "#f97316"; // naranja — distinto del amber del ghost

  return (
    <div style={{
      marginTop: 8, padding: "6px 10px", borderRadius: 6,
      background: "rgba(249,115,22,0.08)", border: "1px solid rgba(249,115,22,0.35)",
      fontFamily: "'SF Mono','Fira Mono',monospace", fontSize: 11,
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontWeight: 700, color }}>
          🧊 POST-RACHA COOLDOWN
        </span>
        <span style={{ color, opacity: 0.9 }}>
          {label} · {Math.ceil(remaining)}s restantes
        </span>
      </div>
      <div style={{
        width: "100%", height: 4, background: "rgba(249,115,22,0.15)",
        borderRadius: 2, overflow: "hidden",
      }}>
        <div style={{
          width: `${pct}%`, height: "100%",
          background: color,
          transition: "width 1s linear",
        }} />
      </div>
      <div style={{ marginTop: 3, opacity: 0.6, fontSize: 10 }}>
        Ghost bloqueado · bot esperando ciclo nuevo
      </div>
    </div>
  );
}

/* ── D.10.2 Slope Gate Section — 4 símbolos, grace countdown, triple lógica ── */
/* ── Displacement Gate (6H price displacement) ───────────────── */
const DISP_SYMBOLS = new Set(["BOOM500", "CRASH500"]);

/* ── ABRE / NO ABRE — señal maestra para operación manual ────── */
function TradeSignalBadge({ symbol }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    if (!DISP_SYMBOLS.has(symbol)) return;
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch("/api/deriv/analytics/displacement-gate", { cache: "no-store" });
        if (!active) return;
        const json = res.ok ? await res.json() : null;
        if (json?.[symbol]) setData({ ...json[symbol], threshold: json.threshold ?? 1.0 });
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 5000);
    return () => { active = false; clearInterval(id); };
  }, [symbol]);

  if (!DISP_SYMBOLS.has(symbol) || !data || data.error) return null;
  const { trade_signal, signal_reason, spikes_1h } = data;
  if (!trade_signal) return null;

  const isAbre = trade_signal === "ABRE";
  const color  = isAbre ? T.green : T.red;

  return (
    <div style={{
      margin: "8px 10px 4px",
      padding: "10px 14px",
      borderRadius: 8,
      background: color + "15",
      border: `2px solid ${color}99`,
      display: "flex", alignItems: "center", justifyContent: "space-between",
      boxShadow: `0 0 18px ${color}20`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 11, height: 11, borderRadius: "50%",
          background: color, boxShadow: `0 0 8px ${color}`, flexShrink: 0,
          animation: isAbre ? "none" : "pulse 1.1s infinite",
        }} />
        <span style={{
          fontFamily: FONT_MONO, fontSize: 22, fontWeight: 900,
          color, letterSpacing: "0.07em",
        }}>
          {isAbre ? "ABRE" : "NO ABRE"}
        </span>
      </div>
      <div style={{ textAlign: "right", display: "flex", flexDirection: "column", gap: 2 }}>
        {spikes_1h != null && (
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: T.textD, fontWeight: 700 }}>
            {spikes_1h} spikes/h
          </span>
        )}
        {signal_reason && (
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>
            {signal_reason}
          </span>
        )}
      </div>
    </div>
  );
}

function DisplacementGateSection({ symbol }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    if (!DISP_SYMBOLS.has(symbol)) return;
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch("/api/deriv/analytics/displacement-gate", { cache: "no-store" });
        if (!active) return;
        const json = res.ok ? await res.json() : null;
        if (json?.[symbol]) setData({ ...json[symbol], threshold: json.threshold ?? 1.0, window_h: json.window_h ?? 6 });
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 5000);
    return () => { active = false; clearInterval(id); };
  }, [symbol]);

  if (!DISP_SYMBOLS.has(symbol) || !data || data.error) return null;

  const { d6h, gate_active, alert, trend, windows = {}, threshold = 1.0, window_h = 6 } = data;
  if (d6h == null) return null;

  const isBoom = symbol.startsWith("BOOM");

  // Color scale
  const gateColor = gate_active ? T.red : alert ? T.amber : T.green;

  // Bar: map d6h from [-2, +3] → 0..100%
  const BAR_MIN = -2, BAR_MAX = 3;
  const barPct   = Math.min(100, Math.max(0, (d6h - BAR_MIN) / (BAR_MAX - BAR_MIN) * 100));
  const threshPct = Math.min(100, Math.max(0, (threshold - BAR_MIN) / (BAR_MAX - BAR_MIN) * 100));

  // Trend arrow
  const trendSymbol = trend === "up" ? "↑" : trend === "down" ? "↓" : "→";
  const trendColor  = trend === "up" ? T.red : trend === "down" ? T.green : T.mute;

  // Danger label
  const label = gate_active
    ? `DISP GATE → $1`
    : alert
    ? `ALERTA ${d6h.toFixed(2)}% (umbral ${threshold}%)`
    : `OK ${d6h.toFixed(2)}%`;

  // Direction explanation
  const dirNote = isBoom
    ? "precio subió (spikes agotados)"
    : "precio bajó (spikes agotados)";

  return (
    <div style={{
      marginTop: 6, padding: "5px 10px", borderRadius: 6,
      fontFamily: FONT_MONO, fontSize: 10,
      border: `1px solid ${gateColor}55`,
      background: `${gateColor}0a`,
      color: gateColor,
    }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 3 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <span style={{ fontWeight: 700, fontSize: 9, letterSpacing: "0.06em" }}>
            {gate_active ? "✗" : "✓"} DISP {window_h}H
          </span>
          {gate_active && (
            <span style={{
              fontSize: 8, fontWeight: 700, padding: "1px 5px", borderRadius: 3,
              background: `${T.red}22`, color: T.red, letterSpacing: "0.05em",
            }}>$1 GATE ACTIVO</span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, fontWeight: 700 }}>
          <span style={{ color: trendColor, fontSize: 10 }}>{trendSymbol}</span>
          <span>{d6h >= 0 ? "+" : ""}{d6h.toFixed(2)}%</span>
        </div>
      </div>

      {/* Bar */}
      <div style={{ position: "relative", width: "100%", height: 5, background: "rgba(255,255,255,0.07)", borderRadius: 3, marginBottom: 4 }}>
        {/* Threshold marker */}
        <div style={{
          position: "absolute", left: `${threshPct}%`, top: -1, width: 2, height: 7,
          background: T.red, borderRadius: 1, opacity: 0.7, zIndex: 2,
        }} />
        {/* Fill */}
        <div style={{
          width: `${barPct}%`, height: "100%",
          background: gateColor, borderRadius: 3,
          transition: "width 0.4s ease",
        }} />
      </div>

      {/* Sub-row: windows */}
      <div style={{ display: "flex", gap: 8, fontSize: 9, color: T.textD, flexWrap: "wrap" }}>
        {[1, 3, 6, 12].map(h => {
          const v = windows[`h${h}`];
          if (v == null) return null;
          const c = v > threshold ? T.red : v > threshold * 0.5 ? T.amber : T.mute;
          return (
            <span key={h} style={{ color: c }}>
              {h}H:{v >= 0 ? "+" : ""}{v.toFixed(2)}%
            </span>
          );
        })}
        {gate_active && (
          <span style={{ color: T.mute, fontSize: 8, marginLeft: "auto" }}>{dirNote}</span>
        )}
      </div>
    </div>
  );
}

function SlopeGateSection({ symbol }) {
  const [data, setData] = useState(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch("/api/deriv/analytics/d10-slope-state", { cache: "no-store" });
        if (!active) return;
        const json = res.ok ? await res.json() : null;
        const sym = symbol.toUpperCase();
        if (json?.[sym]) setData(json[sym]);
      } catch { /* noop */ }
    };
    doFetch();
    const fetchId = setInterval(doFetch, 3000);
    const tickId  = setInterval(() => setNow(Date.now()), 1000);
    return () => { active = false; clearInterval(fetchId); clearInterval(tickId); };
  }, [symbol]);

  if (!data?.available) return null;

  const {
    slope_pct, cambio_pct, delta_valid, grace_countdown_sec,
    passing, active_camino, pending_sec,
    c1_ok, c2_ok, c3_ok,
    n_prices, age_s, spike_ts, spike_count,
    thresholds = {},
  } = data;

  const isBoom = symbol.toUpperCase().startsWith("BOOM");
  const is1000 = symbol.includes("1000");
  const inGrace = !delta_valid;
  const graceSec = typeof grace_countdown_sec === "number" ? grace_countdown_sec : 0;
  const gracePct = inGrace
    ? Math.max(0, Math.min(100, 100 - (graceSec / (is1000 ? 480 : 240)) * 100))
    : 100;

  const gateColor = inGrace ? "#f59e0b" : passing ? T.green : T.red;
  const fmtP   = (v) => v == null ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(5)}%`;
  const fmtThr = (v) => v == null ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(3)}%`;
  const mapToBar = (v) => Math.max(1, Math.min(99, ((v + 0.06) / 0.12) * 100));
  const slopeBarPct = slope_pct != null ? mapToBar(slope_pct) : null;

  const c1 = thresholds.c1 || {};
  const c2 = thresholds.c2 || {};
  const c3 = thresholds.c3 || {};
  const c1Thr = isBoom ? c1.boom_max : c1.crash_min;
  const thrBarPct = c1Thr != null ? mapToBar(c1Thr) : null;

  const CAMINO_LABELS = {
    "camino1_level":    "C1 NIVEL",
    "camino2_pn5":      "C2 PN.5",
    "camino3_breakout": "C3 BREAK",
  };
  const caminoLabel = active_camino ? (CAMINO_LABELS[active_camino] || active_camino) : null;

  return (
    <div style={{
      marginTop: 6, padding: "7px 10px", borderRadius: 6,
      background: inGrace
        ? "rgba(245,158,11,0.05)"
        : passing ? "rgba(34,211,163,0.07)" : "rgba(255,93,108,0.07)",
      border: `1px solid ${gateColor}44`,
      fontFamily: FONT_MONO, fontSize: 11,
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <span style={{ fontWeight: 800, color: gateColor, letterSpacing: "0.07em", fontSize: 10 }}>
            {inGrace ? "⏳" : passing ? "✓" : "✗"} D.10.2 SLOPE
          </span>
          {!inGrace && caminoLabel && (
            <span style={{
              fontSize: 8, fontWeight: 700, padding: "1px 4px", borderRadius: 3,
              background: `${gateColor}22`, color: gateColor, letterSpacing: "0.05em",
            }}>{caminoLabel}{pending_sec != null ? ` ${pending_sec}s` : ""}</span>
          )}
          {is1000 && (
            <span style={{ fontSize: 8, color: T.mute, opacity: 0.7 }}>1000</span>
          )}
        </div>
        <span style={{ fontFamily: FONT_MONO, fontSize: 12, fontWeight: 800, color: gateColor }}>
          {inGrace ? `${Math.ceil(graceSec)}s` : fmtP(slope_pct)}
        </span>
      </div>

      {/* Grace countdown bar */}
      {inGrace && (
        <div style={{ marginBottom: 5 }}>
          <div style={{ position: "relative", width: "100%", height: 5, background: "rgba(245,158,11,0.12)", borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              width: `${gracePct}%`, height: "100%",
              background: "#f59e0b", opacity: 0.7,
              transition: "width 1s linear", borderRadius: 3,
            }} />
          </div>
          <div style={{ fontSize: 8, color: "#f59e0b", marginTop: 2, opacity: 0.8 }}>
            grace post-spike · {Math.ceil(graceSec)}s restantes (acumulando datos limpios)
          </div>
        </div>
      )}

      {/* Slope bar — solo cuando fuera de grace */}
      {!inGrace && slopeBarPct != null && (
        <div style={{ position: "relative", width: "100%", height: 5, background: "rgba(255,255,255,0.06)", borderRadius: 3, marginBottom: 5 }}>
          {thrBarPct != null && (
            <div style={{
              position: "absolute", left: `${thrBarPct}%`,
              top: -1, width: 2, height: 7,
              background: T.textD, opacity: 0.5, borderRadius: 1,
            }} />
          )}
          <div style={{
            position: "absolute", left: `${slopeBarPct}%`,
            top: -1, transform: "translateX(-50%)",
            width: 4, height: 7, background: gateColor, borderRadius: 2,
            transition: "left 1s ease",
          }} />
          <div style={{
            position: "absolute", left: 0,
            width: `${slopeBarPct}%`, height: "100%",
            background: gateColor, opacity: 0.18, borderRadius: 3,
          }} />
        </div>
      )}

      {/* Caminos grid — 3 columnas */}
      {!inGrace && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 3, marginBottom: 4 }}>
          <div style={{
            padding: "3px 5px", borderRadius: 4, fontSize: 9,
            background: c1_ok ? "rgba(34,211,163,0.10)" : "rgba(255,93,108,0.06)",
            border: `1px solid ${c1_ok ? T.green : T.red}33`,
          }}>
            <div style={{ fontWeight: 700, color: c1_ok ? T.green : T.red, marginBottom: 1 }}>
              {c1_ok ? "✓" : "✗"} C1 NIVEL
            </div>
            <div style={{ color: T.mute }}>
              {isBoom ? "≤" : "≥"}{fmtThr(c1Thr)}
            </div>
            <div style={{ color: T.mute, opacity: 0.7 }}>{c1.pending ?? 120}s pend</div>
          </div>
          <div style={{
            padding: "3px 5px", borderRadius: 4, fontSize: 9,
            background: c2_ok ? "rgba(34,211,163,0.10)" : "rgba(255,255,255,0.03)",
            border: `1px solid ${c2_ok ? T.green + "55" : "rgba(255,255,255,0.08)"}`,
          }}>
            <div style={{ fontWeight: 700, color: c2_ok ? T.green : (c2.enabled === false ? T.mute : T.mute), marginBottom: 1 }}>
              {c2_ok ? "✓" : c2.enabled === false ? "–" : "✗"} C2 PN.5
            </div>
            <div style={{ color: T.mute, fontSize: 8 }}>
              {isBoom ? "≤" : "≥"}{fmtThr(isBoom ? c2.boom_max : c2.crash_min)}
            </div>
          </div>
          <div style={{
            padding: "3px 5px", borderRadius: 4, fontSize: 9,
            background: c3_ok ? "rgba(34,211,163,0.10)" : "rgba(255,255,255,0.03)",
            border: `1px solid ${c3_ok ? T.green + "55" : "rgba(255,255,255,0.08)"}`,
          }}>
            <div style={{ fontWeight: 700, color: c3_ok ? T.green : T.mute, marginBottom: 1 }}>
              {c3_ok ? "✓" : c3.enabled === false ? "–" : "✗"} C3 BREAK
            </div>
            <div style={{ color: T.mute, fontSize: 8 }}>Δ giro</div>
          </div>
        </div>
      )}

      {/* Pie: cambio + n_prices */}
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: T.mute }}>
        <span>
          {inGrace
            ? `spike #${spike_count ?? "?"} · acumulando`
            : cambio_pct != null
              ? `Δ ${fmtP(cambio_pct)}`
              : "sin Δ"
          }
        </span>
        <span style={{ opacity: 0.6 }}>
          n={n_prices}{age_s != null && age_s > 10 ? ` · ${age_s}s` : ""}
        </span>
      </div>
    </div>
  );
}

/* ── ENTRADA DIEGO — Segunda línea autónoma ──────────────────── */
const ED_SYMBOLS = new Set(["CRASH500", "BOOM500", "CRASH600", "BOOM600", "CRASH900", "BOOM900", "CRASH1000", "BOOM1000"]);
// 1000s ciclo simple (coincidir con entrada_diego.py)
const K1000_WAIT_S       = 900;   // 15min espera 1000s
const K1000_WAIT_900_S   = 720;   // 12min espera 900s
const K1000_CONTRACT_S   = 600;   // 10min duración contrato
const K1000_SPIKE_HOLD_S = 240;   // 4min espera post-spike
const K1000_STAKE_START  = 20;    // stake inicial $20
const K1000_STAKES_1000  = [20, 20, 40, 40, 80, 80]; // escalera $20×2→$40×2→$80×2
// POWER gate eliminado — 500s y 600s abren directo en spike

// 500s: S20 ventana fija (coincidir con entrada_diego.py)
const S20_WINDOW_S   = 690;   // 11.5 min ventana post-spike
const S20_POSITIVE_S = 150;   // 2.5 min profit+ sostenido → cierre anticipado
const S500_HORA_TARGET = 1.0; // $1/hora por símbolo

function EntradaDiegoSection({ symbol }) {
  const [edState, setEdState] = useState(null);
  const [now, setNow]         = useState(Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!ED_SYMBOLS.has(symbol)) return;
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch("/api/deriv/analytics/entrada-diego-state", { cache: "no-store" });
        if (!active) return;
        const data = res.ok ? await res.json() : null;
        if (!data) return;
        if (!data.enabled) { setEdState({ enabled: false }); return; }
        if (data[symbol]) setEdState({ ...data[symbol], enabled: true });
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 2000);
    return () => { active = false; clearInterval(id); };
  }, [symbol]);

  if (!ED_SYMBOLS.has(symbol)) return null;
  if (!edState || !edState.enabled) return null;

  const {
    contract_id, current_profit = 0,
    spikes_in_contract = 0,
    burst_phase = "IDLE",
    profit_first_positive_ts = 0,
    sym_pnl_since_reset = 0,
  } = edState;

  const nowSec     = now / 1000;
  const is1000     = symbol.includes("1000") || symbol.includes("900");
  const is900      = symbol.includes("900");
  const is600      = symbol.includes("600");
  const is500      = symbol === "BOOM500" || symbol === "CRASH500";
  const isCrash500 = symbol === "CRASH500";
  const isBoom500  = symbol === "BOOM500";
  const _fmtS = s => s >= 60 ? `${Math.floor(s/60)}m${String(Math.round(s%60)).padStart(2,'0')}s` : `${Math.ceil(s)}s`;

  // ── 1000s: ciclo simple WAIT(15m) → IN_CONTRACT(10m) → SPIKE_HOLD(4m) ────
  if (is1000) {
    const {
      k1000_phase          = "WAIT",
      k1000_phase_ts       = 0,
      k1000_stake_idx      = 0,
      k1000_cycle_stake    = K1000_STAKE_START,
      k1000_spike_hold_until = 0,
    } = edState;

    const phase1k   = ["WAIT","IN_CONTRACT","SPIKE_HOLD"].includes(k1000_phase)
      ? k1000_phase : "WAIT";
    const stakeIdx  = Math.max(0, Math.min(k1000_stake_idx, K1000_STAKES_1000.length - 1));
    const stake1k   = k1000_cycle_stake || K1000_STAKES_1000[stakeIdx];

    // Timer por fase (900s espera 12min, 1000s 15min)
    const waitS        = is900 ? K1000_WAIT_900_S : K1000_WAIT_S;
    const phaseElapsed = k1000_phase_ts > 0 ? Math.max(0, nowSec - k1000_phase_ts) : 0;
    const phaseTotal   = phase1k === "WAIT" ? waitS
      : phase1k === "IN_CONTRACT" ? K1000_CONTRACT_S : K1000_SPIKE_HOLD_S;
    const phaseRem   = phase1k === "SPIKE_HOLD" && k1000_spike_hold_until > 0
      ? Math.max(0, k1000_spike_hold_until - nowSec)
      : Math.max(0, phaseTotal - phaseElapsed);
    const phasePct   = Math.min(100, (phaseElapsed / phaseTotal) * 100);

    // Colores por fase
    const phaseColor = phase1k === "IN_CONTRACT" ? "#22d3a3"
      : phase1k === "SPIKE_HOLD"   ? "#f5c43c"
      : "#64748b";

    const pnl1kColor = current_profit > 0 ? "#22d3a3" : current_profit < 0 ? "#ff5d6c" : "#64748b";

    const base1k = {
      marginTop: 8, padding: "8px 10px", borderRadius: 6,
      fontFamily: "'SF Mono','Fira Mono',monospace", fontSize: 11,
      border: `1px solid ${phaseColor}55`, background: `${phaseColor}0d`,
    };

    // Texto de estado por fase
    const stateText = phase1k === "WAIT"
      ? `Esperando ${_fmtS(phaseRem)} · abrirá $${stake1k} (${is900 ? "12" : "15"}min)`
      : phase1k === "IN_CONTRACT"
        ? contract_id
          ? `$${stake1k} en mercado · cierra en ${_fmtS(phaseRem)}`
          : `IN_CONTRACT — abriendo contrato...`
        : `Spike! Cerrando en ${_fmtS(phaseRem)}`;

    return (
      <div style={base1k}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontWeight: 700, color: "#e2e8f0", fontSize: 10 }}>ENTRADA DIEGO</span>
          <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 5px", borderRadius: 3,
            background: `${phaseColor}25`, color: phaseColor, letterSpacing: "0.06em" }}>
            {phase1k}
          </span>
          {(phase1k === "IN_CONTRACT" || phase1k === "SPIKE_HOLD") && contract_id && (
            <span style={{ color: pnl1kColor, fontWeight: 700, marginLeft: "auto", fontSize: 12 }}>
              {current_profit >= 0 ? "+" : ""}{Number(current_profit).toFixed(2)}$
            </span>
          )}
        </div>

        {/* Timer bar */}
        <div style={{ width: "100%", height: 3, background: `${phaseColor}22`, borderRadius: 2, margin: "5px 0 3px", overflow: "hidden" }}>
          <div style={{ width: `${phasePct}%`, height: "100%", background: phaseColor, transition: "width 1s linear" }} />
        </div>

        {/* Stake pills $20×2 → $40×2 → $80×2 */}
        <div style={{ display: "flex", gap: 3, marginTop: 4 }}>
          {K1000_STAKES_1000.map((s, i) => {
            const active = i === stakeIdx;
            return (
              <span key={i} style={{
                fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
                background: active ? `${phaseColor}28` : "rgba(255,255,255,0.03)",
                border: `1px solid ${active ? phaseColor : "rgba(255,255,255,0.09)"}`,
                color: active ? phaseColor : "rgba(255,255,255,0.22)",
                transition: "all 0.3s",
              }}>${s}</span>
            );
          })}
          <span style={{ fontSize: 9, color: "#475569", marginLeft: 2 }}>
            {phase1k === "IN_CONTRACT" ? "→spike:SPIKE_HOLD" : phase1k === "SPIKE_HOLD" ? `→4m:reset·$${K1000_STAKES_1000[0]}` : `→${is900 ? "12" : "15"}m:abrir`}
          </span>
        </div>

        {/* Línea de estado */}
        <div style={{ fontSize: 10, color: "#94a3b8", marginTop: 3 }}>{stateText}</div>
      </div>
    );
  }

  // ── 500s/600s: LADDER — único tier $32, 7min, sin gate ──────────
  // 500s y 600s operan igual: spike → $32 directo, contrato 7min, REST 20min tras 1 win>$1
  const TIER_DEFS = [[420, 32]];  // único tier: spike → $32 / 7min
  const LADDER_CYCLE_S = 420;     // 7 min — ventana activa tras spike
  const LADDER_REST_S  = 1200;    // 20 min REST tras 1 win>$1
  const FLOOR_PCT      = 0.85;
  const { last_spike_ts_500 = 0, burst_phase_started_at = 0, peak_profit_500 = 0,
          ladder_rest_until_500 = 0, consec_wins_500 = 0,
          power_30min_jump = 0, n_spikes_30min = 0 } = edState;

  const nowSec2       = now / 1000;
  const isResting     = ladder_rest_until_500 > 0 && nowSec2 < ladder_rest_until_500;
  const restRemaining = isResting ? Math.max(0, ladder_rest_until_500 - nowSec2) : 0;
  const tSinSpike   = last_spike_ts_500 > 0 ? Math.max(0, nowSec - last_spike_ts_500) : 0;
  const tierIdx = last_spike_ts_500 > 0
    ? (() => { for (let i = 0; i < TIER_DEFS.length; i++) { if (tSinSpike < TIER_DEFS[i][0]) return i; } return -1; })()
    : -1;
  const currentTierStake  = tierIdx >= 0 ? TIER_DEFS[tierIdx][1] : 0;
  const currentTierIsPausa = currentTierStake === 0;
  const CONTRACT_S   = 420;   // 7 min — igual para 500s y 600s
  const powerBlocked = false; // gate eliminado
  const isStop      = burst_phase === "STOP";
  const cyclePct    = last_spike_ts_500 > 0 ? Math.min(100, (tSinSpike / LADDER_CYCLE_S) * 100) : 0;
  const contractAge = contract_id && burst_phase_started_at > 0 ? Math.max(0, nowSec - burst_phase_started_at) : 0;
  const contractPct = contract_id ? Math.min(100, (contractAge / CONTRACT_S) * 100) : 0;

  // Floor: qué % del pico estamos capturando actualmente
  const floorActive = peak_profit_500 >= 0.20 && contract_id;
  const profitVsPeak = floorActive && peak_profit_500 > 0 ? current_profit / peak_profit_500 : null;
  const nearFloor    = profitVsPeak !== null && profitVsPeak < (FLOOR_PCT + 0.05);

  const pnlColor500 = current_profit > 0 ? "#22d3a3" : current_profit < 0 ? "#ff5d6c" : "#64748b";
  const pnlAccColor = sym_pnl_since_reset > 0 ? "#22d3a3" : sym_pnl_since_reset < 0 ? "#ff5d6c" : "#64748b";

  const cycleBarColor = isResting ? "#a78bfa" : cyclePct >= 90 ? "#ff5d6c" : cyclePct >= 65 ? "#f5c43c" : "#62d4ff";
  const baseColor500  = isStop ? "#64748b" : isResting ? "#a78bfa" : burst_phase === "LADDER" ? "#62d4ff" : "#94a3b8";

  const base500 = {
    marginTop: 8, padding: "8px 10px", borderRadius: 6,
    fontFamily: "'SF Mono','Fira Mono',monospace", fontSize: 11,
    border: `1px solid ${baseColor500}33`,
    background: `${baseColor500}08`,
  };

  return (
    <div style={base500}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 5 }}>
        <span style={{ fontWeight: 700, color: "#e2e8f0", fontSize: 10 }}>LADDER</span>
        {isStop ? (
          <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
            background: "#64748b22", color: "#94a3b8", letterSpacing: "0.06em" }}>STOP</span>
        ) : burst_phase === "HOUR_DONE" ? (
          <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
            background: "#22d3a322", color: "#22d3a3", letterSpacing: "0.06em" }}>HORA DONE ✓</span>
        ) : isResting ? (
          <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
            background: "#a78bfa22", color: "#a78bfa", letterSpacing: "0.06em" }}>
            REST {_fmtS(restRemaining)} · 1W&gt;$1
          </span>
        ) : (
          <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
            background: "#62d4ff18", color: "#62d4ff", letterSpacing: "0.06em" }}>
            {_fmtS(tSinSpike)} sin spike
          </span>
        )}
        {contract_id ? (
          <span style={{ color: nearFloor ? "#f5c43c" : pnlColor500, fontWeight: 700, marginLeft: "auto", fontSize: 13 }}>
            {current_profit >= 0 ? "+" : ""}{Number(current_profit).toFixed(2)}$
          </span>
        ) : (
          <span style={{ marginLeft: "auto", fontSize: 8, color: "#475569" }}>sin contrato</span>
        )}
      </div>

      {/* Tier pills */}
      <div style={{ display: "flex", gap: 3, marginBottom: 5 }}>
        {TIER_DEFS.map(([cutoff, stake], i) => {
          const isPausa = stake === 0;
          const active  = !isStop && i === tierIdx;
          const done    = !isStop && i < tierIdx;
          const activeBg = isPausa ? "#ff5d6c" : "#62d4ff";
          const bg      = active ? activeBg : done ? "#62d4ff22" : "rgba(255,255,255,0.06)";
          const col     = active ? "#0f172a" : done ? "#62d4ff88" : "#475569";
          const fw      = active ? 800 : 600;
          return (
            <span key={i} style={{
              flex: 1, textAlign: "center", fontSize: 9, fontWeight: fw,
              padding: "2px 0", borderRadius: 3, background: bg, color: col,
            }}>{isPausa ? "PAUSA" : `$${stake}`}</span>
          );
        })}
      </div>

      {/* Cycle progress bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
        <div style={{ flex: 1, height: 3, background: "rgba(255,255,255,0.08)", borderRadius: 2, overflow: "hidden" }}>
          {isResting ? (
            <div style={{ width: `${Math.min(100, (1 - restRemaining / LADDER_REST_S) * 100)}%`,
              height: "100%", background: "#a78bfa", transition: "width 1s linear" }} />
          ) : (
            <div style={{ width: `${cyclePct}%`, height: "100%", background: cycleBarColor, transition: "width 1s linear" }} />
          )}
        </div>
        <span style={{ fontSize: 8, color: cycleBarColor, fontWeight: 700, minWidth: 28, textAlign: "right" }}>
          {isResting ? `↺${_fmtS(restRemaining)}` : isStop ? "—" : `${Math.floor(Math.max(0, LADDER_CYCLE_S - tSinSpike) / 60)}m${Math.floor(Math.max(0, LADDER_CYCLE_S - tSinSpike) % 60)}s`}
        </span>
      </div>

      {/* Contract 7-min bar con floor indicator */}
      {contract_id && (
        <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
          <div style={{ flex: 1, height: 2, background: "rgba(255,255,255,0.06)", borderRadius: 2, overflow: "hidden", position: "relative" }}>
            <div style={{ width: `${contractPct}%`, height: "100%", background: "#a78bfa", transition: "width 1s linear" }} />
          </div>
          <span style={{ fontSize: 8, color: "#a78bfa", fontWeight: 700, minWidth: 28, textAlign: "right" }}>
            {_fmtS(Math.max(0, CONTRACT_S - contractAge))}
          </span>
        </div>
      )}

      {/* Floor row: solo cuando hay pico capturado */}
      {floorActive && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 8, marginBottom: 2 }}>
          <span style={{ color: nearFloor ? "#f5c43c" : "#64748b" }}>
            floor 85% · pico +{peak_profit_500.toFixed(2)}$
          </span>
          <span style={{ color: nearFloor ? "#f5c43c" : "#22d3a3", fontWeight: 700 }}>
            {profitVsPeak !== null ? `${Math.round(profitVsPeak * 100)}%` : "–"}
          </span>
        </div>
      )}

      {/* Acum */}
      <div style={{ borderTop: "1px solid rgba(255,255,255,0.07)", paddingTop: 3, marginTop: 2,
        display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 8, color: "#475569" }}>
          {isStop ? "esperando spike…" : contract_id ? "contrato 7m activo" : isResting ? "REST — esperando spike" : "esperando spike → $32"}
        </span>
        <span style={{ fontSize: 8, color: pnlAccColor, fontWeight: 700 }}>
          {sym_pnl_since_reset >= 0 ? "+" : ""}{sym_pnl_since_reset.toFixed(2)}$
        </span>
      </div>
    </div>
  );
}

/* ── 900s/1000s Scout Panel ──────────────────────────────────── */
function K1000Panel() {
  return (
    <div style={{
      background: T.panel, border: `1px solid ${T.border}`, borderRadius: 10,
      padding: "10px 12px", minHeight: 90,
    }}>
      <div style={{
        fontSize: 11, fontWeight: 800, color: T.cyan, letterSpacing: "0.07em", marginBottom: 2,
      }}>900s · 1000s — $20→$40→$80</div>
      <EntradaDiegoSection symbol="BOOM900" />
      <EntradaDiegoSection symbol="CRASH900" />
      <EntradaDiegoSection symbol="BOOM1000" />
      <EntradaDiegoSection symbol="CRASH1000" />
    </div>
  );
}

/* ── R_75 Volatility Card ────────────────────────────────────── */
function R75Card() {
  const [edState, setEdState] = useState(null);
  const [now, setNow]         = useState(Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch("/api/deriv/analytics/entrada-diego-state", { cache: "no-store" });
        if (!active) return;
        const data = res.ok ? await res.json() : null;
        if (!data) return;
        if (!data.enabled) { setEdState({ enabled: false }); return; }
        if (data["R_75"]) setEdState({ ...data["R_75"], enabled: true });
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 2000);
    return () => { active = false; clearInterval(id); };
  }, []);

  const R75_MAX_HOLD_S = 300;
  const R75_COOLDOWN_S = 30;
  const R75_STAKE      = 20;
  const R75_TP         = 1.50;
  const R75_SL         = 2.00;

  const PHASE_COLOR = {
    IDLE:     "rgba(90,100,115,0.7)",
    OPEN:     "#62d4ff",
    COOLDOWN: "#a78bfa",
  };

  const outer = {
    background: T.panel, border: `1px solid ${T.border}`, borderRadius: 10,
    padding: "14px 16px", fontFamily: FONT_MONO, fontSize: 11,
  };

  if (!edState || !edState.enabled) {
    return (
      <div style={outer}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 800, color: T.cyan }}>R_75</span>
          <span style={{ fontSize: 9, color: T.mute, padding: "1px 5px", borderRadius: 3, background: T.bg2 }}>VOLATILITY 75</span>
        </div>
        <div style={{ color: T.mute, fontSize: 10 }}>Esperando estado…</div>
      </div>
    );
  }

  const {
    phase, contract_id, current_profit = 0,
    open_ts = 0, cooldown_until = 0, last_close_profit = 0,
  } = edState;

  const nowSec = now / 1000;
  const color  = PHASE_COLOR[phase] || T.mute;

  const remaining = Math.max(0, (() => {
    if (phase === "OPEN")     return (open_ts + R75_MAX_HOLD_S) - nowSec;
    if (phase === "COOLDOWN") return cooldown_until - nowSec;
    return 0;
  })());

  const maxS = phase === "OPEN" ? R75_MAX_HOLD_S : phase === "COOLDOWN" ? R75_COOLDOWN_S : 1;
  const progress = Math.min(100, Math.max(0, ((maxS - remaining) / maxS) * 100));

  const secs    = Math.round(remaining);
  const timeStr = secs >= 60 ? `${Math.floor(secs/60)}m ${secs%60}s` : `${secs}s`;
  const pnlColor = current_profit > 0 ? T.green : current_profit < 0 ? T.red : T.textD;
  const lastColor = last_close_profit > 0 ? T.green : last_close_profit < 0 ? T.red : T.mute;

  return (
    <div style={outer}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 800, color: T.cyan }}>R_75</span>
        <span style={{ fontSize: 9, color: T.mute, padding: "1px 5px", borderRadius: 3, background: T.bg2 }}>VOLATILITY 75</span>
        <span style={{
          marginLeft: "auto", fontSize: 9, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
          background: `${color}22`, color, letterSpacing: "0.05em",
        }}>{phase}</span>
      </div>

      {/* Params */}
      <div style={{ display: "flex", gap: 12, marginBottom: 8, color: T.textD, fontSize: 10 }}>
        <span>Stake <b style={{ color: T.text }}>${R75_STAKE}</b></span>
        <span>TP <b style={{ color: T.green }}>${R75_TP.toFixed(2)}</b></span>
        <span>SL <b style={{ color: T.red }}>${R75_SL.toFixed(2)}</b></span>
        {contract_id && phase === "OPEN" && (
          <span style={{ marginLeft: "auto", color: T.mute }}>#{contract_id}</span>
        )}
      </div>

      {/* Progress bar */}
      {phase !== "IDLE" && (
        <div style={{ width: "100%", height: 3, background: `${color}33`, borderRadius: 2, marginBottom: 6, overflow: "hidden" }}>
          <div style={{ width: `${progress}%`, height: "100%", background: color, transition: "width 0.5s linear" }} />
        </div>
      )}

      {/* Status line */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {phase === "IDLE" && (
          <span style={{ color: T.mute }}>abriendo…</span>
        )}
        {phase === "OPEN" && (
          <>
            <span style={{ color: T.textD }}>cierra en <b style={{ color }}>{timeStr}</b></span>
            <span style={{ marginLeft: "auto", fontWeight: 700, fontSize: 12, color: pnlColor }}>
              {current_profit >= 0 ? "+" : ""}{Number(current_profit).toFixed(2)}$
            </span>
          </>
        )}
        {phase === "COOLDOWN" && (
          <>
            <span style={{ color: T.textD }}>próxima en <b style={{ color }}>{timeStr}</b></span>
            <span style={{ marginLeft: "auto", fontSize: 10, color: lastColor }}>
              último: {last_close_profit >= 0 ? "+" : ""}{Number(last_close_profit).toFixed(2)}$
            </span>
          </>
        )}
      </div>
    </div>
  );
}

/* ── D.6 Ghost Live Section ──────────────────────────────────── */
function GhostLiveSection({ symbol }) {
  const [ghostState, setGhostState] = useState(null);

  useEffect(() => {
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch("/api/deriv/analytics/d6-ghost-state", { cache: "no-store" });
        if (!active) return;
        const data = res.ok ? await res.json() : null;
        if (data?.states?.[symbol]) setGhostState(data.states[symbol]);
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 1000);
    return () => { active = false; clearInterval(id); };
  }, [symbol]);

  if (!ghostState) return null;
  const { state, ghost_data: gd = {}, reason } = ghostState;

  const base = {
    marginTop: 8, padding: "6px 10px", borderRadius: 6,
    fontFamily: "'SF Mono','Fira Mono',monospace", fontSize: 11,
    border: "1px solid currentColor",
  };

  if (state === "WAITING" || !state) return (
    <div style={{ ...base, color: "rgba(90,100,115,0.7)", background: "rgba(90,100,115,0.05)" }}>
      <span style={{ fontWeight: 700 }}>GHOST</span>
      <span style={{ marginLeft: 8, opacity: 0.6 }}>esperando señal…</span>
    </div>
  );

  if (state === "PENDING") {
    const remaining = gd.remaining_seconds ?? 60;
    const totalWait = gd.wait_s ?? 60;  // D.6.3: 60/120/150/200 según calidad+símbolo
    const elapsed = totalWait - remaining;
    const pct = Math.min(100, Math.max(0, (elapsed / totalWait) * 100));
    const isD10 = String(gd.quality_tier || "").startsWith("d10_");
    const isFortisima = gd.quality_tier === "fortisima";
    const pendingColor = isD10 ? T.cyan : isFortisima ? "#f59e0b" : "#fbbf24";
    const pendingBg   = isD10 ? "rgba(98,212,255,0.10)" : isFortisima ? "rgba(245,158,11,0.12)" : "rgba(251,191,36,0.08)";
    const d10CaminoMap = { camino1_level: "C1", camino2_pn5: "C2 PN.5", camino3_breakout: "C3 BREAK" };
    const d10Tag = isD10 ? String(gd.quality_tier).replace("d10_", "") : "";
    const qualityLabel = isD10
      ? `D10 ${d10CaminoMap[d10Tag] || d10Tag.toUpperCase()}`
      : isFortisima ? "⚡ FORTÍSIMA" : "NORMAL";
    const dirArrow = gd.side === "MULTUP" ? "▲" : gd.side === "MULTDOWN" ? "▼" : "?";
    return (
      <div style={{ ...base, color: pendingColor, background: pendingBg }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontWeight: 700 }}>⏳ GHOST PENDING — {remaining}s / {totalWait}s</span>
          <span style={{
            fontSize: 9, fontWeight: 700, padding: "1px 5px", borderRadius: 3,
            background: isD10 ? "rgba(98,212,255,0.22)" : isFortisima ? "rgba(245,158,11,0.25)" : "rgba(251,191,36,0.15)",
            color: pendingColor, letterSpacing: "0.05em",
          }}>{qualityLabel}</span>
        </div>
        <div style={{
          width: "100%", height: 3, background: `${pendingColor}33`,
          borderRadius: 2, margin: "4px 0", overflow: "hidden",
        }}>
          <div style={{ width: `${pct}%`, height: "100%", background: pendingColor, transition: "width 1s linear" }} />
        </div>
        <div style={{ opacity: 0.85 }}>
          {dirArrow}{" "}{gd.setup || "?"} · grade {gd.grade || "?"} · score {gd.score?.toFixed(2) || "?"}
          {gd.imm_state ? ` · imm:${gd.imm_state}` : ""}
          {gd.d80_mode ? ` · D80:${gd.d80_mode}${gd.d80_sum > 0 ? `(Σ${gd.d80_sum})` : ""}` : ""}
        </div>
      </div>
    );
  }

  if (state === "EXECUTED") return (
    <div style={{ ...base, color: "#22c55e", background: "rgba(34,197,94,0.12)" }}>
      <div style={{ fontWeight: 700 }}>✅ GHOST EJECUTADO</div>
      <div style={{ opacity: 0.85 }}>
        {gd.ghost_score_at_approval ? `score ${Number(gd.ghost_score_at_approval).toFixed(2)}` : gd.score ? `score ${gd.score.toFixed(2)}` : ""}
        {gd.quality_tier ? ` · ${gd.quality_tier === "fortisima" ? "⚡fortísima" : "normal"}` : ""}
        {gd.wait_seconds_used ? ` · wait ${gd.wait_seconds_used}s` : ""}
        {gd.executed_at ? ` · ${new Date(gd.executed_at * 1000).toLocaleTimeString()}` : ""}
      </div>
    </div>
  );

  if (state === "CANCELLED") return (
    <div style={{ ...base, color: "#f59e0b", background: "rgba(245,158,11,0.08)" }}>
      <div style={{ fontWeight: 700 }}>⚠️ GHOST CANCELADO</div>
      <div style={{ opacity: 0.75 }}>{reason || "spike"}</div>
    </div>
  );

  if (state === "FAILED") return (
    <div style={{ ...base, color: "#ef4444", background: "rgba(239,68,68,0.08)" }}>
      <div style={{ fontWeight: 700 }}>❌ GHOST FALLO API</div>
      <div style={{ opacity: 0.75 }}>{reason || ""}</div>
    </div>
  );

  if (state === "EXPIRED_GHOST") return (
    <div style={{ ...base, color: T.mute, background: "rgba(90,100,115,0.06)", border: `1px solid ${T.mute}44` }}>
      <div style={{ fontWeight: 700, opacity: 0.8 }}>◌ GHOST LIMPIANDO</div>
      <div style={{ opacity: 0.55, fontSize: 10 }}>{reason || "pending sin actualización"} · limpiando...</div>
    </div>
  );

  return null;
}

/* ── D.6.7 Regime Badge ─────────────────────────────────────────────────── */
function RegimeBadge({ regimeData }) {
  const REGIME_STYLES = {
    BUENO:    { bg: T.green  + "10", border: T.green  + "44", text: T.green,  dot: T.green,  label: "BUENO",    action: "opera todo"    },
    MEDIOCRE: { bg: T.amber  + "10", border: T.amber  + "44", text: T.amber,  dot: T.amber,  label: "MEDIOCRE", action: "opera 1 de 2"  },
    "DIFÍCIL":{ bg: "#ff9f4310",     border: "#ff9f4344",     text: T.orange, dot: T.orange, label: "DIFÍCIL",  action: "opera 1 de 3"  },
    CRÍTICO:  { bg: T.red    + "10", border: T.red    + "44", text: T.red,    dot: T.red,    label: "CRÍTICO",  action: "opera 1 de 4"  },
  };
  const regime = regimeData?.regime;
  if (!regime) {
    return (
      <div style={{
        display: "flex", alignItems: "center", gap: 7,
        padding: "5px 10px", margin: "0 12px 6px",
        borderRadius: 5, background: T.bg2, border: `1px solid ${T.border}`,
      }}>
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: T.mute, display: "inline-block" }} />
        <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>RÉGIMEN D.6.7 — sin datos (feature off o sin trades)</span>
      </div>
    );
  }
  const st = REGIME_STYLES[regime] || REGIME_STYLES["BUENO"];
  const skip          = regimeData?.skip ?? 0;
  const opp           = regimeData?.opportunity_count ?? 0;
  const silRatio      = regimeData?.silence_ratio ?? null;
  const silColor      = silRatio === null ? T.mute
                      : silRatio >= 2.0  ? T.red
                      : silRatio >= 1.5  ? T.orange
                      : silRatio >= 1.0  ? T.amber
                      : T.mute;
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "5px 10px", margin: "0 12px 6px",
      borderRadius: 5, background: st.bg, border: `1px solid ${st.border}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: st.dot, display: "inline-block",
          boxShadow: skip > 0 ? `0 0 5px ${st.dot}` : "none" }} />
        <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800, color: st.text, letterSpacing: "0.05em" }}>
          RÉGIMEN {st.label}
        </span>
        <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: st.text, opacity: 0.75 }}>
          · {st.action}
        </span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: FONT_MONO, fontSize: 8, color: T.textD }}>
        {silRatio !== null && (
          <span style={{ color: silColor }}>{"sil " + silRatio.toFixed(2) + "\xD7"}</span>
        )}
        <span>{skip > 0 ? `skip ${skip} · opp ${opp}` : `opp ${opp}`}</span>
      </div>
    </div>
  );
}

/* ── D.7.0 Regime Badge v2 (Burst + Performance + Frequency) ── */
function RegimeBadgeV2({ regimeData }) {
  const STYLES = {
    BUENO:    { bg: T.green  + "10", border: T.green  + "44", text: T.green,  dot: T.green,  action: "+0s"   },
    MEDIOCRE: { bg: T.amber  + "10", border: T.amber  + "44", text: T.amber,  dot: T.amber,  action: "+2 min" },
    DIFICIL:  { bg: "#ff9f4310",     border: "#ff9f4344",     text: T.orange, dot: T.orange, action: "+4 min" },
    CRITICO:  { bg: T.red    + "10", border: T.red    + "44", text: T.red,    dot: T.red,    action: "+6 min" },
  };
  const TIMING_LABELS = {
    IN_BURST:         "🔥 ráfaga",
    POST_BURST_EARLY: "🌙 post-ráfaga",
    POST_BURST_DEEP:  "💤 post-ráfaga prof.",
    UNIFORM_FAST:     "⚡ uniforme rápido",
    UNIFORM_NORMAL:   "〰 uniforme",
    UNIFORM_SLOW:     "🐢 uniforme lento",
    DEAD:             "☠️ muerto",
  };
  if (!regimeData?.regime) {
    return (
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "5px 10px", margin: "0 12px 6px",
        borderRadius: 5, background: T.amber + "08", border: `1px solid ${T.amber}22`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: T.amber, display: "inline-block", opacity: 0.4 }} />
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800, color: T.amber, letterSpacing: "0.05em", opacity: 0.5 }}>MEDIOCRE</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.amber, opacity: 0.4 }}>· +2 min</span>
        </div>
        <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>sin eval</span>
      </div>
    );
  }
  const st        = STYLES[regimeData.regime] || STYLES.MEDIOCRE;
  const tsLabel   = TIMING_LABELS[regimeData.timing_state] || (regimeData.timing_state || "").replace(/_/g, " ");
  const wr5       = regimeData.wr_5;
  const pnl2h     = regimeData.pnl_2h;
  const losses    = regimeData.consecutive_losses;
  const aln       = regimeData.aligned_per_h ?? 0;
  const cvRaw     = regimeData.gap_cv ?? 0;
  const silenceS  = regimeData.current_silence_s ?? 0;
  const silMin    = Math.floor(silenceS / 60);
  const silSec    = Math.floor(silenceS % 60);
  const hasPerf   = wr5 != null && pnl2h != null;
  const wrColor   = !hasPerf ? T.mute : wr5 >= 40 ? T.green : wr5 >= 25 ? T.amber : T.red;
  const pnlColor  = !hasPerf ? T.mute : pnl2h >= 0 ? T.green : pnl2h >= -2 ? T.amber : T.red;
  const lossColor = !hasPerf ? T.mute : (losses || 0) === 0 ? T.textD : (losses || 0) < 3 ? T.amber : T.red;
  // cv=0 sin datos suficientes → mostrar "—"
  const cvStr     = cvRaw > 0 ? Number(cvRaw).toFixed(2) : "—";
  return (
    <div style={{
      padding: "6px 10px", margin: "0 12px 6px",
      borderRadius: 5, background: st.bg, border: `1px solid ${st.border}`,
      display: "flex", flexDirection: "column", gap: 3,
    }}>
      {/* Fila 1: régimen + acción + timing */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: st.dot, display: "inline-block" }} />
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800, color: st.text, letterSpacing: "0.05em" }}>
            {regimeData.regime}
          </span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: st.text, opacity: 0.75 }}>
            · pending {st.action}
          </span>
        </div>
        {tsLabel && (
          <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>{tsLabel}</span>
        )}
      </div>
      {/* Fila 2: PERF */}
      <div style={{ display: "flex", gap: 6, fontFamily: FONT_MONO, fontSize: 8, color: T.textD, flexWrap: "wrap", alignItems: "center" }}>
        <span style={{ color: T.mute }}>PERF</span>
        {hasPerf ? (
          <>
            <span style={{ color: wrColor }}>WR5 {wr5}%</span>
            <span style={{ color: T.mute }}>·</span>
            <span style={{ color: pnlColor }}>PnL2h {(pnl2h || 0) >= 0 ? "+" : ""}{Number(pnl2h).toFixed(2)}</span>
            <span style={{ color: T.mute }}>·</span>
            <span style={{ color: lossColor }}>losses {losses ?? 0}</span>
          </>
        ) : (
          <span style={{ color: T.mute, opacity: 0.6 }}>sin trades aún</span>
        )}
      </div>
      {/* Fila 3: TIMING */}
      <div style={{ display: "flex", gap: 6, fontFamily: FONT_MONO, fontSize: 8, color: T.textD, flexWrap: "wrap", alignItems: "center" }}>
        <span style={{ color: T.mute }}>TIMING</span>
        <span>sil {silMin}m{silSec}s</span>
        <span style={{ color: T.mute }}>·</span>
        <span>cv {cvStr}</span>
        <span style={{ color: T.mute }}>·</span>
        <span>aln {Number(aln).toFixed(1)}/h</span>
      </div>
    </div>
  );
}

/* ── symbol card (fusión: data técnica completa + nuevo visual) ── */
function SymbolCard({ s }) {
  const [analytics, setAnalytics] = useState(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const sym = s.symbol;
    if (!sym) return;
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch(`/api/deriv/analytics/market-context?symbol=${sym}`);
        if (!active) return;
        const data = res.ok ? await res.json() : null;
        setAnalytics(data);
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 5000);
    return () => { active = false; clearInterval(id); };
  }, [s.symbol]);

  /* ─ data ─ */
  const ready  = s.readiness || { state: "SIN_DATOS", level: 0 };
  const rc     = READY_COLOR[ready.state] || T.mute;
  const dir    = s.lastSpike?.direction;
  const dirC   = dir === "DOWN" ? T.red : dir === "UP" ? T.green : T.textD;
  const secs   = Number(s.secsSinceLastSpike) || 0;
  const p75Sec = Number(s.p75GapSec) || null;
  const medSec = Number(s.medianGapSec) || null;
  const ticks  = s.live?.ticksSinceSpike ?? s.lastSpike?.ticks_since_last_spike ?? null;
  const recent = Number(s.counts?.h1) || 0;
  const cluster = analytics?.snapshot?.spike_cluster_active ?? false;
  const hasOpen = !!s.openContract;
  const hurst  = analytics?.snapshot?.hurst ?? null;

  // ── _sn + V2 fields DECLARED EARLY — used by semaphore, compact msg & accum bar ──
  const _snEarly   = analytics?.snapshot ?? null;
  const _v2Bucket  = _snEarly?.progressive_imminence_bucket ?? null;
  const _v2Ratio   = _snEarly?.progressive_imminence_ratio ?? null;
  const _v2Ceiling = _snEarly?.ceiling_value_s ?? null;
  const _v2CeilSrc = _snEarly?.ceiling_source ?? null;
  const _v2HTBonus = _snEarly?.hurst_time_compound_bonus ?? null;
  const _v2HTReasn = _snEarly?.hurst_time_compound_reason ?? null;
  const hurstH = analytics?.hurst_history ?? [];

  /* ─ score freshness + gap (needed before isInactive / sem) ─ */
  const scoreAgeSec0  = s.live?.ts ? Math.max(0, Date.now() / 1000 - s.live.ts) : null;
  const scoreIsStale0 = scoreAgeSec0 != null && scoreAgeSec0 > 90;
  const scoreGap0     = s.live?.scoreGap ?? null;   // gap available early for semaphore
  // liveEarly extracted early so compact message can use it (FIX 4)
  const liveEarlyActive  = s.live?.earlyEntryActive ?? false;
  const liveEarlyRemain  = s.live?.earlyEntryRemainSec ?? null;

  /* ─ semáforo ─ */
  // isInactive only fires when score is stale — if score is fresh, the symbol IS processing normally.
  const isInactive    = scoreIsStale0 && (s.topReasons?.some(r => r.reason === "dynamic_symbol_inactive") || ready.state === "SIN_DATOS");
  const isManualOnly  = s.topReasons?.some(r => r.reason === "manual_only");
  const hasChaseGuard = s.topReasons?.some(r => r.reason?.includes("chase") || r.reason?.includes("post_spike_chase"));
  const clusterDone   = recent >= 3 && !cluster;

  let sem;
  if (isInactive) sem = "red";
  else if (clusterDone) sem = "amber";  // post-cluster: informational only, bot gates handle kinetic suppression
  else if (isManualOnly && scoreGap0 != null && scoreGap0 <= 0) sem = "manual";
  else if (secs > 0 && p75Sec && secs > p75Sec && !hasChaseGuard) sem = "green";
  else sem = "amber";

  const SEM = {
    green:  { label: "Lista para entrar",          dot: T.green },
    amber:  { label: "Observar",                   dot: T.amber },
    manual: { label: "Condiciones OK — tú entras", dot: T.violet },
    red:    { label: isInactive ? "Pausado" : "No entrar ahora", dot: isInactive ? T.mute : T.red },
  };
  const S = SEM[sem] || SEM["amber"];

  /* ─ compact message (1 línea) ─ */
  const minsSince = secs > 0 ? Math.round(secs / 60) : null;
  const medMins   = medSec ? Math.round(medSec / 60) : null;
  let msgEmoji, msgLine;
  if (isInactive) {
    msgEmoji = "⛔"; msgLine = "Símbolo pausado por el sistema automático";
  } else if (isManualOnly && scoreGap0 != null && scoreGap0 <= 0) {
    msgEmoji = "🎯"; msgLine = `Score OK · entra manualmente${minsSince != null ? ` · ${minsSince} min sin spike` : ""}`;
  } else if (clusterDone) {
    // FIX CRÍTICO 1: usa techo V2 en lugar de medMins (p50 sesgado)
    const _ceilMins = _v2Ceiling ? Math.round(_v2Ceiling / 60) : null;
    msgEmoji = "🚨";
    msgLine = secs > 300
      ? `Post-cluster · ${minsSince}m sin spike · esperando normalización`
      : `Cluster agotado — espera${_ceilMins ? ` ~${_ceilMins}m (techo)` : medMins ? ` ~${medMins}m` : " un rato"} antes de entrar`;
  } else if (sem === "green") {
    msgEmoji = "✅"; msgLine = `Zona cargada${minsSince != null ? ` · ${minsSince} min sin caída` : ""}`;
  } else if (liveEarlyActive) {
    // FIX CRÍTICO 4: warmup no debe mostrarse como "Acumulando"
    msgEmoji = "⏱";
    msgLine = `WARMUP${liveEarlyRemain != null ? ` · ${Math.round(liveEarlyRemain)}s` : " · acumulando señal inicial"}`;
  } else {
    // FIX CRÍTICO 2: ratio V2 en lugar de p75 sesgado
    const pct = _v2Ratio != null
      ? Math.round(_v2Ratio * 100)
      : (p75Sec && secs ? Math.round((secs / p75Sec) * 100) : null);
    const bktLabel = _v2Bucket && _v2Bucket !== "fresh" ? ` [${_v2Bucket}]` : "";
    msgEmoji = "⏳"; msgLine = `Acumulando${pct != null ? ` · ${pct}% del techo` : ""}${bktLabel}`;
  }

  /* ─ score strip ─ */
  const live = s.live;
  const score = live?.score;
  const gate  = live?.gate;
  const gap   = live?.scoreGap;
  const liveGateName = live?.gateName ?? null;
  const liveFvgBos   = live?.fvgBosValidated ?? null;
  // liveEarlyActive / liveEarlyRemain declared early (above) for compact message
  const liveKind = live?.kind;
  const isVetado = liveKind === "BLOQUEADO" && gap != null && gap <= 0;
  const pctToGate    = gate && score != null ? Math.max(0, Math.min(100, (score / gate) * 100)) : null;
  const scoreAgeSec  = scoreAgeSec0;
  const scoreIsStale = scoreIsStale0;
  const scoreColor = isVetado ? T.amber : gap != null && gap <= 0 ? (scoreIsStale ? T.amber : T.green) : gap != null && gap < 0.5 ? T.amber : T.cyan;
  const gapColor   = isVetado ? T.amber : gap != null && gap <= 0 ? (scoreIsStale ? T.amber : T.green) : T.amber;
  const faltaText  = gap == null ? "–" : isVetado ? "VETADO" : gap <= 0 ? (scoreIsStale ? "LISTO ?" : "LISTO ✓") : `+${num(gap)}`;

  /* ─ Hurst ─ */
  const hurstColor    = hurst == null ? T.mute : hurst < 0.45 ? T.green : hurst < 0.55 ? T.amber : T.red;
  const hurstLabel    = hurst == null ? "–" : hurst < 0.45 ? "sin fuerza" : hurst < 0.55 ? "neutro" : "con fuerza";
  const hurstPos      = hurst != null ? Math.max(2, Math.min(98, ((hurst - 0.30) / 0.40) * 100)) : 50;
  const hurstBarColor = (h) => h < 0.45 ? T.green : h < 0.55 ? T.amber : T.red;

  /* ─ accumulation bar — FIX CRÍTICO 3: usa techo V2 como denominador ─ */
  const accumPct  = _v2Ceiling
    ? Math.min(100, Math.round((secs / _v2Ceiling) * 100))
    : Math.min(100, Math.round((secs / (p75Sec || medSec || 1)) * 100));
  const barColor  = sem === "green" ? T.green : sem === "red" ? T.red : sem === "manual" ? T.violet : T.amber;
  const barSuffix = accumPct >= 100 ? "· zona óptima" : accumPct >= 75 ? "· casi lista" : accumPct >= 40 ? "· zona de espera" : "· muy pronto";

  // ── SEÑAL MAESTRA — card-level (drives conditional rendering) ──────────
  const _sn           = analytics?.snapshot ?? null;
  const _lastSpikeWTs = _sn?.last_spike_wall_ts ?? null;
  const _secsSinceSpk = _lastSpikeWTs ? Math.floor(now / 1000 - _lastSpikeWTs) : null;
  const _isBlindZone  = _secsSinceSpk != null && _secsSinceSpk < 120;
  const _burstActive  = _sn?.burst_active ?? false;
  const _burstDepth   = _sn?.burst_depth ?? null;
  // SEÑAL MAESTRA variables (mirrors inner block logic)
  const _scoreRawC    = _sn?.score_raw ?? null;
  const _setupTypeC   = _sn?.setup_type ?? null;
  const _gradeC       = _sn?.execution_grade ?? null;
  const _scarcityC    = _sn?.scarcity_state ?? null;
  const _immStateC    = _sn?.spike_imminence_state ?? null;
  const _immScoreC    = _sn?.spike_imminence_score ?? null;
  const _fvgAnchC     = _sn?.fvg_anchor_active ?? false;
  const _structConfC  = _sn?.structural_fvg_confirm ?? false;
  const _structConflC = _sn?.structural_fvg_conflict ?? false;
  const _ema200DistC  = _sn?.ema200_distance_pct ?? null;
  const _isCrashCard  = s.symbol.toUpperCase().includes("CRASH");
  const _isBoomCard   = s.symbol.toUpperCase().includes("BOOM");
  const _ldThr        = 0.00008; // 0.008% in fraction form
  const _ema200LoadedC = _ema200DistC == null ? null
    : _isCrashCard ? _ema200DistC >= _ldThr
    : _isBoomCard  ? _ema200DistC <= -_ldThr
    : null;
  const _rngProbC   = _sn?.rng_probability ?? null;
  const _rngThreshC = _sn?.rng_threshold ?? 65;
  const _evalAgeS   = _sn?.eval_age_s ?? null;
  const _isStale    = _evalAgeS != null && _evalAgeS > 90;

  const _masterRed    = _structConflC
    || _scarcityC === "SECO"
    || (_scarcityC === "VENCIDO" && (_scoreRawC == null || _scoreRawC < 7.0))
    || _gradeC === "C"
    || _ema200LoadedC === false;
  const _masterGreen  = !_masterRed && !_isStale && _sn != null
    && _rngProbC != null && _rngProbC >= _rngThreshC
    && (_gradeC === "A" || _gradeC === "B")
    && (_scarcityC == null || ["FRESCO","CARGANDO","LISTO"].includes(_scarcityC));

  // ── DISPLAY SCORE — usa score_raw del analytics cuando decisión está vencida ──
  const _usingLiveScore = _scoreRawC != null && (score == null || scoreIsStale0);
  const _dispScore    = _usingLiveScore ? _scoreRawC    : score;
  const _dispGap      = (_usingLiveScore && gate != null) ? +(gate - _scoreRawC).toFixed(2) : gap;
  const _dispPct      = gate && _dispScore != null ? Math.max(0, Math.min(100, (_dispScore / gate) * 100)) : null;
  const _dispFalta    = _dispGap == null ? "–"
    : isVetado ? "VETADO"
    : _dispGap <= 0 ? "LISTO ✓"
    : `+${num(_dispGap)}`;
  const _dispScoreC   = isVetado ? T.amber
    : _dispGap != null && _dispGap <= 0 ? T.green
    : _dispGap != null && _dispGap < 0.5 ? T.amber : T.cyan;
  const _dispGapC     = isVetado ? T.amber
    : _dispGap != null && _dispGap <= 0 ? T.green : T.amber;

  // ── CASCADA ────────────────────────────────────────────────────────────────
  const _cascadeActive    = _sn?.cascade_active ?? false;
  const _cascadeGapTicks  = _sn?.cascade_gap_ticks ?? null;
  const _cascadeDepth     = _sn?.burst_depth ?? null;
  // CASCADE overrides blind zone: continuous momentum = no retroceso window needed
  const _inCascade        = _cascadeActive && _isBlindZone;

  // ── PRESIÓN REAL ───────────────────────────────────────────────────────────
  const _presion          = analytics?.presion ?? null;
  const _presionScore     = _presion?.score ?? null;
  const _presionLabel     = _presion?.label ?? null;
  const _presionColor     = _presionScore == null ? T.mute
    : _presionScore >= 8 ? T.red
    : _presionScore >= 6 ? T.orange
    : _presionScore >= 4 ? T.amber
    : T.green;
  // Z-score for slow-market awareness
  const _zScore           = analytics?.spike_stats?.z_score ?? null;
  const _ticksWait        = analytics?.spike_stats?.current_wait ?? null;
  const _p90Ticks         = analytics?.spike_stats?.p90_gap ?? null;
  const _isOverdueExtreme = _zScore != null && _zScore >= 2.0;
  const _isOverdueHigh    = _zScore != null && _zScore >= 1.5 && _zScore < 2.0;

  return (
    <div style={{
      background: T.panel, borderRadius: 10,
      border: `1px solid ${S.dot}55`,
      boxShadow: `0 0 16px ${S.dot}12`,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>

      {/* posición abierta */}
      {hasOpen && (
        <div style={{
          padding: "7px 12px", background: T.cyan + "10", borderBottom: `1px solid ${T.cyan}33`,
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.cyan, fontWeight: 700 }}>
            ● POSICIÓN ABIERTA · {sideLabel(s.openContract.side)} · {dur(s.openContract.duration_sec)}
          </span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 13, fontWeight: 800, color: pnlColor(s.openContract.floating_pnl) }}>
            {Number(s.openContract.floating_pnl) >= 0 ? "+" : ""}{num(s.openContract.floating_pnl)} USDT
          </span>
        </div>
      )}

      {/* header: dot + symbol + readiness + semaphore badge */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "10px 12px", background: T.panel2, borderBottom: `1px solid ${T.border}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{
            width: 9, height: 9, borderRadius: "50%", background: rc,
            boxShadow: `0 0 8px ${rc}`, display: "inline-block",
            animation: ready.level >= 4 ? "pulse 1.1s infinite" : "none",
          }} />
          <span style={{ fontFamily: FONT_MONO, fontWeight: 800, fontSize: 15, color: T.text, letterSpacing: "0.04em" }}>
            {s.symbol}
          </span>
          {isManualOnly && (
            <span style={{
              fontFamily: FONT_MONO, fontSize: 8, fontWeight: 700, color: T.violet,
              background: T.violet + "18", border: `1px solid ${T.violet}44`,
              borderRadius: 20, padding: "2px 7px", letterSpacing: "0.06em",
            }}>MANUAL</span>
          )}
        </div>
        <span style={{
          fontFamily: FONT_MONO, fontSize: 9.5, fontWeight: 800, color: S.dot,
          background: S.dot + "18", border: `1px solid ${S.dot}44`,
          borderRadius: 20, padding: "3px 9px", letterSpacing: "0.04em",
        }}>{S.label}</span>
      </div>


      {/* compact message — solo para estados críticos (no decorativos) */}
      {(isInactive || (isManualOnly && scoreGap0 != null && scoreGap0 <= 0)) && (
        <div style={{
          padding: "6px 12px", background: S.dot + "0e",
          borderBottom: `1px solid ${S.dot}22`,
        }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, fontWeight: 800, color: S.dot }}>
            {msgEmoji} {msgLine}
          </span>
        </div>
      )}

      {/* ══ CASCADE MOMENTUM (solo cuando está activo) ══ */}
      {_inCascade && (
        <div style={{ padding: "6px 12px", borderBottom: `1px solid ${T.border}` }}>
          <div style={{
            background: T.orange + "0d", border: `1px solid ${T.orange}33`,
            borderRadius: 6, padding: "5px 10px",
          }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800,
              color: T.orange, letterSpacing: "0.08em" }}>
              CASCADE — MOMENTUM ACTIVO
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.mute, marginTop: 1 }}>
              {`${_cascadeDepth ?? "?"}x spikes · gap ${_cascadeGapTicks ?? "?"}t`}
            </div>
          </div>
        </div>
      )}

      {/* ENTRADA CONFIRMADA removido (Diego 2026-06-20) — redundante con SETUP+GRADE+SCAR abajo */}




      {/* ══ tiempo + cadencia ══ */}
      <div style={{ padding: "11px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
        {/* Sin spike hace — grande + timing */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Sin spike hace</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 21, fontWeight: 800, color: rc, lineHeight: 1.1 }}>{ago(secs)}</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, marginTop: 2 }}>
              típico {dur(medSec)} · p75 {dur(p75Sec)}
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>último spike</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 12, fontWeight: 700, color: dirC, lineHeight: 1.3 }}>{dir || "–"} {num(s.lastSpike?.ratio, 0)}x</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>{fmtClock(s.lastSpike?.ts)}</div>
          </div>
        </div>

        {/* Cadencia — h1 grande, h6 + h24 secundarios */}
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, borderTop: `1px solid ${T.border}`, paddingTop: 8 }}>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", minWidth: 40 }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 28, fontWeight: 800, color: T.text, lineHeight: 1 }}>
              {s.counts?.h1 ?? "–"}
            </span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute, letterSpacing: "0.08em" }}>spk/1h</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 3, flex: 1 }}>
            <div style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 17, fontWeight: 700, color: T.textD, lineHeight: 1 }}>
                  {s.counts?.h6 ?? "–"}
                </span>
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>6h</span>
              </div>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 14, fontWeight: 600, color: T.textD, lineHeight: 1, opacity: 0.7 }}>
                  {s.counts?.h24 ?? "–"}
                </span>
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute, opacity: 0.7 }}>24h</span>
              </div>
            </div>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.cyan }}>
              {num(s.ratePerHour?.h6, 1)}/h prom 6h
            </span>
          </div>
        </div>


        {/* Z-score de espera — alerta operacional mínima */}
        {analytics?.spike_stats && (() => {
          const st = analytics.spike_stats;
          const z = st.z_score;
          if (z == null || z < 1.5) return null;
          const zColor = z >= 2.0 ? T.red : T.amber;
          const zLabel = z >= 2.0 ? "SOBREPRESIÓN" : "PRESIÓN ALTA";
          return (
            <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 6,
              display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800, color: zColor }}>
                {zLabel}
              </span>
              <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.textD }}>
                Z{z > 0 ? "+" : ""}{z.toFixed(2)}
              </span>
            </div>
          );
        })()}

        {/* REMOVED: analytics block (V2, Entry Quality, RNG, Triple Lock, etc.) */}
        {false && analytics?.snapshot && (
          <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 5, display: "flex", flexDirection: "column", gap: 3 }}>
            {analytics.spike_stats && (() => {
              const st = analytics.spike_stats;
              const zColor = st.z_score > 2 ? T.red : st.z_score > 1 ? T.amber : st.z_score < -0.5 ? T.green : T.textD;
              return (
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>
                  {"Z "}
                  <span style={{ color: zColor }}>{st.z_score > 0 ? "+" : ""}{st.z_score.toFixed(2)}</span>
                  {" · típico "}
                  <span style={{ color: T.textD }}>{st.p50_gap}t</span>
                  {" · p75 "}
                  <span style={{ color: T.textD }}>{st.p75_gap}t</span>
                  <span style={{ color: T.mute }}>{" ·n="}{st.sample_size}</span>
                </span>
              );
            })()}

            {/* ── FIX 6: TECHO V2 / BUCKET / RATIO ─────────────────────── */}
            {_v2Ceiling != null && (() => {
              const bucketColor = {
                fresh: T.mute, warming: T.cyan, medium: T.amber,
                high: T.orange, very_high: T.red, overdue_extreme: T.red,
              }[_v2Bucket] || T.mute;
              const ratioDisplay = _v2Ratio != null ? `${Math.round(_v2Ratio * 100)}%` : "–";
              return (
                <div style={{ display: "flex", gap: 5, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.mute, letterSpacing: "0.06em" }}>V2</span>
                  <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>
                    techo <span style={{ color: T.textD }}>{Math.round(_v2Ceiling / 60)}m</span>
                    <span style={{ color: T.mute, fontSize: 7 }}> {_v2CeilSrc === "dynamic_p99_higher" ? "p99" : _v2CeilSrc === "hardcoded_fallback" ? "~fijo" : "fijo"}</span>
                  </span>
                  {_v2Bucket && (
                    <span style={{
                      fontFamily: FONT_MONO, fontSize: 7, fontWeight: 700, color: bucketColor,
                      background: bucketColor + "18", border: `1px solid ${bucketColor}44`,
                      borderRadius: 3, padding: "1px 4px",
                    }}>{_v2Bucket}</span>
                  )}
                  <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: bucketColor }}>{ratioDisplay}</span>
                  {/* FIX 8: Compound Hurst+Time */}
                  {_v2HTBonus != null && _v2HTBonus !== 0 && (
                    <span style={{ fontFamily: FONT_MONO, fontSize: 7, color: _v2HTBonus > 0 ? T.green : T.orange }}>
                      H+T {_v2HTBonus > 0 ? "+" : ""}{_v2HTBonus.toFixed(1)}
                      {_v2HTReasn ? ` (${_v2HTReasn.replace(/_/g, " ")})` : ""}
                    </span>
                  )}
                </div>
              );
            })()}

            {/* ── ESTRUCTURA 15M (Vision LLM — 1 línea) ─────────────── */}
            {s.vision && (() => {
              const v = s.vision;
              const trend = v.trend_15m || "ranging";
              const conf = typeof v.confidence === "number" ? v.confidence : 0;
              const isBoom = s.symbol?.startsWith("BOOM");
              const biasAligned = (isBoom && trend === "downtrend") || (!isBoom && trend === "uptrend");
              const biasConflict = (isBoom && trend === "uptrend") || (!isBoom && trend === "downtrend");
              const trendColor = biasAligned ? T.green : biasConflict ? T.red : T.amber;
              const trendArrow = trend === "uptrend" ? "▲" : trend === "downtrend" ? "▼" : "◆";
              const biasLabel = trend === "ranging" ? "LATERAL"
                : isBoom ? (trend === "downtrend" ? "CARGANDO ✓" : "YA SUBIÓ ✗")
                : (trend === "uptrend" ? "CARGANDO ✓" : "YA CAYÓ ✗");
              const ageMin = v.updated_at ? Math.round((Date.now() / 1000 - v.updated_at) / 60) : null;
              return (
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontFamily: FONT_MONO, fontSize: 9 }}>
                  <span style={{ color: T.mute, fontSize: 8 }}>15M</span>
                  <span style={{ color: trendColor, fontWeight: 900 }}>{trendArrow}</span>
                  <span style={{ color: trendColor, fontWeight: 800 }}>{biasLabel}</span>
                  {conf > 0 && <span style={{ color: T.mute, fontSize: 8 }}>conf {Math.round(conf * 100)}%</span>}
                  {ageMin !== null && <span style={{ color: T.mute, fontSize: 7 }}>hace {ageMin}m</span>}
                </div>
              );
            })()}

            {/* ── Alertas operacionales ─────────────────────────────────── */}
            {_inCascade && (
              <div style={{
                padding: "5px 8px",
                background: T.orange + "18", border: `1px solid ${T.orange}55`,
                borderRadius: 5, display: "flex", alignItems: "center", gap: 8,
              }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800, color: T.orange, letterSpacing: "0.10em" }}>
                  ⚡ CASCADE
                </span>
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.textD }}>
                  {_cascadeDepth ?? "?"}x · {_cascadeGapTicks ?? "?"}t entre spikes
                </span>
              </div>
            )}
            {(_isOverdueExtreme || _isOverdueHigh) && !_isBlindZone && (
              <div style={{
                padding: "4px 8px",
                background: (_isOverdueExtreme ? T.red : T.amber) + "14",
                border: `1px solid ${(_isOverdueExtreme ? T.red : T.amber)}44`,
                borderRadius: 5, display: "flex", alignItems: "center", gap: 8,
              }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800,
                  color: _isOverdueExtreme ? T.red : T.amber }}>
                  {_isOverdueExtreme ? "SOBREPRESIÓN" : "PRESIÓN ALTA"}
                </span>
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.textD }}>
                  Z={_zScore != null ? (_zScore > 0 ? "+" : "") + _zScore.toFixed(2) : "?"}
                  {/* FIX 5: techo V2 + bucket en vez de p90 en ticks */}
                  {_v2Ceiling != null
                    ? ` · techo=${Math.round(_v2Ceiling / 60)}m`
                    : (_p90Ticks != null ? ` · p90=${_p90Ticks}t` : "")}
                  {_v2Bucket && ` · [${_v2Bucket}]`}
                </span>
              </div>
            )}
            {/* ── ENTRY QUALITY INDICATORS ─────────────────────────────── */}
            {(() => {
              const sn = analytics.snapshot;
              const setupType    = sn.setup_type    || null;
              const grade        = sn.execution_grade || null;
              const scarcity     = sn.scarcity_state  || null;
              const immState     = sn.spike_imminence_state || null;
              const immScore     = sn.spike_imminence_score ?? null;
              const geoPos       = sn.geo_channel_pos ?? null;
              const fvgTier      = sn.fvg_tier || null;
              const scoreRaw     = sn.score_raw ?? null;
              const burstActive     = sn.burst_active ?? false;
              const burstDepth      = sn.burst_depth ?? null;
              const burstRetroceso  = sn.burst_retroceso ?? null;
              const fvgAnchorActive    = sn.fvg_anchor_active ?? false;
              const fvgAnchorAgeS      = sn.fvg_anchor_age_s ?? null;
              const fvgAnchorWick      = sn.fvg_anchor_wick_survived ?? null;
              const fvgAnchorTolPen    = sn.fvg_anchor_tol_penalty ?? null;
              const fvgAnchorMomPen    = sn.fvg_anchor_mom_penalty ?? null;
              const structFvgConfirm  = sn.structural_fvg_confirm ?? false;
              const structFvgConflict = sn.structural_fvg_conflict ?? false;
              const structFvgAbsent   = sn.structural_fvg_absent ?? false;
              const structFvgActive   = sn.structural_fvg_active ?? null;
              const structFvgDir      = sn.structural_fvg_direction ?? null;
              const atrAnchored       = sn.atr_anchored ?? false;
              const ema200Anchored    = sn.ema200_anchored ?? false;
              const ema200DistPct     = sn.ema200_distance_pct ?? null;
              const kineticVelocity   = sn.kinetic_velocity ?? null;
              const kineticAccel      = sn.kinetic_acceleration ?? null;
              const kineticCompressed = sn.kinetic_compressed ?? false;
              const ghostMad          = sn.ghost_mad ?? null;
              const fvgBosValidated   = sn.fvg_bos_validated ?? false;
              const trifectaMet       = sn.trifecta_met ?? false;
              const trifectaVision    = sn.trifecta_vision ?? false;
              const trifectaKinetic   = sn.trifecta_kinetic ?? false;
              const trifectaScarcity  = sn.trifecta_scarcity ?? false;
              const trifectaFvgBos    = sn.trifecta_fvg_bos ?? false;
              const rngProb           = sn.rng_probability ?? null;
              const rngMissing        = sn.rng_missing ?? [];
              const rngThreshold      = sn.rng_threshold ?? 65;
              const masterKeyBypass   = sn.master_key_bypass ?? null;
              const masterKeyRng      = sn.master_key_rng ?? null;
              // ── Pipeline state (new motor fields) ─────────────────────────
              const tripleLockFired   = sn.triple_lock_fired ?? false;
              const tripleLockZ       = sn.triple_lock_z ?? null;
              const tripleLockElapsed = sn.triple_lock_elapsed ?? null;
              const tripleLockKinetic = sn.triple_lock_kinetic ?? null;
              const tripleLockBlocked = sn.triple_lock_blocked ?? null;
              const elasticityZ       = sn.elasticity_z ?? null;
              const elasticityOrig    = sn.elasticity_regime_min_original ?? null;
              const elasticityElastic = sn.elasticity_regime_min_elastic ?? null;
              const elasticityTimePct = sn.elasticity_time_pressure ?? null;
              const elasticityTimeTriggered = sn.elasticity_time_triggered ?? false;
              const marketContext     = sn.market_context ?? null;
              const fvgLookbackUsed   = sn.fvg_lookback_used ?? null;
              const scarcityElapsedS  = sn.scarcity_elapsed_s ?? null;
              const postClusterExhaustion = sn.post_cluster_exhaustion ?? false;
              // EMA200 loaded: for CRASH need price above EMA200 (dev ≥ +0.008% fraction)
              //                for BOOM  need price below EMA200 (dev ≤ −0.008% fraction)
              const _isCrashSym = s.symbol.toUpperCase().includes("CRASH");
              const _isBoomSym  = s.symbol.toUpperCase().includes("BOOM");
              const _ldThresh   = 0.00008; // = 0.008% / 100
              const ema200Loaded = ema200DistPct == null ? null
                : _isCrashSym ? ema200DistPct >= _ldThresh
                : _isBoomSym  ? ema200DistPct <= -_ldThresh
                : null;
              // Time since last spike in human-readable form (ticks ≈ seconds for synthetics)
              const _ticksSinceSpi  = sn.ticks_since_last_spike ?? null;
              const _timeSinceSpiStr = _ticksSinceSpi == null ? null
                : _ticksSinceSpi < 60 ? `${_ticksSinceSpi}s`
                : `${Math.floor(_ticksSinceSpi / 60)}m${_ticksSinceSpi % 60 > 0 ? ((_ticksSinceSpi % 60) + "s") : ""}`;

              if (!setupType && !scarcity && !grade && !fvgTier && geoPos == null) return null;

              // ── Post-spike blind zone ──────────────────────────────────────
              // Indicators computed in the 120s after a spike are biased by the
              // spike's price action (FVG/SMC/geo all reflect the spike anomaly).
              // Use last_spike_wall_ts for real-time accuracy (not cached ticks).
              const lastSpikeWallTs = sn.last_spike_wall_ts ?? null;
              const secsSinceSpike  = lastSpikeWallTs
                ? Math.floor(Date.now() / 1000 - lastSpikeWallTs) : null;
              // 0-120s: ciega total (indicadores sesgados por el spike)
              // 120-300s: normalizando (primeras velas post-spike, aún parcialmente sesgados)
              const isBlindZone       = secsSinceSpike != null && secsSinceSpike < 120;
              const isNormalizingZone = secsSinceSpike != null && secsSinceSpike >= 120 && secsSinceSpike < 300;
              const postSpikeLabel = secsSinceSpike != null && secsSinceSpike < 300
                ? (secsSinceSpike < 60 ? `${secsSinceSpike}s` : `${Math.floor(secsSinceSpike/60)}m${secsSinceSpike%60}s`)
                : null;

              // ── Staleness (datos viejos del cache) ─────────────────────────
              const ageS = sn.ts ? Math.floor(Date.now() / 1000 - sn.ts) : null;
              const isStale     = !isBlindZone && ageS != null && ageS > 90;
              const isVeryStale = !isBlindZone && ageS != null && ageS > 180;
              const ageLabel = (!isBlindZone && !isNormalizingZone && ageS != null)
                ? (ageS < 60 ? `hace ${ageS}s` : `hace ${Math.floor(ageS/60)}m${ageS%60}s`) : null;

              const staleOpacity = isVeryStale ? 0.35 : isStale ? 0.55 : 1.0;
              const headerColor  = isBlindZone ? T.red
                : isNormalizingZone ? T.amber
                : isVeryStale ? T.red : isStale ? T.amber : T.mute;

              // Setup color — TREND ahora es ámbar (bot lo permite vía RNG)
              const setupColor = setupType === "SMC_FVG" ? T.green
                : setupType === "TREND" ? T.amber
                : setupType === "TREND_NO_STRUCT" ? T.amber
                : setupType === "EMA200_SPIKE" ? T.cyan : T.mute;
              const setupLabel = setupType === "SMC_FVG" ? "SMC_FVG"
                : setupType === "TREND" ? "TREND"
                : setupType === "TREND_NO_STRUCT" ? "SIN BOS"
                : setupType === "EMA200_SPIKE" ? "EMA200" : (setupType || "–");

              // Grade color
              const gradeColor = grade === "A" ? T.green : grade === "B" ? T.amber : grade === "C" ? T.red : T.mute;

              // Scarcity: FRESCO right after a spike is correct but does NOT mean "enter now"
              // Color reflects cycle state, not entry permission
              const scarColor = ["SECO","VENCIDO"].includes(scarcity) ? T.red
                : ["FRESCO","CARGANDO","LISTO"].includes(scarcity) ? T.green : T.mute;

              // Imminence: sweet spot 0.3–0.6
              const immColor = immScore != null
                ? (immScore >= 0.3 && immScore <= 0.6 ? T.green : immScore > 0.6 && immScore <= 0.8 ? T.amber : immScore > 0.8 ? T.red : T.mute)
                : T.mute;
              const immStateColor = immState === "BUILDING" ? T.green
                : immState === "RIPE" ? T.amber : immState === "OVERDUE" ? T.amber : T.mute;

              // Geo channel: < 20% = best zone; < 0 = below channel (green), > 1 = above channel (red)
              const geoColor = geoPos != null ? (geoPos < 0.20 ? T.green : geoPos < 0.40 ? T.amber : T.red) : T.mute;
              const geoPct   = geoPos == null ? "–"
                : geoPos < 0 ? "BOT"
                : geoPos > 1 ? "TOP"
                : (geoPos * 100).toFixed(0) + "%";

              // FVG tier
              const fvgTierColor = fvgTier === "fvg_full_confluence" ? T.green
                : fvgTier === "fvg_mitigated" ? T.amber
                : fvgTier === "dynamic_soft_veto_no_fvg" ? T.red : T.mute;
              const fvgTierLabel = fvgTier === "fvg_full_confluence" ? "CONF"
                : fvgTier === "fvg_mitigated" ? "MITIG"
                : fvgTier === "dynamic_soft_veto_no_fvg" ? "SIN_FVG" : (fvgTier ? fvgTier.toUpperCase().slice(0,8) : "–");

              // Score raw color — master key bypass: score válido aunque bajo para el gate
              const scoreRawColor = scoreRaw == null ? T.mute
                : masterKeyBypass ? T.violet
                : scoreRaw >= 7.8 ? T.green : scoreRaw >= 7.0 ? T.amber : T.red;

              // Chips dim only when data is stale
              const chipOpacity = staleOpacity;

              // ── Chip opacity ──────────────────────────────────────────────

              const chip = (label, value, color) => (
                <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 3,
                  background: color + "18", border: `1px solid ${color}44`,
                  borderRadius: 4, padding: "1px 5px", opacity: chipOpacity }}>
                  <span style={{ color: T.mute, fontSize: 7, fontWeight: 600 }}>{label}</span>
                  <span style={{ color, fontSize: 8, fontWeight: 700 }}>{value}</span>
                </span>
              );

              return (
                <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 5 }}>
                  {/* ── TRIPLE LOCK banner ─────────────────────────────────── */}
                  {tripleLockFired && (
                    <div style={{
                      background: T.cyan + "18", border: `2px solid ${T.cyan}88`,
                      borderRadius: 5, padding: "4px 8px", marginBottom: 5,
                      display: "flex", alignItems: "center", gap: 6,
                    }}>
                      <div style={{ width: 9, height: 9, borderRadius: "50%", background: T.cyan,
                        boxShadow: `0 0 6px ${T.cyan}`, flexShrink: 0 }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ color: T.cyan, fontSize: 8, fontWeight: 800, letterSpacing: "0.10em" }}>
                          TRIPLE LOCK
                        </div>
                        <div style={{ color: T.textD, fontSize: 6, marginTop: 1 }}>
                          {`Z=${(tripleLockZ ?? 0).toFixed(2)} · ${tripleLockElapsed != null ? Math.round(tripleLockElapsed) + "s" : "–"} · kin=${(tripleLockKinetic ?? 0).toFixed(3)}`}
                        </div>
                      </div>
                      <div style={{ color: T.cyan, fontSize: 7, fontWeight: 700 }}>+30 impl</div>
                    </div>
                  )}
                  {/* ── TRIPLE LOCK parcialmente bloqueado ─────────────────── */}
                  {!tripleLockFired && tripleLockBlocked && tripleLockBlocked.length > 0 && (
                    <div style={{
                      background: T.orange + "10", border: `1px solid ${T.orange}44`,
                      borderRadius: 4, padding: "3px 7px", marginBottom: 4,
                    }}>
                      <div style={{ color: T.orange, fontSize: 6, fontWeight: 700 }}>
                        TRIPLE LOCK: {tripleLockBlocked.join(" · ")}
                      </div>
                    </div>
                  )}
                  {/* ── ELASTICITY banner ──────────────────────────────────── */}
                  {elasticityZ != null && !tripleLockFired && (
                    <div style={{
                      background: T.violet + "14", border: `1px solid ${T.violet}55`,
                      borderRadius: 4, padding: "3px 7px", marginBottom: 4,
                      display: "flex", alignItems: "center", gap: 6,
                    }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ color: T.violet, fontSize: 7, fontWeight: 700, letterSpacing: "0.08em" }}>
                          {marketContext === "EXTREME_OVERPRESSURE" ? "EXTREME OVERPRESSURE" : "PRESIÓN ALTA"}
                          {elasticityTimeTriggered ? " · TIME" : ""}
                        </div>
                        <div style={{ color: T.textD, fontSize: 6, marginTop: 1 }}>
                          {elasticityOrig != null && elasticityElastic != null
                            ? `gate ${elasticityOrig.toFixed(2)}→${elasticityElastic.toFixed(2)}`
                            : `Z=${elasticityZ.toFixed(2)}`}
                          {elasticityTimePct != null ? ` · ${(elasticityTimePct * 100).toFixed(0)}% ciclo` : ""}
                        </div>
                      </div>
                      <div style={{ color: T.violet, fontSize: 8, fontWeight: 800 }}>
                        Z+{elasticityZ.toFixed(2)}
                      </div>
                    </div>
                  )}
                  {/* ── DYNAMIC ANCHOR info ────────────────────────────────── */}
                  {fvgLookbackUsed != null && fvgLookbackUsed > 200 && (
                    <div style={{
                      background: T.mute + "0a", border: `1px solid ${T.border}`,
                      borderRadius: 4, padding: "2px 7px", marginBottom: 4,
                    }}>
                      <div style={{ color: T.mute, fontSize: 6 }}>
                        {`FVG ventana: ${fvgLookbackUsed}t (dinámico)`}
                        {scarcityElapsedS != null ? ` · ${Math.round(scarcityElapsedS)}s sin spike` : ""}
                      </div>
                    </div>
                  )}

                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: isBlindZone || isNormalizingZone ? 3 : 4 }}>
                    <span style={{ fontSize: 7, color: headerColor, letterSpacing: "0.08em", fontWeight: 600 }}>
                      CALIDAD DE ENTRADA
                    </span>
                    {ageLabel && (
                      <span style={{ fontSize: 6, color: headerColor, opacity: 0.8 }}>
                        {isStale ? "⚠ " : ""}{ageLabel}
                      </span>
                    )}
                  </div>


                  {/* ── Burst active banner ─────────────────────────────────── */}
                  {burstActive && (
                    <div style={{ background: "#0ff3" , border: "1px solid #0ff6",
                      borderRadius: 4, padding: "3px 6px", marginBottom: 4 }}>
                      <div style={{ color: "#0ff", fontSize: 7, fontWeight: 700, letterSpacing: "0.06em" }}>
                        BURST ACTIVO
                        {burstDepth != null ? ` · ${burstDepth}spikes` : ""}
                        {burstRetroceso != null ? ` · RETR ${(burstRetroceso * 100).toFixed(0)}%` : ""}
                      </div>
                      <div style={{ color: T.mute, fontSize: 6, marginTop: 1 }}>
                        {burstRetroceso != null && burstRetroceso > 0.35
                          ? "Momentum cancelado — esperar reset"
                          : "Momentum de ráfaga — R<35% = válido para entrada rápida"}
                      </div>
                    </div>
                  )}

                  {/* ── MASTER KEY bypass banner ─────────────────────────── */}
                  {masterKeyBypass && (
                    <div style={{
                      background: T.violet + "22",
                      border: `2px solid ${T.violet}88`,
                      borderRadius: 7, padding: "7px 10px", marginBottom: 6,
                      boxShadow: `0 0 10px ${T.violet}22`,
                    }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800,
                            color: T.violet, letterSpacing: "0.12em" }}>
                            MASTER KEY ACTIVO
                          </div>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.textD, marginTop: 2 }}>
                            {masterKeyBypass.replace(/_GATE$/,"").replace(/_/g," ")} bypassed
                            {masterKeyRng != null ? ` · rng=${masterKeyRng}/100` : ""}
                          </div>
                        </div>
                        <div style={{ textAlign: "right", flexShrink: 0 }}>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>SCORE</div>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800,
                            color: T.violet, lineHeight: 1 }}>
                            {scoreRaw?.toFixed(2) ?? "–"}
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* ── RNG Probability — métrica principal ───────────────── */}
                  {rngProb != null && (() => {
                    const pct = Math.min(100, rngProb);
                    const passed = rngProb >= rngThreshold;
                    const barCol = rngProb >= 80 ? T.green
                      : rngProb >= rngThreshold ? T.amber
                      : rngProb >= 45 ? T.orange : T.red;
                    return (
                      <div style={{
                        background: barCol + "18",
                        border: `2px solid ${barCol}${passed ? "99" : "44"}`,
                        borderRadius: 7, padding: "8px 10px", marginBottom: 6,
                        opacity: chipOpacity,
                        boxShadow: passed ? `0 0 10px ${barCol}22` : "none",
                      }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <div style={{ flex: 1 }}>
                            <div style={{ fontSize: 7, color: T.mute, fontWeight: 700, letterSpacing: "0.10em", marginBottom: 3 }}>
                              PROBABILIDAD RNG
                            </div>
                            <div style={{ height: 6, borderRadius: 4, background: T.bg, overflow: "hidden", marginBottom: 3 }}>
                              <div style={{
                                height: "100%", borderRadius: 4, width: `${pct}%`,
                                background: barCol, transition: "width 0.5s ease",
                              }} />
                            </div>
                            {rngMissing.length > 0 && (
                              <div style={{ fontSize: 6, color: T.mute }}>Falta: {rngMissing.join(" · ")}</div>
                            )}
                          </div>
                          <div style={{ textAlign: "right", flexShrink: 0 }}>
                            <div style={{ fontFamily: FONT_MONO, fontSize: 30, fontWeight: 800, color: barCol, lineHeight: 1 }}>
                              {rngProb}
                            </div>
                            <div style={{ fontSize: 8, color: passed ? barCol : T.mute, fontWeight: 700, letterSpacing: "0.08em" }}>
                              {passed ? `✓ ≥${rngThreshold}` : `✗ <${rngThreshold}`}
                            </div>
                          </div>
                        </div>
                      </div>
                    );
                  })()}

                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {setupType && chip("SETUP", setupLabel, setupColor)}
                    {grade && chip("GRADE", grade, gradeColor)}
                    {scarcity && chip("SCAR", scarcity, scarColor)}
                    {scoreRaw != null && chip("SCORE", scoreRaw.toFixed(2), scoreRawColor)}
                    {masterKeyBypass && chip("MK", masterKeyBypass.replace(/_GATE$/,"").replace(/SCARCITY_/,"").replace(/_/g,"-"), T.violet)}
                  </div>
                </div>
              );
            })()}
          </div>
        )}

        {/* D.6.5 Post-Racha Cooldown */}
        <CooldownBar cooldown={s.postRachaCooldown} />

        {/* ENTRADA DIEGO — segunda línea autónoma */}
        <EntradaDiegoSection symbol={s.symbol} />

        {/* Hurst compacto al pie (info de fondo, no decisor) */}
        {hurst != null && (
          <div style={{ padding: "4px 12px 6px", borderTop: `1px solid ${T.border}` }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>
              Hurst <b style={{ color: hurstColor }}>{num(hurst, 3)}</b>
              <span style={{ opacity: 0.6 }}> · {hurstLabel}</span>
              {live?.volRegime && <span style={{ opacity: 0.6 }}> · vol {live.volRegime}</span>}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── main ────────────────────────────────────────────────── */
export default function DerivOperatorConsole() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [paused, setPaused] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [symFilter, setSymFilter] = useState("ALL");
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/deriv/operator", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setData(j);
      setErr(null);
      setLastUpdate(Date.now());
    } catch (e) {
      setErr(e?.message || "error");
    }
  }, []);

  useEffect(() => {
    load();
    if (paused) return;
    timer.current = setInterval(load, 3000);
    return () => clearInterval(timer.current);
  }, [load, paused]);

  const SYMBOL_ORDER = ["CRASH500", "BOOM500", "CRASH600", "BOOM600", "CRASH900", "BOOM900", "BOOM1000", "CRASH1000"];
  const symbols = (data?.symbols || [])
    .filter((s) => SYMBOL_ORDER.includes(s.symbol))
    .slice()
    .sort((a, b) => {
      const ai = SYMBOL_ORDER.indexOf(a.symbol);
      const bi = SYMBOL_ORDER.indexOf(b.symbol);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
  const confFeed = data?.confirmationFeed || [];
  const spikeTable = data?.spikeTable || [];
  const sess = data?.session || {};
  const totals = data?.totals || {};
  const symNames = symbols.map((s) => s.symbol);

  const [ghostData, setGhostData] = useState(null);
  useEffect(() => {
    const loadGhost = async () => {
      try {
        const r = await fetch("/api/deriv/analytics/ghost-trades?hours=24", { cache: "no-store" });
        if (r.ok) setGhostData(await r.json());
      } catch {}
    };
    loadGhost();
    const id = setInterval(loadGhost, 30_000);
    return () => clearInterval(id);
  }, []);

  const tableRows = symFilter === "ALL" ? spikeTable : spikeTable.filter((s) => s.symbol === symFilter);
  const feedRows = symFilter === "ALL" ? confFeed : confFeed.filter((s) => s.symbol === symFilter);

  // Símbolos disponibles para filtro: unión de todos los que aparecen en datos reales
  const _SYM_PREF = ["CRASH500", "BOOM500", "CRASH600", "BOOM600", "CRASH900", "BOOM900", "CRASH1000", "BOOM1000"];
  const _spkSyms  = [...new Set(spikeTable.map(r => r.symbol))];
  const _feedSyms = [...new Set(confFeed.map(r => r.symbol))];
  const _allSyms  = [...new Set([..._SYM_PREF, ..._spkSyms, ..._feedSyms])].filter(Boolean);
  const allFilterSyms = _allSyms.sort((a, b) => {
    const ai = _SYM_PREF.indexOf(a), bi = _SYM_PREF.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  const selectedSymbol = symFilter === "ALL" ? (symNames[0] || "CRASH500") : symFilter;

  return (
    <div style={{ minHeight: "100vh", background: T.bg, color: T.text, fontFamily: FONT_MONO, padding: "16px 18px" }}>
      <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}`}</style>

      {/* HEADER */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        marginBottom: 14, flexWrap: "wrap", gap: 12,
      }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 800, letterSpacing: "0.04em", color: T.text }}>
            CONSOLA DE OPERACIÓN MANUAL <span style={{ color: T.cyan }}>· CRASH</span>
          </h1>
          <p style={{ margin: "3px 0 0", fontSize: 10.5, color: T.mute }}>
            Confirmaciones, scores y spikes en vivo. Tú operas. {" "}
            {lastUpdate && <span style={{ color: T.green }}>● en vivo · {fmtClock(lastUpdate / 1000)}</span>}
            {err && <span style={{ color: T.red }}> ● {err}</span>}
          </p>
        </div>
        <div style={{ display: "flex", gap: 18, alignItems: "center" }}>
          <Stat label="Sesión PnL" value={`${sess.realizedPnl >= 0 ? "+" : ""}${num(sess.realizedPnl)}`} color={pnlColor(sess.realizedPnl)} />
          <Stat label="Win%" value={sess.winRate == null ? "–" : `${Math.round(sess.winRate * 100)}%`} color={T.cyan} />
          <Stat label="Abiertas" value={totals.openPositions ?? "–"} color={T.amber} />
          <Stat label="Spikes 24h" value={totals.spikes24h ?? "–"} color={T.textD} />
          <button onClick={() => setPaused((p) => !p)} style={{
            background: (paused ? T.amber : T.green) + "14", color: paused ? T.amber : T.green,
            border: `1px solid ${(paused ? T.amber : T.green)}44`, borderRadius: 6,
            padding: "7px 12px", cursor: "pointer", fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800,
            letterSpacing: "0.08em",
          }}>{paused ? "▶ REANUDAR" : "❚❚ PAUSAR"}</button>
          <a href="/deriv" style={{
            background: T.cyan + "14", color: T.cyan, border: `1px solid ${T.cyan}44`,
            borderRadius: 6, padding: "7px 12px", textDecoration: "none",
            fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800, letterSpacing: "0.08em",
          }}>← TERMINAL</a>
        </div>
      </div>

      {!data && !err && (
        <div style={{ textAlign: "center", padding: 60, color: T.mute }}>Cargando señales…</div>
      )}

      {/* SYMBOL GRID */}
      <div style={{
        display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
        gap: 12, marginBottom: 14,
      }}>
        {symbols.map((s) => <SymbolCard key={s.symbol} s={s} />)}
        <R75Card />
      </div>

      {/* CONFIRMATION FEED */}
      <div style={{ marginBottom: 14 }}>
        <Panel title="Confirmaciones de posibles spikes · en vivo" accent={T.amber}
          right={
            <div style={{ display: "flex", gap: 5 }}>
              {["ALL", ...allFilterSyms].map((sn) => (
                <button key={sn} onClick={() => setSymFilter(sn)} style={{
                  background: symFilter === sn ? T.amber + "22" : "transparent",
                  color: symFilter === sn ? T.amber : T.mute,
                  border: `1px solid ${symFilter === sn ? T.amber + "55" : T.border}`,
                  borderRadius: 4, padding: "3px 8px", cursor: "pointer",
                  fontFamily: FONT_MONO, fontSize: 9, fontWeight: 700,
                }}>{sn}</button>
              ))}
            </div>
          }>
          <div style={{ maxHeight: 240, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 }}>
            {feedRows.length === 0 && <div style={{ color: T.mute, textAlign: "center", padding: 18 }}>Sin confirmaciones recientes</div>}
            {feedRows.map((f, i) => {
              const k = KIND[f.kind] || KIND.INFO;
              return (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 10, padding: "6px 9px",
                  background: T.bg2, borderRadius: 6, borderLeft: `3px solid ${k.color}`,
                }}>
                  <span style={{ fontSize: 10, color: T.mute, width: 64 }}>{fmtClock(f.ts)}</span>
                  <span style={{ fontSize: 11, fontWeight: 800, color: T.text, width: 78 }}>{f.symbol}</span>
                  <span style={{ fontSize: 9, fontWeight: 800, color: k.color, background: k.color + "16", borderRadius: 4, padding: "2px 7px", width: 92, textAlign: "center" }}>{k.label}</span>
                  <span style={{ fontSize: 11, color: T.text, width: 96 }}>score <b style={{ color: f.gap != null && f.gap <= 0 ? T.green : T.amber }}>{num(f.score)}</b>{f.gate ? <span style={{ color: T.mute }}> /{num(f.gate)}</span> : null}</span>
                  <span style={{ fontSize: 10, color: T.textD, width: 110 }}>
                    {f.gap == null ? "" : f.gap <= 0 ? "✓ supera gate" : `falta +${num(f.gap)}`}
                  </span>
                  <span style={{ fontSize: 10, color: sideColor(f.side), width: 64 }}>{sideLabel(f.side)}</span>
                  <span style={{ fontSize: 10, color: T.mute }}>{f.regime || ""}</span>
                  <span style={{ fontSize: 9.5, color: T.mute, marginLeft: "auto" }}>hace {ago(f.secsAgo)}</span>
                </div>
              );
            })}
          </div>
        </Panel>
      </div>

      {/* SPIKE TABLE — enriched */}
      <Panel title={`Eventos de spike · ${tableRows.length}`} accent={T.cyan}
        right={<span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>ratio · atr · nº en la hora · gap · ticks</span>}>
        <div style={{ maxHeight: 460, overflowY: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
            <thead style={{ position: "sticky", top: 0, background: T.panel }}>
              <tr style={{ color: T.mute, textAlign: "left" }}>
                {["Hora", "Símbolo", "Dir", "Ratio", "ATR", "Nº hora", "Gap previo", "Ticks", "Entró", "Hace"].map((h) => (
                  <th key={h} style={{ padding: "5px 8px", fontSize: 9, textTransform: "uppercase", whiteSpace: "nowrap" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tableRows.length === 0 && (
                <tr><td colSpan={10} style={{ color: T.mute, textAlign: "center", padding: 22 }}>Sin spikes</td></tr>
              )}
              {tableRows.map((s, i) => {
                const dc = s.direction === "DOWN" ? T.red : T.green;
                const entered = s.bot_entered === true;
                const ratioColor = s.ratio >= 500 ? T.red : s.ratio >= 100 ? T.amber : T.textD;
                return (
                  <tr key={i} style={{ borderBottom: `1px solid ${T.border}` }}>
                    <td style={{ padding: "5px 8px", color: T.textD, whiteSpace: "nowrap" }}>{fmtClock(s.ts)}</td>
                    <td style={{ padding: "5px 8px", fontWeight: 700, color: T.text }}>{s.symbol}</td>
                    <td style={{ padding: "5px 8px", color: dc, fontWeight: 700 }}>{s.direction === "DOWN" ? "▼ DOWN" : "▲ UP"}</td>
                    <td style={{ padding: "5px 8px", fontWeight: 800, color: ratioColor }}>{num(s.ratio, 0)}x</td>
                    <td style={{ padding: "5px 8px", color: T.textD }}>{num(s.atr, 4)}</td>
                    <td style={{ padding: "5px 8px" }}>
                      <span style={{ fontWeight: 800, color: s.seqInHour >= 5 ? T.orange : s.seqInHour >= 3 ? T.amber : T.cyan }}>{ord(s.seqInHour)}</span>
                      <span style={{ color: T.mute }}> /h</span>
                    </td>
                    <td style={{ padding: "5px 8px", color: T.textD }}>{s.gapPrevSec == null ? "–" : dur(s.gapPrevSec)}</td>
                    <td style={{ padding: "5px 8px", color: T.cyan, fontWeight: 700 }}>{intC(s.ticks_since_last_spike)}</td>
                    <td style={{ padding: "5px 8px" }}>
                      <span style={{
                        fontSize: 9, fontWeight: 800, padding: "2px 6px", borderRadius: 4,
                        color: entered ? T.green : T.red, background: (entered ? T.green : T.red) + "16",
                      }}>{entered ? "✓ SÍ" : "no"}</span>
                    </td>
                    <td style={{ padding: "5px 8px", color: T.mute, whiteSpace: "nowrap" }}>{ago(s.secsAgo)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      {/* ══ GHOST TRADES ══════════════════════════════════════════════════════ */}
      <Panel title="GHOST TRADES — GATES AUDITADOS" style={{ marginTop: 12 }}>
        {!ghostData ? (
          <div style={{ padding: "14px 16px", color: T.mute, fontSize: 11 }}>Cargando datos fantasma…</div>
        ) : ghostData.totals?.total === 0 ? (
          <div style={{ padding: "14px 16px", color: T.mute, fontSize: 11 }}>Sin trades fantasma en las últimas 24h.</div>
        ) : (
          <div style={{ padding: "12px 16px" }}>
            {/* Summary row */}
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
              {[
                { label: "TOTAL",   val: ghostData.totals?.total,   color: T.text },
                { label: "WIN",     val: ghostData.totals?.WIN,     color: T.green },
                { label: "LOSS",    val: ghostData.totals?.LOSS,    color: T.red },
                { label: "EXPIRED", val: ghostData.totals?.EXPIRED, color: T.amber },
                { label: "WR",      val: ghostData.totals?.win_rate != null ? `${(ghostData.totals.win_rate * 100).toFixed(0)}%` : "–", color: ghostData.totals?.win_rate >= 0.5 ? T.green : T.red },
                { label: "PnL EST", val: ghostData.totals?.net_pnl != null ? `${ghostData.totals.net_pnl >= 0 ? "+" : ""}${ghostData.totals.net_pnl.toFixed(2)}` : "–", color: (ghostData.totals?.net_pnl ?? 0) >= 0 ? T.green : T.red },
                { label: "PF",      val: ghostData.totals?.profit_factor != null ? (ghostData.totals.profit_factor === 999 ? "∞" : ghostData.totals.profit_factor.toFixed(2)) : "–", color: (ghostData.totals?.profit_factor ?? 0) >= 1.5 ? T.green : (ghostData.totals?.profit_factor ?? 0) >= 1.0 ? T.amber : T.red },
              ].map(({ label, val, color }) => (
                <div key={label} style={{ textAlign: "center", minWidth: 52 }}>
                  <div style={{ fontSize: 17, fontWeight: 800, color }}>{val ?? "–"}</div>
                  <div style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em" }}>{label}</div>
                </div>
              ))}
            </div>

            {/* Per-gate table */}
            <div style={{ overflowX: "auto", marginBottom: 12 }}>
              <div style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em", marginBottom: 4 }}>POR GATE</div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                    {["GATE", "TOTAL", "WIN", "LOSS", "EXP", "WR", "PnL EST", "PF", "AVG WIN", "AVG LOSS"].map(h => (
                      <th key={h} style={{ padding: "4px 8px", textAlign: "left", color: T.mute, fontWeight: 700, letterSpacing: "0.06em", whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(ghostData.by_gate || {}).map(([gate, s]) => {
                    const wr = s.win_rate;
                    const wrColor = wr >= 0.55 ? T.green : wr >= 0.40 ? T.amber : T.red;
                    const pnlColor = (s.net_pnl ?? 0) >= 0 ? T.green : T.red;
                    const pfColor = (s.profit_factor ?? 0) >= 1.5 ? T.green : (s.profit_factor ?? 0) >= 1.0 ? T.amber : T.red;
                    return (
                      <tr key={gate} style={{ borderBottom: `1px solid ${T.border}22` }}>
                        <td style={{ padding: "5px 8px", color: T.cyan, fontWeight: 700, fontSize: 10, whiteSpace: "nowrap" }}>{gate.replace("_GATE","").replace(/_/g," ")}</td>
                        <td style={{ padding: "5px 8px", color: T.text, fontWeight: 800 }}>{s.total}</td>
                        <td style={{ padding: "5px 8px", color: T.green, fontWeight: 700 }}>{s.WIN}</td>
                        <td style={{ padding: "5px 8px", color: T.red, fontWeight: 700 }}>{s.LOSS}</td>
                        <td style={{ padding: "5px 8px", color: T.amber }}>{s.EXPIRED}</td>
                        <td style={{ padding: "5px 8px", fontWeight: 800, color: wrColor }}>{s.WIN + s.LOSS > 0 ? `${(wr * 100).toFixed(0)}%` : "–"}</td>
                        <td style={{ padding: "5px 8px", fontWeight: 800, color: pnlColor }}>{s.net_pnl != null ? `${s.net_pnl >= 0 ? "+" : ""}${s.net_pnl.toFixed(2)}` : "–"}</td>
                        <td style={{ padding: "5px 8px", fontWeight: 800, color: pfColor }}>{s.profit_factor != null ? (s.profit_factor === 999 ? "∞" : s.profit_factor.toFixed(2)) : "–"}</td>
                        <td style={{ padding: "5px 8px", color: T.green }}>{s.avg_win_pnl ? `+${s.avg_win_pnl.toFixed(2)}` : "–"}</td>
                        <td style={{ padding: "5px 8px", color: T.red }}>{s.avg_loss_pnl ? `-${s.avg_loss_pnl.toFixed(2)}` : "–"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Per-symbol table */}
            {ghostData.by_symbol && Object.keys(ghostData.by_symbol).length > 0 && (
              <div style={{ overflowX: "auto", marginBottom: 12 }}>
                <div style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em", marginBottom: 4 }}>POR SÍMBOLO</div>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                      {["SÍMBOLO", "TOTAL", "WIN", "LOSS", "EXP", "WR", "PnL EST", "PF"].map(h => (
                        <th key={h} style={{ padding: "4px 8px", textAlign: "left", color: T.mute, fontWeight: 700, letterSpacing: "0.06em" }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(ghostData.by_symbol).map(([sym, s]) => {
                      const wr = s.win_rate;
                      const wrColor = wr >= 0.55 ? T.green : wr >= 0.40 ? T.amber : T.red;
                      const pnlColor = (s.net_pnl ?? 0) >= 0 ? T.green : T.red;
                      const pfColor = (s.profit_factor ?? 0) >= 1.5 ? T.green : (s.profit_factor ?? 0) >= 1.0 ? T.amber : T.red;
                      return (
                        <tr key={sym} style={{ borderBottom: `1px solid ${T.border}22` }}>
                          <td style={{ padding: "5px 8px", color: T.text, fontWeight: 800 }}>{sym}</td>
                          <td style={{ padding: "5px 8px", color: T.text }}>{s.total}</td>
                          <td style={{ padding: "5px 8px", color: T.green, fontWeight: 700 }}>{s.WIN}</td>
                          <td style={{ padding: "5px 8px", color: T.red, fontWeight: 700 }}>{s.LOSS}</td>
                          <td style={{ padding: "5px 8px", color: T.amber }}>{s.EXPIRED}</td>
                          <td style={{ padding: "5px 8px", fontWeight: 800, color: wrColor }}>{s.WIN + s.LOSS > 0 ? `${(wr * 100).toFixed(0)}%` : "–"}</td>
                          <td style={{ padding: "5px 8px", fontWeight: 800, color: pnlColor }}>{s.net_pnl != null ? `${s.net_pnl >= 0 ? "+" : ""}${s.net_pnl.toFixed(2)}` : "–"}</td>
                          <td style={{ padding: "5px 8px", fontWeight: 800, color: pfColor }}>{s.profit_factor != null ? (s.profit_factor === 999 ? "∞" : s.profit_factor.toFixed(2)) : "–"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {/* Recent resolved */}
            {ghostData.recent?.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <div style={{ fontSize: 10, color: T.mute, letterSpacing: "0.08em", marginBottom: 6 }}>ÚLTIMOS RESUELTOS</div>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 10 }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                        {["SYM", "GATE", "SCORE", "GRADE", "OUTCOME", "PnL EST", "HACE"].map(h => (
                          <th key={h} style={{ padding: "3px 8px", textAlign: "left", color: T.mute, fontWeight: 700 }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {ghostData.recent.map((r) => {
                        const oc = r.outcome;
                        const ocColor = oc === "GHOST_WIN" ? T.green : oc === "GHOST_LOSS" ? T.red : T.amber;
                        const pnl = r.estimated_pnl;
                        const pnlStr = pnl != null ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "–";
                        const pnlColor = pnl != null ? (pnl >= 0 ? T.green : T.red) : T.mute;
                        const secsAgo = r.outcome_ts ? Math.round(Date.now() / 1000 - r.outcome_ts) : null;
                        const agoStr = secsAgo == null ? "–" : secsAgo < 60 ? `${secsAgo}s` : secsAgo < 3600 ? `${Math.floor(secsAgo/60)}m` : `${Math.floor(secsAgo/3600)}h`;
                        return (
                          <tr key={r.id} style={{ borderBottom: `1px solid ${T.border}18` }}>
                            <td style={{ padding: "4px 8px", color: T.text, fontWeight: 700 }}>{r.symbol}</td>
                            <td style={{ padding: "4px 8px", color: T.cyan, fontSize: 9 }}>{(r.gate||"").replace("_GATE","").replace(/_/g," ")}</td>
                            <td style={{ padding: "4px 8px", color: T.amber, fontFamily: FONT_MONO }}>{r.score_raw?.toFixed(1) ?? "–"}</td>
                            <td style={{ padding: "4px 8px", color: T.text }}>{r.grade ?? "–"}</td>
                            <td style={{ padding: "4px 8px", fontWeight: 800, color: ocColor }}>{oc.replace("GHOST_","")}</td>
                            <td style={{ padding: "4px 8px", fontWeight: 700, color: pnlColor }}>{pnlStr}</td>
                            <td style={{ padding: "4px 8px", color: T.mute }}>{agoStr}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}
