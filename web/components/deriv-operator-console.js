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
  SECO: { color: "#f59e0b", label: "SECO" },
  TREND_GATE: { color: T.mute, label: "TREND sin FVG" },
  AI_VETO: { color: "#a78bfa", label: "IA VETÓ" },
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
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

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
    const id = setInterval(doFetch, 5000);
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
    msgEmoji = "🚨";
    msgLine = secs > 300
      ? `Post-cluster · ${minsSince}m sin spike · esperando normalización`
      : `Cluster agotado — espera${medMins ? ` ~${medMins} min` : " un rato"} antes de entrar`;
  } else if (sem === "green") {
    msgEmoji = "✅"; msgLine = `Zona cargada${minsSince != null ? ` · ${minsSince} min sin caída` : ""}`;
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

  // ── SEÑAL MAESTRA — card-level (drives conditional rendering) ──────────
  const _sn           = analytics?.snapshot ?? null;
  const _lastSpikeWTs = _sn?.last_spike_wall_ts ?? null;
  const _secsSinceSpk = _lastSpikeWTs ? Math.floor(now / 1000 - _lastSpikeWTs) : null;
  const _isBlindZone  = _secsSinceSpk != null && _secsSinceSpk < 120;
  const _burstActive  = _sn?.burst_active ?? false;
  const _burstDepth   = _sn?.burst_depth ?? null;
  // SEÑAL MAESTRA variables (mirrors inner block logic)
  const _scoreRawC    = _sn?.score_raw ?? null;
  const _setupTypeC   = _sn?.setup_type ?? null;
  const _gradeC       = _sn?.execution_grade ?? null;
  const _scarcityC    = _sn?.scarcity_state ?? null;
  const _immStateC    = _sn?.spike_imminence_state ?? null;
  const _immScoreC    = _sn?.spike_imminence_score ?? null;
  const _fvgAnchC     = _sn?.fvg_anchor_active ?? false;
  const _structConfC  = _sn?.structural_fvg_confirm ?? false;
  const _structConflC = _sn?.structural_fvg_conflict ?? false;
  const _ema200DistC  = _sn?.ema200_distance_pct ?? null;
  const _isCrashCard  = s.symbol.toUpperCase().includes("CRASH");
  const _isBoomCard   = s.symbol.toUpperCase().includes("BOOM");
  const _ldThr        = 0.00008; // 0.008% in fraction form
  const _ema200LoadedC = _ema200DistC == null ? null
    : _isCrashCard ? _ema200DistC >= _ldThr
    : _isBoomCard  ? _ema200DistC <= -_ldThr
    : null;
  const _rngProbC   = _sn?.rng_probability ?? null;
  const _rngThreshC = _sn?.rng_threshold ?? 65;
  const _masterRed    = _structConflC
    || _scarcityC === "SECO"
    || (_scarcityC === "VENCIDO" && (_scoreRawC == null || _scoreRawC < 7.0))
    || _gradeC === "C"
    || _ema200LoadedC === false;
  const _masterGreen  = !_masterRed && _sn != null
    && _rngProbC != null && _rngProbC >= _rngThreshC
    && (_gradeC === "A" || _gradeC === "B")
    && (_scarcityC == null || ["FRESCO","CARGANDO","LISTO"].includes(_scarcityC));

  // ── DISPLAY SCORE — usa score_raw del analytics cuando decisión está vencida ──
  const _usingLiveScore = _scoreRawC != null && (score == null || scoreIsStale0);
  const _dispScore    = _usingLiveScore ? _scoreRawC    : score;
  const _dispGap      = (_usingLiveScore && gate != null) ? +(gate - _scoreRawC).toFixed(2) : gap;
  const _dispPct      = gate && _dispScore != null ? Math.max(0, Math.min(100, (_dispScore / gate) * 100)) : null;
  const _dispFalta    = _dispGap == null ? "–"
    : isVetado ? "VETADO"
    : _dispGap <= 0 ? "LISTO ✓"
    : `+${num(_dispGap)}`;
  const _dispScoreC   = isVetado ? T.amber
    : _dispGap != null && _dispGap <= 0 ? T.green
    : _dispGap != null && _dispGap < 0.5 ? T.amber : T.cyan;
  const _dispGapC     = isVetado ? T.amber
    : _dispGap != null && _dispGap <= 0 ? T.green : T.amber;

  // ── CASCADA ────────────────────────────────────────────────────────────────
  const _cascadeActive    = _sn?.cascade_active ?? false;
  const _cascadeGapTicks  = _sn?.cascade_gap_ticks ?? null;
  const _cascadeDepth     = _sn?.burst_depth ?? null;
  // CASCADE overrides blind zone: continuous momentum = no retroceso window needed
  const _inCascade        = _cascadeActive && _isBlindZone;

  // ── PRESIÓN REAL ───────────────────────────────────────────────────────────
  const _presion          = analytics?.presion ?? null;
  const _presionScore     = _presion?.score ?? null;
  const _presionLabel     = _presion?.label ?? null;
  const _presionColor     = _presionScore == null ? T.mute
    : _presionScore >= 8 ? T.red
    : _presionScore >= 6 ? T.orange
    : _presionScore >= 4 ? T.amber
    : T.green;
  // Z-score for slow-market awareness
  const _zScore           = analytics?.spike_stats?.z_score ?? null;
  const _ticksWait        = analytics?.spike_stats?.current_wait ?? null;
  const _p90Ticks         = analytics?.spike_stats?.p90_gap ?? null;
  const _isOverdueExtreme = _zScore != null && _zScore >= 2.0;
  const _isOverdueHigh    = _zScore != null && _zScore >= 1.5 && _zScore < 2.0;

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

      {/* ══ CASCADE MOMENTUM (solo cuando está activo) ══ */}
      {_inCascade && (
        <div style={{ padding: "6px 12px", borderBottom: `1px solid ${T.border}` }}>
          <div style={{
            background: T.orange + "0d", border: `1px solid ${T.orange}33`,
            borderRadius: 6, padding: "5px 10px",
          }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800,
              color: T.orange, letterSpacing: "0.08em" }}>
              CASCADE — MOMENTUM ACTIVO
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.mute, marginTop: 1 }}>
              {`${_cascadeDepth ?? "?"}x spikes · gap ${_cascadeGapTicks ?? "?"}t`}
            </div>
          </div>
        </div>
      )}

      {/* ══ GO — banner hero cuando todos los filtros pasan ══ */}
      {_masterGreen && (
        <div style={{
          padding: "8px 12px", background: T.green + "12",
          borderBottom: `1px solid ${T.green}33`,
          display: "flex", alignItems: "center", gap: 10,
        }}>
          <div style={{
            width: 13, height: 13, borderRadius: "50%", flexShrink: 0,
            background: T.green, boxShadow: `0 0 10px ${T.green}`,
          }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 12, fontWeight: 800,
              color: T.green, letterSpacing: "0.12em" }}>
              ENTRADA CONFIRMADA
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.green, opacity: 0.7, marginTop: 1 }}>
              {_setupTypeC} · GRADE {_gradeC} · {_scarcityC}
            </div>
          </div>
          <div style={{ display: "flex", gap: 14, flexShrink: 0, alignItems: "flex-end" }}>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.mute, textTransform: "uppercase", letterSpacing: "0.08em" }}>SCORE</div>
              <div style={{ fontFamily: FONT_MONO, fontSize: 28, fontWeight: 800, color: T.green, lineHeight: 1 }}>
                {_scoreRawC?.toFixed(1) ?? "–"}
              </div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.mute, textTransform: "uppercase", letterSpacing: "0.08em" }}>PROB RNG</div>
              <div style={{ fontFamily: FONT_MONO, fontSize: 28, fontWeight: 800, color: T.green, lineHeight: 1 }}>
                {_rngProbC ?? "–"}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* score strip */}
      <div style={{ padding: "9px 12px", background: T.bg2, borderBottom: `1px solid ${T.border}` }}>
        <div style={{ display: "flex", alignItems: "flex-end", gap: 14 }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
              <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase", letterSpacing: "0.08em" }}>Score</span>
              {_usingLiveScore
                ? <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.green }}>· en vivo</span>
                : scoreAgeSec != null && (
                  <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: scoreIsStale ? T.amber : T.mute }}>
                    · hace {scoreAgeSec < 60 ? `${Math.round(scoreAgeSec)}s` : `${Math.round(scoreAgeSec / 60)}m`}
                  </span>
                )
              }
            </div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800, color: _dispScoreC, lineHeight: 1 }}>{num(_dispScore)}</div>
          </div>
          <div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Necesita</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 700, color: T.textD }}>≥ {num(gate)}</div>
          </div>
          {_rngProbC != null && (
            <div>
              <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>RNG</div>
              <div style={{ fontFamily: FONT_MONO, fontSize: 15, fontWeight: 800,
                color: _rngProbC >= (_sn?.rng_threshold ?? 65) ? T.green : T.orange }}>
                {_rngProbC}%
              </div>
            </div>
          )}
          <div style={{ marginLeft: "auto", textAlign: "right" }}>
            <div style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute, textTransform: "uppercase" }}>Estado</div>
            <div style={{ fontFamily: FONT_MONO, fontSize: 13, fontWeight: 800, color: _dispGapC }}>
              {_dispFalta}
            </div>
          </div>
        </div>
        {_dispPct != null && (
          <div style={{ height: 4, borderRadius: 3, background: T.bg, overflow: "hidden", marginTop: 6 }}>
            <div style={{ height: "100%", width: `${_dispPct}%`, background: _dispScoreC, transition: "width 500ms ease" }} />
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


        {/* analytics — Vision 15M + ENTRY QUALITY */}
        {analytics?.snapshot && (
          <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 5, display: "flex", flexDirection: "column", gap: 3 }}>
            {analytics.spike_stats && (() => {
              const st = analytics.spike_stats;
              const zColor = st.z_score > 2 ? T.red : st.z_score > 1 ? T.amber : st.z_score < -0.5 ? T.green : T.textD;
              return (
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: T.mute }}>
                  {"Z "}
                  <span style={{ color: zColor }}>{st.z_score > 0 ? "+" : ""}{st.z_score.toFixed(2)}</span>
                  {" · típico "}
                  <span style={{ color: T.textD }}>{st.p50_gap}t</span>
                  {" · p75 "}
                  <span style={{ color: T.textD }}>{st.p75_gap}t</span>
                  <span style={{ color: T.mute }}>{" ·n="}{st.sample_size}</span>
                </span>
              );
            })()}
            {/* ── ESTRUCTURA 15M (Vision LLM — 1 línea) ─────────────── */}
            {s.vision && (() => {
              const v = s.vision;
              const trend = v.trend_15m || "ranging";
              const conf = typeof v.confidence === "number" ? v.confidence : 0;
              const isBoom = s.symbol?.startsWith("BOOM");
              const biasAligned = (isBoom && trend === "downtrend") || (!isBoom && trend === "uptrend");
              const biasConflict = (isBoom && trend === "uptrend") || (!isBoom && trend === "downtrend");
              const trendColor = biasAligned ? T.green : biasConflict ? T.red : T.amber;
              const trendArrow = trend === "uptrend" ? "▲" : trend === "downtrend" ? "▼" : "◆";
              const biasLabel = trend === "ranging" ? "LATERAL"
                : isBoom ? (trend === "downtrend" ? "CARGANDO ✓" : "YA SUBIÓ ✗")
                : (trend === "uptrend" ? "CARGANDO ✓" : "YA CAYÓ ✗");
              const ageMin = v.updated_at ? Math.round((Date.now() / 1000 - v.updated_at) / 60) : null;
              return (
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontFamily: FONT_MONO, fontSize: 9 }}>
                  <span style={{ color: T.mute, fontSize: 8 }}>15M</span>
                  <span style={{ color: trendColor, fontWeight: 900 }}>{trendArrow}</span>
                  <span style={{ color: trendColor, fontWeight: 800 }}>{biasLabel}</span>
                  {conf > 0 && <span style={{ color: T.mute, fontSize: 8 }}>conf {Math.round(conf * 100)}%</span>}
                  {ageMin !== null && <span style={{ color: T.mute, fontSize: 7 }}>hace {ageMin}m</span>}
                </div>
              );
            })()}

            {/* ── Alertas operacionales ─────────────────────────────────── */}
            {_inCascade && (
              <div style={{
                padding: "5px 8px",
                background: T.orange + "18", border: `1px solid ${T.orange}55`,
                borderRadius: 5, display: "flex", alignItems: "center", gap: 8,
              }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800, color: T.orange, letterSpacing: "0.10em" }}>
                  ⚡ CASCADE
                </span>
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.textD }}>
                  {_cascadeDepth ?? "?"}x · {_cascadeGapTicks ?? "?"}t entre spikes
                </span>
              </div>
            )}
            {(_isOverdueExtreme || _isOverdueHigh) && !_isBlindZone && (
              <div style={{
                padding: "4px 8px",
                background: (_isOverdueExtreme ? T.red : T.amber) + "14",
                border: `1px solid ${(_isOverdueExtreme ? T.red : T.amber)}44`,
                borderRadius: 5, display: "flex", alignItems: "center", gap: 8,
              }}>
                <span style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800,
                  color: _isOverdueExtreme ? T.red : T.amber }}>
                  {_isOverdueExtreme ? "SOBREPRESIÓN" : "PRESIÓN ALTA"}
                </span>
                <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.textD }}>
                  Z={_zScore != null ? (_zScore > 0 ? "+" : "") + _zScore.toFixed(2) : "?"}
                  {_p90Ticks != null && ` · p90=${_p90Ticks}t`}
                </span>
              </div>
            )}
            {/* ── ENTRY QUALITY INDICATORS ─────────────────────────────── */}
            {(() => {
              const sn = analytics.snapshot;
              const setupType    = sn.setup_type    || null;
              const grade        = sn.execution_grade || null;
              const scarcity     = sn.scarcity_state  || null;
              const immState     = sn.spike_imminence_state || null;
              const immScore     = sn.spike_imminence_score ?? null;
              const geoPos       = sn.geo_channel_pos ?? null;
              const fvgTier      = sn.fvg_tier || null;
              const scoreRaw     = sn.score_raw ?? null;
              const burstActive     = sn.burst_active ?? false;
              const burstDepth      = sn.burst_depth ?? null;
              const burstRetroceso  = sn.burst_retroceso ?? null;
              const fvgAnchorActive    = sn.fvg_anchor_active ?? false;
              const fvgAnchorAgeS      = sn.fvg_anchor_age_s ?? null;
              const fvgAnchorWick      = sn.fvg_anchor_wick_survived ?? null;
              const fvgAnchorTolPen    = sn.fvg_anchor_tol_penalty ?? null;
              const fvgAnchorMomPen    = sn.fvg_anchor_mom_penalty ?? null;
              const structFvgConfirm  = sn.structural_fvg_confirm ?? false;
              const structFvgConflict = sn.structural_fvg_conflict ?? false;
              const structFvgAbsent   = sn.structural_fvg_absent ?? false;
              const structFvgActive   = sn.structural_fvg_active ?? null;
              const structFvgDir      = sn.structural_fvg_direction ?? null;
              const atrAnchored       = sn.atr_anchored ?? false;
              const ema200Anchored    = sn.ema200_anchored ?? false;
              const ema200DistPct     = sn.ema200_distance_pct ?? null;
              const kineticVelocity   = sn.kinetic_velocity ?? null;
              const kineticAccel      = sn.kinetic_acceleration ?? null;
              const kineticCompressed = sn.kinetic_compressed ?? false;
              const ghostMad          = sn.ghost_mad ?? null;
              const fvgBosValidated   = sn.fvg_bos_validated ?? false;
              const trifectaMet       = sn.trifecta_met ?? false;
              const trifectaVision    = sn.trifecta_vision ?? false;
              const trifectaKinetic   = sn.trifecta_kinetic ?? false;
              const trifectaScarcity  = sn.trifecta_scarcity ?? false;
              const trifectaFvgBos    = sn.trifecta_fvg_bos ?? false;
              const rngProb           = sn.rng_probability ?? null;
              const rngMissing        = sn.rng_missing ?? [];
              const rngThreshold      = sn.rng_threshold ?? 65;
              const masterKeyBypass   = sn.master_key_bypass ?? null;
              const masterKeyRng      = sn.master_key_rng ?? null;
              // ── Pipeline state (new motor fields) ─────────────────────────
              const tripleLockFired   = sn.triple_lock_fired ?? false;
              const tripleLockZ       = sn.triple_lock_z ?? null;
              const tripleLockElapsed = sn.triple_lock_elapsed ?? null;
              const tripleLockKinetic = sn.triple_lock_kinetic ?? null;
              const tripleLockBlocked = sn.triple_lock_blocked ?? null;
              const elasticityZ       = sn.elasticity_z ?? null;
              const elasticityOrig    = sn.elasticity_regime_min_original ?? null;
              const elasticityElastic = sn.elasticity_regime_min_elastic ?? null;
              const elasticityTimePct = sn.elasticity_time_pressure ?? null;
              const elasticityTimeTriggered = sn.elasticity_time_triggered ?? false;
              const marketContext     = sn.market_context ?? null;
              const fvgLookbackUsed   = sn.fvg_lookback_used ?? null;
              const scarcityElapsedS  = sn.scarcity_elapsed_s ?? null;
              const postClusterExhaustion = sn.post_cluster_exhaustion ?? false;
              // EMA200 loaded: for CRASH need price above EMA200 (dev ≥ +0.008% fraction)
              //                for BOOM  need price below EMA200 (dev ≤ −0.008% fraction)
              const _isCrashSym = s.symbol.toUpperCase().includes("CRASH");
              const _isBoomSym  = s.symbol.toUpperCase().includes("BOOM");
              const _ldThresh   = 0.00008; // = 0.008% / 100
              const ema200Loaded = ema200DistPct == null ? null
                : _isCrashSym ? ema200DistPct >= _ldThresh
                : _isBoomSym  ? ema200DistPct <= -_ldThresh
                : null;
              // Time since last spike in human-readable form (ticks ≈ seconds for synthetics)
              const _ticksSinceSpi  = sn.ticks_since_last_spike ?? null;
              const _timeSinceSpiStr = _ticksSinceSpi == null ? null
                : _ticksSinceSpi < 60 ? `${_ticksSinceSpi}s`
                : `${Math.floor(_ticksSinceSpi / 60)}m${_ticksSinceSpi % 60 > 0 ? ((_ticksSinceSpi % 60) + "s") : ""}`;

              if (!setupType && !scarcity && !grade && !fvgTier && geoPos == null) return null;

              // ── Post-spike blind zone ──────────────────────────────────────
              // Indicators computed in the 120s after a spike are biased by the
              // spike's price action (FVG/SMC/geo all reflect the spike anomaly).
              // Use last_spike_wall_ts for real-time accuracy (not cached ticks).
              const lastSpikeWallTs = sn.last_spike_wall_ts ?? null;
              const secsSinceSpike  = lastSpikeWallTs
                ? Math.floor(Date.now() / 1000 - lastSpikeWallTs) : null;
              // 0-120s: ciega total (indicadores sesgados por el spike)
              // 120-300s: normalizando (primeras velas post-spike, aún parcialmente sesgados)
              const isBlindZone       = secsSinceSpike != null && secsSinceSpike < 120;
              const isNormalizingZone = secsSinceSpike != null && secsSinceSpike >= 120 && secsSinceSpike < 300;
              const postSpikeLabel = secsSinceSpike != null && secsSinceSpike < 300
                ? (secsSinceSpike < 60 ? `${secsSinceSpike}s` : `${Math.floor(secsSinceSpike/60)}m${secsSinceSpike%60}s`)
                : null;

              // ── Staleness (datos viejos del cache) ─────────────────────────
              const ageS = sn.ts ? Math.floor(Date.now() / 1000 - sn.ts) : null;
              const isStale     = !isBlindZone && ageS != null && ageS > 90;
              const isVeryStale = !isBlindZone && ageS != null && ageS > 180;
              const ageLabel = (!isBlindZone && !isNormalizingZone && ageS != null)
                ? (ageS < 60 ? `hace ${ageS}s` : `hace ${Math.floor(ageS/60)}m${ageS%60}s`) : null;

              const staleOpacity = isVeryStale ? 0.35 : isStale ? 0.55 : 1.0;
              const headerColor  = isBlindZone ? T.red
                : isNormalizingZone ? T.amber
                : isVeryStale ? T.red : isStale ? T.amber : T.mute;

              // Setup color — TREND ahora es ámbar (bot lo permite vía RNG)
              const setupColor = setupType === "SMC_FVG" ? T.green
                : setupType === "TREND" ? T.amber
                : setupType === "TREND_NO_STRUCT" ? T.amber
                : setupType === "EMA200_SPIKE" ? T.cyan : T.mute;
              const setupLabel = setupType === "SMC_FVG" ? "SMC_FVG"
                : setupType === "TREND" ? "TREND"
                : setupType === "TREND_NO_STRUCT" ? "SIN BOS"
                : setupType === "EMA200_SPIKE" ? "EMA200" : (setupType || "–");

              // Grade color
              const gradeColor = grade === "A" ? T.green : grade === "B" ? T.amber : grade === "C" ? T.red : T.mute;

              // Scarcity: FRESCO right after a spike is correct but does NOT mean "enter now"
              // Color reflects cycle state, not entry permission
              const scarColor = ["SECO","VENCIDO"].includes(scarcity) ? T.red
                : ["FRESCO","CARGANDO","LISTO"].includes(scarcity) ? T.green : T.mute;

              // Imminence: sweet spot 0.3–0.6
              const immColor = immScore != null
                ? (immScore >= 0.3 && immScore <= 0.6 ? T.green : immScore > 0.6 && immScore <= 0.8 ? T.amber : immScore > 0.8 ? T.red : T.mute)
                : T.mute;
              const immStateColor = immState === "BUILDING" ? T.green
                : immState === "RIPE" ? T.amber : immState === "OVERDUE" ? T.amber : T.mute;

              // Geo channel: < 20% = best zone; < 0 = below channel (green), > 1 = above channel (red)
              const geoColor = geoPos != null ? (geoPos < 0.20 ? T.green : geoPos < 0.40 ? T.amber : T.red) : T.mute;
              const geoPct   = geoPos == null ? "–"
                : geoPos < 0 ? "BOT"
                : geoPos > 1 ? "TOP"
                : (geoPos * 100).toFixed(0) + "%";

              // FVG tier
              const fvgTierColor = fvgTier === "fvg_full_confluence" ? T.green
                : fvgTier === "fvg_mitigated" ? T.amber
                : fvgTier === "dynamic_soft_veto_no_fvg" ? T.red : T.mute;
              const fvgTierLabel = fvgTier === "fvg_full_confluence" ? "CONF"
                : fvgTier === "fvg_mitigated" ? "MITIG"
                : fvgTier === "dynamic_soft_veto_no_fvg" ? "SIN_FVG" : (fvgTier ? fvgTier.toUpperCase().slice(0,8) : "–");

              // Score raw color — master key bypass: score válido aunque bajo para el gate
              const scoreRawColor = scoreRaw == null ? T.mute
                : masterKeyBypass ? T.violet
                : scoreRaw >= 7.8 ? T.green : scoreRaw >= 7.0 ? T.amber : T.red;

              // Chips dim only when data is stale
              const chipOpacity = staleOpacity;

              // ── Chip opacity ──────────────────────────────────────────────

              const chip = (label, value, color) => (
                <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 3,
                  background: color + "18", border: `1px solid ${color}44`,
                  borderRadius: 4, padding: "1px 5px", opacity: chipOpacity }}>
                  <span style={{ color: T.mute, fontSize: 7, fontWeight: 600 }}>{label}</span>
                  <span style={{ color, fontSize: 8, fontWeight: 700 }}>{value}</span>
                </span>
              );

              return (
                <div style={{ borderTop: `1px solid ${T.border}`, paddingTop: 5 }}>
                  {/* ── TRIPLE LOCK banner ─────────────────────────────────── */}
                  {tripleLockFired && (
                    <div style={{
                      background: T.cyan + "18", border: `2px solid ${T.cyan}88`,
                      borderRadius: 5, padding: "4px 8px", marginBottom: 5,
                      display: "flex", alignItems: "center", gap: 6,
                    }}>
                      <div style={{ width: 9, height: 9, borderRadius: "50%", background: T.cyan,
                        boxShadow: `0 0 6px ${T.cyan}`, flexShrink: 0 }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ color: T.cyan, fontSize: 8, fontWeight: 800, letterSpacing: "0.10em" }}>
                          TRIPLE LOCK
                        </div>
                        <div style={{ color: T.textD, fontSize: 6, marginTop: 1 }}>
                          {`Z=${(tripleLockZ ?? 0).toFixed(2)} · ${tripleLockElapsed != null ? Math.round(tripleLockElapsed) + "s" : "–"} · kin=${(tripleLockKinetic ?? 0).toFixed(3)}`}
                        </div>
                      </div>
                      <div style={{ color: T.cyan, fontSize: 7, fontWeight: 700 }}>+30 impl</div>
                    </div>
                  )}
                  {/* ── TRIPLE LOCK parcialmente bloqueado ─────────────────── */}
                  {!tripleLockFired && tripleLockBlocked && tripleLockBlocked.length > 0 && (
                    <div style={{
                      background: T.orange + "10", border: `1px solid ${T.orange}44`,
                      borderRadius: 4, padding: "3px 7px", marginBottom: 4,
                    }}>
                      <div style={{ color: T.orange, fontSize: 6, fontWeight: 700 }}>
                        TRIPLE LOCK: {tripleLockBlocked.join(" · ")}
                      </div>
                    </div>
                  )}
                  {/* ── ELASTICITY banner ──────────────────────────────────── */}
                  {elasticityZ != null && !tripleLockFired && (
                    <div style={{
                      background: T.violet + "14", border: `1px solid ${T.violet}55`,
                      borderRadius: 4, padding: "3px 7px", marginBottom: 4,
                      display: "flex", alignItems: "center", gap: 6,
                    }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ color: T.violet, fontSize: 7, fontWeight: 700, letterSpacing: "0.08em" }}>
                          {marketContext === "EXTREME_OVERPRESSURE" ? "EXTREME OVERPRESSURE" : "PRESIÓN ALTA"}
                          {elasticityTimeTriggered ? " · TIME" : ""}
                        </div>
                        <div style={{ color: T.textD, fontSize: 6, marginTop: 1 }}>
                          {elasticityOrig != null && elasticityElastic != null
                            ? `gate ${elasticityOrig.toFixed(2)}→${elasticityElastic.toFixed(2)}`
                            : `Z=${elasticityZ.toFixed(2)}`}
                          {elasticityTimePct != null ? ` · ${(elasticityTimePct * 100).toFixed(0)}% ciclo` : ""}
                        </div>
                      </div>
                      <div style={{ color: T.violet, fontSize: 8, fontWeight: 800 }}>
                        Z+{elasticityZ.toFixed(2)}
                      </div>
                    </div>
                  )}
                  {/* ── DYNAMIC ANCHOR info ────────────────────────────────── */}
                  {fvgLookbackUsed != null && fvgLookbackUsed > 200 && (
                    <div style={{
                      background: T.mute + "0a", border: `1px solid ${T.border}`,
                      borderRadius: 4, padding: "2px 7px", marginBottom: 4,
                    }}>
                      <div style={{ color: T.mute, fontSize: 6 }}>
                        {`FVG ventana: ${fvgLookbackUsed}t (dinámico)`}
                        {scarcityElapsedS != null ? ` · ${Math.round(scarcityElapsedS)}s sin spike` : ""}
                      </div>
                    </div>
                  )}

                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: isBlindZone || isNormalizingZone ? 3 : 4 }}>
                    <span style={{ fontSize: 7, color: headerColor, letterSpacing: "0.08em", fontWeight: 600 }}>
                      CALIDAD DE ENTRADA
                    </span>
                    {ageLabel && (
                      <span style={{ fontSize: 6, color: headerColor, opacity: 0.8 }}>
                        {isStale ? "⚠ " : ""}{ageLabel}
                      </span>
                    )}
                  </div>


                  {/* ── Burst active banner ─────────────────────────────────── */}
                  {burstActive && (
                    <div style={{ background: "#0ff3" , border: "1px solid #0ff6",
                      borderRadius: 4, padding: "3px 6px", marginBottom: 4 }}>
                      <div style={{ color: "#0ff", fontSize: 7, fontWeight: 700, letterSpacing: "0.06em" }}>
                        BURST ACTIVO
                        {burstDepth != null ? ` · ${burstDepth}spikes` : ""}
                        {burstRetroceso != null ? ` · RETR ${(burstRetroceso * 100).toFixed(0)}%` : ""}
                      </div>
                      <div style={{ color: T.mute, fontSize: 6, marginTop: 1 }}>
                        {burstRetroceso != null && burstRetroceso > 0.35
                          ? "Momentum cancelado — esperar reset"
                          : "Momentum de ráfaga — R<35% = válido para entrada rápida"}
                      </div>
                    </div>
                  )}

                  {/* ── MASTER KEY bypass banner ─────────────────────────── */}
                  {masterKeyBypass && (
                    <div style={{
                      background: T.violet + "22",
                      border: `2px solid ${T.violet}88`,
                      borderRadius: 7, padding: "7px 10px", marginBottom: 6,
                      boxShadow: `0 0 10px ${T.violet}22`,
                    }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 9, fontWeight: 800,
                            color: T.violet, letterSpacing: "0.12em" }}>
                            MASTER KEY ACTIVO
                          </div>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 7, color: T.textD, marginTop: 2 }}>
                            {masterKeyBypass.replace(/_GATE$/,"").replace(/_/g," ")} bypassed
                            {masterKeyRng != null ? ` · rng=${masterKeyRng}/100` : ""}
                          </div>
                        </div>
                        <div style={{ textAlign: "right", flexShrink: 0 }}>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 8, color: T.mute }}>SCORE</div>
                          <div style={{ fontFamily: FONT_MONO, fontSize: 22, fontWeight: 800,
                            color: T.violet, lineHeight: 1 }}>
                            {scoreRaw?.toFixed(2) ?? "–"}
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* ── RNG Probability — métrica principal ───────────────── */}
                  {rngProb != null && (() => {
                    const pct = Math.min(100, rngProb);
                    const passed = rngProb >= rngThreshold;
                    const barCol = rngProb >= 80 ? T.green
                      : rngProb >= rngThreshold ? T.amber
                      : rngProb >= 45 ? T.orange : T.red;
                    return (
                      <div style={{
                        background: barCol + "18",
                        border: `2px solid ${barCol}${passed ? "99" : "44"}`,
                        borderRadius: 7, padding: "8px 10px", marginBottom: 6,
                        opacity: chipOpacity,
                        boxShadow: passed ? `0 0 10px ${barCol}22` : "none",
                      }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <div style={{ flex: 1 }}>
                            <div style={{ fontSize: 7, color: T.mute, fontWeight: 700, letterSpacing: "0.10em", marginBottom: 3 }}>
                              PROBABILIDAD RNG
                            </div>
                            <div style={{ height: 6, borderRadius: 4, background: T.bg, overflow: "hidden", marginBottom: 3 }}>
                              <div style={{
                                height: "100%", borderRadius: 4, width: `${pct}%`,
                                background: barCol, transition: "width 0.5s ease",
                              }} />
                            </div>
                            {rngMissing.length > 0 && (
                              <div style={{ fontSize: 6, color: T.mute }}>Falta: {rngMissing.join(" · ")}</div>
                            )}
                          </div>
                          <div style={{ textAlign: "right", flexShrink: 0 }}>
                            <div style={{ fontFamily: FONT_MONO, fontSize: 30, fontWeight: 800, color: barCol, lineHeight: 1 }}>
                              {rngProb}
                            </div>
                            <div style={{ fontSize: 8, color: passed ? barCol : T.mute, fontWeight: 700, letterSpacing: "0.08em" }}>
                              {passed ? `✓ ≥${rngThreshold}` : `✗ <${rngThreshold}`}
                            </div>
                          </div>
                        </div>
                      </div>
                    );
                  })()}

                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {setupType && chip("SETUP", setupLabel, setupColor)}
                    {grade && chip("GRADE", grade, gradeColor)}
                    {scarcity && chip("SCAR", scarcity, scarColor)}
                    {scoreRaw != null && chip("SCORE", scoreRaw.toFixed(2), scoreRawColor)}
                    {masterKeyBypass && chip("MK", masterKeyBypass.replace(/_GATE$/,"").replace(/SCARCITY_/,"").replace(/_/g,"-"), T.violet)}
                  </div>
                </div>
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

  const [ghostData, setGhostData] = useState(null);
  useEffect(() => {
    const loadGhost = async () => {
      try {
        const r = await fetch("/api/deriv/analytics/ghost-trades?hours=24", { cache: "no-store" });
        if (r.ok) setGhostData(await r.json());
      } catch {}
    };
    loadGhost();
    const id = setInterval(loadGhost, 30_000);
    return () => clearInterval(id);
  }, []);

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

      {/* ══ GHOST TRADES ══════════════════════════════════════════════════════ */}
      <Panel title="GHOST TRADES — GATES AUDITADOS" style={{ marginTop: 12 }}>
        {!ghostData ? (
          <div style={{ padding: "14px 16px", color: T.mute, fontSize: 11 }}>Cargando datos fantasma…</div>
        ) : ghostData.totals?.total === 0 ? (
          <div style={{ padding: "14px 16px", color: T.mute, fontSize: 11 }}>Sin trades fantasma en las últimas 24h.</div>
        ) : (
          <div style={{ padding: "12px 16px" }}>
            {/* Summary row */}
            <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 10 }}>
              {[
                { label: "TOTAL",   val: ghostData.totals?.total,   color: T.text },
                { label: "WIN",     val: ghostData.totals?.WIN,     color: T.green },
                { label: "LOSS",    val: ghostData.totals?.LOSS,    color: T.red },
                { label: "EXPIRED", val: ghostData.totals?.EXPIRED, color: T.amber },
                { label: "WR",      val: ghostData.totals?.win_rate != null ? `${(ghostData.totals.win_rate * 100).toFixed(0)}%` : "–", color: ghostData.totals?.win_rate >= 0.5 ? T.green : T.red },
                { label: "PnL EST", val: ghostData.totals?.net_pnl != null ? `${ghostData.totals.net_pnl >= 0 ? "+" : ""}${ghostData.totals.net_pnl.toFixed(2)}` : "–", color: (ghostData.totals?.net_pnl ?? 0) >= 0 ? T.green : T.red },
                { label: "PF",      val: ghostData.totals?.profit_factor != null ? (ghostData.totals.profit_factor === 999 ? "∞" : ghostData.totals.profit_factor.toFixed(2)) : "–", color: (ghostData.totals?.profit_factor ?? 0) >= 1.5 ? T.green : (ghostData.totals?.profit_factor ?? 0) >= 1.0 ? T.amber : T.red },
              ].map(({ label, val, color }) => (
                <div key={label} style={{ textAlign: "center", minWidth: 52 }}>
                  <div style={{ fontSize: 17, fontWeight: 800, color }}>{val ?? "–"}</div>
                  <div style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em" }}>{label}</div>
                </div>
              ))}
            </div>

            {/* Per-gate table */}
            <div style={{ overflowX: "auto", marginBottom: 12 }}>
              <div style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em", marginBottom: 4 }}>POR GATE</div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                    {["GATE", "TOTAL", "WIN", "LOSS", "EXP", "WR", "PnL EST", "PF", "AVG WIN", "AVG LOSS"].map(h => (
                      <th key={h} style={{ padding: "4px 8px", textAlign: "left", color: T.mute, fontWeight: 700, letterSpacing: "0.06em", whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(ghostData.by_gate || {}).map(([gate, s]) => {
                    const wr = s.win_rate;
                    const wrColor = wr >= 0.55 ? T.green : wr >= 0.40 ? T.amber : T.red;
                    const pnlColor = (s.net_pnl ?? 0) >= 0 ? T.green : T.red;
                    const pfColor = (s.profit_factor ?? 0) >= 1.5 ? T.green : (s.profit_factor ?? 0) >= 1.0 ? T.amber : T.red;
                    return (
                      <tr key={gate} style={{ borderBottom: `1px solid ${T.border}22` }}>
                        <td style={{ padding: "5px 8px", color: T.cyan, fontWeight: 700, fontSize: 10, whiteSpace: "nowrap" }}>{gate.replace("_GATE","").replace(/_/g," ")}</td>
                        <td style={{ padding: "5px 8px", color: T.text, fontWeight: 800 }}>{s.total}</td>
                        <td style={{ padding: "5px 8px", color: T.green, fontWeight: 700 }}>{s.WIN}</td>
                        <td style={{ padding: "5px 8px", color: T.red, fontWeight: 700 }}>{s.LOSS}</td>
                        <td style={{ padding: "5px 8px", color: T.amber }}>{s.EXPIRED}</td>
                        <td style={{ padding: "5px 8px", fontWeight: 800, color: wrColor }}>{s.WIN + s.LOSS > 0 ? `${(wr * 100).toFixed(0)}%` : "–"}</td>
                        <td style={{ padding: "5px 8px", fontWeight: 800, color: pnlColor }}>{s.net_pnl != null ? `${s.net_pnl >= 0 ? "+" : ""}${s.net_pnl.toFixed(2)}` : "–"}</td>
                        <td style={{ padding: "5px 8px", fontWeight: 800, color: pfColor }}>{s.profit_factor != null ? (s.profit_factor === 999 ? "∞" : s.profit_factor.toFixed(2)) : "–"}</td>
                        <td style={{ padding: "5px 8px", color: T.green }}>{s.avg_win_pnl ? `+${s.avg_win_pnl.toFixed(2)}` : "–"}</td>
                        <td style={{ padding: "5px 8px", color: T.red }}>{s.avg_loss_pnl ? `-${s.avg_loss_pnl.toFixed(2)}` : "–"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Per-symbol table */}
            {ghostData.by_symbol && Object.keys(ghostData.by_symbol).length > 0 && (
              <div style={{ overflowX: "auto", marginBottom: 12 }}>
                <div style={{ fontSize: 9, color: T.mute, letterSpacing: "0.08em", marginBottom: 4 }}>POR SÍMBOLO</div>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                      {["SÍMBOLO", "TOTAL", "WIN", "LOSS", "EXP", "WR", "PnL EST", "PF"].map(h => (
                        <th key={h} style={{ padding: "4px 8px", textAlign: "left", color: T.mute, fontWeight: 700, letterSpacing: "0.06em" }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(ghostData.by_symbol).map(([sym, s]) => {
                      const wr = s.win_rate;
                      const wrColor = wr >= 0.55 ? T.green : wr >= 0.40 ? T.amber : T.red;
                      const pnlColor = (s.net_pnl ?? 0) >= 0 ? T.green : T.red;
                      const pfColor = (s.profit_factor ?? 0) >= 1.5 ? T.green : (s.profit_factor ?? 0) >= 1.0 ? T.amber : T.red;
                      return (
                        <tr key={sym} style={{ borderBottom: `1px solid ${T.border}22` }}>
                          <td style={{ padding: "5px 8px", color: T.text, fontWeight: 800 }}>{sym}</td>
                          <td style={{ padding: "5px 8px", color: T.text }}>{s.total}</td>
                          <td style={{ padding: "5px 8px", color: T.green, fontWeight: 700 }}>{s.WIN}</td>
                          <td style={{ padding: "5px 8px", color: T.red, fontWeight: 700 }}>{s.LOSS}</td>
                          <td style={{ padding: "5px 8px", color: T.amber }}>{s.EXPIRED}</td>
                          <td style={{ padding: "5px 8px", fontWeight: 800, color: wrColor }}>{s.WIN + s.LOSS > 0 ? `${(wr * 100).toFixed(0)}%` : "–"}</td>
                          <td style={{ padding: "5px 8px", fontWeight: 800, color: pnlColor }}>{s.net_pnl != null ? `${s.net_pnl >= 0 ? "+" : ""}${s.net_pnl.toFixed(2)}` : "–"}</td>
                          <td style={{ padding: "5px 8px", fontWeight: 800, color: pfColor }}>{s.profit_factor != null ? (s.profit_factor === 999 ? "∞" : s.profit_factor.toFixed(2)) : "–"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {/* Recent resolved */}
            {ghostData.recent?.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <div style={{ fontSize: 10, color: T.mute, letterSpacing: "0.08em", marginBottom: 6 }}>ÚLTIMOS RESUELTOS</div>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 10 }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                        {["SYM", "GATE", "SCORE", "GRADE", "OUTCOME", "PnL EST", "HACE"].map(h => (
                          <th key={h} style={{ padding: "3px 8px", textAlign: "left", color: T.mute, fontWeight: 700 }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {ghostData.recent.map((r) => {
                        const oc = r.outcome;
                        const ocColor = oc === "GHOST_WIN" ? T.green : oc === "GHOST_LOSS" ? T.red : T.amber;
                        const pnl = r.estimated_pnl;
                        const pnlStr = pnl != null ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "–";
                        const pnlColor = pnl != null ? (pnl >= 0 ? T.green : T.red) : T.mute;
                        const secsAgo = r.outcome_ts ? Math.round(Date.now() / 1000 - r.outcome_ts) : null;
                        const agoStr = secsAgo == null ? "–" : secsAgo < 60 ? `${secsAgo}s` : secsAgo < 3600 ? `${Math.floor(secsAgo/60)}m` : `${Math.floor(secsAgo/3600)}h`;
                        return (
                          <tr key={r.id} style={{ borderBottom: `1px solid ${T.border}18` }}>
                            <td style={{ padding: "4px 8px", color: T.text, fontWeight: 700 }}>{r.symbol}</td>
                            <td style={{ padding: "4px 8px", color: T.cyan, fontSize: 9 }}>{(r.gate||"").replace("_GATE","").replace(/_/g," ")}</td>
                            <td style={{ padding: "4px 8px", color: T.amber, fontFamily: FONT_MONO }}>{r.score_raw?.toFixed(1) ?? "–"}</td>
                            <td style={{ padding: "4px 8px", color: T.text }}>{r.grade ?? "–"}</td>
                            <td style={{ padding: "4px 8px", fontWeight: 800, color: ocColor }}>{oc.replace("GHOST_","")}</td>
                            <td style={{ padding: "4px 8px", fontWeight: 700, color: pnlColor }}>{pnlStr}</td>
                            <td style={{ padding: "4px 8px", color: T.mute }}>{agoStr}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}
