"use client";
import { useState, useEffect, useRef, useCallback } from "react";

/* ════════════════════════════════════════════════════════════════════════
   CONSOLA DE OPERACIÓN MANUAL — Deriv CRASH
   Reúne en una sola pantalla todo lo que el bot calcula para que el
   operador humano decida en caliente: spikes en vivo, tiempo desde el
   último spike, cadencia (1h/6h/12h/24h), semáforo "loaded gun" por
   símbolo, operaciones abiertas del bot y log de entradas/salidas.
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

/* ── helpers ─────────────────────────────────────────────── */
const num = (v, d = 2) => (v == null || !Number.isFinite(Number(v)) ? "–" : Number(v).toFixed(d));
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
          padding: "9px 12px", borderBottom: `1px solid ${T.border}`,
          background: T.panel2,
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

/* ── symbol card ─────────────────────────────────────────── */
function SymbolCard({ s }) {
  const ready = s.readiness || { state: "SIN_DATOS", level: 0 };
  const rc = READY_COLOR[ready.state] || T.mute;
  const dir = s.lastSpike?.direction;
  const dirColor = dir === "DOWN" ? T.red : dir === "UP" ? T.green : T.textD;
  const hasOpen = !!s.openContract;

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

      {/* last spike + elapsed */}
      <div style={{ padding: "11px 12px", display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Último spike</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800, color: rc, lineHeight: 1.1 }}>
              hace {ago(s.secsSinceLastSpike)}
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD, marginTop: 2 }}>
              {fmtClock(s.lastSpike?.ts)} · <span style={{ color: dirColor }}>{dir || "–"}</span> · ratio {num(s.lastSpike?.ratio, 1)}x
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Intervalo típico</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 700, color: T.textD }}>{dur(s.medianGapSec)}</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>p75 {dur(s.p75GapSec)}</div>
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

        {/* bot capture stats */}
        <div style={{ display: "flex", gap: 14, paddingTop: 6, borderTop: `1px solid ${T.border}` }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.green }}>● {s.entered24} tomados</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.red }}>● {s.blocked24} bloqueados</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD, marginLeft: "auto" }}>
            captura {s.catchRate24 == null ? "–" : `${Math.round(s.catchRate24 * 100)}%`}
          </span>
        </div>

        {/* notes per symbol (top block reasons) */}
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
  const entries = data?.recentEntries || [];
  const feed = data?.spikeFeed || [];
  const sess = data?.session || {};
  const totals = data?.totals || {};

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
            Todas las señales del bot en una pantalla. Tú operas. {" "}
            {lastUpdate && <span style={{ color: T.green }}>● en vivo · {fmtClock(lastUpdate / 1000)}</span>}
            {err && <span style={{ color: T.red }}> ● {err}</span>}
          </p>
        </div>
        <div style={{ display: "flex", gap: 18, alignItems: "center" }}>
          <Stat label="Sesión PnL" value={`${sess.realizedPnl >= 0 ? "+" : ""}${num(sess.realizedPnl)}`} color={pnlColor(sess.realizedPnl)} />
          <Stat label="Trades" value={sess.trades ?? "–"} color={T.text} />
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
        display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
        gap: 12, marginBottom: 14,
      }}>
        {symbols.map((s) => <SymbolCard key={s.symbol} s={s} />)}
      </div>

      {/* BOTTOM: spike feed + operations log */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.25fr", gap: 12 }}>
        {/* LIVE SPIKE FEED */}
        <Panel title="Feed de spikes en vivo" accent={T.cyan}
          right={<span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>{feed.length} recientes</span>}>
          <div style={{ maxHeight: 360, overflowY: "auto", display: "flex", flexDirection: "column", gap: 5 }}>
            {feed.length === 0 && <div style={{ color: T.mute, textAlign: "center", padding: 20 }}>Sin spikes</div>}
            {feed.map((f, i) => {
              const dc = f.direction === "DOWN" ? T.red : T.green;
              const ok = f.bot_entered === true;
              return (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 9, padding: "6px 8px",
                  background: T.bg2, borderRadius: 6, borderLeft: `3px solid ${dc}`,
                }}>
                  <span style={{ fontSize: 10, color: T.mute, width: 64 }}>{fmtClock(f.ts)}</span>
                  <span style={{ fontSize: 11, fontWeight: 800, color: T.text, width: 78 }}>{f.symbol}</span>
                  <span style={{ fontSize: 10, color: dc, width: 46 }}>{f.direction}</span>
                  <span style={{ fontSize: 10, color: T.amber, width: 52 }}>{num(f.ratio, 0)}x</span>
                  <span style={{ fontSize: 9.5, color: T.mute, marginLeft: "auto" }}>hace {ago(f.secsAgo)}</span>
                  <span style={{
                    fontSize: 9, fontWeight: 800, padding: "2px 6px", borderRadius: 4,
                    color: ok ? T.green : T.red, background: (ok ? T.green : T.red) + "16",
                    minWidth: 64, textAlign: "center",
                  }}>{ok ? "TOMADO" : "no entró"}</span>
                </div>
              );
            })}
          </div>
        </Panel>

        {/* OPERATIONS LOG */}
        <Panel title="Operaciones del bot · entradas y salidas" accent={T.violet}
          right={<span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>{entries.length}</span>}>
          <div style={{ maxHeight: 360, overflowY: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
              <thead>
                <tr style={{ color: T.mute, textAlign: "left" }}>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase" }}>Estado</th>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase" }}>Símbolo</th>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase" }}>Lado</th>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase" }}>Entró</th>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase" }}>Dur</th>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase" }}>Motivo salida</th>
                  <th style={{ padding: "4px 6px", fontSize: 9, textTransform: "uppercase", textAlign: "right" }}>PnL</th>
                </tr>
              </thead>
              <tbody>
                {entries.length === 0 && (
                  <tr><td colSpan={7} style={{ color: T.mute, textAlign: "center", padding: 20 }}>Sin operaciones</td></tr>
                )}
                {entries.map((e, i) => {
                  const isOpen = e.status === "OPEN";
                  const pnl = isOpen ? e.floating_pnl : e.realized_pnl;
                  return (
                    <tr key={i} style={{ borderBottom: `1px solid ${T.border}` }}>
                      <td style={{ padding: "5px 6px" }}>
                        <span style={{
                          fontSize: 9, fontWeight: 800, padding: "2px 6px", borderRadius: 4,
                          color: isOpen ? T.cyan : T.textD,
                          background: (isOpen ? T.cyan : T.mute) + "18",
                        }}>{isOpen ? "ABIERTA" : "cerrada"}</span>
                      </td>
                      <td style={{ padding: "5px 6px", fontWeight: 700, color: T.text }}>{e.symbol}</td>
                      <td style={{ padding: "5px 6px", color: sideColor(e.side) }}>{sideLabel(e.side)}</td>
                      <td style={{ padding: "5px 6px", color: T.textD }}>{fmtClock(e.opened_at_ts)}</td>
                      <td style={{ padding: "5px 6px", color: T.textD }}>{dur(e.duration_sec)}</td>
                      <td style={{ padding: "5px 6px", color: T.mute, fontSize: 10 }}>{isOpen ? "—" : (e.exit_reason || "–")}</td>
                      <td style={{ padding: "5px 6px", textAlign: "right", fontWeight: 800, color: pnlColor(pnl) }}>
                        {pnl == null ? "–" : `${Number(pnl) >= 0 ? "+" : ""}${num(pnl)}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}
