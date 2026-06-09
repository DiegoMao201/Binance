import fs from 'node:fs/promises';
import path from 'node:path';
import { NextRequest, NextResponse } from 'next/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const DERIV_LOGS = process.env.DERIV_STATE_DIR || '/data/deriv-logs';
const MC_FILE = path.join(DERIV_LOGS, 'deriv_market_context.json');

// In-memory cache to avoid parsing 3.6MB on every request
let _cache: { ts: number; data: any[] } | null = null;
const CACHE_TTL = 10_000;

async function loadAll(): Promise<any[]> {
  const now = Date.now();
  if (_cache && now - _cache.ts < CACHE_TTL) return _cache.data;
  const raw = await fs.readFile(MC_FILE, 'utf8');
  const data = JSON.parse(raw) as any[];
  _cache = { ts: now, data };
  return data;
}

interface SpikeStats {
  sample_size: number;
  mean_gap: number;
  std_gap: number;
  p25_gap: number;
  p50_gap: number;
  p75_gap: number;
  p90_gap: number;
  current_wait: number;
  z_score: number;
  prob_50: number;
  prob_100: number;
  prob_200: number;
  prob_500: number;
}

function computeStats(entries: any[]): SpikeStats | null {
  const gaps: number[] = [];
  for (let i = 1; i < entries.length; i++) {
    const prev = entries[i - 1].ticks_since_last_spike;
    const curr = entries[i].ticks_since_last_spike;
    // Spike detected when ticks_since_last_spike resets downward by more than 10
    if (typeof prev === 'number' && typeof curr === 'number' && curr < prev - 10) {
      gaps.push(prev);
    }
  }
  if (gaps.length < 5) return null;

  const sorted = [...gaps].sort((a, b) => a - b);
  const mean = gaps.reduce((s, g) => s + g, 0) / gaps.length;
  const variance = gaps.reduce((s, g) => s + (g - mean) ** 2, 0) / gaps.length;
  const std = Math.sqrt(variance);
  const pct = (q: number) => sorted[Math.min(Math.floor(sorted.length * q), sorted.length - 1)];

  const latest = entries[entries.length - 1];
  const current_wait = latest?.ticks_since_last_spike ?? 0;
  const z = std > 0 ? (current_wait - mean) / std : 0;
  const prob = (x: number) => sorted.filter(g => g <= current_wait + x).length / sorted.length;

  return {
    sample_size: gaps.length,
    mean_gap: Math.round(mean),
    std_gap: Math.round(std),
    p25_gap: pct(0.25),
    p50_gap: pct(0.5),
    p75_gap: pct(0.75),
    p90_gap: pct(0.9),
    current_wait,
    z_score: +z.toFixed(2),
    prob_50: +prob(50).toFixed(3),
    prob_100: +prob(100).toFixed(3),
    prob_200: +prob(200).toFixed(3),
    prob_500: +prob(500).toFixed(3),
  };
}

export async function GET(request: NextRequest) {
  const symbol = request.nextUrl.searchParams.get('symbol');
  const validSymbols = ['BOOM500', 'CRASH500', 'CRASH600', 'CRASH900'];
  if (!symbol || !validSymbols.includes(symbol)) {
    return NextResponse.json({ error: 'Invalid symbol' }, { status: 400 });
  }

  try {
    const all = await loadAll();
    const entries = all
      .filter(e => e.symbol === symbol && typeof e.ts === 'number')
      .sort((a, b) => a.ts - b.ts);

    if (entries.length === 0) {
      return NextResponse.json({ symbol, snapshot: null, spike_stats: null });
    }

    const latest = entries[entries.length - 1];
    const recent = entries.slice(-2000);
    const spike_stats = computeStats(recent);

    // Hurst history — 12 evenly-spaced samples from last 20 min
    const nowSec = Date.now() / 1000;
    const histWindow = entries.filter(e => {
      if (e.hurst == null || typeof e.ts !== 'number') return false;
      const tsSec = e.ts > 1e10 ? e.ts / 1000 : e.ts;
      return tsSec >= nowSec - 20 * 60;
    });
    const HIST_N = 12;
    const hurst_history: number[] = [];
    if (histWindow.length > 0) {
      for (let i = 0; i < HIST_N; i++) {
        const idx = Math.round((i / Math.max(HIST_N - 1, 1)) * (histWindow.length - 1));
        hurst_history.push(+histWindow[Math.min(idx, histWindow.length - 1)].hurst.toFixed(3));
      }
    }

    return NextResponse.json({
      symbol,
      snapshot: {
        ts: latest.ts,
        iso: latest.iso,
        hurst: latest.hurst,
        regime: latest.regime,
        atr: latest.atr,
        atr_percentile: latest.atr_percentile,
        ema200_distance_pct: latest.ema200_distance_pct,
        ticks_since_last_spike: latest.ticks_since_last_spike,
        spike_cluster_active: latest.spike_cluster_active ?? false,
        tick_rate_5s: latest.tick_rate_5s,
        range_rolling_pct_60s: latest.range_rolling_pct_60s,
        post_spike_decay_slope: latest.post_spike_decay_slope,
        fvg_active: latest.fvg_active ?? null,
        fvg_direction: latest.fvg_direction ?? null,
        fvg_mid: latest.fvg_mid ?? null,
        smc_bonus: latest.smc_bonus ?? null,
        setup_type: latest.setup_type ?? null,
        execution_grade: latest.execution_grade ?? null,
        scarcity_state: latest.scarcity_state ?? null,
        scarcity_ratio: latest.scarcity_ratio ?? null,
        spike_imminence_state: latest.spike_imminence_state ?? null,
        spike_imminence_score: latest.spike_imminence_score ?? null,
        geo_channel_pos: latest.geo_channel_pos ?? null,
        fvg_tier: latest.fvg_tier ?? null,
        score_raw: latest.score_raw ?? null,
        last_spike_wall_ts: latest.last_spike_wall_ts ?? null,
        post_spike_blind: latest.post_spike_blind ?? false,
        burst_depth: latest.burst_depth ?? null,
        burst_retroceso: latest.burst_retroceso ?? null,
        burst_active: latest.burst_active ?? false,
        fvg_anchor_active: latest.fvg_anchor_active ?? false,
        fvg_anchor_age_s: latest.fvg_anchor_age_s ?? null,
        structural_fvg_active: latest.structural_fvg_active ?? null,
        structural_fvg_direction: latest.structural_fvg_direction ?? null,
        structural_fvg_confirm: latest.structural_fvg_confirm ?? false,
        structural_fvg_conflict: latest.structural_fvg_conflict ?? false,
        structural_fvg_absent: latest.structural_fvg_absent ?? false,
        atr_anchored: latest.atr_anchored ?? false,
        atr_pre_spike: latest.atr_pre_spike ?? null,
        geo_post_spike_nullified: latest.geo_post_spike_nullified ?? null,
      },
      spike_stats,
      hurst_history,
    });
  } catch (err) {
    console.error('market-context error:', err);
    return NextResponse.json(
      { error: 'Failed to read market context', details: err instanceof Error ? err.message : 'unknown' },
      { status: 500 }
    );
  }
}
