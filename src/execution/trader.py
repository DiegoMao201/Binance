from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.data.binance_client import BinanceClientError, BinanceDataClient
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

    @staticmethod
    def _extract_fill(exchange_order: dict[str, Any]) -> tuple[float, float, float]:
        """Return (filled_amount, average_price, fee_in_quote)."""
        filled = float(exchange_order.get("filled") or exchange_order.get("amount") or 0.0)
        average = float(exchange_order.get("average") or exchange_order.get("price") or 0.0)
        fee_total = 0.0
        fees = exchange_order.get("fees") or []
        for fee in fees:
            try:
                fee_total += float(fee.get("cost") or 0.0)
            except (TypeError, ValueError):
                continue
        return filled, average, fee_total

    def execute(
        self,
        side: str,
        market_price: float,
        risk: RiskSnapshot,
    ) -> dict[str, Any]:
        amount = self.risk_manager.compute_order_size(market_price, risk.equity_usd)
        protection_levels = self.risk_manager.build_protection_levels(market_price, side)

        order_payload: dict[str, Any] = {
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

        try:
            exchange_order = self.client.create_market_order(side, amount)
        except BinanceClientError as exc:
            self.logger.error("Fallo al enviar orden %s: %s", side, exc)
            order_payload["status"] = "error"
            order_payload["reason"] = f"create_market_order: {exc}"
            return order_payload

        filled, average, fee_total = self._extract_fill(exchange_order)
        if filled <= 0 or average <= 0:
            self.logger.error("Orden sin fill confiable: %s", exchange_order)
            order_payload["status"] = "error"
            order_payload["reason"] = "sin fill confiable"
            order_payload["exchange_order"] = exchange_order
            return order_payload

        try:
            asset_balance = self.client.fetch_asset_balance()
        except BinanceClientError as exc:
            self.logger.error("Fallo al reconciliar saldo tras orden: %s", exc)
            order_payload["status"] = "reconcile_failed"
            order_payload["reason"] = f"fetch_asset_balance: {exc}"
            order_payload["exchange_order"] = exchange_order
            return order_payload

        order_payload["status"] = "submitted"
        order_payload["exchange_order"] = exchange_order
        order_payload["filled_amount"] = round(filled, 8)
        order_payload["avg_price"] = round(average, 4)
        order_payload["fee_quote"] = round(fee_total, 6)
        order_payload["reconciled_holdings"] = asset_balance
        # Use real reconciled fields as the source of truth for the position.
        order_payload["amount"] = round(filled, 8)
        order_payload["price"] = round(average, 4)
        order_payload["notional_usdt"] = round(filled * average, 4)
        # Recompute SL/TP against the real average fill, not the projected price.
        order_payload.update(self.risk_manager.build_protection_levels(average, side))
        self.logger.info("Orden ejecutada y reconciliada: %s", order_payload)
        return order_payload

    def close_position_market(self, position: dict[str, Any]) -> dict[str, Any]:
        side = "sell" if position.get("side") == "buy" else "buy"
        amount = float(position.get("amount") or 0.0)
        result: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": self.settings.trading_symbol,
            "side": side,
            "intended_amount": amount,
            "mode": "dry_run" if self.settings.dry_run else "live",
        }

        if amount <= 0:
            result["status"] = "rejected"
            result["reason"] = "Posición sin cantidad operable."
            return result

        if self.settings.dry_run:
            result["status"] = "simulated_close"
            return result

        try:
            asset_balance = self.client.fetch_asset_balance()
        except BinanceClientError as exc:
            result["status"] = "error"
            result["reason"] = f"pre_close fetch_asset_balance: {exc}"
            return result

        sellable = min(amount, float(asset_balance.get("free", 0.0)))
        if sellable <= 0:
            result["status"] = "rejected"
            result["reason"] = "Saldo libre del activo es 0; nada para cerrar."
            result["reconciled_holdings"] = asset_balance
            return result

        try:
            exchange_order = self.client.create_market_order(side, sellable)
        except BinanceClientError as exc:
            self.logger.error("Fallo al cerrar posición: %s", exc)
            result["status"] = "error"
            result["reason"] = f"create_market_order close: {exc}"
            return result

        filled, average, fee_total = self._extract_fill(exchange_order)
        try:
            post_balance = self.client.fetch_asset_balance()
        except BinanceClientError as exc:
            result["status"] = "reconcile_failed"
            result["reason"] = f"post_close fetch_asset_balance: {exc}"
            result["exchange_order"] = exchange_order
            return result

        result["status"] = "submitted"
        result["exchange_order"] = exchange_order
        result["filled_amount"] = round(filled, 8)
        result["avg_price"] = round(average, 4)
        result["fee_quote"] = round(fee_total, 6)
        result["post_close_holdings"] = post_balance
        return result