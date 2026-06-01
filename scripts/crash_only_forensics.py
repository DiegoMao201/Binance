#!/usr/bin/env python3
"""FULL forensic report for CRASH500/600/900 ONLY, isolated from BOOM.

Same machinery as the 14-day analysis, restricted to the 3 CRASH and focused on
their sustained good window. Sections:
  0. Auto-detect the best sustained N-day window (per symbol + combined)
  1. Aggregate performance (raw)
  2. LOADED sniper filter impact (ema200_dev + per-symbol geo)
  3. Per-symbol breakdown
  4. NET-EV with bootstrap CI95 (does CI exclude zero?)
  5. Split-half replication WITHIN the good window (edge real vs regime luck)
  6. Exit-rule TP sweep (peak-resolved) + split-half replication
  7. Inter-spike hazard for CRASH (timing edge?)
  8. Verdict
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
# Good window (UTC). Override via WIN_START / WIN_END (epoch or 'YYYY-MM-DD').
RNG = np.random.default_rng(7)


def _epoch(s: str | None, default: float) -> float:
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def load_closed() -> dict[str, list[dict[str, Any]]]:
    files = sorted(set(
        glob.glob(f"{LOGDIR}/**/deriv_closed_contracts.json", recursive=True)
        + glob.glob(f"{LOGDIR}/*deriv_closed_contracts*.json")
        + glob.glob(f"{LOGDIR}/archive_*closed*.json")
        + glob.glob(f"{LOGDIR}/archive_*_closed_*.json")))
    seen: set[Any] = set()
    by: dict[str, list] = defaultdict(list)
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
            by[r["symbol"]].append(r)
    for s in by:
        by[s].sort(key=lambda r: r["opened_at_ts"])
    return by


def load_spikes() -> dict[str, list[float]]:
    files = sorted(set(
        glob.glob(f"{LOGDIR}/**/deriv_spike_events.json", recursive=True)
        + glob.glob(f"{LOGDIR}/*deriv_spike_events*.json")
        + glob.glob(f"{LOGDIR}/archive_*spike*.json")))
    seen: set[Any] = set()
    by: dict[str, list] = defaultdict(list)
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
            ts = r.get("ts") or r.get("epoch") or r.get("timestamp")
            if ts is None:
                continue
            key = (r["symbol"], round(float(ts), 3))
            if key in seen:
                continue
            seen.add(key)
            by[r["symbol"]].append(float(ts))
    for s in by:
        by[s].sort()
    return by


def gv(r: dict[str, Any], k: str) -> float | None:
    sb = r.get("score_breakdown") or {}
    v = sb.get(k)
    if v is None:
        v = r.get(k)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v) if isinstance(v, (int, float)) else None


def loaded_ok(r: dict[str, Any]) -> bool:
    dev = gv(r, "ema200_dev_pct")
    if dev is None:
        d2 = gv(r, "ema200_distance_pct")
        dev = d2 * 100.0 if d2 is not None else None
    geo = gv(r, "geo_channel_pos")
    if dev is not None and dev < 0.008:
        return False
    if r.get("symbol") == "CRASH500" and geo is not None and geo < 0.10:
        return False
    return True


def stats(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {"n": 0}
    p = np.array([float(r.get("realized_pnl_usdt") or 0) for r in rows])
    pk = np.array([float(r.get("max_pnl_alcanzado") or 0) for r in rows])
    return {
        "n": len(rows), "pnl": float(p.sum()), "wr": float((p > 0).mean()),
        "green": float((pk > 0).mean()), "avg": float(p.mean()),
        "per100": float(p.sum() / len(rows) * 100),
    }


def boot_ci(p: np.ndarray, n: int = 10000) -> tuple[float, float]:
    if len(p) < 5:
        return (float("nan"), float("nan"))
    idx = RNG.integers(0, len(p), size=(n, len(p)))
    means = p[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def best_window(rows: list[dict[str, Any]], days: float = 5.0):
    if len(rows) < 20:
        return None
    ts = np.array([float(r["opened_at_ts"]) for r in rows])
    p = np.array([float(r.get("realized_pnl_usdt") or 0) for r in rows])
    w = days * 86400
    best = None
    start = ts.min()
    while start < ts.max():
        m = (ts >= start) & (ts < start + w)
        if m.sum() >= 20:
            tot = float(p[m].sum())
            if best is None or tot > best[1]:
                best = (start, tot, int(m.sum()))
        start += 3600 * 6
    return best


def fmt(d: dict) -> str:
    if d.get("n", 0) == 0:
        return "n=0"
    return (f"n={d['n']:4d}  WR={d['wr']:4.0%}  green={d['green']:4.0%}  "
            f"pnl={d['pnl']:+8.2f}  per100={d['per100']:+6.2f}")


def main() -> None:
    by = load_closed()
    allrows = [r for v in by.values() for r in v]
    allts = np.array([float(r["opened_at_ts"]) for r in allrows])
    t0g, t1g = allts.min(), allts.max()
    print("#" * 80)
    print("# FORENSE CRASH-ONLY (CRASH500 / CRASH600 / CRASH900)")
    print("#" * 80)
    print(f"Universe: {sorted(by)}  total_n={len(allrows)}")
    print(f"Full span: {datetime.fromtimestamp(t0g,tz=timezone.utc):%Y-%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(t1g,tz=timezone.utc):%Y-%m-%d %H:%M} UTC")

    # ── window selection ──
    bw = best_window(allrows, 5.0)
    ws = _epoch(os.getenv("WIN_START"), bw[0] if bw else t0g)
    we = _epoch(os.getenv("WIN_END"), ws + 5 * 86400)
    inwin = [r for r in allrows if ws <= float(r["opened_at_ts"]) < we]
    print(f"\n[SEC 0] Best sustained 5-day window (auto): "
          f"{datetime.fromtimestamp(ws,tz=timezone.utc):%Y-%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(we,tz=timezone.utc):%Y-%m-%d %H:%M} UTC  (n={len(inwin)})")

    print("\n[SEC 1] AGGREGATE PERFORMANCE")
    print(f"  ALL HISTORY  : {fmt(stats(allrows))}")
    print(f"  GOOD WINDOW  : {fmt(stats(inwin))}")
    print(f"  OUTSIDE WIN  : {fmt(stats([r for r in allrows if not (ws<=float(r['opened_at_ts'])<we)]))}")

    print("\n[SEC 2] LOADED SNIPER FILTER (ema200_dev>=0.008 + geo>=0.10 CRASH500)")
    for lbl, rs in (("ALL HISTORY", allrows), ("GOOD WINDOW", inwin)):
        kept = [r for r in rs if loaded_ok(r)]
        print(f"  {lbl:12s} raw   : {fmt(stats(rs))}")
        print(f"  {lbl:12s} LOADED: {fmt(stats(kept))}")

    print("\n[SEC 3] PER-SYMBOL (good window, raw vs LOADED)")
    for s in sorted(by):
        rs = [r for r in by[s] if ws <= float(r["opened_at_ts"]) < we]
        kept = [r for r in rs if loaded_ok(r)]
        print(f"  {s}")
        print(f"     raw    : {fmt(stats(rs))}")
        print(f"     LOADED : {fmt(stats(kept))}")

    print("\n[SEC 4] NET-EV with BOOTSTRAP CI95 (per-trade avg; CI excludes 0?)")
    for lbl, rs in (("GOOD raw", inwin),
                    ("GOOD LOADED", [r for r in inwin if loaded_ok(r)]),
                    ("ALL raw", allrows),
                    ("ALL LOADED", [r for r in allrows if loaded_ok(r)])):
        p = np.array([float(r.get("realized_pnl_usdt") or 0) for r in rs])
        lo, hi = boot_ci(p)
        sig = "EXCLUDES 0 ✓" if (lo > 0 or hi < 0) else "includes 0 ✗"
        print(f"  {lbl:12s} n={len(rs):4d}  avg={p.mean() if len(p) else 0:+.4f}  "
              f"CI95=[{lo:+.4f},{hi:+.4f}]  {sig}")

    print("\n[SEC 5] SPLIT-HALF REPLICATION WITHIN GOOD WINDOW (edge real vs luck)")
    print("  (chronological first half vs second half; does positive PnL persist?)")
    for lbl, rs in (("raw", inwin), ("LOADED", [r for r in inwin if loaded_ok(r)])):
        rs = sorted(rs, key=lambda r: r["opened_at_ts"])
        h = len(rs) // 2
        a, b = rs[:h], rs[h:]
        sa, sb = stats(a), stats(b)
        ok = (sa.get("pnl", 0) > 0 and sb.get("pnl", 0) > 0)
        print(f"  {lbl:8s} H1: {fmt(sa)}")
        print(f"  {lbl:8s} H2: {fmt(sb)}   → {'REPLICATES ✓' if ok else 'does NOT replicate ✗'}")

    print("\n[SEC 6] EXIT-RULE TP SWEEP (peak-resolved net EV, good window LOADED)")
    rs = [r for r in inwin if loaded_ok(r)]
    base = stats(rs)
    print(f"  baseline (no TP) : {fmt(base)}")
    for tp in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
        sim = []
        for r in rs:
            peak = float(r.get("max_pnl_alcanzado") or 0)
            real = float(r.get("realized_pnl_usdt") or 0)
            sim.append(tp if peak >= tp else real)
        sp = np.array(sim)
        # split-half replication of this TP
        h = len(sp) // 2
        rep = (sp[:h].sum() > 0 and sp[h:].sum() > 0) if len(sp) >= 10 else False
        print(f"  TP=${tp:.2f}        : n={len(sp)} pnl={sp.sum():+8.2f} "
              f"per100={sp.sum()/max(1,len(sp))*100:+6.2f}  "
              f"{'split-half OK ✓' if rep else 'no replic ✗'}")

    print("\n[SEC 7] INTER-SPIKE HAZARD (CRASH timing — do spikes cluster predictably?)")
    sp = load_spikes()
    for s in sorted(TARGET):
        ts = [t for t in sp.get(s, []) if ws <= t < we]
        if len(ts) < 30:
            print(f"  {s}: n_spikes={len(ts)} (insufficient in window)")
            continue
        gaps = np.diff(np.array(ts))
        gaps = gaps[(gaps > 0) & (gaps < 3600)]
        if len(gaps) < 20:
            print(f"  {s}: gaps insufficient")
            continue
        cv = float(gaps.std() / gaps.mean()) if gaps.mean() else float("nan")
        # split-half of gap distribution: do quantiles replicate?
        h = len(gaps) // 2
        q1 = np.percentile(gaps[:h], [25, 50, 75])
        q2 = np.percentile(gaps[h:], [25, 50, 75])
        drift = float(np.abs(q1 - q2).mean())
        print(f"  {s}: n_gaps={len(gaps):4d} mean={gaps.mean():5.1f}s "
              f"median={np.median(gaps):5.1f}s CV={cv:4.2f} "
              f"(CV~1=random/Poisson) split-half_q_drift={drift:4.1f}s")

    print("\n" + "#" * 80)
    print("# Interpretation key:")
    print("#  - CI95 excluding 0 = statistically real per-trade edge")
    print("#  - split-half replicate = edge persists, not a single lucky run")
    print("#  - CV≈1 + low q_drift = spikes are ~memoryless → timing not exploitable")
    print("#" * 80)


if __name__ == "__main__":
    main()
