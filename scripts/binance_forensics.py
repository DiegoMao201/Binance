#!/usr/bin/env python3
"""
Binance SPOT bot — full forensic battery (READ-ONLY).

Same rigor applied to the Deriv bot that returned SIN_EDGE:
  PASO 1: NET EV + CI95 (bootstrap 10k) + max DD + Sharpe + %never-green, per pair + aggregate
  PASO 2: train/test frozen, BOTH directions (the test that killed Deriv)
  PASO 3: signal-vs-random (score->pnl correlation + permutation p-value)
  PASO 4: regime / temporal robustness / capacity-slippage

CRITICAL fee handling (Binance spot):
  - Local fees_usdt is undercounted -> recomputed NET from scratch.
  - NET_i = (exit-entry)*amount  -  (entry+exit)*amount*fee_rate_per_side
  - SPOT => NO funding rate. Slippage already in fill prices (slippage_pct ~0).
  - Reported under two fee scenarios: 0.10%/side (standard taker) and 0.075%/side (BNB).

No bot state is touched. Reads a local snapshot only.
"""
import json
import math
import random
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

random.seed(1337)

SNAPSHOT = "logs/binance_closed_trades_snapshot.json"
BOOT_N = 10_000
FEE_STD = 0.0010   # 0.10% per side, standard spot taker
FEE_BNB = 0.00075  # 0.075% per side with BNB discount
HEADLINE_FEE = FEE_STD  # conservative/honest headline

# ----------------------------------------------------------------------------- helpers

def parse_ts(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.fromisoformat(s.split(".")[0] + "+00:00")
        except Exception:
            return None


def net_pnl(t, fee_rate):
    """Recompute NET pnl from fill prices independently of local accounting."""
    try:
        entry = float(t["entry_price"]); exitp = float(t["exit_price"]); amt = float(t["amount"])
    except (KeyError, TypeError, ValueError):
        return None
    gross = (exitp - entry) * amt  # spot long only (side='buy')
    fees = (entry + exitp) * amt * fee_rate
    return gross - fees


def boot_ci(vals, n=BOOT_N, alpha=0.05):
    if len(vals) < 2:
        return (float("nan"), float("nan"))
    k = len(vals)
    means = []
    for _ in range(n):
        s = 0.0
        for _ in range(k):
            s += vals[random.randrange(k)]
        means.append(s / k)
    means.sort()
    lo = means[int((alpha / 2) * n)]
    hi = means[int((1 - alpha / 2) * n)]
    return (lo, hi)


def max_drawdown(pnls):
    eq = 0.0; peak = 0.0; mdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def sharpe(pnls):
    if len(pnls) < 2:
        return float("nan")
    m = st.mean(pnls); s = st.pstdev(pnls)
    return (m / s) * math.sqrt(len(pnls)) if s > 0 else float("nan")


def pct_never_green(trades):
    n = len(trades)
    if not n:
        return float("nan")
    ng = sum(1 for t in trades if float(t.get("mfe_pct") or 0) <= 0)
    return ng / n


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = st.mean(xs); my = st.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx > 0 and dy > 0 else float("nan")


def perm_pvalue(xs, ys, n=10_000):
    """Two-sided permutation p-value for correlation(score, pnl)."""
    obs = abs(pearson(xs, ys))
    if math.isnan(obs):
        return float("nan")
    ys2 = list(ys)
    cnt = 0
    for _ in range(n):
        random.shuffle(ys2)
        if abs(pearson(xs, ys2)) >= obs:
            cnt += 1
    return (cnt + 1) / (n + 1)


def verdict(lo, hi, n, min_n=20):
    if n < min_n:
        return "INSUFICIENTES_DATOS"
    if math.isnan(lo):
        return "INSUFICIENTES_DATOS"
    if lo > 0:
        return "EDGE_NETO_POSITIVO"
    return "SIN_EDGE_NETO"


# ----------------------------------------------------------------------------- load
data = json.load(open(SNAPSHOT))
for t in data:
    t["_dt"] = parse_ts(t.get("opened_at"))
data = [t for t in data if t["_dt"] is not None]
data.sort(key=lambda t: t["_dt"])

print("=" * 78)
print("BINANCE SPOT BOT — FORENSE READ-ONLY")
print("=" * 78)
print(f"n_trades      : {len(data)}")
print(f"rango         : {data[0]['_dt'].date()} -> {data[-1]['_dt'].date()} "
      f"({(data[-1]['_dt']-data[0]['_dt']).days} dias)")
print(f"mercado       : SPOT (sin funding rate)")
print(f"timeframe     : 5m | estrategia: RSI/EMA escenarios A/B (mean-reversion)")
sides = set(t.get("side") for t in data)
print(f"sides         : {sides}")
print(f"fee headline  : {HEADLINE_FEE*100:.3f}%/lado (taker spot estandar)")

# =================================================================== PASO 1
print("\n" + "=" * 78)
print("PASO 1 — EV NETO + CI95 (bootstrap 10k) por par + agregado")
print("=" * 78)

by_sym = defaultdict(list)
for t in data:
    by_sym[t["symbol"]].append(t)

hdr = f"{'par':12s} {'n':>4s} {'WR%':>6s} {'gross':>8s} {'NET@.10':>9s} {'mean':>8s} {'CI95_lo':>9s} {'CI95_hi':>9s} {'maxDD':>8s} {'Shrp':>6s} {'noGrn':>6s}  veredicto"
print(hdr)
print("-" * len(hdr))

def row(label, trades):
    net = [net_pnl(t, HEADLINE_FEE) for t in trades]
    net = [x for x in net if x is not None]
    gross = [net_pnl(t, 0.0) for t in trades if net_pnl(t, 0.0) is not None]
    n = len(net)
    if n == 0:
        return
    wr = sum(1 for x in net if x > 0) / n * 100
    mean = st.mean(net)
    lo, hi = boot_ci(net)
    mdd = max_drawdown(net)
    shp = sharpe(net)
    ng = pct_never_green(trades)
    vd = verdict(lo, hi, n)
    print(f"{label:12s} {n:4d} {wr:6.1f} {sum(gross):8.3f} {sum(net):9.3f} "
          f"{mean:8.4f} {lo:9.4f} {hi:9.4f} {mdd:8.3f} {shp:6.2f} {ng*100:5.0f}%  {vd}")

for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
    row(sym, by_sym[sym])
print("-" * len(hdr))
row("AGREGADO", data)

# fee sensitivity on aggregate
print("\n  Sensibilidad a fees (agregado):")
for label, fr in [("gross (0%)", 0.0), ("BNB 0.075%/lado", FEE_BNB), ("std 0.10%/lado", FEE_STD)]:
    vals = [net_pnl(t, fr) for t in data if net_pnl(t, fr) is not None]
    lo, hi = boot_ci(vals)
    print(f"    {label:18s} total={sum(vals):+8.3f}  mean={st.mean(vals):+.5f}  CI95=[{lo:+.4f},{hi:+.4f}]")

# =================================================================== PASO 2
print("\n" + "=" * 78)
print("PASO 2 — TRAIN/TEST CONGELADO, AMBOS SENTIDOS (filtro derivado solo en train)")
print("=" * 78)
print("Filtro candidato: entry_setup_score >= thr  (la senal propia del bot).")
print("Se optimiza thr en TRAIN (max NET medio), se congela, se aplica ciego en TEST.\n")

def with_score(trades):
    out = []
    for t in trades:
        s = t.get("entry_setup_score")
        if s is None:
            continue
        net = net_pnl(t, HEADLINE_FEE)
        if net is None:
            continue
        out.append((float(s), net))
    return out

GRID = [-999] + [round(0.50 + 0.02 * i, 3) for i in range(0, 26)]  # 0.50..1.00

def grid_best(pairs, min_retain=0.30):
    best = None
    n = len(pairs)
    for thr in GRID:
        kept = [p for p in pairs if p[0] >= thr]
        if len(kept) < max(8, min_retain * n):
            continue
        m = st.mean([p[1] for p in kept])
        if best is None or m > best[1]:
            best = (thr, m, len(kept))
    return best  # (thr, train_mean, n_kept)

def apply_thr(pairs, thr):
    return [p[1] for p in pairs if p[0] >= thr]

allp = with_score(data)
mid = len(allp) // 2
H1, H2 = allp[:mid], allp[mid:]

for name, train, test in [("A: H1->H2", H1, H2), ("B: H2->H1", H2, H1)]:
    gb = grid_best(train)
    if gb is None:
        print(f"  {name}: sin combo valido en train")
        continue
    thr, tr_mean, n_kept = gb
    test_vals = apply_thr(test, thr)
    if len(test_vals) < 8:
        print(f"  {name}: thr={thr} train_mean={tr_mean:+.4f} -> TEST n={len(test_vals)} INSUF.")
        continue
    lo, hi = boot_ci(test_vals)
    vd = "EDGE" if lo > 0 else "SIN_EDGE"
    print(f"  {name}: thr*={thr}  train_mean={tr_mean:+.4f} (n_kept={n_kept})  "
          f"-> TEST n={len(test_vals)} mean={st.mean(test_vals):+.4f} "
          f"total={sum(test_vals):+.3f} CI95=[{lo:+.4f},{hi:+.4f}]  {vd}")

# pooled OOS
pooled = []
for name, train, test in [("A", H1, H2), ("B", H2, H1)]:
    gb = grid_best(train)
    if gb:
        pooled += apply_thr(test, gb[0])
if pooled:
    lo, hi = boot_ci(pooled)
    print(f"\n  POOLED OOS: n={len(pooled)} mean={st.mean(pooled):+.4f} total={sum(pooled):+.3f} "
          f"CI95=[{lo:+.4f},{hi:+.4f}]  -> {'EDGE' if lo>0 else 'SIN_EDGE'}")

# =================================================================== PASO 3
print("\n" + "=" * 78)
print("PASO 3 — SENAL vs AZAR")
print("=" * 78)
scores = [float(t["entry_setup_score"]) for t in data if t.get("entry_setup_score") is not None
          and net_pnl(t, HEADLINE_FEE) is not None]
nets = [net_pnl(t, HEADLINE_FEE) for t in data if t.get("entry_setup_score") is not None
        and net_pnl(t, HEADLINE_FEE) is not None]
r = pearson(scores, nets)
p = perm_pvalue(scores, nets)
print(f"  corr(entry_setup_score, NET_pnl) = {r:+.3f}   permutation p = {p:.4f}")
print(f"  -> {'la senal informa el resultado' if (not math.isnan(p) and p < 0.05) else 'la senal NO bate al azar (p>=0.05)'}")

# also ai_confidence
ac = [(float(t['ai_confidence']), net_pnl(t, HEADLINE_FEE)) for t in data
      if t.get('ai_confidence') is not None and net_pnl(t, HEADLINE_FEE) is not None]
if len(ac) > 3:
    r2 = pearson([a for a, _ in ac], [b for _, b in ac])
    print(f"  corr(ai_confidence, NET_pnl)     = {r2:+.3f}")

# null reference: random entry on spot zero-drift ~ -fees. Net EV CI vs 0 is the random test.
allnet = [net_pnl(t, HEADLINE_FEE) for t in data if net_pnl(t, HEADLINE_FEE) is not None]
lo, hi = boot_ci(allnet)
print(f"\n  Null 'entrada aleatoria' en spot ~ EV de -fees (<0). "
      f"El bot NET CI95=[{lo:+.4f},{hi:+.4f}] {'BATE' if lo>0 else 'NO BATE'} al azar.")

# =================================================================== PASO 4
print("\n" + "=" * 78)
print("PASO 4 — REGIMEN / TEMPORAL / CAPACIDAD")
print("=" * 78)

# regime (field entry_regime)
by_reg = defaultdict(list)
for t in data:
    by_reg[t.get("entry_regime", "?")].append(net_pnl(t, HEADLINE_FEE))
print("  EV NETO por regimen (entry_regime):")
for reg, vals in sorted(by_reg.items(), key=lambda kv: -len(kv[1])):
    vals = [v for v in vals if v is not None]
    if not vals:
        continue
    lo, hi = boot_ci(vals)
    print(f"    {reg:10s} n={len(vals):3d} mean={st.mean(vals):+.4f} total={sum(vals):+.3f} CI95=[{lo:+.4f},{hi:+.4f}]")

# scenario A/B
by_sc = defaultdict(list)
for t in data:
    by_sc[t.get("scenario", "?")].append(net_pnl(t, HEADLINE_FEE))
print("\n  EV NETO por escenario:")
for sc, vals in sorted(by_sc.items()):
    vals = [v for v in vals if v is not None]
    if not vals:
        continue
    lo, hi = boot_ci(vals)
    print(f"    {sc:10s} n={len(vals):3d} mean={st.mean(vals):+.4f} total={sum(vals):+.3f} CI95=[{lo:+.4f},{hi:+.4f}]")

# temporal: by week
by_week = defaultdict(list)
for t in data:
    wk = t["_dt"].isocalendar()[1]
    by_week[wk].append(net_pnl(t, HEADLINE_FEE))
print("\n  EV NETO por semana ISO (robustez temporal):")
for wk in sorted(by_week):
    vals = [v for v in by_week[wk] if v is not None]
    print(f"    semana {wk}: n={len(vals):3d} total={sum(vals):+.3f} mean={st.mean(vals):+.4f}")

# capacity / slippage
slips = [float(t.get("slippage_pct") or 0) for t in data]
print(f"\n  Slippage: mean={st.mean(slips)*100:.4f}% max={max(slips)*100:.4f}% "
      f"-> {'irrelevante a este tamano' if st.mean(slips) < 0.001 else 'relevante'}")
print(f"  Notional medio ${st.mean([float(t.get('notional_usdt') or 0) for t in data]):.2f} "
      f"-> capacidad: micro, sin impacto de mercado.")

print("\n" + "=" * 78)
print("FIN FORENSE")
print("=" * 78)
