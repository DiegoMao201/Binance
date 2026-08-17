"""
Pessimistic queue model (RiskAdverse type from hftbacktest).

Rules:
- Queue position at entry = full quantity at our level (we're last in queue).
- Queue advances ONLY from aggressive trades hitting our price.
  Cancellations do NOT advance our position (pessimistic hypothesis).
- Fill when accumulated aggressive volume at our level >= our initial queue depth.
- LIMIT_MAKER rejected (-2010) if price would cross the spread at submission.

This model cannot be imported from TradeExecutor. It is structurally isolated.
"""

from decimal import Decimal
from typing import List, Optional

from ..recorder.state import AggTradeSample, MarketState


class QueuePosition:
    """Tracks one resting order's queue position."""

    def __init__(
        self,
        symbol: str,
        side: str,       # "BUY" or "SELL"
        price: Decimal,
        queue_at_entry: Decimal,  # full level qty at submission time
    ):
        self.symbol = symbol
        self.side = side
        self.price = price
        self.queue_at_entry = queue_at_entry
        self.volume_consumed: Decimal = Decimal(0)

    def observe(self, trade: AggTradeSample) -> None:
        """
        A completed aggTrade may consume queue ahead of us.

        Binance isBuyerMaker=True: buyer is the maker (bid), seller is aggressor.
        Binance isBuyerMaker=False: seller is the maker (ask), buyer is aggressor.

        For a resting BUY at price P (we're maker at bid):
          Aggressive sells hit our bid → isBuyerMaker=True → drain.
        For a resting SELL at price P (we're maker at ask):
          Aggressive buys hit our ask → isBuyerMaker=False → drain.
        """
        if trade.price != self.price:
            return
        if self.side == "BUY" and trade.is_buyer_maker:
            self.volume_consumed += trade.qty
        elif self.side == "SELL" and not trade.is_buyer_maker:
            self.volume_consumed += trade.qty

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.queue_at_entry - self.volume_consumed)

    @property
    def is_filled(self) -> bool:
        """True when all queue ahead of us has been consumed."""
        return self.queue_at_entry > 0 and self.remaining == Decimal(0)


def check_limit_maker_rejection(
    side: str,
    price: Decimal,
    state: MarketState,
    symbol: str,
) -> bool:
    """
    Return True if a LIMIT_MAKER at `price` would be rejected with -2010.
    -2010 occurs when the order would immediately cross the spread.

    BUY at price P: rejected if P >= best_ask (would be a taker).
    SELL at price P: rejected if P <= best_bid (would be a taker).
    """
    book = state.books.get(symbol)
    if book is None or not book.is_synced:
        # Conservative: if we don't know the spread, treat as rejected
        return True

    if side == "BUY":
        ask = book.best_ask
        if ask is None:
            return True
        return price >= ask[0]
    else:
        bid = book.best_bid
        if bid is None:
            return True
        return price <= bid[0]


def build_queue_position(
    side: str,
    price: Decimal,
    state: MarketState,
    symbol: str,
) -> Optional[QueuePosition]:
    """
    Create a QueuePosition for a new resting order.
    Returns None if the book is unsynced (order cannot be placed safely).
    """
    book = state.books.get(symbol)
    if book is None or not book.is_synced:
        return None

    level_qty = book.qty_at_level(side, price)
    return QueuePosition(
        symbol=symbol,
        side=side,
        price=price,
        queue_at_entry=level_qty,
    )
