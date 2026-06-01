#!/usr/bin/env python3
"""Backtest candidate ENTRY filters on the 3 CRASH (READ-ONLY).

For each candidate filter we report, per symbol and combined:
  retained n, never_green%, winrate, TOTAL realized PnL, and PnL per 100 trades.
Goal: pick a threshold that cuts never-green losers WITHOUT killing the winners
or turning total PnL negative. Honest test: if no filter improves net PnL, say so.
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from typing import Any, Callable

import numpy as np

LOGDIR = os.getenv("LOGDIR", "/data/deriv-logs")
TARGET = {"CRASH500", "CRASH600", "CRASH900"}


def load() -> list[dict[str, Any]]:
    files = sorted(set(
        glob.glob(f"{LOGDIR}/**/deriv_closed_contracts.json", recursive=True)
        + glob.glob(f"{LOGDIR}/*deriv_closed_contracts*.json")
        + glob.glob(f"{LOGDIR}/archive_closed_*.json")))
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
    return rows


def gv(r: dict[str, Any], k: str) -> float | None:
    sb = r.get("score_breakdown") or {}
    v = sb.get(k)
    if v is None:
        v = r.get(k)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v) if isinstance(v, (int, float)) else None


def report(rows: list[dict[str, Any]], label: str) -> None:
    if not rows:
        print(f"  {label:42s}  n=0")
        return
    pnl = np.array([float(r.get("realized_pnl_usdt") or 0) for r in rows])
    peak = np.array([float(r.get("max_pnl_alcanzado") or 0) for r in rows])
    ng = float((peak <= 0).mean())
    wr = float((pnl > 0).mean())
    tot = float(pnl.sum())
    per100 = tot / len(rows) * 100
    print(f"  {label:42s}  n={len(rows):4d}  ng={ng:4.0%}  WR={wr:4.0%}  "
          f"pnl={tot:+8.2f}  per100={per100:+6.2f}")


def main() -> None:
    rows = load()
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r["symbol"]].append(r)
    print(f"[load] n={len(rows)} {dict((s,len(v)) for s,v in by.items())}\n")

    # Candidate filters (direction-aware: CRASH wants price ABOVE ema200 = loaded)
    def f_all(r):  # noqa
        return True

    def mk_ema(thr):
        def f(r):
            v = gv(r, "ema200_dev_pct")
            return v is not None and v >= thr
        return f

    def mk_geo(thr):
        def f(r):
            v = gv(r, "geo_channel_pos")
            return v is not None and v >= thr
        return f

    def mk_ema_geo(ethr, gthr):
        def f(r):
            e = gv(r, "ema200_dev_pct"); g = gv(r, "geo_channel_pos")
            return e is not None and g is not None and e >= ethr and g >= gthr
        return f

    cands: list[tuple[str, Callable]] = [
        ("BASELINE (no filter)", f_all),
        ("ema200_dev_pct>=0.005", mk_ema(0.005)),
        ("ema200_dev_pct>=0.008", mk_ema(0.008)),
        ("ema200_dev_pct>=0.010", mk_ema(0.010)),
        ("ema200_dev_pct>=0.015", mk_ema(0.015)),
        ("geo_channel_pos>=0.0", mk_geo(0.0)),
        ("geo_channel_pos>=0.10", mk_geo(0.10)),
        ("ema>=0.008 & geo>=0.0", mk_ema_geo(0.008, 0.0)),
        ("ema>=0.010 & geo>=0.0", mk_ema_geo(0.010, 0.0)),
        ("ema>=0.008 & geo>=0.10", mk_ema_geo(0.008, 0.10)),
    ]

    print("=" * 86)
    print("COMBINED (3 CRASH)  — ng=never_green%  per100=PnL per 100 trades")
    print("=" * 86)
    for name, f in cands:
        report([r for r in rows if f(r)], name)

    for s in sorted(by):
        print("\n" + "=" * 86)
        print(f"{s}")
        print("=" * 86)
        for name, f in cands:
            report([r for r in by[s] if f(r)], name)


if __name__ == "__main__":
    main()
