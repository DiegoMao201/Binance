from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.data.binance_client import BinanceDataClient
from src.safety.risk_manager import RiskManager, RiskSnapshot
from src.utils.config import Settings


class TradeExecutor:
    def __init__(
        self,
        settings: Settings,
        client: BinanceDataClient,
        risk_manager: RiskManager,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.client = client
        self.risk_manager = risk_manager
        self.logger = logger

    def execute(
        self,
        side: str,
        market_price: float,
        risk: RiskSnapshot,
    ) -> dict[str, Any]:
        amount = self.risk_manager.compute_order_size(market_price, risk.balance_usd)
        protection_levels = self.risk_manager.build_protection_levels(market_price, side)

        order_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": self.settings.trading_symbol,
            "side": side,
            "amount": amount,
            "price": round(market_price, 4),
            "notional_usdt": round(amount * market_price, 4),
            **protection_levels,
            "mode": "dry_run" if self.settings.dry_run else "live",
        }

        if amount <= 0:
            order_payload["status"] = "rejected"
            order_payload["reason"] = "Tamaño de orden inválido."
            return order_payload

        if self.settings.dry_run:
            order_payload["status"] = "simulated"
            self.logger.info("[DRY RUN] Orden simulada: %s", order_payload)
            return order_payload

        exchange_order = self.client.create_market_order(side, amount)
        order_payload["status"] = "submitted"
        order_payload["exchange_order"] = exchange_order
        self.logger.info("Orden enviada a Binance: %s", order_payload)
        return order_payload