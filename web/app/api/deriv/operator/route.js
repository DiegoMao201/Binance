import fs from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;

const SPIKE_FILE = path.join(DERIV_LOGS, "deriv_spike_events.json");
const OPEN_FILE = path.join(DERIV_LOGS, "deriv_open_contracts.json");
const CLOSED_FILE = path.join(DERIV_LOGS, "deriv_closed_contracts.json");
const STATUS_FILE = path.join(DERIV_LOGS, "deriv_status.json");
const SESSION_FILE = path.join(DERIV_LOGS, "deriv_session.json");
const VISION_FILE = path.join(DERIV_LOGS, "deriv_vision.json");
const COOLDOWN_FILE = path.join(DERIV_LOGS, "d6_cooldown_state.json");
const D67_REGIME_FILE = path.join(DERIV_LOGS, "d67_regime_state.json");
const D70_REGIME_FILE = path.join(DERIV_LOGS, "d70_regime_state.json");

async function readJson(file, fallback) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return fallback;
  }
}

const HOUR = 3600;

// Calcula WR5, PnL2h y consecutive_losses desde los trades reales del JSON.
// El buffer in-memory de D70 se vacía en cada deploy — estos valores son la verdad.
function computeSymbolTradeStats(closedContracts, symbol, nowSec) {
  const symTrades = closedContracts
    .filter((t) => t.symbol === symbol && t.closed_at_ts)
    .sort((a, b) => (a.closed_at_ts || 0) - (b.closed_at_ts || 0));

  if (symTrades.length === 0) {
    return { wr_5: null, pnl_2h: null, consecutive_losses: null, n_trades: 0 };
  }

  const last5 = symTrades.slice(-5);
  const wins5 = last5.filter((t) => (t.realized_pnl_usdt || 0) > 0).length;
  const wr_5 = Math.round((wins5 / last5.length) * 100);

  const ts2hAgo = nowSec - 2 * HOUR;
  const trades2h = symTrades.filter((t) => (t.closed_at_ts || 0) >= ts2hAgo);
  const pnl_2h = Math.round(trades2h.reduce((s, t) => s + (t.realized_pnl_usdt || 0), 0) * 100) / 100;

  let consecutive_losses = 0;
  for (let i = symTrades.length - 1; i >= 0; i--) {
    if ((symTrades[i].realized_pnl_usdt || 0) > 0) break;
    consecutive_losses++;
  }

  return { wr_5, pnl_2h, consecutive_losses, n_trades: symTrades.length };
}

function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function quantile(arr, q) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const pos = (s.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  return s[base + 1] !== undefined ? s[base] + rest * (s[base + 1] - s[base]) : s[base];
}

// Readiness state from elapsed-vs-typical-interval ratio.
function readiness(ratio) {
  if (ratio == null) return { state: "SIN_DATOS", level: 0 };
  if (ratio < 0.5) return { state: "FRESCO", level: 1 };       // acaba de disparar
  if (ratio < 0.9) return { state: "CARGANDO", level: 2 };     // acumulando
  if (ratio < 1.4) return { state: "LISTO", level: 3 };        // ripe — vigilar
  if (ratio < 2.2) return { state: "VENCIDO", level: 4 };      // overdue — alta probabilidad
  return { state: "SECO", level: 5 };                          // muy pasado / mercado lento
}

// Classify a bot decision into a human-readable confirmation signal.
// The bot's `reason` strings carry the live setup state per symbol.
function classifyDecision(d) {
  const reason = String(d?.reason || "");
  const score = Number(d?.score);
  let gate = null;
  const gm = reason.match(/requires?≥?\s*([\d.]+)/i) || reason.match(/≥\s*([\d.]+)/) || reason.match(/strict_min[=:]\s*([\d.]+)/i);
  if (gm) gate = parseFloat(gm[1]);
  // Prefer explicit effective_min from bot (no regex guessing needed).
  if (d?.effective_min != null && Number.isFinite(Number(d.effective_min))) {
    gate = Number(d.effective_min);
  }

  let kind, label, level; // level: 0 idle .. 4 confirmed
  if (d?.allowed === true || /^GO\b/.test(reason)) {
    kind = "CONFIRMADO"; label = "CONFIRMADO — el bot entró"; level = 4;
  } else if (/spike_forced_dir|spike_forced/i.test(reason)) {
    kind = "SPIKE"; label = "SPIKE detectado"; level = 3;
  } else if (/EARLY_ENTRY_GATE/i.test(reason)) {
    kind = "MATURITY"; label = "Warmup activo (EARLY_ENTRY)"; level = 1;
  } else if (/REGIME_SCORE_GATE|SCORE_TOO_LOW|SCORE_GATE/i.test(reason)) {
    kind = "SCORE"; label = "Score bajo para régimen"; level = 2;
  } else if (/IMMINENCE_RIPE_GATE/i.test(reason)) {
    kind = "SCORE"; label = "RIPE: score insuficiente"; level = 2;
  } else if (/SCARCITY_DRY_GATE/i.test(reason)) {
    kind = "SECO"; label = "Mercado seco (umbral reducido)"; level = 2;
  } else if (/TREND_SETUP_GATE/i.test(reason)) {
    kind = "TREND_GATE"; label = "TREND sin FVG"; level = 2;
  } else if (/AI_VETO/i.test(reason)) {
    kind = "AI_VETO"; label = "IA vetó la entrada"; level = 2;
  } else if (/SPIKE_NOT_LOADED/i.test(reason)) {
    kind = "CARGANDO"; label = "Cargando (aún no listo)"; level = 1;
  } else if (/ENTRY_BLOCKED|BLOCK/i.test(reason)) {
    kind = "BLOQUEADO"; label = "Bloqueado"; level = 1;
  } else if (/HURST.*VETO|veto.*hurst/i.test(reason)) {
    kind = "BLOQUEADO"; label = "Hurst veto"; level = 1;
  } else if (/structural_veto|boom_crash_structural|FVG.*VETO/i.test(reason)) {
    kind = "BLOQUEADO"; label = "Veto estructural"; level = 1;
  } else if (/dynamic_symbol_inactive|SYMBOL_INACTIVE/i.test(reason)) {
    kind = "BLOQUEADO"; label = "Símbolo pausado"; level = 1;
  } else if (/trade_cooldown|spike_burst_cooldown|reactivation_awareness/i.test(reason)) {
    kind = "BLOQUEADO"; label = "En cooldown"; level = 0;
  } else if (/veto/i.test(reason)) {
    kind = "BLOQUEADO"; label = reason.split(":")[0] || "Vetado"; level = 1;
  } else {
    kind = "INFO"; label = reason.split(":")[0] || "—"; level = 0;
  }
  const gap = gate != null && Number.isFinite(score) ? +(gate - score).toFixed(2) : null;
  return {
    kind, label, level, gate, gap,
    gateName: d?.gate_name ?? null,
    fvgBosValidated: d?.fvg_bos_validated ?? null,
    remainSec: d?.remain_sec ?? null,
  };
}

export async function GET() {
  const nowSec = Date.now() / 1000;

  const [spikesRaw, openRaw, closedRaw, status, session, visionRaw, cooldownRaw, d67Raw, d70Raw] = await Promise.all([
    readJson(SPIKE_FILE, []),
    readJson(OPEN_FILE, []),
    readJson(CLOSED_FILE, []),
    readJson(STATUS_FILE, {}),
    readJson(SESSION_FILE, null),
    readJson(VISION_FILE, {}),
    readJson(COOLDOWN_FILE, {}),
    readJson(D67_REGIME_FILE, {}),
    readJson(D70_REGIME_FILE, {}),
  ]);
  const cooldownBySymbol = (cooldownRaw && typeof cooldownRaw === "object") ? (cooldownRaw.symbols || {}) : {};
  const d67BySymbol = (d67Raw && typeof d67Raw === "object") ? (d67Raw.symbols || {}) : {};
  const d70BySymbol = (d70Raw && typeof d70Raw === "object") ? (d70Raw.symbols || {}) : {};
  const visionData = (visionRaw && typeof visionRaw === "object") ? visionRaw : {};

  const spikes = (Array.isArray(spikesRaw) ? spikesRaw : [])
    .filter((s) => s && Number.isFinite(Number(s.ts)))
    .map((s) => ({ ...s, ts: Number(s.ts) }))
    .sort((a, b) => a.ts - b.ts);

  const openContracts = Array.isArray(openRaw) ? openRaw : [];
  const closedContracts = Array.isArray(closedRaw) ? closedRaw : [];

  // Live decision stream + tick/analyst context from the bot's status file.
  const lastDecisions = Array.isArray(status?.last_decisions) ? status.last_decisions : [];
  const tickContext = status?.symbol_tick_context || {};
  const analystSummary = status?.analyst_summary || {};
  const activeSymbols = Array.isArray(status?.symbols) ? status.symbols : [];

  // Index decisions per symbol (chronological).
  const decisionsBySymbol = new Map();
  const decoratedDecisions = [];
  for (const d of lastDecisions) {
    if (!d?.symbol) continue;
    const ts = d.ts ? Date.parse(d.ts) / 1000 : null;
    const cls = classifyDecision(d);
    const rec = {
      symbol: d.symbol, ts, iso: d.ts, score: Number(d.score),
      side: d.side, regime: d.regime, allowed: d.allowed === true,
      reason: d.reason, ...cls,
    };
    decoratedDecisions.push(rec);
    if (!decisionsBySymbol.has(d.symbol)) decisionsBySymbol.set(d.symbol, []);
    decisionsBySymbol.get(d.symbol).push(rec);
  }

  // Restrict to the symbols the bot is actively trading (drops stale BOOM500 etc).
  // Discover symbols from active list first, fall back to live data.
  const symbolSet = new Set();
  if (activeSymbols.length) {
    for (const s of activeSymbols) symbolSet.add(s);
  } else {
    for (const s of spikes) if (s.symbol) symbolSet.add(s.symbol);
  }
  for (const c of openContracts) if (c.symbol) symbolSet.add(c.symbol);

  const openBySymbol = new Map();
  for (const c of openContracts) {
    if (c.symbol) openBySymbol.set(c.symbol, c);
  }

  const symbols = [];
  for (const symbol of symbolSet) {
    const sym = spikes.filter((s) => s.symbol === symbol);

    const last = sym.length ? sym[sym.length - 1] : null;
    const secsSince = last ? nowSec - last.ts : null;

    const count = (winSec) => sym.filter((s) => nowSec - s.ts <= winSec).length;
    const c1 = count(1 * HOUR);
    const c6 = count(6 * HOUR);
    const c12 = count(12 * HOUR);
    const c24 = count(24 * HOUR);

    // Gaps between consecutive spikes within last 24h → typical interval.
    const recent24 = sym.filter((s) => nowSec - s.ts <= 24 * HOUR);
    const gaps = [];
    for (let i = 1; i < recent24.length; i++) gaps.push(recent24[i].ts - recent24[i - 1].ts);
    const medianGap = median(gaps);
    const ratio = medianGap && secsSince != null ? secsSince / medianGap : null;
    const ready = readiness(ratio);

    const entered24 = recent24.filter((s) => s.bot_entered === true).length;
    const blocked24 = recent24.filter((s) => s.bot_entered === false).length;

    // Top block reasons (the "notes" per symbol).
    const reasonCounts = {};
    for (const s of recent24) {
      if (s.bot_entered === false && s.block_reason) {
        const key = String(s.block_reason).split(":")[0];
        reasonCounts[key] = (reasonCounts[key] || 0) + 1;
      }
    }
    const topReasons = Object.entries(reasonCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4)
      .map(([reason, n]) => ({ reason, n }));

    // ── Live confirmation/score state from the bot's decision stream ──────
    const symDecisions = decisionsBySymbol.get(symbol) || [];
    const liveDecision = symDecisions.length ? symDecisions[symDecisions.length - 1] : null;
    // Latest known score gate + gate diagnostics from recent decisions.
    let liveGate = null;
    let liveGateName = null;
    let liveFvgBosValidated = null;
    let liveEarlyEntryActive = false;
    let liveEarlyEntryRemainSec = null;
    for (let i = symDecisions.length - 1; i >= 0 && i >= symDecisions.length - 30; i--) {
      const _d = symDecisions[i];
      // EARLY_ENTRY_GATE: only valid if decision is fresh (< 35s — emits every 30s)
      if (!liveEarlyEntryActive && _d.gateName === "EARLY_ENTRY_GATE") {
        if (_d.ts && (nowSec - _d.ts) < 35) {
          liveEarlyEntryActive = true;
          liveEarlyEntryRemainSec = _d.remainSec ?? null;
        }
      }
      if (liveGate == null && _d.gate != null) {
        liveGate = _d.gate;
        liveGateName = _d.gateName ?? null;
      }
      if (liveFvgBosValidated == null && _d.fvgBosValidated != null) {
        liveFvgBosValidated = _d.fvgBosValidated;
      } else if (liveFvgBosValidated == null && _d.score_breakdown?.fvg_bos_validated != null) {
        liveFvgBosValidated = Boolean(_d.score_breakdown.fvg_bos_validated);
      }
    }
    // Walk back up to 20 decisions to find the last one with a real score.
    // Cooldown/geo decisions often have no score field — don't show "—" because of them.
    let liveScore = null;
    let liveScoreDecision = null;
    for (let i = symDecisions.length - 1; i >= 0 && i >= symDecisions.length - 20; i--) {
      if (Number.isFinite(symDecisions[i].score) && symDecisions[i].score > 0) {
        liveScore = symDecisions[i].score;
        liveScoreDecision = symDecisions[i];
        break;
      }
    }
    const scoreGap = liveGate != null && liveScore != null ? +(liveGate - liveScore).toFixed(2) : null;
    // Recent confirmations: meaningful detections (spike / score forming / confirmed).
    const confirmations = symDecisions
      .filter((d) => d.level >= 2)
      .slice(-8)
      .reverse()
      .map((d) => ({
        ts: d.ts, score: d.score, side: d.side, regime: d.regime,
        kind: d.kind, label: d.label, gate: d.gate, gap: d.gap, allowed: d.allowed,
      }));
    const ctx = tickContext[symbol] || {};
    const an = analystSummary[symbol] || {};

    symbols.push({
      symbol,
      live: {
        score: liveScore,
        gate: liveGate,
        scoreGap,
        gateName: liveGateName,
        fvgBosValidated: liveFvgBosValidated,
        earlyEntryActive: liveEarlyEntryActive,
        earlyEntryRemainSec: liveEarlyEntryRemainSec,
        regime: liveScoreDecision?.regime || liveDecision?.regime || null,
        kind: liveDecision?.kind || null,
        label: liveDecision?.label || null,
        side: liveScoreDecision?.side || liveDecision?.side || null,
        ts: liveDecision?.ts || null,
        ticksSinceSpike: ctx.ticks_since_last_spike ?? last?.ticks_since_last_spike ?? null,
        hurst: an.hurst ?? null,
        volRegime: an.vol_regime ?? null,
        lastPrice: an.last_price ?? null,
      },
      confirmations,
      lastSpike: last
        ? {
            ts: last.ts,
            iso: last.iso,
            direction: last.direction,
            ratio: last.ratio,
            jump: last.jump,
            price: last.price,
            ticks_since_last_spike: last.ticks_since_last_spike,
            price_vs_ema200_pct: last.price_vs_ema200_pct,
            bot_entered: last.bot_entered,
            block_reason: last.block_reason,
            had_open_pos: last.had_open_pos,
            spike_cluster: last.spike_cluster,
          }
        : null,
      secsSinceLastSpike: secsSince,
      counts: { h1: c1, h6: c6, h12: c12, h24: c24 },
      ratePerHour: {
        h6: c6 / 6,
        h12: c12 / 12,
        h24: c24 / 24,
      },
      medianGapSec: medianGap,
      p75GapSec: quantile(gaps, 0.75),
      readiness: ready,
      entered24,
      blocked24,
      catchRate24: c24 ? entered24 / c24 : null,
      topReasons,
      openContract: openBySymbol.get(symbol) || null,
      recentSpikes: sym.slice(-12).reverse(),
      vision: visionData[symbol] || null,
      postRachaCooldown: cooldownBySymbol[symbol] || null,
      d67Regime: d67BySymbol[symbol] || null,
      d70Regime: (() => {
        const base = d70BySymbol[symbol] || null;
        if (!base) return null;
        const stats = computeSymbolTradeStats(closedContracts, symbol, nowSec);
        if (stats.n_trades > 0) {
          return { ...base, wr_5: stats.wr_5, pnl_2h: stats.pnl_2h, consecutive_losses: stats.consecutive_losses };
        }
        return base;
      })(),
    });
  }

  // Sort: symbols with an open position first, then by readiness level desc, then by activity.
  symbols.sort((a, b) => {
    const ao = a.openContract ? 1 : 0;
    const bo = b.openContract ? 1 : 0;
    if (ao !== bo) return bo - ao;
    if (b.readiness.level !== a.readiness.level) return b.readiness.level - a.readiness.level;
    return b.counts.h24 - a.counts.h24;
  });

  // Global confirmation feed — the bot's meaningful detections across all symbols,
  // newest first. This is what the manual operator watches to decide.
  // Deduplicate high-frequency noise gates (SECO/TREND_GATE/AI_VETO) — keep only the
  // most recent entry per (symbol, kind) so they don't flood the feed.
  const _seenNoisy = new Set();
  const confirmationFeed = decoratedDecisions
    .filter((d) => d.level >= 2 && d.ts)
    .sort((a, b) => b.ts - a.ts)
    .filter((d) => {
      if (d.kind === "SECO" || d.kind === "TREND_GATE" || d.kind === "AI_VETO") {
        const key = `${d.symbol}:${d.kind}`;
        if (_seenNoisy.has(key)) return false;
        _seenNoisy.add(key);
      }
      return true;
    })
    .slice(0, 30)
    .map((d) => ({
      ts: d.ts,
      symbol: d.symbol,
      score: d.score,
      gate: d.gate,
      gap: d.gap,
      side: d.side,
      regime: d.regime,
      kind: d.kind,
      label: d.label,
      allowed: d.allowed,
      secsAgo: nowSec - d.ts,
    }));

  // Global spike feed (newest first).
  const spikeFeed = spikes.slice(-40).reverse().map((s) => ({
    ts: s.ts,
    iso: s.iso,
    symbol: s.symbol,
    direction: s.direction,
    ratio: s.ratio,
    jump: s.jump,
    bot_entered: s.bot_entered,
    block_reason: s.block_reason,
    had_open_pos: s.had_open_pos,
    secsAgo: nowSec - s.ts,
  }));

  // Enriched spike table — per spike: ordinal within the rolling last hour,
  // gap since previous spike of the same symbol, ticks-since-prev-spike.
  // Only symbols the bot is actively trading.
  const prevTsBySym = new Map();
  const enriched = spikes.map((s) => {
    const prevTs = prevTsBySym.get(s.symbol) ?? null;
    prevTsBySym.set(s.symbol, s.ts);
    const seqInHour = spikes.filter(
      (x) => x.symbol === s.symbol && x.ts <= s.ts && x.ts > s.ts - HOUR
    ).length;
    return {
      ts: s.ts,
      symbol: s.symbol,
      direction: s.direction,
      ratio: s.ratio,
      atr: s.atr,
      price: s.price,
      ticks_since_last_spike: s.ticks_since_last_spike,
      gapPrevSec: prevTs != null ? +(s.ts - prevTs).toFixed(1) : null,
      seqInHour,
      bot_entered: s.bot_entered,
      block_reason: s.block_reason,
      had_open_pos: s.had_open_pos,
    };
  });
  const spikeTable = enriched
    .filter((s) => symbolSet.has(s.symbol))
    .slice(-150)
    .reverse()
    .map((s) => ({ ...s, secsAgo: nowSec - s.ts }));

  // Session-level realized PnL (today) for the header.
  let sinceSec = 0;
  if (session && Number(session.session_start_ts)) {
    const ts = Number(session.session_start_ts);
    sinceSec = ts > 1e12 ? ts / 1000 : ts;
  }
  const sessionClosed = closedContracts.filter(
    (c) => !sinceSec || Number(c.closed_at_ts) >= sinceSec
  );
  const sessionPnl = sessionClosed.reduce(
    (acc, c) => acc + (Number(c.realized_pnl_usdt) || 0),
    0
  );
  const sessionWins = sessionClosed.filter((c) => Number(c.realized_pnl_usdt) > 0).length;

  return NextResponse.json(
    {
      serverTime: new Date().toISOString(),
      serverTs: nowSec,
      status: {
        state: status?.state || status?.status || null,
        message: status?.message || null,
        balance: status?.balance ?? status?.equity ?? null,
      },
      session: {
        sinceSec,
        realizedPnl: sessionPnl,
        trades: sessionClosed.length,
        wins: sessionWins,
        winRate: sessionClosed.length ? sessionWins / sessionClosed.length : null,
      },
      totals: {
        symbols: symbols.length,
        openPositions: openContracts.length,
        spikes24h: spikes.filter((s) => nowSec - s.ts <= 24 * HOUR).length,
      },
      symbols,
      openContracts,
      confirmationFeed,
      spikeFeed,
      spikeTable,
    },
    { headers: { "Cache-Control": "no-store, max-age=0" } }
  );
}
