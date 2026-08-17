"""
Disk guard for the recorder data directory.

Rules:
- Warn at ≤25% free.
- Stop writing (raise DiskFullError) at ≤10% free.
- Log metric disk_free_pct every 60s via health monitor.

If the recorder fills the disk it takes down Deriv (PostgreSQL WAL writes fail).
This guard stops the recorder before that happens.
"""

import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)


class DiskFullError(RuntimeError):
    """Raised when disk is too full to continue writing safely."""


def disk_free_pct(path: str) -> float:
    """Return percentage of free space (0–100) for the filesystem containing `path`."""
    stat = shutil.disk_usage(path)
    return 100.0 * stat.free / stat.total


def check_disk(path: str, warn_pct: float = 25.0, stop_pct: float = 10.0) -> None:
    """
    Check available disk space. Raise DiskFullError if below stop_pct.
    Log a warning if below warn_pct.
    """
    pct = disk_free_pct(path)
    if pct <= stop_pct:
        msg = f"DISK_GUARD: {pct:.1f}% free — STOPPING recorder to protect Deriv"
        logger.error(msg)
        raise DiskFullError(msg)
    if pct <= warn_pct:
        logger.warning(f"DISK_GUARD: {pct:.1f}% free — approaching limit")


class DiskGuardLoop:
    """
    Periodic disk check task. Runs every 60s.
    Raises DiskFullError to propagate up and stop the recorder.
    """

    def __init__(self, data_dir: str, warn_pct: float = 25.0, stop_pct: float = 10.0):
        self._data_dir = data_dir
        self._warn_pct = warn_pct
        self._stop_pct = stop_pct
        self._last_log = 0.0

    async def run(self) -> None:
        import asyncio
        while True:
            await asyncio.sleep(60)
            check_disk(self._data_dir, self._warn_pct, self._stop_pct)
            now = time.monotonic()
            if now - self._last_log > 300:  # log every 5 min
                pct = disk_free_pct(self._data_dir)
                logger.info(f"disk_free_pct={pct:.1f}% path={self._data_dir}")
                self._last_log = now
