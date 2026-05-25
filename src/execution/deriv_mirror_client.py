from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
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
    ) -> None:
        self._primary = primary_client
        self._mirrors = mirror_runtimes
        self._enabled = enabled and bool(mirror_runtimes)
        self._mirror_contract_links: dict[int, list[dict[str, Any]]] = {}

    @classmethod
    def from_settings(
        cls,
        primary_client: DerivClient,
        settings: DerivSettings,
    ) -> "DerivMirrorClient":
        path = resolve_multi_accounts_path()
        if path is None:
            _LOGGER.info("[mirror] disabled: DERIV_MULTI_ACCOUNTS_FILE is not set")
            return cls(primary_client, [], enabled=False)
        if not path.exists():
            _LOGGER.warning("[mirror] disabled: multi-account file not found at %s", path)
            return cls(primary_client, [], enabled=False)

        try:
            config = load_multi_accounts_config(path)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[mirror] disabled: cannot parse %s: %s", path, exc)
            return cls(primary_client, [], enabled=False)

        errors = validate_multi_accounts_config(config)
        if errors:
            _LOGGER.warning("[mirror] disabled: invalid config (%s)", "; ".join(errors))
            return cls(primary_client, [], enabled=False)

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

        wrapper = cls(primary_client, mirror_runtimes, enabled=True)
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
            self._mirror_contract_links[principal_id] = links
            _LOGGER.info(
                "[mirror] principal contract=%s mirrored to %d account(s)",
                principal_id,
                len(links),
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
            alias = str(link.get("alias") or "mirror")
            mirror = next((m for m in self._mirrors if m.alias == alias), None)
            if mirror is None or cid <= 0:
                return

            async def _attempt() -> None:
                await mirror.ensure_connected()
                await mirror.client.sell(cid)

            try:
                await _attempt()
                _LOGGER.info(
                    "[mirror] close ok alias=%s principal_contract=%s mirror_contract=%s reason=%s",
                    alias,
                    principal_contract_id,
                    cid,
                    reason,
                )
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
                        alias,
                        principal_contract_id,
                        cid,
                        reason,
                    )
                    return
                except Exception:
                    pass
                _LOGGER.warning(
                    "[mirror] close failed alias=%s principal_contract=%s mirror_contract=%s reason=%s err=%s",
                    alias,
                    principal_contract_id,
                    cid,
                    reason,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "[mirror] close crashed alias=%s principal_contract=%s mirror_contract=%s reason=%s err=%s",
                    alias,
                    principal_contract_id,
                    cid,
                    reason,
                    exc,
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
