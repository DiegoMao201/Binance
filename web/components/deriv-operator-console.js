"use client";
import { useState, useEffect, useRef, useCallback } from "react";

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

/* ── live confirmation strip inside a symbol card ────────── */
function LiveScore({ live }) {
  if (!live) return null;
  const k = KIND[live.kind] || KIND.INFO;
  const score = live.score;
  const gate = live.gate;
  const gap = live.scoreGap; // gate - score; <=0 means confirmed
  const pctToGate = gate && score != null ? Math.max(0, Math.min(100, (score / gate) * 100)) : null;
  const barColor = gap != null && gap <= 0 ? T.green : gap != null && gap < 0.4 ? T.amber : T.cyan;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 7, padding: "9px 12px", background: T.bg2, borderTop: `1px solid ${T.border}`, borderBottom: `1px solid ${T.border}` }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Confirmación en vivo</span>
        <span style={{
          fontFamily: FONT_MONO, fontSize: 9.5, fontWeight: 800, color: k.color,
          background: k.color + "18", border: `1px solid ${k.color}44`, borderRadius: 4, padding: "2px 7px",
        }}>{k.label}</span>
      </div>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 12 }}>
        <div>
          <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Score</div>
          <div style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800, color: barColor, lineHeight: 1 }}>{num(score)}</div>
        </div>
        <div>
          <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Necesita</div>
          <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 700, color: T.textD }}>≥ {num(gate)}</div>
        </div>
        <div style={{ marginLeft: "auto", textAlign: "right" }}>
          <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Falta</div>
          <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 800, color: gap != null && gap <= 0 ? T.green : T.amber }}>
            {gap == null ? "–" : gap <= 0 ? "LISTO ✓" : `+${num(gap)}`}
          </div>
        </div>
      </div>
      {pctToGate != null && (
        <div style={{ height: 5, borderRadius: 3, background: T.bg, overflow: "hidden" }}>
          <div style={{ height: "100%", width: `${pctToGate}%`, background: barColor, transition: "width 500ms ease" }} />
        </div>
      )}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD }}>régimen <b style={{ color: T.text }}>{live.regime || "–"}</b></span>
        <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD }}>dir <b style={{ color: sideColor(live.side) }}>{sideLabel(live.side)}</b></span>
        <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD }}>Hurst <b style={{ color: T.text }}>{num(live.hurst, 3)}</b></span>
        <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD }}>vol <b style={{ color: T.text }}>{live.volRegime || "–"}</b></span>
      </div>
    </div>
  );
}

/* ── symbol card ─────────────────────────────────────────── */
function SymbolCard({ s }) {
  const ready = s.readiness || { state: "SIN_DATOS", level: 0 };
  const rc = READY_COLOR[ready.state] || T.mute;
  const dir = s.lastSpike?.direction;
  const dirColor = dir === "DOWN" ? T.red : dir === "UP" ? T.green : T.textD;
  const hasOpen = !!s.openContract;
  const ticks = s.live?.ticksSinceSpike ?? s.lastSpike?.ticks_since_last_spike ?? null;

  return (
    <div style={{
      background: T.panel, borderRadius: 10,
      border: `1px solid ${hasOpen ? T.cyan + "66" : rc + "44"}`,
      boxShadow: ready.level >= 4 ? `0 0 22px ${rc}22` : "none",
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {/* header */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "10px 12px", background: T.panel2, borderBottom: `1px solid ${T.border}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{ width: 9, height: 9, borderRadius: "50%", background: rc,
            boxShadow: `0 0 8px ${rc}`, animation: ready.level >= 4 ? "pulse 1.1s infinite" : "none" }} />
          <span style={{ fontFamily: FONT_MONO, fontWeight: 800, fontSize: 15, color: T.text, letterSpacing: "0.04em" }}>{s.symbol}</span>
        </div>
        <span style={{
          fontFamily: FONT_MONO, fontSize: 9.5, fontWeight: 800, color: rc,
          background: rc + "18", border: `1px solid ${rc}44`, borderRadius: 5,
          padding: "3px 7px", letterSpacing: "0.06em",
        }}>{READY_LABEL[ready.state]}</span>
      </div>

      {/* open position banner */}
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

      {/* LIVE confirmation/score */}
      <LiveScore live={s.live} />

      {/* last spike — TIME + TICKS */}
      <div style={{ padding: "11px 12px", display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Sin spike hace</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 21, fontWeight: 800, color: rc, lineHeight: 1.1 }}>
              {ago(s.secsSinceLastSpike)}
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD, marginTop: 2 }}>
              último {fmtClock(s.lastSpike?.ts)} · <span style={{ color: dirColor }}>{dir || "–"}</span> · {num(s.lastSpike?.ratio, 0)}x
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Ticks sin spike</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 21, fontWeight: 800, color: T.cyan, lineHeight: 1.1 }}>{intC(ticks)}</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, marginTop: 2 }}>típico {dur(s.medianGapSec)} · p75 {dur(s.p75GapSec)}</div>
          </div>
        </div>

        {/* readiness bar */}
        <div style={{ height: 6, borderRadius: 4, background: T.bg, overflow: "hidden" }}>
          <div style={{
            height: "100%", borderRadius: 4, background: rc,
            width: `${Math.min(100, ((s.secsSinceLastSpike || 0) / (s.medianGapSec || 1)) * 60)}%`,
            transition: "width 600ms ease",
          }} />
        </div>

        {/* cadence grid */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, marginTop: 2 }}>
          <Stat label="1h" value={s.counts?.h1 ?? "–"} color={T.text} />
          <Stat label="6h" value={s.counts?.h6 ?? "–"} color={T.textD} />
          <Stat label="12h" value={s.counts?.h12 ?? "–"} color={T.textD} />
          <Stat label="24h" value={s.counts?.h24 ?? "–"} color={T.textD} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8 }}>
          <Stat label="prom/h 6h" value={num(s.ratePerHour?.h6, 1)} color={T.cyan} />
          <Stat label="prom/h 12h" value={num(s.ratePerHour?.h12, 1)} color={T.cyan} />
          <Stat label="prom/h 24h" value={num(s.ratePerHour?.h24, 1)} color={T.cyan} />
        </div>

        {/* notes per symbol */}
        {s.topReasons?.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, paddingTop: 6, borderTop: `1px solid ${T.border}` }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Notas (por qué el bot no entró)</span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
              {s.topReasons.map((r) => (
                <span key={r.reason} style={{
                  fontFamily: FONT_MONO, fontSize: 9.5, color: T.amber,
                  background: T.amber + "12", border: `1px solid ${T.amber}33`,
                  borderRadius: 4, padding: "2px 6px",
                }}>{r.reason} ×{r.n}</span>
              ))}
            </div>
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

  const symbols = data?.symbols || [];
  const confFeed = data?.confirmationFeed || [];
  const spikeTable = data?.spikeTable || [];
  const sess = data?.session || {};
  const totals = data?.totals || {};
  const symNames = symbols.map((s) => s.symbol);

  const tableRows = symFilter === "ALL" ? spikeTable : spikeTable.filter((s) => s.symbol === symFilter);
  const feedRows = symFilter === "ALL" ? confFeed : confFeed.filter((s) => s.symbol === symFilter);

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
      </div>

      {/* CONFIRMATION FEED */}
      <div style={{ marginBottom: 14 }}>
        <Panel title="Confirmaciones de posibles spikes · en vivo" accent={T.amber}
          right={
            <div style={{ display: "flex", gap: 5 }}>
              {["ALL", ...symNames].map((sn) => (
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
    </div>
  );
}
