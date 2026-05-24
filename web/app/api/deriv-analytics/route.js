import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;
const TICK_RECENT_WINDOW_SEC = Math.max(900, Number(process.env.DERIV_TICK_RECENT_WINDOW_SEC || 7200));
const TICK_BASELINE_WINDOW_SEC = Math.max(TICK_RECENT_WINDOW_SEC + 1800, Number(process.env.DERIV_TICK_BASELINE_WINDOW_SEC || 21600));

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
const safeTsMs = v => {
  if (v == null) return null;
  if (typeof v === "number") return v > 1e12 ? v : v * 1000;
  const n = Number(v);
  if (Number.isFinite(n)) return n > 1e12 ? n : n * 1000;
  const p = Date.parse(String(v));
  return Number.isFinite(p) ? p : null;
};
const safeNum = v => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
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

function intervalsFromCtx(points) {
  if (!Array.isArray(points) || points.length < 2) return [];
  const sorted = points.slice().sort((a, b) => a.ts - b.ts);
  const out = [];
  let prev = sorted[0]?.ticks;
  for (let i = 1; i < sorted.length; i++) {
    const cur = sorted[i]?.ticks;
    if (!Number.isFinite(prev) || !Number.isFinite(cur)) {
      prev = cur;
      continue;
    }
    if (cur < prev && prev > 0) out.push(prev);
    prev = cur;
  }
  return out;
}

function quantile(values, q) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const vals = values.slice().sort((a, b) => a - b);
  const pos = (vals.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.min(lo + 1, vals.length - 1);
  if (lo === hi) return vals[lo];
  const frac = pos - lo;
  return vals[lo] * (1 - frac) + vals[hi] * frac;
}

function buildSymbolTickTelemetry(status, marketCtxRows, nowMs = Date.now()) {
  const out = {};

  const fromStatus = status?.symbol_tick_context;
  if (fromStatus && typeof fromStatus === "object") {
    for (const [symRaw, row] of Object.entries(fromStatus)) {
      const sym = String(symRaw || "").toUpperCase();
      if (!sym) continue;
      const ticksSince = safeNum(row?.ticks_since_last_spike);
      out[sym] = {
        symbol: sym,
        ticks_since_last_spike_now: ticksSince != null ? Math.max(0, Math.round(ticksSince)) : null,
        source: "status",
      };
    }
  }

  const grouped = {};
  for (const row of Array.isArray(marketCtxRows) ? marketCtxRows : []) {
    const sym = String(row?.symbol || "").toUpperCase();
    const ts = safeTsMs(row?.ts ?? row?.timestamp ?? row?.iso);
    const ticks = safeNum(row?.ticks_since_last_spike);
    if (!sym || ts == null || ticks == null || ticks < 0) continue;
    (grouped[sym] ||= []).push({ ts, ticks });
  }

  const recentMin = nowMs - TICK_RECENT_WINDOW_SEC * 1000;
  const baselineMin = nowMs - TICK_BASELINE_WINDOW_SEC * 1000;

  for (const [sym, points] of Object.entries(grouped)) {
    const baselinePoints = points.filter(p => p.ts >= baselineMin);
    const recentPoints = baselinePoints.filter(p => p.ts >= recentMin);
    const intervalsBaseline = intervalsFromCtx(baselinePoints);
    const intervalsRecent = intervalsFromCtx(recentPoints);
    const p50Baseline = quantile(intervalsBaseline, 0.5);
    const p50Recent = quantile(intervalsRecent, 0.5);
    const ratio =
      Number.isFinite(p50Recent) && Number.isFinite(p50Baseline) && p50Baseline > 0
        ? p50Recent / p50Baseline
        : null;
    const latest = points.reduce((acc, cur) => (!acc || cur.ts > acc.ts ? cur : acc), null);

    out[sym] = {
      symbol: sym,
      ticks_since_last_spike_now: latest ? Math.max(0, Math.round(latest.ticks)) : out[sym]?.ticks_since_last_spike_now ?? null,
      tick_interval_recent_p50: Number.isFinite(p50Recent) ? +p50Recent.toFixed(2) : null,
      tick_interval_baseline_p50: Number.isFinite(p50Baseline) ? +p50Baseline.toFixed(2) : null,
      tick_acceleration_ratio_2h_vs_6h: Number.isFinite(ratio) ? +ratio.toFixed(4) : null,
      tick_intervals_recent_count: intervalsRecent.length,
      tick_intervals_baseline_count: intervalsBaseline.length,
      source: "market_context",
    };
  }

  return out;
}

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

export async function GET() {
  const [status, open, closed, sessionFile, marketCtx] = await Promise.all([
    readJson("deriv_status.json", {}),
    readJson("deriv_open_contracts.json", []),
    readJson("deriv_closed_contracts.json", []),
    readJson("deriv_session.json", null, LOGS),
    readJson("deriv_market_context.json", [], DERIV_LOGS),
  ]);
  const allClosed = (Array.isArray(closed) ? closed : []).slice().sort((a, b) => tsOf(a) - tsOf(b));
  // Session filter: only for global KPIs (SESSION, WIN%, PF, TRADES). Reports unaffected.
  const sessionStartMs = sessionFile?.session_start_ts ?? null;
  const sessionClosed = sessionStartMs
    ? allClosed.filter(c => tsOf(c) >= sessionStartMs)
    : allClosed;
  const tickTelemetryBySymbol = buildSymbolTickTelemetry(status, marketCtx);
  const dynamicCfgBySymbol = status?.dynamic_config?.configs || {};
  const openContracts = (Array.isArray(open) ? open : []).map(c => {
    const sym = String(symOf(c) || "").toUpperCase();
    const tickCtx = tickTelemetryBySymbol[sym] || {};
    const dyn = dynamicCfgBySymbol[sym] || {};
    const ticksNow = safeNum(tickCtx?.ticks_since_last_spike_now);
    const targetTicks = safeNum(dyn?.spike_pre_filter_target);
    const ticksToTarget =
      ticksNow != null && targetTicks != null
        ? Math.max(0, Math.round(targetTicks - ticksNow))
        : null;
    return {
      ...c,
      ticks_since_last_spike_now: ticksNow != null ? Math.round(ticksNow) : null,
      tick_interval_recent_p50: safeNum(tickCtx?.tick_interval_recent_p50),
      tick_interval_baseline_p50: safeNum(tickCtx?.tick_interval_baseline_p50),
      tick_acceleration_ratio_2h_vs_6h: safeNum(tickCtx?.tick_acceleration_ratio_2h_vs_6h),
      spike_pre_filter_target_live: targetTicks != null ? Math.round(targetTicks) : null,
      ticks_to_prefilter_target: ticksToTarget,
      multispike_buffer_ticks_live: safeNum(dyn?.multispike_buffer_ticks),
      multispike_retention_pct_live: safeNum(dyn?.multispike_retention_pct),
      multispike_mode_live: dyn?.market_regime || null,
    };
  });

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
    decisions: { recent, last_by_symbol, rejection_count },
    symbol_tick_telemetry: tickTelemetryBySymbol,
    open_contracts: openContracts,
    closed_contracts: allClosed,
  }, { headers: { "Cache-Control": "no-store, max-age=0" } });
}
