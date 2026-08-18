#!/usr/bin/env python3
"""
CASCADE-REVERSAL THESIS — histórico backtest
============================================
Pregunta: tras una cascada de ventas forzadas, ¿el precio rebota lo suficiente
para que una orden pasiva colocada por debajo gane dinero?

Sigue exactamente el protocolo de PROMPT_TEST_TESIS.md.

Uso:
    python3 scripts/thesis_backtest.py --dir /data/thesis_klines [--output results/]

Requiere: pandas numpy scipy (instala con pip3 install pandas numpy scipy)
"""

import argparse
import gc
import json
import os
import random
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    from scipy import stats
except ImportError:
    print("Instalando pandas numpy scipy...")
    os.system(f"{sys.executable} -m pip install pandas numpy scipy -q")
    import numpy as np
    import pandas as pd
    from scipy import stats

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "BNBUSDT",
]

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count",
    "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
]

ENTRY_DISTANCES_BPS = [25, 50, 100, 200]
MARKOUT_HORIZONS_MIN = [5, 15, 30, 60, 120]
FILL_WINDOW_MIN = 30       # max minutes to wait for fill after onset
MERGE_WINDOW_MIN = 15      # merge consecutive cascades within this window
PLACEBO_N = 500

# All time constants in MILLISECONDS to match pandas 3.0 datetime64[ms, UTC] storage.
# Timestamp.value gives nanoseconds; .values.astype(int64) on datetime64[ms] gives milliseconds.
# We use milliseconds throughout to keep all comparisons in the same unit.
FILL_WINDOW_MS  = FILL_WINDOW_MIN  * 60 * 1_000
MARKOUT_END_MS  = (FILL_WINDOW_MIN + max(MARKOUT_HORIZONS_MIN) + 5) * 60 * 1_000
HORIZON_MS      = [h * 60 * 1_000 for h in MARKOUT_HORIZONS_MIN]
HORIZON_SLACK   = 2 * 60 * 1_000   # ±2 min tolerance for markout candle (ms)

HYPOTHESES_FILE = "hypotheses.jsonl"


# ── Data loading ─────────────────────────────────────────────────────────────

def load_symbol_klines(klines_dir: Path, symbol: str, source: str) -> pd.DataFrame:
    """Load all monthly klines for one symbol from zip files."""
    sym_dir = klines_dir / source / symbol
    if not sym_dir.exists():
        return pd.DataFrame()

    dfs = []
    for zf in sorted(sym_dir.glob(f"{symbol}-1m-*.zip")):
        try:
            with zipfile.ZipFile(zf) as z:
                name = z.namelist()[0]
                with z.open(name) as f:
                    raw = pd.read_csv(f, header=None, names=KLINE_COLS, dtype=str)
                    # Binance changed format ~2022: newer files have a header row
                    try:
                        float(raw.iloc[0]["open_time"])
                    except (ValueError, TypeError):
                        raw = raw.iloc[1:].reset_index(drop=True)
                    numeric_cols = [
                        "open_time", "open", "high", "low", "close", "volume",
                        "close_time", "quote_volume", "count",
                        "taker_buy_base_volume", "taker_buy_quote_volume",
                    ]
                    df = raw.copy()
                    df[numeric_cols] = raw[numeric_cols].astype(float)
                    # Binance changed spot open_time to microseconds in 2025+.
                    # Detect: any value > 1e13 means µs (Jan 2025 in ms = 1.7e12; µs = 1.7e15).
                    if df["open_time"].iloc[0] > 1e13:
                        df["open_time"] = df["open_time"] / 1_000
                    dfs.append(df)
        except Exception as e:
            print(f"  WARN {zf.name}: {e}")

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    df[["open", "high", "low", "close", "quote_volume",
        "taker_buy_quote_volume"]] = df[
        ["open", "high", "low", "close", "quote_volume",
         "taker_buy_quote_volume"]].astype(float)
    return df


# ── Feature computation ───────────────────────────────────────────────────────

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute flow imbalance, intensity, ATR expansion, 1m return."""
    df = df.copy()

    df["flujo_neto_usd"] = 2 * df["taker_buy_quote_volume"] - df["quote_volume"]
    df["desequilibrio"] = df["flujo_neto_usd"] / df["quote_volume"].replace(0, np.nan)

    rolling_vol = df["quote_volume"].rolling(60, min_periods=10)
    df["vol_median_60"] = rolling_vol.median()
    df["intensidad"] = df["flujo_neto_usd"].abs() / df["vol_median_60"].replace(0, np.nan)

    df["range_frac"] = (df["high"] - df["low"]) / df["close"]
    df["atr_frac_60"] = df["range_frac"].rolling(60, min_periods=10).mean()
    df["expansion_rango"] = df["range_frac"] / df["atr_frac_60"].replace(0, np.nan)

    df["retorno_1m"] = df["close"] / df["open"] - 1

    return df


# ── Threshold calibration + pre-registration ─────────────────────────────────

def calibrate_theta(df: pd.DataFrame, symbol: str, percentile: float = 99.5) -> float:
    return float(np.nanpercentile(df["intensidad"].dropna(), percentile))


def preregister(hypotheses: list, symbol: str, theta: float, percentile: float = 99.5):
    """Write pre-registration entry BEFORE looking at cascade counts."""
    entry = {
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "threshold_name": "intensity_theta",
        "method": f"percentile_{percentile}",
        "value": theta,
        "fixed_thresholds": {
            "desequilibrio_long_liq": -0.30,
            "desequilibrio_short_liq": +0.30,
            "expansion_rango": 2.0,
        },
        "note": "Pre-registered before computing cascade counts. Do not adjust.",
    }
    hypotheses.append(entry)


# ── Cascade detection ─────────────────────────────────────────────────────────

def detect_cascades(df: pd.DataFrame, theta: float) -> pd.DataFrame:
    long_mask = (
        (df["desequilibrio"] < -0.30) &
        (df["intensidad"] > theta) &
        (df["expansion_rango"] > 2.0) &
        (df["retorno_1m"] < 0)
    )
    short_mask = (
        (df["desequilibrio"] > +0.30) &
        (df["intensidad"] > theta) &
        (df["expansion_rango"] > 2.0) &
        (df["retorno_1m"] > 0)
    )

    events = []
    for mask, direction in [(long_mask, "LONG_LIQ"), (short_mask, "SHORT_LIQ")]:
        candidates = df[mask][["ts", "close", "open", "high", "low", "intensidad"]].copy()
        candidates["direction"] = direction
        events.append(candidates)

    if not events:
        return pd.DataFrame()

    all_events = pd.concat(events).sort_values("ts")

    merged = []
    last_ts = {}
    for _, row in all_events.iterrows():
        d = row["direction"]
        if d in last_ts and (row["ts"] - last_ts[d]).total_seconds() < MERGE_WINDOW_MIN * 60:
            continue
        last_ts[d] = row["ts"]
        merged.append(row)

    return pd.DataFrame(merged).reset_index(drop=True)


# ── Entry simulation (numpy-based for speed and low memory) ──────────────────

def simulate_entry_np(
    fut_ts: np.ndarray,    # int64 ms timestamps (sorted) — datetime64[ms, UTC] storage
    fut_low: np.ndarray,
    fut_high: np.ndarray,
    spot_ts: np.ndarray,   # int64 ms timestamps (sorted)
    spot_close: np.ndarray,
    onset_ts_ms: int,      # milliseconds since epoch
    onset_price: float,
    direction: str,
) -> list:
    fill_end_ms    = onset_ts_ms + FILL_WINDOW_MS
    markout_end_ms = onset_ts_ms + MARKOUT_END_MS

    i_start = int(np.searchsorted(fut_ts, onset_ts_ms, side="right"))
    i_end   = int(np.searchsorted(fut_ts, fill_end_ms,  side="right"))

    si_end = int(np.searchsorted(spot_ts, markout_end_ms, side="right"))

    results = []
    for d_bps in ENTRY_DISTANCES_BPS:
        if direction == "LONG_LIQ":
            entry_price = onset_price * (1 - d_bps / 10_000)
        else:
            entry_price = onset_price * (1 + d_bps / 10_000)

        filled = False
        fill_ts_ms = 0

        if direction == "LONG_LIQ":
            for i in range(i_start, i_end):
                if fut_low[i] <= entry_price:
                    filled = True
                    fill_ts_ms = int(fut_ts[i])
                    break
        else:
            for i in range(i_start, i_end):
                if fut_high[i] >= entry_price:
                    filled = True
                    fill_ts_ms = int(fut_ts[i])
                    break

        markouts = {}
        if filled:
            for h_ms, h_min in zip(HORIZON_MS, MARKOUT_HORIZONS_MIN):
                target_ms = fill_ts_ms + h_ms
                lo_ms = target_ms - HORIZON_SLACK
                hi_ms = target_ms + HORIZON_SLACK
                j_lo = int(np.searchsorted(spot_ts, lo_ms,  side="left"))
                j_hi = int(np.searchsorted(spot_ts, hi_ms,  side="right"))
                j_hi = min(j_hi, si_end)
                if j_lo >= j_hi:
                    markouts[f"ret_{h_min}m"] = None
                else:
                    diffs = np.abs(spot_ts[j_lo:j_hi] - target_ms)
                    j_best = j_lo + int(np.argmin(diffs))
                    exit_price = float(spot_close[j_best])
                    if direction == "LONG_LIQ":
                        markouts[f"ret_{h_min}m"] = 10_000 * (exit_price / entry_price - 1)
                    else:
                        markouts[f"ret_{h_min}m"] = 10_000 * (entry_price / exit_price - 1)
        else:
            # NOT filled — count as 0, never discard (PROMPT_TEST_TESIS.md rule)
            for h_min in MARKOUT_HORIZONS_MIN:
                markouts[f"ret_{h_min}m"] = 0.0

        row_result = {
            "onset_ts": pd.Timestamp(onset_ts_ms, unit="ms", tz="UTC"),
            "direction": direction,
            "onset_price": onset_price,
            "entry_distance_bps": d_bps,
            "filled": filled,
        }
        row_result.update(markouts)
        results.append(row_result)

    return results


# ── BTC-driven exogenous filter ───────────────────────────────────────────────

def mark_exogenous(
    events_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    window_before: int = 5,
    window_after: int = 1,
) -> pd.DataFrame:
    """
    Mark events as exogenous if BTC moved sharply in [−5, +1] min.
    'Sharp' = abs(return) > 2× BTC rolling std(return, 60).
    Pass btc_df=pd.DataFrame() to label all events as endogenous.
    """
    if btc_df is None or btc_df.empty:
        events_df = events_df.copy()
        events_df["exogenous"] = False
        return events_df

    btc = btc_df[["ts", "open", "close"]].copy()
    btc["ret_1m"] = btc["close"] / btc["open"] - 1
    btc["ret_std_60"] = btc["ret_1m"].rolling(60, min_periods=10).std()
    btc = btc.set_index("ts").sort_index()

    exogenous = []
    for _, ev in events_df.iterrows():
        ts = ev["ts"]
        start = ts - pd.Timedelta(minutes=window_before)
        end   = ts + pd.Timedelta(minutes=window_after)
        btc_window = btc.loc[start:end]
        if btc_window.empty:
            exogenous.append(False)
            continue
        std_row = btc["ret_std_60"].asof(ts)
        std_val = float(std_row) if pd.notna(std_row) else np.nan
        max_ret = float(btc_window["ret_1m"].abs().max())
        exogenous.append(max_ret > 2 * std_val if not np.isnan(std_val) else False)

    events_df = events_df.copy()
    events_df["exogenous"] = exogenous
    return events_df


# ── Table formatting ──────────────────────────────────────────────────────────

def summarize(rows: list, label: str) -> None:
    if not rows:
        print(f"\n{label}: No data")
        return

    df = pd.DataFrame(rows)
    print(f"\n{'─'*80}")
    print(f"  {label}  (n_events = {df['onset_ts'].nunique()})")
    print(f"{'─'*80}")
    header = f"{'distancia':>12}  {'fill%':>6}  " + "  ".join(
        f"{'ret_'+str(h)+'m':>10}" for h in MARKOUT_HORIZONS_MIN
    )
    print(header)

    for d in ENTRY_DISTANCES_BPS:
        sub = df[df["entry_distance_bps"] == d]
        fill_pct = sub["filled"].mean() * 100
        parts = [f"{d:>12}bps  {fill_pct:>5.1f}%"]
        for h in MARKOUT_HORIZONS_MIN:
            col = f"ret_{h}m"
            vals = sub[col].dropna()
            if len(vals) > 0:
                m = vals.mean()
                sd = vals.std()
                parts.append(f"{m:>+7.1f}±{sd:.0f}")
            else:
                parts.append(f"{'N/A':>10}")
        print("  ".join(parts))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/data/thesis_klines", help="Klines directory")
    parser.add_argument("--output", default="results", help="Output directory")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS)
    args = parser.parse_args()

    klines_dir = Path(args.dir)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    hypotheses = []
    all_cascade_rows = []
    all_placebo_long_rows  = []   # direction-matched: BUY at random times
    all_placebo_short_rows = []   # direction-matched: SELL at random times
    symbol_stats = {}

    # Load BTC reference klines (raw only — mark_exogenous computes its own features)
    btc_fut = load_symbol_klines(klines_dir, "BTCUSDT", "futures")

    for symbol in args.symbols:
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")

        fut_df  = load_symbol_klines(klines_dir, symbol, "futures")
        spot_df = load_symbol_klines(klines_dir, symbol, "spot")

        if fut_df.empty:
            print(f"  No futures data — skipping")
            continue
        if spot_df.empty:
            print(f"  No spot data — using futures for markout")
            spot_df = fut_df[["ts", "close"]].copy()

        print(f"  Loaded: {len(fut_df):,} futures candles, {len(spot_df):,} spot candles")
        print(f"  Date range: {fut_df['ts'].min()} → {fut_df['ts'].max()}")

        fut_df = compute_features(fut_df)

        # STEP 1: Pre-register θ BEFORE detecting cascades
        theta = calibrate_theta(fut_df, symbol)
        preregister(hypotheses, symbol, theta)
        print(f"  θ (p99.5 intensity) = {theta:.4f}  [pre-registered]")

        # STEP 2: Detect cascades
        cascades = detect_cascades(fut_df, theta)
        n_long  = (cascades["direction"] == "LONG_LIQ").sum()  if not cascades.empty else 0
        n_short = (cascades["direction"] == "SHORT_LIQ").sum() if not cascades.empty else 0
        print(f"  Cascades: {n_long} LONG_LIQ + {n_short} SHORT_LIQ = {len(cascades)} total")

        # Exogenous marking: BTC drives the others; for BTC itself we skip
        # (loading ETH as reference would double peak RAM and BTC IS the macro index)
        if not cascades.empty:
            exo_ref = pd.DataFrame() if symbol == "BTCUSDT" else btc_fut
            cascades = mark_exogenous(cascades, exo_ref)

        # STEP 3: Slim DataFrames to only simulation-needed columns, then go numpy
        # This frees all feature columns (~60% of RAM) before the hot simulation loop
        fut_slim  = fut_df[["ts", "low", "high", "close"]].copy()
        spot_slim = spot_df[["ts", "close"]].copy()
        del fut_df, spot_df
        gc.collect()

        # pandas 3.0 stores datetime64[ms, UTC] → .astype(int64) gives milliseconds
        fut_ts_ms  = fut_slim["ts"].values.astype(np.int64)
        fut_low    = fut_slim["low"].values.astype(np.float64)
        fut_high   = fut_slim["high"].values.astype(np.float64)
        spot_ts_ms = spot_slim["ts"].values.astype(np.int64)
        spot_close = spot_slim["close"].values.astype(np.float64)

        # Eligible rows for placebo (exclude cascade timestamps)
        cascade_ts_set = set(cascades["ts"].values.tolist()) if not cascades.empty else set()
        eligible = fut_slim[~fut_slim["ts"].isin(cascade_ts_set) & fut_slim["ts"].notna()]

        # STEP 4: Simulate cascade entries (store intensidad for decile cut)
        cascade_rows = []
        for _, ev in cascades.iterrows():
            onset_ts_ms_val = int(ev["ts"].value) // 1_000_000
            rows = simulate_entry_np(
                fut_ts_ms, fut_low, fut_high,
                spot_ts_ms, spot_close,
                onset_ts_ms_val, float(ev["close"]), ev["direction"],
            )
            for r in rows:
                r["symbol"]     = symbol
                r["exogenous"]  = ev.get("exogenous", False)
                r["year"]       = ev["ts"].year
                r["intensidad"] = float(ev.get("intensidad", np.nan))
            cascade_rows.extend(rows)
        all_cascade_rows.extend(cascade_rows)

        # STEP 5: Direction-matched placebos — 500 random BUY + 500 random SELL
        # This is the decisive test: LONG_LIQ cascade vs random BUY,
        # and SHORT_LIQ cascade vs random SELL.
        def run_placebo(direction_fixed: str, target_list: list):
            indices = random.sample(range(len(eligible)), min(PLACEBO_N, len(eligible)))
            for idx in indices:
                pev = eligible.iloc[idx]
                onset_ms = int(pev["ts"].value) // 1_000_000
                rows = simulate_entry_np(
                    fut_ts_ms, fut_low, fut_high,
                    spot_ts_ms, spot_close,
                    onset_ms, float(pev["close"]), direction_fixed,
                )
                for r in rows:
                    r["symbol"] = symbol
                    r["year"]   = pev["ts"].year
                target_list.extend(rows)

        run_placebo("LONG_LIQ",  all_placebo_long_rows)
        run_placebo("SHORT_LIQ", all_placebo_short_rows)

        symbol_stats[symbol] = {
            "n_long_liq":  int(n_long),
            "n_short_liq": int(n_short),
            "theta":       float(theta),
            "date_range":  [str(fut_slim["ts"].min()), str(fut_slim["ts"].max())],
        }
        print(f"  Cascade entries: {len(cascade_rows)} | "
              f"Placebo LONG/SHORT: {PLACEBO_N}/{PLACEBO_N}")

        del fut_slim, spot_slim, fut_ts_ms, fut_low, fut_high, spot_ts_ms, spot_close
        gc.collect()

    # Write hypotheses.jsonl
    hyp_path = out_dir / HYPOTHESES_FILE
    with open(hyp_path, "w") as f:
        for h in hypotheses:
            f.write(json.dumps(h) + "\n")
    print(f"\nHypotheses written: {hyp_path}")

    # Write raw data (3 separate parquets for clean downstream analysis)
    cas_df   = pd.DataFrame(all_cascade_rows)   if all_cascade_rows   else pd.DataFrame()
    plac_long_df  = pd.DataFrame(all_placebo_long_rows)  if all_placebo_long_rows  else pd.DataFrame()
    plac_short_df = pd.DataFrame(all_placebo_short_rows) if all_placebo_short_rows else pd.DataFrame()

    if not cas_df.empty:
        cas_df.to_parquet(out_dir / "cascade_entries.parquet")
    if not plac_long_df.empty:
        plac_long_df.to_parquet(out_dir / "placebo_long_entries.parquet")
    if not plac_short_df.empty:
        plac_short_df.to_parquet(out_dir / "placebo_short_entries.parquet")

    # Pre-register intensity p90 threshold BEFORE looking at decile results
    if not cas_df.empty and "intensidad" in cas_df.columns:
        long_cas = cas_df[cas_df["direction"] == "LONG_LIQ"]["intensidad"].dropna()
        p90_threshold = float(np.nanpercentile(long_cas, 90)) if len(long_cas) > 0 else float("nan")
        hyp_decile = {
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "analysis": "intensity_decile_cut",
            "pre_registered_threshold": "p90 of LONG_LIQ intensidad across all symbols",
            "p90_value": p90_threshold,
            "note": "Pre-registered before stratifying. Top decile = intensidad >= this value.",
        }
        with open(hyp_path, "a") as f:
            f.write(json.dumps(hyp_decile) + "\n")
        print(f"\nPre-registered intensity p90 = {p90_threshold:.4f}  (LONG_LIQ, all symbols)")
    else:
        p90_threshold = float("nan")

    # ── ANÁLISIS 1: PLACEBO EMPAREJADO POR DIRECCIÓN (test decisivo) ─────────
    print("\n\n" + "═"*80)
    print("  ANÁLISIS 1 — PLACEBO EMPAREJADO POR DIRECCIÓN")
    print("  Interpretación: cascada ≈ placebo → señal nula. >10 bps consistente → hay algo.")
    print("═"*80)

    summarize(
        [r for r in all_cascade_rows if r["direction"] == "LONG_LIQ"],
        "LONG_LIQ cascadas (liquidación de largos → BUY)",
    )
    summarize(all_placebo_long_rows, "LONG placebo (BUY al azar, mismos periodos/símbolos)")

    print("\n── DIFERENCIA: LONG cascada − LONG placebo (el edge real, si existe) ──")
    long_cas_df   = cas_df[cas_df["direction"] == "LONG_LIQ"] if not cas_df.empty else pd.DataFrame()
    if not long_cas_df.empty and not plac_long_df.empty:
        print(f"\n{'distancia':>12}  " + "  ".join(
            f"{'Δret_'+str(h)+'m':>12}" for h in MARKOUT_HORIZONS_MIN
        ))
        for d in ENTRY_DISTANCES_BPS:
            c_sub = long_cas_df[long_cas_df["entry_distance_bps"] == d]
            p_sub = plac_long_df[plac_long_df["entry_distance_bps"] == d]
            parts = [f"{d:>12}bps"]
            for h in MARKOUT_HORIZONS_MIN:
                col = f"ret_{h}m"
                cm = c_sub[col].dropna().mean()
                pm = p_sub[col].dropna().mean()
                delta = cm - pm if pd.notna(cm) and pd.notna(pm) else None
                parts.append(f"{delta:>+11.1f}" if delta is not None else f"{'N/A':>12}")
            print("  ".join(parts))

    summarize(
        [r for r in all_cascade_rows if r["direction"] == "SHORT_LIQ"],
        "SHORT_LIQ cascadas (liquidación de cortos → SELL)",
    )
    summarize(all_placebo_short_rows, "SHORT placebo (SELL al azar, mismos periodos/símbolos)")

    print("\n── DIFERENCIA: SHORT cascada − SHORT placebo ──")
    short_cas_df = cas_df[cas_df["direction"] == "SHORT_LIQ"] if not cas_df.empty else pd.DataFrame()
    if not short_cas_df.empty and not plac_short_df.empty:
        print(f"\n{'distancia':>12}  " + "  ".join(
            f"{'Δret_'+str(h)+'m':>12}" for h in MARKOUT_HORIZONS_MIN
        ))
        for d in ENTRY_DISTANCES_BPS:
            c_sub = short_cas_df[short_cas_df["entry_distance_bps"] == d]
            p_sub = plac_short_df[plac_short_df["entry_distance_bps"] == d]
            parts = [f"{d:>12}bps"]
            for h in MARKOUT_HORIZONS_MIN:
                col = f"ret_{h}m"
                cm = c_sub[col].dropna().mean()
                pm = p_sub[col].dropna().mean()
                delta = cm - pm if pd.notna(cm) and pd.notna(pm) else None
                parts.append(f"{delta:>+11.1f}" if delta is not None else f"{'N/A':>12}")
            print("  ".join(parts))

    # ── ANÁLISIS 2: DIAGNÓSTICO 2025-2026 ────────────────────────────────────
    print("\n\n" + "═"*80)
    print("  ANÁLISIS 2 — DIAGNÓSTICO 2025-2026")
    print("  (fill%=0 → ¿bug de datos o fenómeno extinto?)")
    print("═"*80)
    if not cas_df.empty:
        sub = cas_df[(cas_df["entry_distance_bps"] == 100) & (cas_df["direction"] == "LONG_LIQ")]
        by_year = sub.groupby("year").agg(
            n_events=("onset_ts", "nunique"),
            fill_pct=("filled", lambda x: x.mean() * 100),
            ret_60m_mean=("ret_60m", "mean"),
            ret_60m_std=("ret_60m", "std"),
        ).round(2)
        print(by_year.to_string())
        years_2025_26 = sub[sub["year"].isin([2025, 2026])]
        n_filled_recent = int(years_2025_26["filled"].sum())
        print(f"\n  2025-2026 fills at -100bps: {n_filled_recent} / {len(years_2025_26)} entries")
        if n_filled_recent == 0:
            print("  → Precio nunca cae -100 bps en 30 min en 2025-2026.")
            print("    Si el detector detecta eventos pero ninguno llega a -100 bps → fenómeno extinto.")
            print("    Si el detector no detecta eventos en esos años → posible bug de datos.")
        else:
            recent_ret = years_2025_26[years_2025_26["filled"]]["ret_60m"].dropna()
            print(f"  → mean ret_60m (filled): {recent_ret.mean():.2f} bps  (n={len(recent_ret)})")

    # ── ANÁLISIS 3: CORTE POR DÉCIL DE INTENSIDAD (pre-registrado) ───────────
    print("\n\n" + "═"*80)
    print("  ANÁLISIS 3 — CORTE POR DÉCIL DE INTENSIDAD")
    print(f"  Umbral p90 pre-registrado: {p90_threshold:.4f}")
    print("  Top decile LONG_LIQ vs LONG placebo emparejado")
    print("═"*80)
    if not cas_df.empty and not np.isnan(p90_threshold):
        top_decile = cas_df[
            (cas_df["direction"] == "LONG_LIQ") &
            (cas_df["intensidad"] >= p90_threshold)
        ]
        summarize(top_decile.to_dict("records"), f"Top-10% intensidad LONG_LIQ  (intensidad ≥ {p90_threshold:.4f})")
        summarize(all_placebo_long_rows, "LONG placebo (referencia — misma que Análisis 1)")

        print("\n── DIFERENCIA: top-decile cascada − LONG placebo ──")
        if not top_decile.empty and not plac_long_df.empty:
            print(f"\n{'distancia':>12}  " + "  ".join(
                f"{'Δret_'+str(h)+'m':>12}" for h in MARKOUT_HORIZONS_MIN
            ))
            for d in ENTRY_DISTANCES_BPS:
                c_sub = top_decile[top_decile["entry_distance_bps"] == d]
                p_sub = plac_long_df[plac_long_df["entry_distance_bps"] == d]
                parts = [f"{d:>12}bps"]
                for h in MARKOUT_HORIZONS_MIN:
                    col = f"ret_{h}m"
                    cm = c_sub[col].dropna().mean()
                    pm = p_sub[col].dropna().mean()
                    delta = cm - pm if pd.notna(cm) and pd.notna(pm) else None
                    parts.append(f"{delta:>+11.1f}" if delta is not None else f"{'N/A':>12}")
                print("  ".join(parts))
        n_top = top_decile["onset_ts"].nunique() if not top_decile.empty else 0
        print(f"\n  n eventos top-decile LONG_LIQ: {n_top}")
        print(f"  n eventos total   LONG_LIQ:    {long_cas_df['onset_ts'].nunique() if not long_cas_df.empty else 0}")

    # ── DATOS DE REFERENCIA ───────────────────────────────────────────────────
    print("\n\n── ESTADÍSTICAS POR SÍMBOLO ──")
    for sym, st in symbol_stats.items():
        print(f"  {sym}: {st['n_long_liq']} LONG_LIQ, {st['n_short_liq']} SHORT_LIQ, "
              f"θ={st['theta']:.4f}")

    print(f"\n\nResultados guardados en: {out_dir}/")


if __name__ == "__main__":
    main()
