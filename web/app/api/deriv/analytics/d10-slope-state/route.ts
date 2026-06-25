import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const BOT_LOGS = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const PANEL_FILE  = path.join(BOT_LOGS, 'd10_panel_state.json');
const SLOPE_LOG   = path.join(BOT_LOGS, 'slope_history.jsonl');

// Thresholds from env (used as fallback when computing via JSONL)
const C1_BOOM_MAX    = parseFloat(process.env.DERIV_D10_BOOM500_SLOPE_MAX_PCT    || '-0.005');
const C1_CRASH_MIN   = parseFloat(process.env.DERIV_D10_CRASH500_SLOPE_MIN_PCT   || '0.005');
const C1_BOOM1000_MAX   = parseFloat(process.env.DERIV_D10_BOOM1000_SLOPE_MAX_PCT  || '-0.005');
const C1_CRASH1000_MIN  = parseFloat(process.env.DERIV_D10_CRASH1000_SLOPE_MIN_PCT || '0.005');
const C1_CAMBIO_MIN  = parseFloat(process.env.DERIV_D10_C1_CAMBIO_MIN_PCT        || '0.0015');
const C1_PENDING     = parseInt(process.env.DERIV_D10_PENDING_CAMINO1_SEC         || '120', 10);
const C1_PENDING_1000 = parseInt(process.env.DERIV_D10_PENDING_CAMINO1_1000_SEC  || String(C1_PENDING), 10);

const C2_ENABLED    = (process.env.DERIV_D10_PN5_ENABLED ?? 'true').toLowerCase() !== 'false';
const C2_BOOM_MAX   = parseFloat(process.env.DERIV_D10_PN5_BOOM500_SLOPE_MAX_PCT  || '-0.018');
const C2_CRASH_MIN  = parseFloat(process.env.DERIV_D10_PN5_CRASH500_SLOPE_MIN_PCT || '0.022');
const C2_CAMBIO_MAX = parseFloat(process.env.DERIV_D10_PN5_CAMBIO_MAX_PCT         || '0.010');
const C2_PENDING    = parseInt(process.env.DERIV_D10_PENDING_CAMINO2_SEC           || '5', 10);

const C3_ENABLED    = (process.env.DERIV_D10_BREAKOUT_ENABLED ?? 'true').toLowerCase() !== 'false';
const C3_BOOM_MAX   = parseFloat(process.env.DERIV_D10_BREAKOUT_BOOM500_SLOPE_MAX_PCT   || '-0.005');
const C3_CRASH_MIN  = parseFloat(process.env.DERIV_D10_BREAKOUT_CRASH500_SLOPE_MIN_PCT  || '0.005');
const C3_CAMBIO_MIN = parseFloat(process.env.DERIV_D10_BREAKOUT_CAMBIO_MIN_PCT          || '0.015');
const C3_PENDING    = parseInt(process.env.DERIV_D10_PENDING_CAMINO3_SEC                || '120', 10);

const GRACE_500_SEC  = parseInt(process.env.DERIV_D10_DELTA_GRACE_SEC       || '240', 10);
const GRACE_1000_SEC = parseInt(process.env.DERIV_D10_DELTA_GRACE_1000_SEC  || '480', 10);

const ALL_SYMBOLS = ['BOOM500', 'CRASH500', 'BOOM1000', 'CRASH1000'];

interface PanelEntry {
  symbol: string;
  available: boolean;
  last_spike_ts?: number;
  spike_count?: number;
  grace_until?: number;
  grace_countdown_sec?: number;
  delta_valid?: boolean;
  n_prices?: number;
  slope_pct?: number | null;
  cambio_pct?: number | null;
  c1_ok?: boolean;
  c2_ok?: boolean;
  c3_ok?: boolean;
  passing?: boolean;
  active_camino?: string | null;
  pending_sec?: number | null;
  block_reason?: string;
}

interface SlopeEntry {
  ts: number;
  symbol: string;
  price?: number;
  spike_ts: number;
  spike_count?: number;
  estabilizado: boolean;
  delta_valid?: boolean;
  seg_hasta_delta_valid?: number;
  n_prices: number;
  slope_pct: number | null;
  cambio_pct?: number | null;
}

async function readLastLines(file: string, n: number): Promise<string[]> {
  try {
    const stat = await fs.stat(file);
    const size = stat.size;
    if (size === 0) return [];
    const readSize = Math.min(size, 16384);
    const fd = await fs.open(file, 'r');
    const buf = Buffer.alloc(readSize);
    await fd.read(buf, 0, readSize, size - readSize);
    await fd.close();
    return buf.toString('utf8').split('\n').filter(l => l.trim()).slice(-n);
  } catch {
    return [];
  }
}

function detectCaminoAll(sym: string, slope_pct: number, cambio_pct: number | null | undefined) {
  const isBoom = sym === 'BOOM500' || sym === 'BOOM1000';
  const hasCambio = cambio_pct != null;
  const abs_cambio = hasCambio ? Math.abs(cambio_pct!) : 0;

  let c1_ok = false, c2_ok = false, c3_ok = false;
  let active_camino: string | null = null;
  let pending_sec: number | null = null;

  if (C2_ENABLED && hasCambio && sym === 'BOOM500') {
    if (slope_pct <= C2_BOOM_MAX && abs_cambio <= C2_CAMBIO_MAX) c2_ok = true;
  } else if (C2_ENABLED && hasCambio && sym === 'CRASH500') {
    if (slope_pct >= C2_CRASH_MIN && abs_cambio <= C2_CAMBIO_MAX) c2_ok = true;
  }

  if (C3_ENABLED && hasCambio) {
    if (isBoom  && slope_pct <= C3_BOOM_MAX  && cambio_pct! >= C3_CAMBIO_MIN)  c3_ok = true;
    if (!isBoom && slope_pct >= C3_CRASH_MIN && cambio_pct! <= -C3_CAMBIO_MIN) c3_ok = true;
  }

  if (hasCambio && abs_cambio >= C1_CAMBIO_MIN) {
    const boom_max  = sym === 'BOOM1000'  ? C1_BOOM1000_MAX  : C1_BOOM_MAX;
    const crash_min = sym === 'CRASH1000' ? C1_CRASH1000_MIN : C1_CRASH_MIN;
    if (isBoom  && slope_pct <= boom_max)  c1_ok = true;
    if (!isBoom && slope_pct >= crash_min) c1_ok = true;
  }

  if (c2_ok)      { active_camino = 'camino2_pn5';     pending_sec = C2_PENDING; }
  else if (c3_ok) { active_camino = 'camino3_breakout'; pending_sec = C3_PENDING; }
  else if (c1_ok) {
    active_camino = 'camino1_level';
    pending_sec = (sym === 'BOOM1000' || sym === 'CRASH1000') ? C1_PENDING_1000 : C1_PENDING;
  }

  return { c1_ok, c2_ok, c3_ok, active_camino, pending_sec, passing: c1_ok || c2_ok || c3_ok };
}

function buildThresholds(sym: string) {
  const is1000 = sym.includes('1000');
  const isBoom = sym.startsWith('BOOM');
  return {
    c1: {
      boom_max:   is1000 ? C1_BOOM1000_MAX   : C1_BOOM_MAX,
      crash_min:  is1000 ? C1_CRASH1000_MIN  : C1_CRASH_MIN,
      cambio_min: C1_CAMBIO_MIN,
      pending:    is1000 ? C1_PENDING_1000   : C1_PENDING,
    },
    c2: { enabled: C2_ENABLED && !is1000, boom_max: C2_BOOM_MAX, crash_min: C2_CRASH_MIN, cambio_max: C2_CAMBIO_MAX, pending: C2_PENDING },
    c3: { enabled: C3_ENABLED, boom_max: C3_BOOM_MAX, crash_min: C3_CRASH_MIN, cambio_min: C3_CAMBIO_MIN, pending: C3_PENDING },
    grace_sec: is1000 ? GRACE_1000_SEC : GRACE_500_SEC,
  };
}

export async function GET() {
  const now = Date.now() / 1000;

  // Try reading from d10_panel_state.json (written by slope_tracker.py every 5s)
  try {
    const raw = await fs.readFile(PANEL_FILE, 'utf8');
    const panelRaw = JSON.parse(raw) as Record<string, unknown>;
    const panelUpdatedAt = typeof panelRaw.updated_at === 'number' ? panelRaw.updated_at : now;
    const result: Record<string, unknown> = { updated_at: now, source: 'panel_state' };

    for (const sym of ALL_SYMBOLS) {
      const e = panelRaw[sym] as PanelEntry | undefined;
      if (!e?.available) {
        result[sym] = { symbol: sym, available: false };
        continue;
      }
      result[sym] = {
        ...e,
        age_s: Math.round(now - panelUpdatedAt),
        thresholds: buildThresholds(sym),
        // legacy compat
        estabilizado: e.delta_valid ?? true,
        stabilize_sec: 0,
      };
    }
    return NextResponse.json(result);
  } catch { /* fallback to JSONL */ }

  // Fallback: parse slope_history.jsonl
  const lines = await readLastLines(SLOPE_LOG, 80);
  const bySymbol: Record<string, SlopeEntry> = {};
  for (const line of lines) {
    try {
      const entry = JSON.parse(line) as SlopeEntry;
      const sym = String(entry.symbol || '').toUpperCase();
      if (!bySymbol[sym] || entry.ts > bySymbol[sym].ts) bySymbol[sym] = entry;
    } catch { /* skip */ }
  }

  const result: Record<string, unknown> = { updated_at: now, source: 'slope_jsonl' };

  for (const sym of ALL_SYMBOLS) {
    const e = bySymbol[sym];
    if (!e) {
      result[sym] = { symbol: sym, available: false };
      continue;
    }

    const age_s = Math.round(now - e.ts);
    const slope_pct = e.slope_pct;
    const cambio_pct = e.cambio_pct ?? null;
    const delta_valid = e.delta_valid !== undefined ? e.delta_valid : true;
    const grace_countdown_sec = e.seg_hasta_delta_valid ?? 0;

    let passing = false, block_reason = '';
    let c1_ok = false, c2_ok = false, c3_ok = false;
    let active_camino: string | null = null;
    let pending_sec: number | null = null;

    if (!delta_valid) {
      block_reason = `grace_${Math.round(grace_countdown_sec)}s`;
    } else if (e.n_prices < 10) {
      block_reason = `insuf_data_${e.n_prices}_pts`;
    } else if (slope_pct == null) {
      block_reason = 'slope_calc_error';
    } else {
      const res = detectCaminoAll(sym, slope_pct, cambio_pct);
      ({ c1_ok, c2_ok, c3_ok, active_camino, pending_sec, passing } = res);
      if (!passing) {
        const isBoom = sym.startsWith('BOOM');
        const thr = buildThresholds(sym);
        block_reason = `bloqueado slope=${slope_pct >= 0 ? '+' : ''}${slope_pct.toFixed(5)}%`;
      } else {
        block_reason = `${active_camino} slope=${slope_pct >= 0 ? '+' : ''}${slope_pct.toFixed(5)}%`;
      }
    }

    result[sym] = {
      symbol: sym, available: true,
      slope_pct, cambio_pct,
      delta_valid, grace_countdown_sec,
      estabilizado: delta_valid, // legacy compat
      spike_ts: e.spike_ts,
      spike_count: e.spike_count ?? 0,
      n_prices: e.n_prices,
      price: e.price,
      passing, active_camino, pending_sec,
      c1_ok, c2_ok, c3_ok,
      block_reason,
      age_s,
      stabilize_sec: 0,
      thresholds: buildThresholds(sym),
    };
  }

  return NextResponse.json(result);
}
