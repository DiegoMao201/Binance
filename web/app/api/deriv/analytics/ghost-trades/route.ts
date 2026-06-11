import fs from 'node:fs/promises';
import path from 'node:path';
import { NextRequest, NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.BOT_STATE_DIR || '/data/logs';
const GHOST_FILE = path.join(BOT_LOGS, 'ghost_trades.json');

// Per-gate stats structure
interface GateStats {
  total: number;
  WIN: number;
  LOSS: number;
  EXPIRED: number;
  PENDING: number;
  win_rate: number;
  net_pnl: number;
  profit_factor: number;
  avg_win_pnl: number;
  avg_loss_pnl: number;
  gross_win_pnl: number;
  gross_loss_pnl: number;
}

interface GhostRecord {
  id: string;
  ts: number;
  symbol: string;
  price: number;
  side: string;
  score_raw: number;
  gate: string;
  setup_type: string;
  grade: string | null;
  scarcity: string | null;
  imm_state: string | null;
  imm_score: number | null;
  stake_usdt?: number;
  outcome: string;
  outcome_ts: number | null;
  outcome_price: number | null;
  estimated_pnl?: number | null;
}

let _cache: { ts: number; data: GhostRecord[] } | null = null;
const CACHE_TTL = 15_000;

async function loadAll(): Promise<GhostRecord[]> {
  const now = Date.now();
  if (_cache && now - _cache.ts < CACHE_TTL) return _cache.data;
  try {
    const raw = await fs.readFile(GHOST_FILE, 'utf8');
    const data = JSON.parse(raw) as GhostRecord[];
    _cache = { ts: now, data };
    return data;
  } catch {
    return [];
  }
}

function emptyStats(): GateStats {
  return { total: 0, WIN: 0, LOSS: 0, EXPIRED: 0, PENDING: 0, win_rate: 0,
           net_pnl: 0, profit_factor: 0, avg_win_pnl: 0, avg_loss_pnl: 0,
           gross_win_pnl: 0, gross_loss_pnl: 0 };
}

function finalizeStats(s: GateStats): void {
  const resolved = s.WIN + s.LOSS;
  s.win_rate = resolved > 0 ? +(s.WIN / resolved).toFixed(3) : 0;
  s.net_pnl = +(s.gross_win_pnl - s.gross_loss_pnl).toFixed(2);
  s.profit_factor = s.gross_loss_pnl > 0
    ? +(s.gross_win_pnl / s.gross_loss_pnl).toFixed(2)
    : (s.gross_win_pnl > 0 ? 999 : 0);
  s.avg_win_pnl = s.WIN > 0 ? +(s.gross_win_pnl / s.WIN).toFixed(2) : 0;
  s.avg_loss_pnl = s.LOSS > 0 ? +(s.gross_loss_pnl / s.LOSS).toFixed(2) : 0;
  s.gross_win_pnl = +s.gross_win_pnl.toFixed(2);
  s.gross_loss_pnl = +s.gross_loss_pnl.toFixed(2);
}

function accumulatePnl(s: GateStats, r: GhostRecord): void {
  const stake = r.stake_usdt ?? 5.0;
  const pnl = r.estimated_pnl != null ? r.estimated_pnl : (
    r.outcome === 'GHOST_WIN' && r.outcome_price != null && r.price > 0
      ? (() => {
          const movePct = r.side === 'MULTUP'
            ? (r.outcome_price - r.price) / r.price
            : (r.price - r.outcome_price) / r.price;
          const slPct = 0.025;
          return Math.min(stake * Math.max(0, movePct) / slPct, stake * 3);
        })()
      : r.outcome === 'GHOST_LOSS' ? -stake : 0
  );
  if (r.outcome === 'GHOST_WIN') s.gross_win_pnl += pnl;
  else if (r.outcome === 'GHOST_LOSS') s.gross_loss_pnl += Math.abs(pnl);
}

export async function GET(request: NextRequest) {
  const hoursParam = request.nextUrl.searchParams.get('hours');
  const hours = Math.min(168, Math.max(1, parseFloat(hoursParam || '24') || 24));
  const cutoff = Date.now() / 1000 - hours * 3600;

  const all = await loadAll();
  const window = all.filter(r => r.ts >= cutoff);

  // Aggregate per gate
  const byGate: Record<string, GateStats> = {};
  // Aggregate per symbol
  const bySymbol: Record<string, GateStats> = {};

  for (const r of window) {
    const g = r.gate || 'unknown';
    const sym = r.symbol || 'unknown';
    if (!byGate[g])   byGate[g]   = emptyStats();
    if (!bySymbol[sym]) bySymbol[sym] = emptyStats();

    for (const s of [byGate[g], bySymbol[sym]]) {
      s.total++;
      if (r.outcome === 'GHOST_WIN')      s.WIN++;
      else if (r.outcome === 'GHOST_LOSS') s.LOSS++;
      else if (r.outcome === 'GHOST_EXPIRED') s.EXPIRED++;
      else s.PENDING++;
      accumulatePnl(s, r);
    }
  }
  for (const s of [...Object.values(byGate), ...Object.values(bySymbol)]) finalizeStats(s);

  // Recent resolved (last 30)
  const resolved = window
    .filter(r => r.outcome !== 'PENDING')
    .slice(-30)
    .reverse();

  // Summary totals
  const totals = emptyStats();
  totals.total = window.length;
  for (const r of window) {
    if (r.outcome === 'GHOST_WIN')      totals.WIN++;
    else if (r.outcome === 'GHOST_LOSS') totals.LOSS++;
    else if (r.outcome === 'GHOST_EXPIRED') totals.EXPIRED++;
    else totals.PENDING++;
    accumulatePnl(totals, r);
  }
  finalizeStats(totals);

  return NextResponse.json({ hours, totals, by_gate: byGate, by_symbol: bySymbol, recent: resolved });
}
