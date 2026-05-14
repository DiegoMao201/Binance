/**
 * monteCarloEngine.ts
 * Pure TypeScript simulation engine for OptiFerre-Trader Monte Carlo.
 *
 * Bot constraints (Phase 9–11, mirrored from main_loop.py):
 *   Fee:    0.20% round-trip on notional (deducted on EVERY trade)
 *   SL:    -1.20% on notional (hard stop loss, fixed)
 *   Risk:   2.00% of current equity risked per trade
 *   Notional = equity × (RISK_PCT / SL_PCT) = equity × 1.6667
 *
 * Win exit distribution (trailing stop tiers, conditional on winning):
 *   T1 (+0.20%)  →  5% of wins   [price barely moved up, trailed out]
 *   T2 (+0.40%)  → 10% of wins
 *   T3 (+0.80%)  → 10% of wins
 *   T4 (+1.20%)  → 15% of wins   [trail locked in +1.20%]
 *   TP (+1.80–2.40%) → 60% of wins [take profit hit, uniform distribution]
 *
 * This file is the TypeScript reference implementation.
 * The Web Worker (public/workers/monte-carlo-worker.js) contains
 * the equivalent plain-JS version that runs off-main-thread.
 */

// ─── Interfaces ───────────────────────────────────────────────────────────────

export interface SimParams {
  /** Starting capital in USDT */
  initialCapital: number;
  /** Win rate [0.40, 0.60] — bot range: 0.46–0.52 */
  winRate: number;
  /** Average completed trades per day */
  tradesPerDay: number;
  /** Projection horizon in days (default 30) */
  days: number;
  /** Number of Monte Carlo paths (default 10,000) */
  iterations: number;
}

export interface SimResults {
  /** Day index [0 … days] */
  days: number[];
  /** 5th percentile equity at each day */
  p5: number[];
  /** 25th percentile equity at each day */
  p25: number[];
  /** 50th percentile (median) equity at each day */
  p50: number[];
  /** 75th percentile equity at each day */
  p75: number[];
  /** 95th percentile equity at each day */
  p95: number[];

  /** Final-day percentile values */
  finalP5: number;
  finalP25: number;
  finalP50: number;
  finalP75: number;
  finalP95: number;

  /** Fraction of paths that end above initialCapital */
  probProfit: number;
  /** Maximum drawdown on the median (P50) path */
  maxDrawdownMedian: number;
  /** (finalP50 − initialCapital) / initialCapital */
  expectedReturnMedian: number;
  /** Actual number of iterations run */
  iterationsRun: number;
}

// ─── Bot Constants ────────────────────────────────────────────────────────────

/** 0.20% round-trip commission (both legs) */
export const FEE_PCT = 0.002;
/** Hard stop-loss at -1.20% of notional */
export const SL_PCT = 0.012;
/** Maximum capital risked per trade: 2% of equity */
export const RISK_PCT = 0.02;
/** Notional size multiplier: RISK_PCT / SL_PCT = 1.6667× */
export const NOTIONAL_FACTOR = RISK_PCT / SL_PCT;

// Trailing tier win distribution (cumulative thresholds):
const T1_MAX = 0.05;  // 5%  → exit at +0.20%
const T2_MAX = 0.15;  // 10% → exit at +0.40%
const T3_MAX = 0.25;  // 10% → exit at +0.80%
const T4_MAX = 0.40;  // 15% → exit at +1.20%
// remaining 60%       → TP between +1.80% and +2.40%
const TP_MIN  = 0.018;
const TP_BAND = 0.006; // range: [0.018, 0.024]

// ─── Fast PRNG: Mulberry32 ────────────────────────────────────────────────────
// ~4× faster than Math.random() with acceptable statistical quality.

function mulberry32(seed: number): () => number {
  return function () {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ─── Percentile helper (linear interpolation on sorted array) ────────────────

export function percentile(sorted: Float64Array, p: number): number {
  const n = sorted.length;
  if (n === 0) return 0;
  const idx = (p / 100) * (n - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

// ─── Core Simulation ─────────────────────────────────────────────────────────

export function runMonteCarlo(params: SimParams): SimResults {
  const { initialCapital, winRate, tradesPerDay, days, iterations } = params;

  // Flat Float64Array: index = day * iterations + iter
  // More cache-friendly than array-of-arrays.
  const equity = new Float64Array((days + 1) * iterations);
  const rng = mulberry32(((Date.now() ^ 0xdeadbeef) >>> 0) + iterations);

  // ── Run all paths ─────────────────────────────────────────────────────────
  for (let iter = 0; iter < iterations; iter++) {
    let cap = initialCapital;
    equity[iter] = cap; // day 0

    for (let day = 0; day < days; day++) {
      for (let t = 0; t < tradesPerDay; t++) {
        if (cap <= 0) break;

        const notional = cap * NOTIONAL_FACTOR;
        const fee      = notional * FEE_PCT; // always deducted

        if (rng() < winRate) {
          // ── Win: determine which tier ──────────────────────────────────
          const r = rng();
          let winPct: number;
          if      (r < T1_MAX)  winPct = 0.002;                   // T1: +0.20%
          else if (r < T2_MAX)  winPct = 0.004;                   // T2: +0.40%
          else if (r < T3_MAX)  winPct = 0.008;                   // T3: +0.80%
          else if (r < T4_MAX)  winPct = 0.012;                   // T4: +1.20%
          else                  winPct = TP_MIN + rng() * TP_BAND; // TP: +1.80–2.40%

          cap = cap + notional * winPct - fee;
        } else {
          // ── Loss: hard SL ──────────────────────────────────────────────
          cap = cap - notional * SL_PCT - fee;
        }

        if (cap < 0) cap = 0;
      }
      equity[(day + 1) * iterations + iter] = cap;
    }
  }

  // ── Calculate percentiles for each day ───────────────────────────────────
  const p5_arr  = new Array<number>(days + 1);
  const p25_arr = new Array<number>(days + 1);
  const p50_arr = new Array<number>(days + 1);
  const p75_arr = new Array<number>(days + 1);
  const p95_arr = new Array<number>(days + 1);

  const slice = new Float64Array(iterations);

  for (let day = 0; day <= days; day++) {
    const base = day * iterations;
    for (let i = 0; i < iterations; i++) slice[i] = equity[base + i];
    slice.sort(); // in-place ascending
    p5_arr[day]  = percentile(slice, 5);
    p25_arr[day] = percentile(slice, 25);
    p50_arr[day] = percentile(slice, 50);
    p75_arr[day] = percentile(slice, 75);
    p95_arr[day] = percentile(slice, 95);
  }

  // ── Final day distribution ────────────────────────────────────────────────
  const finalBase = days * iterations;
  const finalSlice = new Float64Array(iterations);
  for (let i = 0; i < iterations; i++) finalSlice[i] = equity[finalBase + i];
  finalSlice.sort();

  const probProfit = Array.from(finalSlice).filter(v => v > initialCapital).length / iterations;

  // Max drawdown on the median path
  let hwm = p50_arr[0];
  let maxDD = 0;
  for (let d = 1; d <= days; d++) {
    hwm = Math.max(hwm, p50_arr[d]);
    const dd = hwm > 0 ? (hwm - p50_arr[d]) / hwm : 0;
    maxDD = Math.max(maxDD, dd);
  }

  const finalP50 = percentile(finalSlice, 50);

  return {
    days:  Array.from({ length: days + 1 }, (_, i) => i),
    p5:    p5_arr,
    p25:   p25_arr,
    p50:   p50_arr,
    p75:   p75_arr,
    p95:   p95_arr,
    finalP5:   percentile(finalSlice, 5),
    finalP25:  percentile(finalSlice, 25),
    finalP50:  finalP50,
    finalP75:  percentile(finalSlice, 75),
    finalP95:  percentile(finalSlice, 95),
    probProfit,
    maxDrawdownMedian: maxDD,
    expectedReturnMedian: initialCapital > 0 ? (finalP50 - initialCapital) / initialCapital : 0,
    iterationsRun: iterations,
  };
}
