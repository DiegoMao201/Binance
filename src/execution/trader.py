from __future__ import annotations

import asyncio
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

    @staticmethod
    def _is_insufficient_balance_error(exc: BinanceClientError) -> bool:
        return "insufficient balance" in str(exc).lower()

    def _cap_buy_amount_to_free_quote(self, amount: float, market_price: float, symbol: str) -> tuple[float, float, str | None]:
        if amount <= 0 or market_price <= 0:
            return 0.0, 0.0, "Tamaño de orden inválido."

        try:
            quote_balance = self.client.fetch_asset_balance(asset=self.client.quote_asset_for(symbol))
        except BinanceClientError as exc:
            return 0.0, 0.0, f"fetch_quote_balance: {exc}"

        free_quote = float(quote_balance.get("free", 0.0) or 0.0)
        # Reservamos un colchón real para fees, precisión del exchange y micro-movimientos.
        spendable_quote = max(0.0, min(free_quote * 0.95, free_quote - 1.0))
        if spendable_quote <= 0:
            return 0.0, 0.0, f"Saldo libre insuficiente en {quote_balance.get('asset', 'USDT')}."

        capped_amount = min(amount, round(spendable_quote / market_price, 6))
        target_quote = min(capped_amount * market_price, spendable_quote)
        # Dejamos un margen adicional justo en el presupuesto quote que Binance valida.
        quote_budget = max(0.0, round(target_quote * 0.99, 4))
        if quote_budget < self.settings.minimum_trade_usdt:
            return 0.0, 0.0, (
                f"Saldo libre insuficiente tras buffer operativo: {round(spendable_quote, 4)} "
                f"{quote_balance.get('asset', 'USDT')}."
            )
        return capped_amount, quote_budget, None

    def _format_protection_levels(
        self,
        price: float,
        side: str,
        symbol: str,
        *,
        atr_pct: float | None = None,
    ) -> dict[str, float]:
        raw_levels = self.risk_manager.build_protection_levels(price, side, atr_pct=atr_pct)
        return {
            "stop_loss": self.client.price_to_precision(float(raw_levels["stop_loss"]), symbol=symbol),
            "take_profit": self.client.price_to_precision(float(raw_levels["take_profit"]), symbol=symbol),
            "sl_pct_used": float(raw_levels.get("sl_pct_used", 0.0)),
            "tp_pct_used": float(raw_levels.get("tp_pct_used", 0.0)),
        }

    def execute(
        self,
        side: str,
        market_price: float,
        risk: RiskSnapshot,
        symbol: str | None = None,
        *,
        atr_pct: float | None = None,
        conviction_multiplier: float | None = None,
    ) -> dict[str, Any]:
        target_symbol = symbol or self.settings.trading_symbol
        free_quote_usd: float | None = None
        if side == "buy" and not self.settings.dry_run:
            try:
                quote_balance = self.client.fetch_asset_balance(asset=self.client.quote_asset_for(target_symbol))
                free_quote_usd = float(quote_balance.get("free", 0.0) or 0.0)
            except BinanceClientError:
                free_quote_usd = None
        amount = self.risk_manager.compute_order_size(
            market_price,
            risk.equity_usd,
            free_quote_usd=free_quote_usd,
            conviction_multiplier=conviction_multiplier,
        )
        quote_amount: float | None = None
        if side == "buy" and not self.settings.dry_run:
            amount, quote_budget, balance_reason = self._cap_buy_amount_to_free_quote(amount, market_price, target_symbol)
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
            quote_amount = quote_budget
        try:
            amount = self.client.amount_to_precision(amount, symbol=target_symbol)
            if side == "buy" and quote_amount is None:
                quote_amount = self.client.cost_to_precision(amount * market_price, symbol=target_symbol)
            protection_levels = self._format_protection_levels(market_price, side, target_symbol, atr_pct=atr_pct)
        except BinanceClientError as exc:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": target_symbol,
                "side": side,
                "amount": 0.0,
                "price": round(market_price, 4),
                "signal_price": round(market_price, 4),
                "notional_usdt": 0.0,
                "slippage_pct": 0.0,
                "mode": "dry_run" if self.settings.dry_run else "live",
                "status": "rejected",
                "reason": f"precision_formatting: {exc}",
            }

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
            exchange_order = self.client.create_market_order(
                side,
                amount,
                symbol=target_symbol,
                quote_amount=quote_amount,
            )
        except BinanceClientError as exc:
            self.logger.error("Fallo al enviar orden %s en %s: %s", side, target_symbol, exc)
            order_payload["status"] = "rejected" if self._is_insufficient_balance_error(exc) else "error"
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
        # Recompute SL/TP against the real average fill using exchange-native price precision.
        try:
            order_payload.update(self._format_protection_levels(average, side, target_symbol, atr_pct=atr_pct))
        except BinanceClientError as exc:
            order_payload["status"] = "reconcile_failed"
            order_payload["reason"] = f"precision_formatting protections: {exc}"
            return order_payload
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
            sellable = self.client.amount_to_precision(sellable, symbol=target_symbol)
        except BinanceClientError as exc:
            result["status"] = "rejected"
            result["reason"] = f"precision_formatting close: {exc}"
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

    async def replace_stop_loss_async(
        self,
        *,
        position: dict[str, Any],
        new_stop_loss: float,
        take_profit: float,
        trailing_tier: int,
    ) -> dict[str, Any]:
        """Replace or create the protection OCO without blocking the event loop.

        The trading loop stays responsive because every ccxt network call runs inside
        `asyncio.to_thread`. The caller is responsible for invoking this only when a new
        trailing tier is crossed, which rate-limits OCO churn to one request per tier.
        """

        target_symbol = str(position.get("symbol") or self.settings.trading_symbol)
        target_side = str(position.get("side") or "buy").lower()
        amount = float(position.get("amount") or 0.0)
        protection_order = position.get("protection_order") or {}

        result: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": target_symbol,
            "status": "rejected",
            "trailing_tier": trailing_tier,
            "new_stop_loss": round(new_stop_loss, 8),
            "take_profit": round(take_profit, 8),
            "mode": "dry_run" if self.settings.dry_run else "live",
        }

        if target_side != "buy":
            result["reason"] = "replace_stop_loss_async solo soporta posiciones spot long."
            return result

        if amount <= 0:
            result["reason"] = "Posicion sin cantidad operable para OCO."
            return result

        if self.settings.dry_run:
            result["status"] = "simulated"
            result["protection_order"] = {
                "type": "oco",
                "stop_loss": round(new_stop_loss, 8),
                "take_profit": round(take_profit, 8),
            }
            return result

        if protection_order.get("orderListId") is not None or protection_order.get("listClientOrderId"):
            cancel_payload = await asyncio.to_thread(
                self.client.cancel_order_list,
                symbol=target_symbol,
                order_list_id=protection_order.get("orderListId"),
                list_client_order_id=protection_order.get("listClientOrderId"),
            )
            result["cancel_payload"] = cancel_payload

        holdings = await asyncio.to_thread(self.client.fetch_asset_balance, symbol=target_symbol)
        sellable_amount = min(amount, float(holdings.get("total") or 0.0))
        if sellable_amount <= 0:
            result["reason"] = "Holdings insuficientes para recrear OCO."
            result["reconciled_holdings"] = holdings
            return result

        oco_payload = await asyncio.to_thread(
            self.client.create_protection_oco_order,
            symbol=target_symbol,
            side="sell",
            quantity=sellable_amount,
            take_profit_price=take_profit,
            stop_loss_price=new_stop_loss,
        )
        result["status"] = "submitted"
        result["reconciled_holdings"] = holdings
        result["protection_order"] = oco_payload
        return result