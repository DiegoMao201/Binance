import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const STATE_FILE = path.join(BOT_LOGS, 'entrada_diego_state.json');

export async function GET() {
  try {
    const raw = await fs.readFile(STATE_FILE, 'utf8');
    const data = JSON.parse(raw);
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({
      enabled: false,
      error: 'state_file_not_found',
      CRASH500: { phase: 'IDLE', remaining_s: 0, current_profit: 0, reopens: 0 },
      BOOM500:  { phase: 'IDLE', remaining_s: 0, current_profit: 0, reopens: 0 },
    });
  }
}
