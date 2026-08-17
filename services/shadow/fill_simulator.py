"""
Realistic fill simulator for LIMIT_MAKER orders.

Does NOT import TradeExecutor. Structurally incapable of sending real orders.

Algorithm (pessimistic / RiskAdverse):
1. At signal time: check for -2010 rejection (order would cross spread).
2. If not rejected: estimate queue position = full level qty at entry.
3. On each aggTrade event at our price: drain queue.
4. Fill when queue = 0.
5. Expire if cancelled (max_hold_s reached or price moves away from level).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

import asyncpg

from ..recorder.state import AggTradeSample, MarketState
from .markout import MarkoutTracker, compute_realized_vol, compute_vpin_bucket
from .queue_model import (
    QueuePosition,
    build_queue_position,
    check_limit_maker_rejection,
)

logger = logging.getLogger(__name__)


@dataclass
class OpenOrder:
    db_id: int
    strategy_name: str
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    notional: float
    placed_ts_us: int
    queue: QueuePosition
    max_hold_s: float = 30.0


class FillSimulator:
    """
    Maintains a list of open (resting) simulated orders.
    Called on every aggTrade event by the shadow motor.
    Writes results to shadow_trades.

    Order count budget (simulated, per DECISIONES_TECNICAS limits):
        160 000/day, 50/10s.
    The counter is tracked and written to shadow_order_budget.
    It does NOT throttle in v1 — it only makes the consumption visible.
    """

    def __init__(
        self,
        db: asyncpg.Pool,
        state: MarketState,
        markout_tracker: MarkoutTracker,
    ):
        self._db = db
        self._state = state
        self._mo = markout_tracker
        self._orders: List[OpenOrder] = []
        # Order count tracking
        self._day_count: int = 0
        self._burst_window: list[float] = []   # monotonic timestamps of last 10s
        self._max_burst: int = 0
        self._today: str = ""

    async def submit(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        feature_snapshot: Optional[dict] = None,
    ) -> Optional[int]:
        """
        Simulate submitting a LIMIT_MAKER order.
        Returns the shadow_trades.id if accepted, None if rejected (-2010).
        """
        notional = float(price * qty)

        # Check -2010
        if check_limit_maker_rejection(side, price, self._state, symbol):
            trade_id = await self._db_insert(
                strategy_name, symbol, side, price, qty, notional,
                "REJECTED_2010", "-2010: would cross spread",
                feature_snapshot=feature_snapshot,
            )
            return None

        queue = build_queue_position(side, price, self._state, symbol)
        if queue is None:
            trade_id = await self._db_insert(
                strategy_name, symbol, side, price, qty, notional,
                "REJECTED_2010", "book not synced",
                feature_snapshot=feature_snapshot,
            )
            return None

        trade_id = await self._db_insert(
            strategy_name, symbol, side, price, qty, notional,
            "PENDING", None,
            queue_initial=float(queue.queue_at_entry),
            feature_snapshot=feature_snapshot,
        )

        self._record_order_count()
        self._orders.append(
            OpenOrder(
                db_id=trade_id,
                strategy_name=strategy_name,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                notional=notional,
                placed_ts_us=time.time_ns() // 1000,
                queue=queue,
            )
        )
        return trade_id

    def on_agg_trade(self, symbol: str, trade: AggTradeSample) -> List[OpenOrder]:
        """
        Feed an aggTrade into all open orders for this symbol.
        Returns list of NEWLY filled orders (transition: not-filled → filled).
        Edge-detects the fill so orders scheduled for async removal are not double-counted.
        """
        filled = []
        for order in self._orders:
            if order.symbol != symbol:
                continue
            was_filled = order.queue.is_filled
            order.queue.observe(trade)
            if order.queue.is_filled and not was_filled:
                filled.append(order)
        return filled

    async def process_fills(self, filled_orders: List[OpenOrder]) -> None:
        now_us = time.time_ns() // 1000
        for order in filled_orders:
            try:
                self._orders.remove(order)
            except ValueError:
                continue  # already removed by a concurrent fill call
            mid = self._state.get_mid(order.symbol)
            sigma = compute_realized_vol(self._state, order.symbol)
            vpin = compute_vpin_bucket(self._state, order.symbol)

            # Compute half-spread captured at fill time (bps).
            # mid_at_fill is the mid price the moment the order fills;
            # half_spread = how far our limit was from mid at that instant.
            half_spread_bps = None
            if mid is not None and mid > 0:
                sign = Decimal(1) if order.side == "BUY" else Decimal(-1)
                half_spread_bps = float(10_000 * sign * (mid - order.price) / mid)

            await self._db.execute(
                """
                UPDATE shadow_trades SET
                    fill_ts = NOW(), fill_price = $2, status = 'FILLED',
                    queue_pos_at_fill = 0,
                    mid_at_fill = $5,
                    half_spread_captured_bps = $6,
                    sigma_realized_60m = $3, vpin_bucket = $4
                WHERE id = $1
                """,
                order.db_id,
                float(order.price),
                sigma,
                vpin,
                float(mid) if mid is not None else None,
                half_spread_bps,
            )
            self._mo.register(
                trade_id=order.db_id,
                symbol=order.symbol,
                side=order.side,
                fill_price=order.price,
                mid_at_fill=mid,
            )
            logger.debug(f"fill [{order.strategy_name}] {order.symbol} {order.side} @ {order.price}")

    async def expire_old_orders(self) -> list:
        """Cancel orders open longer than max_hold_s. Returns expired OpenOrder list."""
        now_us = time.time_ns() // 1000
        expired = []
        for order in self._orders:
            age_s = (now_us - order.placed_ts_us) / 1_000_000
            if age_s > order.max_hold_s:
                expired.append(order)

        for order in expired:
            self._orders.remove(order)
            await self._db.execute(
                "UPDATE shadow_trades SET status='EXPIRED' WHERE id=$1",
                order.db_id,
            )
        return expired

    def _record_order_count(self) -> None:
        import time
        from datetime import date
        today = date.today().isoformat()
        if today != self._today:
            self._today = today
            self._day_count = 0
        self._day_count += 1
        now = time.monotonic()
        self._burst_window = [t for t in self._burst_window if now - t <= 10.0]
        self._burst_window.append(now)
        burst = len(self._burst_window)
        if burst > self._max_burst:
            self._max_burst = burst
        if self._day_count > 150_000:
            logger.warning(f"order count {self._day_count}/160000 — approaching daily limit")
        if burst > 45:
            logger.warning(f"burst {burst}/50 in 10s — approaching burst limit")

    async def flush_order_budget(self) -> None:
        """Write daily order count to shadow_order_budget."""
        from datetime import date
        today = date.today()
        try:
            await self._db.execute(
                """
                INSERT INTO shadow_order_budget (day, orders_placed, max_burst_10s, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (day) DO UPDATE SET
                    orders_placed = EXCLUDED.orders_placed,
                    max_burst_10s = GREATEST(shadow_order_budget.max_burst_10s, EXCLUDED.max_burst_10s),
                    updated_at = NOW()
                """,
                today, self._day_count, self._max_burst,
            )
        except Exception as e:
            logger.warning(f"order budget flush: {e}")

    async def _db_insert(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        notional: float,
        status: str,
        reject_reason: Optional[str],
        queue_initial: Optional[float] = None,
        feature_snapshot: Optional[dict] = None,
    ) -> int:
        from datetime import datetime, timezone
        row = await self._db.fetchrow(
            """
            INSERT INTO shadow_trades
                (strategy_name, signal_ts, symbol, side, signal_price,
                 qty, notional, queue_pos_initial, status, reject_reason, feature_snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            strategy_name,
            datetime.now(tz=timezone.utc),
            symbol,
            side,
            float(price),
            float(qty),
            notional,
            queue_initial,
            status,
            reject_reason,
            json.dumps(feature_snapshot) if feature_snapshot else None,
        )
        return row["id"]
