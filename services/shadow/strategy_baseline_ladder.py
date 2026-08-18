"""
baseline_ladder — passive fills using a 4-level ladder at random times.

Third arm of the A/B experiment, needed to isolate the two effects:

  cascade_shadow  −  baseline_ladder  =  value of the CASCADE SIGNAL
  baseline_ladder −  baseline_random  =  value of the PLACEMENT (ladder vs at-market)

Design:
  - Every TRIGGER_INTERVAL_S seconds (per symbol), place a 4-order ladder
    at offsets [25, 50, 100, 200] bps from the current mid.
  - Side is random — no cascade condition.
  - Same notional and markout horizons as cascade_shadow.
  - Tagged strategy_name="baseline_ladder".

This arm will populate much faster than cascade_shadow because it doesn't
wait for cascade events. Use it as the primary control for fill-rate comparison.
"""

import asyncio
import logging
import random
from decimal import Decimal
from typing import List

from ..recorder.state import MarketState
from .fill_simulator import FillSimulator

logger = logging.getLogger(__name__)

STRATEGY_NAME = "baseline_ladder"
ORDER_NOTIONAL = 10.0
OFFSETS_BPS = [25, 50, 100, 200]
TRIGGER_INTERVAL_S = 120.0   # place a ladder every 2 min per symbol


class BaselineLadder:
    """
    Places a 4-level passive limit ladder at random intervals.
    Control arm that shares ladder placement with cascade_shadow but has no signal.
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
        self._stopped = False

    async def run(self) -> None:
        if self._stopped:
            return
        tasks = [
            asyncio.create_task(self._symbol_loop(sym, i * 7.0))
            for i, sym in enumerate(self._symbols)
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()

    async def _symbol_loop(self, symbol: str, initial_delay: float) -> None:
        await asyncio.sleep(initial_delay)
        while not self._stopped:
            await asyncio.sleep(TRIGGER_INTERVAL_S)
            try:
                await self._place_ladder(symbol, side=random.choice(["BUY", "SELL"]))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"baseline_ladder [{symbol}]: {e}")

    async def _place_ladder(self, symbol: str, side: str) -> None:
        book = self._state.books.get(symbol)
        if book is None or not book.is_synced:
            return

        mid = self._state.get_mid(symbol)
        if mid is None or mid <= 0:
            return

        info = self._state.exchange_info.get(symbol, {})
        step_size = info.get("step_size")
        tick_size = info.get("tick_size")
        if not step_size or not tick_size:
            return

        step = Decimal(str(step_size))
        tick = Decimal(str(tick_size))
        mid_d = Decimal(str(mid))

        for offset_bps in OFFSETS_BPS:
            if side == "BUY":
                price = mid_d * (1 - Decimal(offset_bps) / 10_000)
            else:
                price = mid_d * (1 + Decimal(offset_bps) / 10_000)

            # Round price to tick size
            price = (price / tick).quantize(Decimal(1)) * tick

            raw_qty = Decimal(str(ORDER_NOTIONAL)) / price
            qty = (raw_qty // step) * step
            if qty <= 0:
                continue

            await self._sim.submit(
                strategy_name=STRATEGY_NAME,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                offset_bps=offset_bps,
                max_hold_s=900.0,  # 15 min — enough for cascade to reach our level
            )

    def on_order_resolved(self, trade_id: int, filled: bool) -> None:
        pass  # stateless — nothing to clean up
