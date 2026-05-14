import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

// ─── V3 cohort cutoff ────────────────────────────────────────────────────────
// commit e08e40e pushed 2026-05-13. Any trade opened AFTER this timestamp
// OR carrying ai_prompt_version="v3" tag belongs to the V3 cohort.
const V3_CUTOFF_ISO = "2026-05-13T00:00:00.000Z";
const V3_CUTOFF_MS = Date.parse(V3_CUTOFF_ISO);

const ROOT = process.env.BOT_STATE_DIR
  ? process.env.BOT_STATE_DIR
  : path.join(process.cwd(), "..", "logs");

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
  // Primary: explicit tag injected from commit a66075a+
  if (trade.ai_prompt_version === "v3") return true;
  // Fallback: timestamp-based cutoff
  const ts = trade.opened_at || trade.closed_at;
  if (!ts) return false;
  return Date.parse(ts) >= V3_CUTOFF_MS;
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

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  // Allow overriding cutoff via query param (ISO string)
  const cutoffParam = searchParams.get("cutoff");
  const cutoffMs = cutoffParam ? Date.parse(cutoffParam) : V3_CUTOFF_MS;

  const allTrades = await readJson("closed_trades.json", []);

  const v3Trades = allTrades.filter((t) => {
    if (t.ai_prompt_version === "v3") return true;
    const ts = t.opened_at || t.closed_at;
    return ts ? Date.parse(ts) >= cutoffMs : false;
  });

  const legacyTrades = allTrades.filter((t) => !v3Trades.includes(t));

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
      cutoff_iso: new Date(cutoffMs).toISOString(),
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
