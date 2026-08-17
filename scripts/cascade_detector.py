#!/usr/bin/env python3
"""
cascade_detector.py — OI + aggTrades cascade proxy detector.

DECISIONES_TECNICAS.md §6: the throttled liquidations feed gives 1 event/s/symbol
during cascades that produce hundreds/s. The data is truncated exactly when needed.

Instead: caída de OI + burst de aggTrades unidireccionales + expansión de rango
= firma de cascada con datos gratuitos y completos desde 2019.

Usage:
    # Step 1: download metrics first
    python scripts/download_historical.py --oi

    # Step 2: detect cascades
    python scripts/cascade_detector.py --oi-dir data/historical/metrics
    python scripts/cascade_detector.py --oi-dir data/historical/metrics --symbol BTCUSDT

Output:
    data/historical/cascade_events.json  — list of detected cascade events
    data/historical/cascade_report.txt   — human-readable report
"""

import argparse
import csv
import io
import json
import math
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import List, Optional

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"

# Saturation: fraction of 5-min windows where OI drops > threshold
# Cascade onset: 5-min window where OI drop exceeds SIGMA_THRESHOLD × rolling std
SIGMA_THRESHOLD = 2.5      # z-score for OI drop to flag as cascade
MIN_OI_DROP_PCT = 0.5      # minimum % OI drop (filter noise)
LOOKBACK_PERIODS = 48      # periods for rolling std (48 × 5min = 4h)


def load_metrics_zip(path: Path) -> List[dict]:
    """Parse a single *-metrics-YYYY-MM-DD.zip file."""
    rows = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith(".csv"):
                    content = zf.read(name).decode()
                    reader = csv.DictReader(io.StringIO(content))
                    for row in reader:
                        rows.append(row)
    except Exception as e:
        print(f"  WARN: {path.name}: {e}", file=sys.stderr)
    return rows


def load_symbol_metrics(symbol: str, oi_dir: Path) -> List[dict]:
    """Load all metrics zips for a symbol, sorted by date."""
    sym_dir = oi_dir / symbol
    if not sym_dir.exists():
        print(f"  Missing: {sym_dir}", file=sys.stderr)
        return []

    zips = sorted(sym_dir.glob(f"{symbol}-metrics-*.zip"))
    all_rows = []
    for z in zips:
        all_rows.extend(load_metrics_zip(z))

    # Sort by timestamp — the metrics CSV has a 'timestamp' column (ms)
    try:
        all_rows.sort(key=lambda r: int(r.get("timestamp", r.get("create_time", 0))))
    except (ValueError, TypeError):
        pass

    return all_rows


def parse_oi(row: dict) -> Optional[float]:
    """Extract open interest from metrics row."""
    # Binance metrics CSV has: sum_open_interest, sum_open_interest_value
    for key in ("sum_open_interest", "open_interest", "sumOpenInterest"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return None


def parse_ts(row: dict) -> Optional[int]:
    """Extract timestamp in milliseconds."""
    for key in ("timestamp", "create_time", "createTime"):
        v = row.get(key)
        if v is not None:
            try:
                return int(float(v))
            except (ValueError, TypeError):
                pass
    return None


def detect_cascades(rows: List[dict], symbol: str) -> List[dict]:
    """
    Apply OI-based cascade detector.

    A cascade candidate is a period where:
      1. OI drops by > SIGMA_THRESHOLD × rolling_std (relative drop)
      2. The absolute % drop is >= MIN_OI_DROP_PCT

    Saturation metric: fraction of 5-min windows with at least one flagged drop
    in a 60-min window.
    """
    # Extract (ts_ms, oi) pairs
    samples = []
    for r in rows:
        ts = parse_ts(r)
        oi = parse_oi(r)
        if ts is not None and oi is not None and oi > 0:
            samples.append((ts, oi))

    if len(samples) < LOOKBACK_PERIODS + 2:
        print(f"  {symbol}: insufficient data ({len(samples)} rows)", file=sys.stderr)
        return []

    # Compute OI % changes
    pct_changes = []
    for i in range(1, len(samples)):
        ts_prev, oi_prev = samples[i - 1]
        ts_curr, oi_curr = samples[i]
        pct_change = 100.0 * (oi_curr - oi_prev) / oi_prev
        pct_changes.append((ts_curr, pct_change, oi_curr, oi_prev))

    # Rolling z-score of OI drops
    events = []
    for i in range(LOOKBACK_PERIODS, len(pct_changes)):
        window = [x[1] for x in pct_changes[i - LOOKBACK_PERIODS:i]]
        w_mean = mean(window)
        w_std = stdev(window) if len(window) >= 2 else 0.0

        ts_ms, pct, oi_curr, oi_prev = pct_changes[i]
        if pct >= 0 or w_std == 0:
            continue

        z = (pct - w_mean) / w_std  # negative means drop, z << 0 for cascades
        if z < -SIGMA_THRESHOLD and abs(pct) >= MIN_OI_DROP_PCT:
            ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            events.append({
                "symbol": symbol,
                "ts": ts_dt.isoformat(),
                "ts_ms": ts_ms,
                "oi_prev": round(oi_prev, 4),
                "oi_curr": round(oi_curr, 4),
                "oi_drop_pct": round(abs(pct), 4),
                "z_score": round(z, 3),
                "cascade_type": classify_cascade(abs(pct), z),
            })

    return events


def classify_cascade(drop_pct: float, z: float) -> str:
    """Rough severity classification."""
    if drop_pct >= 3.0 or z <= -5.0:
        return "MAJOR"
    elif drop_pct >= 1.5 or z <= -3.5:
        return "SIGNIFICANT"
    else:
        return "MINOR"


def compute_saturation(events: List[dict], total_windows: int) -> float:
    """Fraction of 60-min windows containing at least one cascade event."""
    if total_windows == 0:
        return 0.0
    # group events by 12-period bucket (12 × 5min = 60min)
    buckets = set()
    for e in events:
        bucket = e["ts_ms"] // (12 * 5 * 60 * 1000)
        buckets.add(bucket)
    return len(buckets) / total_windows


def yearly_summary(events: List[dict]) -> dict:
    by_year: dict = defaultdict(lambda: {"total": 0, "MAJOR": 0, "SIGNIFICANT": 0, "MINOR": 0})
    for e in events:
        year = int(e["ts"][:4])
        by_year[year]["total"] += 1
        by_year[year][e["cascade_type"]] += 1
    return dict(sorted(by_year.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="OI + cascade proxy detector")
    parser.add_argument("--oi-dir", type=Path, default=DATA_DIR / "metrics",
                        help="Directory with metrics zips (default: data/historical/metrics)")
    parser.add_argument("--symbol", help="Process single symbol only")
    parser.add_argument("--sigma", type=float, default=SIGMA_THRESHOLD,
                        help=f"Z-score threshold (default: {SIGMA_THRESHOLD})")
    parser.add_argument("--min-drop", type=float, default=MIN_OI_DROP_PCT,
                        help=f"Minimum OI drop %% (default: {MIN_OI_DROP_PCT})")
    args = parser.parse_args()

    oi_dir = args.oi_dir
    if not oi_dir.exists():
        print(f"OI directory not found: {oi_dir}")
        print("Run first: python scripts/download_historical.py --oi")
        return 1

    global SIGMA_THRESHOLD, MIN_OI_DROP_PCT
    SIGMA_THRESHOLD = args.sigma
    MIN_OI_DROP_PCT = args.min_drop

    symbols = [args.symbol] if args.symbol else sorted(
        d.name for d in oi_dir.iterdir() if d.is_dir()
    )

    if not symbols:
        print(f"No symbol directories found in {oi_dir}")
        return 1

    print(f"Cascade detector — OI proxy method")
    print(f"  Source: {oi_dir}")
    print(f"  Symbols: {symbols}")
    print(f"  Sigma threshold: {SIGMA_THRESHOLD}σ  Min drop: {MIN_OI_DROP_PCT}%")
    print()

    all_events: List[dict] = []
    symbol_stats = {}

    for sym in symbols:
        print(f"Loading {sym}…", end=" ", flush=True)
        rows = load_symbol_metrics(sym, oi_dir)
        if not rows:
            print("no data")
            continue
        print(f"{len(rows)} rows")

        events = detect_cascades(rows, sym)
        all_events.extend(events)

        # Saturation
        # 5-min resolution: rows already at 5-min intervals (Binance metrics granularity)
        sat = compute_saturation(events, max(1, len(rows) - LOOKBACK_PERIODS))
        ysum = yearly_summary(events)

        symbol_stats[sym] = {
            "rows": len(rows),
            "cascade_events": len(events),
            "saturation": round(sat, 4),
            "by_year": ysum,
        }

        print(f"  → {len(events)} cascade events detected  saturation={sat:.2%}")
        for year, d in ysum.items():
            print(f"     {year}: {d['total']} total  ({d['MAJOR']} major, {d['SIGNIFICANT']} significant)")

    # Sort all events by timestamp
    all_events.sort(key=lambda e: e["ts_ms"])

    # Save output
    out_events = DATA_DIR / "cascade_events.json"
    out_events.parent.mkdir(parents=True, exist_ok=True)
    with open(out_events, "w") as f:
        json.dump({"generated_at": datetime.now(tz=timezone.utc).isoformat(),
                   "sigma_threshold": SIGMA_THRESHOLD,
                   "min_drop_pct": MIN_OI_DROP_PCT,
                   "total_events": len(all_events),
                   "symbol_stats": symbol_stats,
                   "events": all_events[:5000]}, f, indent=2)
    print(f"\nEvents saved: {out_events}")

    # Text report
    report_path = DATA_DIR / "cascade_report.txt"
    with open(report_path, "w") as f:
        f.write(f"BINANCE CASCADE DETECTOR — OI PROXY\n")
        f.write(f"Generated: {datetime.now(tz=timezone.utc).isoformat()}\n")
        f.write(f"Sigma: {SIGMA_THRESHOLD}  Min drop: {MIN_OI_DROP_PCT}%\n\n")
        for sym, stats in symbol_stats.items():
            f.write(f"{'='*50}\n{sym}\n{'='*50}\n")
            f.write(f"  Rows: {stats['rows']}  Events: {stats['cascade_events']}  Saturation: {stats['saturation']:.2%}\n")
            f.write(f"  By year:\n")
            for year, d in stats["by_year"].items():
                f.write(f"    {year}: {d['total']:4d} total  ({d['MAJOR']} major)\n")
            f.write("\n")

        f.write(f"\nTop 20 events by severity (z-score):\n")
        top = sorted(all_events, key=lambda e: e["z_score"])[:20]
        for e in top:
            f.write(f"  {e['ts'][:19]}  {e['symbol']:10s}  drop={e['oi_drop_pct']:6.2f}%  z={e['z_score']:7.2f}  {e['cascade_type']}\n")

    print(f"Report saved: {report_path}")
    print(f"\nTotal cascade events: {len(all_events)} across {len(symbol_stats)} symbols")

    # Quick top-events summary
    if all_events:
        top5 = sorted(all_events, key=lambda e: e["z_score"])[:5]
        print("\nTop 5 cascade events by z-score:")
        for e in top5:
            print(f"  {e['ts'][:19]}  {e['symbol']:8s}  OI drop={e['oi_drop_pct']:.2f}%  z={e['z_score']:.1f}  [{e['cascade_type']}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
