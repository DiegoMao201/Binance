#!/usr/bin/env python3
"""
V3 Cohort Analytics
===================
Standalone script to evaluate bot performance since the V3 AI prompt deploy
(commit e08e40e — Regime-Aware Dynamic Risk Manager, 2026-05-13).

Usage:
    python scripts/cohort_analytics.py
    python scripts/cohort_analytics.py --cutoff 2026-05-13T00:00:00
    python scripts/cohort_analytics.py --json          # machine-readable output
    python scripts/cohort_analytics.py --all-time      # include legacy cohort
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─── Default cutoff: V3 prompt deploy ────────────────────────────────────────
V3_CUTOFF_DEFAULT = "2026-05-13T00:00:00+00:00"
WIN_RATE_TARGET = 55.0       # % target from design spec
FEE_RT_PCT = 0.002           # round-trip 0.20%

# ─── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
CLOSED_TRADES_FILE = LOGS_DIR / "closed_trades.json"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_trades(path: Path) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Cannot read {path}: {exc}", file=sys.stderr)
        return []


def parse_ts(ts: str | None) -> float | None:
    """Return POSIX timestamp (seconds) or None if unparseable."""
    if not ts:
        return None
    # Try multiple formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    # Last resort: dateutil
    try:
        from dateutil import parser as dp  # optional dep
        return dp.parse(ts).timestamp()
    except Exception:
        return None


def is_v3(trade: dict) -> bool:
    """True if this trade belongs to the V3 cohort.

    Classification is STRICT: only trades explicitly tagged with
    ai_prompt_version='v3' qualify. The timestamp-based fallback
    (opened_at >= cutoff) was removed to prevent data leakage from
    pre-deployment trades executed on the same calendar day.
    """
    return trade.get("ai_prompt_version") == "v3"


def pnl_of(trade: dict) -> float:
    """Return net PnL in USDT. Uses stored value or approximates via prices."""
    if isinstance(trade.get("pnl_usdt"), (int, float)):
        return float(trade["pnl_usdt"])
    # Approximate
    entry = float(trade.get("entry_price") or trade.get("fill_price") or 0)
    exit_p = float(trade.get("exit_price") or 0)
    amount = float(trade.get("amount") or 0)
    side = str(trade.get("side", "buy")).lower()
    if not (entry and exit_p and amount):
        return 0.0
    gross = (exit_p - entry) * amount if side == "buy" else (entry - exit_p) * amount
    fee = (entry * amount + exit_p * amount) * (FEE_RT_PCT / 2)
    return gross - fee


# ─── Core metrics ─────────────────────────────────────────────────────────────

class CohortMetrics:
    def __init__(self, name: str, trades: list[dict]):
        self.name = name
        self.trades = trades
        self._compute()

    def _compute(self):
        wins = [t for t in self.trades if pnl_of(t) > 0]
        losses = [t for t in self.trades if pnl_of(t) <= 0]
        self.total = len(self.trades)
        self.wins = len(wins)
        self.losses = len(losses)
        self.gross_wins = sum(pnl_of(t) for t in wins)
        self.gross_losses = sum(abs(pnl_of(t)) for t in losses)
        self.net_pnl = self.gross_wins - self.gross_losses
        self.win_rate = (self.wins / self.total * 100) if self.total else None
        self.profit_factor = (
            self.gross_wins / self.gross_losses if self.gross_losses else (float("inf") if self.gross_wins else None)
        )
        avg_win = self.gross_wins / self.wins if self.wins else None
        avg_loss = self.gross_losses / self.losses if self.losses else None
        self.avg_win = avg_win
        self.avg_loss = avg_loss
        if avg_win and avg_loss and self.total:
            self.ev = (self.wins / self.total) * avg_win - (self.losses / self.total) * avg_loss
        else:
            self.ev = None

    def _breakdown(self, key: str) -> dict:
        result: dict[str, dict] = {}
        for t in self.trades:
            k = t.get(key) or "unknown"
            if k not in result:
                result[k] = {"trades": 0, "wins": 0, "pnl_usdt": 0.0}
            pnl = pnl_of(t)
            result[k]["trades"] += 1
            if pnl > 0:
                result[k]["wins"] += 1
            result[k]["pnl_usdt"] = round(result[k]["pnl_usdt"] + pnl, 4)
        for v in result.values():
            v["win_rate_pct"] = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else None
        return result

    def by_regime(self):
        return self._breakdown("ai_regime")

    def by_micro_gate_path(self):
        return self._breakdown("ai_micro_gate_path")

    def by_scenario(self):
        return self._breakdown("scenario")

    def by_exit_reason(self):
        return self._breakdown("exit_reason")

    def by_entry_logic(self):
        return self._breakdown("entry_logic_tag")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_trades": self.total,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate, 2) if self.win_rate is not None else None,
            "profit_factor": round(self.profit_factor, 4) if self.profit_factor not in (None, float("inf")) else self.profit_factor,
            "net_pnl_usdt": round(self.net_pnl, 4),
            "gross_wins_usdt": round(self.gross_wins, 4),
            "gross_losses_usdt": round(self.gross_losses, 4),
            "avg_win_usdt": round(self.avg_win, 4) if self.avg_win else None,
            "avg_loss_usdt": round(self.avg_loss, 4) if self.avg_loss else None,
            "ev_per_trade_usdt": round(self.ev, 4) if self.ev is not None else None,
            "by_regime": self.by_regime(),
            "by_path": self.by_micro_gate_path(),
            "by_scenario": self.by_scenario(),
            "by_exit_reason": self.by_exit_reason(),
            "by_entry_logic": self.by_entry_logic(),
        }


# ─── Pretty printer ──────────────────────────────────────────────────────────

def _fmt(val, fmt=".2f", na="—"):
    if val is None:
        return na
    if val == float("inf"):
        return "∞"
    return format(val, fmt)


def print_report(v3: CohortMetrics, legacy: CohortMetrics | None, target: float):
    SEP = "─" * 60
    print(SEP)
    print(f"  V3 COHORT ANALYTICS  (target WR ≥ {target:.0f}%)")
    print(SEP)
    print(f"  Total trades  : {v3.total}")
    print(f"  Wins          : {v3.wins}  |  Losses: {v3.losses}")
    wr_str = _fmt(v3.win_rate) + "%"
    gap = target - (v3.win_rate or 0)
    status = "✅ ON TARGET" if (v3.win_rate or 0) >= target else f"⚠️  {gap:.1f}% below target"
    print(f"  Win Rate      : {wr_str}  ← {status}")
    print(f"  Profit Factor : {_fmt(v3.profit_factor, '.3f')}")
    print(f"  Net PnL       : {_fmt(v3.net_pnl)} USDT")
    print(f"  EV/trade      : {_fmt(v3.ev, '.4f')} USDT")
    print()

    print("  By Regime:")
    for k, d in sorted(v3.by_regime().items()):
        print(f"    {k:12s}  {d['trades']:4d} trades  WR {_fmt(d['win_rate_pct'],'0.1f')}%  PnL {_fmt(d['pnl_usdt'], '.2f')} USDT")

    print()
    print("  By Entry Path (micro_gate vs standard):")
    for k, d in sorted(v3.by_micro_gate_path().items()):
        print(f"    {k:12s}  {d['trades']:4d} trades  WR {_fmt(d['win_rate_pct'],'0.1f')}%  PnL {_fmt(d['pnl_usdt'], '.2f')} USDT")

    print()
    print("  By Scenario:")
    for k, d in sorted(v3.by_scenario().items()):
        print(f"    Scen {k:4s}    {d['trades']:4d} trades  WR {_fmt(d['win_rate_pct'],'0.1f')}%  PnL {_fmt(d['pnl_usdt'], '.2f')} USDT")

    print()
    print("  By Exit Reason:")
    for k, d in sorted(v3.by_exit_reason().items(), key=lambda x: -x[1]["trades"]):
        print(f"    {k:30s}  {d['trades']:4d}  WR {_fmt(d['win_rate_pct'],'0.1f')}%")

    if legacy is not None:
        print()
        print(SEP)
        print("  LEGACY COHORT (pre-V3)")
        print(SEP)
        print(f"  Total trades  : {legacy.total}")
        print(f"  Win Rate      : {_fmt(legacy.win_rate)}%  |  PF {_fmt(legacy.profit_factor, '.3f')}  |  Net PnL {_fmt(legacy.net_pnl)} USDT")

    print(SEP)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="V3 Cohort Analytics")
    ap.add_argument("--json", action="store_true", dest="as_json", help="Output JSON instead of pretty report")
    ap.add_argument("--all-time", action="store_true", dest="all_time", help="Show legacy cohort too")
    ap.add_argument("--file", default=str(CLOSED_TRADES_FILE), help="Path to closed_trades.json")
    args = ap.parse_args()

    all_trades = load_trades(Path(args.file))
    # Strict tag-only filter: only trades with ai_prompt_version='v3' belong to V3 cohort.
    # Timestamp fallback removed — it caused pre-deployment trades to leak into V3.
    v3_trades = [t for t in all_trades if is_v3(t)]
    legacy_trades = [t for t in all_trades if not is_v3(t)]

    v3_metrics = CohortMetrics("v3", v3_trades)
    legacy_metrics = CohortMetrics("legacy", legacy_trades) if args.all_time else None

    if args.as_json:
        out = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "filter": "tag:ai_prompt_version=v3 (strict, no timestamp fallback)",
            "target_win_rate_pct": WIN_RATE_TARGET,
            "gap_to_target_pct": round(WIN_RATE_TARGET - (v3_metrics.win_rate or 0), 2),
            "on_track": (v3_metrics.win_rate or 0) >= WIN_RATE_TARGET,
            "v3": v3_metrics.to_dict(),
            "legacy": legacy_metrics.to_dict() if legacy_metrics else None,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_report(v3_metrics, legacy_metrics, WIN_RATE_TARGET)


if __name__ == "__main__":
    main()
