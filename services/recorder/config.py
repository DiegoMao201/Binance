import os
from dataclasses import dataclass, field
from typing import List

SPOT_SYMBOLS: List[str] = [
    "ETHFDUSD", "SOLFDUSD", "XRPFDUSD", "DOGEFDUSD", "LINKFDUSD", "BNBFDUSD"
]
# BTCUSDT included as market factor only — not tradeable
FUTURES_SYMBOLS: List[str] = [
    "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT", "BNBUSDT", "BTCUSDT"
]


@dataclass
class RecorderConfig:
    database_url: str = field(
        default_factory=lambda: os.environ["DATABASE_URL"]
    )
    data_dir: str = field(
        default_factory=lambda: os.getenv("RECORDER_DATA_DIR", "/data/binance-recorder")
    )
    proxy_url: str = field(
        default_factory=lambda: os.getenv("BINANCE_PROXY_URL", "")
    )
    spot_symbols: List[str] = field(default_factory=lambda: list(SPOT_SYMBOLS))
    futures_symbols: List[str] = field(default_factory=lambda: list(FUTURES_SYMBOLS))

    # Parquet flush thresholds
    parquet_flush_rows: int = field(
        default_factory=lambda: int(os.getenv("PARQUET_FLUSH_ROWS", "10000"))
    )
    parquet_flush_seconds: int = field(
        default_factory=lambda: int(os.getenv("PARQUET_FLUSH_SECONDS", "60"))
    )

    # Intervals
    depth_snapshot_interval_s: int = 300    # resync spot book every 5 min
    rest_poll_interval_s: int = 300         # OI/L/S metrics every 5 min
    exchange_info_interval_s: int = 86_400  # exchangeInfo daily

    # Dead-WS detector (Hummingbot exchange_py_base.py:1129 pattern)
    ws_silence_threshold_s: float = 10.0

    # Set FUTURES_WS_ENABLED=false when fstream.binance.com is geo-restricted
    # (data flows fine via REST fallback: fapi.binance.com/fapi/v1/premiumIndex)
    futures_ws_enabled: bool = field(
        default_factory=lambda: os.getenv("FUTURES_WS_ENABLED", "true").lower() != "false"
    )

    # Set DEPTH_ENABLED=false to drop @depth@100ms streams (~77GB/month bandwidth).
    # Order book state becomes unavailable; book_imbalance signals use @bookTicker only.
    depth_enabled: bool = field(
        default_factory=lambda: os.getenv("DEPTH_ENABLED", "true").lower() != "false"
    )

    @property
    def raw_dir(self) -> str:
        return os.path.join(self.data_dir, "raw")

    @property
    def parquet_dir(self) -> str:
        return os.path.join(self.data_dir, "parquet")

    @property
    def futures_ws_url(self) -> str:
        streams = ["!forceOrder@arr"]
        for sym in self.futures_symbols:
            streams.append(f"{sym.lower()}@markPrice@1s")
        return "wss://fstream.binance.com/stream?streams=" + "/".join(streams)

    @property
    def spot_ws_url(self) -> str:
        streams = []
        for sym in self.spot_symbols:
            s = sym.lower()
            base = [f"{s}@bookTicker", f"{s}@aggTrade"]
            if self.depth_enabled:
                base.append(f"{s}@depth@100ms")
            streams.extend(base)
        streams.append("fdusdusdt@bookTicker")
        # Port 443 used instead of 9443: proxy (tinyproxy) ACL only allows :443
        return "wss://stream.binance.com:443/stream?streams=" + "/".join(streams)

    @classmethod
    def from_env(cls) -> "RecorderConfig":
        return cls()
