"use client";
/**
 * components/client/ActiveTradeRadar.tsx
 *
 * Bloomberg Terminal × Cyberpunk — SVG spinning sweep radar.
 *
 * STRICTLY PROHIBITED: never renders floating PnL or ROI numbers.
 * Props are plain strings / booleans (serialisable from Server Component).
 */

import { useEffect, useState, memo } from "react";
import { motion, AnimatePresence, type Variants } from "framer-motion";
import { Activity, Brain, Radio, Shield, ShieldCheck, Cpu } from "lucide-react";
import type { ReactNode } from "react";

// ─── Public prop contract (unchanged — page.tsx already passes these) ─────────
export type ActiveTradeProps = {
  /** Is there currently an open position? */
  active: boolean;
  /** E.g. "BTCUSDT" — only present when active */
  symbol?: string;
  /** ISO 8601 open timestamp — only present when active */
  openedAt?: string;
};

// ─── Colour tokens ────────────────────────────────────────────────────────────
const EMERALD_STROKE = "#10b981";
const EMERALD_TEXT   = "#34d399";
const EMERALD_GLOW   = "rgba(16,185,129,0.45)";
const CYAN_STROKE    = "#06b6d4";
const CYAN_TEXT      = "#22d3ee";
const CYAN_GLOW      = "rgba(6,182,212,0.45)";
const MUTE           = "#6b8299";
const PANEL_BG       = "rgba(10,15,22,0.72)";
const PANEL_BORD     = "rgba(63,87,114,0.28)";

// ─── Animation variants (OUTSIDE components) ─────────────────────────────────
const sweepVariants: Variants = {
  spinning: {
    rotate: 360,
    transition: { duration: 4, ease: "linear", repeat: Infinity },
  },
};

const blipVariants: Variants = {
  pulse: {
    scale: [1, 2.4, 1],
    opacity: [0.9, 0, 0.9],
    transition: { duration: 1.7, ease: "easeInOut", repeat: Infinity },
  },
};

const idleVariants: Variants = {
  scan: {
    opacity: [0.35, 1, 0.35],
    transition: { duration: 2.4, ease: "easeInOut", repeat: Infinity },
  },
};

const panelEnter: Variants = {
  hidden:  { opacity: 0, y: 12 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.45, ease: [0.22, 1, 0.36, 1] } },
  exit:    { opacity: 0, y: -8, transition: { duration: 0.25 } },
};

// ─── Elapsed-time hook ────────────────────────────────────────────────────────
function useElapsed(openedAt?: string): string {
  const [label, setLabel] = useState(() => calcLabel(openedAt));

  useEffect(() => {
    if (!openedAt) return;
    const id = setInterval(() => setLabel(calcLabel(openedAt)), 10_000);
    return () => clearInterval(id);
  }, [openedAt]);

  return label;
}

function calcLabel(openedAt?: string): string {
  if (!openedAt) return "";
  const diffMs = Date.now() - new Date(openedAt).getTime();
  if (diffMs < 0 || !Number.isFinite(diffMs)) return "hace instantes";
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "hace instantes";
  if (mins < 60) return `hace ${mins} min`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem > 0 ? `hace ${hrs}h ${rem}min` : `hace ${hrs}h`;
}

// ─── Radar SVG constants ──────────────────────────────────────────────────────
const SIZE = 200;
const CX   = SIZE / 2;
const CY   = SIZE / 2;

// ─── Main component ───────────────────────────────────────────────────────────
export const ActiveTradeRadar = memo(function ActiveTradeRadar({
  active,
  symbol,
  openedAt,
}: ActiveTradeProps) {
  const elapsed = useElapsed(openedAt);

  const safeSymbol   = (symbol ?? "").toString().replace("USDT", "").trim() || null;
  const accentStroke = active ? EMERALD_STROKE : CYAN_STROKE;
  const accentText   = active ? EMERALD_TEXT   : CYAN_TEXT;
  const accentGlow   = active ? EMERALD_GLOW   : CYAN_GLOW;
  const stateLabel   = active ? "OPERACIÓN EN CURSO" : "SCANNING MARKET";

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={active ? "active" : "standby"}
        variants={panelEnter}
        initial="hidden"
        animate="visible"
        exit="exit"
        style={{
          position: "relative",
          overflow: "hidden",
          background: PANEL_BG,
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          borderRadius: 20,
          padding: "20px 24px",
          border: `1px solid ${PANEL_BORD}`,
          borderLeft: `2px solid ${accentStroke}`,
          boxShadow: `0 0 0 1px rgba(255,255,255,0.02) inset, 0 12px 36px -16px ${accentGlow}`,
        }}
        role="region"
        aria-label={active ? "Live trade radar" : "Market scanner"}
      >
        {/* Ambient glow blob */}
        <div
          style={{
            position: "absolute", top: -60, right: -60,
            width: 220, height: 220, borderRadius: "50%",
            background: `radial-gradient(circle, ${accentGlow} 0%, transparent 70%)`,
            pointerEvents: "none", opacity: 0.5,
          }}
          aria-hidden="true"
        />

        {/* Header badge */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
          <Activity size={13} color={accentText} />
          <span style={{
            color: accentText, fontSize: 10, fontWeight: 700,
            letterSpacing: "0.18em", textShadow: `0 0 8px ${accentGlow}`,
            fontFamily: "ui-monospace, Menlo, monospace",
          }}>
            {stateLabel}
          </span>
        </div>

        {/* Grid: radar SVG + metadata */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "200px 1fr",
          gap: 24, alignItems: "center",
        }}>
          {/* SVG radar */}
          <div style={{
            position: "relative", width: SIZE, height: SIZE,
            willChange: "transform", flexShrink: 0,
          }}>
            {/* Static rings */}
            <svg viewBox={`0 0 ${SIZE} ${SIZE}`} width={SIZE} height={SIZE}
              style={{ position: "absolute", inset: 0 }} aria-hidden="true">
              <defs>
                <radialGradient id="rdrBg2" cx="50%" cy="50%" r="50%">
                  <stop offset="0%"  stopColor={accentStroke} stopOpacity="0.16" />
                  <stop offset="80%" stopColor="transparent" />
                </radialGradient>
                <linearGradient id="rdrSweep2" x1="0%" y1="0%" x2="100%" y2="0%">
                  <stop offset="0%"   stopColor={accentStroke} stopOpacity="0" />
                  <stop offset="100%" stopColor={accentStroke} stopOpacity="0.9" />
                </linearGradient>
              </defs>
              <circle cx={CX} cy={CY} r={96} fill="url(#rdrBg2)" />
              {[28, 52, 76, 93].map((r) => (
                <circle key={r} cx={CX} cy={CY} r={r} fill="none"
                  stroke={accentStroke} strokeOpacity={0.18} strokeWidth={0.7} />
              ))}
              <line x1={CX} y1={2}   x2={CX} y2={198} stroke={accentStroke} strokeOpacity={0.18} strokeWidth={0.6} />
              <line x1={2}  y1={CY}  x2={198} y2={CY} stroke={accentStroke} strokeOpacity={0.18} strokeWidth={0.6} />
              <circle cx={CX} cy={CY} r={2.5} fill={accentText} />
            </svg>

            {/* Rotating sweep arm */}
            <motion.svg viewBox={`0 0 ${SIZE} ${SIZE}`} width={SIZE} height={SIZE}
              style={{ position: "absolute", inset: 0, willChange: "transform", transformOrigin: "50% 50%" }}
              variants={sweepVariants} animate="spinning" aria-hidden="true">
              <path d="M100,100 L100,4 A96,96 0 0,1 196,100 Z" fill="url(#rdrSweep2)" opacity={0.55} />
              <line x1={CX} y1={CY} x2={CX} y2={4}
                stroke={accentText} strokeWidth={1.5} strokeLinecap="round" opacity={0.9} />
            </motion.svg>

            {/* Active blip */}
            {active && (
              <motion.div variants={blipVariants} animate="pulse" style={{
                position: "absolute", top: "34%", left: "64%",
                width: 11, height: 11, borderRadius: "50%",
                background: EMERALD_STROKE,
                boxShadow: `0 0 8px ${EMERALD_GLOW}, 0 0 18px ${EMERALD_GLOW}`,
                willChange: "transform, opacity",
              }} aria-hidden="true" />
            )}
          </div>

          {/* Metadata column */}
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {active && safeSymbol && (
              <div>
                <p style={{ color: MUTE, fontSize: 9, fontWeight: 700, letterSpacing: "0.18em",
                  fontFamily: "ui-monospace, Menlo, monospace", marginBottom: 4 }}>TICKER</p>
                <p style={{ color: EMERALD_TEXT, fontSize: 26, fontWeight: 800,
                  fontFamily: "ui-monospace, Menlo, monospace", letterSpacing: "-0.02em",
                  lineHeight: 1, textShadow: `0 0 12px ${EMERALD_GLOW}` }}>
                  {safeSymbol}
                  <span style={{ color: "rgba(52,211,153,0.5)", fontSize: 13, fontWeight: 500, marginLeft: 4 }}>/USDT</span>
                </p>
              </div>
            )}

            <MetaRow icon={<Shield size={11} color={MUTE} />}  label="MODO"     value="AUTÓNOMO — IA ACTIVA" color={active ? EMERALD_TEXT : MUTE} />
            <MetaRow icon={<Brain  size={11} color="#c084fc" />} label="ANÁLISIS" value="EN TIEMPO REAL"       color="#c084fc" />
            {elapsed && active && (
              <MetaRow icon={<Radio size={11} color={accentText} />} label="DURACIÓN" value={elapsed} color={accentText} />
            )}

            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 2 }}>
              <StatusPill icon={<Cpu size={10} />}         label="IA Analizando"   color={EMERALD_TEXT} glow={EMERALD_GLOW} />
              <StatusPill icon={<ShieldCheck size={10} />} label="Gestión de Riesgo" color={CYAN_TEXT}  glow={CYAN_GLOW} />
            </div>

            {!active && (
              <motion.p variants={idleVariants} animate="scan"
                style={{ color: MUTE, fontSize: 10, letterSpacing: "0.16em",
                  fontFamily: "ui-monospace, Menlo, monospace", fontWeight: 600 }}>
                ▸ EVALUANDO ORDERFLOW…
              </motion.p>
            )}

            <p style={{ color: "rgba(107,130,153,0.7)", fontSize: 11, lineHeight: 1.6, marginTop: 2 }}>
              Los resultados intermedios no se muestran para evitar sesgos emocionales.
              El sistema trabaja de forma autónoma en tu nombre.
            </p>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
});

// ─── MetaRow ──────────────────────────────────────────────────────────────────
const MetaRow = memo(function MetaRow({
  icon, label, value, color,
}: { icon: ReactNode; label: string; value: string; color: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <span style={{ display: "inline-flex", width: 16 }}>{icon}</span>
      <span style={{ color: MUTE, fontSize: 9, fontWeight: 700, letterSpacing: "0.16em",
        minWidth: 80, fontFamily: "ui-monospace, Menlo, monospace" }}>
        {label}
      </span>
      <span style={{ color, fontSize: 12, fontWeight: 700, fontFamily: "ui-monospace, Menlo, monospace" }}>
        {value}
      </span>
    </div>
  );
});

// ─── StatusPill ───────────────────────────────────────────────────────────────
const StatusPill = memo(function StatusPill({
  icon, label, color, glow,
}: { icon: ReactNode; label: string; color: string; glow: string }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      background: glow.replace("0.45", "0.1"),
      border: `1px solid ${glow.replace("0.45", "0.3")}`,
      borderRadius: 100, padding: "3px 9px",
      fontSize: 10, fontWeight: 600, color,
    }}>
      {icon}{label}
    </span>
  );
});
