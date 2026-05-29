from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.data.deriv_client import DerivClient, DerivClientError
from src.utils.deriv_config import DerivSettings
from src.utils.deriv_multi_accounts import (
    load_multi_accounts_config,
    redacted_accounts_summary,
    resolve_multi_accounts_path,
    validate_multi_accounts_config,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _MirrorRuntime:
    alias: str
    account_id: str
    user_id: str
    client: DerivClient
    _connected: bool = False
    _connect_lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self._connected:
                return
            await self.client.connect()
            self._connected = True


class DerivMirrorClient:
    """Client wrapper that mirrors principal buy/sell to follower accounts.

    Principal account remains the only source of:
    - open-contract tracking
    - trailing/monitoring decisions
    - closed records + webhook audit flow

    Mirror accounts only receive entry and exit execution requests.
    """

    def __init__(
        self,
        primary_client: DerivClient,
        mirror_runtimes: list[_MirrorRuntime],
        *,
        enabled: bool,
        state_file: Path | None = None,
    ) -> None:
        self._primary = primary_client
        self._mirrors = mirror_runtimes
        self._enabled = enabled and bool(mirror_runtimes)
        self._state_file = state_file
        self._mirror_contract_links: dict[int, list[dict[str, Any]]] = {}
        if self._enabled:
            self._load_links()

    @classmethod
    def from_settings(
        cls,
        primary_client: DerivClient,
        settings: DerivSettings,
    ) -> "DerivMirrorClient":
        _state_file = settings.logs_dir / "deriv_mirror_links.json"
        path = resolve_multi_accounts_path()
        if path is None:
            _LOGGER.info("[mirror] disabled: DERIV_MULTI_ACCOUNTS_FILE is not set")
            return cls(primary_client, [], enabled=False, state_file=_state_file)
        if not path.exists():
            _LOGGER.warning("[mirror] disabled: multi-account file not found at %s", path)
            return cls(primary_client, [], enabled=False, state_file=_state_file)

        try:
            config = load_multi_accounts_config(path)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[mirror] disabled: cannot parse %s: %s", path, exc)
            return cls(primary_client, [], enabled=False, state_file=_state_file)

        errors = validate_multi_accounts_config(config)
        if errors:
            _LOGGER.warning("[mirror] disabled: invalid config (%s)", "; ".join(errors))
            return cls(primary_client, [], enabled=False, state_file=_state_file)

        mirror_runtimes: list[_MirrorRuntime] = []
        principal_account = settings.account_id.strip()

        for row in config.accounts:
            if not row.enabled:
                continue
            if row.account_id.strip() == principal_account:
                continue
            try:
                mirror_settings: DerivSettings = copy.deepcopy(settings)
                mirror_settings.account_id = row.account_id.strip()
                mirror_settings.api_token = row.api_token.strip()
                mirror_settings.user_id = row.user_id.strip()
                mirror_settings.app_id = row.app_id.strip() or settings.app_id
                mirror_client = DerivClient(mirror_settings)
                mirror_runtimes.append(
                    _MirrorRuntime(
                        alias=row.alias,
                        account_id=mirror_settings.account_id,
                        user_id=mirror_settings.user_id,
                        client=mirror_client,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "[mirror] skipping account alias=%s account_id=%s: %s",
                    row.alias,
                    row.account_id,
                    exc,
                )

        wrapper = cls(primary_client, mirror_runtimes, enabled=True, state_file=_state_file)
        if wrapper._enabled:
            _LOGGER.info(
                "[mirror] enabled: principal=%s mirrors=%d summary=%s",
                principal_account,
                len(mirror_runtimes),
                redacted_accounts_summary(config),
            )
        else:
            _LOGGER.info("[mirror] disabled: no eligible mirror accounts after filtering")
        return wrapper

    @property
    def mirror_enabled(self) -> bool:
        return self._enabled

    @property
    def mirror_count(self) -> int:
        return len(self._mirrors)

    @staticmethod
    def _normalise_contract_side(raw: Any) -> str:
        side = str(raw or "").strip().upper()
        if side in {"MULTUP", "CALL", "RISE"}:
            return "MULTUP"
        if side in {"MULTDOWN", "PUT", "FALL"}:
            return "MULTDOWN"
        return side

    @staticmethod
    def _extract_contract_symbol(contract: dict[str, Any]) -> str:
        symbol = str(
            contract.get("symbol")
            or contract.get("underlying_symbol")
            or contract.get("underlying")
            or ""
        ).strip().upper()
        if symbol:
            return symbol
        shortcode = str(contract.get("shortcode") or "")
        if not shortcode:
            return ""
        parts = shortcode.split("_")
        sym_parts: list[str] = []
        for part in parts[1:]:
            if "." in part:
                break
            sym_parts.append(part)
        return "_".join(sym_parts).strip().upper()

    def _load_links(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[mirror] cannot load link-state %s: %s", self._state_file, exc)
            return
        if not isinstance(payload, dict):
            return

        loaded: dict[int, list[dict[str, Any]]] = {}
        for raw_pid, raw_links in payload.items():
            try:
                pid = int(raw_pid)
            except Exception:
                continue
            if pid <= 0 or not isinstance(raw_links, list):
                continue
            rows: list[dict[str, Any]] = []
            for link in raw_links:
                if not isinstance(link, dict):
                    continue
                try:
                    cid = int(link.get("contract_id") or 0)
                except Exception:
                    cid = 0
                if cid <= 0:
                    continue
                alias = str(link.get("alias") or "").strip()
                account_id = str(link.get("account_id") or "").strip()
                user_id = str(link.get("user_id") or "").strip()
                if not alias and not account_id:
                    continue
                rows.append(
                    {
                        "alias": alias,
                        "account_id": account_id,
                        "user_id": user_id,
                        "contract_id": cid,
                    }
                )
            if rows:
                loaded[pid] = rows
        if loaded:
            self._mirror_contract_links = loaded
            _LOGGER.info(
                "[mirror] loaded %d persisted principal link group(s) from %s",
                len(loaded),
                self._state_file,
            )

    def _persist_links(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                str(pid): links
                for pid, links in self._mirror_contract_links.items()
                if links
            }
            tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self._state_file)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[mirror] cannot persist link-state %s: %s", self._state_file, exc)

    def _upsert_link(self, principal_id: int, link: dict[str, Any]) -> bool:
        pid = int(principal_id)
        if pid <= 0:
            return False
        try:
            cid = int(link.get("contract_id") or 0)
        except Exception:
            return False
        if cid <= 0:
            return False
        alias = str(link.get("alias") or "").strip()
        account_id = str(link.get("account_id") or "").strip()

        links = self._mirror_contract_links.setdefault(pid, [])
        for cur in links:
            try:
                cur_cid = int(cur.get("contract_id") or 0)
            except Exception:
                cur_cid = 0
            if cur_cid != cid:
                continue
            if alias and str(cur.get("alias") or "").strip() == alias:
                return False
            if account_id and str(cur.get("account_id") or "").strip() == account_id:
                return False
        links.append(link)
        return True

    def _find_mirror_runtime(self, alias: str, account_id: str) -> _MirrorRuntime | None:
        if alias:
            for m in self._mirrors:
                if m.alias == alias:
                    return m
        if account_id:
            for m in self._mirrors:
                if m.account_id == account_id:
                    return m
        return None

    async def _close_mirror_contract(
        self,
        mirror: _MirrorRuntime,
        contract_id: int,
        *,
        reason: str,
        principal_contract_id: int | None = None,
    ) -> bool:
        cid = int(contract_id)
        if cid <= 0:
            return False

        async def _attempt() -> None:
            await mirror.ensure_connected()
            await mirror.client.sell(cid)

        try:
            await _attempt()
            _LOGGER.info(
                "[mirror] close ok alias=%s principal_contract=%s mirror_contract=%s reason=%s",
                mirror.alias,
                principal_contract_id if principal_contract_id is not None else "n/a",
                cid,
                reason,
            )
            return True
        except DerivClientError as exc:
            mirror._connected = False
            try:
                await mirror.client.close()
            except Exception:
                pass
            try:
                await _attempt()
                _LOGGER.info(
                    "[mirror] close ok after reconnect alias=%s principal_contract=%s mirror_contract=%s reason=%s",
                    mirror.alias,
                    principal_contract_id if principal_contract_id is not None else "n/a",
                    cid,
                    reason,
                )
                return True
            except Exception:
                pass
            _LOGGER.warning(
                "[mirror] close failed alias=%s principal_contract=%s mirror_contract=%s reason=%s err=%s",
                mirror.alias,
                principal_contract_id if principal_contract_id is not None else "n/a",
                cid,
                reason,
                exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[mirror] close crashed alias=%s principal_contract=%s mirror_contract=%s reason=%s err=%s",
                mirror.alias,
                principal_contract_id if principal_contract_id is not None else "n/a",
                cid,
                reason,
                exc,
            )
            return False

    async def boot_sync_followers(self, principal_open_contracts: list[dict[str, Any]]) -> dict[str, Any]:
        """Boot-time follower sync after deploy/restart.

        Rebuilds missing principal->mirror links from live portfolios and closes
        unmatched mirror contracts (orphans) so follower accounts do not keep
        stale opens after principal restart/deploy.
        """
        if not self._enabled:
            return {"enabled": False, "linked": 0, "closed_orphans": 0, "scanned": 0}

        principals: list[dict[str, Any]] = []
        for row in principal_open_contracts:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("contract_id") or 0)
            except Exception:
                pid = 0
            if pid <= 0:
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            side = self._normalise_contract_side(row.get("side"))
            try:
                stake = float(row.get("stake_usdt") or 0.0)
            except Exception:
                stake = 0.0
            try:
                opened_at = float(row.get("opened_at_ts") or 0.0)
            except Exception:
                opened_at = 0.0
            principals.append(
                {
                    "contract_id": pid,
                    "symbol": symbol,
                    "side": side,
                    "stake_usdt": stake,
                    "opened_at_ts": opened_at,
                }
            )

        principal_ids = {p["contract_id"] for p in principals}

        stale_principal_links = 0
        for pid in list(self._mirror_contract_links.keys()):
            if pid in principal_ids:
                continue
            stale_principal_links += len(self._mirror_contract_links.get(pid) or [])
            self._mirror_contract_links.pop(pid, None)

        linked_cids_by_alias: dict[str, set[int]] = {}
        for pid, links in self._mirror_contract_links.items():
            if pid not in principal_ids:
                continue
            for link in links:
                alias = str(link.get("alias") or "").strip()
                try:
                    cid = int(link.get("contract_id") or 0)
                except Exception:
                    cid = 0
                if alias and cid > 0:
                    linked_cids_by_alias.setdefault(alias, set()).add(cid)

        linked = 0
        closed_orphans = 0
        scanned = 0

        for mirror in self._mirrors:
            try:
                await mirror.ensure_connected()
                portfolio = await mirror.client.portfolio_full()
            except Exception as exc:  # noqa: BLE001
                mirror._connected = False
                _LOGGER.warning(
                    "[mirror] boot-sync portfolio fetch failed alias=%s account_id=%s: %s",
                    mirror.alias,
                    mirror.account_id,
                    exc,
                )
                continue

            if not portfolio:
                continue

            used_principal_ids: set[int] = set()
            for pid, links in self._mirror_contract_links.items():
                if pid not in principal_ids:
                    continue
                for link in links:
                    if str(link.get("alias") or "").strip() == mirror.alias:
                        used_principal_ids.add(pid)
                        break

            for contract in portfolio:
                if not isinstance(contract, dict):
                    continue
                try:
                    mirror_cid = int(contract.get("contract_id") or 0)
                except Exception:
                    mirror_cid = 0
                if mirror_cid <= 0:
                    continue
                scanned += 1

                if mirror_cid in linked_cids_by_alias.get(mirror.alias, set()):
                    continue

                mirror_symbol = self._extract_contract_symbol(contract)
                mirror_side = self._normalise_contract_side(contract.get("contract_type"))
                try:
                    mirror_opened = float(contract.get("date_start") or 0.0)
                except Exception:
                    mirror_opened = 0.0
                try:
                    mirror_stake = float(contract.get("buy_price") or contract.get("stake") or 0.0)
                except Exception:
                    mirror_stake = 0.0

                best_match: dict[str, Any] | None = None
                best_key: tuple[float, float] | None = None
                for p in principals:
                    pid = int(p["contract_id"])
                    if pid in used_principal_ids:
                        continue
                    if p["symbol"] and mirror_symbol and p["symbol"] != mirror_symbol:
                        continue
                    if p["side"] and mirror_side and p["side"] != mirror_side:
                        continue

                    if p["opened_at_ts"] > 0 and mirror_opened > 0:
                        dt_gap = abs(p["opened_at_ts"] - mirror_opened)
                    else:
                        dt_gap = 9999.0
                    if dt_gap > 300.0:
                        continue

                    if p["stake_usdt"] > 0 and mirror_stake > 0:
                        stake_gap = abs(p["stake_usdt"] - mirror_stake)
                        if stake_gap > max(0.50, p["stake_usdt"] * 0.35):
                            continue
                    else:
                        stake_gap = 0.0

                    key = (dt_gap, stake_gap)
                    if best_key is None or key < best_key:
                        best_key = key
                        best_match = p

                if best_match is not None:
                    link = {
                        "alias": mirror.alias,
                        "account_id": mirror.account_id,
                        "user_id": mirror.user_id,
                        "contract_id": mirror_cid,
                    }
                    pid = int(best_match["contract_id"])
                    if self._upsert_link(pid, link):
                        linked += 1
                    linked_cids_by_alias.setdefault(mirror.alias, set()).add(mirror_cid)
                    used_principal_ids.add(pid)
                    _LOGGER.info(
                        "[mirror] boot-sync linked principal=%s <-> alias=%s mirror_contract=%s symbol=%s",
                        pid,
                        mirror.alias,
                        mirror_cid,
                        mirror_symbol or "?",
                    )
                    continue

                ok = await self._close_mirror_contract(
                    mirror,
                    mirror_cid,
                    reason="boot_orphan_no_principal_match",
                    principal_contract_id=None,
                )
                if ok:
                    closed_orphans += 1

        self._persist_links()
        summary = {
            "enabled": True,
            "principal_open": len(principals),
            "linked": linked,
            "closed_orphans": closed_orphans,
            "scanned": scanned,
            "stale_principal_links": stale_principal_links,
        }
        _LOGGER.info("[mirror] BOOT-SYNC summary: %s", summary)
        return summary

    async def buy(
        self,
        *,
        symbol: str,
        contract_type: str,
        stake_usdt: float,
        multiplier: int,
        stop_loss_pct: float,
        take_profit_pct: float,
    ) -> dict[str, Any]:
        principal = await self._primary.buy(
            symbol=symbol,
            contract_type=contract_type,
            stake_usdt=stake_usdt,
            multiplier=multiplier,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        if not self._enabled:
            return principal

        principal_id = int(principal.get("contract_id") or 0)
        if principal_id <= 0:
            return principal

        links: list[dict[str, Any]] = []
        tasks: list[asyncio.Task[dict[str, Any] | None]] = []

        async def _open_one(mirror: _MirrorRuntime) -> dict[str, Any] | None:
            async def _attempt() -> dict[str, Any] | None:
                await mirror.ensure_connected()
                result = await mirror.client.buy(
                    symbol=symbol,
                    contract_type=contract_type,
                    stake_usdt=stake_usdt,
                    multiplier=multiplier,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                cid = int(result.get("contract_id") or 0)
                if cid <= 0:
                    return None
                return {
                    "alias": mirror.alias,
                    "account_id": mirror.account_id,
                    "user_id": mirror.user_id,
                    "contract_id": cid,
                }

            try:
                return await _attempt()
            except Exception as exc:  # noqa: BLE001
                mirror._connected = False
                try:
                    await mirror.client.close()
                except Exception:
                    pass
                try:
                    return await _attempt()
                except Exception:
                    pass
                _LOGGER.warning(
                    "[mirror] buy failed alias=%s account_id=%s symbol=%s: %s",
                    mirror.alias,
                    mirror.account_id,
                    symbol,
                    exc,
                )
                return None

        for runtime in self._mirrors:
            tasks.append(asyncio.create_task(_open_one(runtime), name=f"mirror-buy-{runtime.alias}"))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=False)
            for item in results:
                if item is not None:
                    links.append(item)

        if links:
            _added = 0
            for link in links:
                if self._upsert_link(principal_id, link):
                    _added += 1
            self._persist_links()
            _LOGGER.info(
                "[mirror] principal contract=%s mirrored to %d account(s) (new_links=%d)",
                principal_id,
                len(links),
                _added,
            )
        else:
            _LOGGER.warning(
                "[mirror] principal contract=%s had no successful mirror fills",
                principal_id,
            )

        return principal

    async def sell(self, contract_id: int) -> dict[str, Any]:
        result = await self._primary.sell(contract_id)
        if self._enabled:
            await self._close_mirrors(contract_id, reason="principal_sell")
        return result

    async def notify_principal_settled(self, contract_id: int, *, reason: str) -> None:
        if not self._enabled:
            return
        await self._close_mirrors(contract_id, reason=reason)

    async def _close_mirrors(self, principal_contract_id: int, *, reason: str) -> None:
        links = self._mirror_contract_links.pop(int(principal_contract_id), [])
        if not links:
            return

        tasks: list[asyncio.Task[None]] = []

        async def _close_one(link: dict[str, Any]) -> None:
            cid = int(link.get("contract_id") or 0)
            alias = str(link.get("alias") or "").strip()
            account_id = str(link.get("account_id") or "").strip()
            mirror = self._find_mirror_runtime(alias, account_id)
            if mirror is None or cid <= 0:
                return
            await self._close_mirror_contract(
                mirror,
                cid,
                reason=reason,
                principal_contract_id=int(principal_contract_id),
            )

        for link in links:
            tasks.append(
                asyncio.create_task(
                    _close_one(link),
                    name=f"mirror-close-{link.get('alias', 'mirror')}",
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)
        self._persist_links()

    async def close(self) -> None:
        errors: list[str] = []
        try:
            await self._primary.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"primary: {exc}")

        for mirror in self._mirrors:
            try:
                await mirror.client.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{mirror.alias}: {exc}")

        if errors:
            _LOGGER.warning("[mirror] close completed with errors: %s", errors)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)
