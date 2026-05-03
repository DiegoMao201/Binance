from __future__ import annotations

from typing import Any

import ccxt
import pandas as pd

from src.utils.config import Settings


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

    def fetch_ohlcv(self, limit: int = 200) -> pd.DataFrame:
        candles = self.public_exchange.fetch_ohlcv(
            self.settings.trading_symbol,
            timeframe=self.settings.timeframe,
            limit=limit,
        )
        frame = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        return frame

    def fetch_balance_usd(self) -> float:
        if self.settings.dry_run or not self.settings.binance_api_key:
            return self.settings.initial_capital_usd

        balance = self.private_exchange.fetch_balance()
        usdt_info = balance.get("USDT", {})
        return float(usdt_info.get("free", 0.0))

    def create_market_order(self, side: str, amount: float) -> dict[str, Any]:
        return self.private_exchange.create_market_order(self.settings.trading_symbol, side, amount)