"""
Stream health tracker.
Persists counters to recorder_health table every 30 s.
Exposes is_stale() for the dead-WS detector (Hummingbot pattern).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class StreamHealth:
    stream_name: str
    last_msg_at: float = 0.0   # monotonic clock
    msg_count: int = 0         # incremental since last DB flush
    gap_count: int = 0
    status: str = "starting"


class HealthMonitor:
    def __init__(self, db: asyncpg.Pool, silence_threshold_s: float = 10.0):
        self._db = db
        self._silence = silence_threshold_s
        self._streams: Dict[str, StreamHealth] = {}

    def register(self, name: str) -> StreamHealth:
        h = StreamHealth(stream_name=name)
        self._streams[name] = h
        return h

    def touch(self, name: str) -> None:
        h = self._streams.get(name)
        if h:
            h.last_msg_at = time.monotonic()
            h.msg_count += 1

    def mark_gap(self, name: str) -> None:
        h = self._streams.get(name)
        if h:
            h.gap_count += 1

    def mark_connected(self, name: str) -> None:
        h = self._streams.get(name)
        if h:
            h.status = "connected"
            h.last_msg_at = time.monotonic()

    def mark_disconnected(self, name: str) -> None:
        h = self._streams.get(name)
        if h:
            h.status = "reconnecting"

    def is_stale(self, name: str) -> bool:
        h = self._streams.get(name)
        if h is None or h.last_msg_at == 0:
            return True
        return time.monotonic() - h.last_msg_at > self._silence

    def age_seconds(self, name: str) -> Optional[float]:
        h = self._streams.get(name)
        if h is None or h.last_msg_at == 0:
            return None
        return time.monotonic() - h.last_msg_at

    async def flush_to_db(self) -> None:
        ts = datetime.now(tz=timezone.utc)
        for h in self._streams.values():
            status = "stale" if self.is_stale(h.stream_name) else h.status
            try:
                await self._db.execute(
                    """
                    INSERT INTO recorder_health
                        (stream_name, last_msg_at, msg_count, gap_count, status, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (stream_name) DO UPDATE SET
                        last_msg_at  = EXCLUDED.last_msg_at,
                        msg_count    = recorder_health.msg_count + EXCLUDED.msg_count,
                        gap_count    = recorder_health.gap_count + EXCLUDED.gap_count,
                        status       = EXCLUDED.status,
                        updated_at   = EXCLUDED.updated_at
                    """,
                    h.stream_name,
                    ts if h.last_msg_at > 0 else None,
                    h.msg_count,
                    h.gap_count,
                    status,
                    ts,
                )
                h.msg_count = 0
                h.gap_count = 0
            except Exception as e:
                logger.warning(f"health flush [{h.stream_name}]: {e}")

    async def run(self, interval_s: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self.flush_to_db()
