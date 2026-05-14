import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

// ─── V3 cohort filter ────────────────────────────────────────────────────────
// Classification is STRICT: only trades explicitly tagged with
// ai_prompt_version='v3' qualify. The timestamp-based fallback was removed
// to prevent data leakage — pre-deployment trades on 2026-05-13 (morning)
// were incorrectly captured by the midnight cutoff.

const ROOT = process.env.BOT_STATE_DIR
  ? process.env.BOT_STATE_DIR
  : path.join(process.cwd(), "..", "logs");

// ─── DB-backed cache (written by src/analysis/cohort_v3_service.py) ──────────
// When cohort_v3_metrics.json exists AND is fresh (< 5 min old), the route
// returns it directly — no JSON-file scan needed.  Falls back to the legacy
// in-process computation when the cache is absent or stale.
const CACHE_MAX_AGE_MS = 5 * 60 * 1000; // 5 minutes

async function readDbCache() {
  try {
    const cachePath = path.join(ROOT, "cohort_v3_metrics.json");
    const raw = await fs.readFile(cachePath, "utf8");
    const parsed = JSON.parse(raw);
    const generatedAt = new Date(parsed.generated_at).getTime();
    if (Date.now() - generatedAt > CACHE_MAX_AGE_MS) return null; // stale
    return parsed; // { generated_at, cohort, filter, metrics }
  } catch {
    return null; // file absent or malformed — fall through to legacy path
  }
}

async function readJson(file, fallback) {
  try {
    const raw = await fs.readFile(path.join(ROOT, file), "utf8");
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

// ─── Fee constant (RT round-trip 0.20%) ─────────────────────────────────────
const FEE_RT_PCT = 0.002;

function isV3(trade) {
  // Strict tag-only check. Null/undefined ai_prompt_version → Legacy.
  // Timestamp fallback removed: it caused morning-of-deploy trades to
  // contaminate the V3 cohort (data leakage).
  return trade.ai_prompt_version === "v3";
}

function computeCohortMetrics(trades) {
  if (!trades.length) {
    return {
      total_trades: 0,
      win_rate_pct: null,
      profit_factor: null,
      net_pnl_usdt: 0,
      gross_wins_usdt: 0,
      gross_losses_usdt: 0,
      avg_win_usdt: null,
      avg_loss_usdt: null,
      ev_per_trade_usdt: null,
      total_fees_usdt: 0,
      by_regime: {},
      by_path: {},
      by_exit_reason: {},
      by_scenario: {},
    };
  }

  let wins = 0, losses = 0;
  let grossWins = 0, grossLosses = 0;
  let totalFees = 0;
  const byRegime = {};
  const byPath = {};
  const byExitReason = {};
  const byScenario = {};

  for (const t of trades) {
    // pnl_usdt in the JSON is already net (fees deducted at close).
    // If not present, approximate from entry/exit prices.
    let pnl = typeof t.pnl_usdt === "number" ? t.pnl_usdt : null;
    if (pnl === null) {
      const entry = t.entry_price || t.fill_price || 0;
      const exit = t.exit_price || 0;
      const amount = t.amount || 0;
      const side = t.side || "buy";
      const gross = side === "buy"
        ? (exit - entry) * amount
        : (entry - exit) * amount;
      const fee = (entry * amount + exit * amount) * (FEE_RT_PCT / 2);
      pnl = gross - fee;
    }

    // Estimate fees if not stored
    const entryFee = t.entry_fee_quote || (t.entry_price || 0) * (t.amount || 0) * (FEE_RT_PCT / 2);
    const closeFee = (t.live_close?.fee_quote) || (t.exit_price || 0) * (t.amount || 0) * (FEE_RT_PCT / 2);
    totalFees += entryFee + closeFee;

    const isWin = pnl > 0;
    if (isWin) { wins++; grossWins += pnl; }
    else { losses++; grossLosses += Math.abs(pnl); }

    // Breakdown dimensions
    const regime = t.ai_regime || (isV3Regime(t) ? "LEGACY" : "LEGACY");
    const path_ = t.ai_micro_gate_path || "standard";
    const exitReason = t.exit_reason || "unknown";
    const scenario = t.scenario || "unknown";

    for (const [dim, key] of [[byRegime, regime], [byPath, path_], [byExitReason, exitReason], [byScenario, scenario]]) {
      if (!dim[key]) dim[key] = { trades: 0, wins: 0, pnl: 0 };
      dim[key].trades++;
      if (isWin) dim[key].wins++;
      dim[key].pnl = round2(dim[key].pnl + pnl);
    }
  }

  const total = wins + losses;
  const winRate = total > 0 ? (wins / total) * 100 : null;
  const profitFactor = grossLosses > 0 ? grossWins / grossLosses : grossWins > 0 ? Infinity : null;
  const netPnl = grossWins - grossLosses;
  const avgWin = wins > 0 ? grossWins / wins : null;
  const avgLoss = losses > 0 ? grossLosses / losses : null;
  const evPerTrade = (avgWin !== null && avgLoss !== null && total > 0)
    ? (wins / total) * avgWin - (losses / total) * avgLoss
    : null;

  // Add win_rate to each dimension breakdown
  for (const dim of [byRegime, byPath, byExitReason, byScenario]) {
    for (const k of Object.keys(dim)) {
      const d = dim[k];
      d.win_rate_pct = d.trades > 0 ? round2((d.wins / d.trades) * 100) : null;
    }
  }

  return {
    total_trades: total,
    wins,
    losses,
    win_rate_pct: winRate !== null ? round2(winRate) : null,
    profit_factor: profitFactor !== null ? round4(profitFactor) : null,
    net_pnl_usdt: round2(netPnl),
    gross_wins_usdt: round2(grossWins),
    gross_losses_usdt: round2(grossLosses),
    avg_win_usdt: avgWin !== null ? round4(avgWin) : null,
    avg_loss_usdt: avgLoss !== null ? round4(avgLoss) : null,
    ev_per_trade_usdt: evPerTrade !== null ? round4(evPerTrade) : null,
    total_fees_usdt: round2(totalFees),
    by_regime: byRegime,
    by_path: byPath,
    by_exit_reason: byExitReason,
    by_scenario: byScenario,
  };
}

function isV3Regime(t) {
  return t.ai_regime != null;
}

function round2(n) { return Math.round(n * 100) / 100; }
function round4(n) { return Math.round(n * 10000) / 10000; }

export async function GET() {
  // ── Priority 1: DB-backed cache (cohort_v3_metrics.json) ─────────────────
  // Written by src/analysis/cohort_v3_service.py after every trade close.
  // When fresh, this path is O(1) — a single file read with no trade scan.
  const dbCache = await readDbCache();
  if (dbCache) {
    const m = dbCache.metrics;
    const allTrades = await readJson("closed_trades.json", []);
    const v3Trades  = allTrades.filter(isV3);
    const WR_TARGET = 55.0;
    const wrGap = m.win_rate_pct !== null ? round2(WR_TARGET - m.win_rate_pct) : null;

    return NextResponse.json(
      {
        cohort:        "v3",
        source:        "db_cache",
        filter:        dbCache.filter,
        generated_at:  dbCache.generated_at,
        summary: {
          total_all_time:     allTrades.length,
          total_v3:           v3Trades.length,
          total_legacy:       allTrades.length - v3Trades.length,
          micro_gate_entries: v3Trades.filter((t) => t.ai_micro_gate_path === "micro_gate").length,
          standard_entries:   v3Trades.filter((t) => t.ai_micro_gate_path !== "micro_gate").length,
        },
        target: {
          win_rate_target_pct:  WR_TARGET,
          current_win_rate_pct: m.win_rate_pct,
          gap_to_target_pct:    wrGap,
          on_track:             wrGap !== null ? wrGap <= 0 : null,
        },
        v3: m,
      },
      { headers: { "Cache-Control": "no-store, max-age=0" } }
    );
  }

  // ── Priority 2: legacy in-process computation from closed_trades.json ────
  // Used while the DB is not yet live (no DATABASE_URL configured) or during
  // the first minutes after deployment before the service writes the cache.
  const allTrades = await readJson("closed_trades.json", []);

  // Strict tag-based split — isV3() checks ai_prompt_version='v3' exclusively.
  const v3Trades = allTrades.filter(isV3);
  const legacyTrades = allTrades.filter((t) => !isV3(t));

  const v3Metrics = computeCohortMetrics(v3Trades);
  const legacyMetrics = computeCohortMetrics(legacyTrades);
  const allMetrics = computeCohortMetrics(allTrades);

  // Win-rate target progress
  const WR_TARGET = 55.0;
  const wrGap = v3Metrics.win_rate_pct !== null
    ? round2(WR_TARGET - v3Metrics.win_rate_pct)
    : null;

  // micro_gate vs standard path breakdown for V3
  const microGateTrades = v3Trades.filter((t) => t.ai_micro_gate_path === "micro_gate");
  const standardTrades = v3Trades.filter((t) => t.ai_micro_gate_path !== "micro_gate");

  return NextResponse.json(
    {
      cohort: "v3",
      source: "json_file",
      filter: "tag:ai_prompt_version=v3 (strict, no timestamp fallback)",
      generated_at: new Date().toISOString(),
      summary: {
        total_all_time: allTrades.length,
        total_v3: v3Trades.length,
        total_legacy: legacyTrades.length,
        micro_gate_entries: microGateTrades.length,
        standard_entries: standardTrades.length,
      },
      target: {
        win_rate_target_pct: WR_TARGET,
        current_win_rate_pct: v3Metrics.win_rate_pct,
        gap_to_target_pct: wrGap,
        on_track: wrGap !== null ? wrGap <= 0 : null,
      },
      v3: v3Metrics,
      legacy: legacyMetrics,
      all_time: allMetrics,
    },
    { headers: { "Cache-Control": "no-store, max-age=0" } }
  );
}
