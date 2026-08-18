"""
baseline_random — deliberately losing strategy for sanity validation.

Intent: confirm the shadow motor works correctly by running a strategy that
MUST lose over time. If baseline_random shows positive EV, the simulator
has a bug (fill bias, wrong markout sign, etc.).

Design:
- Every SIGNAL_INTERVAL_S seconds, pick a random symbol and direction.
- Place a LIMIT_MAKER order at the current best bid (BUY) or best ask (SELL).
- No signal — pure coin flip.
- Expected outcome: negative EV due to adverse selection + zero edge.
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from ..recorder.state import MarketState
from .fill_simulator import FillSimulator

logger = logging.getLogger(__name__)

SIGNAL_INTERVAL_S = 30.0   # attempt a signal per symbol every 30 s
ORDER_NOTIONAL = 10.0      # $10 notional per order (matches min_notional=5)
STRATEGY_NAME = "baseline_random"


class BaselineRandom:
    """
    Coin-flip maker strategy.
    One independent timer per symbol → ~6 signals/30s = ~12 attempts/min.
    At ~20% fill rate: ~145 fills/hour — enough to reach 50 fills in <30 min.
    """

    def __init__(
        self,
        symbols: List[str],
        state: MarketState,
        simulator: FillSimulator,
    ):
        self._symbols = symbols
        self._state = state
        self._sim = simulator
        # Per-symbol active order tracking (one pending order per symbol max)
        self._active: dict[str, Optional[int]] = {s: None for s in symbols}
        self._trade_count = 0
        self._fill_count = 0
        self._reject_count = 0

    async def run(self) -> None:
        # One task per symbol, staggered start to avoid burst
        tasks = [
            asyncio.create_task(self._symbol_loop(sym, i * 5))
            for i, sym in enumerate(self._symbols)
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()

    async def _symbol_loop(self, symbol: str, initial_delay: float) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            await asyncio.sleep(SIGNAL_INTERVAL_S)
            try:
                if self._active[symbol] is not None:
                    continue  # previous order for this symbol still open
                await self._place_signal(symbol)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"baseline_random [{symbol}] signal error: {e}")

    async def _place_signal(self, symbol: str) -> None:
        side = random.choice(["BUY", "SELL"])

        # get_bid_ask falls back to bookTicker when depth@100ms is disabled
        bid, ask = self._state.get_bid_ask(symbol)
        if bid is None or ask is None:
            return
        price = bid if side == "BUY" else ask

        # Size: ORDER_NOTIONAL / price, rounded to step_size
        info = self._state.exchange_info.get(symbol, {})
        step_size = info.get("step_size")
        if not step_size:
            return  # exchange info not yet loaded

        raw_qty = Decimal(str(ORDER_NOTIONAL)) / price
        step = Decimal(str(step_size))
        qty = (raw_qty // step) * step
        if qty <= 0:
            return

        # Build feature snapshot (full market state at signal time)
        snapshot = self._build_snapshot(symbol)

        trade_id = await self._sim.submit(
            strategy_name=STRATEGY_NAME,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            feature_snapshot=snapshot,
        )

        self._trade_count += 1
        if trade_id is None:
            self._reject_count += 1
        else:
            self._active[symbol] = trade_id

    def on_order_resolved(self, trade_id: int, filled: bool) -> None:
        """Called by the shadow motor when an order is filled or expired."""
        for sym, active_id in self._active.items():
            if active_id == trade_id:
                self._active[sym] = None
                if filled:
                    self._fill_count += 1
                break

    def _build_snapshot(self, symbol: str) -> dict:
        bid, ask = self._state.get_bid_ask(symbol)
        mid = self._state.get_mid(symbol)
        return {
            "ts_us": 0,
            "symbol": symbol,
            "best_bid_price": float(bid) if bid else None,
            "best_ask_price": float(ask) if ask else None,
            "mid":            float(mid) if mid else None,
            "fdusd_peg":      self._state.fdusd_peg,
            "mark_prices":    dict(self._state.mark_prices),
            "agg_trade_count": len(self._state.agg_trades.get(symbol, [])),
        }
