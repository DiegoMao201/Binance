import fs from 'node:fs';
import path from 'node:path';
import { NextResponse } from 'next/server';

export const runtime  = 'nodejs';
export const dynamic  = 'force-dynamic';

const BOT_LOGS   = process.env.DERIV_STATE_DIR || process.env.BOT_STATE_DIR || '/data/logs';
const SLOPE_FILE = path.join(BOT_LOGS, 'slope_history.jsonl');
const CHUNK_SIZE = 1_500_000; // 1.5 MB ≈ 16h de historia con 4 símbolos

const DISP_WINDOW_H  = parseFloat(process.env.ENTRADA_DIEGO_DISP_WINDOW_H   || '6.0');
const DISP_THRESH    = parseFloat(process.env.ENTRADA_DIEGO_DISP_THRESH_PCT  || '1.0');
const SYMBOLS        = ['BOOM500', 'CRASH500'];

function loadTail(): Map<string, Array<[number, number]>> {
  const result = new Map<string, Array<[number, number]>>();
  for (const s of SYMBOLS) result.set(s, []);

  let raw: Buffer;
  try {
    const fd   = fs.openSync(SLOPE_FILE, 'r');
    const size = fs.fstatSync(fd).size;
    const chunk = Math.min(size, CHUNK_SIZE);
    raw = Buffer.alloc(chunk);
    fs.readSync(fd, raw, 0, chunk, size - chunk);
    fs.closeSync(fd);
  } catch {
    return result;
  }

  for (const line of raw.toString('utf8').split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      const d = JSON.parse(t);
      const sym: string = d.symbol;
      if (SYMBOLS.includes(sym)) {
        result.get(sym)!.push([d.ts as number, d.price as number]);
      }
    } catch { /* skip malformed */ }
  }

  for (const arr of result.values()) arr.sort((a, b) => a[0] - b[0]);
  return result;
}

function priceAt(arr: Array<[number, number]>, ts: number): number | null {
  if (!arr.length) return null;
  let lo = 0, hi = arr.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid][0] < ts) lo = mid + 1; else hi = mid;
  }
  // nearest neighbor
  if (lo > 0 && Math.abs(arr[lo - 1][0] - ts) < Math.abs(arr[lo][0] - ts)) lo--;
  return arr[lo][1];
}

export async function GET() {
  const now = Date.now() / 1000;
  const history = loadTail();
  const out: Record<string, unknown> = { updated_at: now, threshold: DISP_THRESH, window_h: DISP_WINDOW_H };

  for (const sym of SYMBOLS) {
    const arr = history.get(sym)!;
    if (arr.length < 2) {
      out[sym] = { error: 'no_data' };
      continue;
    }

    const p_now = arr[arr.length - 1][1];
    const data_age_s = now - arr[arr.length - 1][0];

    // Compute displacement for multiple windows
    const windows: Record<string, number | null> = {};
    for (const h of [1, 3, 6, 12]) {
      const p_past = priceAt(arr, now - h * 3600);
      if (p_past == null || p_past === 0) { windows[`h${h}`] = null; continue; }
      const d_raw = (p_now - p_past) / p_past * 100;
      // For CRASH500, danger = price went DOWN (d_raw negative), flip for display
      windows[`h${h}`] = sym === 'BOOM500' ? d_raw : -d_raw;
    }

    // Trend: compare 6H disp now vs 30min ago
    let trend: 'up' | 'down' | 'flat' | null = null;
    const d6h_now  = windows['h6'];
    const p_6h_30a = priceAt(arr, now - DISP_WINDOW_H * 3600 - 30 * 60);
    const p_30a    = priceAt(arr, now - 30 * 60);
    if (p_6h_30a && p_30a && p_6h_30a !== 0) {
      const d6h_30a = sym === 'BOOM500'
        ? (p_30a - p_6h_30a) / p_6h_30a * 100
        : -(p_30a - p_6h_30a) / p_6h_30a * 100;
      if (d6h_now != null) {
        const delta = d6h_now - d6h_30a;
        trend = delta > 0.05 ? 'up' : delta < -0.05 ? 'down' : 'flat';
      }
    }

    const gate_active = d6h_now != null && d6h_now > DISP_THRESH;
    const alert       = d6h_now != null && d6h_now > DISP_THRESH * 0.5;

    out[sym] = {
      p_now,
      data_age_s: Math.round(data_age_s),
      windows,         // { h1, h3, h6, h12 } en % desplazamiento favorable
      d6h: d6h_now,   // alias principal
      gate_active,     // true → bot abre en $1
      alert,           // true → >0.5 × umbral, acercándose al peligro
      trend,           // 'up' | 'down' | 'flat' — ¿el desplazamiento está creciendo?
    };
  }

  return NextResponse.json(out);
}
