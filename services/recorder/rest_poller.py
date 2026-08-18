"""
REST poller: OI/L/S metrics (every 5 min), exchangeInfo (daily),
depth snapshots for book resync.
Uses aiohttp with proxy support — no ccxt.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp
import asyncpg

from .config import RecorderConfig
from .state import MarketState

logger = logging.getLogger(__name__)

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"


class RestPoller:
    def __init__(self, cfg: RecorderConfig, db: asyncpg.Pool, state: MarketState):
        self._cfg = cfg
        self._db = db
        self._state = state
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(ssl=True, limit=20)
        self._session = aiohttp.ClientSession(connector=connector)

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    @property
    def _proxy(self) -> Optional[str]:
        return self._cfg.proxy_url or None

    async def _get(self, base: str, path: str, params: dict = None, timeout: int = 10) -> dict:
        url = f"{base}{path}"
        async with self._session.get(
            url,
            params=params,
            proxy=self._proxy,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ── Depth snapshots ───────────────────────────────────────────────────────

    async def fetch_depth_snapshot(self, symbol: str) -> dict:
        return await self._get(
            SPOT_BASE, "/api/v3/depth", {"symbol": symbol, "limit": 1000}
        )

    async def apply_depth_snapshots(self) -> None:
        """Fetch and apply snapshots for all symbols that need resync."""
        for sym in self._cfg.spot_symbols:
            if not self._state.resync_needed.get(sym, True):
                continue
            try:
                snap = await self.fetch_depth_snapshot(sym)
                book = self._state.books[sym]
                book.apply_snapshot(snap)
                if book.is_synced:
                    self._state.resync_needed[sym] = False
                    logger.info(f"book synced: {sym} lastUpdateId={snap['lastUpdateId']}")
            except Exception as e:
                logger.warning(f"depth snapshot failed [{sym}]: {e}")
            await asyncio.sleep(0.1)  # spread requests

    async def run_depth_resync(self) -> None:
        """Periodic full resync loop."""
        while True:
            await asyncio.sleep(self._cfg.depth_snapshot_interval_s)
            # mark all symbols for resync
            for sym in self._cfg.spot_symbols:
                self._state.resync_needed[sym] = True
            await self.apply_depth_snapshots()

    # ── OI / L/S metrics ─────────────────────────────────────────────────────

    async def _fetch_oi_for_symbol(self, futures_sym: str) -> dict:
        results = await asyncio.gather(
            self._get(FUTURES_BASE, "/fapi/v1/openInterest", {"symbol": futures_sym}),
            self._get(FUTURES_BASE, "/futures/data/topLongShortPositionRatio",
                      {"symbol": futures_sym, "period": "5m", "limit": 1}),
            self._get(FUTURES_BASE, "/futures/data/topLongShortAccountRatio",
                      {"symbol": futures_sym, "period": "5m", "limit": 1}),
            self._get(FUTURES_BASE, "/futures/data/globalLongShortAccountRatio",
                      {"symbol": futures_sym, "period": "5m", "limit": 1}),
            self._get(FUTURES_BASE, "/futures/data/takerlongshortRatio",
                      {"symbol": futures_sym, "period": "5m", "limit": 1}),
            return_exceptions=True,
        )
        keys = ("oi", "top_pos", "top_acc", "global_ls", "taker")
        return {k: (v if not isinstance(v, Exception) else None) for k, v in zip(keys, results)}

    async def _upsert_oi(self, ts: datetime, symbol: str, data: dict) -> None:
        def _first(d):
            return d[0] if isinstance(d, list) and d else (d or {})

        oi = data.get("oi") or {}
        top_pos = _first(data.get("top_pos"))
        top_acc = _first(data.get("top_acc"))
        global_ls = _first(data.get("global_ls"))
        taker = _first(data.get("taker"))

        def _f(d, k):
            v = d.get(k)
            return float(v) if v is not None else None

        await self._db.execute(
            """
            INSERT INTO binance_oi_metrics
                (ts, symbol, open_interest, open_interest_value,
                 long_short_ratio_top_pos, long_short_ratio_top_acc,
                 long_short_ratio_global, taker_buy_vol, taker_sell_vol)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (ts, symbol) DO NOTHING
            """,
            ts, symbol,
            _f(oi, "openInterest"),
            _f(oi, "openInterestValue"),
            _f(top_pos, "longShortRatio"),
            _f(top_acc, "longShortRatio"),
            _f(global_ls, "longShortRatio"),
            _f(taker, "buyVol"),
            _f(taker, "sellVol"),
        )

    async def run_oi_poll(self) -> None:
        # 30-day backfill on first run, then periodic
        await self._backfill_oi_history()
        while True:
            try:
                await asyncio.sleep(self._cfg.rest_poll_interval_s)
                ts = datetime.now(tz=timezone.utc)
                for sym in self._cfg.futures_symbols:
                    if sym == "BTCUSDT":
                        continue
                    try:
                        data = await self._fetch_oi_for_symbol(sym)
                        await self._upsert_oi(ts, sym, data)
                        oi_raw = data.get("oi") or {}
                        oi_val = float(oi_raw.get("openInterestValue", 0) or 0)
                        if oi_val > 0 and sym in self._state.oi_snapshots:
                            self._state.oi_snapshots[sym].append(
                                (int(ts.timestamp() * 1_000_000), oi_val)
                            )
                    except Exception as e:
                        logger.warning(f"OI poll [{sym}]: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"OI poll loop: {e}")

    async def _backfill_oi_history(self) -> None:
        """Fetch 30 days of 5m OI history if DB is empty."""
        row = await self._db.fetchrow(
            "SELECT MAX(ts) FROM binance_oi_metrics"
        )
        if row and row[0] and (datetime.now(tz=timezone.utc) - row[0]).days < 1:
            return  # already populated

        logger.info("backfilling 30d OI history…")
        end_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        start_ts = end_ts - 30 * 86_400 * 1000

        for sym in self._cfg.futures_symbols:
            if sym == "BTCUSDT":
                continue
            cursor = start_ts
            while cursor < end_ts:
                try:
                    data = await self._get(
                        FUTURES_BASE,
                        "/futures/data/openInterestHist",
                        {"symbol": sym, "period": "5m",
                         "startTime": cursor, "limit": 500},
                    )
                    for row in data:
                        ts = datetime.fromtimestamp(
                            row["timestamp"] / 1000, tz=timezone.utc
                        )
                        await self._db.execute(
                            """
                            INSERT INTO binance_oi_metrics (ts, symbol, open_interest, open_interest_value)
                            VALUES ($1,$2,$3,$4) ON CONFLICT (ts, symbol) DO NOTHING
                            """,
                            ts, sym,
                            float(row.get("sumOpenInterest", 0)),
                            float(row.get("sumOpenInterestValue", 0)),
                        )
                    if not data:
                        break
                    last_ts = data[-1]["timestamp"]
                    cursor = last_ts + 300_000  # +5 min
                    await asyncio.sleep(0.2)
                except Exception as e:
                    logger.warning(f"OI backfill [{sym}]: {e}")
                    break

        logger.info("OI backfill complete")

    # ── Exchange info ─────────────────────────────────────────────────────────

    async def run_exchange_info(self) -> None:
        while True:
            try:
                await self._refresh_exchange_info()
            except Exception as e:
                logger.error(f"exchange_info refresh: {e}")
            await asyncio.sleep(self._cfg.exchange_info_interval_s)

    async def run_mid_snapshots(self) -> None:
        """
        Persist spot mid prices at ~5s resolution to binance_mid_snapshots.
        Used exclusively for markout backfill after a process restart.
        Prunes rows older than 2 hours to keep the table small.
        """
        while True:
            await asyncio.sleep(5)
            try:
                from datetime import datetime, timezone
                ts = datetime.now(tz=timezone.utc)
                for sym in self._cfg.spot_symbols:
                    mid = self._state.get_mid(sym)
                    bid, ask = self._state.get_bid_ask(sym)
                    if mid is None or bid is None:
                        continue
                    await self._db.execute(
                        """
                        INSERT INTO binance_mid_snapshots (ts, symbol, mid, bid, ask)
                        VALUES ($1,$2,$3,$4,$5) ON CONFLICT (ts, symbol) DO NOTHING
                        """,
                        ts, sym, float(mid), float(bid), float(ask),
                    )
                # prune rows older than 2 hours every ~5 min
                if int(ts.timestamp()) % 300 < 5:
                    await self._db.execute(
                        "DELETE FROM binance_mid_snapshots WHERE ts < NOW() - INTERVAL '2 hours'"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"mid_snapshot flush: {e}")

    async def run_mark_prices_rest(self) -> None:
        """
        REST fallback for futures mark prices when fstream.binance.com is geo-restricted.
        Polls /fapi/v1/premiumIndex every 3s — returns all symbols in one request.
        Feeds state.mark_prices and inserts into binance_mark_price.
        """
        logger.info("mark prices: starting REST fallback (futures WS geo-restricted)")
        while True:
            await asyncio.sleep(3)
            try:
                rows = await self._get(FUTURES_BASE, "/fapi/v1/premiumIndex")
                if not isinstance(rows, list):
                    continue
                ts_now = datetime.now(tz=timezone.utc)
                batch = []
                for row in rows:
                    sym = row.get("symbol", "")
                    if sym not in self._cfg.futures_symbols:
                        continue
                    mark = float(row.get("markPrice", 0))
                    idx = float(row.get("indexPrice", 0))
                    funding = float(row.get("lastFundingRate", 0))
                    next_funding = int(row.get("nextFundingTime", 0))
                    self._state.mark_prices[sym] = mark
                    batch.append((
                        ts_now,
                        int(ts_now.timestamp() * 1_000_000),
                        sym, mark, idx, funding, next_funding,
                    ))
                if batch:
                    await self._db.executemany(
                        """
                        INSERT INTO binance_mark_price
                            (ts, recv_ts_us, symbol, mark_price, index_price,
                             funding_rate, next_funding_time)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (ts, symbol) DO NOTHING
                        """,
                        batch,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"mark price REST poll: {e}")

    async def _refresh_exchange_info(self) -> None:
        ts = datetime.now(tz=timezone.utc)
        try:
            data = await self._get(
                SPOT_BASE,
                "/api/v3/exchangeInfo",
                # separators=(',', ':') removes spaces from JSON → avoids URL-encoding issues
                {"symbols": json.dumps(self._cfg.spot_symbols, separators=(',', ':'))},
            )
        except Exception as e:
            logger.error(f"exchangeInfo fetch failed: {e}")
            return

        for sym_info in data.get("symbols", []):
            symbol = sym_info.get("symbol")
            if symbol not in self._cfg.spot_symbols:
                continue

            tick_size = step_size = min_notional = None
            for f in sym_info.get("filters", []):
                ft = f["filterType"]
                if ft == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
                elif ft == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional") or f.get("notional") or 5.0)

            amend_allowed = sym_info.get("amendAllowed", False)
            peg_allowed = sym_info.get("pegInstructionsAllowed", False)

            self._state.exchange_info[symbol] = {
                "tick_size": tick_size,
                "step_size": step_size,
                "min_notional": min_notional,
                "amend_allowed": amend_allowed,
                "peg_allowed": peg_allowed,
            }

            await self._db.execute(
                """
                INSERT INTO binance_exchange_info_cache
                    (ts, symbol, tick_size, step_size, min_notional,
                     amend_allowed, peg_instructions_allowed, raw)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (ts, symbol) DO NOTHING
                """,
                ts, symbol,
                tick_size, step_size, min_notional or 5.0,
                amend_allowed, peg_allowed,
                json.dumps(sym_info),
            )

        logger.info(f"exchangeInfo refreshed ({len(self._cfg.spot_symbols)} symbols)")
