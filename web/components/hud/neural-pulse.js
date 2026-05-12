"use client";
import { motion, AnimatePresence } from "framer-motion";
import { Activity, Brain } from "lucide-react";
import { extractAiKeywords } from "../../lib/derive-hud-state";

// Confidence IA con anillo pulsante. Color cambia según threshold (default 0.55).
export default function NeuralPulse({ confidence = 0, threshold = 0.55, signal = "hold", model = "lazy_gate", approved = false, fallbackMode = false, rationale = "" }) {
  const conf = Math.max(0, Math.min(1, Number(confidence) || 0));
  const pct = Math.round(conf * 100);
  const above = conf >= threshold;
  const color = !approved ? "#6b7280" : above ? "#22c55e" : conf >= threshold - 0.1 ? "#facc15" : "#ef4444";
  const ring = `conic-gradient(${color} ${pct * 3.6}deg, rgba(255,255,255,0.05) 0)`;

  const keywords = extractAiKeywords(rationale);

  return (
    <div className="hud-neural">
      <div className="hud-neural-head">
        <Brain size={14} />
        <span>NEURAL PULSE</span>
        {fallbackMode && <span className="hud-tag-warn">FALLBACK TÉCNICO</span>}
      </div>

      <div className="hud-neural-ring-wrap">
        <motion.div
          className="hud-neural-ring"
          style={{ background: ring, boxShadow: `0 0 32px ${color}40, inset 0 0 24px ${color}20` }}
          animate={{ scale: above ? [1, 1.04, 1] : 1, opacity: 1 }}
          transition={{ duration: above ? 1.6 : 0.4, repeat: above ? Infinity : 0, ease: "easeInOut" }}
        >
          <div className="hud-neural-core" style={{ borderColor: `${color}88` }}>
            <div className="hud-neural-pct" style={{ color }}>{pct}%</div>
            <div className="hud-neural-sub">CONFIDENCE</div>
          </div>
        </motion.div>
      </div>

      {keywords.length > 0 && (
        <div className="hud-neural-keywords">
          {keywords.map((kw, i) => (
            <motion.span
              key={kw.tag}
              className="hud-keyword-badge"
              style={{ borderColor: kw.color, color: kw.color, boxShadow: `0 0 8px ${kw.color}55` }}
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: i * 0.08, type: "spring", stiffness: 400, damping: 20 }}
            >
              {kw.tag}
            </motion.span>
          ))}
        </div>
      )}

      <div className="hud-neural-meta">
        <div className="hud-neural-row">
          <span className="muted">Signal</span>
          <AnimatePresence mode="wait">
            <motion.span
              key={signal}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              className={`hud-neural-signal hud-neural-signal-${signal}`}
            >
              <Activity size={12} /> {String(signal).toUpperCase()}
            </motion.span>
          </AnimatePresence>
        </div>
        <div className="hud-neural-row">
          <span className="muted">Threshold</span>
          <span>{(threshold * 100).toFixed(0)}%</span>
        </div>
        <div className="hud-neural-row">
          <span className="muted">Approved</span>
          <span style={{ color: approved ? "#22c55e" : "#ef4444" }}>{approved ? "YES" : "NO"}</span>
        </div>
        <div className="hud-neural-row">
          <span className="muted">Model</span>
          <span className="hud-neural-model">{(model || "?").split("/").pop().slice(0, 28)}</span>
        </div>
      </div>
    </div>
  );
}
