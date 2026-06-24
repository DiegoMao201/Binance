import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const SLOPE_LOG = path.join(BOT_LOGS, 'slope_history.jsonl');

// Mirrors DERIV_D10_* env var defaults from slope_tracker.py
const BOOM500_SLOPE_MIN = parseFloat(process.env.DERIV_D10_BOOM500_SLOPE_MIN || '0.24');
const CRASH500_SLOPE_MAX = parseFloat(process.env.DERIV_D10_CRASH500_SLOPE_MAX || '-0.90');
const STABILIZE_SEC = parseInt(process.env.DERIV_D10_SPIKE_STABILIZE_SEC || '180', 10);

interface SlopeEntry {
  ts: number;
  symbol: string;
  price?: number;
  spike_ts: number;
  estabilizado: boolean;
  n_prices: number;
  slope: number | null;
}

async function readLastLines(file: string, n: number): Promise<string[]> {
  try {
    const stat = await fs.stat(file);
    const size = stat.size;
    if (size === 0) return [];
    // 150 bytes/entry × 60 entries = 9 KB
    const readSize = Math.min(size, 9216);
    const fd = await fs.open(file, 'r');
    const buf = Buffer.alloc(readSize);
    await fd.read(buf, 0, readSize, size - readSize);
    await fd.close();
    return buf.toString('utf8').split('\n').filter(l => l.trim()).slice(-n);
  } catch {
    return [];
  }
}

function fmtSlope(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(3)}`;
}

export async function GET() {
  const lines = await readLastLines(SLOPE_LOG, 60);
  const now = Date.now() / 1000;

  const bySymbol: Record<string, SlopeEntry> = {};
  for (const line of lines) {
    try {
      const entry = JSON.parse(line) as SlopeEntry;
      const sym = String(entry.symbol || '').toUpperCase();
      if (!bySymbol[sym] || entry.ts > bySymbol[sym].ts) bySymbol[sym] = entry;
    } catch { /* skip malformed */ }
  }

  const result: Record<string, unknown> = { updated_at: now };

  for (const sym of ['BOOM500', 'CRASH500']) {
    const e = bySymbol[sym];
    if (!e) {
      result[sym] = { symbol: sym, available: false };
      continue;
    }

    const age_s = Math.round(now - e.ts);
    const slope = e.slope;
    let passing = false;
    let block_reason = '';

    if (!e.estabilizado) {
      const elapsed = e.spike_ts > 0 ? Math.round(now - e.spike_ts) : 0;
      block_reason = `stabilizing_${elapsed}s_of_${STABILIZE_SEC}s`;
    } else if (e.n_prices < 10) {
      block_reason = `insuf_data_${e.n_prices}_pts`;
    } else if (slope == null) {
      block_reason = 'slope_calc_error';
    } else if (sym === 'BOOM500') {
      passing = slope >= BOOM500_SLOPE_MIN;
      block_reason = passing
        ? `${fmtSlope(slope)}>=${fmtSlope(BOOM500_SLOPE_MIN)}`
        : `${fmtSlope(slope)}<${fmtSlope(BOOM500_SLOPE_MIN)}`;
    } else {
      passing = slope <= CRASH500_SLOPE_MAX;
      block_reason = passing
        ? `${fmtSlope(slope)}<=${fmtSlope(CRASH500_SLOPE_MAX)}`
        : `${fmtSlope(slope)}>${fmtSlope(CRASH500_SLOPE_MAX)}`;
    }

    result[sym] = {
      symbol: sym,
      available: true,
      slope,
      estabilizado: e.estabilizado,
      spike_ts: e.spike_ts,
      n_prices: e.n_prices,
      price: e.price,
      passing,
      threshold: sym === 'BOOM500' ? BOOM500_SLOPE_MIN : CRASH500_SLOPE_MAX,
      block_reason,
      age_s,
      stabilize_sec: STABILIZE_SEC,
    };
  }

  return NextResponse.json(result);
}
