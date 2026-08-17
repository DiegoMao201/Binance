"""
baseline_burnin — high-frequency burn-in strategy for motor validation only.

Purpose: exercise every code path (fill, expire, -2010 reject, markout) fast,
so the 5-outcome / ≥500-fill acceptance criteria are met within 24h.

NOT a real strategy. Tagged with strategy_name="baseline_burnin".
NEVER used for any EV or signal analysis — it's a test harness.

Auto-stops after BURNIN_FILL_TARGET fills. Then the motor runs on baseline_random only.
"""

import asyncio
import logging
import random
from decimal import Decimal
from typing import List, Optional

from ..recorder.state import MarketState
from .fill_simulator import FillSimulator

logger = logging.getLogger(__name__)

STRATEGY_NAME = "baseline_burnin"
SIGNAL_INTERVAL_S = 5.0      # 5s per symbol → ~72 attempts/min across 6 symbols
ORDER_NOTIONAL = 10.0
BURNIN_FILL_TARGET = 500     # auto-stop after 500 fills


class BurnIn:
    """
    High-cadence coin-flip maker for validation.
    One independent timer per symbol, 5s cycle, staggered 1s apart.
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
        self._active: dict[str, Optional[int]] = {s: None for s in symbols}
        self._fill_count = 0
        self._stopped = False

    async def run(self) -> None:
        if self._stopped:
            return
        tasks = [
            asyncio.create_task(self._symbol_loop(sym, i * 1.0))
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
            await asyncio.sleep(SIGNAL_INTERVAL_S)
            if self._stopped:
                break
            try:
                if self._active[symbol] is not None:
                    continue
                await self._place_signal(symbol)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"burnin [{symbol}]: {e}")

    async def _place_signal(self, symbol: str) -> None:
        side = random.choice(["BUY", "SELL"])
        book = self._state.books.get(symbol)
        if book is None or not book.is_synced:
            return

        level = book.best_bid if side == "BUY" else book.best_ask
        if level is None:
            return
        price = level[0]

        info = self._state.exchange_info.get(symbol, {})
        step_size = info.get("step_size")
        if not step_size:
            return

        raw_qty = Decimal(str(ORDER_NOTIONAL)) / price
        step = Decimal(str(step_size))
        qty = (raw_qty // step) * step
        if qty <= 0:
            return

        trade_id = await self._sim.submit(
            strategy_name=STRATEGY_NAME,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
        )
        if trade_id is not None:
            self._active[symbol] = trade_id

    def on_order_resolved(self, trade_id: int, filled: bool) -> None:
        for sym, active_id in self._active.items():
            if active_id == trade_id:
                self._active[sym] = None
                if filled:
                    self._fill_count += 1
                    if self._fill_count >= BURNIN_FILL_TARGET:
                        logger.info(
                            f"burnin complete: {self._fill_count} fills reached — stopping"
                        )
                        self._stopped = True
                break
