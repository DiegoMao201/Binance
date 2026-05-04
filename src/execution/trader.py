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

    @staticmethod
    def _compute_slippage_pct(signal_price: float, fill_price: float, side: str) -> float:
        if signal_price <= 0 or fill_price <= 0:
            return 0.0
        if side == "buy":
            return (fill_price - signal_price) / signal_price
        return (signal_price - fill_price) / signal_price

    def _cap_buy_amount_to_free_quote(self, amount: float, market_price: float, symbol: str) -> tuple[float, str | None]:
        if amount <= 0 or market_price <= 0:
            return 0.0, "Tamaño de orden inválido."

        try:
            quote_balance = self.client.fetch_asset_balance(asset=self.client.quote_asset_for(symbol))
        except BinanceClientError as exc:
            return 0.0, f"fetch_quote_balance: {exc}"

        free_quote = float(quote_balance.get("free", 0.0) or 0.0)
        # Reservamos 2% para fees, redondeos y micro-movimientos entre señal y fill.
        spendable_quote = max(0.0, free_quote * 0.98)
        if spendable_quote <= 0:
            return 0.0, f"Saldo libre insuficiente en {quote_balance.get('asset', 'USDT')}."

        capped_amount = min(amount, round(spendable_quote / market_price, 6))
        capped_notional = capped_amount * market_price
        if capped_notional < self.settings.minimum_trade_usdt:
            return 0.0, (
                f"Saldo libre insuficiente tras buffer operativo: {round(spendable_quote, 4)} "
                f"{quote_balance.get('asset', 'USDT')}."
            )
        return capped_amount, None

    def execute(
        self,
        side: str,
        market_price: float,
        risk: RiskSnapshot,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        target_symbol = symbol or self.settings.trading_symbol
        amount = self.risk_manager.compute_order_size(market_price, risk.equity_usd)
        if side == "buy" and not self.settings.dry_run:
            amount, balance_reason = self._cap_buy_amount_to_free_quote(amount, market_price, target_symbol)
            if balance_reason is not None:
                return {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbol": target_symbol,
                    "side": side,
                    "amount": 0.0,
                    "price": round(market_price, 4),
                    "signal_price": round(market_price, 4),
                    "notional_usdt": 0.0,
                    "slippage_pct": 0.0,
                    "mode": "live",
                    "status": "rejected",
                    "reason": balance_reason,
                }
        protection_levels = self.risk_manager.build_protection_levels(market_price, side)

        order_payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": target_symbol,
            "side": side,
            "amount": amount,
            "price": round(market_price, 4),
            "signal_price": round(market_price, 4),
            "notional_usdt": round(amount * market_price, 4),
            "slippage_pct": 0.0,
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
            exchange_order = self.client.create_market_order(side, amount, symbol=target_symbol)
        except BinanceClientError as exc:
            self.logger.error("Fallo al enviar orden %s en %s: %s", side, target_symbol, exc)
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
            asset_balance = self.client.fetch_asset_balance(symbol=target_symbol)
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
        order_payload["fill_price"] = round(average, 4)
        order_payload["fee_quote"] = round(fee_total, 6)
        order_payload["slippage_pct"] = round(
            self._compute_slippage_pct(float(order_payload["signal_price"]), average, side),
            6,
        )
        order_payload["reconciled_holdings"] = asset_balance
        # Use real reconciled fields as the source of truth for the position.
        order_payload["amount"] = round(filled, 8)
        order_payload["price"] = round(average, 4)
        order_payload["notional_usdt"] = round(filled * average, 4)
        # Recompute SL/TP against the real average fill, not the projected price.
        order_payload.update(self.risk_manager.build_protection_levels(average, side))
        self.logger.info(
            "Slippage %s en %s: signal=%s fill=%s slippage=%.4f%%",
            side,
            target_symbol,
            order_payload["signal_price"],
            order_payload["fill_price"],
            order_payload["slippage_pct"] * 100,
        )
        self.logger.info("Orden ejecutada y reconciliada: %s", order_payload)
        return order_payload

    def close_position_market(self, position: dict[str, Any]) -> dict[str, Any]:
        side = "sell" if position.get("side") == "buy" else "buy"
        target_symbol = position.get("symbol") or self.settings.trading_symbol
        amount = float(position.get("amount") or 0.0)
        result: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": target_symbol,
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
            asset_balance = self.client.fetch_asset_balance(symbol=target_symbol)
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
            exchange_order = self.client.create_market_order(side, sellable, symbol=target_symbol)
        except BinanceClientError as exc:
            self.logger.error("Fallo al cerrar posición: %s", exc)
            result["status"] = "error"
            result["reason"] = f"create_market_order close: {exc}"
            return result

        filled, average, fee_total = self._extract_fill(exchange_order)
        try:
            post_balance = self.client.fetch_asset_balance(symbol=target_symbol)
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