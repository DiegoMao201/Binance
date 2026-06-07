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

/* ── symbol card (fusión: data técnica completa + nuevo visual) ── */
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
  const ready  = s.readiness || { state: "SIN_DATOS", level: 0 };
  const rc     = READY_COLOR[ready.state] || T.mute;
  const dir    = s.lastSpike?.direction;
  const dirC   = dir === "DOWN" ? T.red : dir === "UP" ? T.green : T.textD;
  const secs   = Number(s.secsSinceLastSpike) || 0;
  const p75Sec = Number(s.p75GapSec) || null;
  const medSec = Number(s.medianGapSec) || null;
  const ticks  = s.live?.ticksSinceSpike ?? s.lastSpike?.ticks_since_last_spike ?? null;
  const recent = Number(s.counts?.h1) || 0;
  const cluster = analytics?.snapshot?.spike_cluster_active ?? false;
  const hasOpen = !!s.openContract;
  const hurst  = analytics?.snapshot?.hurst ?? null;
  const hurstH = analytics?.hurst_history ?? [];

  /* ─ score freshness + gap (needed before isInactive / sem) ─ */
  const scoreAgeSec0  = s.live?.ts ? Math.max(0, Date.now() / 1000 - s.live.ts) : null;
  const scoreIsStale0 = scoreAgeSec0 != null && scoreAgeSec0 > 90;
  const scoreGap0     = s.live?.scoreGap ?? null;   // gap available early for semaphore

  /* ─ semáforo ─ */
  // isInactive only fires when score is stale — if score is fresh, the symbol IS processing normally.
  const isInactive    = scoreIsStale0 && (s.topReasons?.some(r => r.reason === "dynamic_symbol_inactive") || ready.state === "SIN_DATOS");
  const isManualOnly  = s.topReasons?.some(r => r.reason === "manual_only");
  const hasChaseGuard = s.topReasons?.some(r => r.reason?.includes("chase") || r.reason?.includes("post_spike_chase"));
  const clusterDone   = recent >= 3 && !cluster;

  let sem;
  if (isInactive || clusterDone) sem = "red";
  else if (isManualOnly && scoreGap0 != null && scoreGap0 <= 0) sem = "manual";
  else if (secs > 0 && p75Sec && secs > p75Sec && !hasChaseGuard) sem = "green";
  else sem = "amber";

  const SEM = {
    green:  { label: "Lista para entrar",          dot: T.green },
    amber:  { label: "Observar",                   dot: T.amber },
    manual: { label: "Condiciones OK — tú entras", dot: T.violet },
    red:    { label: isInactive ? "Pausado" : "No entrar ahora", dot: isInactive ? T.mute : T.red },
  };
  const S = SEM[sem] || SEM["amber"];

  /* ─ compact message (1 línea) ─ */
  const minsSince = secs > 0 ? Math.round(secs / 60) : null;
  const medMins   = medSec ? Math.round(medSec / 60) : null;
  let msgEmoji, msgLine;
  if (isInactive) {
    msgEmoji = "⛔"; msgLine = "Símbolo pausado por el sistema automático";
  } else if (isManualOnly && scoreGap0 != null && scoreGap0 <= 0) {
    msgEmoji = "🎯"; msgLine = `Score OK · entra manualmente${minsSince != null ? ` · ${minsSince} min sin spike` : ""}`;
  } else if (clusterDone) {
    msgEmoji = "🚨"; msgLine = `Cluster agotado — espera${medMins ? ` ~${medMins} min` : " un rato"} antes de entrar`;
  } else if (sem === "amber" && ticks !== null && ticks < 120) {
    msgEmoji = "⏳"; msgLine = "Acaba de caer — espera 2 min antes de entrar";
  } else if (sem === "green") {
    const hurstNote = hurst != null && hurst < 0.45 ? " · Hurst bajo = sin fuerza" : "";
    msgEmoji = "✅"; msgLine = `Zona cargada${minsSince != null ? ` · ${minsSince} min sin caída` : ""}${hurstNote}`;
  } else {
    const pct = p75Sec && secs ? Math.round((secs / p75Sec) * 100) : null;
    msgEmoji = "⏳"; msgLine = `Acumulando${pct != null ? ` · ${pct}% del tiempo típico` : ""}`;
  }

  /* ─ score strip ─ */
  const live = s.live;
  const score = live?.score;
  const gate  = live?.gate;
  const gap   = live?.scoreGap;
  const liveKind = live?.kind;
  const isVetado = liveKind === "BLOQUEADO" && gap != null && gap <= 0;
  const pctToGate    = gate && score != null ? Math.max(0, Math.min(100, (score / gate) * 100)) : null;
  const scoreAgeSec  = scoreAgeSec0;
  const scoreIsStale = scoreIsStale0;
  const scoreColor = isVetado ? T.amber : gap != null && gap <= 0 ? (scoreIsStale ? T.amber : T.green) : gap != null && gap < 0.5 ? T.amber : T.cyan;
  const gapColor   = isVetado ? T.amber : gap != null && gap <= 0 ? (scoreIsStale ? T.amber : T.green) : T.amber;
  const faltaText  = gap == null ? "–" : isVetado ? "VETADO" : gap <= 0 ? (scoreIsStale ? "LISTO ?" : "LISTO ✓") : `+${num(gap)}`;

  /* ─ Hurst ─ */
  const hurstColor    = hurst == null ? T.mute : hurst < 0.45 ? T.green : hurst < 0.55 ? T.amber : T.red;
  const hurstLabel    = hurst == null ? "–" : hurst < 0.45 ? "sin fuerza" : hurst < 0.55 ? "neutro" : "con fuerza";
  const hurstPos      = hurst != null ? Math.max(2, Math.min(98, ((hurst - 0.30) / 0.40) * 100)) : 50;
  const hurstBarColor = (h) => h < 0.45 ? T.green : h < 0.55 ? T.amber : T.red;

  /* ─ accumulation bar ─ */
  const accumPct  = Math.min(100, Math.round((secs / (p75Sec || medSec || 1)) * 100));
  const barColor  = sem === "green" ? T.green : sem === "red" ? T.red : sem === "manual" ? T.violet : T.amber;
  const barSuffix = accumPct >= 100 ? "· zona óptima" : accumPct >= 75 ? "· casi lista" : accumPct >= 40 ? "· zona de espera" : "· muy pronto";

  return (
    <div style={{
      background: T.panel, borderRadius: 10,
      border: `1px solid ${S.dot}55`,
      boxShadow: `0 0 16px ${S.dot}12`,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>

      {/* posición abierta */}
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

      {/* header: dot + symbol + readiness + semaphore badge */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "10px 12px", background: T.panel2, borderBottom: `1px solid ${T.border}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{
            width: 9, height: 9, borderRadius: "50%", background: rc,
            boxShadow: `0 0 8px ${rc}`, display: "inline-block",
            animation: ready.level >= 4 ? "pulse 1.1s infinite" : "none",
          }} />
          <span style={{ fontFamily: FONT_MONO, fontWeight: 800, fontSize: 15, color: T.text, letterSpacing: "0.04em" }}>
            {s.symbol}
          </span>
          {isManualOnly && (
            <span style={{
              fontFamily: FONT_MONO, fontSize: 8, fontWeight: 700, color: T.violet,
              background: T.violet + "18", border: `1px solid ${T.violet}44`,
              borderRadius: 20, padding: "2px 7px", letterSpacing: "0.06em",
            }}>MANUAL</span>
          )}
        </div>
        <span style={{
          fontFamily: FONT_MONO, fontSize: 9.5, fontWeight: 800, color: S.dot,
          background: S.dot + "18", border: `1px solid ${S.dot}44`,
          borderRadius: 20, padding: "3px 9px", letterSpacing: "0.04em",
        }}>{S.label}</span>
      </div>

      {/* compact message — 1 línea de contexto */}
      <div style={{
        padding: "6px 12px", background: S.dot + "0e",
        borderBottom: `1px solid ${S.dot}22`,
      }}>
        <span style={{ fontFamily: FONT_MONO, fontSize: 11, fontWeight: 800, color: S.dot }}>
          {msgEmoji} {msgLine}
        </span>
      </div>

      {/* score strip */}
      <div style={{ padding: "9px 12px", background: T.bg2, borderBottom: `1px solid ${T.border}` }}>
        <div style={{ display: "flex", alignItems: "flex-end", gap: 14 }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
              <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase", letterSpacing: "0.08em" }}>Score</span>
              {scoreAgeSec != null && (
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: scoreIsStale ? T.amber : T.mute }}>
                  · hace {scoreAgeSec < 60 ? `${Math.round(scoreAgeSec)}s` : `${Math.round(scoreAgeSec / 60)}m`}
                </span>
              )}
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800, color: scoreColor, lineHeight: 1 }}>{num(score)}</div>
          </div>
          <div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Necesita</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 700, color: T.textD }}>≥ {num(gate)}</div>
          </div>
          <div style={{ marginLeft: "auto", textAlign: "right" }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Falta</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 800, color: gapColor }}>
              {faltaText}
            </div>
          </div>
        </div>
        {pctToGate != null && (
          <div style={{ height: 4, borderRadius: 3, background: T.bg, overflow: "hidden", marginTop: 6 }}>
            <div style={{ height: "100%", width: `${pctToGate}%`, background: scoreColor, transition: "width 500ms ease" }} />
          </div>
        )}
        {live && (
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 5 }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.textD }}>régimen <b style={{ color: T.text }}>{live.regime || "–"}</b></span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.textD }}>dir <b style={{ color: sideColor(live.side) }}>{sideLabel(live.side)}</b></span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.textD }}>Hurst <b style={{ color: T.text }}>{num(live.hurst, 3)}</b></span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.textD }}>vol <b style={{ color: T.text }}>{live.volRegime || "–"}</b></span>
            {isVetado && live?.label && (
              <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.amber, background: T.amber + "12", border: `1px solid ${T.amber}33`, borderRadius: 4, padding: "1px 5px" }}>
                ⛔ {live.label}
              </span>
            )}
          </div>
        )}
      </div>

      {/* ══ HURST — fuerza del mercado ══ */}
      <div style={{ padding: "10px 12px 10px", borderBottom: `1px solid ${T.border}` }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 7 }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 700, color: T.mute, letterSpacing: "0.10em", textTransform: "uppercase" }}>
            Fuerza del mercado (Hurst)
          </span>
          <div style={{ display: "flex", alignItems: "baseline", gap: 5 }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800, color: hurstColor, lineHeight: 1 }}>
              {hurst != null ? hurst.toFixed(3) : "–"}
            </span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 600, color: hurstColor }}>{hurstLabel}</span>
          </div>
        </div>
        <div style={{ position: "relative", height: 8, marginBottom: 5 }}>
          <div style={{
            height: "100%", borderRadius: 99,
            background: `linear-gradient(to right, ${T.green} 0%, ${T.amber} 50%, ${T.red} 100%)`,
          }} />
          {hurst != null && (
            <div style={{
              position: "absolute", top: "50%", transform: "translate(-50%, -50%)",
              left: `${hurstPos}%`, width: 14, height: 14, borderRadius: "50%",
              background: T.panel2, border: `2.5px solid ${hurstColor}`,
              boxShadow: `0 0 7px ${hurstColor}`,
            }} />
          )}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 8.5, color: T.green }}>sin fuerza · entra</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 8.5, color: T.mute }}>neutro</span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 8.5, color: T.red }}>con fuerza · espera</span>
        </div>
        {hurstH.length > 0 && (
          <div style={{ display: "flex", gap: 2 }}>
            {hurstH.map((h, i) => {
              const isLast = i === hurstH.length - 1;
              const hc = hurstBarColor(h);
              return (
                <div key={i} style={{
                  flex: 1, height: 16, borderRadius: 3,
                  background: hc + (isLast ? "ff" : "6a"),
                  border: isLast ? `1.5px solid ${hc}` : "none",
                  boxShadow: isLast ? `0 0 5px ${hc}` : "none",
                }} />
              );
            })}
          </div>
        )}
      </div>

      {/* ══ tiempo + ticks (grande) ══ */}
      <div style={{ padding: "11px 12px", display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Sin spike hace</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 21, fontWeight: 800, color: rc, lineHeight: 1.1 }}>{ago(secs)}</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: T.textD, marginTop: 2 }}>
              último {fmtClock(s.lastSpike?.ts)} · <span style={{ color: dirC }}>{dir || "–"}</span> · {num(s.lastSpike?.ratio, 0)}x
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>Ticks sin spike</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 21, fontWeight: 800, color: T.cyan, lineHeight: 1.1 }}>{intC(ticks)}</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, marginTop: 2 }}>típico {dur(medSec)} · p75 {dur(p75Sec)}</div>
          </div>
        </div>

        {/* barra de acumulación */}
        <div style={{ height: 6, borderRadius: 4, background: T.bg, overflow: "hidden" }}>
          <div style={{ height: "100%", borderRadius: 4, background: barColor, width: `${accumPct}%`, transition: "width 600ms ease" }} />
        </div>
        <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, marginTop: -4 }}>
          {accumPct}% del tiempo hasta zona óptima {barSuffix}
        </div>

        {/* cadencia */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, marginTop: 2 }}>
          <Stat label="1h"  value={s.counts?.h1  ?? "–"} color={T.text} />
          <Stat label="6h"  value={s.counts?.h6  ?? "–"} color={T.textD} />
          <Stat label="12h" value={s.counts?.h12 ?? "–"} color={T.textD} />
          <Stat label="24h" value={s.counts?.h24 ?? "–"} color={T.textD} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8 }}>
          <Stat label="prom/h 6h"  value={num(s.ratePerHour?.h6,  1)} color={T.cyan} />
          <Stat label="prom/h 12h" value={num(s.ratePerHour?.h12, 1)} color={T.cyan} />
          <Stat label="prom/h 24h" value={num(s.ratePerHour?.h24, 1)} color={T.cyan} />
        </div>

        {/* razones (por qué el bot no entró) */}
        {s.topReasons?.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, paddingTop: 6, borderTop: `1px solid ${T.border}` }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, letterSpacing: "0.08em", textTransform: "uppercase" }}>
              Notas (por qué el bot no entró)
            </span>
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

        {/* analytics inline — ATR · EMA200 · CLSTR · Z · PROB */}
        {analytics?.snapshot && (
          <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 5, display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>
              {"ATR "}
              <span style={{ color: T.text }}>{analytics.snapshot.atr != null ? analytics.snapshot.atr.toFixed(4) : "–"}</span>
              {" Pct "}
              <span style={{ color: (analytics.snapshot.atr_percentile ?? 50) > 75 ? T.red : (analytics.snapshot.atr_percentile ?? 50) > 40 ? T.amber : T.green }}>
                {analytics.snapshot.atr_percentile ?? "–"}%
              </span>
              {" · EMA200 "}
              <span style={{ color: (analytics.snapshot.ema200_distance_pct ?? 0) > 0 ? T.green : T.red }}>
                {analytics.snapshot.ema200_distance_pct != null ? (analytics.snapshot.ema200_distance_pct * 100).toFixed(3) + "%" : "–"}
              </span>
              {" · CLSTR "}
              <span style={{ color: cluster ? T.red : T.textD, fontWeight: 700 }}>
                {cluster ? "●ON" : "○OFF"}
              </span>
            </span>
            <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>
              {"TICK "}
              <span style={{ color: T.cyan }}>{analytics.snapshot.tick_rate_5s != null ? analytics.snapshot.tick_rate_5s.toFixed(2) : "–"}/5s</span>
              {" · RANGE60s "}
              <span style={{ color: T.cyan }}>{analytics.snapshot.range_rolling_pct_60s != null ? (analytics.snapshot.range_rolling_pct_60s * 100).toFixed(2) + "%" : "–"}</span>
            </span>
            {analytics.spike_stats && (() => {
              const st = analytics.spike_stats;
              const zColor = st.z_score > 2 ? T.red : st.z_score > 1 ? T.amber : st.z_score < -0.5 ? T.green : T.textD;
              return (
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>
                  {"Z "}
                  <span style={{ color: zColor }}>{st.z_score > 0 ? "+" : ""}{st.z_score.toFixed(2)}</span>
                  {" · PROB "}
                  <span style={{ color: st.prob_100 > 0.65 ? T.green : st.prob_100 > 0.35 ? T.amber : T.textD }}>100t:{(st.prob_100 * 100).toFixed(0)}%</span>
                  {" "}
                  <span style={{ color: st.prob_200 > 0.65 ? T.green : st.prob_200 > 0.35 ? T.amber : T.textD }}>200t:{(st.prob_200 * 100).toFixed(0)}%</span>
                  {" "}
                  <span style={{ color: st.prob_500 > 0.65 ? T.green : st.prob_500 > 0.35 ? T.amber : T.textD }}>500t:{(st.prob_500 * 100).toFixed(0)}%</span>
                  <span style={{ color: T.mute }}>{" ·n="}{st.sample_size}</span>
                </span>
              );
            })()}
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

  const SYMBOL_ORDER = ["CRASH500", "CRASH600", "CRASH900", "CRASH1000", "BOOM500", "BOOM600", "BOOM900"];
  const symbols = (data?.symbols || []).slice().sort((a, b) => {
    const ai = SYMBOL_ORDER.indexOf(a.symbol);
    const bi = SYMBOL_ORDER.indexOf(b.symbol);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
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
