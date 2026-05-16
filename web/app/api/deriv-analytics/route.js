import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const ROOT = path.join(process.cwd(), "..");
const LOGS = process.env.BOT_STATE_DIR || path.join(ROOT, "logs");
const DERIV_LOGS = process.env.DERIV_STATE_DIR || LOGS;

async function readJson(fileName, fallback, dir = DERIV_LOGS) {
  try {
    const content = await fs.readFile(path.join(dir, fileName), "utf8");
    return JSON.parse(content);
  } catch {
    return fallback;
  }
}

export async function GET() {
  const [status, open, closed] = await Promise.all([
    readJson("deriv_status.json", {}),
    readJson("deriv_open_contracts.json", []),
    readJson("deriv_closed_contracts.json", []),
  ]);

  // ── Compute analytics server-side ────────────────────────────────────────
  const allClosed = Array.isArray(closed) ? closed : [];

  // Session PnL (all closed)
  const sessionPnl = allClosed.reduce(
    (s, c) => s + Number(c.realized_pnl_usdt ?? c.pnl ?? 0),
    0
  );

  // Win rate
  const wins = allClosed.filter(
    (c) => Number(c.realized_pnl_usdt ?? c.pnl ?? 0) > 0
  ).length;
  const winRate = allClosed.length > 0 ? wins / allClosed.length : 0;

  // Per-symbol stats
  const bySymbol = {};
  for (const c of allClosed) {
    const sym = c.symbol || c.underlying || "unknown";
    if (!bySymbol[sym]) {
      bySymbol[sym] = {
        symbol: sym,
        trades: 0,
        wins: 0,
        losses: 0,
        pnl: 0,
        best: null,
        worst: null,
        avg_hold_sec: 0,
        total_hold_sec: 0,
        by_side: { MULTUP: { trades: 0, wins: 0, pnl: 0 }, MULTDOWN: { trades: 0, wins: 0, pnl: 0 } },
        by_exit: {},
      };
    }
    const s = bySymbol[sym];
    const p = Number(c.realized_pnl_usdt ?? c.pnl ?? 0);
    const side = c.side || "unknown";
    const exit = c.exit_reason || "unknown";
    s.trades++;
    s.pnl = Math.round((s.pnl + p) * 1e6) / 1e6;
    if (p > 0) { s.wins++; }
    else if (p < 0) { s.losses++; }
    if (s.best === null || p > s.best) s.best = p;
    if (s.worst === null || p < s.worst) s.worst = p;
    if (c.opened_at_ts && c.closed_at_ts) {
      s.total_hold_sec += (c.closed_at_ts - c.opened_at_ts);
    }
    if (side === "MULTUP" || side === "MULTDOWN") {
      s.by_side[side].trades++;
      s.by_side[side].pnl = Math.round((s.by_side[side].pnl + p) * 1e6) / 1e6;
      if (p > 0) s.by_side[side].wins++;
    }
    s.by_exit[exit] = (s.by_exit[exit] || 0) + 1;
  }
  for (const s of Object.values(bySymbol)) {
    s.win_rate = s.trades > 0 ? s.wins / s.trades : 0;
    s.avg_pnl = s.trades > 0 ? s.pnl / s.trades : 0;
    s.avg_hold_sec = s.trades > 0 ? s.total_hold_sec / s.trades : 0;
  }

  // Score distribution (from decisions log)
  const decisions = Array.isArray(status.last_decisions) ? status.last_decisions : [];
  const scoreHist = { "0-2": 0, "2-4": 0, "4-6": 0, "6-8": 0, "8-10": 0 };
  for (const d of decisions) {
    const sc = Number(d.score || 0);
    if (sc < 2) scoreHist["0-2"]++;
    else if (sc < 4) scoreHist["2-4"]++;
    else if (sc < 6) scoreHist["4-6"]++;
    else if (sc < 8) scoreHist["6-8"]++;
    else scoreHist["8-10"]++;
  }

  // Exit reason breakdown
  const exitReasons = {};
  for (const c of allClosed) {
    const r = c.exit_reason || "unknown";
    if (!exitReasons[r]) exitReasons[r] = { count: 0, pnl: 0 };
    exitReasons[r].count++;
    exitReasons[r].pnl = Math.round((exitReasons[r].pnl + Number(c.realized_pnl_usdt ?? c.pnl ?? 0)) * 1e6) / 1e6;
  }

  // Multiplier distribution
  const multiplierDist = {};
  for (const c of allClosed) {
    const m = String(c.multiplier || "?");
    multiplierDist[m] = (multiplierDist[m] || 0) + 1;
  }

  // Streak analysis
  let currentStreak = 0;
  let bestStreak = 0;
  let worstStreak = 0;
  let tmpStreak = 0;
  let tmpIsWin = null;
  for (const c of allClosed) {
    const won = Number(c.realized_pnl_usdt ?? c.pnl ?? 0) > 0;
    if (tmpIsWin === null) { tmpIsWin = won; tmpStreak = 1; }
    else if (won === tmpIsWin) { tmpStreak++; }
    else { tmpIsWin = won; tmpStreak = 1; }
    if (won && tmpStreak > bestStreak) bestStreak = tmpStreak;
    if (!won && tmpStreak > worstStreak) worstStreak = tmpStreak;
  }
  if (allClosed.length > 0) {
    const last = Number(allClosed[allClosed.length - 1].realized_pnl_usdt ?? allClosed[allClosed.length - 1].pnl ?? 0);
    currentStreak = tmpIsWin === (last > 0) ? tmpStreak : 0;
  }

  return NextResponse.json({
    status,
    open_contracts: Array.isArray(open) ? open : [],
    closed_contracts: allClosed,
    analytics: {
      session_pnl: Math.round(sessionPnl * 1e4) / 1e4,
      win_rate: winRate,
      total_trades: allClosed.length,
      total_wins: wins,
      total_losses: allClosed.length - wins,
      by_symbol: bySymbol,
      score_distribution: scoreHist,
      exit_reasons: exitReasons,
      multiplier_dist: multiplierDist,
      streaks: { current: currentStreak, best_win: bestStreak, worst_loss: worstStreak },
      equity_history: Array.isArray(status.equity_history) ? status.equity_history : [],
    },
    server_time: new Date().toISOString(),
  }, {
    headers: { "Cache-Control": "no-store, max-age=0" },
  });
}
