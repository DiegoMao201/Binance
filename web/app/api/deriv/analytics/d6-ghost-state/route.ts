import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const D6_STATE_FILE = path.join(BOT_LOGS, 'd6_ghost_state.json');

const SYMBOLS = ['BOOM500', 'BOOM1000', 'CRASH500', 'CRASH1000'];

// PENDING sin actualización >90s → EXPIRED_GHOST; EXPIRED_GHOST >120s → WAITING
const PENDING_TTL_S = 90;
const EXPIRED_GHOST_TTL_S = 120;

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
      const updatedAt = Number(e.updated_at || 0);
      const state = String(e.state || 'WAITING');
      const age = now - updatedAt;
      if ((state === 'EXECUTED' || state === 'CANCELLED' || state === 'FAILED') && age > 90) {
        // Trade terminal — auto-clear al WAITING
        states[sym] = { symbol: sym, state: 'WAITING', updated_at: now, ghost_data: {} };
      } else if (state === 'PENDING' && age > PENDING_TTL_S) {
        // PENDING fantasma — bot no actualizó en >90s, bug visible
        states[sym] = { ...e, state: 'EXPIRED_GHOST', reason: `pending_stale_${Math.round(age)}s` };
      } else if (state === 'EXPIRED_GHOST' && age > EXPIRED_GHOST_TTL_S) {
        // EXPIRED_GHOST ya mostrado suficiente tiempo — volver a WAITING
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
