"""reconstruct_ledger.py — Retrospective trade-ledger reconciliation tool.

USAGE
-----
    python -m scripts.reconstruct_ledger              # dry-run (no writes)
    python -m scripts.reconstruct_ledger --apply      # write corrected file
    python -m scripts.reconstruct_ledger --apply --since 2026-04-01

WHAT IT DOES
------------
1. Loads every closed trade from logs/closed_trades.json (the local "truth" today).
2. For each trade, queries Binance via `ccxt.fetch_my_trades(symbol, since)` looking
   for the matching SELL execution by exchange_order_id, falling back to a fuzzy
   match on amount/price/timestamp when the order id is missing (legacy trades).
3. Recomputes the **NET** PnL using the on-exchange truth: real fill price, real
   filled amount, real fees (in quote currency).
4. Tags every corrected trade with:
        ledger_reconstructed_at: ISO timestamp of this audit
        ledger_pnl_delta_usdt:   audited_pnl - stored_pnl  (so cohort dashboards
                                  can quantify the historical drift)
5. **Auto-backs-up** the file to logs/closed_trades.<ts>.bak.json before writing.

WHY
---
External-reconcile and async-close trades historically used a stale mark-price
to estimate PnL.  This drift contaminates V3 cohort analytics.  This script
realigns the local ledger with Binance's authoritative state and is safe to run
repeatedly (idempotent — the same trade always reconciles to the same numbers).

CONSTRAINTS
-----------
- Rate-limit: 250 ms between fetch_my_trades calls (well under Binance's 1200/min
  spot weight cap).  Adjustable via --rate-ms.
- Auto-backup before any write (CSV-flat dump of the original JSON).
- Uses Decimal for arithmetic; JSON output rounded to 6 decimals on persist.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

import ccxt

from src.utils.config import load_settings

# 28-digit precision is plenty for spot trades (BTC max 8 dec, USDT 8 dec).
getcontext().prec = 28

# Default Binance taker fee on spot (0.10%).  Used only when ccxt does not
# return a fee object on a trade (rare but possible for very old executions).
_FALLBACK_TAKER_FEE_PCT = Decimal("0.001")

# Tolerance for fuzzy matching legacy trades that lack exchange_order_id.
_AMOUNT_REL_TOL = Decimal("0.005")    # 0.5%
_PRICE_REL_TOL  = Decimal("0.003")    # 0.3%
_TIME_WINDOW_MS = 5 * 60 * 1000       # ±5 min around the closed_at timestamp


# ────────────────────────── helpers ──────────────────────────────────────────


def _to_dec(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _iso_to_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        # Strip trailing Z if present.
        clean = ts.replace("Z", "+00:00")
        return int(datetime.fromisoformat(clean).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _close(a: Decimal, b: Decimal, rel_tol: Decimal) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= rel_tol


# ────────────────────────── core reconciliation ──────────────────────────────


def _build_ccxt_client() -> ccxt.binance:
    settings = load_settings()
    cfg: dict[str, Any] = {
        "apiKey": settings.binance_api_key,
        "secret": settings.binance_api_secret,
        "enableRateLimit": True,
        "timeout": 15_000,
        "options": {"defaultType": "spot"},
    }
    if settings.binance_proxy_url:
        cfg["proxies"] = {
            "http": settings.binance_proxy_url,
            "https": settings.binance_proxy_url,
        }
    return ccxt.binance(cfg)


def _find_matching_fill(
    trades: list[dict[str, Any]],
    *,
    expected_order_id: str | None,
    expected_amount: Decimal,
    expected_price: Decimal,
    expected_ts_ms: int | None,
    side: str,
) -> dict[str, Any] | None:
    """Locate the on-exchange trade that matches our local close.

    Strategy 1: exact match by orderId  (most reliable, used for new trades).
    Strategy 2: fuzzy match by (timestamp ±5 min, amount within 0.5%, price within 0.3%).
    """
    side_lower = side.lower()

    # Strategy 1 — order id match.
    if expected_order_id:
        for t in trades:
            if str(t.get("order")) == str(expected_order_id):
                return t

    # Strategy 2 — fuzzy.  Score candidates by closeness; pick best.
    best: tuple[float, dict[str, Any]] | None = None
    for t in trades:
        if side_lower and str(t.get("side", "")).lower() != side_lower:
            continue
        amount = _to_dec(t.get("amount"))
        price  = _to_dec(t.get("price"))
        ts_ms  = int(t.get("timestamp") or 0)
        if not _close(amount, expected_amount, _AMOUNT_REL_TOL):
            continue
        if not _close(price, expected_price, _PRICE_REL_TOL):
            continue
        if expected_ts_ms is not None and abs(ts_ms - expected_ts_ms) > _TIME_WINDOW_MS:
            continue
        # Composite distance score (lower is better).
        a_dist = float(abs(amount - expected_amount) / max(expected_amount, Decimal("1e-12")))
        p_dist = float(abs(price - expected_price) / max(expected_price, Decimal("1e-12")))
        t_dist = abs(ts_ms - (expected_ts_ms or ts_ms)) / 1000.0
        score = a_dist + p_dist + t_dist / 600.0
        if best is None or score < best[0]:
            best = (score, t)
    return best[1] if best else None


def _audit_trade(
    trade: dict[str, Any],
    exchange: ccxt.binance,
    logger: logging.Logger,
    rate_sleep_s: float,
) -> dict[str, Any]:
    """Returns a *copy* of the trade with audited fields filled in."""
    out = dict(trade)
    symbol = str(trade.get("symbol") or "")
    if not symbol:
        out["ledger_audit_status"] = "skipped_no_symbol"
        return out

    # Build (since) window: from opened_at - 1h to closed_at + 1h.  Binance
    # fetch_my_trades is paginated; we use a reasonable window to limit weight.
    opened_ms = _iso_to_ms(str(trade.get("opened_at") or ""))
    closed_ms = _iso_to_ms(str(trade.get("closed_at") or ""))
    if closed_ms is None:
        out["ledger_audit_status"] = "skipped_no_closed_at"
        return out
    since_ms = (opened_ms or closed_ms) - 60 * 60 * 1000  # 1 h before opened

    try:
        trades = exchange.fetch_my_trades(symbol, since=since_ms, limit=200)
    except ccxt.BaseError as exc:
        logger.warning("fetch_my_trades failed for %s: %s", symbol, exc)
        out["ledger_audit_status"] = f"fetch_error: {exc.__class__.__name__}"
        time.sleep(rate_sleep_s)
        return out

    # Rate-limit politely between API calls.
    time.sleep(rate_sleep_s)

    live_close = trade.get("live_close") or {}
    expected_order_id = live_close.get("exchange_order_id")
    expected_amount   = _to_dec(live_close.get("filled_amount") or trade.get("amount"))
    expected_price    = _to_dec(trade.get("exit_price") or live_close.get("avg_price"))

    match = _find_matching_fill(
        trades,
        expected_order_id=str(expected_order_id) if expected_order_id else None,
        expected_amount=expected_amount,
        expected_price=expected_price,
        expected_ts_ms=closed_ms,
        side="sell",  # close of a long is a sell
    )

    if match is None:
        out["ledger_audit_status"] = "no_match_found"
        return out

    # ── Recompute Net PnL from the authoritative fill ────────────────────────
    real_price  = _to_dec(match.get("price"))
    real_amount = _to_dec(match.get("amount"))
    fee_obj     = match.get("fee") or {}
    fee_cost    = _to_dec(fee_obj.get("cost"))
    fee_currency = str(fee_obj.get("currency") or "")
    quote = symbol.split("/")[1] if "/" in symbol else "USDT"

    # If fee was paid in BNB or another currency we cannot convert without an
    # extra ticker call — fall back to a synthetic estimate to stay rate-safe.
    if fee_currency.upper() != quote.upper() and fee_currency != "":
        fee_quote = real_price * real_amount * _FALLBACK_TAKER_FEE_PCT
        fee_currency_used = f"{fee_currency}_fallback_to_{quote}"
    else:
        fee_quote = fee_cost if fee_cost > 0 else real_price * real_amount * _FALLBACK_TAKER_FEE_PCT
        fee_currency_used = quote

    entry_price  = _to_dec(trade.get("entry_price"))
    entry_fee    = entry_price * real_amount * _FALLBACK_TAKER_FEE_PCT  # synthetic — most legacy trades lack the entry fee
    gross_pnl    = (real_price - entry_price) * real_amount
    audited_pnl  = gross_pnl - fee_quote - entry_fee

    stored_pnl = _to_dec(trade.get("pnl_usdt"))
    delta = audited_pnl - stored_pnl

    out["ledger_audit_status"]       = "ok"
    out["ledger_audit_match_method"] = "order_id" if expected_order_id and str(match.get("order")) == str(expected_order_id) else "fuzzy"
    out["ledger_reconstructed_at"]   = datetime.now(timezone.utc).isoformat()
    out["ledger_pnl_delta_usdt"]     = float(round(delta, 6))
    out["ledger_audited_exit_price"] = float(round(real_price, 8))
    out["ledger_audited_amount"]     = float(round(real_amount, 8))
    out["ledger_audited_fee_usdt"]   = float(round(fee_quote, 6))
    out["ledger_audited_fee_currency"] = fee_currency_used
    out["pnl_usdt_legacy"]           = float(stored_pnl)
    out["pnl_usdt"]                  = float(round(audited_pnl, 6))

    notional = entry_price * real_amount
    if notional > 0:
        out["pnl_pct"] = float(round(audited_pnl / notional, 6))

    return out


# ────────────────────────── backup helpers ───────────────────────────────────


def _backup_to_csv(closed_trades: list[dict[str, Any]], dest: Path) -> None:
    if not closed_trades:
        return
    keys: set[str] = set()
    for t in closed_trades:
        keys.update(t.keys())
    columns = sorted(keys)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for t in closed_trades:
            row = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in t.items()}
            writer.writerow(row)


# ────────────────────────── CLI driver ───────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Persist corrections (default: dry-run)")
    parser.add_argument("--since", type=str, help="ISO date (YYYY-MM-DD) — only audit trades closed on/after this date")
    parser.add_argument("--rate-ms", type=int, default=250, help="ms to sleep between Binance API calls (default 250)")
    parser.add_argument("--limit", type=int, default=0, help="Audit only the first N trades (0 = all)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger = logging.getLogger("reconstruct_ledger")

    settings = load_settings()
    closed_path: Path = settings.closed_trades_file
    if not closed_path.exists():
        logger.error("closed_trades file not found at %s", closed_path)
        return 2

    raw = json.loads(closed_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        logger.error("Expected a JSON list at %s, got %s", closed_path, type(raw).__name__)
        return 2

    since_ms: int | None = None
    if args.since:
        try:
            since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            logger.error("Invalid --since (use YYYY-MM-DD): %s", args.since)
            return 2

    target = []
    for t in raw:
        if since_ms is not None:
            ts = _iso_to_ms(str(t.get("closed_at") or ""))
            if ts is None or ts < since_ms:
                continue
        target.append(t)

    if args.limit > 0:
        target = target[: args.limit]

    logger.info("Auditing %d closed trades (of %d total in file)", len(target), len(raw))

    exchange = _build_ccxt_client()
    rate_sleep_s = max(args.rate_ms, 50) / 1000.0

    audited: list[dict[str, Any]] = []
    deltas: list[float] = []
    n_ok, n_skip, n_nomatch, n_err = 0, 0, 0, 0

    for idx, trade in enumerate(target, 1):
        new_trade = _audit_trade(trade, exchange, logger, rate_sleep_s)
        audited.append(new_trade)
        status = new_trade.get("ledger_audit_status", "")
        if status == "ok":
            n_ok += 1
            deltas.append(float(new_trade.get("ledger_pnl_delta_usdt", 0.0)))
        elif status.startswith("skipped"):
            n_skip += 1
        elif status == "no_match_found":
            n_nomatch += 1
        else:
            n_err += 1
        if idx % 25 == 0:
            logger.info("  progress: %d/%d (ok=%d nomatch=%d skip=%d err=%d)", idx, len(target), n_ok, n_nomatch, n_skip, n_err)

    total_delta = sum(deltas)
    logger.info("─" * 60)
    logger.info("AUDIT SUMMARY:")
    logger.info("  Reconciled OK : %d", n_ok)
    logger.info("  No match      : %d", n_nomatch)
    logger.info("  Skipped       : %d", n_skip)
    logger.info("  Errors        : %d", n_err)
    logger.info("  Σ ΔPnL (audit - stored) = %.4f USDT", total_delta)
    logger.info("─" * 60)

    # Merge audited results back into the full list (preserving non-target trades).
    if args.since or args.limit > 0:
        audited_by_key = {(t.get("opened_at"), t.get("symbol")): t for t in audited}
        merged: list[dict[str, Any]] = []
        for t in raw:
            key = (t.get("opened_at"), t.get("symbol"))
            merged.append(audited_by_key.get(key, t))
        final = merged
    else:
        final = audited

    if not args.apply:
        logger.info("DRY-RUN — no files written.  Re-run with --apply to persist.")
        return 0

    # ── Auto-backup before write (JSON + flat CSV) ──────────────────────────
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_backup = closed_path.with_suffix(f".{ts_tag}.bak.json")
    csv_backup  = closed_path.with_suffix(f".{ts_tag}.bak.csv")
    shutil.copy2(closed_path, json_backup)
    _backup_to_csv(raw, csv_backup)
    logger.info("Backups written: %s | %s", json_backup.name, csv_backup.name)

    closed_path.write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Corrected ledger written to %s (%d entries)", closed_path, len(final))
    return 0


if __name__ == "__main__":
    sys.exit(main())
