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
OI_START_DATE = date(2020, 1, 1)
AGG_START_DATE = date(2019, 1, 1)
TARDIS_START = date(2020, 1, 1)


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


def download_bytes(url: str, timeout: int = 60) -> Optional[bytes]:
    try:
        req = Request(url, headers={"User-Agent": "binance-downloader/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
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

def check_liq_snapshot_range(symbol: str) -> dict:
    """Probe the actual available date range for liquidationSnapshot."""
    prefix = f"data/futures/um/daily/liquidationSnapshot/{symbol}"
    result = {"symbol": symbol, "first_date": None, "last_date": None, "sample_count": 0}

    # probe known boundary
    last_known = LIQ_END_DATE
    while last_known >= date(2024, 1, 1):
        url = f"{BASE_URL}/{prefix}/{symbol}-liquidationSnapshot-{last_known.isoformat()}.zip"
        status = head_url(url)
        if status == 200:
            result["last_date"] = last_known.isoformat()
            break
        last_known -= timedelta(days=1)

    # probe start
    first = LIQ_START_DATE
    while first <= date(2021, 1, 1):
        url = f"{BASE_URL}/{prefix}/{symbol}-liquidationSnapshot-{first.isoformat()}.zip"
        status = head_url(url)
        if status == 200:
            result["first_date"] = first.isoformat()
            break
        first += timedelta(days=1)

    return result


def check_ranges(symbols: List[str]) -> dict:
    print("Checking available date ranges…")
    info = {}
    for sym in symbols:
        print(f"  {sym}…", end=" ", flush=True)
        r = check_liq_snapshot_range(sym)
        info[sym] = r
        print(f"  liq: {r['first_date']} → {r['last_date']}")
        time.sleep(0.3)
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
        data = download_bytes(url)
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

def main() -> int:
    parser = argparse.ArgumentParser(description="Download Binance historical datasets")
    parser.add_argument("--check-range", action="store_true", help="Only probe date ranges")
    parser.add_argument("--liq",    action="store_true", help="Download liquidationSnapshot")
    parser.add_argument("--oi",     action="store_true", help="Download OI metrics")
    parser.add_argument("--agg",    action="store_true", help="Download aggTrades (monthly)")
    parser.add_argument("--tardis", action="store_true", help="Download Tardis free day-1 samples")
    parser.add_argument("--all",    action="store_true", help="Download all datasets")
    parser.add_argument(
        "--symbols", nargs="+", default=SYMBOLS_FUTURES,
        help="Futures symbols to download",
    )
    args = parser.parse_args()

    if not any([args.check_range, args.liq, args.oi, args.agg, args.tardis, args.all]):
        args.all = True

    symbols = args.symbols
    today = date.today()
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "symbols": symbols,
        "datasets": {},
    }

    t0 = time.time()

    if args.check_range or args.all:
        ranges = check_ranges(symbols)
        report["date_ranges"] = ranges

    if args.liq or args.all:
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
        for sym in symbols:
            print(f"  {sym}…")
            out = DATA_DIR / "metrics" / sym
            stats = download_oi_metrics(sym, OI_START_DATE, today, out)
            report["datasets"].setdefault("metrics", {})[sym] = stats
            print(f"    ✓ {stats['downloaded']} downloaded, {stats['skipped']} skipped, "
                  f"{bytes_human(stats['bytes'])}")

    if args.agg or args.all:
        print(f"\nDownloading aggTrades monthly ({AGG_START_DATE} → {today})")
        for sym in symbols:
            print(f"  {sym}…")
            out = DATA_DIR / "aggTrades" / sym
            stats = download_agg_trades(sym, AGG_START_DATE, today, out)
            report["datasets"].setdefault("aggTrades", {})[sym] = stats
            print(f"    ✓ {stats['downloaded']} downloaded, {stats['skipped']} skipped, "
                  f"{bytes_human(stats['bytes'])}")

    if args.tardis or args.all:
        print(f"\nDownloading Tardis free samples (day-1 monthly, {TARDIS_START} → today)")
        for sym in symbols:
            print(f"  {sym}…")
            out = DATA_DIR / "tardis" / sym
            stats = download_tardis_samples(sym, TARDIS_START, today, out)
            report["datasets"].setdefault("tardis", {})[sym] = stats
            print(f"    ✓ {stats['downloaded']} downloaded, {stats['skipped']} skipped, "
                  f"{bytes_human(stats['bytes'])}")

    elapsed = time.time() - t0
    total_bytes = sum(
        stats.get("bytes", 0)
        for ds in report["datasets"].values()
        for stats in ds.values()
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
