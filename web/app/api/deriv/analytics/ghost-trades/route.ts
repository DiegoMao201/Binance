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
  outcome: string;
  outcome_ts: number | null;
  outcome_price: number | null;
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

export async function GET(request: NextRequest) {
  const hoursParam = request.nextUrl.searchParams.get('hours');
  const hours = Math.min(168, Math.max(1, parseFloat(hoursParam || '24') || 24));
  const cutoff = Date.now() / 1000 - hours * 3600;

  const all = await loadAll();
  const window = all.filter(r => r.ts >= cutoff);

  // Aggregate per gate
  const byGate: Record<string, GateStats> = {};
  for (const r of window) {
    const g = r.gate || 'unknown';
    if (!byGate[g]) byGate[g] = { total: 0, WIN: 0, LOSS: 0, EXPIRED: 0, PENDING: 0, win_rate: 0 };
    byGate[g].total++;
    if (r.outcome === 'GHOST_WIN') byGate[g].WIN++;
    else if (r.outcome === 'GHOST_LOSS') byGate[g].LOSS++;
    else if (r.outcome === 'GHOST_EXPIRED') byGate[g].EXPIRED++;
    else byGate[g].PENDING++;
  }
  for (const g of Object.keys(byGate)) {
    const s = byGate[g];
    const resolved = s.WIN + s.LOSS;
    s.win_rate = resolved > 0 ? +(s.WIN / resolved).toFixed(3) : 0;
  }

  // Recent resolved (last 30)
  const resolved = window
    .filter(r => r.outcome !== 'PENDING')
    .slice(-30)
    .reverse();

  // Summary totals
  const totals = { total: window.length, WIN: 0, LOSS: 0, EXPIRED: 0, PENDING: 0, win_rate: 0 };
  for (const r of window) {
    if (r.outcome === 'GHOST_WIN') totals.WIN++;
    else if (r.outcome === 'GHOST_LOSS') totals.LOSS++;
    else if (r.outcome === 'GHOST_EXPIRED') totals.EXPIRED++;
    else totals.PENDING++;
  }
  const resolvedTotal = totals.WIN + totals.LOSS;
  totals.win_rate = resolvedTotal > 0 ? +(totals.WIN / resolvedTotal).toFixed(3) : 0;

  return NextResponse.json({ hours, totals, by_gate: byGate, recent: resolved });
}
