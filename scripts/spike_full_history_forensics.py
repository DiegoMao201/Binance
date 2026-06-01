#!/usr/bin/env python3
"""Full-history Deriv BOOM/CRASH forensic analysis (READ-ONLY, no bot code).

Sources (all on-disk archives merged + de-duplicated — PostgreSQL bot tables are
truncated on every sample reset, so disk archives are the real 10-day history):

  SPIKES    : every */deriv_spike_events.json   (dedupe by symbol+ts)
              fields used: ts, symbol, ticks_since_last_spike
  CONTRACTS : every */deriv_closed_contracts.json (dedupe by contract_id)
              fields used: symbol, side, opened_at_ts, closed_at_ts, exit_reason,
                           realized_pnl_usdt, max_pnl_alcanzado, ticks_held,
                           max_hold_seconds

Delivers:
  PASO 0  inventory: n_spikes / n_trades per symbol + date ranges
  PASO 1  inter-spike hazard + KS-exponential + SPLIT-HALF replication +
          expected-wait vs effective max_hold (reachability)
  PASO 2  entry-gap forensics: gap (ticks-since-last-spike) of winners vs losers,
          gap_floor per symbol, win-rate by gap bin
  TABLE   per-symbol verdict TRADEABLE_POR_TIMING / MEMORYLESS_NO_TIMING / DESCARTAR
"""
from __future__ import annotations

import glob
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np

LOGDIR = os.getenv("LOGDIR", "/data/deriv-logs")

# Exit-reason → win/loss classification (per user's framing).
WIN_REASONS = {
    "spike_tp", "spike_capture", "ratchet_hit",
    "tier_tp_window", "post_entry_small_spike_degraded",
}
LOSS_REASONS = {
    "broker_sl_hit", "spike_timeout", "timeout_max", "zero_peak_exit",
    "post_entry_no_confirmation", "unknown_pnl_-1.00",
}
# timeout_multispike and tier_staircase_* judged by realized pnl sign.


def is_win_reason(reason: str) -> bool:
    if reason in WIN_REASONS:
        return True
    if reason.startswith("tier_staircase_") or reason.startswith("tier_tp_window"):
        return True
    return False


def is_loss_reason(reason: str) -> bool:
    return reason in LOSS_REASONS


# ──────────────────────────────────────────────────────────────────────────
# Loaders (merge + dedupe across all archives)
# ──────────────────────────────────────────────────────────────────────────
def load_spikes() -> dict[str, list[dict[str, float]]]:
    files = sorted(glob.glob(f"{LOGDIR}/**/deriv_spike_events.json", recursive=True))
    files += sorted(glob.glob(f"{LOGDIR}/deriv_spike_events.json"))
    seen: set[tuple] = set()
    by_sym: dict[str, list[dict[str, float]]] = defaultdict(list)
    n_files = 0
    for f in files:
        try:
            data = json.load(open(f))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        n_files += 1
        for r in data:
            if not isinstance(r, dict):
                continue
            sym = r.get("symbol")
            ts = r.get("ts")
            if sym is None or ts is None:
                continue
            key = (sym, round(float(ts), 2))
            if key in seen:
                continue
            seen.add(key)
            by_sym[sym].append(
                {
                    "ts": float(ts),
                    "gap": float(r.get("ticks_since_last_spike") or 0.0),
                }
            )
    for s in by_sym:
        by_sym[s].sort(key=lambda r: r["ts"])
    print(f"[spikes] merged {n_files} files → "
          f"{sum(len(v) for v in by_sym.values())} unique events, {len(by_sym)} symbols")
    return by_sym


def load_contracts() -> dict[str, list[dict[str, Any]]]:
    files = sorted(glob.glob(f"{LOGDIR}/**/deriv_closed_contracts.json", recursive=True))
    files += sorted(glob.glob(f"{LOGDIR}/*deriv_closed_contracts*.json"))
    files += sorted(glob.glob(f"{LOGDIR}/archive_closed_*.json"))
    files += sorted(glob.glob(f"{LOGDIR}/*_closed.json"))
    seen: set[Any] = set()
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    n_files = 0
    for f in sorted(set(files)):
        try:
            data = json.load(open(f))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        n_files += 1
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
            by_sym[sym].append(
                {
                    "open": float(op),
                    "close": float(r.get("closed_at_ts") or op),
                    "exit": str(r.get("exit_reason") or "unknown"),
                    "pnl": float(r.get("realized_pnl_usdt") or 0.0),
                    "peak": float(r.get("max_pnl_alcanzado") or 0.0),
                    "ticks_held": float(r.get("ticks_held") or 0.0),
                    "max_hold_s": float(r.get("max_hold_seconds") or 0.0),
                    "side": r.get("side"),
                }
            )
    for s in by_sym:
        by_sym[s].sort(key=lambda r: r["open"])
    print(f"[trades] merged {n_files} files → "
          f"{sum(len(v) for v in by_sym.values())} unique contracts, {len(by_sym)} symbols")
    return by_sym


# ──────────────────────────────────────────────────────────────────────────
# Stats helpers
# ──────────────────────────────────────────────────────────────────────────
def ks_vs_exponential(iv: np.ndarray) -> tuple[float, float, float]:
    iv = np.sort(iv)
    n = len(iv)
    mean = float(np.mean(iv)) if n else 0.0
    if mean <= 0:
        return 0.0, 1.0, 0.0
    lam = 1.0 / mean
    f_th = 1.0 - np.exp(-lam * iv)
    f_hi = np.arange(1, n + 1) / n
    f_lo = np.arange(0, n) / n
    d = float(np.max(np.maximum(np.abs(f_hi - f_th), np.abs(f_th - f_lo))))
    en = math.sqrt(n)
    x = (en + 0.12 + 0.11 / en) * d
    if x < 1e-3:
        p = 1.0
    else:
        p = max(0.0, min(1.0, 2.0 * sum(((-1) ** (k - 1)) * math.exp(-2 * k * k * x * x)
                                         for k in range(1, 101))))
    return d, p, lam


def hazard_curve(iv: np.ndarray, bin_w: int = 50) -> list[dict[str, float]]:
    iv = np.sort(iv)
    if len(iv) == 0:
        return []
    max_gap = float(np.percentile(iv, 98)) + bin_w
    rows = []
    a = 0.0
    while a < max_gap:
        b = a + bin_w
        at_risk = int(np.sum(iv >= a))
        d = int(np.sum((iv >= a) & (iv < b)))
        if at_risk > 0:
            rows.append({"lo": a, "hi": b, "at_risk": at_risk,
                         "events": d, "h": (d / at_risk) / bin_w})
        a = b
    return rows


def peak_bin(rows: list[dict[str, float]], lam: float, p75: float, n: int) -> dict[str, Any]:
    floor = max(15, int(0.18 * n))
    cand = [r for r in rows if r["hi"] <= p75 * 1.05 and r["at_risk"] >= floor]
    if not cand:
        cand = [r for r in rows if r["at_risk"] >= floor] or rows
    pk = max(cand, key=lambda r: r["h"])
    return {"lo": pk["lo"], "hi": pk["hi"], "ratio": pk["h"] / lam if lam > 0 else 0.0}


# ──────────────────────────────────────────────────────────────────────────
# PASO 1
# ──────────────────────────────────────────────────────────────────────────
def analyze_spikes(sym: str, events: list[dict[str, float]], tick_rate: float,
                   max_hold_s: float) -> dict[str, Any]:
    iv = np.array([e["gap"] for e in events if e["gap"] >= 1], dtype=float)
    if len(iv) < 12:
        return {"symbol": sym, "n": len(iv), "insufficient": True}
    d, p, lam = ks_vs_exponential(iv)
    rows = hazard_curve(iv)
    p75 = float(np.percentile(iv, 75))
    pk = peak_bin(rows, lam, p75, len(iv))

    # SPLIT-HALF (chronological by event order, already ts-sorted)
    mid = len(events) // 2
    iv1 = np.array([e["gap"] for e in events[:mid] if e["gap"] >= 1], dtype=float)
    iv2 = np.array([e["gap"] for e in events[mid:] if e["gap"] >= 1], dtype=float)
    _, _, lam1 = ks_vs_exponential(iv1)
    _, _, lam2 = ks_vs_exponential(iv2)
    pk1 = peak_bin(hazard_curve(iv1), lam1, float(np.percentile(iv1, 75)), len(iv1)) if len(iv1) >= 8 else None
    pk2 = peak_bin(hazard_curve(iv2), lam2, float(np.percentile(iv2, 75)), len(iv2)) if len(iv2) >= 8 else None
    replica = False
    if pk1 and pk2:
        same_bin = abs(pk1["lo"] - pk2["lo"]) <= 50
        both_elev = pk1["ratio"] >= 1.3 and pk2["ratio"] >= 1.3
        replica = bool(same_bin and both_elev)

    # Reachability: expected wait from random entry ≈ mean interval (ticks).
    mean_iv = float(np.mean(iv))
    max_hold_ticks = max_hold_s * tick_rate if tick_rate > 0 else 0.0
    reachable = max_hold_ticks >= mean_iv if max_hold_ticks > 0 else None

    return {
        "symbol": sym, "n": int(len(iv)), "mean": mean_iv,
        "p25": float(np.percentile(iv, 25)), "p50": float(np.percentile(iv, 50)),
        "p75": p75, "ks_p": p, "lam": lam,
        "peak_lo": pk["lo"], "peak_hi": pk["hi"], "peak_ratio": pk["ratio"],
        "pk1": pk1, "pk2": pk2, "replica": replica,
        "max_hold_ticks": max_hold_ticks, "reachable": reachable,
        "tick_rate": tick_rate, "insufficient": False,
    }


# ──────────────────────────────────────────────────────────────────────────
# PASO 2  entry-gap forensics
# ──────────────────────────────────────────────────────────────────────────
def entry_gaps(sym: str, contracts: list[dict[str, Any]],
               spikes: list[dict[str, float]], tick_rate: float,
               max_match_s: float = 3600.0) -> dict[str, Any]:
    """Entry-gap forensics.

    A trade is only matched to a preceding spike if that spike is within
    `max_match_s` seconds (default 1h). Matches beyond that are cross-session
    contamination (archive timeline has reset gaps) and are DISCARDED rather
    than producing nonsensical multi-hour gaps.
    """
    sp_ts = np.array([s["ts"] for s in spikes], dtype=float)
    sp_ts.sort()
    winners, losers = [], []
    rows = []
    discarded = 0
    for c in contracts:
        op = c["open"]
        idx = np.searchsorted(sp_ts, op, side="right") - 1
        if idx < 0:
            discarded += 1
            continue
        gap_s = op - sp_ts[idx]
        if gap_s > max_match_s or gap_s < 0:
            discarded += 1
            continue
        gap_ticks = gap_s * tick_rate
        win = c["pnl"] > 0 or is_win_reason(c["exit"])
        loss = c["pnl"] < 0 or is_loss_reason(c["exit"])
        rec = {"gap_ticks": gap_ticks, "gap_s": gap_s, "pnl": c["pnl"],
               "exit": c["exit"], "win": win}
        rows.append(rec)
        if win and not (c["pnl"] < 0):
            winners.append(gap_ticks)
        elif loss and not (c["pnl"] > 0):
            losers.append(gap_ticks)

    w = np.array(winners, dtype=float)
    l = np.array(losers, dtype=float)

    def q(a, p):
        return float(np.percentile(a, p)) if len(a) else float("nan")

    # Win-rate by gap bin (100t) to locate a gap_floor.
    floor = None
    wr_by_bin = []
    if rows:
        gaps = np.array([r["gap_ticks"] for r in rows])
        wins = np.array([1 if r["win"] else 0 for r in rows])
        edges = np.arange(0, np.percentile(gaps, 95) + 100, 100)
        for i in range(len(edges) - 1):
            m = (gaps >= edges[i]) & (gaps < edges[i + 1])
            nt = int(m.sum())
            if nt >= 8:
                wr = float(wins[m].mean())
                wr_by_bin.append((edges[i], edges[i + 1], nt, wr))
        base_wr = float(wins.mean())
        # gap_floor = top of the leading run of below-baseline bins
        for lo, hi, nt, wr in wr_by_bin:
            if wr < base_wr - 0.05:
                floor = hi
            else:
                break

    return {
        "symbol": sym, "n_trades": len(rows),
        "n_win": len(winners), "n_loss": len(losers), "discarded": discarded,
        "win_gap_p25": q(w, 25), "win_gap_p50": q(w, 50), "win_gap_p75": q(w, 75),
        "loss_gap_p25": q(l, 25), "loss_gap_p50": q(l, 50), "loss_gap_p75": q(l, 75),
        "win_gap_mean": float(np.mean(w)) if len(w) else float("nan"),
        "loss_gap_mean": float(np.mean(l)) if len(l) else float("nan"),
        "gap_floor": floor, "wr_by_bin": wr_by_bin,
        "base_wr": float(np.mean([1 if r["win"] else 0 for r in rows])) if rows else float("nan"),
    }


# ──────────────────────────────────────────────────────────────────────────
def fdt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def main() -> None:
    spikes = load_spikes()
    trades = load_contracts()

    # Tick rate per symbol from contracts: ticks_held / duration_seconds.
    tick_rate: dict[str, float] = {}
    max_hold: dict[str, float] = {}
    for s, cs in trades.items():
        rates = [c["ticks_held"] / (c["close"] - c["open"])
                 for c in cs if c["close"] > c["open"] and c["ticks_held"] > 0]
        tick_rate[s] = float(np.median(rates)) if rates else 1.0
        mh = [c["max_hold_s"] for c in cs if c["max_hold_s"] > 0]
        max_hold[s] = float(np.median(mh)) if mh else 0.0

    syms = sorted(set(spikes) | set(trades))

    print("\n" + "=" * 78)
    print("PASO 0 — INVENTORY (full merged archive history)")
    print("=" * 78)
    print(f"{'symbol':10s} {'n_spikes':>8s} {'n_trades':>8s} {'spike_range':>26s} "
          f"{'tick/s':>6s} {'hold_s':>7s}")
    for s in syms:
        sp = spikes.get(s, [])
        tr = trades.get(s, [])
        sr = f"{fdt(sp[0]['ts'])}→{fdt(sp[-1]['ts'])}" if sp else "—"
        print(f"{s:10s} {len(sp):8d} {len(tr):8d} {sr:>26s} "
              f"{tick_rate.get(s,0):6.2f} {max_hold.get(s,0):7.0f}")

    print("\n" + "=" * 78)
    print("PASO 1 — INTER-SPIKE HAZARD + SPLIT-HALF + REACHABILITY")
    print("=" * 78)
    p1 = {}
    hdr = (f"{'symbol':10s} {'n':>5s} {'mean':>6s} {'p25':>5s} {'p50':>5s} {'p75':>5s} "
           f"{'KS_p':>6s} {'peak':>9s} {'pk1':>6s} {'pk2':>6s} {'REPLICA':>8s} "
           f"{'hold_t':>7s} {'reach':>6s}")
    print(hdr); print("-" * len(hdr))
    for s in syms:
        ev = spikes.get(s, [])
        r = analyze_spikes(s, ev, tick_rate.get(s, 1.0), max_hold.get(s, 0.0))
        p1[s] = r
        if r.get("insufficient"):
            print(f"{s:10s} {r['n']:5d}  (insufficient spikes)"); continue
        pk1 = f"{r['pk1']['lo']:.0f}" if r['pk1'] else "—"
        pk2 = f"{r['pk2']['lo']:.0f}" if r['pk2'] else "—"
        rep = "YES" if r["replica"] else "no"
        reach = "—" if r["reachable"] is None else ("YES" if r["reachable"] else "NO")
        print(f"{s:10s} {r['n']:5d} {r['mean']:6.0f} {r['p25']:5.0f} {r['p50']:5.0f} "
              f"{r['p75']:5.0f} {r['ks_p']:6.3f} {r['peak_lo']:4.0f}-{r['peak_hi']:<4.0f} "
              f"{pk1:>6s} {pk2:>6s} {rep:>8s} {r['max_hold_ticks']:7.0f} {reach:>6s}")

    print("\n" + "=" * 78)
    print("PASO 2 — ENTRY-GAP FORENSICS (winners vs losers, ticks since last spike)")
    print("=" * 78)
    p2 = {}
    hdr2 = (f"{'symbol':10s} {'matched':>7s} {'disc':>5s} {'win':>4s} {'los':>4s} {'baseWR':>6s} "
            f"{'WIN_gap p25/50/75':>20s} {'LOSS_gap p25/50/75':>20s} {'gap_floor':>9s}")
    print(hdr2); print("-" * len(hdr2))
    for s in syms:
        if not trades.get(s) or not spikes.get(s):
            continue
        r = entry_gaps(s, trades[s], spikes[s], tick_rate.get(s, 1.0))
        p2[s] = r
        wg = f"{r['win_gap_p25']:.0f}/{r['win_gap_p50']:.0f}/{r['win_gap_p75']:.0f}"
        lg = f"{r['loss_gap_p25']:.0f}/{r['loss_gap_p50']:.0f}/{r['loss_gap_p75']:.0f}"
        gf = f"{r['gap_floor']:.0f}" if r['gap_floor'] else "—"
        print(f"{s:10s} {r['n_trades']:7d} {r['discarded']:5d} {r['n_win']:4d} {r['n_loss']:4d} "
              f"{r['base_wr']:6.2f} {wg:>20s} {lg:>20s} {gf:>9s}")

    # ── Final verdict table ──
    print("\n" + "=" * 78)
    print("DELIVERABLE — PER-SYMBOL VERDICT")
    print("=" * 78)
    hdrv = (f"{'symbol':10s} {'n_sp':>5s} {'n_tr':>5s} {'mean_iv':>7s} {'KS_p':>6s} "
            f"{'peak':>9s} {'REPL':>5s} {'reach':>6s} {'gap_floor':>9s} "
            f"{'win>loss_gap':>11s} {'VERDICT':>22s}")
    print(hdrv); print("-" * len(hdrv))
    for s in syms:
        a = p1.get(s, {})
        b = p2.get(s, {})
        if a.get("insufficient") or not a:
            continue
        win_gt_loss = (not math.isnan(b.get("win_gap_mean", float("nan")))
                       and not math.isnan(b.get("loss_gap_mean", float("nan")))
                       and b["win_gap_mean"] > b["loss_gap_mean"] * 1.10)
        # Verdict logic
        if a["replica"] and a.get("reachable") and win_gt_loss:
            verdict = "TRADEABLE_POR_TIMING"
        elif not a["replica"] and a.get("reachable") is False:
            verdict = "DESCARTAR"
        elif not a["replica"]:
            verdict = "MEMORYLESS_NO_TIMING"
        else:
            verdict = "REVIEW (replica, weak gap)"
        peak = f"{a['peak_lo']:.0f}-{a['peak_hi']:.0f}"
        repl = "YES" if a["replica"] else "no"
        reach = "—" if a.get("reachable") is None else ("Y" if a["reachable"] else "N")
        gf = f"{b['gap_floor']:.0f}" if b.get("gap_floor") else "—"
        wgl = "YES" if win_gt_loss else "no"
        print(f"{s:10s} {a['n']:5d} {b.get('n_trades',0):5d} {a['mean']:7.0f} "
              f"{a['ks_p']:6.3f} {peak:>9s} {repl:>5s} {reach:>6s} {gf:>9s} "
              f"{wgl:>11s} {verdict:>22s}")

    print("\nNotes:")
    print("  REPLICA   = hazard peak lands in same ±50t bin AND ≥1.3× in BOTH halves.")
    print("  reach     = expected random-entry wait (mean interval) ≤ max_hold in ticks.")
    print("  gap_floor = entry gap (ticks) below which win-rate drops >5pp under baseline.")
    print("  win>loss  = winners' mean entry-gap > losers' by ≥10% (late entry edge).")


if __name__ == "__main__":
    main()
