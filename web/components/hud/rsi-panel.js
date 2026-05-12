"use client";
import { useEffect, useRef } from "react";

// Zonas de activación de escenarios (deben coincidir con config del bot)
const ZONES = [
  { from: 70, to: 100, color: "rgba(239,68,68,0.22)",   label: "OVERBOUGHT",    labelColor: "#ef4444" },
  { from: 52, to:  70, color: "rgba(34,197,94,0.12)",   label: "SCENARIO C",    labelColor: "#22c55e" },
  { from: 36, to:  52, color: "rgba(34,211,238,0.09)",  label: "SCENARIO A",    labelColor: "#22d3ee" },
  { from: 28, to:  36, color: "rgba(139,92,246,0.14)",  label: "SCENARIO B",    labelColor: "#a78bfa" },
  { from:  0, to:  28, color: "rgba(239,68,68,0.18)",   label: "DEEP OVERSOLD", labelColor: "#fb923c" },
];

const RSI_MIN = 0;
const RSI_MAX = 100;
const PANEL_H = 80; // px — altura del panel RSI

function rsiY(rsi, h) {
  return h - ((Math.max(RSI_MIN, Math.min(RSI_MAX, rsi)) - RSI_MIN) / (RSI_MAX - RSI_MIN)) * h;
}

export default function RsiPanel({ candles = [], rsiMaxA = 52, rsiMaxB = 36 }) {
  const ref = useRef(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.offsetWidth;
    const H = PANEL_H;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    // Fondo
    ctx.fillStyle = "rgba(3,9,20,0.96)";
    ctx.fillRect(0, 0, W, H);

    // Zonas coloreadas (usando valores del bot si difieren)
    const dynamicZones = [
      { from: 70,      to: 100,    color: "rgba(239,68,68,0.22)",   label: "OB",    labelColor: "#ef4444" },
      { from: rsiMaxA, to: 70,     color: "rgba(34,197,94,0.12)",   label: "C",     labelColor: "#22c55e" },
      { from: rsiMaxB, to: rsiMaxA,color: "rgba(34,211,238,0.09)",  label: "A",     labelColor: "#22d3ee" },
      { from: 28,      to: rsiMaxB,color: "rgba(139,92,246,0.14)",  label: "B",     labelColor: "#a78bfa" },
      { from: 0,       to: 28,     color: "rgba(239,68,68,0.22)",   label: "DEEP",  labelColor: "#fb923c" },
    ];

    for (const z of dynamicZones) {
      const y1 = rsiY(z.to, H);
      const y2 = rsiY(z.from, H);
      ctx.fillStyle = z.color;
      ctx.fillRect(0, y1, W, y2 - y1);
      // Label a la derecha
      ctx.fillStyle = z.labelColor;
      ctx.font = `bold 9px 'JetBrains Mono', monospace`;
      ctx.textAlign = "right";
      ctx.globalAlpha = 0.7;
      ctx.fillText(z.label, W - 4, y1 + 10);
      ctx.globalAlpha = 1;
    }

    // Líneas de referencia
    const refLines = [28, rsiMaxB, rsiMaxA, 70];
    for (const val of refLines) {
      const y = rsiY(val, H);
      ctx.beginPath();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = "rgba(255,255,255,0.12)";
      ctx.lineWidth = 1;
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Sin datos
    if (!candles || candles.length < 2) {
      ctx.fillStyle = "#334155";
      ctx.font = "10px monospace";
      ctx.textAlign = "center";
      ctx.fillText("RSI — sin datos", W / 2, H / 2);
      return;
    }

    // RSI line
    const visible = candles.slice(-Math.floor(W / 2));
    const step = W / visible.length;

    ctx.beginPath();
    let started = false;
    for (let i = 0; i < visible.length; i++) {
      const rsi = Number(visible[i].rsi);
      if (!Number.isFinite(rsi)) continue;
      const x = i * step + step / 2;
      const y = rsiY(rsi, H);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = "#facc15";
    ctx.lineWidth = 1.5;
    ctx.shadowColor = "#facc1599";
    ctx.shadowBlur = 4;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Punto actual (último)
    const last = visible[visible.length - 1];
    if (last) {
      const rsiLast = Number(last.rsi);
      if (Number.isFinite(rsiLast)) {
        const x = (visible.length - 1) * step + step / 2;
        const y = rsiY(rsiLast, H);
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = "#facc15";
        ctx.shadowColor = "#facc15";
        ctx.shadowBlur = 8;
        ctx.fill();
        ctx.shadowBlur = 0;

        // Valor actual
        ctx.fillStyle = "#facc15";
        ctx.font = "bold 9px 'JetBrains Mono', monospace";
        ctx.textAlign = "left";
        ctx.fillText(rsiLast.toFixed(1), x + 5, y + 4);
      }
    }

    // Etiqueta RSI
    ctx.fillStyle = "#64748b";
    ctx.font = "8px monospace";
    ctx.textAlign = "left";
    ctx.fillText("RSI(14)", 4, 10);
  }, [candles, rsiMaxA, rsiMaxB]);

  return (
    <div className="hud-rsi-panel">
      <canvas
        ref={ref}
        style={{ width: "100%", height: `${PANEL_H}px`, display: "block" }}
      />
    </div>
  );
}
