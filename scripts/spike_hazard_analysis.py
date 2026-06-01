#!/usr/bin/env python3
"""Inter-spike HAZARD analysis for Deriv BOOM/CRASH synthetic indices.

PURPOSE
-------
Decide — from REAL data, not hand-tuned thresholds — whether the time between
spikes carries information that justifies an "imminence" timing edge.

For each symbol we:
  1. Extract every inter-spike interval (in ticks) from the spike-event log.
     The `ticks_since_last_spike` field on each event IS that interval (the gap
     measured at the moment the spike fired).
  2. Estimate the empirical discrete HAZARD via an actuarial life-table:
         h(gap) = P(spike fires in this tick-bin | survived gap ticks w/o spike)
     using fixed-width bins (default 50 ticks).
  3. Test MEMORYLESSNESS: a geometric/exponential process has a FLAT hazard
     (the gap tells you nothing — imminence would be fiction). We compare the
     interval distribution against the best-fit exponential with a
     Kolmogorov–Smirnov statistic AND measure the slope/correlation of the
     per-tick hazard vs gap. Verdict: FLAT (memoryless) or INCREASING
     (refractory period → imminence justified, with a MEASURED threshold).
  4. If hazard increases: report the gap where it first crosses the baseline
     constant rate (lambda) and where it crosses an "elevated" floor
     (default 1.5x lambda) — these are the data-driven loaded-zone thresholds.
  5. Emit a per-symbol summary table (n, mean, p25/p50/p75, verdict, gap_opt).

This is ANALYSIS ONLY. No integration, no side effects on the bot.

USAGE
-----
    python3 spike_hazard_analysis.py FILE [FILE ...] [--bin 50] [--floor 1.5]
                                     [--min-gap 1] [--json OUT.json]

FILE may be one or more deriv_spike_events.json paths (live + archives). Records
are merged and de-duplicated by (symbol, ts).
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from typing import Any

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"numpy required: {exc}")


# ──────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────
def load_events(paths: list[str]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            data = json.load(open(p))
        except Exception as exc:
            print(f"[warn] could not read {p}: {exc}")
            continue
        if not isinstance(data, list):
            continue
        for r in data:
            if not isinstance(r, dict):
                continue
            key = (r.get("symbol"), round(float(r.get("ts") or 0.0), 3))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


def intervals_by_symbol(
    events: list[dict[str, Any]], min_gap: int
) -> dict[str, np.ndarray]:
    """Return per-symbol arrays of inter-spike intervals (ticks).

    Primary source is `ticks_since_last_spike`. As a fallback/cross-check we
    can also reconstruct intervals from consecutive spike timestamps, but the
    tick-domain field is the canonical one used by the rest of the stack.
    """
    raw: dict[str, list[float]] = defaultdict(list)
    for r in events:
        sym = r.get("symbol")
        g = r.get("ticks_since_last_spike")
        if sym is None or g is None:
            continue
        try:
            gv = float(g)
        except (TypeError, ValueError):
            continue
        if gv >= min_gap and math.isfinite(gv):
            raw[sym].append(gv)
    return {s: np.asarray(v, dtype=float) for s, v in raw.items() if len(v) >= 8}


# ──────────────────────────────────────────────────────────────────────────
# Hazard estimation (actuarial life-table)
# ──────────────────────────────────────────────────────────────────────────
def life_table_hazard(
    intervals: np.ndarray, bin_width: int
) -> list[dict[str, float]]:
    iv = np.sort(intervals)
    n = len(iv)
    if n == 0:
        return []
    # Cap the table at p98 so a couple of huge gaps don't create empty tails.
    max_gap = float(np.percentile(iv, 98)) + bin_width
    edges = np.arange(0.0, max_gap + bin_width, bin_width)
    rows: list[dict[str, float]] = []
    for a in edges[:-1]:
        b = a + bin_width
        n_at_risk = int(np.sum(iv >= a))
        d = int(np.sum((iv >= a) & (iv < b)))
        if n_at_risk == 0:
            continue
        h_bin = d / n_at_risk            # P(fire in bin | survived to a)
        h_tick = h_bin / bin_width       # ~per-tick hazard
        rows.append(
            {
                "gap_lo": float(a),
                "gap_hi": float(b),
                "mid": float(a + bin_width / 2.0),
                "at_risk": n_at_risk,
                "events": d,
                "h_bin": h_bin,
                "h_tick": h_tick,
            }
        )
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Memorylessness tests
# ──────────────────────────────────────────────────────────────────────────
def ks_vs_exponential(intervals: np.ndarray) -> tuple[float, float, float]:
    """KS statistic + asymptotic p-value of intervals vs best-fit exponential.

    Returns (D, p_value, lambda_hat). High p (>~0.10) => cannot reject
    exponential => consistent with memoryless.
    """
    iv = np.sort(intervals)
    n = len(iv)
    mean = float(np.mean(iv))
    if mean <= 0:
        return 0.0, 1.0, 0.0
    lam = 1.0 / mean
    # Empirical CDF at each sample vs theoretical exponential CDF.
    f_theory = 1.0 - np.exp(-lam * iv)
    f_emp_hi = np.arange(1, n + 1) / n
    f_emp_lo = np.arange(0, n) / n
    d = float(np.max(np.maximum(np.abs(f_emp_hi - f_theory), np.abs(f_theory - f_emp_lo))))
    # Asymptotic Kolmogorov p-value.
    en = math.sqrt(n)
    x = (en + 0.12 + 0.11 / en) * d
    if x < 1e-3:
        p = 1.0
    else:
        s = 0.0
        for k in range(1, 101):
            s += ((-1) ** (k - 1)) * math.exp(-2.0 * k * k * x * x)
        p = max(0.0, min(1.0, 2.0 * s))
    return d, p, lam


def hazard_trend(rows: list[dict[str, float]]) -> dict[str, float]:
    """Weighted linear regression of per-tick hazard vs gap midpoint.

    Weight by n_at_risk so early, well-populated bins dominate. Returns slope,
    Pearson correlation, and the ratio of late-half mean hazard to early-half
    mean hazard (a robust monotonicity proxy).
    """
    if len(rows) < 3:
        return {"slope": 0.0, "corr": 0.0, "late_early_ratio": 1.0}
    x = np.array([r["mid"] for r in rows])
    y = np.array([r["h_tick"] for r in rows])
    w = np.array([r["at_risk"] for r in rows], dtype=float)
    w = w / w.sum()
    xm = np.sum(w * x)
    ym = np.sum(w * y)
    cov = np.sum(w * (x - xm) * (y - ym))
    vx = np.sum(w * (x - xm) ** 2)
    vy = np.sum(w * (y - ym) ** 2)
    slope = cov / vx if vx > 0 else 0.0
    corr = cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0
    half = len(rows) // 2
    early = np.mean([r["h_tick"] for r in rows[:half]]) or 1e-12
    late = np.mean([r["h_tick"] for r in rows[half:]])
    return {
        "slope": float(slope),
        "corr": float(corr),
        "late_early_ratio": float(late / early),
    }


def crossings(rows: list[dict[str, float]], lam: float, floor_mult: float) -> dict[str, Any]:
    """Gap where per-tick hazard first crosses the baseline lambda and floor."""
    cross_base = None
    cross_floor = None
    for r in rows:
        if cross_base is None and r["h_tick"] >= lam:
            cross_base = r["gap_lo"]
        if cross_floor is None and r["h_tick"] >= lam * floor_mult:
            cross_floor = r["gap_lo"]
    return {"gap_cross_baseline": cross_base, "gap_cross_floor": cross_floor}


def dominant_peak(
    rows: list[dict[str, float]], lam: float, p75: float, n_total: int
) -> dict[str, Any]:
    """Find the dominant hazard hump in the WELL-POPULATED region.

    Restricts to bins below p75 (the bulk of the mass) AND with enough at-risk
    survivors (>=20% of n) so a sparse tail bin with 1-2 events isn't mistaken
    for the peak. Returns the gap of the robust dominant hump.
    """
    floor_mass = max(20, int(0.20 * n_total))
    cand = [r for r in rows if r["gap_hi"] <= p75 * 1.05 and r["at_risk"] >= floor_mass]
    if not cand:
        cand = [r for r in rows if r["at_risk"] >= floor_mass] or rows
    peak = max(cand, key=lambda r: r["h_tick"])
    elevated = sum(1 for r in cand if r["h_tick"] >= lam * 1.3)
    return {
        "peak_gap_lo": peak["gap_lo"],
        "peak_gap_hi": peak["gap_hi"],
        "peak_ratio": peak["h_tick"] / lam if lam > 0 else 0.0,
        "n_elevated_bins": elevated,
        "n_bins": len(cand),
    }


# ──────────────────────────────────────────────────────────────────────────
# Per-symbol orchestration
# ──────────────────────────────────────────────────────────────────────────
def analyze_symbol(
    sym: str, iv: np.ndarray, bin_width: int, floor_mult: float
) -> dict[str, Any]:
    rows = life_table_hazard(iv, bin_width)
    d, p, lam = ks_vs_exponential(iv)
    trend = hazard_trend(rows)
    cr = crossings(rows, lam, floor_mult)
    p90 = float(np.percentile(iv, 90))
    p75v = float(np.percentile(iv, 75))
    peak = dominant_peak(rows, lam, p75v, len(iv))

    # Verdict logic (4 patterns):
    #   MEMORYLESS  KS cannot reject exponential (p>0.10) AND hazard ~flat.
    #               → the gap is noise; imminence is fiction.
    #   INCREASING  hazard rises monotonically (corr>0.40, late/early>1.30).
    #               → genuine refractory period; imminence justified.
    #   FRONT-LOAD  hazard concentrated early (corr<-0.30, late/early<0.80) or
    #               dominant peak in the first ~p25 zone. → enter soon AFTER spike.
    #   PERIODIC    KS rejects exponential (p<0.05) but hazard is not monotone
    #               (multimodal humps near the cycle period). → there IS an edge,
    #               but it lives at the hazard PEAK, not "the longer the better".
    increasing = (trend["corr"] > 0.40 and trend["late_early_ratio"] > 1.30)
    front_load = (
        (trend["corr"] < -0.30 and trend["late_early_ratio"] < 0.80)
        or peak["peak_gap_hi"] <= np.percentile(iv, 25)
    )
    memoryless = (p > 0.10 and abs(trend["corr"]) < 0.30 and 0.7 < trend["late_early_ratio"] < 1.4)
    structured = (p < 0.05)
    if increasing:
        verdict = "INCREASING (refractory → imminence JUSTIFIED)"
        gap_opt = cr["gap_cross_floor"] or cr["gap_cross_baseline"]
    elif memoryless:
        verdict = "FLAT (memoryless → gap is NOISE, imminence unjustified)"
        gap_opt = None
    elif front_load:
        verdict = "FRONT-LOADED (hazard peaks EARLY → edge is soon after a spike)"
        gap_opt = peak["peak_gap_lo"]
    elif structured:
        verdict = "PERIODIC (non-memoryless, multimodal → edge at hazard PEAK)"
        gap_opt = peak["peak_gap_lo"]
    else:
        verdict = "AMBIGUOUS (weak signal / small sample)"
        gap_opt = cr["gap_cross_floor"]

    return {
        "symbol": sym,
        "n_intervals": int(len(iv)),
        "mean": float(np.mean(iv)),
        "std": float(np.std(iv)),
        "cv": float(np.std(iv) / np.mean(iv)) if np.mean(iv) > 0 else 0.0,
        "p25": float(np.percentile(iv, 25)),
        "p50": float(np.percentile(iv, 50)),
        "p75": float(np.percentile(iv, 75)),
        "p90": p90,
        "ks_D": d,
        "ks_p": p,
        "lambda_per_tick": lam,
        "hazard_slope": trend["slope"],
        "hazard_corr": trend["corr"],
        "late_early_ratio": trend["late_early_ratio"],
        "peak_gap_lo": peak["peak_gap_lo"],
        "peak_gap_hi": peak["peak_gap_hi"],
        "peak_ratio": peak["peak_ratio"],
        "n_elevated_bins": peak["n_elevated_bins"],
        "gap_cross_baseline": cr["gap_cross_baseline"],
        "gap_cross_floor": cr["gap_cross_floor"],
        "gap_opt_suggested": gap_opt,
        "verdict": verdict,
        "_rows": rows,
    }


def fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description="Inter-spike hazard analysis")
    ap.add_argument("files", nargs="+", help="deriv_spike_events.json path(s)")
    ap.add_argument("--bin", type=int, default=50, help="hazard bin width (ticks)")
    ap.add_argument("--floor", type=float, default=1.5, help="elevated-hazard multiplier vs lambda")
    ap.add_argument("--min-gap", type=int, default=1, help="discard intervals below this")
    ap.add_argument("--json", default=None, help="write full JSON summary here")
    ap.add_argument("--show-curve", action="store_true", help="print per-bin hazard curve")
    args = ap.parse_args()

    events = load_events(args.files)
    by_sym = intervals_by_symbol(events, args.min_gap)
    if not by_sym:
        raise SystemExit("no symbols with >=8 intervals found")

    results = {
        s: analyze_symbol(s, iv, args.bin, args.floor)
        for s, iv in sorted(by_sym.items())
    }

    print(f"\nLoaded {len(events)} events → {len(by_sym)} symbols with usable intervals")
    print(f"Bin width = {args.bin} ticks | elevated floor = {args.floor}x lambda\n")
    hdr = (
        f"{'symbol':9s} {'n':>4s} {'mean':>7s} {'p25':>6s} {'p50':>6s} {'p75':>6s} "
        f"{'KS_p':>6s} {'h_corr':>7s} {'lt/er':>6s} {'peak_gap':>9s} {'pk_x':>5s} "
        f"{'gap_opt':>8s}  verdict"
    )
    print(hdr)
    print("-" * len(hdr))
    for s, r in results.items():
        print(
            f"{s:9s} {r['n_intervals']:4d} {r['mean']:7.1f} {r['p25']:6.0f} "
            f"{r['p50']:6.0f} {r['p75']:6.0f} {r['ks_p']:6.3f} {r['hazard_corr']:7.2f} "
            f"{r['late_early_ratio']:6.2f} {r['peak_gap_lo']:4.0f}-{r['peak_gap_hi']:<4.0f} "
            f"{r['peak_ratio']:4.1f}x {fmt(r['gap_opt_suggested'],0):>8s}  {r['verdict']}"
        )

    if args.show_curve:
        for s, r in results.items():
            print(f"\n── hazard curve {s} (lambda={r['lambda_per_tick']:.5f}/tick) ──")
            print(f"{'gap':>10s} {'at_risk':>8s} {'events':>7s} {'h/tick':>9s} {'vs_lambda':>9s}")
            for row in r["_rows"]:
                ratio = row["h_tick"] / r["lambda_per_tick"] if r["lambda_per_tick"] > 0 else 0.0
                bar = "#" * int(min(40, ratio * 10))
                print(
                    f"{row['gap_lo']:5.0f}-{row['gap_hi']:<4.0f} {row['at_risk']:8d} "
                    f"{row['events']:7d} {row['h_tick']:9.5f} {ratio:8.2f}x {bar}"
                )

    if args.json:
        clean = {
            s: {k: v for k, v in r.items() if k != "_rows"} | {"curve": r["_rows"]}
            for s, r in results.items()
        }
        json.dump(clean, open(args.json, "w"), indent=2)
        print(f"\nWrote JSON summary → {args.json}")

    print("\nINTERPRETATION")
    print("  FLAT/memoryless  → the gap-since-last-spike does NOT predict the next")
    print("                     spike; the current imminence curve is unjustified.")
    print("  INCREASING       → a refractory period exists; imminence is real and")
    print("                     gap_opt is the DATA-DRIVEN loaded-zone threshold.")
    print("  DECREASING       → spikes cluster; best edge is right AFTER one fires.")


if __name__ == "__main__":
    main()
