"use client";
import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { Terminal } from "lucide-react";

const LEVEL_STYLE = {
  win:   { color: "#22c55e", glow: "0 0 6px rgba(34,197,94,0.5)" },
  loss:  { color: "#ef4444", glow: "0 0 6px rgba(239,68,68,0.5)" },
  warn:  { color: "#facc15", glow: "0 0 6px rgba(250,204,21,0.5)" },
  info:  { color: "#22d3ee", glow: "0 0 6px rgba(34,211,238,0.4)" },
  muted: { color: "#6b8299", glow: "none" },
};

function fmtTime(ms) {
  try {
    const d = new Date(ms);
    return d.toISOString().slice(11, 19);
  } catch { return "--:--:--"; }
}

export default function AuditTerminal({ events = [] }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!ref.current) return;
    ref.current.scrollTop = 0; // último arriba (events ya van DESC)
  }, [events]);

  return (
    <div className="hud-terminal">
      <div className="hud-terminal-head">
        <Terminal size={14} />
        <span>AUDIT FEED</span>
        <span className="hud-terminal-count">{events.length} eventos</span>
        <span className="hud-terminal-blink">●</span>
      </div>
      <div className="hud-terminal-body" ref={ref}>
        {events.length === 0 && <div className="hud-terminal-empty">// esperando primer evento del bot…</div>}
        {events.map((e, i) => {
          const st = LEVEL_STYLE[e.level] || LEVEL_STYLE.muted;
          return (
            <motion.div
              key={`${e.time}-${i}`}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.18 }}
              className="hud-terminal-line"
            >
              <span className="hud-terminal-time">{fmtTime(e.time)}</span>
              <span className="hud-terminal-tag" style={{ color: st.color, textShadow: st.glow }}>{e.tag}</span>
              <span className="hud-terminal-symbol">{e.symbol}</span>
              <span className="hud-terminal-msg" style={{ color: st.color === "#6b8299" ? "#9fb6cc" : st.color }}>{e.msg}</span>
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
