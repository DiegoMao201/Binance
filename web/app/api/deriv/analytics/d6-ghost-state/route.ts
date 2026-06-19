import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const D6_STATE_FILE = path.join(BOT_LOGS, 'd6_ghost_state.json');

const SYMBOLS = ['BOOM500', 'BOOM600', 'BOOM900', 'BOOM1000', 'CRASH500', 'CRASH600', 'CRASH900', 'CRASH1000'];

async function readJson(file: string, fallback: unknown) {
  try {
    return JSON.parse(await fs.readFile(file, 'utf8'));
  } catch {
    return fallback;
  }
}

export async function GET() {
  const raw = await readJson(D6_STATE_FILE, {});
  const now = Date.now() / 1000;

  const states: Record<string, object> = {};
  for (const sym of SYMBOLS) {
    const entry = (raw as Record<string, unknown>)[sym];
    if (entry && typeof entry === 'object') {
      const e = entry as Record<string, unknown>;
      // Auto-expire EXECUTED/CANCELLED states older than 90s
      const updatedAt = Number(e.updated_at || 0);
      const state = String(e.state || 'WAITING');
      if ((state === 'EXECUTED' || state === 'CANCELLED' || state === 'FAILED') && now - updatedAt > 90) {
        states[sym] = { symbol: sym, state: 'WAITING', updated_at: now, ghost_data: {} };
      } else {
        states[sym] = e;
      }
    } else {
      states[sym] = { symbol: sym, state: 'WAITING', updated_at: now, ghost_data: {} };
    }
  }

  return NextResponse.json({ updated_at: now, states });
}
