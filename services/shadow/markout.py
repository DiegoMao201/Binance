"""
Markout calculation: adverse-selection signal after a fill.

markout_h = sign * (mid_{t+h} - fill_price)

where sign = +1 for BUY (we want mid to go up after buying),
             -1 for SELL (we want mid to go down after selling).

A negative markout_h means the market moved against us after the fill
— classic adverse-selection signature.

Per DECISIONES_TECNICAS:
  If E[markout_60s] + medio_spread < 0 in high-vol regime,
  passive strategy during cascades is unviable — stop and redesign.
"""

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Deque, Dict, List, Optional

import asyncpg

from ..recorder.state import MarketState

logger = logging.getLogger(__name__)

HORIZONS_S = [1, 5, 30, 60, 300, 900, 1800, 3600]   # seconds; long = 5m/15m/30m/60m
# Raw legacy columns — short horizons only (kept for backward compat)
HORIZON_COLS = {1: "markout_1s", 5: "markout_5s", 30: "markout_30s", 60: "markout_60s"}
# bps columns: markout_h_bps = half_spread_captured_bps + adverse_drift_h_bps
HORIZON_BPS_COLS = {
    1: "markout_1s_bps",    5: "markout_5s_bps",
    30: "markout_30s_bps",  60: "markout_60s_bps",
    300: "markout_5m_bps",  900: "markout_15m_bps",
    1800: "markout_30m_bps", 3600: "markout_60m_bps",
}
HORIZON_DRIFT_COLS = {
    1: "adverse_drift_1s_bps",   5: "adverse_drift_5s_bps",
    30: "adverse_drift_30s_bps", 60: "adverse_drift_60s_bps",
    300: "adverse_drift_5m_bps",  900: "adverse_drift_15m_bps",
    1800: "adverse_drift_30m_bps", 3600: "adverse_drift_60m_bps",
}


@dataclass
class PendingMarkout:
    trade_id: int
    symbol: str
    side: str              # "BUY" or "SELL"
    fill_price: Decimal
    fill_ts_us: int        # microseconds, monotonic-derived
    mid_at_fill: Optional[Decimal]   # mid price at fill instant, for bps decomposition
    remaining_horizons: List[int] = field(default_factory=lambda: list(HORIZONS_S))


class MarkoutTracker:
    """
    Watches filled shadow_trades and fills in markout_Ns columns as time passes.
    Runs as a background asyncio task in the same event loop as the recorder.
    """

    def __init__(self, db: asyncpg.Pool, state: MarketState):
        self._db = db
        self._state = state
        self._pending: Deque[PendingMarkout] = deque()

    def register(
        self,
        trade_id: int,
        symbol: str,
        side: str,
        fill_price: Decimal,
        mid_at_fill: Optional[Decimal] = None,
    ) -> None:
        fill_ts_us = time.time_ns() // 1000
        self._pending.append(
            PendingMarkout(trade_id, symbol, side, fill_price, fill_ts_us, mid_at_fill)
        )

    async def backfill_on_startup(self) -> None:
        """
        On startup, find fills with pending markouts and re-queue them.
        Window: 65 minutes (covers the full 60m horizon plus a buffer).
        Condition: markout_60s NULL (short horizons not yet computed) OR
                   markout_60m_bps NULL (long horizons not yet computed).
        """
        rows = await self._db.fetch(
            """
            SELECT id, symbol, side, fill_price, mid_at_fill,
                   extract(epoch from fill_ts) * 1e6 AS fill_ts_us
            FROM shadow_trades
            WHERE fill_ts IS NOT NULL
              AND (markout_60s IS NULL
                   OR markout_60m_bps IS NULL
                   OR markout_5m_bps  IS NULL)
              AND fill_ts > NOW() - INTERVAL '65 minutes'
            """
        )
        for row in rows:
            mid_at_fill = Decimal(str(row["mid_at_fill"])) if row["mid_at_fill"] is not None else None
            self._pending.append(
                PendingMarkout(
                    trade_id=row["id"],
                    symbol=row["symbol"],
                    side=row["side"],
                    fill_price=Decimal(str(row["fill_price"])),
                    fill_ts_us=int(row["fill_ts_us"]),
                    mid_at_fill=mid_at_fill,
                )
            )
        if rows:
            logger.info(f"markout backfill: re-queued {len(rows)} fills from restart")

    async def run(self, poll_interval_s: float = 0.5) -> None:
        while True:
            await asyncio.sleep(poll_interval_s)
            now_us = time.time_ns() // 1000
            completed = []
            for pm in list(self._pending):
                done = await self._try_fill(pm, now_us)
                if done:
                    completed.append(pm)
            for pm in completed:
                try:
                    self._pending.remove(pm)
                except ValueError:
                    pass

    async def _try_fill(self, pm: PendingMarkout, now_us: int) -> bool:
        """Fill markout values for elapsed horizons. Returns True when all done."""
        if not pm.remaining_horizons:
            return True

        updates: Dict[str, float] = {}
        still_pending: List[int] = []
        sign = Decimal(1) if pm.side == "BUY" else Decimal(-1)

        for h in pm.remaining_horizons:
            elapsed_s = (now_us - pm.fill_ts_us) / 1_000_000
            if elapsed_s < h:
                still_pending.append(h)
                continue

            # Look up mid at fill_ts + h seconds
            target_us = pm.fill_ts_us + h * 1_000_000
            mid = self._state.get_mid_at_us(pm.symbol, target_us)
            if mid is None:
                # History not in memory — try DB snapshot (after restart)
                mid = await self._mid_from_db(pm.symbol, target_us)
            if mid is None:
                # No data available — never fall back to current mid.
                # NULL is honest ("we don't know"); current mid would bias toward 0,
                # masking adverse selection. Give up after 5 min and leave as NULL.
                if elapsed_s < h + 300:
                    still_pending.append(h)
                # else: column stays NULL in DB — correct, not silence
                continue

            raw_markout = float(sign * (mid - pm.fill_price))
            # Raw legacy columns exist only for short horizons
            if h in HORIZON_COLS:
                updates[HORIZON_COLS[h]] = raw_markout

            # bps markout: 10000 * sign * (mid_h - fill_price) / fill_price
            if pm.fill_price > 0:
                updates[HORIZON_BPS_COLS[h]] = float(
                    10_000 * sign * (mid - pm.fill_price) / pm.fill_price
                )

            # bps adverse drift: 10000 * sign * (mid_h - mid_at_fill) / mid_at_fill
            # Only meaningful when we have mid_at_fill; NULL is correct otherwise.
            if pm.mid_at_fill is not None and pm.mid_at_fill > 0:
                updates[HORIZON_DRIFT_COLS[h]] = float(
                    10_000 * sign * (mid - pm.mid_at_fill) / pm.mid_at_fill
                )

        pm.remaining_horizons = still_pending

        if updates:
            set_clause = ", ".join(f"{col} = ${i+2}" for i, col in enumerate(updates))
            values = [pm.trade_id] + list(updates.values())
            try:
                await self._db.execute(
                    f"UPDATE shadow_trades SET {set_clause} WHERE id = $1",
                    *values,
                )
            except Exception as e:
                logger.warning(f"markout update [{pm.trade_id}]: {e}")

        return not pm.remaining_horizons


    async def _mid_from_db(self, symbol: str, target_us: int) -> Optional[Decimal]:
        """Query binance_mid_snapshots for the closest mid to target_us."""
        from datetime import datetime, timezone
        target_ts = datetime.fromtimestamp(target_us / 1_000_000, tz=timezone.utc)
        try:
            row = await self._db.fetchrow(
                """
                SELECT mid FROM binance_mid_snapshots
                WHERE symbol = $1
                  AND ts BETWEEN $2::timestamptz - INTERVAL '30 seconds'
                             AND $2::timestamptz + INTERVAL '30 seconds'
                ORDER BY abs(extract(epoch from (ts - $2::timestamptz)))
                LIMIT 1
                """,
                symbol, target_ts,
            )
            return Decimal(str(row["mid"])) if row else None
        except Exception as e:
            logger.warning(f"_mid_from_db [{symbol} t={target_ts}]: {type(e).__name__}: {e}")
            return None


def compute_realized_vol(
    state: MarketState, symbol: str, window_s: int = 3600
) -> Optional[float]:
    """
    Compute realized volatility over the last `window_s` seconds
    from mid price history. Returns annualized σ.
    """
    history = state.mid_history.get(symbol)
    if not history or len(history) < 2:
        return None

    now_us = time.time_ns() // 1000
    cutoff_us = now_us - window_s * 1_000_000
    samples = [s for s in history if s.ts_us >= cutoff_us]
    if len(samples) < 10:
        return None

    log_returns = []
    for i in range(1, len(samples)):
        prev = float(samples[i - 1].mid)
        curr = float(samples[i].mid)
        if prev > 0 and curr > 0:
            log_returns.append(math.log(curr / prev))

    if len(log_returns) < 5:
        return None

    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    # Annualize: assume ~10 samples/s → 315_360_000 samples/year
    samples_per_s = len(samples) / (window_s or 1)
    ann_factor = math.sqrt(samples_per_s * 365 * 24 * 3600)
    return math.sqrt(variance) * ann_factor


def compute_vpin_bucket(
    state: MarketState, symbol: str, n_buckets: int = 5
) -> int:
    """
    Approximate VPIN bucket [1..5] from recent taker buy/sell volume imbalance.
    Bucket 5 = high VPIN (informed flow, avoid quoting).
    """
    trades = state.agg_trades.get(symbol)
    if not trades or len(trades) < 10:
        return 3  # neutral when insufficient data

    buy_vol = sum(float(t.qty) for t in trades if t.is_buyer_maker)
    sell_vol = sum(float(t.qty) for t in trades if not t.is_buyer_maker)
    total = buy_vol + sell_vol
    if total == 0:
        return 3

    # VPIN ≈ |buy_vol - sell_vol| / total_vol
    vpin = abs(buy_vol - sell_vol) / total  # [0, 1]
    bucket = min(n_buckets, max(1, int(vpin * n_buckets) + 1))
    return bucket
