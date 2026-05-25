from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from src.utils.deriv_multi_accounts import (
    DERIV_MULTI_ACCOUNTS_ENV,
    load_multi_accounts_config,
    redacted_accounts_summary,
    resolve_multi_accounts_path,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/update one account entry in Deriv multi-account JSON."
    )
    parser.add_argument(
        "--file",
        default=os.getenv(DERIV_MULTI_ACCOUNTS_ENV, ""),
        help=f"JSON file path. Defaults to env {DERIV_MULTI_ACCOUNTS_ENV}.",
    )
    parser.add_argument("--alias", required=True, help="Stable alias (e.g. principal, esposa)")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--api-token", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--app-id", default=os.getenv("DERIV_APP_ID", "1089") or "1089")
    parser.add_argument(
        "--disabled",
        action="store_true",
        help="Store the account disabled (enabled=false).",
    )
    return parser


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"accounts": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        accounts = payload.get("accounts")
        if isinstance(accounts, list):
            return {"accounts": [row for row in accounts if isinstance(row, dict)]}
    if isinstance(payload, list):
        return {"accounts": [row for row in payload if isinstance(row, dict)]}
    raise ValueError("invalid payload: expected object with accounts[] or array")


def main() -> int:
    args = _build_parser().parse_args()

    path = resolve_multi_accounts_path(args.file)
    if path is None:
        print(f"missing path (set --file or {DERIV_MULTI_ACCOUNTS_ENV})")
        return 2

    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        payload = _load_raw(path)
    except Exception as exc:
        print(f"failed to read {path}: {exc}")
        return 2

    accounts: list[dict[str, Any]] = payload.get("accounts", [])
    new_row: dict[str, Any] = {
        "alias": args.alias.strip(),
        "account_id": args.account_id.strip(),
        "api_token": args.api_token.strip(),
        "user_id": args.user_id.strip(),
        "app_id": str(args.app_id).strip() or "1089",
        "enabled": not args.disabled,
    }

    replaced = False
    for idx, row in enumerate(accounts):
        alias = str(row.get("alias", "")).strip()
        account_id = str(row.get("account_id", "")).strip()
        if alias == new_row["alias"] or account_id == new_row["account_id"]:
            accounts[idx] = new_row
            replaced = True
            break

    if not replaced:
        accounts.append(new_row)

    path.write_text(json.dumps({"accounts": accounts}, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    config = load_multi_accounts_config(path)
    summary = redacted_accounts_summary(config)
    print(
        json.dumps(
            {
                "path": str(path),
                "updated_alias": new_row["alias"],
                "replaced": replaced,
                "accounts": summary,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
