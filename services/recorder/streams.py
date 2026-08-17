"""
WebSocket stream handlers.
Two connections: spot (stream.binance.com) and futures (fstream.binance.com).
Both use aiohttp for proxy-aware WSS tunneling.

Dead-WS detector: if no message in ws_silence_threshold_s, close and reconnect.
(Hummingbot exchange_py_base.py:1129 pattern — 6-line check adapted for asyncio.)
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import aiohttp
import asyncpg

from .config import RecorderConfig
from .health import HealthMonitor
from .state import AggTradeSample, MarketState
from .writer import RawGzipWriter, make_parquet_writers

logger = logging.getLogger(__name__)

STREAM_SPOT = "spot"
STREAM_FUTURES = "futures"


class StreamHandler:
    """
    Manages the two WebSocket connections and routes incoming messages.
    Call run() to start; cancel the returned tasks to stop.
    """

    def __init__(
        self,
        cfg: RecorderConfig,
        db: asyncpg.Pool,
        state: MarketState,
        health: HealthMonitor,
    ):
        # Optional: shadow motor registers here to receive live aggTrade events.
        # Using a callable hook avoids circular imports between recorder ↔ shadow.
        self._agg_trade_hook = None  # set via set_agg_trade_hook()
        self._cfg = cfg
        self._db = db
        self._state = state
        self._health = health

        self._raw = RawGzipWriter(cfg.raw_dir)
        self._pq = make_parquet_writers(
            cfg.parquet_dir, cfg.parquet_flush_rows, cfg.parquet_flush_seconds
        )

        # Liquidations batch buffer (flushed to DB)
        self._liq_batch = []
        self._mark_batch = []

        health.register(STREAM_SPOT)
        if cfg.futures_ws_enabled:
            health.register(STREAM_FUTURES)

    @property
    def _proxy(self) -> Optional[str]:
        return self._cfg.proxy_url or None

    def set_agg_trade_hook(self, hook) -> None:
        """Register a callable(symbol, AggTradeSample) called on every aggTrade."""
        self._agg_trade_hook = hook

    # ── Public ────────────────────────────────────────────────────────────────

    def tasks(self) -> list:
        tasks = [
            asyncio.create_task(self._spot_loop(), name="ws-spot"),
            asyncio.create_task(self._db_flush_loop(), name="db-flush"),
        ]
        if self._cfg.futures_ws_enabled:
            tasks.append(asyncio.create_task(self._futures_loop(), name="ws-futures"))
        else:
            logger.warning("futures WS disabled — mark prices via REST fallback")
        return tasks

    # ── Connection loops ─────────────────────────────────────────────────────

    async def _spot_loop(self) -> None:
        silence = self._cfg.ws_silence_threshold_s
        while True:
            self._health.mark_disconnected(STREAM_SPOT)
            try:
                connector = aiohttp.TCPConnector(ssl=True)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.ws_connect(
                        self._cfg.spot_ws_url,
                        proxy=self._proxy,
                        heartbeat=20,
                        timeout=aiohttp.ClientTimeout(total=30, connect=15),
                    ) as ws:
                        self._health.mark_connected(STREAM_SPOT)
                        last_recv = time.monotonic()
                        async for msg in ws:
                            last_recv = time.monotonic()
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                ts_us = time.time_ns() // 1000
                                raw = msg.data.encode()
                                self._raw.write(STREAM_SPOT, ts_us, raw)
                                self._health.touch(STREAM_SPOT)
                                await self._handle_spot(ts_us, msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                            # dead-WS detector
                            if time.monotonic() - last_recv > silence:
                                logger.warning("spot WS silent, reconnecting")
                                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"spot WS error: {e}")
            await asyncio.sleep(1)

    async def _futures_loop(self) -> None:
        silence = self._cfg.ws_silence_threshold_s
        while True:
            self._health.mark_disconnected(STREAM_FUTURES)
            try:
                connector = aiohttp.TCPConnector(ssl=True)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.ws_connect(
                        self._cfg.futures_ws_url,
                        proxy=self._proxy,
                        heartbeat=20,
                        timeout=aiohttp.ClientTimeout(total=30, connect=15),
                    ) as ws:
                        self._health.mark_connected(STREAM_FUTURES)
                        last_recv = time.monotonic()
                        async for msg in ws:
                            last_recv = time.monotonic()
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                ts_us = time.time_ns() // 1000
                                raw = msg.data.encode()
                                self._raw.write(STREAM_FUTURES, ts_us, raw)
                                self._health.touch(STREAM_FUTURES)
                                await self._handle_futures(ts_us, msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                            if time.monotonic() - last_recv > silence:
                                logger.warning("futures WS silent, reconnecting")
                                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"futures WS error: {e}")
            await asyncio.sleep(1)

    # ── Message routing ──────────────────────────────────────────────────────

    async def _handle_spot(self, ts_us: int, raw: str) -> None:
        try:
            outer = json.loads(raw)
        except json.JSONDecodeError:
            return

        stream_name: str = outer.get("stream", "")
        data: dict = outer.get("data", outer)

        if stream_name.endswith("@bookTicker"):
            self._on_book_ticker(ts_us, stream_name, data)
        elif stream_name.endswith("@aggTrade"):
            self._on_agg_trade(ts_us, stream_name, data)
        elif stream_name.endswith("@depth@100ms"):
            await self._on_depth(ts_us, stream_name, data)

    async def _handle_futures(self, ts_us: int, raw: str) -> None:
        try:
            outer = json.loads(raw)
        except json.JSONDecodeError:
            return

        stream_name: str = outer.get("stream", "")
        data: dict = outer.get("data", outer)
        event_type = data.get("e", "")

        if event_type == "forceOrder" or stream_name == "!forceOrder@arr":
            self._on_liquidation(ts_us, data)
        elif event_type == "markPriceUpdate" or "@markPrice" in stream_name:
            self._on_mark_price(ts_us, data)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _on_book_ticker(self, ts_us: int, stream_name: str, data: dict) -> None:
        symbol = data.get("s", "")
        self._state.book_tickers[symbol] = data

        if symbol == "FDUSDUSDT":
            bid = float(data.get("b", 1.0))
            ask = float(data.get("a", 1.0))
            self._state.fdusd_peg = (bid + ask) / 2
            if self._state.fdusd_peg < 0.9950:
                logger.warning(
                    f"FDUSD PEG ALERT: {self._state.fdusd_peg:.4f} < 0.9950"
                )

        if symbol in self._state.books:
            self._state.record_mid(symbol, ts_us)

        self._pq["bookticker"].append({
            "recv_ts_us":     ts_us,
            "symbol":         symbol,
            "best_bid_price": float(data.get("b", 0)),
            "best_bid_qty":   float(data.get("B", 0)),
            "best_ask_price": float(data.get("a", 0)),
            "best_ask_qty":   float(data.get("A", 0)),
        })

    def _on_agg_trade(self, ts_us: int, stream_name: str, data: dict) -> None:
        symbol = data.get("s", "")
        price = Decimal(str(data.get("p", 0)))
        qty = Decimal(str(data.get("q", 0)))
        is_buyer_maker = bool(data.get("m", False))
        ts_ms = int(data.get("T", 0))

        if symbol in self._state.agg_trades:
            sample = AggTradeSample(ts_ms, price, qty, is_buyer_maker)
            self._state.agg_trades[symbol].append(sample)
            if self._agg_trade_hook:
                self._agg_trade_hook(symbol, sample)

        self._pq["aggtrade"].append({
            "recv_ts_us":     ts_us,
            "ts_trade_ms":    ts_ms,
            "symbol":         symbol,
            "agg_trade_id":   int(data.get("a", 0)),
            "price":          float(price),
            "qty":            float(qty),
            "first_trade_id": int(data.get("f", 0)),
            "last_trade_id":  int(data.get("l", 0)),
            "is_buyer_maker": is_buyer_maker,
        })

    async def _on_depth(self, ts_us: int, stream_name: str, data: dict) -> None:
        symbol = data.get("s", "")
        if symbol not in self._state.books:
            return

        book = self._state.books[symbol]
        U = data.get("U", 0)
        u = data.get("u", 0)

        if self._state.resync_needed.get(symbol, True):
            book.buffer(data)
        else:
            updated = book.update(data)
            if not updated and not book.is_synced:
                # Gap detected — flag for resync; log to DB
                self._state.resync_needed[symbol] = True
                self._health.mark_gap(STREAM_SPOT)
                logger.warning(f"book gap [{symbol}] last={book._last_update_id} got U={U}")
                try:
                    await self._db.execute(
                        """
                        INSERT INTO recorder_book_gaps (symbol, expected_u, got_U, action)
                        VALUES ($1,$2,$3,'resync')
                        """,
                        symbol,
                        book._last_update_id + 1,
                        U,
                    )
                except Exception:
                    pass

        import json as _json
        self._pq["depth"].append({
            "recv_ts_us":      ts_us,
            "ts_event_ms":     int(data.get("E", 0)),
            "symbol":          symbol,
            "first_update_id": U,
            "last_update_id":  u,
            "bids_json":       _json.dumps(data.get("b", [])),
            "asks_json":       _json.dumps(data.get("a", [])),
        })

    def _on_liquidation(self, ts_us: int, data: dict) -> None:
        order = data.get("o", data)
        ts = datetime.fromtimestamp(
            int(order.get("T", ts_us // 1000)) / 1000, tz=timezone.utc
        )
        self._liq_batch.append({
            "recv_ts_us":    ts_us,
            "ts":            ts,
            "symbol":        order.get("s", ""),
            "side":          order.get("S", ""),
            "order_type":    order.get("o", ""),
            "time_in_force": order.get("f", ""),
            "original_qty":  float(order.get("q", 0)),
            "price":         float(order.get("p", 0)),
            "avg_price":     float(order.get("ap", 0)),
            "order_status":  order.get("X", ""),
            "last_filled_qty": float(order.get("l", 0)),
            "acc_filled_qty":  float(order.get("z", 0)),
            "trade_time_ms": int(order.get("T", 0)),
        })

    def _on_mark_price(self, ts_us: int, data: dict) -> None:
        symbol = data.get("s", "")
        mark = float(data.get("p", 0))
        self._state.mark_prices[symbol] = mark

        self._mark_batch.append({
            "recv_ts_us":      ts_us,
            "ts_event_ms":     int(data.get("E", 0)),
            "symbol":          symbol,
            "mark_price":      mark,
            "index_price":     float(data.get("i", 0)),
            "funding_rate":    float(data.get("r", 0)),
            "next_funding_ms": int(data.get("T", 0)),
        })
        self._pq["markprice"].append(self._mark_batch[-1])

    # ── DB flush loop ─────────────────────────────────────────────────────────

    async def _db_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self._flush_liquidations()
                await self._flush_mark_prices()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"db flush loop: {e}")

    async def _flush_liquidations(self) -> None:
        if not self._liq_batch:
            return
        batch, self._liq_batch = self._liq_batch, []
        rows = [
            (
                r["recv_ts_us"], r["ts"], r["symbol"], r["side"],
                r["order_type"], r["time_in_force"],
                r["original_qty"], r["price"], r["avg_price"],
                r["order_status"], r["last_filled_qty"], r["acc_filled_qty"],
                r["trade_time_ms"],
            )
            for r in batch
        ]
        await self._db.executemany(
            """
            INSERT INTO binance_liquidations
                (recv_ts_us, ts, symbol, side, order_type, time_in_force,
                 original_qty, price, avg_price, order_status,
                 last_filled_qty, acc_filled_qty, trade_time_ms)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            rows,
        )

    async def _flush_mark_prices(self) -> None:
        if not self._mark_batch:
            return
        batch, self._mark_batch = self._mark_batch, []
        rows = [
            (
                datetime.fromtimestamp(r["ts_event_ms"] / 1000, tz=timezone.utc),
                r["recv_ts_us"],
                r["symbol"],
                r["mark_price"],
                r["index_price"],
                r["funding_rate"],
                r["next_funding_ms"],
            )
            for r in batch
        ]
        await self._db.executemany(
            """
            INSERT INTO binance_mark_price
                (ts, recv_ts_us, symbol, mark_price, index_price, funding_rate, next_funding_time)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (ts, symbol) DO NOTHING
            """,
            rows,
        )

    def close(self) -> None:
        self._raw.close()
        for pw in self._pq.values():
            pw.close()
