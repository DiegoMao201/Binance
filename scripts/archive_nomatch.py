"""archive_nomatch.py — Surgical pruning tool for no-match audit residuals.

USAGE
-----
    # Step 1: inspect what would be archived (dry-run)
    LOGS_DIR=./logs python -m scripts.archive_nomatch

    # Step 2: apply (archives trades listed in logs/nomatch_debug.json)
    LOGS_DIR=./logs python -m scripts.archive_nomatch --apply

WHAT IT DOES
------------
Reads logs/nomatch_debug.json (produced by reconstruct_ledger dry-run),
identifies every trade listed as "no_match_found", and marks them in
closed_trades.json as:

    "ledger_audit_status": "archived_no_exchange_record"
    "archived_at": <ISO timestamp>
    "archive_reason": <auto-classified reason>

The trade is NOT deleted.  It is tagged so that:
  1. reconstruct_ledger --skip-archived ignores them on future runs.
  2. PAMM allocator excludes them from the master_trades insert.
  3. The audit summary reaches 0 "No match", enabling a clean --apply.

CLASSIFICATION HEURISTICS (applied to each no-match trade)
------------------------------------------------------------
  dry_run_mode     : trade['mode'] in {dry_run, paper, test, simulation}
  no_order_id      : live_close.exchange_order_id is None/empty
  zero_fetch_count : _debug_fetch_count == 0  (API returned nothing for that symbol/window)
  low_notional     : notional_usdt < 5  (likely a dust test trade)
  external_manual  : mode == 'live' but no bot metadata (scenario is None/missing)
  unclassified     : catch-all
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.config import load_settings


def _classify(trade: dict[str, Any]) -> str:
    mode = str(trade.get("mode") or "").lower()
    if mode in {"dry_run", "paper", "test", "simulation"}:
        return "dry_run_mode"
    live_close = trade.get("live_close") or {}
    order_id = live_close.get("exchange_order_id")
    if not order_id:
        return "no_order_id"
    fetch_count = trade.get("_debug_fetch_count", -1)
    if fetch_count == 0:
        return "zero_fetch_count"
    notional = float(trade.get("notional_usdt") or 0.0)
    if notional < 5.0:
        return "low_notional_dust_trade"
    scenario = trade.get("scenario")
    if not scenario or scenario in {"recovered_live"}:
        return "external_manual"
    return "unclassified"


def _key(trade: dict[str, Any]) -> tuple:
    """Unique key matching the merge logic in reconstruct_ledger.py."""
    return (trade.get("opened_at"), trade.get("symbol"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger = logging.getLogger("archive_nomatch")

    settings = load_settings()
    nomatch_path: Path = settings.logs_dir / "nomatch_debug.json"
    closed_path: Path = settings.closed_trades_file

    if not nomatch_path.exists():
        logger.error("nomatch_debug.json not found at %s — run reconstruct_ledger dry-run first.", nomatch_path)
        return 2
    if not closed_path.exists():
        logger.error("closed_trades.json not found at %s.", closed_path)
        return 2

    report = json.loads(nomatch_path.read_text(encoding="utf-8"))
    nomatch_trades: list[dict[str, Any]] = report.get("trades", [])
    if not nomatch_trades:
        logger.info("nomatch_debug.json is empty — nothing to archive.")
        return 0

    closed: list[dict[str, Any]] = json.loads(closed_path.read_text(encoding="utf-8"))

    # Index closed_trades by key for fast lookup.
    closed_by_key: dict[tuple, int] = {_key(t): i for i, t in enumerate(closed)}

    archive_tag = datetime.now(timezone.utc).isoformat()
    operations: list[dict[str, Any]] = []

    for nm in nomatch_trades:
        k = _key(nm)
        idx = closed_by_key.get(k)
        if idx is None:
            logger.warning("  SKIP  — no matching closed_trade for key %s (already removed?)", k)
            continue
        reason = _classify(nm)
        operations.append({"key": k, "idx": idx, "reason": reason, "trade": nm})

    if not operations:
        logger.info("No trades to archive.")
        return 0

    logger.info("─" * 60)
    logger.info("NOMATCH ARCHIVE REPORT (surgically tagging %d trades):", len(operations))
    for op in operations:
        logger.info(
            "  [%s] opened_at=%-28s symbol=%-12s order_id=%-18s  fetch_count=%-4s  → reason: %s",
            "ARCHIVE" if args.apply else "DRY-RUN",
            str(op["key"][0])[:26],
            str(op["key"][1]),
            str((op["trade"].get("live_close") or {}).get("exchange_order_id") or "N/A"),
            str(op["trade"].get("_debug_fetch_count", "?")),
            op["reason"],
        )
    logger.info("─" * 60)

    if not args.apply:
        logger.info("DRY-RUN — no files written. Re-run with --apply to tag these trades.")
        return 0

    # Backup before write.
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = closed_path.with_suffix(f".{ts_tag}.prearchive.bak.json")
    shutil.copy2(closed_path, backup)
    logger.info("Backup written: %s", backup.name)

    for op in operations:
        idx = op["idx"]
        closed[idx] = {
            **closed[idx],
            "ledger_audit_status": "archived_no_exchange_record",
            "archived_at": archive_tag,
            "archive_reason": op["reason"],
        }

    closed_path.write_text(json.dumps(closed, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Archived %d trades in %s.", len(operations), closed_path)
    logger.info("Re-run 'python -m scripts.reconstruct_ledger' to confirm 0 no-match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
