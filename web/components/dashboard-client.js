"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import MicroGateRadar from "./MicroGateRadar";

const Plot = dynamic(() => import("react-plotly.js"), {
  ssr: false,
  loading: () => (
    <div style={{ height: 220, display: "flex", alignItems: "center", justifyContent: "center", color: "#6b8299", fontSize: 12 }}>
      Cargando gráfico…
    </div>
  ),
});

// ── Paleta de colores ──────────────────────────────────────────────
const G = "#12d98b"; // green  buy
const R = "#eb4b61"; // red    sell
const Y = "#f4b942"; // yellow warn
const B = "#57c1ff"; // blue   info
const P = "#a78bfa"; // purple scenario D
const BG   = "#080e16";
const CARD = "rgba(10,18,28,0.96)";
const BORD = "#1a2b3c";
const TEXT = "#dce7f5";
const MUTE = "#6b8299";

// ── Formatters ────────────────────────────────────────────────────
const n = (v, d = 2) => Number(v || 0).toFixed(d);
const pct = (v, d = 2) => `${(Number(v || 0) * 100).toFixed(d)}%`;
const tone = (v) => (Number(v || 0) >= 0 ? G : R);

function fmtDate(v) {
  if (!v) return "--";
  const d = new Date(v);
  if (isNaN(d)) return "--";
  return d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC" }) + " UTC";
}

// ── Primitivos de UI ──────────────────────────────────────────────

/** Badge de escenario A/B/C/D */
function ScenBadge({ sc, size = 22 }) {
  if (!sc) return <span style={{ color: MUTE, fontSize: 11 }}>–</span>;
  const col = sc === "A" ? G : sc === "B" ? B : sc === "C" ? Y : sc === "D" ? P : MUTE;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center",
      width: size, height: size, borderRadius: 6,
      background: `${col}22`, border: `1px solid ${col}55`,
      color: col, fontWeight: 700, fontSize: size * 0.55, flexShrink: 0,
    }}>{sc}</span>
  );
}

/** Badge de señal BUY / HOLD / SELL */
function SignalBadge({ signal, approved }) {
  const col = signal === "buy" && approved ? G : signal === "buy" ? Y : signal === "sell" ? R : MUTE;
  const label = signal === "buy" ? (approved ? "▲ BUY" : "▲ PENDING") : signal === "sell" ? "▼ SELL" : "● HOLD";
  return (
    <span style={{
      display: "inline-flex", gap: 5, alignItems: "center",
      padding: "4px 12px", borderRadius: 999,
      background: `${col}18`, border: `1px solid ${col}44`,
      color: col, fontWeight: 700, fontSize: 13, whiteSpace: "nowrap",
    }}>{label}</span>
  );
}

/** Barra de progreso horizontal */
function Bar({ value, max = 1, color = G, height = 6, label }) {
  const p = Math.max(0, Math.min(1, max > 0 ? value / max : 0)) * 100;
  return (
    <div>
      {label && <div style={{ fontSize: 10, color: MUTE, marginBottom: 2 }}>{label}</div>}
      <div style={{ background: "rgba(255,255,255,0.06)", borderRadius: 4, height, overflow: "hidden" }}>
        <div style={{ background: color, width: `${p}%`, height: "100%", transition: "width 0.4s ease" }} />
      </div>
    </div>
  );
}

/** Barra RSI con zonas de color */
function RsiBar({ value }) {
  const v = Number(value || 50);
  const col = v <= 36 ? B : v >= 70 ? R : v >= 52 ? Y : G;
  return (
    <div style={{ position: "relative", height: 16 }}>
      <div style={{ position: "absolute", left: 0,   width: "36%", height: "100%", background: "#57c1ff11" }} />
      <div style={{ position: "absolute", left: "36%", width: "34%", height: "100%", background: "#12d98b0a" }} />
      <div style={{ position: "absolute", left: "70%", width: "30%", height: "100%", background: "#eb4b6111" }} />
      <div style={{ position: "absolute", left: `${v}%`, top: 0, width: 3, height: "100%", background: col, transform: "translateX(-50%)", borderRadius: 2, transition: "left 0.4s ease" }} />
      <div style={{ position: "absolute", right: 0, top: 0, fontSize: 10, color: col, fontWeight: 700, lineHeight: "16px" }}>{n(v, 1)}</div>
    </div>
  );
}

/** Gauge de confianza IA (arco SVG semicircular) */
function ConfGauge({ value, approved }) {
  const p = Math.max(0, Math.min(1, Number(value || 0)));
  const col = approved && p >= 0.55 ? G : p >= 0.50 ? Y : R;
  const r = 42, cx = 60, cy = 56;
  const circ = Math.PI * r;
  const offset = circ * (1 - p);
  const pathD = `M ${cx - r},${cy} A ${r},${r} 0 0,1 ${cx + r},${cy}`;
  return (
    <svg width={120} height={72} viewBox="0 0 120 72" style={{ display: "block", margin: "0 auto" }}>
      <path d={pathD} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={10} strokeLinecap="round" />
      <path d={pathD} fill="none" stroke={col} strokeWidth={10} strokeLinecap="round"
        strokeDasharray={circ} strokeDashoffset={offset}
        style={{ transition: "stroke-dashoffset 0.6s ease, stroke 0.4s ease" }} />
      <text x={cx} y={cy - 4} textAnchor="middle" fill={col} fontSize={22} fontWeight={700} fontFamily="monospace">
        {Math.round(p * 100)}%
      </text>
      <text x={cx} y={cy + 10} textAnchor="middle" fill={MUTE} fontSize={9} fontFamily="sans-serif">CONFIDENCE IA</text>
    </svg>
  );
}

/** Sparkline SVG simple */
function Spark({ values = [], color = G, height = 32 }) {
  if (!values.length) return null;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const w = 200;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = height - ((v - min) / range) * (height - 4) - 2;
    return `${x},${y}`;
  }).join(" ");
  const lastY = height - ((values[values.length - 1] - min) / range) * (height - 4) - 2;
  const id = `sg${color.replace("#", "")}`;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} style={{ width: "100%", height }} preserveAspectRatio="none">
      <defs>
        <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.3} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <polygon points={`0,${height} ${pts} ${w},${height}`} fill={`url(#${id})`} />
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} />
      <circle cx={w} cy={lastY} r={3} fill={color} />
    </svg>
  );
}

// ── Card contenedor estándar ───────────────────────────────────────
function Card({ title, right, children, style = {} }) {
  return (
    <div style={{ background: CARD, border: `1px solid ${BORD}`, borderRadius: 16, padding: 20, display: "flex", flexDirection: "column", gap: 12, ...style }}>
      {(title || right) && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
          {title && <div style={{ color: MUTE, fontSize: 10, textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 600 }}>{title}</div>}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

// ── Trailing tier data (Strict Percentage-Based, sincronizado con main_loop.py) ─
// Estructura espejo exacta de _TRAILING_TIERS en el backend.
// trigger = MFE mínimo para activar | slAt = SL garantizado desde entry
const TIERS = [
  { tier: 1, trigger: 0.0050, slAt: 0.0020, label: "T1 · MFE +0.50% → SL +0.20%" },
  { tier: 2, trigger: 0.0080, slAt: 0.0040, label: "T2 · MFE +0.80% → SL +0.40%" },
  { tier: 3, trigger: 0.0120, slAt: 0.0080, label: "T3 · MFE +1.20% → SL +0.80%" },
  { tier: 4, trigger: 0.0160, slAt: 0.0120, label: "T4 · MFE +1.60% → SL +1.20%" },
];

// ── Entry Logic Tag config (Telemetry) ─────────────────────────────────────
const ENTRY_TAG_META = {
  standard_ai:  { label: "IA Standard",     color: "#12D98B", icon: "◈", title: "Gemini conf ≥ 55% — flujo normal" },
  bypass_ai:    { label: "Bypass IA",        color: "#F5A623", icon: "⚡", title: "lazy_gate / API error — override RSI≤30 + vol≥0.8" },
  bypass_macro: { label: "Mean Reversion",   color: "#E040FB", icon: "↩", title: "Macro bearish + RSI≤30 + vol≥1.2 + OB≥30%" },
};

function buildPos(openPosition, openPositions) {
  if (!openPosition) return null;
  const full = openPositions?.find((p) => p.symbol === openPosition.symbol) || openPosition;
  const entry = Number(full.entry_price || openPosition.entry_price || 0);
  const mark  = Number(full.mark_price  || openPosition.mark_price  || entry);
  const sl    = Number(full.stop_loss   || openPosition.stop_loss   || 0);
  const tp    = Number(full.take_profit || openPosition.take_profit || 0);
  const mfe   = Number(full.mfe_pct     || openPosition.mfe_pct     || 0);
  const mae   = Number(full.mae_pct     || openPosition.mae_pct     || 0);
  const pnl   = Number(full.unrealized_pnl_usdt || openPosition.unrealized_pnl_usdt || 0);
  const pnlP  = entry > 0 ? (mark - entry) / entry : 0;
  const tier  = Number(full.trailing_tier || openPosition.trailing_tier || 0);
  // Telemetry Tag: qué lógica autorizó la entrada (standard_ai | bypass_ai | bypass_macro)
  const entryTag = full.entry_logic_tag || openPosition.entry_logic_tag || "standard_ai";
  return {
    symbol: full.symbol, side: full.side, scenario: full.scenario,
    entry, mark, sl, tp, mfe, mae, pnl, pnlP, tier, entryTag,
    holdM: Number(full.hold_minutes || 0),
  };
}

// ── Panel: Posición viva ──────────────────────────────────────────
function PositionPanel({ openPosition, openPositions }) {
  const pos = buildPos(openPosition, openPositions);
  const [closeState, setCloseState] = useState("idle"); // idle | confirming | sending | done

  if (!pos) {
    return (
      <Card title="Posición">
        <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8, padding: "28px 0", opacity: 0.35 }}>
          <div style={{ fontSize: 36 }}>◌</div>
          <div style={{ fontSize: 12, color: MUTE }}>Sin posición abierta</div>
          <div style={{ fontSize: 11, color: MUTE }}>Escaneando mercado…</div>
        </div>
      </Card>
    );
  }

  const handleRotateClick = () => setCloseState("confirming");
  const handleConfirm = async () => {
    setCloseState("sending");
    try {
      const res = await fetch("/api/manual-close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: pos.symbol }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCloseState("done");
    } catch (err) {
      console.error("Manual close error:", err);
      setCloseState("idle");
    }
  };
  const handleCancel = () => setCloseState("idle");

  const pnlCol = pos.pnl >= 0 ? G : R;
  const slRisk  = pos.sl > 0 && pos.entry > 0 ? Math.abs(pos.sl - pos.entry) / pos.entry : 0;
  const tpRange = pos.tp > 0 && pos.entry > 0 ? Math.abs(pos.tp - pos.entry) / pos.entry : 0;
  return (
    <Card title="Posición activa" right={<ScenBadge sc={pos.scenario} />}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span style={{ fontSize: 20, fontWeight: 700 }}>{pos.symbol}</span>
        <span style={{ fontSize: 11, background: "rgba(18,217,139,0.15)", color: G, borderRadius: 4, padding: "2px 6px", fontWeight: 700 }}>LONG</span>
        <span style={{ fontSize: 11, color: MUTE }}>{Math.round(pos.holdM)}min</span>
      </div>

      {/* ── Entry Logic Tag Badge (Telemetry) ─────────────────────────────── */}
      {(() => {
        const meta = ENTRY_TAG_META[pos.entryTag] || ENTRY_TAG_META.standard_ai;
        const isOverride = pos.entryTag !== "standard_ai";
        return (
          <div title={meta.title} style={{
            display: "inline-flex", alignItems: "center", gap: 5,
            background: `${meta.color}18`,
            border: `1px solid ${meta.color}44`,
            borderRadius: 6, padding: "4px 8px",
            fontSize: 10, color: meta.color, fontWeight: 700,
            letterSpacing: "0.03em",
          }}>
            <span style={{ fontSize: 12 }}>{meta.icon}</span>
            {meta.label}
            {isOverride && <span style={{ fontSize: 9, opacity: 0.7, marginLeft: 2 }}>OVERRIDE</span>}
          </div>
        );
      })()}

      {/* Banner: posición recuperada del exchange, datos de entrada son estimados */}
      {pos.scenario === "recovered_live" && (
        <div style={{ background: `${Y}10`, border: `1px solid ${Y}44`, borderRadius: 8, padding: "8px 12px", display: "flex", gap: 8, alignItems: "flex-start" }}>
          <span style={{ fontSize: 14, flexShrink: 0 }}>⚠</span>
          <div>
            <div style={{ fontSize: 11, color: Y, fontWeight: 700, marginBottom: 2 }}>Posición recuperada del exchange</div>
            <div style={{ fontSize: 10, color: MUTE }}>El bot se reinició sin estado previo. Precio de entrada y SL/TP son estimados. Verifica en Binance antes de operar.</div>
          </div>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div>
          <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>Entrada</div>
          <div style={{ fontSize: 17, fontWeight: 700, fontFamily: "monospace" }}>{n(pos.entry, 4)}</div>
        </div>
        <div>
          <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>Mark</div>
          <div style={{ fontSize: 17, fontWeight: 700, fontFamily: "monospace", color: pos.mark >= pos.entry ? G : R }}>{n(pos.mark, 4)}</div>
        </div>
      </div>

      <div style={{ background: `${pnlCol}10`, borderRadius: 10, padding: "10px 14px", border: `1px solid ${pnlCol}25` }}>
        <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>P&L Flotante</div>
        <div style={{ fontSize: 22, fontWeight: 700, color: pnlCol, fontFamily: "monospace" }}>
          {pos.pnl >= 0 ? "+" : ""}{n(pos.pnl, 4)} USDT
        </div>
        <div style={{ fontSize: 12, color: pnlCol, opacity: 0.8 }}>{pos.pnlP >= 0 ? "+" : ""}{pct(pos.pnlP)}</div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div>
          <div style={{ fontSize: 9, color: R, textTransform: "uppercase" }}>Stop Loss</div>
          <div style={{ fontSize: 13, fontFamily: "monospace", color: R }}>{n(pos.sl, 4)}</div>
          <div style={{ fontSize: 10, color: MUTE }}>−{pct(slRisk)}</div>
        </div>
        <div>
          <div style={{ fontSize: 9, color: G, textTransform: "uppercase" }}>Take Profit</div>
          <div style={{ fontSize: 13, fontFamily: "monospace", color: G }}>{n(pos.tp, 4)}</div>
          <div style={{ fontSize: 10, color: MUTE }}>+{pct(tpRange)}</div>
        </div>
      </div>

      <div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE, marginBottom: 3 }}>
          <span>MFE {pct(pos.mfe)}</span><span>MAE {pct(pos.mae)}</span>
        </div>
        <Bar value={pos.mfe} max={Math.max(pos.mfe || 0.001, tpRange || 0.02)} color={G} height={5} />
      </div>

      <div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE, marginBottom: 5 }}>
          <span>Trailing Tier {pos.tier > 0 ? pos.tier : "–"}</span>
          {(() => {
            const activeTier = TIERS.find(t => t.tier === pos.tier);
            const nextTier   = TIERS.find(t => t.tier === pos.tier + 1);
            if (pos.tier === 0) {
              const needed = TIERS[0].trigger - pos.mfe;
              return <span style={{ color: MUTE }}>esperando +0.50% (faltan {pct(Math.max(0, needed))})</span>;
            }
            if (activeTier && nextTier) {
              const needed = nextTier.trigger - pos.mfe;
              return (
                <span style={{ color: G }}>
                  ✓ SL en +{pct(activeTier.slAt)}
                  {needed > 0 && <span style={{ color: MUTE }}> · T{nextTier.tier} en {pct(Math.max(0, needed))}</span>}
                </span>
              );
            }
            return <span style={{ color: G }}>✓ SL en +{pct(activeTier?.slAt ?? 0)} · máx protección</span>;
          })()}
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {TIERS.map((t) => (
            <div key={t.tier} style={{
              flex: 1, height: 6, borderRadius: 4,
              background: pos.tier >= t.tier ? (t.tier === 1 ? Y : G) : "rgba(255,255,255,0.07)",
              transition: "background 0.3s ease",
            }} title={t.label} />
          ))}
        </div>
        {pos.tier > 0 && (
          <div style={{ fontSize: 9, color: MUTE, marginTop: 4, textAlign: "right" }}>
            SL bloqueado en entry {pos.tier > 0 ? `+${pct(TIERS[pos.tier - 1]?.slAt ?? 0)}` : "−"}
          </div>
        )}
      </div>

      {/* ── Botón Rotar Capital ─────────────────────────────────── */}
      {closeState === "idle" && (
        <button onClick={handleRotateClick} style={{
          width: "100%", padding: "9px 0", borderRadius: 8, border: `1px solid ${Y}44`,
          background: `${Y}12`, color: Y, fontWeight: 700, fontSize: 12,
          cursor: "pointer", letterSpacing: "0.04em", marginTop: 2,
        }}>
          ⟳ Rotar Capital (Cerrar)
        </button>
      )}
      {closeState === "confirming" && (
        <div style={{ border: `1px solid ${R}44`, borderRadius: 8, padding: "10px 12px", background: `${R}0a`, display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 12, color: TEXT, fontWeight: 600 }}>¿Cerrar {pos.symbol} ahora?</div>
          <div style={{ fontSize: 11, color: MUTE }}>El bot cerrará a mercado en el próximo ciclo (≤60s). Esta acción se registrará en el historial.</div>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={handleConfirm} style={{
              flex: 1, padding: "7px 0", borderRadius: 6, border: `1px solid ${R}55`,
              background: `${R}22`, color: R, fontWeight: 700, fontSize: 12, cursor: "pointer",
            }}>Confirmar cierre</button>
            <button onClick={handleCancel} style={{
              flex: 1, padding: "7px 0", borderRadius: 6, border: `1px solid ${BORD}`,
              background: "transparent", color: MUTE, fontWeight: 600, fontSize: 12, cursor: "pointer",
            }}>Cancelar</button>
          </div>
        </div>
      )}
      {closeState === "sending" && (
        <div style={{ textAlign: "center", fontSize: 12, color: Y, padding: "8px 0", opacity: 0.8 }}>
          Enviando orden al bot…
        </div>
      )}
      {closeState === "done" && (
        <div style={{ textAlign: "center", fontSize: 12, color: G, padding: "8px 0", border: `1px solid ${G}33`, borderRadius: 8, background: `${G}0a` }}>
          ✓ Orden enviada — el bot cerrará en el próximo ciclo
        </div>
      )}
    </Card>
  );
}

// ── Panel: Centro de decisión IA ──────────────────────────────────
function AiPanel({ ai, scan, decision }) {
  const conf     = Number(ai?.confidence || 0);
  const approved = Boolean(ai?.approved);
  const flags    = Array.isArray(ai?.risk_flags) ? ai.risk_flags : [];
  const setup    = ai?.setup_quality || "low";
  const setupCol = setup === "high" ? G : setup === "medium" ? Y : R;
  const scenario = scan?.scenario || decision?.scenario;
  const cached   = ai?.cached;
  return (
    <Card
      title="Motor IA"
      right={<span style={{ fontSize: 10, color: MUTE }}>{cached ? `cache ${Math.round(ai?.cached_age_seconds || 0)}s` : ai?.consulted ? "● fresca" : "○ no consultada"}</span>}
    >
      <ConfGauge value={conf} approved={approved} />

      <div style={{ display: "flex", justifyContent: "center", gap: 8, flexWrap: "wrap" }}>
        <SignalBadge signal={ai?.signal} approved={approved} />
        <ScenBadge sc={scenario} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 8, padding: "8px 10px" }}>
          <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>Setup</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: setupCol, textTransform: "uppercase" }}>{setup}</div>
        </div>
        <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 8, padding: "8px 10px" }}>
          <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>Aprobado</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: approved ? G : R }}>{approved ? "SÍ" : "NO"}</div>
        </div>
      </div>

      {ai?.direction_alignment && (
        <div style={{ fontSize: 12, color: ai.direction_alignment === "aligned" ? G : R }}>
          {ai.direction_alignment === "aligned" ? "↑ Alineado con técnica" : "↕ Desalineado con técnica"}
        </div>
      )}

      {flags.includes("technical_fallback_mode") && (
        <div style={{ fontSize: 10, color: Y, background: "rgba(245,158,11,0.10)", border: `1px solid rgba(245,158,11,0.30)`, borderRadius: 6, padding: "4px 8px", textAlign: "center" }}>
          ⚠ Modo fallback técnico — OpenRouter no disponible
        </div>
      )}

      {flags.length > 0 && (
        <div>
          <div style={{ fontSize: 9, color: MUTE, marginBottom: 4, textTransform: "uppercase" }}>Risk Flags</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {flags.map((f, i) => (
              <span key={i} style={{ fontSize: 10, background: "rgba(235,75,97,0.12)", border: `1px solid rgba(235,75,97,0.25)`, color: R, borderRadius: 4, padding: "2px 6px" }}>{f}</span>
            ))}
          </div>
        </div>
      )}

      {ai?.rationale && (
        <div style={{ fontSize: 11, color: MUTE, lineHeight: 1.5, borderTop: `1px solid ${BORD}`, paddingTop: 8 }}>
          {String(ai.rationale).slice(0, 200)}{String(ai.rationale).length > 200 ? "…" : ""}
        </div>
      )}

      <div style={{ fontSize: 10, color: MUTE, borderTop: `1px solid ${BORD}`, paddingTop: 8 }}>
        Modelo: {flags.includes("technical_fallback_mode") ? "⚠ fallback técnico (sin OpenRouter)" : ai?.model || "OpenRouter"}
      </div>
    </Card>
  );
}

// ── Panel: Pulso de mercado ───────────────────────────────────────
function MarketPanel({ scan, technicalSignal, targetSymbols, onSymbol }) {
  const ts   = scan || {};
  const rsi  = Number(ts.rsi  ?? technicalSignal?.rsi  ?? 50);
  const vol  = Number(ts.volume_ratio ?? technicalSignal?.volume_ratio ?? 0);
  const atr  = Number(ts.atr_pct ?? technicalSignal?.atr_pct ?? 0);
  const flow = Number(ts.trade_flow_score || 0);
  const ob   = Number(ts.orderbook_imbalance || 0.5);
  const spread = Number(ts.spread_pct || 0);
  const macro = ts.macro_regime || {};
  const macroTrend = String(ts.macro_trend || macro.trend || "neutral").toLowerCase();
  const macroCol = macroTrend === "bullish" ? G : macroTrend === "bearish" ? R : MUTE;

  return (
    <Card
      title="Pulso de mercado"
      right={
        <select onChange={(e) => onSymbol(e.target.value)} value={ts.symbol || ""}
          style={{ background: "rgba(255,255,255,0.05)", color: TEXT, border: `1px solid ${BORD}`, borderRadius: 6, padding: "2px 8px", fontSize: 11, cursor: "pointer" }}>
          {targetSymbols.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      }
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <span style={{ fontSize: 22, fontWeight: 700, fontFamily: "monospace" }}>{n(ts.close || technicalSignal?.close, 4)}</span>
        <ScenBadge sc={ts.scenario} />
        <span style={{ fontSize: 11, color: MUTE }}>{ts.regime || "–"}</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
            <span style={{ color: MUTE }}>RSI</span>
            <span style={{ color: rsi <= 36 ? B : rsi >= 70 ? R : rsi >= 52 ? Y : G, fontWeight: 600 }}>{n(rsi, 1)}</span>
          </div>
          <RsiBar value={rsi} />
        </div>

        <div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
            <span style={{ color: MUTE }}>Volumen relativo</span>
            <span style={{ color: vol >= 1.2 ? G : vol < 0.5 ? R : MUTE, fontWeight: 600 }}>{n(vol, 2)}x</span>
          </div>
          <Bar value={vol} max={3} color={vol >= 1.0 ? G : Y} height={6} />
        </div>

        <div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
            <span style={{ color: MUTE }}>ATR</span>
            <span style={{ fontWeight: 600 }}>{pct(atr)}</span>
          </div>
          <Bar value={atr} max={0.04} color={B} height={5} />
        </div>

        <div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
            <span style={{ color: MUTE }}>Trade Flow Score</span>
            <span style={{ color: flow >= 0.55 ? G : flow < 0.44 ? R : Y, fontWeight: 600 }}>{Math.round(flow * 100)}%</span>
          </div>
          <Bar value={flow} max={1} color={flow >= 0.55 ? G : flow < 0.44 ? R : Y} height={6} />
        </div>

        <div>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
            <span style={{ color: MUTE }}>Orderbook Imbalance</span>
            <span style={{ color: ob >= 0.55 ? G : ob < 0.45 ? R : MUTE, fontWeight: 600 }}>{Math.round(ob * 100)}%</span>
          </div>
          {/* Barra bipolar centrada en 50% */}
          <div style={{ position: "relative", height: 6, background: "rgba(255,255,255,0.06)", borderRadius: 4, overflow: "hidden" }}>
            <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "rgba(255,255,255,0.2)" }} />
            <div style={{
              position: "absolute",
              left:  ob >= 0.5 ? "50%" : `${ob * 100}%`,
              width: `${Math.abs(ob - 0.5) * 100}%`,
              height: "100%",
              background: ob >= 0.5 ? G : R,
              transition: "all 0.4s ease",
            }} />
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, paddingTop: 4 }}>
          <div>
            <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>Macro (15m)</div>
            <div style={{ fontSize: 13, fontWeight: 700, color: macroCol, textTransform: "uppercase" }}>{macroTrend}</div>
          </div>
          <div>
            <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase" }}>Spread</div>
            <div style={{ fontSize: 13, fontWeight: 700, color: spread > 0.002 ? R : MUTE }}>{spread > 0 ? pct(spread) : "--"}</div>
          </div>
        </div>
      </div>
    </Card>
  );
}

// ── Equity Curve ──────────────────────────────────────────────────
function EquityPanel({ equityHistory }) {
  const pts = Array.isArray(equityHistory) ? equityHistory.slice(-100) : [];
  const vals = pts.map((p) => Number(p.equity_usdt || 0));
  const hwms = pts.map((p) => Number(p.high_water_mark || 0));
  const lastVal = vals[vals.length - 1] || 0;
  const firstVal = vals[0] || lastVal;
  const lineCol = lastVal >= firstVal ? G : R;
  const maxHwm = hwms.length ? Math.max(...hwms.filter(Boolean)) : 0;

  const data = [
    {
      type: "scatter", mode: "lines", name: "Equity",
      x: pts.map((p) => p.timestamp), y: vals,
      line: { color: lineCol, width: 2 },
      fill: "tozeroy", fillcolor: `${lineCol}14`,
    },
    {
      type: "scatter", mode: "lines", name: "HWM",
      x: pts.map((p) => p.timestamp), y: hwms,
      line: { color: Y, width: 1.5, dash: "dot" },
    },
  ];
  const layout = {
    paper_bgcolor: "transparent", plot_bgcolor: "transparent",
    margin: { t: 10, r: 20, b: 30, l: 55 },
    showlegend: true,
    legend: { orientation: "h", x: 0, y: 1.12, font: { size: 10, color: MUTE } },
    xaxis: { color: MUTE, tickfont: { size: 9, color: MUTE }, gridcolor: "rgba(255,255,255,0.04)", zeroline: false },
    yaxis: { color: MUTE, tickfont: { size: 9, color: MUTE }, gridcolor: "rgba(255,255,255,0.04)", zeroline: false },
    font: { family: "IBM Plex Mono, monospace", color: TEXT },
    height: 190,
  };

  return (
    <Card
      title="Equity Curve"
      right={
        <div style={{ display: "flex", gap: 16, fontSize: 12 }}>
          <span style={{ color: MUTE }}>Actual: <strong style={{ color: lineCol, fontFamily: "monospace" }}>{n(lastVal)} USDT</strong></span>
          <span style={{ color: MUTE }}>HWM: <strong style={{ color: Y, fontFamily: "monospace" }}>{n(maxHwm)} USDT</strong></span>
        </div>
      }
    >
      {typeof window !== "undefined" && pts.length > 0 ? (
        <Plot data={data} layout={layout} config={{ displayModeBar: false, responsive: true }} style={{ width: "100%" }} />
      ) : (
        <div style={{ height: 190, display: "flex", alignItems: "center", justifyContent: "center", color: MUTE, fontSize: 12 }}>
          Sin datos de equity aún.
        </div>
      )}
    </Card>
  );
}

// ── Market Profiles · tarjeta por mercado con estrategia de entrada ──────
// Cada tarjeta muestra: símbolo · perfil · RSI vs umbrales (A/B) ·
// ATR vs rango · Volume vs piso · Orderbook vs piso · Flow vs piso ·
// IA confidence vs umbral · estado en vivo · escenario activo.
//
// Lee `state.last_scans[*].thresholds` (publicado por _build_scan_summary
// con apply_market_profile aplicado) y los valores live del mismo scan.
function MarketProfilesPanel({ lastScans, targetSymbols, focusSymbol, onSymbol }) {
  const symbols = (targetSymbols && targetSymbols.length)
    ? targetSymbols
    : lastScans.map((s) => s.symbol);
  const scanMap = Object.fromEntries(lastScans.map((s) => [s.symbol, s]));

  // Etiqueta de "carácter" del mercado — refleja la intención del perfil.
  const PROFILE_TAG = {
    "BTC/USDT":  { tag: "Oro Digital",        color: B,  desc: "Lento · institucional · convicción alta" },
    "ETH/USDT":  { tag: "Plata Líquida",      color: B,  desc: "Reactivo · líquido · gates moderados" },
    "SOL/USDT":  { tag: "Velocista",          color: G,  desc: "Explosivo · ATR alto · flow permisivo" },
    "BNB/USDT":  { tag: "Nativo Binance",     color: Y,  desc: "Vol moderada · ejecución limpia" },
    "DOGE/USDT": { tag: "Scalper Salvaje",    color: P,  desc: "Meme · momentum · vol irregular" },
    "WIF/USDT":  { tag: "Scalper Extremo",    color: R,  desc: "Small-cap · ATR enorme · gates laxos" },
  };

  return (
    <Card title={`Estrategias por Mercado · ${symbols.length} perfiles activos`}>
      <div style={{ fontSize: 11, color: MUTE, marginBottom: 4 }}>
        Cada mercado opera con su propio perfil de <b style={{ color: TEXT }}>entrada</b> (RSI, ATR, volumen, orderbook, flow, IA).
        SL/TP/Trailing son globales (T1 +0.5% → T4 +1.6%).
      </div>
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
        gap: 12,
      }}>
        {symbols.map((sym) => {
          const sc = scanMap[sym] || {};
          const th = sc.thresholds || {};
          const meta = PROFILE_TAG[sym] || { tag: "Default", color: MUTE, desc: "Sin perfil dedicado" };
          const isActive = sym === focusSymbol;
          const status = sc.status || "waiting";

          // Live values vs thresholds
          const rsi  = Number(sc.rsi || 0);
          const atr  = Number(sc.atr_pct || 0);
          const vol  = Number(sc.volume_ratio || 0);
          const ob   = Number(sc.orderbook_imbalance || 0);
          const flow = Number(sc.trade_flow_score || 0);
          const conf = Number(sc.ia_confidence || 0);
          const spread = Number(sc.spread_pct || 0);

          const rsiAMax = Number(th.scenario_a_rsi_max || 0);
          const rsiBMax = Number(th.scenario_b_rsi_max || 0);
          const atrMin  = Number(th.min_atr_pct || 0);
          const atrMax  = Number(th.max_atr_pct || 0);
          const volMin  = Number(th.min_volume_ratio || 0);
          const obMin   = Number(th.min_orderbook_imbalance || 0);
          const flowMin = Number(th.min_trade_flow_score || 0);
          const confMin = Number(th.ai_confidence_threshold || 0);
          const spreadMax = Number(th.max_spread_pct || 0);

          // Bar helper (live/limit) → percentage 0..100
          const ratio = (v, max) => {
            if (max <= 0) return 0;
            return Math.max(0, Math.min(100, (v / max) * 100));
          };

          // Status pill
          const statusColor =
            status === "candidate" ? G :
            status === "scenario_only" ? Y :
            status === "locked" ? B :
            MUTE;

          return (
            <div
              key={sym}
              onClick={() => onSymbol && onSymbol(sym)}
              style={{
                background: isActive ? "rgba(87,193,255,0.06)" : "rgba(255,255,255,0.015)",
                border: `1px solid ${isActive ? `${B}66` : BORD}`,
                borderRadius: 12,
                padding: 14,
                cursor: "pointer",
                display: "flex",
                flexDirection: "column",
                gap: 10,
                transition: "all 0.2s",
              }}
            >
              {/* Header: símbolo + perfil + estado */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 800, letterSpacing: "0.04em" }}>{sym}</div>
                  <div style={{ fontSize: 10, color: meta.color, fontWeight: 600, marginTop: 2 }}>{meta.tag}</div>
                </div>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
                  <span style={{
                    fontSize: 9, fontWeight: 700, letterSpacing: "0.08em",
                    padding: "3px 8px", borderRadius: 4,
                    background: status === "candidate" ? "rgba(18,217,139,0.16)" : "rgba(255,255,255,0.04)",
                    color: statusColor,
                    textTransform: "uppercase",
                  }}>{status}</span>
                  <ScenBadge sc={sc.scenario} size={20} />
                </div>
              </div>

              {/* Cadence badge: latencia/prioridad real del mercado */}
              {(() => {
                const cad = sc.cadence || {};
                const ttl = Number(cad.ai_cache_ttl_seconds || 0);
                const prio = Number(cad.priority || 5);
                const tag = String(cad.cadence_tag || "estandar");
                const tagColor = tag === "turbo" ? R : tag === "rapida" ? G : tag === "institucional" ? B : Y;
                const aiAge = Number(sc.ia_cached_age_seconds || 0);
                const aiCached = Boolean(sc.ia_cached);
                const ageColor = ttl > 0 && aiAge > ttl * 0.8 ? Y : MUTE;
                return (
                  <div style={{
                    display: "flex", alignItems: "center", gap: 6, fontSize: 9,
                    padding: "4px 8px",
                    background: "rgba(0,0,0,0.25)",
                    border: `1px solid ${BORD}`,
                    borderRadius: 6,
                    fontFamily: "monospace",
                  }}>
                    <span style={{ color: tagColor, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                      {tag}
                    </span>
                    <span style={{ color: MUTE }}>·</span>
                    <span style={{ color: TEXT }}>IA cada <b>{ttl || "—"}s</b></span>
                    <span style={{ color: MUTE }}>·</span>
                    <span style={{ color: MUTE }}>prio <b style={{ color: TEXT }}>{prio}</b></span>
                    {ttl > 0 && (
                      <>
                        <span style={{ color: MUTE }}>·</span>
                        <span style={{ color: ageColor }}>
                          {aiCached ? `cache ${Math.round(aiAge)}s` : "● fresh"}
                        </span>
                      </>
                    )}
                  </div>
                );
              })()}

              <div style={{ fontSize: 10, color: MUTE, fontStyle: "italic", lineHeight: 1.3 }}>
                {meta.desc}
              </div>

              {/* RSI vs umbrales */}
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE }}>
                  <span>RSI <b style={{ color: TEXT, fontFamily: "monospace" }}>{n(rsi, 1)}</b></span>
                  <span>A≤{n(rsiAMax, 0)} · B≤{n(rsiBMax, 0)}</span>
                </div>
                <div style={{ height: 6, background: "rgba(255,255,255,0.05)", borderRadius: 3, marginTop: 4, position: "relative" }}>
                  {/* Zone B (oversold target) */}
                  <div style={{ position: "absolute", left: 0, top: 0, height: "100%", width: `${rsiBMax}%`, background: "rgba(87,193,255,0.18)", borderRadius: "3px 0 0 3px" }} />
                  {/* Zone A (pullback target) */}
                  <div style={{ position: "absolute", left: `${rsiBMax}%`, top: 0, height: "100%", width: `${rsiAMax - rsiBMax}%`, background: "rgba(18,217,139,0.16)" }} />
                  {/* Marker */}
                  <div style={{ position: "absolute", left: `calc(${Math.min(100, rsi)}% - 1px)`, top: -2, width: 2, height: 10, background: rsi <= rsiBMax ? B : rsi <= rsiAMax ? G : rsi >= 70 ? R : Y }} />
                </div>
              </div>

              {/* ATR range */}
              <Gauge
                label="ATR%"
                live={atr * 100}
                min={atrMin * 100}
                max={atrMax * 100}
                unit="%"
                decimals={2}
                inRange={atr >= atrMin && atr <= atrMax}
              />

              {/* Volume ratio */}
              <Gauge
                label="Volume×"
                live={vol}
                min={volMin}
                max={2.0}
                unit=""
                decimals={2}
                inRange={vol >= volMin}
              />

              {/* Orderbook imbalance */}
              <Gauge
                label="OB imbalance"
                live={ob * 100}
                min={obMin * 100}
                max={100}
                unit="%"
                decimals={0}
                inRange={ob >= obMin}
              />

              {/* Trade flow score */}
              <Gauge
                label="Trade flow"
                live={flow * 100}
                min={flowMin * 100}
                max={100}
                unit="%"
                decimals={0}
                inRange={flow >= flowMin}
              />

              {/* IA confidence (only if consulted) */}
              <Gauge
                label="IA conf"
                live={conf * 100}
                min={confMin * 100}
                max={100}
                unit="%"
                decimals={0}
                inRange={conf >= confMin}
                muted={!sc.ia_consulted}
              />

              {/* Spread (lower-is-better; show as `live ≤ max`) */}
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE }}>
                <span>Spread <b style={{ color: spread > 0 && spread <= spreadMax ? G : spread > spreadMax ? R : MUTE, fontFamily: "monospace" }}>{(spread * 100).toFixed(3)}%</b></span>
                <span>≤{(spreadMax * 100).toFixed(3)}%</span>
              </div>

              {/* Reason */}
              <div style={{
                fontSize: 10, color: MUTE, marginTop: 4,
                paddingTop: 8, borderTop: `1px dashed ${BORD}`,
                lineHeight: 1.4,
              }}>
                {sc.candidate_reason || sc.rejection_reason || "Sin pre-señal — esperando setup"}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ── Gauge: live value vs [min..max] with in-range coloring ────────────────
function Gauge({ label, live, min, max, unit = "", decimals = 2, inRange = false, muted = false }) {
  const ratio = max > 0 ? Math.max(0, Math.min(100, (live / max) * 100)) : 0;
  const minRatio = max > 0 ? Math.max(0, Math.min(100, (min / max) * 100)) : 0;
  const liveColor = muted ? MUTE : inRange ? G : R;
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE }}>
        <span>{label} <b style={{ color: liveColor, fontFamily: "monospace" }}>{Number(live || 0).toFixed(decimals)}{unit}</b></span>
        <span style={{ opacity: 0.65 }}>≥{Number(min || 0).toFixed(decimals)}{unit}</span>
      </div>
      <div style={{ height: 5, background: "rgba(255,255,255,0.05)", borderRadius: 3, marginTop: 4, position: "relative" }}>
        {/* In-range zone (min..max) */}
        <div style={{ position: "absolute", left: `${minRatio}%`, top: 0, height: "100%", width: `${100 - minRatio}%`, background: "rgba(18,217,139,0.12)", borderRadius: "0 3px 3px 0" }} />
        {/* Live marker */}
        <div style={{ position: "absolute", left: `calc(${ratio}% - 1px)`, top: -1, width: 2, height: 7, background: liveColor }} />
      </div>
    </div>
  );
}

// ── Market Audit · estadísticas históricas por mercado/escenario ─────────
// Tabla pivote: símbolo × scenario → trades, win%, pnl, hold avg.
// Lee state.closed_trades (el bot persiste cada cierre con symbol+scenario).
function MarketAuditPanel({ closedTrades, targetSymbols, focusSymbol, onSymbol }) {
  const symbols = (targetSymbols && targetSymbols.length)
    ? targetSymbols
    : Array.from(new Set((closedTrades || []).map((t) => t.symbol).filter(Boolean)));

  // Agregar por symbol y por symbol+scenario.
  const bySym = {};
  for (const t of (closedTrades || [])) {
    const sym = t.symbol;
    if (!sym) continue;
    if (!bySym[sym]) {
      bySym[sym] = {
        symbol: sym, count: 0, wins: 0, losses: 0,
        pnlSum: 0, pnlPctSum: 0, holdSum: 0, fees: 0,
        best: -Infinity, worst: Infinity,
        scenarios: { A: 0, B: 0, C: 0, D: 0, other: 0 },
        winsByScenario: { A: 0, B: 0, C: 0, D: 0, other: 0 },
      };
    }
    const row = bySym[sym];
    const pnl = Number(t.pnl_usdt || 0);
    const pnlPct = Number(t.pnl_pct || 0);
    const fees = Number(t.fees_usdt || 0);
    const sc = t.scenario && ["A","B","C","D"].includes(t.scenario) ? t.scenario : "other";
    row.count += 1;
    if (pnl > 0) { row.wins += 1; row.winsByScenario[sc] += 1; }
    else if (pnl < 0) row.losses += 1;
    row.pnlSum += pnl;
    row.pnlPctSum += pnlPct;
    row.fees += fees;
    if (pnl > row.best) row.best = pnl;
    if (pnl < row.worst) row.worst = pnl;
    row.scenarios[sc] += 1;
    // Hold time
    if (t.opened_at && t.closed_at) {
      try {
        const op = new Date(t.opened_at).getTime();
        const cl = new Date(t.closed_at).getTime();
        if (op > 0 && cl > op) row.holdSum += (cl - op) / 60000;
      } catch (_) {}
    }
  }

  return (
    <Card title={`Auditoría por Mercado · ${symbols.length} mercados · ${(closedTrades||[]).length} trades cerrados`}>
      <div style={{ fontSize: 11, color: MUTE, marginBottom: 4 }}>
        Performance histórica por mercado. Win-rate por <b style={{ color: TEXT }}>escenario</b> permite
        identificar qué setup funciona mejor por moneda y refinar perfiles.
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ color: MUTE, fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              {["Símbolo", "Trades", "Win%", "PnL Σ", "PnL avg", "Hold avg", "Best", "Worst", "Esc A", "Esc B", "Esc C", "Esc D", "Fees"].map((h) => (
                <th key={h} style={{ textAlign: h === "Símbolo" ? "left" : "center", padding: "6px 8px", borderBottom: `1px solid ${BORD}`, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {symbols.map((sym) => {
              const r = bySym[sym] || {
                symbol: sym, count: 0, wins: 0, losses: 0,
                pnlSum: 0, pnlPctSum: 0, holdSum: 0, fees: 0,
                best: 0, worst: 0,
                scenarios: { A: 0, B: 0, C: 0, D: 0, other: 0 },
                winsByScenario: { A: 0, B: 0, C: 0, D: 0, other: 0 },
              };
              const winRate = r.count > 0 ? (r.wins / r.count) : 0;
              const avgPnl = r.count > 0 ? (r.pnlSum / r.count) : 0;
              const avgHold = r.count > 0 ? (r.holdSum / r.count) : 0;
              const isActive = sym === focusSymbol;
              const pnlCol = r.pnlSum > 0 ? G : r.pnlSum < 0 ? R : MUTE;
              const wrCol = r.count === 0 ? MUTE : winRate >= 0.5 ? G : winRate >= 0.4 ? Y : R;

              const cellSc = (sc) => {
                const total = r.scenarios[sc] || 0;
                if (total === 0) return <span style={{ color: MUTE }}>–</span>;
                const w = r.winsByScenario[sc] || 0;
                const wr = w / total;
                return (
                  <span style={{ fontFamily: "monospace", color: wr >= 0.5 ? G : wr >= 0.4 ? Y : R }}>
                    {total}<span style={{ color: MUTE, fontSize: 9 }}> ({Math.round(wr*100)}%)</span>
                  </span>
                );
              };

              return (
                <tr key={sym} onClick={() => onSymbol && onSymbol(sym)}
                  style={{ cursor: "pointer", background: isActive ? "rgba(87,193,255,0.06)" : "transparent", borderLeft: `3px solid ${isActive ? B : "transparent"}` }}>
                  <td style={{ padding: "8px 8px", fontWeight: 700 }}>{sym}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace" }}>{r.count}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: wrCol, fontWeight: 600 }}>
                    {r.count === 0 ? "–" : `${Math.round(winRate*100)}%`}
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: pnlCol, fontWeight: 700 }}>
                    {r.pnlSum >= 0 ? "+" : ""}{r.pnlSum.toFixed(3)}
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: avgPnl >= 0 ? G : R }}>
                    {r.count === 0 ? "–" : (avgPnl >= 0 ? "+" : "") + avgPnl.toFixed(3)}
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: MUTE }}>
                    {r.count === 0 ? "–" : `${avgHold.toFixed(0)}m`}
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: r.best > 0 ? G : MUTE }}>
                    {r.count === 0 ? "–" : (r.best >= 0 ? "+" : "") + r.best.toFixed(3)}
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: r.worst < 0 ? R : MUTE }}>
                    {r.count === 0 ? "–" : r.worst.toFixed(3)}
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}>{cellSc("A")}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}>{cellSc("B")}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}>{cellSc("C")}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}>{cellSc("D")}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: MUTE, fontSize: 11 }}>
                    {r.count === 0 ? "–" : r.fees.toFixed(3)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ── Scanner Matrix ────────────────────────────────────────────────
function ScannerMatrix({ lastScans, targetSymbols, focusSymbol, onSymbol }) {
  const symbols = targetSymbols.length ? targetSymbols : lastScans.map((s) => s.symbol);
  const scanMap = Object.fromEntries(lastScans.map((s) => [s.symbol, s]));

  return (
    <Card title={`Scanner · ${symbols.length} símbolos`}>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 4, fontSize: 10, color: MUTE }}>
        {["A", "B", "C", "D"].map((sc) => (
          <span key={sc} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <ScenBadge sc={sc} size={18} />
            {sc === "A" ? "Pullback" : sc === "B" ? "Sobreventa" : sc === "C" ? "Continuación" : "EMA Cross"}
          </span>
        ))}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ color: MUTE, fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              {["Símbolo", "Esc", "Estado", "RSI", "Vol×", "ATR%", "Flow", "OB%", "IA conf", "Motivo / Bloqueo"].map((h) => (
                <th key={h} style={{ textAlign: h === "Símbolo" || h === "Motivo / Bloqueo" ? "left" : "center", padding: "6px 8px", borderBottom: `1px solid ${BORD}`, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {symbols.map((sym) => {
              const sc  = scanMap[sym] || {};
              const rsi = Number(sc.rsi || 0);
              const vol = Number(sc.volume_ratio || 0);
              const atr = Number(sc.atr_pct || 0);
              const flow = Number(sc.trade_flow_score || 0);
              const ob  = Number(sc.orderbook_imbalance || 0);
              const conf = Number(sc.ia_confidence || 0);
              const isCandidate = sc.status === "candidate";
              const isActive    = sym === focusSymbol;
              const leftBol = isCandidate ? G : sc.scenario ? Y : "transparent";
              const rsiCol  = rsi <= 36 ? B : rsi >= 70 ? R : rsi >= 52 ? Y : G;

              return (
                <tr key={sym} onClick={() => onSymbol(sym)}
                  style={{ cursor: "pointer", background: isActive ? "rgba(87,193,255,0.06)" : "transparent", borderLeft: `3px solid ${leftBol}`, transition: "background 0.2s" }}>
                  <td style={{ padding: "8px 8px", fontWeight: 700 }}>{sym}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}><ScenBadge sc={sc.scenario} /></td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}>
                    <span style={{ fontSize: 10, borderRadius: 4, padding: "2px 6px", background: isCandidate ? "rgba(18,217,139,0.15)" : "rgba(255,255,255,0.05)", color: isCandidate ? G : MUTE }}>
                      {sc.status || "waiting"}
                    </span>
                  </td>
                  <td style={{ padding: "8px 8px", textAlign: "center", color: rsiCol, fontFamily: "monospace", fontWeight: 600 }}>{n(rsi, 1)}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", color: vol >= 1 ? G : vol < 0.5 ? R : MUTE, fontFamily: "monospace" }}>{n(vol, 2)}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: MUTE }}>{pct(atr)}</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: flow >= 0.55 ? G : flow < 0.44 ? R : Y }}>{Math.round(flow * 100)}%</td>
                  <td style={{ padding: "8px 8px", textAlign: "center", fontFamily: "monospace", color: ob >= 0.55 ? G : ob < 0.45 ? R : MUTE }}>{Math.round(ob * 100)}%</td>
                  <td style={{ padding: "8px 8px", textAlign: "center" }}>
                    {sc.ia_consulted ? (
                      <span style={{ color: conf >= 0.55 ? G : conf >= 0.45 ? Y : R, fontFamily: "monospace", fontWeight: 700 }}>{Math.round(conf * 100)}%</span>
                    ) : (
                      <span style={{ color: MUTE, fontSize: 10 }}>–</span>
                    )}
                  </td>
                  <td style={{ padding: "8px 8px", color: MUTE, fontSize: 11, maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {sc.rejection_reason || sc.candidate_reason || "–"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ── Historial de trades ───────────────────────────────────────────
function TradeHistoryPanel({ closedTrades }) {
  const trades = [...(closedTrades || [])].reverse().slice(0, 10);
  return (
    <Card title={`Historial · ${closedTrades?.length || 0} trades`}>
      {trades.length === 0 ? (
        <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "20px 0" }}>Sin trades cerrados aún.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          {trades.map((t, i) => {
            const pnl = Number(t.pnl_usdt || t.realized_pnl_usdt || 0);
            const pc  = pnl >= 0 ? G : R;
            // Telemetry Tag: muestra el badge de lógica de entrada en cada trade cerrado
            const tagMeta = ENTRY_TAG_META[t.entry_logic_tag] || ENTRY_TAG_META.standard_ai;
            return (
              <div key={i} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "7px 10px", borderRadius: 8, background: `${pc}0a`, border: `1px solid ${pc}20` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                  <ScenBadge sc={t.scenario} size={18} />
                  <span style={{ fontWeight: 600, fontSize: 12 }}>{t.symbol}</span>
                  <span style={{ fontSize: 10, color: MUTE }}>{t.exit_reason}</span>
                  {/* Entry Logic Tag badge en historial */}
                  <span title={tagMeta.title} style={{
                    fontSize: 9, color: tagMeta.color, fontWeight: 700,
                    background: `${tagMeta.color}18`, borderRadius: 4,
                    padding: "1px 5px", border: `1px solid ${tagMeta.color}33`,
                  }}>{tagMeta.icon} {tagMeta.label}</span>
                </div>
                <div style={{ textAlign: "right" }}>
                  <div style={{ color: pc, fontWeight: 700, fontFamily: "monospace", fontSize: 12 }}>{pnl >= 0 ? "+" : ""}{n(pnl, 4)} USDT</div>
                  <div style={{ fontSize: 10, color: MUTE }}>{fmtDate(t.exit_time || t.closed_at)}</div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ── Analytics ─────────────────────────────────────────────────────
function AnalyticsPanel({ portfolio, equityHistory, closedTrades }) {
  const trades  = closedTrades || [];
  const winners = trades.filter((t) => Number(t.pnl_usdt || 0) > 0);
  const losers  = trades.filter((t) => Number(t.pnl_usdt || 0) < 0);
  const avgWin  = winners.length ? winners.reduce((s, t) => s + Number(t.pnl_usdt || 0), 0) / winners.length : 0;
  const avgLoss = losers.length  ? Math.abs(losers.reduce((s, t) => s + Number(t.pnl_usdt || 0), 0) / losers.length) : 0;
  const rr      = avgLoss > 0 ? avgWin / avgLoss : 0;
  const winPct  = trades.length ? (winners.length / trades.length) * 100 : 0;
  const sparkVals = equityHistory.slice(-30).map((p) => Number(p.equity_usdt || 0));
  const pnlTotal  = Number(portfolio?.realized_pnl_usdt || 0);

  const r = 36, cx = 50, cy = 50;
  const circ = 2 * Math.PI * r;
  const dashOffset = circ * (1 - winPct / 100);

  return (
    <Card title="Analytics">
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 16, alignItems: "center" }}>
        <div>
          <svg width={100} height={100} viewBox="0 0 100 100">
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={8} />
            <circle cx={cx} cy={cy} r={r} fill="none" stroke={winPct >= 50 ? G : R} strokeWidth={8}
              strokeDasharray={circ} strokeDashoffset={dashOffset}
              strokeLinecap="round" transform="rotate(-90, 50, 50)"
              style={{ transition: "stroke-dashoffset 0.6s ease" }} />
            <text x={cx} y={cy} textAnchor="middle" dominantBaseline="middle" fill={TEXT} fontSize={16} fontWeight={700}>{Math.round(winPct)}%</text>
            <text x={cx} y={cy + 16} textAnchor="middle" fill={MUTE} fontSize={8}>WIN RATE</text>
          </svg>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 5 }}>
            <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 8, padding: "5px 8px" }}>
              <div style={{ fontSize: 9, color: MUTE }}>TOTAL</div>
              <div style={{ fontWeight: 700 }}>{trades.length}</div>
            </div>
            <div style={{ background: "rgba(255,255,255,0.03)", borderRadius: 8, padding: "5px 8px" }}>
              <div style={{ fontSize: 9, color: MUTE }}>R:R</div>
              <div style={{ fontWeight: 700, color: rr >= 1 ? G : R }}>{n(rr, 2)}</div>
            </div>
            <div style={{ background: "rgba(18,217,139,0.08)", borderRadius: 8, padding: "5px 8px" }}>
              <div style={{ fontSize: 9, color: MUTE }}>AVG WIN</div>
              <div style={{ fontWeight: 700, color: G, fontFamily: "monospace", fontSize: 11 }}>+{n(avgWin, 4)}</div>
            </div>
            <div style={{ background: "rgba(235,75,97,0.08)", borderRadius: 8, padding: "5px 8px" }}>
              <div style={{ fontSize: 9, color: MUTE }}>AVG LOSS</div>
              <div style={{ fontWeight: 700, color: R, fontFamily: "monospace", fontSize: 11 }}>−{n(avgLoss, 4)}</div>
            </div>
          </div>
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: MUTE, marginBottom: 3 }}>
              <span>PnL total</span>
              <span style={{ color: tone(pnlTotal), fontFamily: "monospace", fontWeight: 700 }}>
                {pnlTotal >= 0 ? "+" : ""}{n(pnlTotal)} USDT
              </span>
            </div>
            <Spark values={sparkVals} color={pnlTotal >= 0 ? G : R} height={30} />
          </div>
        </div>
      </div>
    </Card>
  );
}

// ── Timeline de señales ───────────────────────────────────────────
function SignalTimeline({ signalHistory }) {
  const items = [...(signalHistory || [])].slice(-8).reverse();
  return (
    <Card title="Timeline de señales">
      {items.length === 0 ? (
        <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "16px 0" }}>Sin historial de señales.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {items.map((item, i) => {
            const act = item.decision_action;
            const isBuy = act === "buy";
            const ac = isBuy ? G : act === "sell" ? R : MUTE;
            return (
              <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                <div style={{ width: 26, height: 26, borderRadius: 8, background: `${ac}18`, border: `1px solid ${ac}33`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, fontSize: 11, color: ac, fontWeight: 700 }}>
                  {isBuy ? "▲" : act === "sell" ? "▼" : "●"}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
                    <span style={{ fontSize: 12, fontWeight: 600 }}>{item.symbol || "–"}</span>
                    <span style={{ fontSize: 10, color: MUTE, flexShrink: 0 }}>{fmtDate(item.timestamp)}</span>
                  </div>
                  <div style={{ fontSize: 11, color: MUTE }}>
                    T:{item.technical_signal} · IA:{item.ai_signal} {Math.round((item.ai_confidence || 0) * 100)}%
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ── Guardrails detallados ─────────────────────────────────────────
function GuardrailsPanel({ guardrails, scan }) {
  const g = guardrails || {};
  const checks = [
    { label: "Señal ejecutable", ok: g.executable_signal },
    { label: "Escenario A",      ok: g.scenario_a },
    { label: "Escenario B",      ok: g.scenario_b },
    { label: "Escenario C",      ok: g.scenario_c },
    { label: "Escenario D",      ok: g.scenario_d },
    { label: "IA confianza",     ok: g.ai_confident,   sub: g.ai_confidence ? `${pct(g.ai_confidence)}` : undefined },
    { label: "IA aprobada",      ok: g.ai_approved },
    { label: "IA alineada",      ok: g.ai_alignment },
    { label: "Setup ready",      ok: g.ai_setup_ready },
    { label: "Risk clear",       ok: g.ai_risk_clear },
    { label: "Volatilidad",      ok: g.volatility_ready },
    { label: "Volumen",          ok: g.volume_ready },
    { label: "Micro spread",     ok: g.micro_ready },
    { label: "Flow score",       ok: g.flow_ready },
    { label: "Regime ready",     ok: g.regime_ready },
    { label: "Score ok",         ok: g.score_ready,    sub: g.setup_score != null ? `score ${n(g.effective_setup_score, 2)} / min ${n(g.regime_min_score, 2)}` : undefined },
    { label: "Cooldown libre",   ok: !g.cooldown_active },
  ];
  return (
    <Card title={`Guardrails · ${scan?.symbol || "–"}`} right={<span style={{ fontSize: 10, color: g.ai_gate_ready ? G : MUTE }}>{g.ai_gate_ready ? "✓ GATE ABIERTO" : "● observando"}</span>}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
        {checks.map(({ label, ok, sub }) => (
          <div key={label} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11 }}>
            <span style={{ color: ok ? G : "rgba(255,255,255,0.2)", fontSize: 13 }}>{ok ? "✓" : "○"}</span>
            <span style={{ color: ok ? TEXT : MUTE }}>{label}</span>
            {sub && <span style={{ color: MUTE, fontSize: 10 }}>({sub})</span>}
          </div>
        ))}
      </div>
    </Card>
  );
}

// ── Panel: Monitor IA de Trade Activo ────────────────────────────
function TradeMonitorPanel({ tradeMonitorLog, tradeMonitor }) {
  // Combina el log persistido con el veredicto actual del state (si existe)
  const logEntries = Array.isArray(tradeMonitorLog) ? tradeMonitorLog : [];
  // Últimas 8 entradas del log, más reciente primero
  const recentLog = [...logEntries].slice(-8).reverse();

  // Si el state tiene trade_monitor (veredicto de este ciclo), mostrarlo primero
  const currentVerdict = tradeMonitor && tradeMonitor.action ? tradeMonitor : null;

  const actionColor = (action) => {
    if (action === "EMERGENCY_CLOSE") return R;
    if (action === "UPDATE_SL") return Y;
    if (action === "HOLD") return G;
    return MUTE;
  };

  const actionIcon = (action) => {
    if (action === "EMERGENCY_CLOSE") return "✕";
    if (action === "UPDATE_SL") return "↑";
    if (action === "HOLD") return "●";
    return "?";
  };

  const hasAnyData = currentVerdict || recentLog.length > 0;

  return (
    <Card
      title="Monitor IA · Trade Activo"
      right={
        currentVerdict ? (
          <span style={{
            fontSize: 11, fontWeight: 700, padding: "3px 10px", borderRadius: 999,
            background: `${actionColor(currentVerdict.action)}18`,
            border: `1px solid ${actionColor(currentVerdict.action)}44`,
            color: actionColor(currentVerdict.action),
          }}>
            {actionIcon(currentVerdict.action)} {currentVerdict.action}
          </span>
        ) : (
          <span style={{ fontSize: 10, color: MUTE }}>sin posicion activa</span>
        )
      }
    >
      {!hasAnyData ? (
        <div style={{ color: MUTE, fontSize: 12, textAlign: "center", padding: "16px 0" }}>
          Sin veredictos del monitor. Se activa cuando hay una posicion abierta.
        </div>
      ) : (
        <>
          {/* Veredicto actual con detalles de estado */}
          {currentVerdict && (
            <div style={{
              background: `${actionColor(currentVerdict.action)}0d`,
              border: `1px solid ${actionColor(currentVerdict.action)}30`,
              borderRadius: 10, padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{
                  width: 28, height: 28, borderRadius: 8, flexShrink: 0,
                  background: `${actionColor(currentVerdict.action)}18`,
                  border: `1px solid ${actionColor(currentVerdict.action)}44`,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 14, color: actionColor(currentVerdict.action), fontWeight: 700,
                }}>
                  {actionIcon(currentVerdict.action)}
                </div>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 700, color: actionColor(currentVerdict.action) }}>
                    {currentVerdict.action}
                  </div>
                  <div style={{ fontSize: 10, color: MUTE }}>{fmtDate(currentVerdict.timestamp)}</div>
                </div>
                <div style={{ marginLeft: "auto", fontSize: 10, color: MUTE }}>
                  mem:{currentVerdict.memory_window ?? "–"}
                </div>
              </div>

              {currentVerdict.rationale && (
                <div style={{ fontSize: 12, color: TEXT, lineHeight: 1.5, opacity: 0.85 }}>
                  {currentVerdict.rationale}
                </div>
              )}

              {/* Métricas del estado en el momento del veredicto */}
              {currentVerdict.state && (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
                  {[
                    { label: "PnL%", value: `${Number(currentVerdict.state.unrealized_pnl_pct || 0) >= 0 ? "+" : ""}${n(currentVerdict.state.unrealized_pnl_pct, 3)}%`, color: tone(currentVerdict.state.unrealized_pnl_pct) },
                    { label: "Flow", value: `${Math.round(Number(currentVerdict.state.trade_flow_score || 0) * 100)}%`, color: Number(currentVerdict.state.trade_flow_score) >= 0.52 ? G : R },
                    { label: "Vol×", value: `${n(currentVerdict.state.volume_ratio, 2)}x`, color: Number(currentVerdict.state.volume_ratio) >= 1 ? G : R },
                    { label: "OB%", value: `${Math.round(Number(currentVerdict.state.orderbook_imbalance || 0.5) * 100)}%`, color: Number(currentVerdict.state.orderbook_imbalance) >= 0.52 ? G : R },
                  ].map(({ label, value, color }) => (
                    <div key={label} style={{ background: "rgba(255,255,255,0.03)", borderRadius: 6, padding: "5px 7px" }}>
                      <div style={{ fontSize: 9, color: MUTE }}>{label}</div>
                      <div style={{ fontSize: 12, fontWeight: 700, fontFamily: "monospace", color }}>{value}</div>
                    </div>
                  ))}
                </div>
              )}

              {currentVerdict.new_sl_price && (
                <div style={{ fontSize: 11, color: Y }}>
                  → Nuevo SL: <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{n(currentVerdict.new_sl_price, 6)}</span>
                  {currentVerdict.new_sl_pct && ` (+${n(currentVerdict.new_sl_pct * 100, 2)}%)`}
                </div>
              )}

              <div style={{ fontSize: 10, color: MUTE }}>
                Modelo: {currentVerdict.model || "OpenRouter"}
              </div>
            </div>
          )}

          {/* Historial de veredictos */}
          {recentLog.length > 0 && (
            <div>
              <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
                Historial ({recentLog.length} entradas)
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                {recentLog.slice(0, 6).map((entry, i) => {
                  const ac = actionColor(entry.action);
                  const st = entry.state || {};
                  return (
                    <div key={i} style={{
                      display: "flex", alignItems: "flex-start", gap: 8, padding: "7px 10px",
                      borderRadius: 8, background: "rgba(255,255,255,0.025)",
                      border: `1px solid rgba(255,255,255,0.05)`,
                    }}>
                      <div style={{
                        width: 22, height: 22, borderRadius: 6, flexShrink: 0,
                        background: `${ac}18`, border: `1px solid ${ac}40`,
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 11, color: ac, fontWeight: 700,
                      }}>
                        {actionIcon(entry.action)}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
                          <span style={{ fontSize: 11, fontWeight: 700, color: ac }}>{entry.action}</span>
                          <span style={{ fontSize: 10, color: MUTE, flexShrink: 0 }}>{fmtDate(entry.timestamp)}</span>
                        </div>
                        <div style={{ fontSize: 11, color: MUTE, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {entry.rationale}
                        </div>
                        <div style={{ display: "flex", gap: 8, fontSize: 10, color: MUTE, marginTop: 2 }}>
                          <span>PnL:{Number(st.unrealized_pnl_pct || 0) >= 0 ? "+" : ""}{n(st.unrealized_pnl_pct, 2)}%</span>
                          <span>Flow:{Math.round(Number(st.trade_flow_score || 0) * 100)}%</span>
                          <span>Vol:{n(st.volume_ratio, 1)}×</span>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// ── Header terminal ───────────────────────────────────────────────
function TerminalHeader({ status, isOnline, control, risk, portfolio, payload, controlBusy, sendControl }) {
  const pnl = Number(portfolio?.realized_pnl_usdt || 0);
  const dd  = Number(portfolio?.max_drawdown_pct  || 0);
  return (
    <header style={{
      background: "rgba(6,12,20,0.98)", borderBottom: `1px solid ${BORD}`,
      padding: "10px 24px", display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap",
      position: "sticky", top: 0, zIndex: 100, backdropFilter: "blur(12px)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
          background: isOnline ? G : R,
          boxShadow: isOnline ? `0 0 8px ${G}` : "none",
          animation: isOnline ? "pulse 2s infinite" : "none",
        }} />
        <span style={{ fontWeight: 700, fontSize: 14, color: TEXT }}>OptiFerre Terminal</span>
        <span style={{ fontSize: 11, color: MUTE, borderLeft: `1px solid ${BORD}`, paddingLeft: 10 }}>
          {(() => {
            const syms = status?.target_symbols;
            if (syms?.length > 1) return `${syms.length} mercados`;
            return syms?.[0] || status?.symbol || "–";
          })()} · {status?.timeframe || "5m"} · {isOnline ? "LIVE" : "OFFLINE"}
        </span>
      </div>

      <div style={{ flex: 1 }} />

      <div style={{ display: "flex", alignItems: "center", gap: 20, fontSize: 12 }}>
        <div><span style={{ color: MUTE }}>Balance </span><strong style={{ fontFamily: "monospace" }}>{n(risk?.balance_usd)} USDT</strong></div>
        <div><span style={{ color: MUTE }}>PnL </span><strong style={{ fontFamily: "monospace", color: tone(pnl) }}>{pnl >= 0 ? "+" : ""}{n(pnl)} USDT</strong></div>
        <div><span style={{ color: MUTE }}>DD </span><strong style={{ fontFamily: "monospace", color: dd >= 0.03 ? R : MUTE }}>{pct(dd)}</strong></div>
        <span style={{ fontSize: 11, color: MUTE }}>{fmtDate(payload?.serverTime)}</span>
      </div>

      <div style={{ display: "flex", gap: 6 }}>
        {["running", "paused", "stopped"].map((s) => {
          const active = control?.desired_state === s;
          const col = s === "running" ? G : s === "paused" ? Y : R;
          return (
            <button key={s} disabled={controlBusy} onClick={() => sendControl(s)} style={{
              padding: "5px 12px", borderRadius: 8, border: `1px solid ${active ? col + "66" : BORD}`,
              cursor: controlBusy ? "not-allowed" : "pointer",
              background: active ? `${col}18` : "rgba(255,255,255,0.03)",
              color: active ? col : MUTE, fontSize: 11, fontWeight: 600, transition: "all 0.2s",
            }}>
              {s === "running" ? "▶ Activo" : s === "paused" ? "⏸ Pausar" : "⏹ Detener"}
            </button>
          );
        })}
        <Link href="/matriz" style={{ padding: "5px 12px", borderRadius: 8, border: `1px solid ${BORD}`, fontSize: 11, color: MUTE, textDecoration: "none", background: "rgba(255,255,255,0.03)" }}>
          Matriz →
        </Link>
        <Link href="/montecarlo" style={{ padding: "5px 12px", borderRadius: 8, border: `1px solid #1a3a2a`, fontSize: 11, color: "#12d98b", textDecoration: "none", background: "rgba(18,217,139,0.06)" }}>
          Monte Carlo →
        </Link>
        <Link href="/cohort" style={{ padding: "5px 12px", borderRadius: 8, border: `1px solid #1a2a3a`, fontSize: 11, color: "#60a5fa", textDecoration: "none", background: "rgba(96,165,250,0.06)" }}>
          V3 Cohort →
        </Link>
        <Link href="/admin/dashboard" style={{ padding: "5px 12px", borderRadius: 8, border: `1px solid #2a1a3a`, fontSize: 11, color: "#a78bfa", textDecoration: "none", background: "rgba(167,139,250,0.06)", fontWeight: 600 }}>
          Admin Panel →
        </Link>
      </div>
    </header>
  );
}

// ── KPI Strip ─────────────────────────────────────────────────────
function KpiStrip({ risk, portfolio, lastScans, control, preFlight, isOnline, cohort }) {
  const pnl    = Number(portfolio?.realized_pnl_usdt || 0);
  const equity = Number(risk?.equity_usd || 0);
  const winRate = Number(portfolio?.win_rate_pct || 0);
  const dd     = Number(portfolio?.max_drawdown_pct || 0);
  const candidates = lastScans.filter((s) => s.status === "candidate").length;
  const total  = (portfolio?.wins || 0) + (portfolio?.losses || 0);

  // V3 cohort sub-info (from /api/cohort-analytics fetched at parent level)
  const v3Total = cohort?.summary?.total_v3 ?? null;
  const v3WR    = cohort?.v3?.win_rate_pct ?? null;
  const v3PnL   = cohort?.v3?.net_pnl_usdt ?? null;
  const v3Wins  = cohort?.v3?.wins ?? null;
  const v3Loss  = cohort?.v3?.losses ?? null;

  const kpis = [
    { label: "Equity USDT",     value: `${n(equity)} $`,    color: TEXT, sub: "live balance" },
    { label: "PnL realizado",   value: `${pnl >= 0 ? "+" : ""}${n(pnl)} $`,  color: tone(pnl),
      sub: v3PnL !== null ? `V3: ${v3PnL >= 0 ? "+" : ""}${n(v3PnL)} $` : "all-time" },
    { label: "WR all-time",     value: `${n(winRate)}%`,     color: winRate >= 50 ? G : R,
      sub: v3WR !== null ? `V3: ${n(v3WR)}% (${v3Wins}W/${v3Loss}L)` : `${portfolio?.wins || 0}W / ${portfolio?.losses || 0}L` },
    { label: "Max Drawdown",    value: pct(dd),              color: dd >= 0.03 ? R : dd >= 0.015 ? Y : G, sub: "vs HWM" },
    { label: "Trades all-time", value: String(total),        color: MUTE,
      sub: v3Total !== null ? `V3: ${v3Total} · Legacy: ${(cohort?.summary?.total_legacy ?? 0)}` : "" },
    { label: "Candidatas",      value: String(candidates),   color: candidates > 0 ? G : MUTE, sub: "scan actual" },
    { label: "Pre-flight",      value: preFlight?.ok ? "VERDE" : "BLOQUEADO", color: preFlight?.ok ? G : R, sub: preFlight?.ok ? "go-live" : "veto" },
    { label: "Estado",          value: (control?.desired_state || "running").toUpperCase(), color: control?.desired_state === "running" ? G : R, sub: "control" },
  ];

  return (
    <div style={{ display: "flex", gap: 8, overflowX: "auto", paddingBottom: 2 }}>
      {kpis.map((k, i) => (
        <div key={i} style={{ background: CARD, border: `1px solid ${BORD}`, borderRadius: 12, padding: "10px 16px", flexShrink: 0, minWidth: 130 }}>
          <div style={{ fontSize: 9, color: MUTE, textTransform: "uppercase", letterSpacing: "0.08em" }}>{k.label}</div>
          <div style={{ fontSize: 14, fontWeight: 700, fontFamily: "monospace", color: k.color, marginTop: 2 }}>{k.value}</div>
          {k.sub && <div style={{ fontSize: 10, color: MUTE }}>{k.sub}</div>}
        </div>
      ))}
    </div>
  );
}

// ── COMPONENTE PRINCIPAL ──────────────────────────────────────────
export default function DashboardClient({ initialData }) {
  const [payload, setPayload]       = useState(initialData);
  const [controlBusy, setControlBusy] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [cohort, setCohort]         = useState(null);

  const refresh = useCallback(async () => {
    try {
      const res  = await fetch("/api/state", { cache: "no-store" });
      const next = await res.json();
      setPayload(next);
      setLastRefresh(new Date());
    } catch (err) {
      // BUG FIX: was silent — now logs to console so the "ghost trade" cause
      // is visible in DevTools without requiring backend changes.
      console.error("[DashboardClient] /api/state refresh error:", err);
    }
  }, []);

  // Fetch cohort metrics (separate cadence — 30s — from main /api/state poll)
  const refreshCohort = useCallback(async () => {
    try {
      const res = await fetch("/api/cohort-analytics", { cache: "no-store" });
      if (!res.ok) return;
      const next = await res.json();
      setCohort(next);
    } catch (err) {
      console.warn("[DashboardClient] cohort fetch error:", err);
    }
  }, []);

  useEffect(() => {
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    refreshCohort();
    const id = setInterval(refreshCohort, 30000);
    return () => clearInterval(id);
  }, [refreshCohort]);

  // ── Extraer estado ──────────────────────────────────────────────
  const state          = payload?.state        || {};
  const status         = payload?.status       || {};
  const control        = payload?.control      || {};
  const risk           = state?.risk           || {};
  const portfolio      = state?.portfolio      || {};
  const decision       = state?.decision       || {};
  const ai             = state?.ai_signal      || {};
  const technicalSignal = state?.technical_signal || {};
  const signalHistory  = payload?.signalHistory || [];
  const openPositions  = payload?.openPositions || state?.open_positions || [];
  const closedTrades   = payload?.closedTrades  || state?.closed_trades  || [];
  const equityHistory  = payload?.equityHistory || [];
  const lastScans      = state?.last_scans      || [];
  const targetSymbols  = state?.target_symbols  || [];
  const preFlight      = payload?.preFlight     || {};
  // BUG FIX: state.open_position (bot_state.json) is written at the END of the ~60s
  // main loop cycle, while open_positions.json is written immediately on trade open.
  // During the gap, the 5s poll returns openPositions[0] with data but
  // state.open_position is still null — causing "Sin posición abierta".
  // Fallback: use openPositions[0] when the summary key is absent.
  const openPosition   = state?.open_position   || openPositions[0] || null;
  const tradeMonitorLog = payload?.tradeMonitorLog || [];
  const tradeMonitor   = state?.trade_monitor    || {};
  const recoveryStatus = payload?.recoveryStatus  || {};

  const [focusSymbol, setFocusSymbol] = useState("");
  const activeSymbol = state?.active_symbol || null;

  useEffect(() => {
    const pref = activeSymbol || openPosition?.symbol || lastScans[0]?.symbol || targetSymbols[0] || "";
    if (pref && !focusSymbol) setFocusSymbol(pref);
  }, [activeSymbol, openPosition, lastScans, targetSymbols]);

  const isOnline = useMemo(() => {
    const hb  = status?.heartbeat_at;
    if (!hb) return false;
    const ref = payload?.serverTime ? new Date(payload.serverTime).getTime() : NaN;
    if (isNaN(ref)) return false;
    // 300 s threshold: with 4 markets + sequential AI calls a cycle can take >2 min.
    return ref - new Date(hb).getTime() < 300000;
  }, [payload?.serverTime, status?.heartbeat_at]);

  const focusScan = useMemo(() => {
    if (!lastScans.length) return null;
    return lastScans.find((s) => s.symbol === focusSymbol) || lastScans.find((s) => s.symbol === activeSymbol) || lastScans[0];
  }, [lastScans, focusSymbol, activeSymbol]);

  const focusAi         = focusScan?.ai_signal || ai;
  const focusGuardrails = focusScan?.guardrails || decision;

  async function sendControl(desiredState) {
    setControlBusy(true);
    try {
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ desiredState, reason: `Cambio a ${desiredState} desde terminal.` }),
      });
      await refresh();
    } finally {
      setControlBusy(false);
    }
  }

  const body = {
    padding: "14px 20px",
    display: "flex",
    flexDirection: "column",
    gap: 14,
    maxWidth: 1800,
    margin: "0 auto",
    width: "100%",
  };

  const killActive = Boolean(risk?.kill_switch_triggered);
  const botStopped = control?.desired_state === "stopped";
  const closeFailedFlag = control?.manual_close_result === "failed";
  const showAlert  = killActive || !isOnline || botStopped;
  const isKillSwitchStop = botStopped && control?.updated_by === "kill_switch";
  const [ksResetting, setKsResetting] = useState(false);

  return (
    <div style={{ minHeight: "100vh", background: BG, color: TEXT, fontFamily: "'IBM Plex Sans','Segoe UI',sans-serif" }}>
      <TerminalHeader
        status={status} isOnline={isOnline} control={control} risk={risk}
        portfolio={portfolio} payload={payload} controlBusy={controlBusy} sendControl={sendControl}
      />

      <div style={body}>
        {/* KPI strip */}
        <KpiStrip risk={risk} portfolio={portfolio} lastScans={lastScans} control={control} preFlight={preFlight} isOnline={isOnline} cohort={cohort} />

        {/* Alert banner — bot offline / kill switch / stopped */}
        {showAlert && (
          <div style={{ background: "rgba(235,75,97,0.12)", border: `1px solid rgba(235,75,97,0.35)`, borderRadius: 12, padding: "12px 20px", display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 18 }}>⚠</span>
            <span style={{ fontSize: 13, color: R, fontWeight: 600, flex: 1 }}>
              {killActive ? "Kill Switch activo" : !isOnline ? "Bot offline · sin heartbeat reciente" : "Bot detenido"} · {control?.reason || status?.detail || "Verificar Coolify"}
            </span>
            {isKillSwitchStop && (
              <button
                disabled={ksResetting}
                onClick={async () => {
                  setKsResetting(true);
                  try {
                    await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "reset_kill_switch" }) });
                    await refresh();
                  } finally {
                    setKsResetting(false);
                  }
                }}
                style={{ background: ksResetting ? "rgba(235,75,97,0.15)" : "rgba(235,75,97,0.25)", border: `1px solid rgba(235,75,97,0.55)`, borderRadius: 6, color: R, fontSize: 12, fontWeight: 700, padding: "6px 14px", cursor: ksResetting ? "default" : "pointer", flexShrink: 0, whiteSpace: "nowrap" }}
              >
                {ksResetting ? "Reseteando…" : "🔄 Resetear y Reiniciar"}
              </button>
            )}
          </div>
        )}

        {/* Alert banner — cierre manual falló (BNB bloqueado en OCO o error Binance) */}
        {closeFailedFlag && (
          <div style={{ background: `${Y}12`, border: `1px solid ${Y}44`, borderRadius: 12, padding: "12px 20px", display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 18 }}>⚠</span>
            <span style={{ fontSize: 13, color: Y, fontWeight: 600, flex: 1 }}>
              Cierre manual falló: {control?.manual_close_error || "error desconocido"} · El bot reintentará en el próximo ciclo · Verifica saldo libre en Binance
            </span>
            <button
              onClick={async () => {
                await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "clear_close_error" }) });
                await refresh();
              }}
              style={{ background: "transparent", border: `1px solid ${Y}55`, borderRadius: 6, color: Y, fontSize: 12, padding: "4px 10px", cursor: "pointer", flexShrink: 0 }}
            >
              × Cerrar aviso
            </button>
          </div>
        )}

        {/* Tres paneles principales */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
          <PositionPanel openPosition={openPosition} openPositions={openPositions} />
          <AiPanel ai={focusAi} scan={focusScan} decision={decision} />
          <MarketPanel
            scan={focusScan} technicalSignal={technicalSignal}
            targetSymbols={targetSymbols.length ? targetSymbols : lastScans.map((s) => s.symbol)}
            onSymbol={setFocusSymbol}
          />
        </div>

        {/* Equity curve */}
        <EquityPanel equityHistory={equityHistory} />

        {/* Monitor IA de trade activo */}
        <TradeMonitorPanel tradeMonitorLog={tradeMonitorLog} tradeMonitor={tradeMonitor} />

        {/* Estrategias por mercado · perfiles de entrada en vivo */}
        <MarketProfilesPanel
          lastScans={lastScans}
          targetSymbols={targetSymbols}
          focusSymbol={focusSymbol}
          onSymbol={setFocusSymbol}
        />

        {/* Auditoría histórica por mercado · trades cerrados agrupados por symbol/scenario */}
        <MarketAuditPanel
          closedTrades={closedTrades}
          targetSymbols={targetSymbols}
          focusSymbol={focusSymbol}
          onSymbol={setFocusSymbol}
        />

        {/* Scanner completo */}
        <ScannerMatrix lastScans={lastScans} targetSymbols={targetSymbols} focusSymbol={focusSymbol} onSymbol={setFocusSymbol} />

        {/* Micro-Gate Radar: distancia a los gates V3 del símbolo enfocado */}
        <MicroGateRadar scan={focusScan} />

        {/* Fila inferior: trades + analytics + timeline */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
          <TradeHistoryPanel closedTrades={closedTrades} />
          <AnalyticsPanel portfolio={portfolio} equityHistory={equityHistory} closedTrades={closedTrades} />
          <SignalTimeline signalHistory={signalHistory} />
        </div>

        {/* Guardrails detallados */}
        <GuardrailsPanel guardrails={focusGuardrails} scan={focusScan} />

        {/* Footer */}
        <div style={{ fontSize: 10, color: MUTE, textAlign: "center", paddingBottom: 12 }}>
          OptiFerre Terminal v2.0 · Refresh automático 5s · Último: {lastRefresh ? lastRefresh.toLocaleTimeString() : "–"}
          &nbsp;·&nbsp;
          <Link href="/matriz" style={{ color: MUTE, textDecoration: "underline" }}>Ver Matriz completa</Link>
          &nbsp;·&nbsp;
          <Link href="/montecarlo" style={{ color: "#12d98b", textDecoration: "underline" }}>Monte Carlo 30d →</Link>
          &nbsp;·&nbsp;
          <Link href="/cohort" style={{ color: "#60a5fa", textDecoration: "underline" }}>V3 Cohort →</Link>
        </div>
      </div>
    </div>
  );
}
