"""
Bootstrap the per-symbol / per-UTC-hour spike-rate baseline from historical
``deriv_spike_events.json``.

Designed to run once on the server (or locally over a downloaded copy) to
seed ``deriv_session_baseline.json`` so that ``scripts/market_phase`` does
not need to spend days warming up its EMA.

Usage on the production host:

    python3 -m scripts.bootstrap_session_baseline \
        --spikes /data/deriv-logs/deriv_spike_events.json \
        --out /data/deriv-logs/deriv_session_baseline.json \
        --days 14

Methodology
-----------
For each symbol and each UTC hour (0..23) we compute the spikes-per-minute
rate inside that hour for each day present in the file, then store the
**median** rate across days as the seed ``ema_rate``. ``samples`` is set to
the number of days that contributed, so the EMA already considers itself
"warm" once you've bootstrapped from >= MIN_BUCKET_SAMPLES days of history.

Why median (not mean)? The spike feed contains rare days with extraordinary
outliers (network blips, restarts) that would otherwise inflate the baseline
and mute the deceleration signal we want.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _safe_ts(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x if x > 0 else None
    s = str(v).strip()
    if not s:
        return None
    try:
        x = float(s)
        return x if x > 0 else None
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def build(spikes_path: Path, out_path: Path, days: int) -> dict:
    raw = json.loads(spikes_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"unexpected spike file shape (not a list): {spikes_path}")

    now_ts = time.time()
    cutoff = now_ts - max(1, int(days)) * 86400

    # per (symbol, day_iso, hour) -> count
    counts: dict[tuple[str, str, int], int] = defaultdict(int)
    seen_days_per_bucket: dict[tuple[str, int], set[str]] = defaultdict(set)

    for ev in raw:
        if not isinstance(ev, dict):
            continue
        sym = str(ev.get("symbol") or "").upper()
        if not sym:
            continue
        ts = _safe_ts(ev.get("ts") or ev.get("timestamp") or ev.get("iso"))
        if ts is None or ts < cutoff:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        day_iso = dt.date().isoformat()
        hour = dt.hour
        counts[(sym, day_iso, hour)] += 1
        seen_days_per_bucket[(sym, hour)].add(day_iso)

    # per (symbol, hour) -> list of spikes-per-minute (one per day with data)
    by_bucket: dict[tuple[str, int], list[float]] = defaultdict(list)
    for (sym, day_iso, hour), n in counts.items():
        # 60-minute window inside each (day, hour) bucket
        by_bucket[(sym, hour)].append(float(n) / 60.0)

    symbols: dict[str, dict[str, dict]] = {}
    for (sym, hour), rates in by_bucket.items():
        if not rates:
            continue
        rate = statistics.median(rates)
        slot = symbols.setdefault(sym, {})
        slot[f"{hour:02d}"] = {
            "ema_rate": round(max(0.0, float(rate)), 6),
            "samples": int(len(seen_days_per_bucket[(sym, hour)])),
            "last_update_ts": now_ts,
        }

    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "bootstrap": {
            "source": str(spikes_path),
            "lookback_days": int(days),
            "spike_events_considered": sum(counts.values()),
            "symbols_seen": len(symbols),
        },
        "symbols": symbols,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spikes", required=True, type=Path, help="Path to deriv_spike_events.json")
    ap.add_argument("--out", required=True, type=Path, help="Destination deriv_session_baseline.json")
    ap.add_argument("--days", type=int, default=14, help="Lookback window in days (default 14)")
    args = ap.parse_args()

    if not args.spikes.exists():
        print(f"spikes file not found: {args.spikes}", file=sys.stderr)
        return 2

    payload = build(args.spikes, args.out, args.days)
    summary = {
        "events": payload["bootstrap"]["spike_events_considered"],
        "symbols": payload["bootstrap"]["symbols_seen"],
        "out": str(args.out),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
