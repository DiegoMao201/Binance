"""
Recorder + Shadow motor entry point.

Usage:
    python -m services.recorder.main

Env vars required:
    DATABASE_URL          postgresql://binance_bot:...@10.0.1.8:5432/optiferre_binance
    BINANCE_PROXY_URL     http://user:pass@host:port
    RECORDER_DATA_DIR     /data/binance-recorder   (default)

DRY_RUN has no effect here — recorder never sends orders.
"""

import asyncio
import logging
import os
import signal
import sys

import asyncpg

from .config import RecorderConfig
from .health import HealthMonitor
from .rest_poller import RestPoller
from .state import MarketState
from .streams import StreamHandler
from ..shadow.main import ShadowMotor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def run() -> None:
    cfg = RecorderConfig.from_env()
    logger.info("recorder starting — data_dir=%s", cfg.data_dir)
    logger.info("spot_url: %s…", cfg.spot_ws_url[:80])
    logger.info("futures_url: %s…", cfg.futures_ws_url[:80])

    os.makedirs(cfg.raw_dir, exist_ok=True)
    os.makedirs(cfg.parquet_dir, exist_ok=True)

    db: asyncpg.Pool = await asyncpg.create_pool(cfg.database_url, min_size=2, max_size=8)

    state = MarketState(cfg.spot_symbols, cfg.futures_symbols)
    health = HealthMonitor(db, silence_threshold_s=cfg.ws_silence_threshold_s)

    poller = RestPoller(cfg, db, state)
    await poller.start()

    streams = StreamHandler(cfg, db, state, health)
    shadow = ShadowMotor(db, state, cfg.spot_symbols)

    # Wire aggTrade events from recorder → shadow motor
    streams.set_agg_trade_hook(shadow.on_agg_trade)

    # Markout backfill: re-queue fills that were open when process last died
    await shadow._tracker.backfill_on_startup()

    async def _initial_depth_sync():
        # Wait for WS to connect and start buffering depth updates, THEN apply
        # snapshots so buffered updates can anchor correctly (Binance sync protocol).
        await asyncio.sleep(3)
        logger.info("fetching initial depth snapshots…")
        await poller.apply_depth_snapshots()

    all_tasks = [
        *streams.tasks(),
        *shadow.tasks(),
        asyncio.create_task(_initial_depth_sync(), name="initial-depth-sync"),
        asyncio.create_task(health.run(), name="health-flush"),
        asyncio.create_task(poller.run_oi_poll(), name="rest-oi-poll"),
        asyncio.create_task(poller.run_depth_resync(), name="rest-depth-resync"),
        asyncio.create_task(poller.run_exchange_info(), name="rest-exchange-info"),
        asyncio.create_task(poller.run_mid_snapshots(), name="rest-mid-snapshots"),
    ]
    if not cfg.futures_ws_enabled:
        all_tasks.append(
            asyncio.create_task(poller.run_mark_prices_rest(), name="rest-mark-prices")
        )

    stop_event = asyncio.Event()

    def _on_signal(*_):
        logger.info("shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT, _on_signal)

    logger.info("recorder + shadow motor running (%d tasks)", len(all_tasks))

    try:
        await stop_event.wait()
    finally:
        logger.info("stopping…")
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        streams.close()
        await poller.close()
        await db.close()
        logger.info("shutdown complete")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
