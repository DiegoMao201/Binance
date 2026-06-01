#!/usr/bin/env python3
"""Honest OUT-OF-SAMPLE split-half test on ALL 1279 CRASH (no window pre-selection).

Protocol (no peeking):
  1. Sort all CRASH trades chronologically. Do NOT filter by 'good window'.
  2. Split 50/50 by time: TRAIN = first half, TEST = second half.
  3. GRID-SEARCH the LOADED threshold (ema200_dev_pct, per-symbol geo) on TRAIN ONLY,
     maximizing TRAIN net EV (with a minimum-retention guard).
  4. FREEZE that threshold; apply unchanged to TEST. Report TEST net EV + bootstrap CI95.
  5. Reverse: derive on 2nd half, test on 1st half.
Verdict: if EITHER out-of-sample CI95 includes zero → SIN_EDGE (no certified edge).

The EV anchor is realized_pnl_usdt (broker commission confirmed 0 = already net).
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np

LOGDIR = os.getenv("LOGDIR", "/data/deriv-logs")
TARGET = {"CRASH500", "CRASH600", "CRASH900"}
RNG = np.random.default_rng(20260601)
MIN_RETAIN = 0.20  # threshold must keep >=20% of train trades (avoid degenerate n=3)


def load_closed() -> list[dict[str, Any]]:
    files = sorted(set(
        glob.glob(f"{LOGDIR}/**/deriv_closed_contracts.json", recursive=True)
        + glob.glob(f"{LOGDIR}/*deriv_closed_contracts*.json")
        + glob.glob(f"{LOGDIR}/archive_*closed*.json")
        + glob.glob(f"{LOGDIR}/archive_*_closed_*.json")))
    seen: set[Any] = set()
    rows: list[dict[str, Any]] = []
    for f in files:
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        for r in d:
            if not isinstance(r, dict) or r.get("symbol") not in TARGET:
                continue
            op = r.get("opened_at_ts")
            if op is None:
                continue
            key = r.get("contract_id") or (r["symbol"], round(float(op), 2))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    rows.sort(key=lambda r: r["opened_at_ts"])
    return rows


def dev_of(r: dict[str, Any]) -> float | None:
    sb = r.get("score_breakdown") or {}
    v = sb.get("ema200_dev_pct")
    if v is None:
        d2 = sb.get("ema200_distance_pct")
        v = float(d2) * 100.0 if isinstance(d2, (int, float)) else None
    return float(v) if isinstance(v, (int, float)) else None


def geo_of(r: dict[str, Any]) -> float | None:
    sb = r.get("score_breakdown") or {}
    v = sb.get("geo_channel_pos")
    return float(v) if isinstance(v, (int, float)) else None


def pnl_of(r: dict[str, Any]) -> float:
    return float(r.get("realized_pnl_usdt") or 0.0)


def passes(r: dict[str, Any], dev_thr: float, geo_thr_500: float) -> bool:
    """LOADED filter with given thresholds. geo floor applies ONLY to CRASH500."""
    d = dev_of(r)
    if dev_thr > -990 and d is not None and d < dev_thr:
        return False
    if r.get("symbol") == "CRASH500":
        g = geo_of(r)
        if geo_thr_500 > -990 and g is not None and g < geo_thr_500:
            return False
    return True


def boot_ci(p: np.ndarray, n: int = 10000) -> tuple[float, float, float]:
    if len(p) < 5:
        return (float(p.mean()) if len(p) else 0.0, float("nan"), float("nan"))
    idx = RNG.integers(0, len(p), size=(n, len(p)))
    means = p[idx].mean(axis=1)
    return float(p.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def grid_search(train: list[dict[str, Any]]) -> tuple[float, float, float, int]:
    """Maximize TRAIN net EV (mean pnl) over dev + geo grid, with retention guard."""
    dev_grid = [-999, 0.003, 0.005, 0.006, 0.008, 0.010, 0.012, 0.015]
    geo_grid = [-999, -0.10, 0.0, 0.05, 0.10, 0.20, 0.30]
    n_train = len(train)
    best = (-999.0, -1e9, -1e9, 0)  # dev, geo, train_ev, kept
    for dv in dev_grid:
        for gv in geo_grid:
            kept = [pnl_of(r) for r in train if passes(r, dv, gv)]
            if len(kept) < max(20, int(MIN_RETAIN * n_train)):
                continue
            ev = float(np.mean(kept))
            if ev > best[2]:
                best = (dv, gv, ev, len(kept))
    return best


def stats(rows_or_p) -> str:
    if isinstance(rows_or_p, list) and rows_or_p and isinstance(rows_or_p[0], dict):
        p = np.array([pnl_of(r) for r in rows_or_p])
    else:
        p = np.asarray(rows_or_p, dtype=float)
    if len(p) == 0:
        return "n=0"
    mean, lo, hi = boot_ci(p)
    sig = "EXCLUDES 0 ✓ (EDGE)" if (lo > 0 or hi < 0) else "includes 0 ✗ (SIN_EDGE)"
    return (f"n={len(p):4d}  pnl={p.sum():+8.2f}  avg={mean:+.4f}  "
            f"WR={(p>0).mean():4.0%}  CI95=[{lo:+.4f},{hi:+.4f}]  {sig}")


def run_direction(train, test, label_train, label_test):
    dv, gv, train_ev, kept = grid_search(train)
    print(f"\n  ── Derive on {label_train} → freeze → apply to {label_test} ──")
    print(f"  Grid-optimal LOADED threshold (from {label_train}): "
          f"ema200_dev_pct>={dv}  geo_CRASH500>={gv}  "
          f"(train kept={kept}/{len(train)}, train_avg={train_ev:+.4f})")
    test_kept = [r for r in test if passes(r, dv, gv)]
    print(f"  TRAIN  in-sample  : {stats([r for r in train if passes(r, dv, gv)])}")
    print(f"  TEST   OUT-SAMPLE : {stats(test_kept)}")
    # also the raw (unfiltered) test for reference
    print(f"  TEST   raw (ref)  : {stats(test)}")
    p = np.array([pnl_of(r) for r in test_kept])
    _, lo, hi = boot_ci(p) if len(p) >= 5 else (0, float('nan'), float('nan'))
    edge = (lo > 0 or hi < 0) if len(p) >= 5 else False
    return edge, dv, gv, p


def main() -> None:
    rows = load_closed()
    n = len(rows)
    t0, t1 = float(rows[0]["opened_at_ts"]), float(rows[-1]["opened_at_ts"])
    h = n // 2
    first, second = rows[:h], rows[h:]
    cut = float(rows[h]["opened_at_ts"])
    print("#" * 82)
    print("# HONEST OUT-OF-SAMPLE SPLIT-HALF — ALL CRASH, NO WINDOW PRE-SELECTION")
    print("#" * 82)
    print(f"Total CRASH trades: {n}")
    print(f"Span: {datetime.fromtimestamp(t0,tz=timezone.utc):%Y-%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(t1,tz=timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"Chronological 50/50 cut at: "
          f"{datetime.fromtimestamp(cut,tz=timezone.utc):%Y-%m-%d %H:%M} UTC "
          f"(H1 n={len(first)}, H2 n={len(second)})")
    print(f"Baseline (no filter) — H1: {stats(first)}")
    print(f"Baseline (no filter) — H2: {stats(second)}")

    print("\n" + "=" * 82)
    print("DIRECTION A: derive on H1 (first), test on H2 (second)")
    print("=" * 82)
    edgeA, dvA, gvA, pA = run_direction(first, second, "H1", "H2")

    print("\n" + "=" * 82)
    print("DIRECTION B: derive on H2 (second), test on H1 (first)")
    print("=" * 82)
    edgeB, dvB, gvB, pB = run_direction(second, first, "H2", "H1")

    print("\n" + "#" * 82)
    print("# FINAL VERDICT (out-of-sample, both directions)")
    print("#" * 82)
    print(f"  Direction A (H1→H2): {'EDGE ✓' if edgeA else 'SIN_EDGE ✗'}")
    print(f"  Direction B (H2→H1): {'EDGE ✓' if edgeB else 'SIN_EDGE ✗'}")
    if edgeA and edgeB:
        verd = "ROBUST EDGE — both OOS CI95 exclude zero"
    elif edgeA or edgeB:
        verd = "FRAGILE / ONE-SIDED — only one direction holds OOS (regime-dependent)"
    else:
        verd = "SIN_EDGE — neither OOS CI95 excludes zero"
    print(f"\n  >>> {verd}")
    # pooled OOS (both test sets under their own frozen thresholds)
    pooled = np.concatenate([pA, pB]) if len(pA) and len(pB) else np.array([])
    if len(pooled) >= 5:
        print(f"\n  Pooled OOS (A test + B test): {stats(pooled)}")
    print("#" * 82)


if __name__ == "__main__":
    main()
