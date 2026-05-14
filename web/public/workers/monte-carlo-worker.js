/**
 * monte-carlo-worker.js
 * Web Worker — Off-main-thread Monte Carlo engine for OptiFerre-Trader.
 *
 * Equivalent logic to components/monteCarloEngine.ts.
 * Served as a static file from /public/workers/ so Next.js never touches it.
 * Instantiated in MonteCarloSimulator.tsx via:
 *   new Worker('/workers/monte-carlo-worker.js')
 *
 * Bot constraints mirrored from main_loop.py (Phase 9–11):
 *   Fee:    0.20% round-trip on notional (ALWAYS deducted)
 *   SL:    -1.20% on notional (hard stop)
 *   Risk:   2.00% equity per trade → Notional = equity × (0.02/0.012) = 1.6667×
 *
 * Win exit distribution (conditional on win):
 *   T1 (+0.20%)   → 5%  of wins
 *   T2 (+0.40%)   → 10% of wins
 *   T3 (+0.80%)   → 10% of wins
 *   T4 (+1.20%)   → 15% of wins
 *   TP (+1.8–2.4%)→ 60% of wins (uniform)
 */

"use strict";

// ─── Constants ────────────────────────────────────────────────────────────────
var FEE_PCT         = 0.002;
var SL_PCT          = 0.012;
var RISK_PCT        = 0.02;
var NOTIONAL_FACTOR = RISK_PCT / SL_PCT;  // 1.66667

var T1_MAX  = 0.05;
var T2_MAX  = 0.15;
var T3_MAX  = 0.25;
var T4_MAX  = 0.40;
var TP_MIN  = 0.018;
var TP_BAND = 0.006;

// ─── Fast PRNG: Mulberry32 ────────────────────────────────────────────────────
function mulberry32(seed) {
  return function () {
    seed = (seed + 0x6d2b79f5) | 0;
    var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ─── Percentile (linear interpolation on sorted Float64Array) ────────────────
function pctile(sorted, p) {
  var n = sorted.length;
  if (n === 0) return 0;
  var idx = (p / 100) * (n - 1);
  var lo  = Math.floor(idx);
  var hi  = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

// ─── Core Simulation ─────────────────────────────────────────────────────────
function runSimulation(params) {
  var initialCapital = params.initialCapital;
  var winRate        = params.winRate;
  var tradesPerDay   = params.tradesPerDay;
  var days           = params.days;
  var iterations     = params.iterations;

  var rng = mulberry32(((Date.now() ^ 0xdeadbeef) >>> 0) + iterations);

  // Flat Float64Array: equity[day * iterations + iter]
  var equity = new Float64Array((days + 1) * iterations);

  // ── Run all paths ─────────────────────────────────────────────────────────
  for (var iter = 0; iter < iterations; iter++) {
    var cap = initialCapital;
    equity[iter] = cap; // day 0

    for (var day = 0; day < days; day++) {
      for (var t = 0; t < tradesPerDay; t++) {
        if (cap <= 0) break;

        var notional = cap * NOTIONAL_FACTOR;
        var fee      = notional * FEE_PCT; // always deducted

        if (rng() < winRate) {
          // Win: determine trailing tier exit
          var r = rng();
          var winPct;
          if      (r < T1_MAX)  winPct = 0.002;                 // T1: +0.20%
          else if (r < T2_MAX)  winPct = 0.004;                 // T2: +0.40%
          else if (r < T3_MAX)  winPct = 0.008;                 // T3: +0.80%
          else if (r < T4_MAX)  winPct = 0.012;                 // T4: +1.20%
          else                  winPct = TP_MIN + rng() * TP_BAND; // TP: +1.80–2.40%

          cap = cap + notional * winPct - fee;
        } else {
          // Loss: hard SL hit
          cap = cap - notional * SL_PCT - fee;
        }

        if (cap < 0) cap = 0;
      }
      equity[(day + 1) * iterations + iter] = cap;
    }
  }

  // ── Percentiles for each day ──────────────────────────────────────────────
  var p5_arr  = new Array(days + 1);
  var p25_arr = new Array(days + 1);
  var p50_arr = new Array(days + 1);
  var p75_arr = new Array(days + 1);
  var p95_arr = new Array(days + 1);

  var slice = new Float64Array(iterations);

  for (var d = 0; d <= days; d++) {
    var base = d * iterations;
    for (var i = 0; i < iterations; i++) slice[i] = equity[base + i];
    slice.sort(); // ascending in-place
    p5_arr[d]  = pctile(slice, 5);
    p25_arr[d] = pctile(slice, 25);
    p50_arr[d] = pctile(slice, 50);
    p75_arr[d] = pctile(slice, 75);
    p95_arr[d] = pctile(slice, 95);
  }

  // ── Final day statistics ──────────────────────────────────────────────────
  var finalBase  = days * iterations;
  var finalSlice = new Float64Array(iterations);
  for (var i = 0; i < iterations; i++) finalSlice[i] = equity[finalBase + i];
  finalSlice.sort();

  var profitCount = 0;
  for (var i = 0; i < iterations; i++) {
    if (finalSlice[i] > initialCapital) profitCount++;
  }
  var probProfit = profitCount / iterations;

  // Max drawdown on median path
  var hwm   = p50_arr[0];
  var maxDD = 0;
  for (var d = 1; d <= days; d++) {
    if (p50_arr[d] > hwm) hwm = p50_arr[d];
    var dd = hwm > 0 ? (hwm - p50_arr[d]) / hwm : 0;
    if (dd > maxDD) maxDD = dd;
  }

  var finalP50 = pctile(finalSlice, 50);

  var daysArr = new Array(days + 1);
  for (var i = 0; i <= days; i++) daysArr[i] = i;

  return {
    days:    daysArr,
    p5:      p5_arr,
    p25:     p25_arr,
    p50:     p50_arr,
    p75:     p75_arr,
    p95:     p95_arr,
    finalP5:   pctile(finalSlice, 5),
    finalP25:  pctile(finalSlice, 25),
    finalP50:  finalP50,
    finalP75:  pctile(finalSlice, 75),
    finalP95:  pctile(finalSlice, 95),
    probProfit:            probProfit,
    maxDrawdownMedian:     maxDD,
    expectedReturnMedian:  initialCapital > 0 ? (finalP50 - initialCapital) / initialCapital : 0,
    iterationsRun:         iterations,
  };
}

// ─── Worker message handler ───────────────────────────────────────────────────
self.onmessage = function (e) {
  var result = runSimulation(e.data);
  self.postMessage(result);
};
