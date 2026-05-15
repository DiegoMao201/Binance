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

// ─── Per-market cadence (kept in sync with src/utils/market_profiles.py) ────
// We do NOT import from Python; we mirror the cadence_tag/priority/ttl here so
// the cohort UI can group by cadence even when the cached payload was written
// by the DB service (which doesn't yet expose cadence).
const MARKET_CADENCE = {
  "WIF/USDT":  { tag: "turbo",         label: "Meme Volátil",     ttl: 30,  prio: 1 },
  "DOGE/USDT": { tag: "turbo",         label: "Meme Volátil",     ttl: 45,  prio: 2 },
  "SOL/USDT":  { tag: "rapida",        label: "Layer-1 Rápida",   ttl: 75,  prio: 3 },
  "BNB/USDT":  { tag: "estandar",      label: "Nativo Binance",   ttl: 90,  prio: 4 },
  "ETH/USDT":  { tag: "estandar",      label: "Plata Líquida",    ttl: 120, prio: 4 },
  "BTC/USDT":  { tag: "institucional", label: "Oro Digital",      ttl: 180, prio: 5 },
  // Legacy symbols seen in pre-V3 cohort (not active any more)
  "LINK/USDT": { tag: "estandar",      label: "Legacy",           ttl: 120, prio: 9 },
};

function cadenceFor(symbol) {
  return MARKET_CADENCE[symbol] || { tag: "unknown", label: symbol, ttl: 0, prio: 9 };
}

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

// ─── Per-trade PnL extractor (used by enrichment passes) ────────────────────
function tradePnl(t) {
  if (typeof t.pnl_usdt === "number") return t.pnl_usdt;
  const entry = t.entry_price || t.fill_price || 0;
  const exit  = t.exit_price || 0;
  const amt   = t.amount || 0;
  const side  = t.side || "buy";
  const gross = side === "buy" ? (exit - entry) * amt : (entry - exit) * amt;
  const fee   = (entry * amt + exit * amt) * (FEE_RT_PCT / 2);
  return gross - fee;
}

// ─── Build by_symbol breakdown with cadence + scenario sub-buckets ──────────
function buildBySymbol(trades) {
  const map = {};
  for (const t of trades) {
    const sym = t.symbol || "UNKNOWN";
    const pnl = tradePnl(t);
    const isWin = pnl > 0;
    if (!map[sym]) {
      const cad = cadenceFor(sym);
      map[sym] = {
        symbol: sym,
        cadence_tag: cad.tag,
        cadence_label: cad.label,
        cadence_ttl_s: cad.ttl,
        cadence_priority: cad.prio,
        trades: 0, wins: 0, losses: 0, pnl: 0,
        gross_wins: 0, gross_losses: 0,
        win_rate_pct: null, profit_factor: null, ev_per_trade_usdt: null,
        by_scenario: {},
      };
    }
    const e = map[sym];
    e.trades++;
    if (isWin) { e.wins++; e.gross_wins += pnl; }
    else { e.losses++; e.gross_losses += Math.abs(pnl); }
    e.pnl = round4(e.pnl + pnl);

    const sc = t.scenario || "unknown";
    if (!e.by_scenario[sc]) e.by_scenario[sc] = { trades: 0, wins: 0, pnl: 0, win_rate_pct: null };
    e.by_scenario[sc].trades++;
    if (isWin) e.by_scenario[sc].wins++;
    e.by_scenario[sc].pnl = round4(e.by_scenario[sc].pnl + pnl);
  }

  // Finalise rates
  for (const e of Object.values(map)) {
    if (e.trades > 0) {
      e.win_rate_pct = round2((e.wins / e.trades) * 100);
      const avgWin  = e.wins > 0 ? e.gross_wins / e.wins : 0;
      const avgLoss = e.losses > 0 ? e.gross_losses / e.losses : 0;
      e.avg_win_usdt  = round4(avgWin);
      e.avg_loss_usdt = round4(avgLoss);
      e.ev_per_trade_usdt = round4((e.wins / e.trades) * avgWin - (e.losses / e.trades) * avgLoss);
      e.profit_factor = e.gross_losses > 0
        ? round4(e.gross_wins / e.gross_losses)
        : (e.gross_wins > 0 ? Infinity : null);
    }
    for (const s of Object.values(e.by_scenario)) {
      s.win_rate_pct = s.trades > 0 ? round2((s.wins / s.trades) * 100) : null;
      s.pnl = round2(s.pnl);
    }
    e.pnl = round2(e.pnl);
  }
  return map;
}

// ─── Build symbol × scenario heatmap matrix ─────────────────────────────────
function buildScenarioMatrix(bySymbol) {
  const symbols   = Object.keys(bySymbol).sort((a, b) => bySymbol[a].cadence_priority - bySymbol[b].cadence_priority);
  const scenarios = ["A", "B", "C"];
  const matrix    = symbols.map((sym) => {
    const row = { symbol: sym, cadence_tag: bySymbol[sym].cadence_tag };
    for (const sc of scenarios) {
      const cell = bySymbol[sym].by_scenario[sc];
      row[sc] = cell ? { trades: cell.trades, wins: cell.wins, pnl: cell.pnl, win_rate_pct: cell.win_rate_pct } : null;
    }
    return row;
  });
  return { symbols, scenarios, matrix };
}

// ─── Build by_cadence aggregation ───────────────────────────────────────────
function buildByCadence(bySymbol) {
  const out = {};
  for (const e of Object.values(bySymbol)) {
    const tag = e.cadence_tag;
    if (!out[tag]) out[tag] = { trades: 0, wins: 0, losses: 0, pnl: 0, gross_wins: 0, gross_losses: 0, symbols: [] };
    const o = out[tag];
    o.trades += e.trades;
    o.wins += e.wins;
    o.losses += e.losses;
    o.pnl += e.pnl;
    o.gross_wins += e.gross_wins;
    o.gross_losses += e.gross_losses;
    o.symbols.push(e.symbol);
  }
  for (const o of Object.values(out)) {
    o.win_rate_pct = o.trades > 0 ? round2((o.wins / o.trades) * 100) : null;
    o.profit_factor = o.gross_losses > 0 ? round4(o.gross_wins / o.gross_losses) : (o.gross_wins > 0 ? Infinity : null);
    o.pnl = round2(o.pnl);
    o.gross_wins = round2(o.gross_wins);
    o.gross_losses = round2(o.gross_losses);
    o.symbols.sort();
  }
  return out;
}

// ─── Refinement insights — concrete, actionable diagnostics ─────────────────
function buildInsights({ v3Metrics, bySymbol, byCadence }) {
  const insights = [];
  const WR_TARGET = 55;
  const MIN_TRADES = 4; // don't flag low-sample dimensions

  // Worst scenario by WR (with min trades guard)
  if (v3Metrics?.by_scenario) {
    const scenarios = Object.entries(v3Metrics.by_scenario).filter(([, d]) => d.trades >= MIN_TRADES);
    if (scenarios.length) {
      const worst = scenarios.reduce((a, b) => (a[1].win_rate_pct < b[1].win_rate_pct ? a : b));
      if (worst[1].win_rate_pct !== null && worst[1].win_rate_pct < 30) {
        insights.push({
          severity: "high",
          dimension: "scenario",
          key: worst[0],
          headline: `Escenario ${worst[0]} sangrando (${worst[1].win_rate_pct}% WR · ${worst[1].trades} trades · PnL ${worst[1].pnl} USDT)`,
          action: worst[0] === "C"
            ? "Endurecer Scenario C: subir AI threshold a 70% o exigir alignment 'aligned' (no 'partial')."
            : `Revisar gates de Scenario ${worst[0]} — el WR está muy por debajo del 55% objetivo.`,
        });
      }
    }
  }

  // Exit reason: dominant losing exit
  if (v3Metrics?.by_exit_reason) {
    for (const [reason, d] of Object.entries(v3Metrics.by_exit_reason)) {
      if (d.trades >= MIN_TRADES && d.win_rate_pct === 0) {
        insights.push({
          severity: "high",
          dimension: "exit_reason",
          key: reason,
          headline: `${reason}: ${d.trades} trades, 0 wins (PnL ${d.pnl} USDT)`,
          action: reason === "microstructure_bailout"
            ? "Cada bailout cierra en pérdida — endurecer entry filter o relajar el bailout cuando el spread es transitorio."
            : reason === "smart_stagnation_loss_cut"
            ? "Stagnation cut está cerrando perdedores antes de tiempo o entradas mal timed — revisar timeout/gradient."
            : `Investigar por qué ${reason} nunca produce ganancias.`,
        });
      }
    }
  }

  // Per-symbol: worst performer
  const symbols = Object.values(bySymbol || {}).filter((s) => s.trades >= MIN_TRADES);
  if (symbols.length) {
    const worstSym = symbols.reduce((a, b) => (a.win_rate_pct < b.win_rate_pct ? a : b));
    if (worstSym.win_rate_pct < 35) {
      insights.push({
        severity: "medium",
        dimension: "symbol",
        key: worstSym.symbol,
        headline: `${worstSym.symbol} bajo (${worstSym.win_rate_pct}% WR · ${worstSym.trades} trades · PnL ${worstSym.pnl} USDT · ${worstSym.cadence_tag})`,
        action: `Considerar pausar ${worstSym.symbol} temporalmente o exigir confluencia más alta para este mercado.`,
      });
    }
    const bestSym = symbols.reduce((a, b) => (a.win_rate_pct > b.win_rate_pct ? a : b));
    if (bestSym.win_rate_pct >= WR_TARGET) {
      insights.push({
        severity: "info",
        dimension: "symbol",
        key: bestSym.symbol,
        headline: `${bestSym.symbol} sobre objetivo (${bestSym.win_rate_pct}% WR · ${bestSym.trades} trades · PnL ${bestSym.pnl} USDT)`,
        action: `Mantener configuración de ${bestSym.symbol}. Considerar subir asignación o copiar parámetros a mercados similares.`,
      });
    }
  }

  // Cadence: identify which tier is bleeding
  if (byCadence) {
    for (const [tag, d] of Object.entries(byCadence)) {
      if (d.trades >= MIN_TRADES && d.win_rate_pct < 30) {
        insights.push({
          severity: "medium",
          dimension: "cadence",
          key: tag,
          headline: `Cadencia ${tag.toUpperCase()}: ${d.win_rate_pct}% WR (${d.trades} trades, PnL ${d.pnl} USDT) en ${d.symbols.join(", ")}`,
          action: tag === "turbo"
            ? "Mercados turbo necesitan filtro extra (volumen mínimo, no FOMO en rangos)."
            : "Revisar parámetros de la cadencia — no está conviertiendo entradas en wins.",
        });
      }
    }
  }

  // Profit factor warning
  if (v3Metrics?.profit_factor !== null && v3Metrics?.profit_factor < 1.0 && v3Metrics?.total_trades >= MIN_TRADES) {
    insights.push({
      severity: "high",
      dimension: "global",
      key: "profit_factor",
      headline: `Profit Factor ${v3Metrics.profit_factor} (< 1.0 = perdedor neto)`,
      action: "Para llegar a PF≥1.5: subir avg_win (TPs más amplios) o reducir avg_loss (SL más ajustado o cortar perdedores antes).",
    });
  }

  return insights.sort((a, b) => {
    const order = { high: 0, medium: 1, info: 2, low: 3 };
    return (order[a.severity] || 9) - (order[b.severity] || 9);
  });
}

export async function GET() {
  // ── Always load closed_trades.json so we can enrich with dimensions the DB
  //    cache doesn't yet expose (per-symbol, scenario × symbol, cadence,
  //    refinement insights).
  const allTrades = await readJson("closed_trades.json", []);
  const v3Trades  = allTrades.filter(isV3);
  const legacyTrades = allTrades.filter((t) => !isV3(t));

  // Enrichment passes (cheap — bounded list of trades)
  const v3BySymbol     = buildBySymbol(v3Trades);
  const allBySymbol    = buildBySymbol(allTrades);
  const v3ByCadence    = buildByCadence(v3BySymbol);
  const allByCadence   = buildByCadence(allBySymbol);
  const scenarioMatrix = buildScenarioMatrix(v3BySymbol);

  const WR_TARGET = 55.0;

  // ── Priority 1: DB-backed cache (cohort_v3_metrics.json) ─────────────────
  // Written by src/analysis/cohort_v3_service.py after every trade close.
  const dbCache = await readDbCache();
  if (dbCache) {
    const m = dbCache.metrics;
    const wrGap = m.win_rate_pct !== null ? round2(WR_TARGET - m.win_rate_pct) : null;
    const insights = buildInsights({ v3Metrics: m, bySymbol: v3BySymbol, byCadence: v3ByCadence });

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
        v3:               m,
        v3_by_symbol:     v3BySymbol,
        v3_by_cadence:    v3ByCadence,
        v3_scenario_matrix: scenarioMatrix,
        all_by_symbol:    allBySymbol,
        all_by_cadence:   allByCadence,
        insights,
      },
      { headers: { "Cache-Control": "no-store, max-age=0" } }
    );
  }

  // ── Priority 2: legacy in-process computation from closed_trades.json ────
  const v3Metrics     = computeCohortMetrics(v3Trades);
  const legacyMetrics = computeCohortMetrics(legacyTrades);
  const allMetrics    = computeCohortMetrics(allTrades);

  // Win-rate target progress
  const wrGap = v3Metrics.win_rate_pct !== null
    ? round2(WR_TARGET - v3Metrics.win_rate_pct)
    : null;

  // micro_gate vs standard path breakdown for V3
  const microGateTrades = v3Trades.filter((t) => t.ai_micro_gate_path === "micro_gate");
  const standardTrades  = v3Trades.filter((t) => t.ai_micro_gate_path !== "micro_gate");

  const insights = buildInsights({ v3Metrics, bySymbol: v3BySymbol, byCadence: v3ByCadence });

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
      v3_by_symbol:       v3BySymbol,
      v3_by_cadence:      v3ByCadence,
      v3_scenario_matrix: scenarioMatrix,
      all_by_symbol:      allBySymbol,
      all_by_cadence:     allByCadence,
      legacy: legacyMetrics,
      all_time: allMetrics,
      insights,
    },
    { headers: { "Cache-Control": "no-store, max-age=0" } }
  );
}
