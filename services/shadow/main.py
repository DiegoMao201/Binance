"""
Shadow motor — runs alongside the recorder in the same asyncio event loop.
Shares MarketState with StreamHandler for real-time book + trade access.

Wiring:
  StreamHandler feeds aggTrades → FillSimulator.on_agg_trade()
  BaselineRandom generates signals → FillSimulator.submit()
  MarkoutTracker fills markout columns as time passes
  shadow_equity is updated every 60 s
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List

import asyncpg

from ..recorder.state import MarketState
from .fill_simulator import FillSimulator, OpenOrder
from .markout import MarkoutTracker
from .strategy_random import BaselineRandom, STRATEGY_NAME

logger = logging.getLogger(__name__)


class ShadowMotor:
    def __init__(self, db: asyncpg.Pool, state: MarketState, symbols: List[str]):
        self._db = db
        self._state = state
        self._symbols = symbols

        self._tracker = MarkoutTracker(db, state)
        self._sim = FillSimulator(db, state, self._tracker)
        self._strategies: List[BaselineRandom] = [
            BaselineRandom(symbols, state, self._sim)
        ]

    def tasks(self) -> list:
        return [
            asyncio.create_task(self._trade_loop(), name="shadow-trade-loop"),
            asyncio.create_task(self._tracker.run(), name="shadow-markout"),
            asyncio.create_task(self._expire_loop(), name="shadow-expire"),
            asyncio.create_task(self._equity_loop(), name="shadow-equity"),
            *[
                asyncio.create_task(s.run(), name=f"shadow-strategy-{i}")
                for i, s in enumerate(self._strategies)
            ],
        ]

    def on_agg_trade(self, symbol: str, trade) -> None:
        """
        Called by StreamHandler on every incoming aggTrade.
        Routes to fill simulator; fills are processed in _trade_loop.
        """
        filled = self._sim.on_agg_trade(symbol, trade)
        if filled:
            # schedule DB update without blocking the WS handler
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self._handle_fills(filled))
            )

    async def _handle_fills(self, filled: List[OpenOrder]) -> None:
        await self._sim.process_fills(filled)
        # notify strategies
        for order in filled:
            for s in self._strategies:
                s.on_order_resolved(order.db_id, filled=True)

    async def _trade_loop(self) -> None:
        """Periodic book snapshot for strategies that need it."""
        while True:
            await asyncio.sleep(1.0)

    async def _expire_loop(self) -> None:
        """Cancel stale open orders and notify strategies."""
        while True:
            await asyncio.sleep(5.0)
            try:
                expired = await self._sim.expire_old_orders()
                for order in expired:
                    for s in self._strategies:
                        s.on_order_resolved(order.db_id, filled=False)
            except Exception as e:
                logger.warning(f"expire loop: {e}")

    async def _equity_loop(self) -> None:
        """Snapshot equity curve and order budget into DB every 60 s."""
        while True:
            await asyncio.sleep(60.0)
            try:
                await self._snapshot_equity()
                await self._sim.flush_order_budget()
            except Exception as e:
                logger.warning(f"equity snapshot: {e}")

    async def _snapshot_equity(self) -> None:
        ts = datetime.now(tz=timezone.utc)
        row = await self._db.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status='FILLED') AS fill_count,
                COUNT(*) FILTER (WHERE status LIKE 'REJECTED%') AS reject_count,
                COUNT(*) AS trade_count,
                COALESCE(SUM(markout_60s), 0) AS realized_pnl
            FROM shadow_trades
            WHERE strategy_name = $1
            """,
            STRATEGY_NAME,
        )
        if row is None:
            return

        await self._db.execute(
            """
            INSERT INTO shadow_equity
                (ts, strategy_name, equity, realized_pnl, trade_count, fill_count, reject_count)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (ts, strategy_name) DO NOTHING
            """,
            ts,
            STRATEGY_NAME,
            float(row["realized_pnl"] or 0),
            float(row["realized_pnl"] or 0),
            int(row["trade_count"] or 0),
            int(row["fill_count"] or 0),
            int(row["reject_count"] or 0),
        )
