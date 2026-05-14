"use client";
/**
 * components/client/ActiveTradeRadar.tsx
 *
 * "Live Radar" widget — shown in the client dashboard.
 *
 * Shows operation metadata when the bot has an open position.
 * STRICTLY PROHIBITED: never renders floating PnL or ROI numbers.
 *
 * Props are plain strings / booleans (serialisable from Server Component).
 */

import { motion, AnimatePresence } from "framer-motion";
import { Activity, Radio, ShieldCheck, ScanLine, Cpu } from "lucide-react";
import { useEffect, useState } from "react";

export type ActiveTradeProps = {
  /** Is there currently an open position? */
  active: boolean;
  /** E.g. "BTCUSDT" — only present when active */
  symbol?: string;
  /** ISO 8601 open timestamp — only present when active */
  openedAt?: string;
};

// ─── Elapsed time helper ─────────────────────────────────────────────────────
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
  if (diffMs < 0) return "hace instantes";
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "hace instantes";
  if (mins < 60) return `hace ${mins} min`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem > 0 ? `hace ${hrs}h ${rem}min` : `hace ${hrs}h`;
}

// ─── Equaliser bars (CSS-animated) ──────────────────────────────────────────
function EqualiserBars() {
  const delays = [0, 0.2, 0.4, 0.2, 0.35];
  return (
    <span style={{ display: "inline-flex", alignItems: "flex-end", gap: 2, height: 16 }}>
      {delays.map((delay, i) => (
        <motion.span
          key={i}
          style={{
            display: "block",
            width: 3,
            background: "rgba(16,185,129,0.85)",
            borderRadius: 2,
          }}
          animate={{ height: ["4px", "14px", "6px", "12px", "4px"] }}
          transition={{
            duration: 1.2,
            repeat: Infinity,
            ease: "easeInOut",
            delay,
          }}
        />
      ))}
    </span>
  );
}

// ─── Radar ring (standby) ────────────────────────────────────────────────────
function RadarRing() {
  return (
    <span style={{ position: "relative", display: "inline-flex", alignItems: "center", justifyContent: "center", width: 28, height: 28 }}>
      <motion.span
        style={{
          position: "absolute",
          inset: 0,
          borderRadius: "50%",
          border: "1.5px solid rgba(99,102,241,0.5)",
        }}
        animate={{ scale: [1, 1.55, 1], opacity: [0.6, 0, 0.6] }}
        transition={{ duration: 3, repeat: Infinity, ease: "easeInOut" }}
      />
      <Radio size={14} color="#6366f1" />
    </span>
  );
}

// ─── Main component ──────────────────────────────────────────────────────────
export function ActiveTradeRadar({ active, symbol, openedAt }: ActiveTradeProps) {
  const elapsed = useElapsed(openedAt);

  return (
    <AnimatePresence mode="wait">
      {active ? (
        <motion.div
          key="active"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
          style={{
            position: "relative",
            overflow: "hidden",
            background: "linear-gradient(135deg, #061210 0%, #080e10 100%)",
            borderRadius: 20,
            padding: "20px 24px",
            border: "1px solid rgba(16,185,129,0.22)",
            boxShadow:
              "0 0 0 1px rgba(16,185,129,0.08), 0 0 24px rgba(16,185,129,0.08), inset 0 1px 0 rgba(16,185,129,0.06)",
          }}
        >
          {/* Ambient glow pulse */}
          <motion.div
            style={{
              position: "absolute",
              top: -60,
              right: -60,
              width: 200,
              height: 200,
              borderRadius: "50%",
              background: "radial-gradient(circle, rgba(16,185,129,0.07) 0%, transparent 70%)",
              pointerEvents: "none",
            }}
            animate={{ scale: [1, 1.2, 1], opacity: [0.6, 1, 0.6] }}
            transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
          />

          {/* Header */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {/* Live dot */}
              <span style={{ position: "relative", display: "inline-flex" }}>
                <motion.span
                  style={{
                    position: "absolute",
                    inset: 0,
                    borderRadius: "50%",
                    background: "rgba(16,185,129,0.4)",
                  }}
                  animate={{ scale: [1, 2.2], opacity: [0.6, 0] }}
                  transition={{ duration: 1.5, repeat: Infinity }}
                />
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#10b981", display: "block", boxShadow: "0 0 8px #10b981" }} />
              </span>
              <span style={{ color: "#10b981", fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase" }}>
                Operación en vivo
              </span>
            </div>
            <EqualiserBars />
          </div>

          {/* Symbol hero */}
          {symbol && (
            <p style={{ color: "#ecfdf5", fontSize: 26, fontWeight: 800, fontFamily: "monospace", letterSpacing: "-0.02em", marginBottom: 14 }}>
              {symbol.replace("USDT", "")}
              <span style={{ color: "rgba(16,185,129,0.6)", fontSize: 14, fontWeight: 500, marginLeft: 4 }}>/USDT</span>
            </p>
          )}

          {/* Status pills */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 16 }}>
            <StatusPill icon={<Cpu size={11} />} label="IA Analizando" color="#10b981" />
            <StatusPill icon={<ShieldCheck size={11} />} label="Gestión de Riesgo Activa" color="#10b981" />
            {elapsed && (
              <StatusPill icon={<Activity size={11} />} label={`Operación iniciada ${elapsed}`} color="rgba(220,231,245,0.5)" />
            )}
          </div>

          {/* Disclaimer */}
          <p style={{ color: "rgba(107,130,153,0.8)", fontSize: 11, lineHeight: 1.5 }}>
            Los resultados intermedios no se muestran para evitar sesgos emocionales.
            El sistema trabaja de forma autónoma en tu nombre.
          </p>
        </motion.div>
      ) : (
        <motion.div
          key="standby"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
          style={{
            background: "linear-gradient(135deg, #090c18 0%, #080e16 100%)",
            borderRadius: 20,
            padding: "20px 24px",
            border: "1px solid rgba(99,102,241,0.15)",
            boxShadow: "0 0 20px rgba(99,102,241,0.04)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <RadarRing />
              <span style={{ color: "#6366f1", fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase" }}>
                Escaneando Mercados
              </span>
            </div>
            <ScanLine size={16} color="rgba(99,102,241,0.5)" />
          </div>
          <p style={{ color: "rgba(107,130,153,0.9)", fontSize: 13, lineHeight: 1.55 }}>
            La IA evalúa condiciones macro, orderbook y señales técnicas en tiempo real.
            Se notificará cuando se identifique una oportunidad de alta probabilidad.
          </p>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ─── Status pill ─────────────────────────────────────────────────────────────
function StatusPill({
  icon,
  label,
  color,
}: {
  icon: React.ReactNode;
  label: string;
  color: string;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        background: "rgba(255,255,255,0.04)",
        border: "1px solid rgba(255,255,255,0.07)",
        borderRadius: 100,
        padding: "4px 10px",
        fontSize: 11,
        fontWeight: 500,
        color,
      }}
    >
      {icon}
      {label}
    </span>
  );
}
