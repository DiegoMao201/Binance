"use client";
import { motion } from "framer-motion";
import { CheckCircle2, XCircle, Zap } from "lucide-react";

// Cyber-grid de los 4 escenarios (A/B/C/D). Cada check parpadea en rojo neón si falla.
export default function DecisionMatrix({ matrix = [], focus = "" }) {
  return (
    <div className="hud-matrix">
      <div className="hud-matrix-head">
        <Zap size={14} />
        <span>DECISION MATRIX</span>
        <span className="hud-matrix-focus">{focus || "—"}</span>
      </div>
      <div className="hud-matrix-grid">
        {matrix.map((sc) => {
          const passing = sc.checks.filter((c) => c.ok).length;
          const total = sc.checks.length;
          const ratio = total ? passing / total : 0;
          const isCandidate = sc.active;

          return (
            <motion.div
              key={sc.id}
              className={`hud-matrix-cell ${isCandidate ? "hud-matrix-cell-active" : ""}`}
              animate={isCandidate ? { boxShadow: ["0 0 0 rgba(34,211,238,0)", "0 0 18px rgba(34,211,238,0.5)", "0 0 0 rgba(34,211,238,0)"] } : { boxShadow: "0 0 0 rgba(0,0,0,0)" }}
              transition={{ duration: 1.6, repeat: isCandidate ? Infinity : 0 }}
            >
              <div className="hud-matrix-cell-head">
                <div className="hud-matrix-id" data-state={isCandidate ? "fire" : ratio > 0.6 ? "warm" : "cold"}>{sc.id}</div>
                <div className="hud-matrix-cell-meta">
                  <div className="hud-matrix-label">{sc.label}</div>
                  <div className="hud-matrix-ratio">{passing}/{total}</div>
                </div>
              </div>
              <ul className="hud-matrix-checks">
                {sc.checks.map((c, i) => (
                  <li key={i} className={c.ok ? "ok" : "fail"}>
                    <span className="hud-matrix-check-icon">
                      {c.ok ? <CheckCircle2 size={12} /> : <motion.span animate={{ opacity: [1, 0.35, 1] }} transition={{ duration: 1.0, repeat: Infinity }}><XCircle size={12} /></motion.span>}
                    </span>
                    <span className="hud-matrix-check-name">{c.name}</span>
                    <span className="hud-matrix-check-value">{c.value}</span>
                  </li>
                ))}
              </ul>
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
