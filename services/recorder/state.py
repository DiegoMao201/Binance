"""
Shared in-process market state — single asyncio event loop, no locks needed.
Consumed by both the recorder streams and the shadow motor.
"""

from collections import deque
from decimal import Decimal
from typing import Any, Deque, Dict, List, Optional, Tuple  # noqa: F401

from .book import OrderBook


class MidSample:
    __slots__ = ("ts_us", "mid")

    def __init__(self, ts_us: int, mid: Decimal):
        self.ts_us = ts_us
        self.mid = mid


class AggTradeSample:
    __slots__ = ("ts_ms", "price", "qty", "is_buyer_maker")

    def __init__(self, ts_ms: int, price: Decimal, qty: Decimal, is_buyer_maker: bool):
        self.ts_ms = ts_ms
        self.price = price
        self.qty = qty
        self.is_buyer_maker = is_buyer_maker


class MarketState:
    def __init__(self, spot_symbols: List[str], futures_symbols: List[str]):
        # Reconstructed spot order books
        self.books: Dict[str, OrderBook] = {s: OrderBook(s) for s in spot_symbols}

        # Latest bookTicker snapshots per symbol (also fdusdusdt)
        self.book_tickers: Dict[str, dict] = {}

        # Mid price history. At ~1.2 samples/s (bookTicker-driven), maxlen=7200 covers
        # ~100 minutes — enough for the 60m markout horizon with headroom.
        self.mid_history: Dict[str, Deque[MidSample]] = {
            s: deque(maxlen=7200) for s in spot_symbols
        }

        # Recent aggTrades per symbol — shadow motor uses these to drain queue
        self.agg_trades: Dict[str, Deque[AggTradeSample]] = {
            s: deque(maxlen=2000) for s in spot_symbols
        }

        # Exchange info cache (refreshed daily via REST)
        self.exchange_info: Dict[str, Dict[str, Any]] = {
            s: {} for s in spot_symbols
        }

        # OI snapshots per futures symbol — updated by RestPoller.run_oi_poll()
        # Each entry: (ts_us, open_interest_value_usd)
        self.oi_snapshots: Dict[str, Deque[Tuple[int, float]]] = {
            s: deque(maxlen=24) for s in futures_symbols  # 24 × 5min ≈ 2h
        }

        # FDUSD peg monitor
        self.fdusd_peg: float = 1.0

        # Mark prices from futures stream
        self.mark_prices: Dict[str, float] = {}

        # Pending resync requests — set by book.update() returning False (gap)
        self.resync_needed: Dict[str, bool] = {s: True for s in spot_symbols}

    def get_mid(self, symbol: str) -> Optional[Decimal]:
        book = self.books.get(symbol)
        if book and book.is_synced:
            return book.mid
        bt = self.book_tickers.get(symbol)
        if bt:
            bid = Decimal(str(bt.get("b", 0)))
            ask = Decimal(str(bt.get("a", 0)))
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
        return None

    def get_mid_at_us(self, symbol: str, ts_us: int) -> Optional[Decimal]:
        """
        Return the mid price closest to ts_us from history.
        Returns None if history is empty or ts_us is too old.
        """
        history = self.mid_history.get(symbol)
        if not history:
            return None
        # history is time-ordered (deque, oldest on left)
        # linear scan backward from most recent
        best: Optional[Decimal] = None
        min_diff = float("inf")
        for sample in reversed(history):
            diff = abs(sample.ts_us - ts_us)
            if diff < min_diff:
                min_diff = diff
                best = sample.mid
            else:
                break  # deque is monotonically increasing in time
        return best

    def record_mid(self, symbol: str, ts_us: int) -> None:
        mid = self.get_mid(symbol)
        if mid is not None:
            self.mid_history[symbol].append(MidSample(ts_us, mid))

    def get_bid_ask(self, symbol: str) -> tuple:
        """Returns (bid, ask) as Decimal or (None, None)."""
        book = self.books.get(symbol)
        if book and book.is_synced:
            bid = book.best_bid
            ask = book.best_ask
            if bid and ask:
                return bid[0], ask[0]
        bt = self.book_tickers.get(symbol)
        if bt:
            return Decimal(str(bt.get("b", 0))), Decimal(str(bt.get("a", 0)))
        return None, None
