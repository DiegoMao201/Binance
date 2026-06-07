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

/* ── Q&A row ─────────────────────────────────────────────── */
function QARow({ q, a, aColor }) {
  const col = aColor || T.text;
  return (
    <div style={{
      display: "flex", justifyContent: "space-between", alignItems: "center",
      gap: 8, padding: "8px 0", borderBottom: `1px solid ${T.border}`,
    }}>
      <span style={{ fontFamily: FONT_MONO, fontSize: 12, color: T.textD }}>{q}</span>
      <span style={{
        fontFamily: FONT_MONO, fontSize: 11, fontWeight: 700, color: col, whiteSpace: "nowrap",
        background: col + "18", border: `1px solid ${col}33`,
        borderRadius: 20, padding: "3px 10px",
      }}>{a}</span>
    </div>
  );
}

/* ── symbol card ─────────────────────────────────────────── */
function SymbolCard({ s }) {
  const [analytics, setAnalytics] = useState(null);
  useEffect(() => {
    const sym = s.symbol;
    if (!sym) return;
    let active = true;
    const doFetch = async () => {
      try {
        const res = await fetch(`/api/deriv/analytics/market-context?symbol=${sym}`);
        if (!active) return;
        const data = res.ok ? await res.json() : null;
        setAnalytics(data);
      } catch { /* noop */ }
    };
    doFetch();
    const id = setInterval(doFetch, 15000);
    return () => { active = false; clearInterval(id); };
  }, [s.symbol]);

  /* ─ data ─ */
  const secs    = Number(s.secsSinceLastSpike) || 0;
  const p75Sec  = Number(s.p75GapSec) || null;
  const medSec  = Number(s.medianGapSec) || null;
  const ticks   = s.live?.ticksSinceSpike ?? s.lastSpike?.ticks_since_last_spike ?? null;
  const recent  = Number(s.counts?.h1) || 0;
  const cluster = analytics?.snapshot?.spike_cluster_active ?? false;
  const hasOpen = !!s.openContract;
  const hurst   = analytics?.snapshot?.hurst ?? null;
  const hurstHistory = analytics?.hurst_history ?? [];

  const isInactive    = s.topReasons?.some(r => r.reason === "dynamic_symbol_inactive")
                      || s.readiness?.state === "SIN_DATOS";
  const hasChaseGuard = s.topReasons?.some(r =>
    r.reason?.includes("chase") || r.reason?.includes("post_spike_chase"));
  const clusterDone   = recent >= 3 && !cluster;

  /* ─ semáforo ─ */
  let sem;
  if (isInactive || clusterDone) sem = "red";
  else if (secs > 0 && p75Sec && secs > p75Sec && !hasChaseGuard) sem = "green";
  else sem = "amber";

  const SEM = {
    green: { label: "Lista para entrar", dot: T.green },
    amber: { label: "Observar",          dot: T.amber },
    red:   { label: isInactive ? "Pausado" : "No entrar ahora", dot: isInactive ? T.mute : T.red },
  };
  const S = SEM[sem];

  /* ─ main message ─ */
  const minsSince = secs > 0 ? Math.round(secs / 60) : null;
  const medMins   = medSec ? Math.round(medSec / 60) : null;
  let msgEmoji, msgTitle, msgBody;

  if (isInactive) {
    msgEmoji = "⛔"; msgTitle = "El bot lo tiene pausado";
    msgBody = "El sistema automático decidió pausar este símbolo temporalmente. Puedes operar manual, pero las condiciones no son favorables ahora mismo.";
  } else if (clusterDone) {
    msgEmoji = "🚨"; msgTitle = "Demasiado pronto — el mercado se agotó";
    msgBody = `Cayó ${recent} veces seguidas hace poco. Cuando pasa eso, el mercado descansa y las siguientes caídas tardan el doble de lo normal. Espera${medMins ? ` al menos ${medMins} min` : " un buen rato"} antes de entrar.`;
  } else if (sem === "amber" && ticks !== null && ticks < 120) {
    msgEmoji = "⏳"; msgTitle = "Acaba de caer — espera un momento";
    msgBody = "Hace poco ocurrió una caída fuerte. A veces viene otra rápido. Espera 2 min viendo la pantalla. Si el precio sube y vuelve a bajar rápido, ese es el momento.";
  } else if (sem === "green") {
    const hurstNote = hurst != null && hurst < 0.45 ? " y el Hurst está bajando — el mercado perdió fuerza" : "";
    msgEmoji = "✅"; msgTitle = "Buena ventana — está cargada";
    msgBody = `Llevan casi ${minsSince != null ? minsSince + " min" : "varios minutos"} sin caída${hurstNote}. Las probabilidades están a tu favor ahora.`;
  } else {
    const pct = p75Sec && secs ? Math.round((secs / p75Sec) * 100) : null;
    msgEmoji = "⏳"; msgTitle = "Acumulando energía";
    msgBody = `El mercado está acumulando energía.${pct != null ? ` Ya pasó el ${pct}% del tiempo típico entre caídas.` : ""} En unos minutos podría estar lista la próxima oportunidad.`;
  }

  /* ─ Hurst visualization ─ */
  const hurstColor = hurst == null ? T.mute : hurst < 0.45 ? T.green : hurst < 0.55 ? T.amber : T.red;
  const hurstLabel = hurst == null ? "–" : hurst < 0.45 ? "sin fuerza" : hurst < 0.55 ? "neutro" : "con fuerza";
  // map hurst [0.30, 0.70] → [0%, 100%] for bar position
  const hurstPos   = hurst != null ? Math.max(2, Math.min(98, ((hurst - 0.30) / 0.40) * 100)) : 50;
  const hurstBarColor = (h) => h < 0.45 ? T.green : h < 0.55 ? T.amber : T.red;

  /* ─ Q&A answers ─ */
  const z = analytics?.spike_stats?.z_score;
  let energyLabel, energyColor;
  if (clusterDone)    { energyLabel = "No · se agotó";   energyColor = T.red; }
  else if (z == null) { energyLabel = "Calculando…";      energyColor = T.mute; }
  else if (z > 1.2)   { energyLabel = "Sí, hay energía"; energyColor = T.green; }
  else if (z > 0.2)   { energyLabel = "Puede que sí";    energyColor = T.amber; }
  else                { energyLabel = "Todavía poca";     energyColor = T.mute; }

  let botLabel, botColor;
  if (isInactive)                                                   { botLabel = "No · desactivado"; botColor = T.mute; }
  else if (hasChaseGuard)                                           { botLabel = "No · esperando";   botColor = T.amber; }
  else if (s.live?.kind === "CONFIRMADO" || s.live?.scoreGap <= 0) { botLabel = "¡Sí! · entrando";  botColor = T.green; }
  else if (s.live?.scoreGap != null && s.live.scoreGap < 1)        { botLabel = `Casi · falta ${num(s.live.scoreGap, 1)}`; botColor = T.amber; }
  else if (s.live)                                                  { botLabel = "No · falta score"; botColor = T.red; }
  else                                                              { botLabel = "Evaluando…";        botColor = T.mute; }

  /* ─ progress bar ─ */
  const acumPct  = medSec && secs ? Math.min(100, Math.round((secs / medSec) * 100)) : 0;
  const barColor = sem === "green" ? T.green : sem === "red" ? T.red : T.amber;
  const barSuffix = acumPct < 40 ? "· aún muy pronto"
    : acumPct < 75  ? "· zona de espera"
    : acumPct < 100 ? "· acercándose a la zona"
    : "· zona óptima";
  const barText = ticks !== null && ticks < 120
    ? "Ventana de oportunidad: primeros minutos después de la caída"
    : clusterDone
    ? `Solo el ${acumPct}% del tiempo típico ha pasado desde el cluster`
    : `Acumulación: ${acumPct}% del tiempo típico pasó ${barSuffix}`;

  /* ─ dots ─ */
  const TOTAL   = 7;
  const filled  = Math.min(Math.max(0, recent), TOTAL);
  const hasPoss = sem !== "red" && filled < TOTAL;
  const dots    = Array.from({ length: TOTAL }, (_, i) => {
    if (i < filled)                  return "filled";
    if (i === filled && hasPoss)     return "possible";
    return "empty";
  });
  const dotText = filled === 0         ? "Sin caídas recientes en esta hora"
    : filled === 1                     ? "Caída sola · la siguiente se acerca"
    : clusterDone                      ? `${filled} seguidas · ya se descargó`
    : `${filled} seguidas · posible una más`;

  const symDisplay = (s.symbol || "").replace(/^CRASH/, "CRASH ");

  return (
    <div style={{
      background: T.panel, borderRadius: 14,
      border: `1px solid ${S.dot}55`,
      boxShadow: `0 0 20px ${S.dot}14`,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>

      {/* posición abierta */}
      {hasOpen && (
        <div style={{
          padding: "7px 14px", background: T.cyan + "14", borderBottom: `1px solid ${T.cyan}33`,
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 700, color: T.cyan }}>
            ● POSICIÓN ABIERTA · {sideLabel(s.openContract.side)} · {dur(s.openContract.duration_sec)}
          </span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 13, fontWeight: 800, color: pnlColor(s.openContract.floating_pnl) }}>
            {Number(s.openContract.floating_pnl) >= 0 ? "+" : ""}{num(s.openContract.floating_pnl)} USDT
          </span>
        </div>
      )}

      {/* header — nombre + badge */}
      <div style={{
        display: "flex", alignItems: "flex-start", justifyContent: "space-between",
        padding: "16px 16px 10px", background: T.panel2, borderBottom: `1px solid ${T.border}`,
      }}>
        <div>
          <div style={{ fontFamily: FONT_MONO, fontWeight: 800, fontSize: 20, color: T.text, letterSpacing: "0.03em", lineHeight: 1 }}>
            {symDisplay}
          </div>
          <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.mute, marginTop: 3 }}>Índice sintético · baja</div>
        </div>
        <div style={{
          display: "flex", alignItems: "center", gap: 5,
          background: S.dot + "18", border: `1px solid ${S.dot}44`,
          borderRadius: 20, padding: "5px 12px",
        }}>
          <div style={{
            width: 7, height: 7, borderRadius: "50%", background: S.dot,
            boxShadow: sem === "green" ? `0 0 6px ${S.dot}` : "none",
            animation: sem === "green" ? "pulse 1.6s infinite" : "none",
          }} />
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, fontWeight: 700, color: S.dot, letterSpacing: "0.04em" }}>
            {S.label}
          </span>
        </div>
      </div>

      {/* caja de decisión — explicación en español coloquial */}
      <div style={{
        margin: "12px 14px 10px",
        background: S.dot + "14", border: `1px solid ${S.dot}33`,
        borderRadius: 10, padding: "12px 14px",
      }}>
        <div style={{ fontFamily: FONT_MONO, fontWeight: 800, fontSize: 13, color: S.dot, marginBottom: 5 }}>
          {msgEmoji} {msgTitle}
        </div>
        <div style={{ fontFamily: FONT_MONO, fontSize: 11.5, lineHeight: 1.6, color: T.textD }}>{msgBody}</div>
      </div>

      {/* ══ HURST — fuerza del mercado ══ */}
      <div style={{ padding: "10px 14px 12px", borderBottom: `1px solid ${T.border}` }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
          <span style={{
            fontFamily: FONT_MONO, fontSize: 9, fontWeight: 700, color: T.mute,
            letterSpacing: "0.10em", textTransform: "uppercase",
          }}>Fuerza del mercado (Hurst)</span>
          <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 26, fontWeight: 800, color: hurstColor, lineHeight: 1 }}>
              {hurst != null ? hurst.toFixed(3) : "–"}
            </span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 11, fontWeight: 600, color: hurstColor }}>{hurstLabel}</span>
          </div>
        </div>

        {/* gradient bar + marker */}
        <div style={{ position: "relative", height: 10, marginBottom: 6 }}>
          <div style={{
            height: "100%", borderRadius: 99,
            background: `linear-gradient(to right, ${T.green} 0%, ${T.amber} 50%, ${T.red} 100%)`,
          }} />
          {hurst != null && (
            <div style={{
              position: "absolute", top: "50%", transform: "translate(-50%, -50%)",
              left: `${hurstPos}%`,
              width: 16, height: 16, borderRadius: "50%",
              background: T.panel, border: `2.5px solid ${hurstColor}`,
              boxShadow: `0 0 8px ${hurstColor}99`,
            }} />
          )}
        </div>

        {/* bar labels */}
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.green }}>sin fuerza · entra</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>neutro</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.red }}>con fuerza · espera</span>
        </div>

        {/* hurst history bars */}
        {hurstHistory.length > 0 && (
          <div style={{ display: "flex", gap: 3, alignItems: "flex-end" }}>
            {hurstHistory.map((h, i) => {
              const isLast = i === hurstHistory.length - 1;
              const hc = hurstBarColor(h);
              return (
                <div key={i} style={{
                  flex: 1, height: 18, borderRadius: 4,
                  background: hc + (isLast ? "ff" : "77"),
                  border: isLast ? `1.5px solid ${hc}` : "none",
                  boxShadow: isLast ? `0 0 6px ${hc}` : "none",
                }} />
              );
            })}
          </div>
        )}
      </div>

      {/* ══ 3 stats rápidos ══ */}
      <div style={{
        display: "grid", gridTemplateColumns: "1fr 1fr 1fr",
        gap: 0, borderBottom: `1px solid ${T.border}`,
      }}>
        {[
          { label: "Sin caída hace", value: secs > 0 ? ago(secs) : "–", color: T.amber },
          { label: "Ticks acum.", value: ticks != null ? intC(ticks) : "–", color: T.text },
          { label: "Spikes / hora", value: recent > 0 ? `#${recent}` : "–", color: T.cyan },
        ].map(({ label, value, color }, i) => (
          <div key={i} style={{
            padding: "10px 0", textAlign: "center",
            borderRight: i < 2 ? `1px solid ${T.border}` : "none",
          }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 4 }}>{label}</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 18, fontWeight: 800, color, lineHeight: 1 }}>{value}</div>
          </div>
        ))}
      </div>

      {/* ══ 3 preguntas humanas ══ */}
      <div style={{ padding: "4px 14px 0" }}>
        <QARow q="¿Cuánto tiempo sin caída?" a={secs > 0 ? ago(secs) : "–"} />
        <QARow q="¿Tiene energía el mercado?" a={energyLabel} aColor={energyColor} />
        <QARow q="¿Puede entrar el bot ahora?" a={botLabel} aColor={botColor} />
      </div>

      {/* ══ puntitos — últimas caídas seguidas ══ */}
      <div style={{ padding: "12px 14px 6px" }}>
        <div style={{
          fontFamily: FONT_MONO, fontSize: 9, fontWeight: 700, color: T.mute,
          letterSpacing: "0.10em", textTransform: "uppercase", marginBottom: 8,
        }}>
          Últimas caídas seguidas
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <div style={{ display: "flex", gap: 4 }}>
            {dots.map((d, i) => (
              <div key={i} style={{
                width: 13, height: 13, borderRadius: "50%",
                background: d === "filled" ? S.dot : "transparent",
                border: `2px ${d === "possible" ? "dashed" : "solid"} ${
                  d === "filled" ? S.dot : d === "possible" ? S.dot + "99" : T.mute + "66"
                }`,
              }} />
            ))}
          </div>
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: T.textD }}>{dotText}</span>
        </div>

        {/* barra de acumulación */}
        <div style={{ height: 6, borderRadius: 99, background: T.bg, overflow: "hidden" }}>
          <div style={{
            height: "100%", borderRadius: 99, background: barColor,
            width: `${acumPct}%`, transition: "width 600ms ease",
          }} />
        </div>
        <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.mute, marginTop: 5, paddingBottom: 14 }}>
          {barText}
        </div>
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

  const selectedSymbol = symFilter === "ALL" ? (symNames[0] || "CRASH500") : symFilter;

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
