import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const SLOPE_LOG = path.join(BOT_LOGS, 'slope_history.jsonl');
const STABILIZE_SEC = parseInt(process.env.DERIV_D10_SPIKE_STABILIZE_SEC || '180', 10);

// Camino 1 — Slope Level (%/min)
const C1_BOOM_MIN  = parseFloat(process.env.DERIV_D10_BOOM500_SLOPE_MIN_PCT  || '0.005');
const C1_CRASH_MAX = parseFloat(process.env.DERIV_D10_CRASH500_SLOPE_MAX_PCT || '-0.005');
const C1_PENDING   = parseInt(process.env.DERIV_D10_PENDING_CAMINO1_SEC || '120', 10);

// Camino 2 — Tendencia extrema + estable (|cambio| PEQUEÑO = no revierte)
const C2_ENABLED    = (process.env.DERIV_D10_PN5_ENABLED ?? 'true').toLowerCase() !== 'false';
const C2_BOOM_MIN   = parseFloat(process.env.DERIV_D10_PN5_BOOM500_SLOPE_MIN_PCT  || '0.018');
const C2_CRASH_MAX  = parseFloat(process.env.DERIV_D10_PN5_CRASH500_SLOPE_MAX_PCT || '-0.022');
const C2_CAMBIO_MAX = parseFloat(process.env.DERIV_D10_PN5_CAMBIO_MAX_PCT || '0.010'); // umbral MÁXIMO de cambio (estable)
const C2_STABILIZE  = parseInt(process.env.DERIV_D10_PN5_STABILIZE_SEC || '60', 10);   // 60s propio (no 180s global)
const C2_PENDING    = parseInt(process.env.DERIV_D10_PENDING_CAMINO2_SEC || '5', 10);

// Camino 3 — Breakout (slope opuesto + giro)
const C3_ENABLED    = (process.env.DERIV_D10_BREAKOUT_ENABLED ?? 'true').toLowerCase() !== 'false';
const C3_BOOM_MAX   = parseFloat(process.env.DERIV_D10_BREAKOUT_BOOM500_SLOPE_MAX_PCT   || '-0.005');
const C3_CRASH_MIN  = parseFloat(process.env.DERIV_D10_BREAKOUT_CRASH500_SLOPE_MIN_PCT  || '0.005');
const C3_CAMBIO_MIN = parseFloat(process.env.DERIV_D10_BREAKOUT_CAMBIO_MIN_PCT || '0.015');
const C3_PENDING    = parseInt(process.env.DERIV_D10_PENDING_CAMINO3_SEC || '120', 10);

interface SlopeEntry {
  ts: number;
  symbol: string;
  price?: number;
  spike_ts: number;
  estabilizado: boolean;
  n_prices: number;
  slope_pct: number | null;
  cambio_pct?: number | null;
}

async function readLastLines(file: string, n: number): Promise<string[]> {
  try {
    const stat = await fs.stat(file);
    const size = stat.size;
    if (size === 0) return [];
    const readSize = Math.min(size, 12288);
    const fd = await fs.open(file, 'r');
    const buf = Buffer.alloc(readSize);
    await fd.read(buf, 0, readSize, size - readSize);
    await fd.close();
    return buf.toString('utf8').split('\n').filter(l => l.trim()).slice(-n);
  } catch {
    return [];
  }
}

interface CaminoResult { camino: string; pending_sec: number }

function detectCamino(
  sym: string,
  slope_pct: number,
  cambio_pct: number | null | undefined,
): CaminoResult | null {
  const isBoom = sym === 'BOOM500';
  const hasCambio = cambio_pct != null;
  const abs_cambio = hasCambio ? Math.abs(cambio_pct!) : 0;

  if (C2_ENABLED && hasCambio) {
    // C2: slope extremo en dirección correcta + cambio PEQUEÑO (tendencia estable)
    if (isBoom  && slope_pct >= C2_BOOM_MIN  && abs_cambio <= C2_CAMBIO_MAX)
      return { camino: 'camino2_pn5', pending_sec: C2_PENDING };
    if (!isBoom && slope_pct <= C2_CRASH_MAX && abs_cambio <= C2_CAMBIO_MAX)
      return { camino: 'camino2_pn5', pending_sec: C2_PENDING };
  }

  if (C3_ENABLED && hasCambio) {
    if (isBoom  && slope_pct <= C3_BOOM_MAX  && cambio_pct! >= C3_CAMBIO_MIN)
      return { camino: 'camino3_breakout', pending_sec: C3_PENDING };
    if (!isBoom && slope_pct >= C3_CRASH_MIN && cambio_pct! <= -C3_CAMBIO_MIN)
      return { camino: 'camino3_breakout', pending_sec: C3_PENDING };
  }

  if (isBoom  && slope_pct >= C1_BOOM_MIN)  return { camino: 'camino1_level', pending_sec: C1_PENDING };
  if (!isBoom && slope_pct <= C1_CRASH_MAX) return { camino: 'camino1_level', pending_sec: C1_PENDING };

  return null;
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
    } catch { /* skip */ }
  }

  const result: Record<string, unknown> = { updated_at: now };

  for (const sym of ['BOOM500', 'CRASH500']) {
    const e = bySymbol[sym];
    if (!e) {
      result[sym] = { symbol: sym, available: false };
      continue;
    }

    const age_s = Math.round(now - e.ts);
    const slope_pct = e.slope_pct;
    const cambio_pct = e.cambio_pct ?? null;
    let passing = false;
    let active_camino: string | null = null;
    let pending_sec: number | null = null;
    let block_reason = '';

    if (!e.estabilizado) {
      const elapsed = e.spike_ts > 0 ? Math.round(now - e.spike_ts) : 0;
      block_reason = `stabilizing_${elapsed}s_of_${STABILIZE_SEC}s`;
    } else if (e.n_prices < 10) {
      block_reason = `insuf_data_${e.n_prices}_pts`;
    } else if (slope_pct == null) {
      block_reason = 'slope_calc_error';
    } else {
      const res = detectCamino(sym, slope_pct, cambio_pct);
      if (res) {
        passing = true;
        active_camino = res.camino;
        pending_sec = res.pending_sec;
        block_reason = `${active_camino} slope=${slope_pct >= 0 ? '+' : ''}${slope_pct.toFixed(5)}%`;
      } else {
        const isBoom = sym === 'BOOM500';
        block_reason = `bloqueado slope=${slope_pct >= 0 ? '+' : ''}${slope_pct.toFixed(5)}% umbral_c1=${isBoom ? '>=' : '<='}${isBoom ? C1_BOOM_MIN : C1_CRASH_MAX}%`;
      }
    }

    result[sym] = {
      symbol: sym,
      available: true,
      slope_pct,
      cambio_pct,
      estabilizado: e.estabilizado,
      spike_ts: e.spike_ts,
      n_prices: e.n_prices,
      price: e.price,
      passing,
      active_camino,
      pending_sec,
      block_reason,
      age_s,
      stabilize_sec: STABILIZE_SEC,
      thresholds: {
        c1: { boom_min: C1_BOOM_MIN, crash_max: C1_CRASH_MAX, pending: C1_PENDING },
        c2: { enabled: C2_ENABLED, boom_min: C2_BOOM_MIN, crash_max: C2_CRASH_MAX, cambio_max: C2_CAMBIO_MAX, stabilize: C2_STABILIZE, pending: C2_PENDING },
        c3: { enabled: C3_ENABLED, boom_max: C3_BOOM_MAX, crash_min: C3_CRASH_MIN, cambio_min: C3_CAMBIO_MIN, pending: C3_PENDING },
      },
    };
  }

  return NextResponse.json(result);
}
