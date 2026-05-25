from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DERIV_MULTI_ACCOUNTS_ENV = "DERIV_MULTI_ACCOUNTS_FILE"


@dataclass(frozen=True, slots=True)
class DerivAccountCredential:
    """Single Deriv account credential + PAMM linkage."""

    alias: str
    account_id: str
    api_token: str
    user_id: str
    app_id: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class DerivMultiAccountsConfig:
    """Collection of configured Deriv accounts for future fanout/multi-runner."""

    source_path: Path
    accounts: tuple[DerivAccountCredential, ...]


def resolve_multi_accounts_path(value: str | None = None) -> Path | None:
    """Resolve the multi-account JSON path from argument/env var."""
    raw = (value if value is not None else os.getenv(DERIV_MULTI_ACCOUNTS_ENV, "")).strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _normalise_alias(raw_alias: str, account_id: str) -> str:
    alias = (raw_alias or "").strip()
    if alias:
        return alias
    tail = account_id[-6:] if len(account_id) >= 6 else account_id
    return f"acct_{tail}"


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _extract_accounts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        accounts = payload.get("accounts")
        if isinstance(accounts, list):
            return [item for item in accounts if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def load_multi_accounts_config(path: Path) -> DerivMultiAccountsConfig:
    """Load and parse the multi-account JSON file."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = _extract_accounts(payload)
    default_app_id = os.getenv("DERIV_APP_ID", "1089").strip() or "1089"

    parsed: list[DerivAccountCredential] = []
    for row in rows:
        account_id = str(row.get("account_id", "")).strip()
        api_token = str(row.get("api_token", "")).strip()
        user_id = str(row.get("user_id", "")).strip()
        app_id = str(row.get("app_id", default_app_id)).strip() or default_app_id
        alias = _normalise_alias(str(row.get("alias") or row.get("name") or ""), account_id)
        enabled = _as_bool(row.get("enabled"), default=True)

        parsed.append(
            DerivAccountCredential(
                alias=alias,
                account_id=account_id,
                api_token=api_token,
                user_id=user_id,
                app_id=app_id,
                enabled=enabled,
            )
        )

    return DerivMultiAccountsConfig(source_path=path, accounts=tuple(parsed))


def validate_multi_accounts_config(config: DerivMultiAccountsConfig) -> list[str]:
    """Return a list of validation errors. Empty list means valid."""
    errors: list[str] = []

    if not config.accounts:
        errors.append("accounts list is empty")
        return errors

    alias_seen: set[str] = set()
    account_seen: set[str] = set()
    user_seen: set[str] = set()

    for idx, account in enumerate(config.accounts, start=1):
        prefix = f"account[{idx}] ({account.alias})"

        if not account.enabled:
            # Disabled accounts may keep partial draft values.
            continue

        if not account.account_id:
            errors.append(f"{prefix}: account_id is required")
        if not account.api_token:
            errors.append(f"{prefix}: api_token is required")
        if not account.user_id:
            errors.append(f"{prefix}: user_id is required")

        if account.alias in alias_seen:
            errors.append(f"{prefix}: duplicate alias '{account.alias}'")
        else:
            alias_seen.add(account.alias)

        if account.account_id:
            if account.account_id in account_seen:
                errors.append(f"{prefix}: duplicate account_id '{account.account_id}'")
            else:
                account_seen.add(account.account_id)

        if account.user_id:
            if account.user_id in user_seen:
                errors.append(f"{prefix}: duplicate user_id '{account.user_id}'")
            else:
                user_seen.add(account.user_id)

    if all(not account.enabled for account in config.accounts):
        errors.append("no enabled accounts found")

    return errors


def redacted_accounts_summary(config: DerivMultiAccountsConfig) -> list[dict[str, Any]]:
    """Safe summary for logs/CLI output without leaking full secrets."""
    out: list[dict[str, Any]] = []
    for account in config.accounts:
        token_tail = account.api_token[-6:] if account.api_token else ""
        out.append(
            {
                "alias": account.alias,
                "enabled": account.enabled,
                "account_id": account.account_id,
                "user_id": account.user_id,
                "app_id": account.app_id,
                "token_tail": token_tail,
            }
        )
    return out
