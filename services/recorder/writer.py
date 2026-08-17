"""
Dual-write layer: raw gzip (byte-exact JSON with local μs timestamp)
and Parquet ZSTD via pyarrow (no pandas in hot path).

Raw format (one line per WS message):
    {timestamp_local_microseconds} {json_as_received}\n

The local timestamp is the ONLY source of real network latency from
the Binance gateway to our server — do not drop it.
"""

import gzip
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# ── Parquet schemas (no pandas) ───────────────────────────────────────────────

SCHEMA_AGGTRADE = pa.schema([
    pa.field("recv_ts_us",      pa.int64()),
    pa.field("ts_trade_ms",     pa.int64()),
    pa.field("symbol",          pa.string()),
    pa.field("agg_trade_id",    pa.int64()),
    pa.field("price",           pa.float64()),
    pa.field("qty",             pa.float64()),
    pa.field("first_trade_id",  pa.int64()),
    pa.field("last_trade_id",   pa.int64()),
    pa.field("is_buyer_maker",  pa.bool_()),
])

SCHEMA_DEPTH = pa.schema([
    pa.field("recv_ts_us",      pa.int64()),
    pa.field("ts_event_ms",     pa.int64()),
    pa.field("symbol",          pa.string()),
    pa.field("first_update_id", pa.int64()),
    pa.field("last_update_id",  pa.int64()),
    pa.field("bids_json",       pa.string()),
    pa.field("asks_json",       pa.string()),
])

SCHEMA_BOOKTICKER = pa.schema([
    pa.field("recv_ts_us",      pa.int64()),
    pa.field("symbol",          pa.string()),
    pa.field("best_bid_price",  pa.float64()),
    pa.field("best_bid_qty",    pa.float64()),
    pa.field("best_ask_price",  pa.float64()),
    pa.field("best_ask_qty",    pa.float64()),
])

SCHEMA_MARKPRICE = pa.schema([
    pa.field("recv_ts_us",      pa.int64()),
    pa.field("ts_event_ms",     pa.int64()),
    pa.field("symbol",          pa.string()),
    pa.field("mark_price",      pa.float64()),
    pa.field("index_price",     pa.float64()),
    pa.field("funding_rate",    pa.float64()),
    pa.field("next_funding_ms", pa.int64()),
])

SCHEMAS: Dict[str, pa.Schema] = {
    "aggtrade":   SCHEMA_AGGTRADE,
    "depth":      SCHEMA_DEPTH,
    "bookticker": SCHEMA_BOOKTICKER,
    "markprice":  SCHEMA_MARKPRICE,
}


# ── Raw gzip writer ───────────────────────────────────────────────────────────

class RawGzipWriter:
    """
    Appends raw WS messages to daily gzip files.
    One file per (channel, date): raw/spot_2026-08-14.gz
    """

    def __init__(self, base_dir: str):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._handles: Dict[str, gzip.GzipFile] = {}
        self._dates: Dict[str, str] = {}

    def write(self, channel: str, ts_us: int, raw_json: bytes) -> None:
        date_str = datetime.fromtimestamp(
            ts_us / 1_000_000, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        if self._dates.get(channel) != date_str:
            self._rotate(channel, date_str)

        fh = self._handles[channel]
        fh.write(f"{ts_us} ".encode() + raw_json + b"\n")

    def _rotate(self, channel: str, date_str: str) -> None:
        old_fh = self._handles.pop(channel, None)
        if old_fh:
            try:
                old_fh.close()
            except Exception:
                pass

        path = self._base / f"{channel}_{date_str}.gz"
        self._handles[channel] = gzip.open(str(path), "ab")
        self._dates[channel] = date_str

    def flush(self, channel: Optional[str] = None) -> None:
        targets = [channel] if channel else list(self._handles)
        for ch in targets:
            fh = self._handles.get(ch)
            if fh:
                try:
                    fh.flush()
                except Exception:
                    pass

    def close(self) -> None:
        for fh in self._handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._handles.clear()


# ── Parquet writer ────────────────────────────────────────────────────────────

class ParquetWriter:
    """
    Buffers rows and flushes to Parquet (ZSTD) when a row or time threshold
    is crossed. Partitioned by stream_type / date / part-NNNNN.parquet.
    Never uses pandas.
    """

    def __init__(
        self,
        base_dir: str,
        stream_type: str,
        schema: pa.Schema,
        flush_rows: int = 10_000,
        flush_seconds: int = 60,
    ):
        self._base = Path(base_dir) / stream_type
        self._stream_type = stream_type
        self._schema = schema
        self._flush_rows = flush_rows
        self._flush_seconds = flush_seconds
        self._buffer: List[Dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._parts: Dict[str, int] = {}

    def append(self, row: dict) -> None:
        self._buffer.append(row)
        elapsed = time.monotonic() - self._last_flush
        if len(self._buffer) >= self._flush_rows or elapsed >= self._flush_seconds:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        part = self._parts.get(date_str, 0)
        self._parts[date_str] = part + 1

        out_dir = self._base / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"part-{part:05d}.parquet"

        # Build record batch without pandas: collect per-column lists
        columns: Dict[str, list] = {name: [] for name in self._schema.names}
        for row in self._buffer:
            for name in self._schema.names:
                columns[name].append(row.get(name))

        try:
            table = pa.table(columns, schema=self._schema)
            pq.write_table(table, str(path), compression="zstd")
        except Exception as e:
            logger.error(f"parquet flush error [{self._stream_type}]: {e}")

        self._buffer.clear()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self._flush()


# ── Factory ───────────────────────────────────────────────────────────────────

def make_parquet_writers(
    parquet_dir: str, flush_rows: int = 10_000, flush_seconds: int = 60
) -> Dict[str, ParquetWriter]:
    return {
        name: ParquetWriter(parquet_dir, name, schema, flush_rows, flush_seconds)
        for name, schema in SCHEMAS.items()
    }
