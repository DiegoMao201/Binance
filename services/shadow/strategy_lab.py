"""
Signal lab — 15 strategy variants deployed simultaneously.

Signals:
  ctrl_random_taker          — TAKER control: random direction, immediate fill
  rsi_reversion_{maker,taker}
  ema_cross_{maker,taker}
  bollinger_reversion_{maker,taker}
  volume_breakout_{maker,taker}
  book_imbalance_{maker,taker}
  taker_flow_{maker,taker}
  oi_delta_{maker,taker}

All pre-registered in hypotheses.jsonl before deployment.
Parameters are fixed at registration; nothing is tuned after the fact.

TAKER fee: 10.0 bps (VIP0 standard) — see fill_simulator.TAKER_FEE_BPS.
"""

import asyncio
import logging
import random
import time
from decimal import Decimal
from typing import Dict, List, Optional

from ..recorder.state import MarketState
from .fill_simulator import FillSimulator

logger = logging.getLogger(__name__)

ORDER_NOTIONAL = 10.0     # USD per order, matches baseline_random
SIGNAL_INTERVAL_S = 15.0  # check cadence per symbol
COOLDOWN_S = 300.0        # 5 min per (strategy, symbol) — prevents correlated event runs

# Spot → futures symbol mapping for OI signal
_SPOT_TO_FUT: Dict[str, str] = {
    "ETHFDUSD": "ETHUSDT",
    "SOLFDUSD": "SOLUSDT",
    "XRPFDUSD": "XRPUSDT",
    "DOGEFDUSD": "DOGEUSDT",
    "LINKFDUSD": "LINKUSDT",
    "BNBFDUSD": "BNBUSDT",
}


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = sum(d for d in deltas[-period:] if d > 0)
    losses = sum(-d for d in deltas[-period:] if d < 0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _ema(closes: list, period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def _bollinger(closes: list, period: int = 20, k: float = 2.0) -> Optional[str]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    ma = sum(window) / period
    std = (sum((c - ma) ** 2 for c in window) / period) ** 0.5
    if std == 0:
        return None
    price = closes[-1]
    if price < ma - k * std:
        return "BUY"
    if price > ma + k * std:
        return "SELL"
    return None


# ── Base class ─────────────────────────────────────────────────────────────────

class LabStrategyBase:
    """
    Common infrastructure for lab signal strategies.

    Subclasses implement `compute_signal(symbol) -> Optional[str]`.
    Mode ("MAKER" or "TAKER") controls the submission path.
    """

    def __init__(
        self,
        name: str,
        mode: str,
        symbols: List[str],
        state: MarketState,
        sim: FillSimulator,
    ):
        self._name = name
        self._mode = mode  # "MAKER" or "TAKER"
        self._symbols = symbols
        self._state = state
        self._sim = sim
        self._active: Dict[str, Optional[int]] = {s: None for s in symbols}
        self._last_signal: Dict[str, float] = {s: 0.0 for s in symbols}

    @property
    def strategy_name(self) -> str:
        return self._name

    def compute_signal(self, symbol: str) -> Optional[str]:
        raise NotImplementedError

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._symbol_loop(sym, i * 2.5))
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
                # Block if MAKER order is still pending
                if self._active[symbol] is not None:
                    continue
                # Cooldown applies to both modes: prevents correlated event runs
                if time.monotonic() - self._last_signal[symbol] < COOLDOWN_S:
                    continue
                signal = self.compute_signal(symbol)
                if signal is None:
                    continue
                await self._place_order(symbol, signal)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"{self._name} [{symbol}] error: {e}")

    async def _place_order(self, symbol: str, side: str) -> None:
        bid, ask = self._state.get_bid_ask(symbol)
        if bid is None or ask is None:
            return
        info = self._state.exchange_info.get(symbol, {})
        step_size = info.get("step_size")
        if not step_size:
            return

        # MAKER: post at best bid (BUY) or best ask (SELL) — passive
        # TAKER: fill at best ask (BUY) or best bid (SELL) — crosses spread
        if self._mode == "MAKER":
            price = bid if side == "BUY" else ask
        else:
            price = ask if side == "BUY" else bid

        raw_qty = Decimal(str(ORDER_NOTIONAL)) / price
        step = Decimal(str(step_size))
        qty = (raw_qty // step) * step
        if qty <= 0:
            return

        snapshot = self._build_snapshot(symbol, side)

        if self._mode == "MAKER":
            trade_id = await self._sim.submit(
                strategy_name=self._name,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                feature_snapshot=snapshot,
                max_hold_s=900,
            )
            if trade_id is not None:
                self._active[symbol] = trade_id
                self._last_signal[symbol] = time.monotonic()
        else:
            await self._sim.submit_taker(
                strategy_name=self._name,
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                feature_snapshot=snapshot,
            )
            self._last_signal[symbol] = time.monotonic()

    def on_order_resolved(self, trade_id: int, filled: bool) -> None:
        if self._mode == "TAKER":
            return  # TAKER orders fill immediately — no pending tracking
        for sym, active_id in self._active.items():
            if active_id == trade_id:
                self._active[sym] = None
                break

    def _build_snapshot(self, symbol: str, side: str) -> dict:
        bid, ask = self._state.get_bid_ask(symbol)
        mid = self._state.get_mid(symbol)
        return {
            "strategy": self._name,
            "mode": self._mode,
            "side": side,
            "symbol": symbol,
            "best_bid": float(bid) if bid else None,
            "best_ask": float(ask) if ask else None,
            "mid": float(mid) if mid else None,
            "fdusd_peg": self._state.fdusd_peg,
        }

    # ── Shared indicator helpers ───────────────────────────────────────────────

    def _minute_bars(self, symbol: str, n: int) -> list:
        """Last n 1-minute close prices from mid_history."""
        history = self._state.mid_history.get(symbol)
        if not history or len(history) < 10:
            return []
        now_us = history[-1].ts_us
        bars = []
        for i in range(n, 0, -1):
            t_end = now_us - (i - 1) * 60_000_000
            t_start = t_end - 60_000_000
            close = None
            for s in reversed(history):
                if s.ts_us < t_start:
                    break
                if t_start <= s.ts_us < t_end:
                    close = float(s.mid)
                    break
            if close is not None:
                bars.append(close)
        return bars

    def _taker_vol_window(self, symbol: str, window_ms: int, buyer_maker: Optional[bool] = None) -> float:
        """Sum aggTrade qty in the last window_ms milliseconds."""
        trades = self._state.agg_trades.get(symbol, [])
        now_ms = time.time_ns() // 1_000_000
        total = 0.0
        for t in reversed(trades):
            if now_ms - t.ts_ms > window_ms:
                break  # deque is oldest-first; everything beyond is also out of window
            if buyer_maker is None or t.is_buyer_maker == buyer_maker:
                total += float(t.qty)
        return total


# ── Strategies ─────────────────────────────────────────────────────────────────

class CtrlRandomTaker(LabStrategyBase):
    """TAKER control: coin-flip entries paying the spread. Every signal beats this."""

    def compute_signal(self, symbol: str) -> Optional[str]:
        return random.choice(["BUY", "SELL"])


class RsiReversion(LabStrategyBase):
    """
    RSI(14) reversion: buy oversold (<30), sell overbought (>70).
    Uses 1-minute close bars from mid_history. Needs 25+ bars (~25 min warmup).
    Hypothesis: mean reversion in short-term oversold/overbought conditions.
    """

    RSI_BUY = 30
    RSI_SELL = 70
    N_BARS = 25

    def compute_signal(self, symbol: str) -> Optional[str]:
        bars = self._minute_bars(symbol, self.N_BARS)
        if len(bars) < self.N_BARS:
            return None
        rsi = _rsi(bars)
        if rsi is None:
            return None
        if rsi < self.RSI_BUY:
            return "BUY"
        if rsi > self.RSI_SELL:
            return "SELL"
        return None


class EmaCross(LabStrategyBase):
    """
    EMA(9) / EMA(21) crossover. Needs 25+ minute bars.
    BUY when fast crosses above slow; SELL when fast crosses below slow.
    Hypothesis: trend-following signal captures momentum.
    """

    FAST = 9
    SLOW = 21
    N_BARS = 25

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prev_fast: Dict[str, Optional[float]] = {s: None for s in self._symbols}
        self._prev_slow: Dict[str, Optional[float]] = {s: None for s in self._symbols}

    def compute_signal(self, symbol: str) -> Optional[str]:
        bars = self._minute_bars(symbol, self.N_BARS)
        if len(bars) < self.SLOW:
            return None
        fast = _ema(bars, self.FAST)
        slow = _ema(bars, self.SLOW)
        if fast is None or slow is None:
            return None
        prev_fast = self._prev_fast[symbol]
        prev_slow = self._prev_slow[symbol]
        self._prev_fast[symbol] = fast
        self._prev_slow[symbol] = slow
        if prev_fast is None or prev_slow is None:
            return None
        # Cross detection
        if prev_fast <= prev_slow and fast > slow:
            return "BUY"
        if prev_fast >= prev_slow and fast < slow:
            return "SELL"
        return None


class BollingerReversion(LabStrategyBase):
    """
    Bollinger Bands(20, 2σ): buy below lower band, sell above upper band.
    Needs 20+ minute bars.
    Hypothesis: price mean-reverts from statistical extremes.
    """

    N_BARS = 22  # 20 for the band + 2 extra

    def compute_signal(self, symbol: str) -> Optional[str]:
        bars = self._minute_bars(symbol, self.N_BARS)
        if len(bars) < 20:
            return None
        return _bollinger(bars, period=20, k=2.0)


class VolumeBreakout(LabStrategyBase):
    """
    Volume spike: 1-minute aggTrade volume > 3× 20-minute average.
    Direction: dominant taker side in the last 30 seconds.
    Hypothesis: volume spikes precede directional moves.
    """

    SPIKE_MULTIPLE = 3.0
    AVG_WINDOW_MS = 20 * 60 * 1_000   # 20 min
    VOL_WINDOW_MS = 60 * 1_000        # 1 min
    DIR_WINDOW_MS = 30 * 1_000        # 30 s

    def compute_signal(self, symbol: str) -> Optional[str]:
        vol_1m = self._taker_vol_window(symbol, self.VOL_WINDOW_MS)
        vol_20m = self._taker_vol_window(symbol, self.AVG_WINDOW_MS)
        if vol_20m <= 0:
            return None
        avg_per_min = vol_20m / 20.0
        if vol_1m < self.SPIKE_MULTIPLE * avg_per_min:
            return None
        buy_30s = self._taker_vol_window(symbol, self.DIR_WINDOW_MS, buyer_maker=False)
        sell_30s = self._taker_vol_window(symbol, self.DIR_WINDOW_MS, buyer_maker=True)
        if buy_30s + sell_30s == 0:
            return None
        return "BUY" if buy_30s > sell_30s else "SELL"


class BookImbalance(LabStrategyBase):
    """
    Best-level book imbalance from @bookTicker.
    imbalance = (bid_qty − ask_qty) / (bid_qty + ask_qty)
    Signal fires when |imbalance| > threshold (0.30).
    Hypothesis: best-level pressure predicts short-term price direction.
    Note: only single best-level quantities available without depth@100ms.
    """

    THRESHOLD = 0.30

    def compute_signal(self, symbol: str) -> Optional[str]:
        bt = self._state.book_tickers.get(symbol)
        if not bt:
            return None
        bid_qty = float(bt.get("B", 0) or 0)
        ask_qty = float(bt.get("A", 0) or 0)
        total = bid_qty + ask_qty
        if total <= 0:
            return None
        imbalance = (bid_qty - ask_qty) / total
        if imbalance > self.THRESHOLD:
            return "BUY"
        if imbalance < -self.THRESHOLD:
            return "SELL"
        return None


class TakerFlow(LabStrategyBase):
    """
    Taker flow imbalance over a 1-minute rolling window.
    flow = (taker_buy_vol − taker_sell_vol) / total_vol
    Signal fires when |flow| > threshold (0.30).
    Hypothesis: sustained order-flow imbalance predicts short-term direction.
    """

    THRESHOLD = 0.30
    WINDOW_MS = 60 * 1_000

    def compute_signal(self, symbol: str) -> Optional[str]:
        buy = self._taker_vol_window(symbol, self.WINDOW_MS, buyer_maker=False)
        sell = self._taker_vol_window(symbol, self.WINDOW_MS, buyer_maker=True)
        total = buy + sell
        if total <= 0:
            return None
        flow = (buy - sell) / total
        if flow > self.THRESHOLD:
            return "BUY"
        if flow < -self.THRESHOLD:
            return "SELL"
        return None


class OiDelta(LabStrategyBase):
    """
    Open Interest delta from futures REST (polled every 5 min).
    Signal fires when |ΔOIA| > 0.5% between the two most recent snapshots.
    Direction: follows recent (5-min) price move from mid_history.
    Hypothesis: OI spikes with price confirmation signal institutional positioning.
    Warmup: ≥2 OI snapshots (10+ minutes after startup).
    """

    THRESHOLD_PCT = 0.5

    def compute_signal(self, symbol: str) -> Optional[str]:
        fut_sym = _SPOT_TO_FUT.get(symbol)
        if fut_sym is None:
            return None
        snaps = self._state.oi_snapshots.get(fut_sym)
        if not snaps or len(snaps) < 2:
            return None

        ts_now, oi_now = snaps[-1]
        ts_prev, oi_prev = snaps[-2]
        if oi_prev <= 0:
            return None

        delta_pct = (oi_now - oi_prev) / oi_prev * 100.0
        if abs(delta_pct) < self.THRESHOLD_PCT:
            return None

        # Direction: follow the price move over the same interval
        history = self._state.mid_history.get(symbol)
        if not history or len(history) < 2:
            return None
        now_us = history[-1].ts_us
        lookback_us = 5 * 60 * 1_000_000  # 5 min
        past_price = None
        for s in reversed(history):
            if now_us - s.ts_us >= lookback_us:
                past_price = float(s.mid)
                break
        if past_price is None:
            return None
        price_move = float(history[-1].mid) - past_price
        if price_move > 0:
            return "BUY"
        if price_move < 0:
            return "SELL"
        return None


# ── Factory ────────────────────────────────────────────────────────────────────

def make_lab_strategies(
    symbols: List[str],
    state: MarketState,
    sim: FillSimulator,
) -> List[LabStrategyBase]:
    """
    Instantiate all 15 lab strategies.
    Call this AFTER hypotheses.jsonl has been written (pre-registration rule).
    """
    strategies: List[LabStrategyBase] = []

    def _add(cls, base_name: str, modes=("MAKER", "TAKER")):
        for mode in modes:
            strategies.append(
                cls(f"{base_name}_{mode.lower()}", mode, symbols, state, sim)
            )

    strategies.append(CtrlRandomTaker("ctrl_random_taker", "TAKER", symbols, state, sim))
    _add(RsiReversion, "rsi_reversion")
    _add(EmaCross, "ema_cross")
    _add(BollingerReversion, "bollinger_reversion")
    _add(VolumeBreakout, "volume_breakout")
    _add(BookImbalance, "book_imbalance")
    _add(TakerFlow, "taker_flow")
    _add(OiDelta, "oi_delta")

    return strategies
