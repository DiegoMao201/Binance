"use client";
import { useEffect, useRef } from "react";
import RsiPanel from "./rsi-panel";

// Componente de gráfica TradingView-style con lightweight-charts.
// - Renderiza velas + EMA20 + markers ENTRY/EXIT/LIVE + trailing SL en vivo.
// - Side-effect free: solo escribe en el container y limpia al desmontar.
// - 60fps: lightweight-charts es WebGL-free pero altamente optimizado (canvas).
export default function LiveChart({ candles = [], markers = [], trailingSL = null, entryPrice = null, takeProfit = null, height = 360, symbol = "", rsiMaxA = 52, rsiMaxB = 36 }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const emaSeriesRef = useRef(null);
  const slLineRef = useRef(null);
  const tpLineRef = useRef(null);
  const entryLineRef = useRef(null);

  // Init chart
  useEffect(() => {
    let disposed = false;
    let resizeObs;
    (async () => {
      const lib = await import("lightweight-charts");
      if (disposed || !containerRef.current) return;

      const chart = lib.createChart(containerRef.current, {
        autoSize: true,
        layout: {
          background: { type: "solid", color: "transparent" },
          textColor: "#7d93a8",
          fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace",
        },
        grid: {
          vertLines: { color: "rgba(34, 211, 238, 0.05)" },
          horzLines: { color: "rgba(34, 211, 238, 0.05)" },
        },
        rightPriceScale: { borderColor: "rgba(34, 211, 238, 0.15)" },
        timeScale: {
          borderColor: "rgba(34, 211, 238, 0.15)",
          timeVisible: true,
          secondsVisible: false,
        },
        crosshair: {
          mode: 1,
          vertLine: { color: "#22d3ee", style: 2, labelBackgroundColor: "#0a1622" },
          horzLine: { color: "#22d3ee", style: 2, labelBackgroundColor: "#0a1622" },
        },
      });

      // v5 API: addSeries(SeriesType, options) ; v4 API: addCandlestickSeries(opts)
      const candleOpts = {
        upColor: "#22c55e",
        downColor: "#ef4444",
        borderUpColor: "#22c55e",
        borderDownColor: "#ef4444",
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444",
      };
      const emaOpts = { color: "#facc15", lineWidth: 2, priceLineVisible: false, lastValueVisible: false };
      let candleSeries, emaSeries;
      if (typeof chart.addSeries === "function" && lib.CandlestickSeries) {
        candleSeries = chart.addSeries(lib.CandlestickSeries, candleOpts);
        emaSeries = chart.addSeries(lib.LineSeries, emaOpts);
      } else if (typeof chart.addCandlestickSeries === "function") {
        candleSeries = chart.addCandlestickSeries(candleOpts);
        emaSeries = chart.addLineSeries(emaOpts);
      } else {
        console.error("[LiveChart] lightweight-charts API not detected");
        return;
      }

      chartRef.current = chart;
      seriesRef.current = candleSeries;
      emaSeriesRef.current = emaSeries;
    })();

    return () => {
      disposed = true;
      if (resizeObs) resizeObs.disconnect();
      try { chartRef.current?.remove(); } catch { /* noop */ }
      chartRef.current = null;
      seriesRef.current = null;
      emaSeriesRef.current = null;
    };
  }, []);

  // Push data
  useEffect(() => {
    const series = seriesRef.current;
    const ema = emaSeriesRef.current;
    if (!series || !ema || !candles.length) return;
    const candleData = candles.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }));
    const emaData = candles.filter((c) => c.ema_slow != null).map((c) => ({ time: c.time, value: c.ema_slow }));
    try { series.setData(candleData); } catch { /* race on unmount */ }
    try { ema.setData(emaData); } catch { /* race */ }
  }, [candles]);

  // Markers
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    try { series.setMarkers(markers); } catch { /* noop */ }
  }, [markers]);

  // Líneas de trailing SL / TP / entry (en vivo)
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const removeLine = (ref) => {
      try { if (ref.current) series.removePriceLine(ref.current); } catch { /* noop */ }
      ref.current = null;
    };
    removeLine(slLineRef);
    removeLine(tpLineRef);
    removeLine(entryLineRef);

    if (entryPrice != null) {
      try {
        entryLineRef.current = series.createPriceLine({
          price: Number(entryPrice),
          color: "#22d3ee",
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: "ENTRY",
        });
      } catch { /* noop */ }
    }
    if (trailingSL != null) {
      try {
        slLineRef.current = series.createPriceLine({
          price: Number(trailingSL),
          color: "#ef4444",
          lineWidth: 2,
          lineStyle: 0,
          axisLabelVisible: true,
          title: "SL",
        });
      } catch { /* noop */ }
    }
    if (takeProfit != null) {
      try {
        tpLineRef.current = series.createPriceLine({
          price: Number(takeProfit),
          color: "#22c55e",
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: "TP",
        });
      } catch { /* noop */ }
    }
  }, [trailingSL, takeProfit, entryPrice]);

  return (
    <div className="hud-chart-wrap">
      <div className="hud-chart-header">
        <span className="hud-chart-symbol">{symbol || "—"}</span>
        <span className="hud-chart-meta">
          {candles.length} velas · EMA20 · markers IA
        </span>
      </div>
      <div ref={containerRef} style={{ width: "100%", height }} className="hud-chart-canvas" />
      <RsiPanel candles={candles} rsiMaxA={rsiMaxA} rsiMaxB={rsiMaxB} />
    </div>
  );
}
