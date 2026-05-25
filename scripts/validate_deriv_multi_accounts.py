from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.utils.deriv_multi_accounts import (
    DERIV_MULTI_ACCOUNTS_ENV,
    load_multi_accounts_config,
    redacted_accounts_summary,
    resolve_multi_accounts_path,
    validate_multi_accounts_config,
)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Deriv multi-account JSON and optional DB user linkage."
    )
    parser.add_argument(
        "--file",
        default=os.getenv(DERIV_MULTI_ACCOUNTS_ENV, ""),
        help=f"JSON file path. Defaults to env {DERIV_MULTI_ACCOUNTS_ENV}.",
    )
    parser.add_argument(
        "--check-db",
        action="store_true",
        help="Validate that each enabled user_id exists and is active in PostgreSQL.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    return parser


async def _check_db_users(database_url: str, user_ids: list[str]) -> tuple[bool, str]:
    try:
        import asyncpg  # type: ignore
    except Exception as exc:  # pragma: no cover
        return False, f"asyncpg not available: {exc}"

    conn = None
    try:
        conn = await asyncpg.connect(database_url)
        rows = await conn.fetch(
            """
            SELECT id::text AS id, is_active, role
            FROM users
            WHERE id = ANY($1::uuid[])
            """,
            user_ids,
        )
    except Exception as exc:
        return False, f"db query failed: {exc}"
    finally:
        if conn is not None:
            await conn.close()

    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        found[str(row["id"])] = {
            "is_active": bool(row["is_active"]),
            "role": str(row["role"]),
        }

    missing = [uid for uid in user_ids if uid not in found]
    inactive = [uid for uid in user_ids if uid in found and not found[uid]["is_active"]]
    bad_role = [
        uid for uid in user_ids
        if uid in found and found[uid]["role"] not in {"client", "investor", "admin"}
    ]

    if missing or inactive or bad_role:
        parts: list[str] = []
        if missing:
            parts.append(f"missing={len(missing)}")
        if inactive:
            parts.append(f"inactive={len(inactive)}")
        if bad_role:
            parts.append(f"bad_role={len(bad_role)}")
        return False, ", ".join(parts)

    return True, f"linked_users={len(user_ids)}"


def _emit(results: list[CheckResult], summary: dict[str, Any], as_json: bool) -> int:
    ok = all(item.ok for item in results)

    if as_json:
        payload = {
            "ok": ok,
            "summary": summary,
            "checks": [
                {"name": item.name, "ok": item.ok, "detail": item.detail}
                for item in results
            ],
        }
        print(json.dumps(payload, ensure_ascii=True))
    else:
        print(f"[validate-deriv-multi-accounts] ok={ok}")
        for item in results:
            status = "PASS" if item.ok else "FAIL"
            print(f"- {status:<4} {item.name:<22} {item.detail}")

    return 0 if ok else 1


def main() -> int:
    args = _build_parser().parse_args()

    path = resolve_multi_accounts_path(args.file)
    if path is None:
        return _emit(
            [CheckResult("path", False, f"missing path (set --file or {DERIV_MULTI_ACCOUNTS_ENV})")],
            summary={},
            as_json=args.json,
        )

    results: list[CheckResult] = []
    summary: dict[str, Any] = {"path": str(path)}

    if not path.exists():
        return _emit(
            [CheckResult("file_exists", False, f"missing {path}")],
            summary=summary,
            as_json=args.json,
        )

    try:
        config = load_multi_accounts_config(path)
    except Exception as exc:
        return _emit(
            [CheckResult("json_parse", False, f"invalid JSON: {exc}")],
            summary=summary,
            as_json=args.json,
        )

    errors = validate_multi_accounts_config(config)
    results.append(
        CheckResult(
            "structure",
            ok=(len(errors) == 0),
            detail="ok" if not errors else "; ".join(errors),
        )
    )

    enabled_accounts = [acc for acc in config.accounts if acc.enabled]
    summary["total_accounts"] = len(config.accounts)
    summary["enabled_accounts"] = len(enabled_accounts)
    summary["accounts"] = redacted_accounts_summary(config)

    uuid_bad = [acc.user_id for acc in enabled_accounts if not _UUID_RE.match(acc.user_id)]
    results.append(
        CheckResult(
            "user_uuid_format",
            ok=(len(uuid_bad) == 0),
            detail=f"bad_uuid={len(uuid_bad)}",
        )
    )

    if args.check_db:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            results.append(CheckResult("db_user_link", False, "DATABASE_URL not set"))
        else:
            user_ids = sorted({acc.user_id for acc in enabled_accounts if _UUID_RE.match(acc.user_id)})
            ok, detail = asyncio.run(_check_db_users(database_url, user_ids))
            results.append(CheckResult("db_user_link", ok, detail))

    return _emit(results, summary, args.json)


if __name__ == "__main__":
    sys.exit(main())
