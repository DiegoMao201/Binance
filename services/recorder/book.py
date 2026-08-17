"""
Order book reconstruction with correct U/u diff stream synchronisation.

Reference: cryptofeed/exchanges/binance.py::_check_update_id
Covers 4 cases including messages buffered during snapshot download.
Uses Decimal (not float) to avoid rounding at sub-tick levels.
Never continues with a corrupted book — resync on any gap.
"""

from collections import deque
from decimal import Decimal
from typing import Deque, Dict, Optional, Tuple


class OrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: Dict[Decimal, Decimal] = {}
        self.asks: Dict[Decimal, Decimal] = {}

        self._snapshot_last_update_id: int = 0
        self._last_update_id: int = 0
        self._synced: bool = False
        self._pending: Deque[dict] = deque()
        self._gap_count: int = 0
        self._total_updates: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def buffer(self, msg: dict) -> None:
        """Buffer a diff message received before snapshot arrives."""
        self._pending.append(msg)

    def apply_snapshot(self, data: dict) -> None:
        """
        Apply REST snapshot (GET /api/v3/depth?limit=1000).
        Drains any buffered messages that follow the snapshot.
        """
        last_id: int = data["lastUpdateId"]
        self.bids = {
            Decimal(p): Decimal(q) for p, q in data["bids"] if q != "0"
        }
        self.asks = {
            Decimal(p): Decimal(q) for p, q in data["asks"] if q != "0"
        }
        self._snapshot_last_update_id = last_id
        self._last_update_id = last_id
        self._synced = False

        while self._pending:
            msg = self._pending.popleft()
            result = self._try_apply(msg)
            if result == "gap":
                # Gap in buffered messages — discard buffer and resync externally
                self._pending.clear()
                self._synced = False
                self._gap_count += 1
                return
        self._synced = True

    def update(self, msg: dict) -> bool:
        """
        Process a live diff message.
        Returns True when the book was updated.
        Returns False and buffers when not yet synced.
        Caller must trigger a snapshot resync when this returns False after sync was True.
        """
        if not self._synced:
            self._pending.append(msg)
            return False

        result = self._try_apply(msg)
        if result == "gap":
            self._gap_count += 1
            self._synced = False
            return False
        return result in ("synced", "applied")

    @property
    def is_synced(self) -> bool:
        return self._synced

    @property
    def gap_count(self) -> int:
        return self._gap_count

    @property
    def best_bid(self) -> Optional[Tuple[Decimal, Decimal]]:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    @property
    def best_ask(self) -> Optional[Tuple[Decimal, Decimal]]:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    @property
    def mid(self) -> Optional[Decimal]:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        return (bid[0] + ask[0]) / 2

    def qty_at_level(self, side: str, price: Decimal) -> Decimal:
        if side == "BUY":
            return self.bids.get(price, Decimal(0))
        return self.asks.get(price, Decimal(0))

    def qty_ahead_of(self, side: str, price: Decimal) -> Decimal:
        """
        Queue depth ahead of an order at `price` (pessimistic: we're last).
        For BUY: sum of bids strictly above our price (better price = higher in queue).
        For SELL: sum of asks strictly below our price.
        """
        if side == "BUY":
            return sum(q for p, q in self.bids.items() if p > price)
        return sum(q for p, q in self.asks.items() if p < price)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _try_apply(self, msg: dict) -> str:
        """
        Returns: "synced" | "applied" | "old" | "gap"

        Binance depth diff message fields:
          U — first update ID in this event
          u — final update ID in this event
          b — bid diffs [[price, qty], ...]
          a — ask diffs [[price, qty], ...]
        """
        U: int = msg["U"]
        u: int = msg["u"]

        # Discard messages older than snapshot
        if u <= self._snapshot_last_update_id:
            return "old"

        if not self._synced:
            # First sync: need U <= snapshot_lastUpdateId + 1 <= u
            if U <= self._snapshot_last_update_id + 1 <= u:
                self._apply_levels(msg)
                self._last_update_id = u
                self._synced = True
                return "synced"
            if U > self._snapshot_last_update_id + 1:
                # Gap between snapshot and first valid message
                return "gap"
            return "old"

        # Subsequent: U must be exactly last_u + 1 (no gaps, no duplicates)
        if U != self._last_update_id + 1:
            return "gap"

        self._apply_levels(msg)
        self._last_update_id = u
        self._total_updates += 1
        return "applied"

    def _apply_levels(self, msg: dict) -> None:
        for price_s, qty_s in msg.get("b", []):
            price = Decimal(price_s)
            qty = Decimal(qty_s)
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty

        for price_s, qty_s in msg.get("a", []):
            price = Decimal(price_s)
            qty = Decimal(qty_s)
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
