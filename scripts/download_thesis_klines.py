#!/usr/bin/env python3
"""
Download Binance 1-minute klines for the cascade-reversal thesis backtest.

Usage (on server):
    python3 scripts/download_thesis_klines.py --dir /data/thesis_klines

Sources:
  Futures: https://data.binance.vision/data/futures/um/monthly/klines/{SYM}/1m/
  Spot:    https://data.binance.vision/data/spot/monthly/klines/{SYM}/1m/

Each monthly zip is ~3-8 MB. Total: ~7 symbols × 78 months × 2 = ~1 092 files, ~5-9 GB.

Checksums are verified against the .CHECKSUM file from the same URL.
Missing months (symbol not yet listed) are silently skipped.
"""

import argparse
import hashlib
import os
import sys
import time
import urllib.request
from pathlib import Path
from datetime import date
from itertools import product

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "BNBUSDT",
]

# Futures start dates (approx) — earlier months will 404, that's fine
START_YEAR, START_MONTH = 2020, 1
END_YEAR = date.today().year
END_MONTH = date.today().month - 1 or 12  # last complete month

BASE_FUTURES = "https://data.binance.vision/data/futures/um/monthly/klines"
BASE_SPOT    = "https://data.binance.vision/data/spot/monthly/klines"


def months_range():
    y, m = START_YEAR, START_MONTH
    while (y, m) <= (END_YEAR, END_MONTH):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def download_file(url: str, dest: Path, verify_checksum: bool = True) -> bool:
    """Download url to dest. Return True if file obtained, False if skipped/failed."""
    if dest.exists():
        return True  # already have it

    checksum_url = url + ".CHECKSUM"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            if r.status != 200:
                return False
            data = r.read()
    except Exception as e:
        if "404" in str(e) or "HTTP Error 404" in str(e):
            return False  # symbol not listed yet for this month
        print(f"  WARN {url}: {e}")
        return False

    if verify_checksum:
        try:
            with urllib.request.urlopen(checksum_url, timeout=10) as r:
                chk_line = r.read().decode().strip()
            expected_sha = chk_line.split()[0]
            actual_sha = hashlib.sha256(data).hexdigest()
            if actual_sha != expected_sha:
                print(f"  CHECKSUM MISMATCH {dest.name} — skipping")
                return False
        except Exception:
            pass  # checksum file missing for some months — proceed anyway

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/data/thesis_klines", help="Output directory")
    parser.add_argument("--no-checksum", action="store_true", help="Skip checksum verification")
    args = parser.parse_args()

    out = Path(args.dir)
    verify = not args.no_checksum

    total = downloaded = skipped = 0
    t0 = time.time()

    for sym, (y, m) in product(SYMBOLS, months_range()):
        fname = f"{sym}-1m-{y:04d}-{m:02d}.zip"

        for label, base in [("futures", BASE_FUTURES), ("spot", BASE_SPOT)]:
            url = f"{base}/{sym}/1m/{fname}"
            dest = out / label / sym / fname
            total += 1
            already = dest.exists()

            ok = download_file(url, dest, verify_checksum=verify)
            if ok and not already:
                downloaded += 1
                # Don't print every file — too noisy
            elif not ok:
                skipped += 1

        elapsed = time.time() - t0
        rate = total / max(elapsed, 1)
        sys.stdout.write(f"\r{total} checked | {downloaded} new | {skipped} skipped | {rate:.1f} files/s    ")
        sys.stdout.flush()

    print(f"\nDone. {downloaded} new files in {time.time()-t0:.0f}s")
    print(f"Dir: {out}")


if __name__ == "__main__":
    main()
