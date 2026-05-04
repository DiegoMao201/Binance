from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

import ccxt
import pandas as pd

from src.utils.config import Settings


T = TypeVar("T")


class BinanceClientError(Exception):
    def __init__(self, message: str, *, category: str = "exchange_other") -> None:
        super().__init__(message)
        self.category = category


def classify_binance_error(exc: Exception | str) -> str:
    message = str(exc).lower()

    if "429" in message or "too many requests" in message or "rate limit" in message:
        return "rate_limit"
    if "timed out" in message or "timeout" in message or "requesttimeout" in message:
        return "timeout_binance"
    if any(token in message for token in [
        "name or service not known",
        "temporary failure in name resolution",
        "failed to establish a new connection",
        "connection aborted",
        "connection reset",
        "network is unreachable",
        "nodename nor servname provided",
        "max retries exceeded",
        "remote end closed connection",
        "ssl",
    ]):
        return "network_local"
    return "exchange_other"


class BinanceDataClient:
    def __init__(self, settings: Settings) -> None:
        public_config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 8000,
            "options": {"defaultType": "spot"},
        }
        self.public_exchange = ccxt.binance(public_config)

        private_config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 8000,
            "options": {"defaultType": "spot"},
        }
        if settings.binance_api_key and settings.binance_api_secret:
            private_config.update(
                {
                    "apiKey": settings.binance_api_key,
                    "secret": settings.binance_api_secret,
                }
            )

        self.private_exchange = ccxt.binance(private_config)
        self.settings = settings

    def _ensure_market_loaded(self, symbol: str) -> None:
        self._with_retries(lambda: self.private_exchange.load_markets())
        if symbol not in (self.private_exchange.markets or {}):
            raise BinanceClientError(f"Mercado no disponible en Binance: {symbol}")

    @staticmethod
    def _with_retries(operation: Callable[[], T], attempts: int = 4, base_delay: float = 0.75) -> T:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except ccxt.RequestTimeout as exc:
                last_error = exc
                time.sleep(base_delay * (2 ** (attempt - 1)))
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
                last_error = exc
                time.sleep(base_delay * (2 ** (attempt - 1)))
            except ccxt.BaseError as exc:
                raise BinanceClientError(str(exc), category=classify_binance_error(exc)) from exc
        raise BinanceClientError(
            f"Binance no respondió tras {attempts} intentos: {last_error}",
            category=classify_binance_error(last_error or "timeout"),
        )

    @property
    def base_asset(self) -> str:
        symbol = self.settings.trading_symbol
        return symbol.split("/")[0] if "/" in symbol else symbol

    @property
    def quote_asset(self) -> str:
        symbol = self.settings.trading_symbol
        return symbol.split("/")[1] if "/" in symbol else "USDT"

    def base_asset_for(self, symbol: str | None = None) -> str:
        target = symbol or self.settings.trading_symbol
        return target.split("/")[0] if "/" in target else target

    def quote_asset_for(self, symbol: str | None = None) -> str:
        target = symbol or self.settings.trading_symbol
        return target.split("/")[1] if "/" in target else "USDT"

    def fetch_ohlcv(self, limit: int = 200, symbol: str | None = None) -> pd.DataFrame:
        target = symbol or self.settings.trading_symbol
        candles = self._with_retries(
            lambda: self.public_exchange.fetch_ohlcv(
                target,
                timeframe=self.settings.timeframe,
                limit=limit,
            )
        )
        frame = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        return frame

    def _fetch_spot_account(self) -> dict[str, Any]:
        return self._with_retries(lambda: self.private_exchange.privateGetAccount())

    def _fetch_full_balance(self) -> dict[str, Any]:
        account = self._fetch_spot_account()
        balances = account.get("balances", []) or []
        normalized: dict[str, Any] = {"info": account}
        free_bucket: dict[str, float] = {}
        used_bucket: dict[str, float] = {}
        total_bucket: dict[str, float] = {}

        for entry in balances:
            asset = str(entry.get("asset") or "").upper()
            if not asset:
                continue
            free = float(entry.get("free", 0.0) or 0.0)
            locked = float(entry.get("locked", 0.0) or 0.0)
            total = free + locked
            normalized[asset] = {
                "free": free,
                "used": locked,
                "total": total,
            }
            free_bucket[asset] = free
            used_bucket[asset] = locked
            total_bucket[asset] = total

        normalized["free"] = free_bucket
        normalized["used"] = used_bucket
        normalized["total"] = total_bucket
        return normalized

    @staticmethod
    def _balance_aliases(asset: str) -> tuple[str, ...]:
        # Stablecoins distintos no son intercambiables al ejecutar pares /USDT;
        # leer USD/FDUSD/USDC como si fueran USDT provoca rejects en Binance.
        return (asset,)

    def _extract_balance_value(self, balance: dict[str, Any], asset: str, field: str) -> float:
        for candidate in self._balance_aliases(asset):
            info = balance.get(candidate, {}) or {}
            value = info.get(field)
            if value not in (None, 0, 0.0):
                return float(value)

        bucket = balance.get(field, {}) or {}
        for candidate in self._balance_aliases(asset):
            value = bucket.get(candidate)
            if value not in (None, 0, 0.0):
                return float(value)

        return 0.0

    def fetch_balance_usd(self) -> float:
        if self.settings.dry_run or not self.settings.binance_api_key:
            return self.settings.initial_capital_usd

        balance = self._fetch_full_balance()
        return self._extract_balance_value(balance, "USDT", "free")

    def fetch_asset_balance(self, asset: str | None = None, symbol: str | None = None) -> dict[str, float]:
        target = asset or self.base_asset_for(symbol)
        if self.settings.dry_run or not self.settings.binance_api_key:
            return {"asset": target, "free": 0.0, "used": 0.0, "total": 0.0}

        balance = self._fetch_full_balance()
        return {
            "asset": target,
            "free": self._extract_balance_value(balance, target, "free"),
            "used": self._extract_balance_value(balance, target, "used"),
            "total": self._extract_balance_value(balance, target, "total"),
        }

    def fetch_ticker_price(self, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        ticker = self._with_retries(lambda: self.public_exchange.fetch_ticker(target))
        return float(ticker.get("last") or ticker.get("close") or 0.0)

    def ping(self) -> bool:
        try:
            self._with_retries(lambda: self.public_exchange.fetch_time())
            return True
        except BinanceClientError:
            return False

    def amount_to_precision(self, amount: float, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        try:
            formatted = self.private_exchange.amount_to_precision(target, amount)
            return float(formatted)
        except (ccxt.BaseError, ValueError, TypeError) as exc:
            raise BinanceClientError(f"amount_to_precision {target}: {exc}") from exc

    def price_to_precision(self, price: float, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        try:
            formatted = self.private_exchange.price_to_precision(target, price)
            return float(formatted)
        except (ccxt.BaseError, ValueError, TypeError) as exc:
            raise BinanceClientError(f"price_to_precision {target}: {exc}") from exc

    def cost_to_precision(self, cost: float, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        try:
            formatted = self.private_exchange.cost_to_precision(target, cost)
            return float(formatted)
        except (ccxt.BaseError, ValueError, TypeError) as exc:
            raise BinanceClientError(f"cost_to_precision {target}: {exc}") from exc

    def create_market_order(
        self,
        side: str,
        amount: float,
        symbol: str | None = None,
        *,
        quote_amount: float | None = None,
    ) -> dict[str, Any]:
        target = symbol or self.settings.trading_symbol
        if side == "buy" and quote_amount is not None:
            return self._with_retries(
                lambda: self.private_exchange.create_market_buy_order_with_cost(target, quote_amount)
            )
        return self._with_retries(
            lambda: self.private_exchange.create_market_order(target, side, amount)
        )