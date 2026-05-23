import fs from "node:fs/promises";
import path from "node:path";
import { prisma } from "../../../lib/db";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;
const AI_DECISIONS_LOOKBACK = 220;
const AI_TRADES_LOOKBACK = 80;
const PATTERN_MEMORY_LIMIT = 30;

async function readJson(fileName, fallback, dir = DERIV_LOGS) {
  try {
    const content = await fs.readFile(path.join(dir, fileName), "utf8");
    return JSON.parse(content);
  } catch { return fallback; }
}

const pnlOf = c => Number(c?.realized_pnl_usdt ?? c?.pnl ?? 0) || 0;
const tsOf = c => {
  const t = Number(c?.closed_at_ts ?? c?.opened_at_ts ?? c?.ts ?? 0);
  return t > 1e12 ? t : t * 1000;
};
const holdOf = c => {
  const o = Number(c?.opened_at_ts ?? 0);
  const cl = Number(c?.closed_at_ts ?? 0);
  if (!o || !cl) return null;
  return Math.max(0, (cl - o) * 1000);
};
const symOf = c => c?.symbol || c?.underlying || "?";
const sideOf = c => c?.side || c?.contract_type || "?";
const regimeOf = c => c?.score_breakdown?.regime || c?.regime || "UNKNOWN";
const strategyModeOf = c => {
  const bd = c?.score_breakdown || {};
  if (bd.spike_entry) return "SPIKE";
  if (bd.micro_scalp) return "SCALP";
  if (bd.mean_rev_mode) return "MEAN_REV";
  if (bd.fvg_active) return "FVG";
  return bd.strategy || "TREND";
};
const hurstZoneOf = c => c?.score_breakdown?.hurst_zone || "—";
const scoreBand = s => {
  if (s == null || !Number.isFinite(s)) return "?";
  if (s < 4) return "<4";
  if (s < 5) return "4-5";
  if (s < 6) return "5-6";
  if (s < 7) return "6-7";
  if (s < 8) return "7-8";
  if (s < 9) return "8-9";
  return "9+";
};
const isoDay = ts => new Date(ts).toISOString().slice(0, 10);

function emptyBucket() {
  return { n: 0, wins: 0, losses: 0, pnl: 0, gross_profit: 0, gross_loss: 0, hold_sum: 0, hold_n: 0 };
}
function pushBucket(b, c) {
  const p = pnlOf(c);
  b.n++; b.pnl += p;
  if (p > 0) { b.wins++; b.gross_profit += p; }
  else if (p < 0) { b.losses++; b.gross_loss += -p; }
  const h = holdOf(c);
  if (h != null) { b.hold_sum += h; b.hold_n++; }
}
function finalizeBucket(b) {
  return {
    n: b.n, wins: b.wins, losses: b.losses,
    pnl: +b.pnl.toFixed(4),
    winrate: b.n ? b.wins / b.n : 0,
    profit_factor: b.gross_loss > 0 ? +(b.gross_profit / b.gross_loss).toFixed(3) : (b.gross_profit > 0 ? null : 0),
    expectancy: b.n ? +(b.pnl / b.n).toFixed(4) : 0,
    avg_win: b.wins ? +(b.gross_profit / b.wins).toFixed(4) : 0,
    avg_loss: b.losses ? +(-b.gross_loss / b.losses).toFixed(4) : 0,
    avg_hold_sec: b.hold_n ? +(b.hold_sum / b.hold_n / 1000).toFixed(1) : null,
  };
}
function aggBy(arr, keyFn) {
  const m = {};
  for (const c of arr) {
    const k = keyFn(c) ?? "?";
    (m[k] ||= emptyBucket()); pushBucket(m[k], c);
  }
  const out = {};
  for (const k of Object.keys(m)) out[k] = finalizeBucket(m[k]);
  return out;
}
function rolling(arr, w) {
  const out = [];
  for (let i = 0; i < arr.length; i++) {
    const slice = arr.slice(Math.max(0, i - w + 1), i + 1);
    const b = emptyBucket();
    for (const c of slice) pushBucket(b, c);
    const f = finalizeBucket(b);
    out.push({ i, ts: tsOf(arr[i]), winrate: f.winrate, profit_factor: f.profit_factor || 0, expectancy: f.expectancy });
  }
  return out;
}
function maxDrawdown(eq) {
  let peak = -Infinity, peakV = 0, maxDd = 0, maxDdPct = 0, valley = 0;
  for (const p of eq) {
    if (p.pnl > peak) { peak = p.pnl; peakV = p.pnl; }
    const dd = peak - p.pnl;
    if (dd > maxDd) { maxDd = dd; valley = p.pnl; maxDdPct = peak !== 0 ? dd / Math.max(1, Math.abs(peak)) : 0; }
  }
  return { max_dd: +maxDd.toFixed(4), max_dd_pct: +maxDdPct.toFixed(4), peak: +peakV.toFixed(4), valley: +valley.toFixed(4) };
}
function pearson(x, y) {
  const n = Math.min(x.length, y.length);
  if (n < 2) return 0;
  let sx = 0, sy = 0, sxy = 0, sx2 = 0, sy2 = 0, c = 0;
  for (let i = 0; i < n; i++) {
    const a = Number(x[i]), b = Number(y[i]);
    if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
    sx += a; sy += b; sxy += a * b; sx2 += a * a; sy2 += b * b; c++;
  }
  if (c < 2) return 0;
  const num = c * sxy - sx * sy;
  const den = Math.sqrt((c * sx2 - sx * sx) * (c * sy2 - sy * sy));
  return den === 0 ? 0 : +(num / den).toFixed(3);
}

function toRatio(v) {
  const x = Number(v);
  if (!Number.isFinite(x)) return 0;
  if (x > 1.01) return x / 100;
  if (x < 0) return 0;
  return x;
}

function computeAiQualityBySymbol(aiDecisions, closes) {
  const bySymbol = {};
  const aiRows = (Array.isArray(aiDecisions) ? aiDecisions : []).slice(-AI_DECISIONS_LOOKBACK);
  const closeRows = (Array.isArray(closes) ? closes : []).slice(-AI_TRADES_LOOKBACK);

  for (const row of aiRows) {
    const sym = String(row?.symbol || "").toUpperCase();
    if (!sym) continue;
    bySymbol[sym] ||= {
      symbol: sym,
      decisions_n: 0,
      approvals_n: 0,
      trades_n: 0,
      wins_n: 0,
      ev_sum: 0,
      weak_exits_n: 0,
    };
    bySymbol[sym].decisions_n += 1;
    if (Boolean(row?.approved)) bySymbol[sym].approvals_n += 1;
  }

  for (const row of closeRows) {
    const sym = String(row?.symbol || "").toUpperCase();
    if (!sym) continue;
    bySymbol[sym] ||= {
      symbol: sym,
      decisions_n: 0,
      approvals_n: 0,
      trades_n: 0,
      wins_n: 0,
      ev_sum: 0,
      weak_exits_n: 0,
    };
    const pnl = Number(row?.realized_pnl_usdt ?? 0) || 0;
    const reason = String(row?.exit_reason || row?.close_reason || "").toLowerCase();
    bySymbol[sym].trades_n += 1;
    bySymbol[sym].ev_sum += pnl;
    if (pnl > 0) bySymbol[sym].wins_n += 1;
    if (reason === "zero_peak_exit" || reason === "spike_timeout" || reason === "sl_inicial") {
      bySymbol[sym].weak_exits_n += 1;
    }
  }

  const rows = Object.values(bySymbol).map((r) => {
    const approval_rate = r.decisions_n ? r.approvals_n / r.decisions_n : 0;
    const win_rate = r.trades_n ? r.wins_n / r.trades_n : 0;
    const weak_exit_rate = r.trades_n ? r.weak_exits_n / r.trades_n : 0;
    const ev_per_trade = r.trades_n ? r.ev_sum / r.trades_n : 0;
    let pressure = 0;
    if (r.decisions_n >= 12 && approval_rate >= 0.82) pressure += 0.3;
    if (r.decisions_n >= 12 && approval_rate >= 0.90) pressure += 0.15;
    if (r.trades_n >= 8 && win_rate < 0.35) pressure += 0.3;
    if (r.trades_n >= 8 && ev_per_trade < 0) pressure += 0.2;
    if (r.trades_n >= 8 && weak_exit_rate >= 0.55) pressure += 0.1;
    return {
      symbol: r.symbol,
      decisions_n: r.decisions_n,
      approvals_n: r.approvals_n,
      trades_n: r.trades_n,
      wins_n: r.wins_n,
      approval_rate: +approval_rate.toFixed(4),
      win_rate: +win_rate.toFixed(4),
      weak_exit_rate: +weak_exit_rate.toFixed(4),
      ev_per_trade: +ev_per_trade.toFixed(4),
      adaptive_pressure: +Math.min(1, pressure).toFixed(4),
    };
  }).sort((a, b) => b.adaptive_pressure - a.adaptive_pressure || b.decisions_n - a.decisions_n);

  const summaryBase = rows.reduce((acc, r) => {
    acc.decisions_n += r.decisions_n;
    acc.approvals_n += r.approvals_n;
    acc.trades_n += r.trades_n;
    acc.wins_n += r.wins_n;
    acc.ev_sum += (r.ev_per_trade * r.trades_n);
    return acc;
  }, { decisions_n: 0, approvals_n: 0, trades_n: 0, wins_n: 0, ev_sum: 0 });

  return {
    by_symbol: rows,
    summary: {
      decisions_n: summaryBase.decisions_n,
      approvals_n: summaryBase.approvals_n,
      trades_n: summaryBase.trades_n,
      wins_n: summaryBase.wins_n,
      approval_rate: summaryBase.decisions_n ? +(summaryBase.approvals_n / summaryBase.decisions_n).toFixed(4) : 0,
      win_rate: summaryBase.trades_n ? +(summaryBase.wins_n / summaryBase.trades_n).toFixed(4) : 0,
      ev_per_trade: summaryBase.trades_n ? +(summaryBase.ev_sum / summaryBase.trades_n).toFixed(4) : 0,
    },
  };
}

function buildPatternFallback(closes) {
  const rows = (Array.isArray(closes) ? closes : []).slice(-220);
  const map = new Map();
  for (const row of rows) {
    const sb = (typeof row?.score_breakdown === "object" && row?.score_breakdown) ? row.score_breakdown : {};
    const symbol = String(row?.symbol || "").toUpperCase();
    if (!symbol) continue;
    const side = String(row?.side || "UNKNOWN").toUpperCase();
    const setup = String(sb?.setup_type || sb?.entry_setup || "unknown").toLowerCase();
    const regime = String(sb?.market_regime || sb?.regime || "NORMAL").toUpperCase();
    const score = Math.round(((Number(row?.score ?? sb?.score_raw ?? 0) || 0) / 0.25)) * 0.25;
    const hurst = Math.round(((Number(sb?.hurst ?? 0) || 0) / 0.02)) * 0.02;
    const atr = Math.round(((Number(sb?.atr_pct_at_entry ?? sb?.atr_pct ?? 0) || 0) / 0.001)) * 0.001;
    const geo = Math.round(((Number(sb?.geo_channel_pos ?? 0) || 0) / 0.1)) * 0.1;
    const key = `${symbol}|${side}|${setup}|${regime}|${score}|${hurst}|${atr}|${geo}`;
    if (!map.has(key)) {
      map.set(key, {
        symbol,
        side,
        setup_type: setup,
        regime,
        score_bucket: +score.toFixed(2),
        hurst_bucket: +hurst.toFixed(2),
        atr_bucket: +atr.toFixed(4),
        geo_bucket: +geo.toFixed(2),
        sample_trades: 0,
        wins: 0,
        losses: 0,
        pnl_sum: 0,
      });
    }
    const slot = map.get(key);
    const pnl = Number(row?.realized_pnl_usdt ?? 0) || 0;
    slot.sample_trades += 1;
    slot.pnl_sum += pnl;
    if (pnl > 0) slot.wins += 1;
    else if (pnl < 0) slot.losses += 1;
  }
  return Array.from(map.values())
    .map((r) => ({
      symbol: r.symbol,
      side: r.side,
      setup_type: r.setup_type,
      regime: r.regime,
      score_bucket: r.score_bucket,
      hurst_bucket: r.hurst_bucket,
      atr_bucket: r.atr_bucket,
      geo_bucket: r.geo_bucket,
      sample_trades: r.sample_trades,
      wins: r.wins,
      losses: r.losses,
      win_rate: r.sample_trades ? +(r.wins / r.sample_trades).toFixed(4) : 0,
      avg_pnl_usdt: r.sample_trades ? +(r.pnl_sum / r.sample_trades).toFixed(4) : 0,
      source: "fallback_logs",
    }))
    .filter((r) => r.sample_trades >= 3)
    .sort((a, b) => b.win_rate - a.win_rate || b.sample_trades - a.sample_trades)
    .slice(0, PATTERN_MEMORY_LIMIT);
}

async function fetchPatternMemory(closes) {
  try {
    const rows = await prisma.$queryRaw`
      SELECT
        symbol,
        side,
        setup_type,
        regime,
        score_bucket,
        hurst_bucket,
        atr_bucket,
        geo_bucket,
        sample_trades,
        wins,
        losses,
        win_rate,
        avg_pnl_usdt,
        last_trade_ts
      FROM ai_entry_pattern_memory
      ORDER BY updated_at DESC
      LIMIT ${PATTERN_MEMORY_LIMIT}
    `;
    const normalized = (rows || [])
      .map((r) => ({
        symbol: String(r.symbol || ""),
        side: String(r.side || ""),
        setup_type: String(r.setup_type || ""),
        regime: String(r.regime || ""),
        score_bucket: +(Number(r.score_bucket) || 0).toFixed(2),
        hurst_bucket: +(Number(r.hurst_bucket) || 0).toFixed(2),
        atr_bucket: +(Number(r.atr_bucket) || 0).toFixed(4),
        geo_bucket: +(Number(r.geo_bucket) || 0).toFixed(2),
        sample_trades: Number(r.sample_trades) || 0,
        wins: Number(r.wins) || 0,
        losses: Number(r.losses) || 0,
        win_rate: +toRatio(r.win_rate).toFixed(4),
        avg_pnl_usdt: +(Number(r.avg_pnl_usdt) || 0).toFixed(4),
        last_trade_ts: r.last_trade_ts ? new Date(r.last_trade_ts).toISOString() : null,
        source: "db",
      }))
      .filter((r) => r.symbol);
    if (normalized.length) return normalized;
    return buildPatternFallback(closes);
  } catch {
    return buildPatternFallback(closes);
  }
}

export async function GET() {
  const [status, open, closed, sessionFile, aiDecisions] = await Promise.all([
    readJson("deriv_status.json", {}),
    readJson("deriv_open_contracts.json", []),
    readJson("deriv_closed_contracts.json", []),
    readJson("deriv_session.json", null, LOGS),
    readJson("deriv_ai_decisions.json", []),
  ]);
  const allClosed = (Array.isArray(closed) ? closed : []).slice().sort((a, b) => tsOf(a) - tsOf(b));
  // Session filter: only for global KPIs (SESSION, WIN%, PF, TRADES). Reports unaffected.
  const sessionStartMs = sessionFile?.session_start_ts ?? null;
  const sessionClosed = sessionStartMs
    ? allClosed.filter(c => tsOf(c) >= sessionStartMs)
    : allClosed;
  const openContracts = Array.isArray(open) ? open : [];

  let cum = 0;
  const equity_curve = allClosed.map(c => {
    const p = pnlOf(c);
    cum += p;
    return { ts: tsOf(c), pnl: +cum.toFixed(4), trade_pnl: +p.toFixed(4), symbol: symOf(c) };
  });

  const dailyMap = {};
  for (const c of allClosed) {
    const d = isoDay(tsOf(c));
    (dailyMap[d] ||= { date: d, pnl: 0, trades: 0, wins: 0 });
    dailyMap[d].pnl += pnlOf(c);
    dailyMap[d].trades++;
    if (pnlOf(c) > 0) dailyMap[d].wins++;
  }
  const daily_pnl = Object.values(dailyMap)
    .map(d => ({ ...d, pnl: +d.pnl.toFixed(4), winrate: d.trades ? d.wins / d.trades : 0 }))
    .sort((a, b) => a.date.localeCompare(b.date));

  const today = new Date();
  const calendar = [];
  for (let i = 89; i >= 0; i--) {
    const d = new Date(today); d.setDate(today.getDate() - i);
    const k = d.toISOString().slice(0, 10);
    const v = dailyMap[k];
    calendar.push({ date: k, pnl: v ? +v.pnl.toFixed(4) : 0, trades: v ? v.trades : 0 });
  }

  const heatmap_pnl = Array.from({ length: 7 }, () => Array(24).fill(0));
  const heatmap_count = Array.from({ length: 7 }, () => Array(24).fill(0));
  for (const c of allClosed) {
    const dt = new Date(tsOf(c));
    if (isNaN(dt)) continue;
    heatmap_pnl[dt.getDay()][dt.getHours()] += pnlOf(c);
    heatmap_count[dt.getDay()][dt.getHours()] += 1;
  }
  for (let d = 0; d < 7; d++) for (let h = 0; h < 24; h++) heatmap_pnl[d][h] = +heatmap_pnl[d][h].toFixed(4);

  const by_regime    = aggBy(allClosed, regimeOf);
  const by_strategy  = aggBy(allClosed, strategyModeOf);
  const by_hurst_zone = aggBy(allClosed, hurstZoneOf);
  const by_score_band = aggBy(allClosed, c => scoreBand(Number(c?.score)));
  const by_side      = aggBy(allClosed, c => sideOf(c) === "MULTUP" ? "LONG" : sideOf(c) === "MULTDOWN" ? "SHORT" : "?");
  const by_exit_reason = aggBy(allClosed, c => c?.exit_reason || "—");

  const by_symbol = {};
  const symbolGroups = {};
  for (const c of allClosed) (symbolGroups[symOf(c)] ||= []).push(c);
  for (const [s, trades] of Object.entries(symbolGroups)) {
    const bAll = emptyBucket(), bL = emptyBucket(), bS = emptyBucket();
    const regCnt = {};
    for (const c of trades) {
      pushBucket(bAll, c);
      const side = sideOf(c);
      if (side === "MULTUP") pushBucket(bL, c);
      else if (side === "MULTDOWN") pushBucket(bS, c);
      const r = regimeOf(c); regCnt[r] = (regCnt[r] || 0) + 1;
    }
    by_symbol[s] = { ...finalizeBucket(bAll), long: finalizeBucket(bL), short: finalizeBucket(bS), regime_counts: regCnt };
  }

  const rolling_20 = rolling(allClosed, 20);
  const rolling_50 = rolling(allClosed, 50);

  const scatterPool = allClosed.map(c => {
    const bd = c?.score_breakdown || {};
    return {
      ts: tsOf(c),
      symbol: symOf(c),
      side: sideOf(c),
      score: Number(c?.score ?? bd.score_raw ?? NaN),
      hurst: Number(bd.hurst ?? NaN),
      atr_pct: Number(bd.atr_pct ?? NaN),
      pnl: pnlOf(c),
      hold: holdOf(c) ? holdOf(c) / 1000 : null,
      regime: regimeOf(c),
    };
  });
  let scatter = scatterPool;
  if (scatterPool.length > 800) {
    const step = scatterPool.length / 800;
    scatter = [];
    for (let i = 0; i < scatterPool.length; i += step) scatter.push(scatterPool[Math.floor(i)]);
  }

  const cols = { score: [], hurst: [], atr_pct: [], pnl: [], hold: [] };
  for (const p of scatterPool) {
    cols.score.push(p.score); cols.hurst.push(p.hurst); cols.atr_pct.push(p.atr_pct);
    cols.pnl.push(p.pnl); cols.hold.push(p.hold);
  }
  const corrKeys = Object.keys(cols);
  const correlations = {};
  for (const a of corrKeys) {
    correlations[a] = {};
    for (const b of corrKeys) correlations[a][b] = a === b ? 1 : pearson(cols[a], cols[b]);
  }

  const scoreHist = Array(11).fill(0);
  const hurstHist = Array(20).fill(0);
  for (const p of scatterPool) {
    if (Number.isFinite(p.score)) scoreHist[Math.max(0, Math.min(10, Math.floor(p.score)))]++;
    if (Number.isFinite(p.hurst)) hurstHist[Math.max(0, Math.min(19, Math.floor(p.hurst * 20)))]++;
  }

  let curType = null, curN = 0, maxW = 0, maxL = 0;
  for (const c of sessionClosed) {
    const p = pnlOf(c);
    const t = p > 0 ? "W" : p < 0 ? "L" : null;
    if (!t) continue;
    if (t === curType) curN++; else { curType = t; curN = 1; }
    if (t === "W") maxW = Math.max(maxW, curN); else maxL = Math.max(maxL, curN);
  }

  // Global KPIs use sessionClosed (filtered from session_start_ts). Reports use allClosed.
  const totalPnl = sessionClosed.reduce((s, c) => s + pnlOf(c), 0);
  const wins = sessionClosed.filter(c => pnlOf(c) > 0);
  const losses = sessionClosed.filter(c => pnlOf(c) < 0);
  const grossP = wins.reduce((s, c) => s + pnlOf(c), 0);
  const grossL = -losses.reduce((s, c) => s + pnlOf(c), 0);
  const holdValid = sessionClosed.map(holdOf).filter(h => h != null);
  const avgDur = holdValid.length ? holdValid.reduce((s, h) => s + h, 0) / holdValid.length / 1000 : null;

  const pnlArr = sessionClosed.map(pnlOf);
  const mean = pnlArr.length ? pnlArr.reduce((a, b) => a + b, 0) / pnlArr.length : 0;
  const variance = pnlArr.length ? pnlArr.reduce((a, b) => a + (b - mean) ** 2, 0) / pnlArr.length : 0;
  const std = Math.sqrt(variance);
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;

  const todayKey = new Date().toISOString().slice(0, 10);
  const todayPnl = sessionClosed
    .filter(c => isoDay(tsOf(c)) === todayKey)
    .reduce((s, c) => s + pnlOf(c), 0);

  const global = {
    total_trades: sessionClosed.length,
    wins: wins.length,
    losses: losses.length,
    winrate: sessionClosed.length ? wins.length / sessionClosed.length : 0,
    profit_factor: grossL > 0 ? +(grossP / grossL).toFixed(3) : (grossP > 0 ? null : 0),
    expectancy: sessionClosed.length ? +(totalPnl / sessionClosed.length).toFixed(4) : 0,
    avg_win: wins.length ? +(grossP / wins.length).toFixed(4) : 0,
    avg_loss: losses.length ? +(-grossL / losses.length).toFixed(4) : 0,
    gross_profit: +grossP.toFixed(4),
    gross_loss: +grossL.toFixed(4),
    session_pnl: +totalPnl.toFixed(4),
    today_pnl: +todayPnl.toFixed(4),
    session_start_ts: sessionStartMs,
    avg_duration_sec: avgDur,
    sharpe_like: +sharpe.toFixed(3),
    max_drawdown: maxDrawdown(equity_curve),
    streaks: { current: { type: curType, n: curN }, max_win: maxW, max_loss: maxL },
  };

  const rawDec = Array.isArray(status?.last_decisions) ? status.last_decisions : [];
  const recent = rawDec.slice(-200);
  const last_by_symbol = {};
  for (const d of rawDec) { const s = d?.symbol; if (s) last_by_symbol[s] = d; }
  const rejection_count = {};
  for (const d of recent) {
    if (d?.allowed) continue;
    const k = String(d?.reason || "UNKNOWN").split(":")[0].slice(0, 60);
    rejection_count[k] = (rejection_count[k] || 0) + 1;
  }

  const ai_quality = computeAiQualityBySymbol(aiDecisions, allClosed);
  const pattern_memory = await fetchPatternMemory(allClosed);

  return Response.json({
    ts: Date.now(),
    status: {
      balance: status?.balance ?? null,
      floating_pnl: status?.floating_pnl ?? null,
      ws_state: status?.ws_state ?? null,
      online: status?.online ?? null,
      today_trades: status?.today_trades ?? null,
    },
    global,
    equity_curve, daily_pnl, calendar,
    heatmap_pnl, heatmap_count,
    by_regime, by_strategy, by_hurst_zone, by_score_band, by_side, by_exit_reason, by_symbol,
    rolling_20, rolling_50,
    scatter, correlations,
    histograms: { score: scoreHist, hurst: hurstHist },
    ai_quality,
    pattern_memory,
    decisions: { recent, last_by_symbol, rejection_count },
    open_contracts: openContracts,
    closed_contracts: allClosed,
  }, { headers: { "Cache-Control": "no-store, max-age=0" } });
}
