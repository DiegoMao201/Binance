#!/usr/bin/env python3
"""
download_historical.py — Download and verify Binance historical datasets.

Implements DECISIONES_TECNICAS.md Annex: parallel task, no bot/DB involved.

Datasets:
  1. liquidationSnapshot  (2020 → 2024-03-31, 7 futures symbols)
  2. metrics/openInterest (2020 → today, 7 futures symbols)
  3. aggTrades            (2019 → today, monthly, 7 futures symbols)
  4. Tardis free samples  (day-1 of each month since 2020-01)

Output: data/historical/ + informe_historical.json

Usage:
    python scripts/download_historical.py [--check-range] [--liq] [--oi]
                                          [--agg] [--tardis] [--all]
                                          [--symbols ETH BTC ...]

    --check-range   Only verify what date ranges are available (fast)
    --all           Download everything (default if no flag given)
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"

BASE_URL = "https://data.binance.vision"
TARDIS_BASE = "https://datasets.tardis.dev/v1/binance-futures/liquidations"

SYMBOLS_FUTURES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "BNBUSDT",
]

LIQ_END_DATE = date(2024, 3, 31)    # last known date for liquidationSnapshot
LIQ_START_DATE = date(2020, 1, 1)
OI_START_DATE = date(2020, 9, 1)    # metrics start 2020-09-01 per S3 listing
AGG_START_DATE = date(2020, 1, 1)
TARDIS_START = date(2020, 1, 1)

# Known crisis months worth downloading for cascade analysis (saves 95% of disk)
CRISIS_MONTHS = [
    (2020, 3),   # COVID crash
    (2021, 5),   # crypto crash
    (2021, 9),   # China ban
    (2022, 5),   # LUNA collapse
    (2022, 11),  # FTX collapse
    (2023, 3),   # USDC depeg / SVB
    (2023, 8),   # regulatory sell-off
    (2024, 1),   # ETF approval (high OI delta)
    (2024, 3),   # post-ETF crash
]

# Estimated compressed file sizes (MB), for storage checks
AGG_MB_PER_MONTH = {
    "BTCUSDT": 800, "ETHUSDT": 400, "SOLUSDT": 120,
    "XRPUSDT": 100, "DOGEUSDT": 80, "LINKUSDT": 60, "BNBUSDT": 150,
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def head_url(url: str, timeout: int = 10) -> int:
    req = Request(url, method="HEAD")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status
    except HTTPError as e:
        return e.code
    except URLError:
        return 0


def download_bytes(url: str, timeout: int = 60, silent_404: bool = False) -> Optional[bytes]:
    try:
        req = Request(url, headers={"User-Agent": "binance-downloader/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as e:
        if silent_404 and e.code in (400, 404):
            return None  # symbol not listed in this period — expected, not an error
        print(f"    ERROR downloading {url}: HTTP {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    ERROR downloading {url}: {e}", file=sys.stderr)
        return None


def verify_checksum(data: bytes, checksum_data: bytes) -> bool:
    expected = checksum_data.decode().split()[0].strip()
    sha256 = hashlib.sha256(data).hexdigest()
    return sha256 == expected


def iter_months(start: date, end: date) -> Iterator[tuple[int, int]]:
    cur = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    while cur <= end_month:
        yield cur.year, cur.month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


def iter_days(start: date, end: date) -> Iterator[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


# ── Range verification ────────────────────────────────────────────────────────

def s3_list(prefix: str, max_keys: int = 10) -> List[str]:
    """Return S3 object keys under prefix using XML listing."""
    url = (
        f"https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
        f"?delimiter=/&prefix={prefix}&max-keys={max_keys}"
    )
    data = download_bytes(url, timeout=15)
    if not data:
        return []
    text = data.decode()
    import re
    return re.findall(r"<Key>(.*?)</Key>", text)


def check_liquidation_snapshot_exists() -> bool:
    """Fast check: does the liquidationSnapshot dataset still exist in S3?"""
    keys = s3_list("data/futures/um/daily/liquidationSnapshot/BTCUSDT/", max_keys=3)
    return len(keys) > 0


def check_ranges(symbols: List[str]) -> dict:
    """Check available date ranges for all datasets via S3 listing."""
    print("Checking available date ranges…")
    info: dict = {}

    # liquidationSnapshot
    liq_exists = check_liquidation_snapshot_exists()
    if not liq_exists:
        print("  ⚠ liquidationSnapshot: GONE from S3 (removed without notice)")
        info["liquidationSnapshot"] = {"status": "GONE", "note": "Removed from data.binance.vision"}
    else:
        # find actual date range via S3 listing
        for sym in symbols[:1]:  # just check BTCUSDT
            keys = s3_list(f"data/futures/um/daily/liquidationSnapshot/{sym}/", max_keys=5)
            dates = sorted(k.split("-liquidationSnapshot-")[1].replace(".zip","")
                           for k in keys if "liquidationSnapshot" in k and ".zip" in k and "CHECKSUM" not in k)
            if dates:
                info["liquidationSnapshot"] = {"first": dates[0], "sample": dates[:3]}
        print(f"  liquidationSnapshot: {'found' if liq_exists else 'NOT FOUND'}")

    # metrics — check first and last available date
    for sym in symbols[:1]:
        keys_first = s3_list(f"data/futures/um/daily/metrics/{sym}/", max_keys=3)
        first_dates = sorted(k.split(f"-metrics-")[1].replace(".zip","").replace(".CHECKSUM","")
                             for k in keys_first if "metrics" in k and "CHECKSUM" not in k and ".zip" in k)
        # get last
        last_marker = f"data/futures/um/daily/metrics/{sym}/{sym}-metrics-2026-07-30.zip"
        url = (
            f"https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
            f"?delimiter=/&prefix=data/futures/um/daily/metrics/{sym}/&max-keys=5"
            f"&marker={last_marker}"
        )
        data = download_bytes(url, timeout=15)
        import re
        last_keys = re.findall(r"<Key>(.*?)</Key>", data.decode() if data else "")
        last_dates = sorted(k.split(f"-metrics-")[1].replace(".zip","").replace(".CHECKSUM","")
                            for k in last_keys if "metrics" in k and "CHECKSUM" not in k and ".zip" in k)
        info["metrics"] = {
            "first": first_dates[0] if first_dates else "unknown",
            "last": last_dates[-1] if last_dates else "unknown",
        }
        print(f"  metrics: {info['metrics']['first']} → {info['metrics']['last']}")

    # aggTrades monthly
    for sym in symbols[:1]:
        keys_first = s3_list(f"data/futures/um/monthly/aggTrades/{sym}/", max_keys=3)
        first_mo = sorted(k.split(f"-aggTrades-")[1].replace(".zip","").replace(".CHECKSUM","")
                          for k in keys_first if "aggTrades" in k and "CHECKSUM" not in k and ".zip" in k)
        last_marker = f"data/futures/um/monthly/aggTrades/{sym}/{sym}-aggTrades-2026-05.zip"
        url2 = (
            f"https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
            f"?delimiter=/&prefix=data/futures/um/monthly/aggTrades/{sym}/&max-keys=5"
            f"&marker={last_marker}"
        )
        data2 = download_bytes(url2, timeout=15)
        last_mo_keys = re.findall(r"<Key>(.*?)</Key>", data2.decode() if data2 else "")
        last_mo = sorted(k.split(f"-aggTrades-")[1].replace(".zip","").replace(".CHECKSUM","")
                         for k in last_mo_keys if "aggTrades" in k and "CHECKSUM" not in k and ".zip" in k)
        info["aggTrades"] = {
            "first": first_mo[0] if first_mo else "unknown",
            "last": last_mo[-1] if last_mo else "unknown",
        }
        print(f"  aggTrades: {info['aggTrades']['first']} → {info['aggTrades']['last']}")

    return info


# ── liquidationSnapshot ───────────────────────────────────────────────────────

def download_liq_snapshot(symbol: str, start: date, end: date, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"data/futures/um/daily/liquidationSnapshot/{symbol}"
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "events": 0, "bytes": 0}

    for day in iter_days(start, end):
        date_str = day.isoformat()
        fname = f"{symbol}-liquidationSnapshot-{date_str}.zip"
        out_path = out_dir / fname
        if out_path.exists():
            stats["skipped"] += 1
            continue

        url = f"{BASE_URL}/{prefix}/{fname}"
        data = download_bytes(url)
        if data is None:
            stats["failed"] += 1
            continue

        # verify checksum
        cksum_data = download_bytes(f"{url}.CHECKSUM")
        if cksum_data and not verify_checksum(data, cksum_data):
            print(f"    CHECKSUM MISMATCH: {fname}", file=sys.stderr)
            stats["failed"] += 1
            continue

        out_path.write_bytes(data)
        stats["downloaded"] += 1
        stats["bytes"] += len(data)

        # count events
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.endswith(".csv"):
                        content = zf.read(name).decode()
                        stats["events"] += max(0, content.count("\n") - 1)
        except Exception:
            pass

        time.sleep(0.05)

    return stats


# ── OI metrics ────────────────────────────────────────────────────────────────

def download_oi_metrics(symbol: str, start: date, end: date, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"data/futures/um/daily/metrics/{symbol}"
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    for day in iter_days(start, end):
        date_str = day.isoformat()
        fname = f"{symbol}-metrics-{date_str}.zip"
        out_path = out_dir / fname
        if out_path.exists():
            stats["skipped"] += 1
            continue

        url = f"{BASE_URL}/{prefix}/{fname}"
        data = download_bytes(url)
        if data is None:
            stats["failed"] += 1
            continue

        out_path.write_bytes(data)
        stats["downloaded"] += 1
        stats["bytes"] += len(data)
        time.sleep(0.05)

    return stats


# ── aggTrades (monthly) ───────────────────────────────────────────────────────

def download_agg_trades(symbol: str, start: date, end: date, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"data/futures/um/monthly/aggTrades/{symbol}"
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    for year, month in iter_months(start, end):
        month_str = f"{year}-{month:02d}"
        fname = f"{symbol}-aggTrades-{month_str}.zip"
        out_path = out_dir / fname
        if out_path.exists():
            stats["skipped"] += 1
            continue

        url = f"{BASE_URL}/{prefix}/{fname}"
        data = download_bytes(url, timeout=300)
        if data is None:
            stats["failed"] += 1
            continue

        cksum_data = download_bytes(f"{url}.CHECKSUM")
        if cksum_data and not verify_checksum(data, cksum_data):
            print(f"    CHECKSUM MISMATCH: {fname}", file=sys.stderr)
            stats["failed"] += 1
            continue

        out_path.write_bytes(data)
        stats["downloaded"] += 1
        stats["bytes"] += len(data)
        time.sleep(0.1)

    return stats


# ── Tardis free samples (day 1 of each month) ────────────────────────────────

def download_tardis_samples(symbol: str, start: date, end: date, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    sym_lower = symbol.lower()

    for year, month in iter_months(start, end):
        fname = f"tardis_{sym_lower}_{year}-{month:02d}-01.csv.gz"
        out_path = out_dir / fname
        if out_path.exists():
            stats["skipped"] += 1
            continue

        url = f"{TARDIS_BASE}/{year}/{month:02d}/01/{sym_lower}.csv.gz"
        data = download_bytes(url, silent_404=True)  # 400/404 = symbol not listed yet
        if data is None:
            stats["failed"] += 1
            continue

        out_path.write_bytes(data)
        stats["downloaded"] += 1
        stats["bytes"] += len(data)
        time.sleep(0.5)

    return stats


# ── Informe ───────────────────────────────────────────────────────────────────

def bytes_human(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} TB"


def write_informe(report: dict) -> Path:
    path = DATA_DIR / "informe_historical.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def disk_free_gb(path: Path) -> float:
    import shutil
    stat = shutil.disk_usage(path)
    return stat.free / 1024**3


def estimate_agg_gb(symbols: List[str], months: List[tuple]) -> float:
    """Rough estimate of compressed GB for the given symbols × months."""
    mb_per_month = sum(AGG_MB_PER_MONTH.get(s, 100) for s in symbols)
    return mb_per_month * len(months) / 1024


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Binance historical datasets")
    parser.add_argument("--check-range", action="store_true", help="Only probe date ranges")
    parser.add_argument("--liq",    action="store_true", help="Download liquidationSnapshot (likely gone)")
    parser.add_argument("--oi",     action="store_true", help="Download OI metrics (recommended first step)")
    parser.add_argument("--agg",    action="store_true", help="Download aggTrades monthly")
    parser.add_argument("--tardis", action="store_true", help="Download Tardis free day-1 samples")
    parser.add_argument("--all",    action="store_true", help="Download oi + agg + tardis (skips liq if gone)")
    parser.add_argument("--crisis-only", action="store_true",
                        help="For --agg: download only known crisis months instead of all")
    parser.add_argument(
        "--symbols", nargs="+", default=SYMBOLS_FUTURES,
        help="Futures symbols to download",
    )
    args = parser.parse_args()

    if not any([args.check_range, args.liq, args.oi, args.agg, args.tardis, args.all]):
        # Default: range check + OI metrics only (safe, small)
        args.check_range = True
        args.oi = True

    symbols = args.symbols
    today = date.today()
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "symbols": symbols,
        "datasets": {},
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    free_gb = disk_free_gb(DATA_DIR)
    print(f"Disk free: {free_gb:.1f} GB on {DATA_DIR}")

    t0 = time.time()

    if args.check_range or args.all:
        ranges = check_ranges(symbols)
        report["date_ranges"] = ranges

    if args.liq or args.all:
        liq_exists = check_liquidation_snapshot_exists()
        if not liq_exists:
            print("\n⚠  liquidationSnapshot: GONE from S3 — skipping")
            print("   Dataset was removed without notice. Use OI+aggTrades cascade proxy instead.")
            report["datasets"]["liquidationSnapshot"] = {"status": "GONE_FROM_S3"}
        else:
            print(f"\nDownloading liquidationSnapshot ({LIQ_START_DATE} → {LIQ_END_DATE})")
            for sym in symbols:
                print(f"  {sym}…")
                out = DATA_DIR / "liquidationSnapshot" / sym
                stats = download_liq_snapshot(sym, LIQ_START_DATE, LIQ_END_DATE, out)
                report["datasets"].setdefault("liquidationSnapshot", {})[sym] = stats
                print(f"    ✓ {stats['downloaded']} downloaded, {stats['skipped']} skipped, "
                      f"{stats['events']} events, {bytes_human(stats['bytes'])}")

    if args.oi or args.all:
        print(f"\nDownloading OI metrics ({OI_START_DATE} → {today})")
        est_mb = 2 * len(symbols) * (today - OI_START_DATE).days / 30  # ~2KB/day/symbol
        print(f"  Estimated size: ~{est_mb:.0f} MB for {len(symbols)} symbols")
        for sym in symbols:
            print(f"  {sym}…", end=" ", flush=True)
            out = DATA_DIR / "metrics" / sym
            stats = download_oi_metrics(sym, OI_START_DATE, today, out)
            report["datasets"].setdefault("metrics", {})[sym] = stats
            print(f"✓ {stats['downloaded']}↓ {stats['skipped']}= {bytes_human(stats['bytes'])}")

    if args.agg or args.all:
        if args.crisis_only:
            months = CRISIS_MONTHS
            est_gb = estimate_agg_gb(symbols, months)
            print(f"\nDownloading aggTrades (crisis months only: {len(months)} months × {len(symbols)} symbols)")
            print(f"  Estimated: ~{est_gb:.1f} GB compressed")
        else:
            months = list(iter_months(AGG_START_DATE, today))
            est_gb = estimate_agg_gb(symbols, months)
            print(f"\nDownloading ALL aggTrades monthly ({AGG_START_DATE} → {today})")
            print(f"  Estimated: ~{est_gb:.1f} GB compressed — need {est_gb:.0f} GB free (have {free_gb:.1f} GB)")
            if est_gb > free_gb * 0.8:
                print("  ⚠  Not enough disk space for full download. Use --crisis-only instead.")
                if not input("  Continue anyway? [y/N] ").strip().lower().startswith("y"):
                    print("  Aborted.")
                    return 1

        for sym in symbols:
            print(f"  {sym}…")
            out = DATA_DIR / "aggTrades" / sym
            for year, month in months:
                start_m = date(year, month, 1)
                end_m = date(year, month, 1)
                s = download_agg_trades(sym, start_m, end_m, out)
                if s["downloaded"]:
                    print(f"    {year}-{month:02d}: {bytes_human(s['bytes'])}")
                elif s["failed"]:
                    print(f"    {year}-{month:02d}: failed ({s['failed']} errors)")
            report["datasets"].setdefault("aggTrades", {})[sym] = {"months": len(months)}

    if args.tardis or args.all:
        print(f"\nDownloading Tardis free samples (day-1 monthly, {TARDIS_START} → today)")
        for sym in symbols:
            print(f"  {sym}…", end=" ", flush=True)
            out = DATA_DIR / "tardis" / sym
            stats = download_tardis_samples(sym, TARDIS_START, today, out)
            report["datasets"].setdefault("tardis", {})[sym] = stats
            print(f"✓ {stats['downloaded']}↓ {stats['skipped']}= {stats['failed']} failed")

    elapsed = time.time() - t0
    total_bytes = sum(
        (stats.get("bytes", 0) if isinstance(stats, dict) else 0)
        for ds in report["datasets"].values()
        for stats in (ds.values() if isinstance(ds, dict) else [])
    )
    report["summary"] = {
        "elapsed_s": round(elapsed, 1),
        "total_bytes": total_bytes,
        "total_human": bytes_human(total_bytes),
    }

    informe_path = write_informe(report)
    print(f"\nInforme written to: {informe_path}")
    print(f"Total: {bytes_human(total_bytes)} in {elapsed:.0f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
