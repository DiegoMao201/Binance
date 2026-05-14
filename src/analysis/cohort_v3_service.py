"""
cohort_v3_service.py
────────────────────
V3 Cohort Analytics Engine — Python service layer.

Queries v_cohort_v3_metrics and v_cohort_v3_breakdown from PostgreSQL and
returns (or writes) the complete JSON payload consumed by the Next.js
frontend dashboard.

PRECISION CONTRACT
  asyncpg maps PostgreSQL NUMERIC columns to Python Decimal objects.
  All internal aggregation stays in Decimal arithmetic — float conversion
  happens only at the final serialisation step (_to_float).

USAGE
  # As a one-shot script (writes cohort_v3_metrics.json to LOGS_DIR)
  DATABASE_URL=postgresql://... LOGS_DIR=/data/logs python -m src.analysis.cohort_v3_service

  # Programmatic — called by main_loop after every trade close
  from src.analysis.cohort_v3_service import run_and_cache
  await run_and_cache(dsn=settings.database_url, logs_dir=settings.logs_dir)

DEPENDENCIES
  asyncpg>=0.29  (added to requirements.txt)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import asyncpg
    _HAS_ASYNCPG = True
except ImportError:
    _HAS_ASYNCPG = False


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _to_float(v: Decimal | None, places: int = 4) -> float | None:
    """Cast asyncpg NUMERIC (Decimal) to rounded float for JSON output.

    NEVER called inside the aggregation logic — only at the boundary where
    the result dict is assembled.  This keeps all intermediate arithmetic
    in Decimal precision.
    """
    if v is None:
        return None
    return float(round(v, places))


def _json_default(obj: Any) -> Any:
    """json.dumps default — handle Decimal residuals defensively."""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


# ── Empty payload (edge-case: zero trades) ────────────────────────────────────

def _empty_metrics() -> dict[str, Any]:
    """Return KPI-initialised payload when the V3 cohort has no trades yet.

    All KPIs are 0 or None (not 0) to let the frontend distinguish
    "not enough data" from "zero result".
    """
    return {
        "total_trades":      0,
        "wins":              0,
        "losses":            0,
        "win_rate_pct":      None,   # KPI 1
        "profit_factor":     None,   # KPI 2
        "ev_per_trade_usdt": None,   # KPI 3
        "avg_win_usdt":      None,   # KPI 4
        "avg_loss_usdt":     None,   # KPI 5
        "net_pnl_usdt":      0.0,
        "gross_wins_usdt":   0.0,
        "gross_losses_usdt": 0.0,
        "total_fees_usdt":   0.0,
        "by_regime":         {},
        "by_path":           {},
        "by_exit_reason":    {},
    }


# ── Breakdown pivot ───────────────────────────────────────────────────────────

def _pivot_breakdown(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Convert flat v_cohort_v3_breakdown rows into the nested dict expected
    by the frontend.

    Input row shape (from asyncpg):
        dimension TEXT, bucket TEXT, n_trades BIGINT, n_wins BIGINT,
        win_rate_pct NUMERIC, net_pnl_usdt NUMERIC

    Output shape:
        {
          "by_regime":      { "range": {"trades": 8, "wins": 5, "pnl": 3.12, "win_rate_pct": 62.5}, ... },
          "by_path":        { ... },
          "by_exit_reason": { ... },
        }
    """
    result: dict[str, dict[str, Any]] = {
        "by_regime":      {},
        "by_path":        {},
        "by_exit_reason": {},
    }

    for row in rows:
        dim    = row["dimension"]
        bucket = row["bucket"] or "unknown"
        if dim not in result:
            continue  # unknown dimension — skip gracefully

        result[dim][bucket] = {
            "trades":       int(row["n_trades"]),
            "wins":         int(row["n_wins"]),
            "pnl":          _to_float(row["net_pnl_usdt"],  2),
            "win_rate_pct": _to_float(row["win_rate_pct"],  2),
        }

    return result


# ── Core DB fetch ─────────────────────────────────────────────────────────────

async def _fetch_from_db(dsn: str) -> dict[str, Any]:
    """Open a single asyncpg connection, run both view queries concurrently,
    and return the assembled payload.

    Uses asyncio.gather for a single round-trip latency instead of two
    sequential queries.  Both views are read-only so there is no isolation
    concern.
    """
    conn = await asyncpg.connect(dsn)
    try:
        kpis_row, breakdown_rows = await asyncio.gather(
            conn.fetchrow("SELECT * FROM v_cohort_v3_metrics"),
            conn.fetch(
                "SELECT * FROM v_cohort_v3_breakdown ORDER BY dimension, n_trades DESC"
            ),
        )
    finally:
        await conn.close()

    # ── Edge-case: view returned no row (empty table) ─────────────────────
    if kpis_row is None or int(kpis_row["n_trades"]) == 0:
        return _empty_metrics()

    breakdown = _pivot_breakdown(list(breakdown_rows))

    # ── Assemble final payload — NUMERIC → float only here ────────────────
    return {
        "total_trades":      int(kpis_row["n_trades"]),
        "wins":              int(kpis_row["n_wins"]),
        "losses":            int(kpis_row["n_losses"]),

        # KPI 1 — Win Rate (0-100 %)
        "win_rate_pct":      _to_float(kpis_row["win_rate_pct"],      2),

        # KPI 2 — Profit Factor
        "profit_factor":     _to_float(kpis_row["profit_factor"],     4),

        # KPI 3 — Expected Value per Trade
        "ev_per_trade_usdt": _to_float(kpis_row["ev_per_trade_usdt"], 4),

        # KPI 4 & 5 — Average Win / Loss
        "avg_win_usdt":      _to_float(kpis_row["avg_win_usdt"],      4),
        "avg_loss_usdt":     _to_float(kpis_row["avg_loss_usdt"],     4),

        # Totals
        "net_pnl_usdt":      _to_float(kpis_row["total_net_pnl_usdt"],  2),
        "gross_wins_usdt":   _to_float(kpis_row["gross_wins_usdt"],     2),
        "gross_losses_usdt": _to_float(kpis_row["gross_losses_usdt"],   2),
        "total_fees_usdt":   _to_float(kpis_row["total_fees_usdt"],     2),

        # Breakdowns
        **breakdown,
    }


# ── Cache writer ──────────────────────────────────────────────────────────────

def write_metrics_cache(metrics: dict[str, Any], logs_dir: Path) -> Path:
    """Persist the payload as cohort_v3_metrics.json so the Next.js frontend
    can read it without a direct PostgreSQL connection.

    Written atomically (write to .tmp, then rename) so the frontend never
    reads a partially-written file.
    """
    envelope = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cohort":        "v3",
        "filter":        (
            "ai_prompt_version='v3' "
            "AND exchange_order_id IS NOT NULL "
            "AND ledger_audited_at IS NOT NULL"
        ),
        "metrics": metrics,
    }

    out_path = logs_dir / "cohort_v3_metrics.json"
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    tmp_path.replace(out_path)
    return out_path


# ── Public entry points ───────────────────────────────────────────────────────

async def fetch_cohort_v3_metrics(dsn: str) -> dict[str, Any]:
    """Public coroutine — call from main_loop or any async context.

    Returns the KPI payload dict (no I/O side effects).
    """
    if not _HAS_ASYNCPG:
        raise RuntimeError(
            "asyncpg is required for cohort_v3_service. "
            "Run: pip install 'asyncpg>=0.29'"
        )
    return await _fetch_from_db(dsn)


async def run_and_cache(dsn: str, logs_dir: Path) -> dict[str, Any]:
    """Fetch from DB + write cache file.  Idempotent — safe to call on every
    trade close.

    Returns the metrics dict (can be logged or forwarded to a Telegram alert).
    """
    metrics = await fetch_cohort_v3_metrics(dsn)
    write_metrics_cache(metrics, logs_dir)
    return metrics


# ── CLI entrypoint ────────────────────────────────────────────────────────────

async def _main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL environment variable not set.", file=sys.stderr)
        print("  Example: DATABASE_URL=postgresql://user:pass@host/db", file=sys.stderr)
        return 1

    logs_dir = Path(os.environ.get("LOGS_DIR", "./logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        metrics = await run_and_cache(dsn=dsn, logs_dir=logs_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    out_path = logs_dir / "cohort_v3_metrics.json"
    print(f"cohort_v3_metrics.json written → {out_path}")
    print(json.dumps(metrics, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
