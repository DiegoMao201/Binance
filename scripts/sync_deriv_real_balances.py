from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.data.deriv_client import DerivClient, DerivClientError
from src.utils.deriv_config import load_deriv_settings
from src.utils.deriv_multi_accounts import (
    DERIV_MULTI_ACCOUNTS_ENV,
    load_multi_accounts_config,
    redacted_accounts_summary,
    resolve_multi_accounts_path,
)


def _decimal_from_any(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _extract_balance(resp: dict[str, Any]) -> Decimal:
    balance_node = resp.get("balance") if isinstance(resp, dict) else None
    if isinstance(balance_node, dict):
        for key in ("balance", "available", "amount"):
            parsed = _decimal_from_any(balance_node.get(key))
            if parsed is not None:
                return parsed
    parsed_root = _decimal_from_any(resp.get("balance")) if isinstance(resp, dict) else None
    if parsed_root is not None:
        return parsed_root
    raise ValueError(f"Cannot extract balance from response: {resp}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch real balances for every enabled Deriv account in multi-account JSON "
            "and optionally sync users.balance_usdt in PostgreSQL."
        )
    )
    parser.add_argument(
        "--file",
        default=os.getenv(DERIV_MULTI_ACCOUNTS_ENV, ""),
        help=f"Path to multi-account JSON. Defaults to env {DERIV_MULTI_ACCOUNTS_ENV}.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL DSN. Defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply DB updates. Without this flag the script runs in preview mode.",
    )
    parser.add_argument(
        "--reset-deriv-audit",
        action="store_true",
        help=(
            "When used with --apply, deletes existing Deriv rows in "
            "user_trade_allocations and Deriv broker rows in ledger_transactions "
            "before syncing balances."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    return parser


async def _fetch_one_balance(account: Any) -> dict[str, Any]:
    base = load_deriv_settings()
    settings = replace(
        base,
        api_token=account.api_token,
        app_id=account.app_id,
        account_id=account.account_id,
        user_id=account.user_id,
    )

    client = DerivClient(settings)
    try:
        await client.connect()
        resp = await client.balance()
        balance = _extract_balance(resp)
        return {
            "alias": account.alias,
            "user_id": account.user_id,
            "account_id": account.account_id,
            "balance": str(balance),
            "ok": True,
        }
    except (DerivClientError, ValueError) as exc:
        return {
            "alias": account.alias,
            "user_id": account.user_id,
            "account_id": account.account_id,
            "ok": False,
            "error": str(exc),
        }
    finally:
        await client.close()


async def _sync_db(
    database_url: str,
    balances: list[dict[str, Any]],
    apply: bool,
    reset_deriv_audit: bool,
) -> dict[str, Any]:
    try:
        import asyncpg  # type: ignore
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"asyncpg not available: {exc}"}

    conn = None
    updates: list[dict[str, Any]] = []

    try:
        conn = await asyncpg.connect(database_url)

        if apply and reset_deriv_audit:
            await conn.execute("DELETE FROM user_trade_allocations WHERE broker = 'deriv'")
            await conn.execute(
                """
                DELETE FROM ledger_transactions
                WHERE broker = 'deriv'
                  AND type IN ('TRADE_PNL', 'BINANCE_FEE_REIMBURSEMENT', 'PERFORMANCE_FEE')
                """
            )

        for item in balances:
            if not item.get("ok"):
                updates.append(
                    {
                        "alias": item.get("alias"),
                        "user_id": item.get("user_id"),
                        "ok": False,
                        "error": "balance_fetch_failed",
                    }
                )
                continue

            uid = str(item["user_id"])
            target = Decimal(str(item["balance"]))

            row = await conn.fetchrow(
                """
                SELECT id::text AS id, balance_usdt, is_active
                FROM users
                WHERE id = $1::uuid
                """,
                uid,
            )
            if row is None:
                updates.append(
                    {
                        "alias": item.get("alias"),
                        "user_id": uid,
                        "ok": False,
                        "error": "user_not_found",
                    }
                )
                continue

            before = Decimal(str(row["balance_usdt"]))
            delta = target - before

            if apply:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE users
                        SET balance_usdt = $1::numeric,
                            entry_fee_pct = 0,
                            performance_fee_pct = 0.20,
                            is_active = TRUE,
                            updated_at = NOW()
                        WHERE id = $2::uuid
                        """,
                        str(target),
                        uid,
                    )

                    if delta > 0:
                        await conn.execute(
                            """
                            INSERT INTO ledger_transactions (user_id, type, broker, amount_usdt, description)
                            VALUES ($1::uuid, 'DEPOSIT', 'deriv', $2::numeric, $3)
                            """,
                            uid,
                            str(delta),
                            f"Sync saldo real Deriv ({item.get('alias')}): {before} -> {target}",
                        )
                    elif delta < 0:
                        await conn.execute(
                            """
                            INSERT INTO ledger_transactions (user_id, type, broker, amount_usdt, description)
                            VALUES ($1::uuid, 'WITHDRAWAL', 'deriv', $2::numeric, $3)
                            """,
                            uid,
                            str(abs(delta)),
                            f"Sync saldo real Deriv ({item.get('alias')}): {before} -> {target}",
                        )

            updates.append(
                {
                    "alias": item.get("alias"),
                    "user_id": uid,
                    "before": str(before),
                    "target": str(target),
                    "delta": str(delta),
                    "applied": bool(apply),
                    "ok": True,
                }
            )

        return {"ok": True, "updates": updates}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "updates": updates}
    finally:
        if conn is not None:
            await conn.close()


def _emit(payload: dict[str, Any], as_json: bool) -> int:
    ok = bool(payload.get("ok"))

    if as_json:
        print(json.dumps(payload, ensure_ascii=True))
        return 0 if ok else 1

    print(f"[sync-deriv-real-balances] ok={ok}")
    if "error" in payload:
        print(f"- error: {payload['error']}")

    for item in payload.get("balances", []):
        if item.get("ok"):
            print(
                f"- BALANCE alias={item.get('alias')} user={str(item.get('user_id'))[:8]} "
                f"account={item.get('account_id')} balance={item.get('balance')}"
            )
        else:
            print(
                f"- BALANCE_FAIL alias={item.get('alias')} user={str(item.get('user_id'))[:8]} "
                f"error={item.get('error')}"
            )

    db = payload.get("db") or {}
    for row in db.get("updates", []):
        if row.get("ok"):
            print(
                f"- DB alias={row.get('alias')} user={str(row.get('user_id'))[:8]} "
                f"before={row.get('before')} target={row.get('target')} delta={row.get('delta')} "
                f"applied={row.get('applied')}"
            )
        else:
            print(
                f"- DB_FAIL alias={row.get('alias')} user={str(row.get('user_id'))[:8]} "
                f"error={row.get('error')}"
            )

    return 0 if ok else 1


async def _run(args: argparse.Namespace) -> int:
    path = resolve_multi_accounts_path(args.file)
    if path is None:
        return _emit(
            {
                "ok": False,
                "error": f"Missing account file path (set --file or {DERIV_MULTI_ACCOUNTS_ENV})",
            },
            args.json,
        )

    path = Path(path)
    if not path.exists():
        return _emit({"ok": False, "error": f"File not found: {path}"}, args.json)

    try:
        config = load_multi_accounts_config(path)
    except Exception as exc:
        return _emit({"ok": False, "error": f"Invalid account config: {exc}"}, args.json)

    enabled_accounts = [acc for acc in config.accounts if acc.enabled]
    if not enabled_accounts:
        return _emit({"ok": False, "error": "No enabled accounts in config."}, args.json)

    fetch_jobs = [_fetch_one_balance(acc) for acc in enabled_accounts]
    balances = await asyncio.gather(*fetch_jobs)

    db_summary: dict[str, Any] = {
        "ok": True,
        "updates": [],
        "note": "DB sync skipped (preview mode)",
    }

    if args.apply or args.database_url:
        database_url = str(args.database_url or "").strip()
        if not database_url:
            return _emit(
                {
                    "ok": False,
                    "error": "DATABASE_URL missing. Set --database-url or env DATABASE_URL.",
                    "balances": balances,
                },
                args.json,
            )
        db_summary = await _sync_db(
            database_url=database_url,
            balances=balances,
            apply=bool(args.apply),
            reset_deriv_audit=bool(args.reset_deriv_audit and args.apply),
        )

    payload = {
        "ok": all(item.get("ok") for item in balances) and bool(db_summary.get("ok", True)),
        "config_path": str(path),
        "accounts": redacted_accounts_summary(config),
        "balances": balances,
        "db": db_summary,
    }
    return _emit(payload, args.json)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
