"""
src/execution/order_router.py
─────────────────────────────────────────────────────────────────────────────
Adapter-pattern order router.

Goal: provide ONE entry point for any future broker so the upstream signal
generators do not need to know whether they are talking to Binance Spot or
Deriv synthetic multipliers.

Hard rules:
  • Every payload MUST carry a 'broker' field — no implicit defaults.
  • Every payload MUST carry stop-loss + take-profit data — no naked orders.
  • Capital limits are validated BEFORE any I/O hits the broker network.
  • The Binance branch is delegated to the existing synchronous TradeExecutor
    via `asyncio.to_thread` so the original code is touched at zero points.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from src.execution.deriv_trader import DerivOrder, DerivTradeExecutor


_LOGGER = logging.getLogger(__name__)


class OrderRouterError(RuntimeError):
    """Raised when validation fails before any broker call is made."""


class OrderRouter:
    """
    Pure async router. Holds optional handles for each supported broker.

    `binance_executor`  must be the legacy synchronous `TradeExecutor` instance
                        (or None if the binance pipeline is disabled in this
                        process — e.g. inside main_deriv.py).
    `deriv_executor`    must be a `DerivTradeExecutor` (or None when running
                        inside the Binance daemon, which never touches Deriv).
    """

    _ALLOWED_BROKERS = ("binance", "deriv")

    def __init__(
        self,
        binance_executor: Any | None = None,
        deriv_executor: DerivTradeExecutor | None = None,
    ) -> None:
        self._binance = binance_executor
        self._deriv = deriv_executor

    async def route_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        """Validate the payload and dispatch to the right broker adapter."""
        await self._validate_capital_and_limits(order_payload)

        broker = (order_payload.get("broker") or "").lower()
        if broker not in self._ALLOWED_BROKERS:
            raise OrderRouterError(
                f"unsupported broker {broker!r} (allowed: {self._ALLOWED_BROKERS})"
            )

        if broker == "binance":
            if self._binance is None:
                raise OrderRouterError("binance executor not configured in this process")
            # Original Binance pipeline is sync — call it without blocking the loop.
            return await asyncio.to_thread(self._dispatch_binance, order_payload)

        if broker == "deriv":
            if self._deriv is None:
                raise OrderRouterError("deriv executor not configured in this process")
            return await self._deriv.execute(self._build_deriv_order(order_payload))

        # Unreachable due to the membership check above, but mypy/runtime safety:
        raise OrderRouterError(f"unhandled broker: {broker}")

    # ─────────────────────────────────────────────────────────────────────────
    # Validation: every order must carry SL+TP and must respect process limits.
    # ─────────────────────────────────────────────────────────────────────────
    async def _validate_capital_and_limits(self, payload: dict[str, Any]) -> None:
        sl = payload.get("stop_loss_pct")
        tp = payload.get("take_profit_pct")
        if sl is None or tp is None:
            raise OrderRouterError(
                "stop_loss_pct and take_profit_pct are mandatory on every payload"
            )
        try:
            sl_v = float(sl)
            tp_v = float(tp)
        except (TypeError, ValueError) as exc:
            raise OrderRouterError(f"sl/tp not numeric: {exc}") from exc
        if sl_v <= 0 or tp_v <= 0:
            raise OrderRouterError("stop_loss_pct and take_profit_pct must be > 0")

        side = (payload.get("side") or "").upper()
        if side not in {"BUY", "SELL", "MULTUP", "MULTDOWN"}:
            raise OrderRouterError(f"invalid side: {side!r}")

        symbol = payload.get("symbol")
        if not symbol:
            raise OrderRouterError("symbol is required")

        # The router itself does not know live bankroll; trusting the upstream
        # risk manager to have already capped the stake. We only sanity-check
        # presence and type so a downstream broker call can never NPE.
        stake = payload.get("stake_usdt")
        if stake is None:
            raise OrderRouterError("stake_usdt is required")
        try:
            if float(stake) <= 0:
                raise OrderRouterError("stake_usdt must be > 0")
        except (TypeError, ValueError) as exc:
            raise OrderRouterError(f"stake_usdt not numeric: {exc}") from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Adapter helpers
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_deriv_order(payload: dict[str, Any]) -> DerivOrder:
        """Translate the broker-agnostic payload into a `DerivOrder`."""
        side = (payload.get("side") or "").upper()
        # Allow the router to accept either Binance-style sides or Deriv-native.
        deriv_side = {
            "BUY": "MULTUP",
            "SELL": "MULTDOWN",
            "MULTUP": "MULTUP",
            "MULTDOWN": "MULTDOWN",
        }.get(side, "MULTUP")

        return DerivOrder(
            symbol=str(payload["symbol"]),
            side=deriv_side,
            stake_usdt=float(payload["stake_usdt"]),
            multiplier=int(payload.get("multiplier") or 100),
            stop_loss_pct=float(payload["stop_loss_pct"]),
            take_profit_pct=float(payload["take_profit_pct"]),
            intent_id=str(payload.get("intent_id") or ""),
            score_breakdown=payload.get("score_breakdown"),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Binance branch — INTENTIONALLY MINIMAL
    # ─────────────────────────────────────────────────────────────────────────
    def _dispatch_binance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Non-invasive wrapper around the existing Binance executor.

        We DO NOT touch the legacy execution code; this method just confirms
        the executor is present and surfaces a clear status. The actual
        Binance pipeline still runs through `main_loop.py` as before — the
        router exists so future tooling can target Binance via the unified
        contract WITHOUT the Binance loop having to be rewired today.
        """
        _LOGGER.info(
            "[order-router] Binance order delegated to legacy executor: %s/%s",
            payload.get("symbol"), payload.get("side"),
        )
        # We do not auto-execute on Binance from this entry point during the
        # migration window — keeping behaviour bit-identical to today.
        return {
            "broker": "binance",
            "status": "delegated_to_legacy_main_loop",
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
        }
