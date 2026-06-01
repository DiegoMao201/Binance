#!/usr/bin/env python3
"""FINAL exit-rule EV test for Deriv BOOM/CRASH (READ-ONLY, no bot code).

Question: ignoring entry timing (treat it as random), does ANY exit rule have a
POSITIVE NET-of-commission EV whose edge REPLICATES out-of-sample (split-half)?

Ground truth on cost
---------------------
PG ledger user_trade_allocations records broker commission_usdt = 0.00 for every
deriv trade: Deriv's multiplier commission is embedded in the settled contract
value, so `realized_pnl_usdt` (from the broker WS close event) is ALREADY net of
broker cost. Total broker gross over the pool = -185 USD / 2452 trades.
We therefore anchor NET EV on realized_pnl_usdt, and ADD an explicit modeled
spread/commission stress (spread_at_entry × stake × MULT, plus a flat %-of-stake
sweep) to prove robustness.

Trajectory constraint (honest)
------------------------------
Per trade we have max_pnl_alcanzado (PEAK) + ticks_held + realized. We do NOT have
the per-tick price path. So a TP sweep is fully resolvable (peak tells us if a TP
level was touched during the trade's real lifetime); an SL-tightening or a
hold-shortening sweep is UNDERDETERMINED (needs the trough / intra-trade path we
do not have). We run the TP sweep as the defensible exit-rule search and report
the hold/winners-vs-losers tick distributions for context.

Outputs: PASO1 EV+bootstrap (global, per-symbol, + commission stress) ·
PASO2 TP sweep + split-half replication · PASO3 random/azar baseline +
score→pnl signal test · EPOCH control BUGGY vs CLEAN.
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
CLEAN_CUTOFF = os.getenv("CLEAN_CUTOFF", "2026-05-30")  # ISO date, UTC
LIVE_SYMS = {"BOOM500", "CRASH500", "CRASH600"}
# Deriv multiplier per symbol (context: BOOM/CRASH MULT). Used only for the
# explicit spread-cost stress; realized PnL is the anchored net.
MULT_DEFAULT = {"BOOM500": 200, "BOOM600": 200, "BOOM900": 200, "BOOM1000": 200,
                "CRASH500": 200, "CRASH600": 200, "CRASH900": 200, "CRASH1000": 200}
RNG = np.random.default_rng(20260601)


def load_contracts() -> dict[str, list[dict[str, Any]]]:
    files = sorted(set(
        glob.glob(f"{LOGDIR}/**/deriv_closed_contracts.json", recursive=True)
        + glob.glob(f"{LOGDIR}/*deriv_closed_contracts*.json")
        + glob.glob(f"{LOGDIR}/archive_closed_*.json")
        + glob.glob(f"{LOGDIR}/*_closed.json")))
    seen: set[Any] = set()
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in files:
        try:
            data = json.load(open(f))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for r in data:
            if not isinstance(r, dict):
                continue
            cid = r.get("contract_id")
            op = r.get("opened_at_ts")
            sym = r.get("symbol")
            if sym is None or op is None:
                continue
            key = cid if cid else (sym, round(float(op), 2))
            if key in seen:
                continue
            seen.add(key)
            sb = r.get("score_breakdown") or {}
            score = None
            if isinstance(sb, dict):
                # achieved entry score: prefer score_raw, else sum of core components
                if isinstance(sb.get("score_raw"), (int, float)):
                    score = sb["score_raw"]
                else:
                    comps = ("trend", "momentum", "spread", "atr", "stability",
                             "headroom", "hd_bonus", "smc_bonus")
                    vals = [sb[k] for k in comps if isinstance(sb.get(k), (int, float))]
                    score = sum(vals) if vals else None
            by_sym[sym].append({
                "open": float(op),
                "close": float(r.get("closed_at_ts") or op),
                "exit": str(r.get("exit_reason") or "unknown"),
                "pnl": float(r.get("realized_pnl_usdt") or 0.0),
                "peak": float(r.get("max_pnl_alcanzado") or 0.0),
                "ticks_held": float(r.get("ticks_held") or 0.0),
                "stake": float(r.get("stake_usdt") or 0.0),
                "spread": float(r.get("spread_at_entry") or 0.0),
                "score": float(score) if isinstance(score, (int, float)) else float("nan"),
            })
    for s in by_sym:
        by_sym[s].sort(key=lambda r: r["open"])
    n = sum(len(v) for v in by_sym.values())
    print(f"[contracts] {len(by_sym)} symbols, {n} unique trades")
    return by_sym


def boot_ci(x: np.ndarray, n_boot: int = 10000) -> tuple[float, float, float]:
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    idx = RNG.integers(0, len(x), size=(n_boot, len(x)))
    means = x[idx].mean(axis=1)
    return float(np.mean(x)), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def spread_cost(t: dict[str, Any]) -> float:
    mult = MULT_DEFAULT.get(t.get("_sym", ""), 200)
    return t["spread"] * t["stake"] * mult  # fractional spread × leveraged notional


def net_pnl(t: dict[str, Any], extra_pct_stake: float = 0.0) -> float:
    return t["pnl"] - spread_cost(t) - extra_pct_stake * t["stake"]


# ── PASO 2: TP sweep via peak ──────────────────────────────────────────────
def tp_sweep(trades: list[dict[str, Any]], tps: list[float]) -> list[dict[str, Any]]:
    out = []
    for tp in tps:
        pnls = []
        for t in trades:
            base = tp if t["peak"] >= tp else t["pnl"]   # TP fills if peak reached it
            pnls.append(base - spread_cost(t))
        arr = np.array(pnls)
        m, lo, hi = boot_ci(arr)
        out.append({"tp": tp, "ev": m, "lo": lo, "hi": hi,
                    "wr": float((arr > 0).mean()), "n": len(arr),
                    "pos": lo > 0})
    return out


def tp_sweep_ev(trades: list[dict[str, Any]], tp: float) -> float:
    pnls = [(tp if t["peak"] >= tp else t["pnl"]) - spread_cost(t) for t in trades]
    return float(np.mean(pnls)) if pnls else float("nan")


def main() -> None:
    by_sym = load_contracts()
    for s, cs in by_sym.items():
        for t in cs:
            t["_sym"] = s
    cutoff = datetime.fromisoformat(CLEAN_CUTOFF).replace(tzinfo=timezone.utc).timestamp()
    syms = sorted(by_sym)

    # ── PASO 1 ──
    print("\n" + "=" * 80)
    print("PASO 1 — BASE EV NET OF COMMISSION  (realized_pnl anchored, broker comm=0)")
    print("=" * 80)
    hdr = (f"{'symbol':10s} {'n':>5s} {'WR':>5s} {'EV_gross':>9s} {'EV_net':>9s} "
           f"{'CI95_lo':>9s} {'CI95_hi':>9s} {'excl0':>6s} {'tot_net':>9s} {'losPk0':>7s}")
    print(hdr); print("-" * len(hdr))
    allt = []
    for s in syms:
        cs = by_sym[s]; allt += cs
        gross = np.array([t["pnl"] for t in cs])
        net = np.array([net_pnl(t) for t in cs])
        m, lo, hi = boot_ci(net)
        excl = "POS" if lo > 0 else ("NEG" if hi < 0 else "no")
        losers = [t for t in cs if t["pnl"] <= 0]
        los_pk0 = np.mean([t["peak"] <= 0 for t in losers]) if losers else float("nan")
        print(f"{s:10s} {len(cs):5d} {(gross>0).mean():5.2f} {gross.mean():9.4f} "
              f"{m:9.4f} {lo:9.4f} {hi:9.4f} {excl:>6s} {net.sum():9.2f} {los_pk0:7.2f}")
    g = np.array([t["pnl"] for t in allt]); n = np.array([net_pnl(t) for t in allt])
    m, lo, hi = boot_ci(n)
    print("-" * len(hdr))
    print(f"{'ALL':10s} {len(allt):5d} {(g>0).mean():5.2f} {g.mean():9.4f} "
          f"{m:9.4f} {lo:9.4f} {hi:9.4f} {('POS' if lo>0 else 'NEG' if hi<0 else 'no'):>6s} {n.sum():9.2f}")

    # commission stress: extra %-of-stake
    print("\n  Commission stress (extra cost as % of stake) — ALL symbols net EV:")
    for pct in (0.0, 0.005, 0.01, 0.02, 0.03):
        arr = np.array([net_pnl(t, pct) for t in allt])
        m2, lo2, hi2 = boot_ci(arr)
        print(f"    +{pct*100:4.1f}%/stake  EV={m2:8.4f}  CI95=[{lo2:7.4f},{hi2:7.4f}]  "
              f"{'POS' if lo2>0 else 'NEG' if hi2<0 else 'spans0'}")

    # ── PASO 2 ──
    print("\n" + "=" * 80)
    print("PASO 2 — TP SWEEP (peak-resolved) + SPLIT-HALF REPLICATION  [live symbols]")
    print("=" * 80)
    tps = [0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    for s in [x for x in syms if x in LIVE_SYMS]:
        cs = by_sym[s]
        peaks = np.array([t["peak"] for t in cs])
        print(f"\n── {s}  (n={len(cs)}, median peak={np.median(peaks):.3f}, "
              f"max peak={peaks.max():.2f}, real EV_net={np.mean([net_pnl(t) for t in cs]):.4f}) ──")
        print(f"  {'TP':>5s} {'EV_net':>9s} {'CI95_lo':>9s} {'CI95_hi':>9s} {'WR':>5s} "
              f"{'excl0':>6s} {'EV_h1':>8s} {'EV_h2':>8s} {'REPLICA':>8s}")
        mid = len(cs) // 2
        h1, h2 = cs[:mid], cs[mid:]
        for row in tp_sweep(cs, tps):
            tp = row["tp"]
            ev1 = tp_sweep_ev(h1, tp); ev2 = tp_sweep_ev(h2, tp)
            replica = "YES" if (row["pos"] and ev1 > 0 and ev2 > 0) else \
                      ("h+only" if ev1 > 0 and ev2 > 0 else "no")
            print(f"  {tp:5.2f} {row['ev']:9.4f} {row['lo']:9.4f} {row['hi']:9.4f} "
                  f"{row['wr']:5.2f} {('POS' if row['pos'] else 'no'):>6s} "
                  f"{ev1:8.4f} {ev2:8.4f} {replica:>8s}")

    # ── PASO 3 ──
    print("\n" + "=" * 80)
    print("PASO 3 — AZAR / SIGNAL BASELINE")
    print("=" * 80)
    print("  (No per-tick price series available → random-tick sim not possible.")
    print("   Proxies: (a) fair-game EV = 0 minus cost; (b) score→pnl signal test.)")
    print(f"\n  {'symbol':10s} {'EV_net':>9s} {'fairgame(-cost)':>15s} {'edge_vs_random':>14s} "
          f"{'corr(score,pnl)':>15s} {'p_perm':>7s}")
    for s in syms:
        cs = by_sym[s]
        net = np.array([net_pnl(t) for t in cs])
        cost = np.array([spread_cost(t) for t in cs])
        fair = -float(cost.mean())  # random fair MULT ≈ 0 gross − cost
        edge = float(net.mean()) - fair
        sc = np.array([t["score"] for t in cs])
        pn = np.array([t["pnl"] for t in cs])
        mask = ~np.isnan(sc)
        if mask.sum() > 20 and np.std(sc[mask]) > 0:
            r = float(np.corrcoef(sc[mask], pn[mask])[0, 1])
            # permutation p-value
            obs = abs(r); cnt = 0; B = 2000
            scm = sc[mask]; pnm = pn[mask]
            for _ in range(B):
                rp = np.corrcoef(scm, RNG.permutation(pnm))[0, 1]
                if abs(rp) >= obs:
                    cnt += 1
            pp = (cnt + 1) / (B + 1)
        else:
            r, pp = float("nan"), float("nan")
        print(f"  {s:10s} {net.mean():9.4f} {fair:15.4f} {edge:14.4f} "
              f"{r:15.3f} {pp:7.3f}")

    # ── EPOCH CONTROL ──
    print("\n" + "=" * 80)
    print(f"EPOCH CONTROL — BUGGY (<{CLEAN_CUTOFF}) vs CLEAN (≥{CLEAN_CUTOFF})")
    print("=" * 80)
    print(f"  {'symbol':10s} {'n_buggy':>7s} {'EVnet_b':>8s} {'n_clean':>7s} "
          f"{'EVnet_c':>8s} {'CI95_clean':>20s} {'clean_excl0':>11s}")
    for s in syms + ["ALL"]:
        cs = allt if s == "ALL" else by_sym[s]
        b = np.array([net_pnl(t) for t in cs if t["open"] < cutoff])
        c = np.array([net_pnl(t) for t in cs if t["open"] >= cutoff])
        evb = b.mean() if len(b) else float("nan")
        if len(c) >= 5:
            mc, loc, hic = boot_ci(c)
            ci = f"[{loc:7.4f},{hic:7.4f}]"
            excl = "POS" if loc > 0 else ("NEG" if hic < 0 else "spans0")
        else:
            mc, ci, excl = (c.mean() if len(c) else float("nan")), "(n<5)", "—"
        small = "  ⚠small-n" if 0 < len(c) < 30 else ""
        print(f"  {s:10s} {len(b):7d} {evb:8.4f} {len(c):7d} {mc:8.4f} {ci:>20s} {excl:>11s}{small}")

    print("\nLegend: excl0=POS → CI95 entirely above zero (real positive net edge).")
    print("        REPLICA=YES → positive net EV AND positive in BOTH chronological halves.")


if __name__ == "__main__":
    main()
