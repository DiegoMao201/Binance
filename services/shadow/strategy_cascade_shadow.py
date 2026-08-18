"""
cascade_shadow — passive fills conditioned on cascade detector signal.

This is the TREATMENT ARM of the A/B experiment:

  baseline_random  →  control  (fills at any time, at best bid/ask)
  baseline_ladder  →  control+ (fills at any time, same ladder as cascade_shadow)
  cascade_shadow   →  treatment (fills only when cascade detector fires, ladder)

Three-way comparison isolates two distinct effects:
  cascade_shadow − baseline_ladder  =  value of the CASCADE SIGNAL
  baseline_ladder − baseline_random =  value of the PLACEMENT (ladder vs at-market)

Design:
  - Cascade detector fires on_cascade(symbol, direction="LONG_LIQ"|"SHORT_LIQ")
  - LONG_LIQ (price falling) → enter BUY (wait for cascade to come down to us)
  - SHORT_LIQ (price rising) → enter SELL (wait for cascade to come up to us)
  - On detection: capture mid_at_detection ONCE, place 4 orders at 25/50/100/200 bps
  - Ladders are anchored to mid_at_detection, NOT best bid (which moves during crash)
  - offset_bps column records which rung of the ladder each order sits on

Markout note: markout tracker is decoupled from order lifetime.
  MAX_HOLD_S = 900 applies to UNFILLED orders (they expire).
  For FILLED orders, markout is computed at all horizons up to 3600s (60m)
  regardless of when the originating order expired. The tracker only cares
  about fill_ts, not order status.

Status: STUB. Detector (BLOQUE C) must be wired before orders are submitted.
        Until then, _cascade_active is always None and no orders are placed.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Dict, List, Optional

from ..recorder.state import MarketState
from .fill_simulator import FillSimulator

logger = logging.getLogger(__name__)

STRATEGY_NAME = "cascade_shadow"
ORDER_NOTIONAL = 10.0
OFFSETS_BPS = [25, 50, 100, 200]
MAX_HOLD_S = 900.0   # 15 min — unfilled orders expire; markout tracking is independent


class CascadeShadow:
    """
    Places a 4-level passive limit ladder anchored to mid at cascade detection.
    Direction is conditioned on cascade type: LONG_LIQ→BUY, SHORT_LIQ→SELL.
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
        # None = inactive; "LONG_LIQ" or "SHORT_LIQ" = active
        self._cascade_active: Dict[str, Optional[str]] = {s: None for s in symbols}
        self._mid_at_detection: Dict[str, Optional[Decimal]] = {s: None for s in symbols}
        self._stopped = False

    # --- Cascade signal interface (called by detector when BLOQUE C is wired) ---

    def on_cascade(self, symbol: str, direction: str) -> None:
        """
        Detector fires a cascade event.

        direction: "LONG_LIQ" (price falling, forced long sellers)
                   "SHORT_LIQ" (price rising, forced short sellers)

        Captures mid at this exact moment — ladder anchors here, not to the
        best bid which is moving fast during the cascade.
        """
        if symbol not in self._cascade_active:
            return
        mid = self._state.get_mid(symbol)
        if mid is None or float(mid) <= 0:
            logger.warning(f"cascade_shadow [{symbol}]: cascade detected but no mid — skipping")
            return
        self._cascade_active[symbol] = direction
        self._mid_at_detection[symbol] = Decimal(str(mid))
        logger.info(
            f"cascade_shadow [{symbol}]: {direction} cascade — "
            f"mid_at_detection={float(mid):.4f} — placing ladder"
        )
        # Schedule ladder placement without blocking the detector callback
        asyncio.get_event_loop().create_task(self._place_ladder(symbol))

    def on_cascade_end(self, symbol: str) -> None:
        """Detector clears: new cascades can be detected again."""
        if symbol in self._cascade_active:
            self._cascade_active[symbol] = None
            self._mid_at_detection[symbol] = None
            logger.info(f"cascade_shadow [{symbol}]: cascade ended")

    # --- Main loop (kept for compatibility; actual placement is event-driven) ---

    async def run(self) -> None:
        # This strategy is event-driven: ladder placement happens in on_cascade().
        # The run() loop just keeps the task alive.
        while not self._stopped:
            await asyncio.sleep(60.0)

    # --- Ladder placement ---

    async def _place_ladder(self, symbol: str) -> None:
        direction = self._cascade_active.get(symbol)
        mid_d = self._mid_at_detection.get(symbol)
        if direction is None or mid_d is None:
            return

        side = "BUY" if direction == "LONG_LIQ" else "SELL"

        info = self._state.exchange_info.get(symbol, {})
        step_size = info.get("step_size")
        tick_size = info.get("tick_size")
        if not step_size or not tick_size:
            logger.warning(f"cascade_shadow [{symbol}]: missing exchange_info — no ladder")
            return

        step = Decimal(str(step_size))
        tick = Decimal(str(tick_size))

        placed = 0
        for offset_bps in OFFSETS_BPS:
            if side == "BUY":
                price = mid_d * (1 - Decimal(offset_bps) / 10_000)
            else:
                price = mid_d * (1 + Decimal(offset_bps) / 10_000)

            price = (price / tick).quantize(Decimal(1)) * tick

            raw_qty = Decimal(str(ORDER_NOTIONAL)) / price
            qty = (raw_qty // step) * step
            if qty <= 0:
                continue

            trade_id = await self._sim.submit(
                strategy_name=STRATEGY_NAME,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                offset_bps=offset_bps,
                max_hold_s=MAX_HOLD_S,
            )
            if trade_id is not None:
                placed += 1

        logger.info(
            f"cascade_shadow [{symbol}]: {side} ladder placed — "
            f"{placed}/{len(OFFSETS_BPS)} rungs accepted"
        )

    def on_order_resolved(self, trade_id: int, filled: bool) -> None:
        pass  # ladder orders are fire-and-forget; cascade_active cleared by on_cascade_end()
