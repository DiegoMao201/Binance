#!/usr/bin/env python3
"""Sniper-signature miner for CRASH500/600/900 (READ-ONLY).

Goal: find what separated the WINNERS (the real snipers) from the ~90% of losers
that NEVER touched green (peak == 0), using only the entry-time features stored in
each closed contract's score_breakdown. Output = a ranked discriminator list +
proposed entry filters, plus a 24h rolling-PnL confirmation of "good windows".
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


def load() -> dict[str, list[dict[str, Any]]]:
    files = sorted(set(
        glob.glob(f"{LOGDIR}/**/deriv_closed_contracts.json", recursive=True)
        + glob.glob(f"{LOGDIR}/*deriv_closed_contracts*.json")
        + glob.glob(f"{LOGDIR}/archive_closed_*.json")
        + glob.glob(f"{LOGDIR}/*_closed.json")))
    seen: set[Any] = set()
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in files:
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        for r in d:
            if not isinstance(r, dict):
                continue
            sym = r.get("symbol")
            op = r.get("opened_at_ts")
            if sym not in TARGET or op is None:
                continue
            cid = r.get("contract_id")
            key = cid if cid else (sym, round(float(op), 2))
            if key in seen:
                continue
            seen.add(key)
            by[sym].append(r)
    for s in by:
        by[s].sort(key=lambda r: r["opened_at_ts"])
    return by


def feat(r: dict[str, Any]) -> dict[str, float]:
    sb = r.get("score_breakdown") or {}
    out: dict[str, float] = {}
    for k, v in sb.items():
        if isinstance(v, bool):
            out[k] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)):
            out[k] = float(v)
    for k in ("ema200_distance_at_entry_pct", "spread_at_entry", "momentum_peak",
              "duracion_real_seg", "ticks_held", "max_hold_seconds"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """Mann-Whitney AUC: P(feature_winner > feature_loser). 0.5 = no separation."""
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(allv) + 1)
    # average ties
    _, inv, cnt = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.zeros(len(cnt)); np.add.at(sums, inv, ranks)
    ranks = (sums / cnt)[inv]
    r_pos = ranks[:len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def main() -> None:
    by = load()
    print(f"[load] symbols={sorted(by)} "
          f"counts={ {s: len(v) for s, v in by.items()} }")

    for s in sorted(by):
        cs = by[s]
        pnl = np.array([float(r.get("realized_pnl_usdt") or 0) for r in cs])
        peak = np.array([float(r.get("max_pnl_alcanzado") or 0) for r in cs])
        feats = [feat(r) for r in cs]
        win = pnl > 0
        never_green = peak <= 0
        print("\n" + "=" * 78)
        print(f"{s}: n={len(cs)} | winners={int(win.sum())} ({win.mean():.0%}) | "
              f"never_green={int(never_green.sum())} ({never_green.mean():.0%}) | "
              f"tot_pnl={pnl.sum():+.2f}")
        print("=" * 78)

        # Discriminator: winners vs never-green, per feature, by AUC.
        keys = set()
        for f in feats:
            keys |= set(f.keys())
        rows = []
        for k in sorted(keys):
            vals = np.array([f.get(k, np.nan) for f in feats])
            m = ~np.isnan(vals)
            wv = vals[m & win]
            lv = vals[m & never_green]
            if len(wv) < 15 or len(lv) < 15:
                continue
            a = auc(wv, lv)
            sep = abs(a - 0.5)
            rows.append((sep, a, k, float(np.median(wv)), float(np.median(lv))))
        rows.sort(reverse=True)
        print(f"  {'feature':28s} {'AUC':>6s} {'sep':>5s} {'win_med':>9s} {'never_med':>9s}")
        print(f"  {'-'*28} {'-'*6} {'-'*5} {'-'*9} {'-'*9}")
        for sep, a, k, wm, lm in rows[:14]:
            arrow = "↑win" if a > 0.5 else "↓win"
            print(f"  {k:28s} {a:6.3f} {sep:5.3f} {wm:9.4f} {lm:9.4f}  {arrow}")

    # ── 24h rolling PnL windows per symbol (confirm "good windows") ──
    print("\n" + "=" * 78)
    print("24h ROLLING PnL WINDOWS (step 6h) — confirm sustained good periods")
    print("=" * 78)
    for s in sorted(by):
        cs = by[s]
        ts = np.array([float(r["opened_at_ts"]) for r in cs])
        pnl = np.array([float(r.get("realized_pnl_usdt") or 0) for r in cs])
        peak = np.array([float(r.get("max_pnl_alcanzado") or 0) for r in cs])
        t0, t1 = ts.min(), ts.max()
        print(f"\n── {s} ──  ({datetime.fromtimestamp(t0,tz=timezone.utc):%m-%d %H:%M} → "
              f"{datetime.fromtimestamp(t1,tz=timezone.utc):%m-%d %H:%M})")
        print(f"  {'window_start':>14s} {'n':>4s} {'WR':>5s} {'pnl24h':>8s} {'green%':>7s}")
        w = 86400.0
        best = None
        start = t0
        while start < t1:
            m = (ts >= start) & (ts < start + w)
            nt = int(m.sum())
            if nt >= 10:
                p = float(pnl[m].sum())
                wr = float((pnl[m] > 0).mean())
                gr = float((peak[m] > 0).mean())
                tag = ""
                if best is None or p > best[1]:
                    best = (start, p, wr, gr, nt)
                if p > 0:
                    tag = "  +"
                print(f"  {datetime.fromtimestamp(start,tz=timezone.utc):%m-%d %H:%M} "
                      f"{nt:4d} {wr:5.2f} {p:8.2f} {gr:6.0%}{tag}")
            start += w / 4  # 6h step
        if best:
            print(f"  BEST 24h: start={datetime.fromtimestamp(best[0],tz=timezone.utc):%m-%d %H:%M} "
                  f"pnl={best[1]:+.2f} WR={best[2]:.2f} green%={best[3]:.0%} n={best[4]}")


if __name__ == "__main__":
    main()
