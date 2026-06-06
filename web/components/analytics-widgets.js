"use client";
import { useState, useEffect, useCallback } from "react";
import { motion } from "framer-motion";

/* ════════════════════════════════════════════════════════════════════════
   DESIGN TOKENS — premium quant terminal
   ════════════════════════════════════════════════════════════════════════ */
const T = {
  bg:       "#06080d",
  bg2:      "#0a0d14",
  panel:    "#0c1018",
  panel2:   "#10151f",
  border:   "rgba(255,255,255,0.06)",
  borderH:  "rgba(255,255,255,0.14)",
  text:     "#e6ebf2",
  textD:    "#aab1bd",
  mute:     "#5b6473",
  green:    "#22d3a3",
  greenD:   "#0e8f6e",
  red:      "#ff5d6c",
  redD:     "#c43747",
  cyan:     "#62d4ff",
  violet:   "#a78bfa",
  amber:    "#f5c43c",
  blue:     "#5b8cff",
  glow:     "0 0 24px rgba(98,212,255,0.10)",
};

const FONT_MONO = "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace";

/* ════════════════════════════════════════════════════════════════════════
   UTILS
   ════════════════════════════════════════════════════════════════════════ */
const n = (v, d = 2) => (v == null || !Number.isFinite(Number(v))) ? "–" : Number(v).toFixed(d);
const pct = (v, d = 1) => (v == null || !Number.isFinite(Number(v))) ? "–" : `${(Number(v) * 100).toFixed(d)}%`;
const fmtT = ts => {
  if (!ts) return "–";
  const d = new Date(typeof ts === "number" ? (ts > 1e12 ? ts : ts * 1000) : ts);
  return isNaN(d) ? "–" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
};

/* ════════════════════════════════════════════════════════════════════════
   WIDGET 1: SPIKE PROBABILITY
   ════════════════════════════════════════════════════════════════════════ */
export function SpikeProbabilityWidget({ symbol = "CRASH500" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/deriv/analytics/spike-probability?symbol=${symbol}&window=100,200,500`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000); // Poll every 10 seconds
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <WidgetSkeleton title="Spike Probability" />;
  if (error) return <WidgetError title="Spike Probability" error={error} />;

  const { probabilities = {} } = data || {};
  const maxProb = Math.max(...Object.values(probabilities).filter(v => v != null));
  const maxProbWindow = Object.keys(probabilities).find(k => probabilities[k] === maxProb);

  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
      boxShadow: maxProb > 0.7 ? `0 0 20px ${T.green}22` : maxProb > 0.5 ? `0 0 20px ${T.amber}22` : "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.text,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>SPIKE PROBABILITY</span>
        <span style={{
          fontSize: 9,
          color: T.mute,
          fontFamily: FONT_MONO,
        }}>{symbol}</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {Object.entries(probabilities).map(([window, prob]) => (
          <div key={window} style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "6px 8px",
            background: "rgba(255,255,255,0.02)",
            borderRadius: 6,
            border: `1px solid ${T.border}`,
          }}>
            <span style={{ fontSize: 10, color: T.textD, fontFamily: FONT_MONO }}>
              {window} ticks
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{
                width: 60,
                height: 6,
                background: T.bg,
                borderRadius: 3,
                overflow: "hidden",
              }}>
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${prob * 100}%` }}
                  transition={{ duration: 0.8, ease: "easeOut" }}
                  style={{
                    height: "100%",
                    background: prob > 0.7 ? T.green : prob > 0.5 ? T.amber : T.cyan,
                    borderRadius: 3,
                  }}
                />
              </div>
              <span style={{
                fontSize: 12,
                fontWeight: 700,
                color: prob > 0.7 ? T.green : prob > 0.5 ? T.amber : T.cyan,
                fontFamily: FONT_MONO,
                minWidth: 40,
                textAlign: "right",
              }}>
                {pct(prob)}
              </span>
            </div>
          </div>
        ))}
      </div>

      {maxProbWindow && (
        <div style={{
          fontSize: 9,
          color: T.mute,
          textAlign: "center",
          padding: "4px 8px",
          background: "rgba(255,255,255,0.03)",
          borderRadius: 4,
          border: `1px dashed ${T.border}`,
        }}>
          Máxima probabilidad en {maxProbWindow} ticks: <span style={{ color: T.green, fontWeight: 700 }}>{pct(maxProb)}</span>
        </div>
      )}

      <div style={{
        fontSize: 8,
        color: T.mute,
        textAlign: "center",
        borderTop: `1px solid ${T.border}`,
        paddingTop: 6,
      }}>
        Actualizado: {fmtT(data?.timestamp)}
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   WIDGET 2: WAIT Z-SCORE
   ════════════════════════════════════════════════════════════════════════ */
export function WaitZScoreWidget({ symbol = "CRASH500" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/deriv/analytics/wait-zscore?symbol=${symbol}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <WidgetSkeleton title="Wait Z-Score" />;
  if (error) return <WidgetError title="Wait Z-Score" error={error} />;

  const { zscore, mean_wait, std_wait, current_wait, recommendation } = data || {};

  const getZScoreColor = (z) => {
    if (z > 2) return T.red;
    if (z > 1) return T.amber;
    if (z > -1) return T.green;
    if (z > -2) return T.amber;
    return T.red;
  };

  const zscoreColor = zscore != null ? getZScoreColor(zscore) : T.text;

  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
      boxShadow: `0 0 20px ${zscoreColor}22`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.text,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>WAIT Z-SCORE</span>
        <span style={{
          fontSize: 9,
          color: T.mute,
          fontFamily: FONT_MONO,
        }}>{symbol}</span>
      </div>

      <div style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 6,
        padding: "12px",
        background: "rgba(255,255,255,0.03)",
        borderRadius: 8,
        border: `1px solid ${zscoreColor}44`,
      }}>
        <span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>Z-SCORE ACTUAL</span>
        <span style={{
          fontSize: 28,
          fontWeight: 800,
          color: zscoreColor,
          fontFamily: FONT_MONO,
          textShadow: `0 0 12px ${zscoreColor}88`,
        }}>
          {zscore != null ? n(zscore, 2) : "–"}
        </span>
        <span style={{
          fontSize: 10,
          color: zscoreColor,
          fontFamily: FONT_MONO,
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}>
          {recommendation || "–"}
        </span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>MEAN WAIT</div>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.cyan, fontFamily: FONT_MONO }}>
            {mean_wait != null ? `${n(mean_wait, 1)}s` : "–"}
          </div>
        </div>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>STD WAIT</div>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.violet, fontFamily: FONT_MONO }}>
            {std_wait != null ? `${n(std_wait, 1)}s` : "–"}
          </div>
        </div>
      </div>

      <div style={{
        background: "rgba(255,255,255,0.02)",
        borderRadius: 6,
        padding: "8px",
        border: `1px solid ${T.border}`,
      }}>
        <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>CURRENT WAIT</div>
        <div style={{ fontSize: 12, fontWeight: 700, color: T.amber, fontFamily: FONT_MONO }}>
          {current_wait != null ? `${n(current_wait, 1)}s` : "–"}
        </div>
      </div>

      <div style={{
        fontSize: 8,
        color: T.mute,
        textAlign: "center",
        borderTop: `1px solid ${T.border}`,
        paddingTop: 6,
      }}>
        Actualizado: {fmtT(data?.timestamp)}
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   WIDGET 3: SCORE BREAKDOWN
   ════════════════════════════════════════════════════════════════════════ */
export function ScoreBreakdownWidget({ symbol = "CRASH500" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/deriv/analytics/score-breakdown?symbol=${symbol}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <WidgetSkeleton title="Score Breakdown" />;
  if (error) return <WidgetError title="Score Breakdown" error={error} />;

  const { score_breakdown = {}, overall_score } = data || {};
  const components = score_breakdown.components || {};

  const scoreColor = overall_score >= 7 ? T.green : overall_score >= 5 ? T.amber : T.red;

  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
      boxShadow: `0 0 20px ${scoreColor}22`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.text,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>SCORE BREAKDOWN</span>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{
            fontSize: 9,
            color: T.mute,
            fontFamily: FONT_MONO,
          }}>{symbol}</span>
          <span style={{
            fontSize: 14,
            fontWeight: 800,
            color: scoreColor,
            fontFamily: FONT_MONO,
          }}>
            {overall_score != null ? n(overall_score, 1) : "–"}
          </span>
        </div>
      </div>

      <div style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}>
        {Object.entries(components).map(([name, value]) => (
          <div key={name} style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "4px 0",
          }}>
            <span style={{
              fontSize: 9,
              color: T.textD,
              fontFamily: FONT_MONO,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}>
              {name.replace(/_/g, " ")}
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: 8, width: "60%" }}>
              <div style={{
                flex: 1,
                height: 4,
                background: T.bg,
                borderRadius: 2,
                overflow: "hidden",
              }}>
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${(value / 10) * 100}%` }}
                  transition={{ duration: 0.8, ease: "easeOut" }}
                  style={{
                    height: "100%",
                    background: value >= 7 ? T.green : value >= 5 ? T.amber : T.cyan,
                    borderRadius: 2,
                  }}
                />
              </div>
              <span style={{
                fontSize: 10,
                fontWeight: 700,
                color: value >= 7 ? T.green : value >= 5 ? T.amber : T.cyan,
                fontFamily: FONT_MONO,
                minWidth: 24,
                textAlign: "right",
              }}>
                {n(value, 1)}
              </span>
            </div>
          </div>
        ))}
      </div>

      {score_breakdown.regime && (
        <div style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "6px 8px",
          background: "rgba(255,255,255,0.03)",
          borderRadius: 6,
          border: `1px solid ${T.border}`,
        }}>
          <span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>REGIME</span>
          <span style={{
            fontSize: 10,
            fontWeight: 700,
            color: T.violet,
            fontFamily: FONT_MONO,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
          }}>
            {score_breakdown.regime}
          </span>
        </div>
      )}

      <div style={{
        fontSize: 8,
        color: T.mute,
        textAlign: "center",
        borderTop: `1px solid ${T.border}`,
        paddingTop: 6,
      }}>
        Actualizado: {fmtT(data?.timestamp)}
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   WIDGET 4: LIVE BACKTEST
   ════════════════════════════════════════════════════════════════════════ */
export function LiveBacktestWidget({ symbol = "CRASH500" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/deriv/analytics/live-backtest?symbol=${symbol}&lookback=24`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <WidgetSkeleton title="Live Backtest" />;
  if (error) return <WidgetError title="Live Backtest" error={error} />;

  const { metrics = {} } = data || {};
  const upPct = metrics.up_percentage || 0;
  const downPct = metrics.down_percentage || 0;
  const biasColor = upPct > downPct ? T.green : upPct < downPct ? T.red : T.amber;

  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
      boxShadow: `0 0 20px ${biasColor}22`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.text,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>LIVE BACKTEST</span>
        <span style={{
          fontSize: 9,
          color: T.mute,
          fontFamily: FONT_MONO,
        }}>{symbol} · 24h</span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
          textAlign: "center",
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>UP TICKS</div>
          <div style={{ fontSize: 16, fontWeight: 700, color: T.green, fontFamily: FONT_MONO }}>
            {pct(upPct / 100)}
          </div>
          <div style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>
            {metrics.up_ticks || 0} ticks
          </div>
        </div>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
          textAlign: "center",
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>DOWN TICKS</div>
          <div style={{ fontSize: 16, fontWeight: 700, color: T.red, fontFamily: FONT_MONO }}>
            {pct(downPct / 100)}
          </div>
          <div style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>
            {metrics.down_ticks || 0} ticks
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>AVG ΔT</div>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.cyan, fontFamily: FONT_MONO }}>
            {metrics.avg_time_diff_sec != null ? `${n(metrics.avg_time_diff_sec, 2)}s` : "–"}
          </div>
        </div>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>STD ΔT</div>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.violet, fontFamily: FONT_MONO }}>
            {metrics.std_time_diff_sec != null ? `${n(metrics.std_time_diff_sec, 2)}s` : "–"}
          </div>
        </div>
      </div>

      <div style={{
        background: "rgba(255,255,255,0.02)",
        borderRadius: 6,
        padding: "8px",
        border: `1px solid ${T.border}`,
      }}>
        <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>PRICE RANGE</div>
        <div style={{ fontSize: 12, fontWeight: 700, color: T.amber, fontFamily: FONT_MONO }}>
          {metrics.price_range != null ? `$${n(metrics.price_range, 2)}` : "–"}
        </div>
        <div style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>
          {metrics.min_price != null ? `Min: $${n(metrics.min_price, 2)}` : ""} 
          {metrics.max_price != null ? ` · Max: $${n(metrics.max_price, 2)}` : ""}
        </div>
      </div>

      <div style={{
        fontSize: 8,
        color: T.mute,
        textAlign: "center",
        borderTop: `1px solid ${T.border}`,
        paddingTop: 6,
      }}>
        {metrics.total_ticks != null ? `${metrics.total_ticks} ticks analizados · ` : ""}
        Actualizado: {fmtT(data?.timestamp)}
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   WIDGET 5: RARITY PERCENTILE
   ════════════════════════════════════════════════════════════════════════ */
export function RarityPercentileWidget({ symbol = "CRASH500" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/deriv/analytics/rarity-percentile?symbol=${symbol}&lookback=72`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <WidgetSkeleton title="Rarity Percentile" />;
  if (error) return <WidgetError title="Rarity Percentile" error={error} />;

  const { metrics = {} } = data || {};
  const percentile = metrics.price_change_percentile || 0;
  const rarity = metrics.rarity_category || "UNKNOWN";
  
  const getRarityColor = (cat) => {
    switch(cat) {
      case "EXTREME_RARE": return T.red;
      case "RARE": return T.amber;
      case "COMMON": return T.cyan;
      case "VERY_COMMON": return T.green;
      default: return T.mute;
    }
  };
  
  const rarityColor = getRarityColor(rarity);

  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
      boxShadow: `0 0 20px ${rarityColor}22`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.text,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>RARITY PERCENTILE</span>
        <span style={{
          fontSize: 9,
          color: T.mute,
          fontFamily: FONT_MONO,
        }}>{symbol} · 72h</span>
      </div>

      <div style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 6,
        padding: "12px",
        background: "rgba(255,255,255,0.03)",
        borderRadius: 8,
        border: `1px solid ${rarityColor}44`,
      }}>
        <span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>PERCENTIL ACTUAL</span>
        <span style={{
          fontSize: 28,
          fontWeight: 800,
          color: rarityColor,
          fontFamily: FONT_MONO,
          textShadow: `0 0 12px ${rarityColor}88`,
        }}>
          {pct(percentile, 0)}
        </span>
        <span style={{
          fontSize: 10,
          color: rarityColor,
          fontFamily: FONT_MONO,
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}>
          {rarity.replace(/_/g, " ")}
        </span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>AVG ΔPRICE</div>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.cyan, fontFamily: FONT_MONO }}>
            {metrics.avg_price_change != null ? `$${n(metrics.avg_price_change, 4)}` : "–"}
          </div>
        </div>
        <div style={{
          background: "rgba(255,255,255,0.02)",
          borderRadius: 6,
          padding: "8px",
          border: `1px solid ${T.border}`,
        }}>
          <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>STD ΔPRICE</div>
          <div style={{ fontSize: 12, fontWeight: 700, color: T.violet, fontFamily: FONT_MONO }}>
            {metrics.std_price_change != null ? `$${n(metrics.std_price_change, 4)}` : "–"}
          </div>
        </div>
      </div>

      <div style={{
        background: "rgba(255,255,255,0.02)",
        borderRadius: 6,
        padding: "8px",
        border: `1px solid ${T.border}`,
      }}>
        <div style={{ fontSize: 8, color: T.mute, marginBottom: 2 }}>PERCENTILES HISTÓRICOS</div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, fontFamily: FONT_MONO }}>
          <span style={{ color: T.green }}>P50: ${n(metrics.p50_price_change, 4)}</span>
          <span style={{ color: T.amber }}>P75: ${n(metrics.p75_price_change, 4)}</span>
          <span style={{ color: T.red }}>P90: ${n(metrics.p90_price_change, 4)}</span>
        </div>
      </div>

      <div style={{
        fontSize: 8,
        color: T.mute,
        textAlign: "center",
        borderTop: `1px solid ${T.border}`,
        paddingTop: 6,
      }}>
        {metrics.total_ticks != null ? `${metrics.total_ticks} ticks en distribución · ` : ""}
        Actualizado: {fmtT(data?.timestamp)}
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   HELPER COMPONENTS
   ════════════════════════════════════════════════════════════════════════ */
function WidgetSkeleton({ title }) {
  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.textD,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>{title}</span>
        <motion.div
          animate={{ opacity: [0.4, 0.8, 0.4] }}
          transition={{ duration: 1.5, repeat: Infinity }}
          style={{
            width: 40,
            height: 12,
            background: T.border,
            borderRadius: 3,
          }}
        />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {[1, 2, 3].map(i => (
          <motion.div
            key={i}
            animate={{ opacity: [0.3, 0.6, 0.3] }}
            transition={{ duration: 1.5, repeat: Infinity, delay: i * 0.2 }}
            style={{
              height: 20,
              background: T.border,
              borderRadius: 4,
            }}
          />
        ))}
      </div>
    </div>
  );
}

function WidgetError({ title, error }) {
  return (
    <div style={{
      background: T.panel,
      border: `1px solid ${T.red}44`,
      borderRadius: 10,
      padding: "14px",
      display: "flex",
      flexDirection: "column",
      gap: 8,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{
          fontSize: 11,
          fontWeight: 700,
          color: T.red,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          fontFamily: FONT_MONO,
        }}>{title}</span>
        <span style={{
          fontSize: 9,
          color: T.mute,
          fontFamily: FONT_MONO,
        }}>ERROR</span>
      </div>
      <div style={{
        fontSize: 9,
        color: T.textD,
        fontFamily: FONT_MONO,
        padding: "6px 8px",
        background: "rgba(255,255,255,0.02)",
        borderRadius: 4,
        border: `1px dashed ${T.red}44`,
      }}>
        {error.length > 60 ? `${error.substring(0, 60)}...` : error}
      </div>
      <button
        onClick={() => window.location.reload()}
        style={{
          background: `${T.red}22`,
          color: T.red,
          border: `1px solid ${T.red}44`,
          borderRadius: 5,
          padding: "4px 8px",
          cursor: "pointer",
          fontFamily: FONT_MONO,
          fontSize: 9,
          fontWeight: 700,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          alignSelf: "flex-end",
        }}
      >
        RETRY
      </button>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   ANALYTICS GRID COMPONENT
   ════════════════════════════════════════════════════════════════════════ */
export function AnalyticsGrid({ symbol = "CRASH500" }) {
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
      gap: 14,
      width: "100%",
    }}>
      <SpikeProbabilityWidget symbol={symbol} />
      <WaitZScoreWidget symbol={symbol} />
      <ScoreBreakdownWidget symbol={symbol} />
      <LiveBacktestWidget symbol={symbol} />
      <RarityPercentileWidget symbol={symbol} />
    </div>
  );
}