from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

import ccxt
import pandas as pd

from src.utils.config import Settings


T = TypeVar("T")


class BinanceClientError(Exception):
    pass


class BinanceDataClient:
    def __init__(self, settings: Settings) -> None:
        public_config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        self.public_exchange = ccxt.binance(public_config)

        private_config: dict[str, Any] = {
            "enableRateLimit": True,
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

    @staticmethod
    def _with_retries(operation: Callable[[], T], attempts: int = 4, base_delay: float = 0.75) -> T:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as exc:
                last_error = exc
                time.sleep(base_delay * (2 ** (attempt - 1)))
            except ccxt.BaseError as exc:
                raise BinanceClientError(str(exc)) from exc
        raise BinanceClientError(f"Binance no respondió tras {attempts} intentos: {last_error}")

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

    def _fetch_full_balance(self) -> dict[str, Any]:
        return self._with_retries(lambda: self.private_exchange.fetch_balance())

    @staticmethod
    def _balance_aliases(asset: str) -> tuple[str, ...]:
        aliases = [asset]
        if asset == "USDT":
            aliases.extend(["USD", "FDUSD", "USDC"])
        return tuple(dict.fromkeys(aliases))

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
        info = balance.get(target, {}) or {}
        return {
            "asset": target,
            "free": float(info.get("free", 0.0) or 0.0),
            "used": float(info.get("used", 0.0) or 0.0),
            "total": float(info.get("total", 0.0) or 0.0),
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

    def create_market_order(self, side: str, amount: float, symbol: str | None = None) -> dict[str, Any]:
        target = symbol or self.settings.trading_symbol
        return self._with_retries(
            lambda: self.private_exchange.create_market_order(target, side, amount)
        )