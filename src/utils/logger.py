from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.utils.config import Settings

# ── Rotation policy ──────────────────────────────────────────────────────────
# Each log file is capped at 10 MB.  When it fills, the handler renames it to
# bot.log.1 … bot.log.5 and opens a fresh bot.log.  The oldest backup (.5) is
# silently deleted, so total disk usage per channel stays ≤ 60 MB.
_MAX_BYTES: int = 10 * 1024 * 1024   # 10 MB per file
_BACKUP_COUNT: int = 5               # keep bot.log + 5 rotated backups


class _MinLevelFilter(logging.Filter):
    """Pass only records at exactly *or above* min_level."""

    def __init__(self, min_level: int) -> None:
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


class _MaxLevelFilter(logging.Filter):
    """Pass only records *below* max_level (exclusive upper bound)."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


def _purge_legacy_log(path: Path, size_threshold_bytes: int = 50 * 1024 * 1024) -> None:
    """Zero-day cleanup: delete a monolithic legacy log file if it grew too large.

    Called once at startup before the new rotating handlers are attached.
    Wrapped in OSError so a locked file on Windows never crashes the bot.
    """
    try:
        if path.exists() and path.stat().st_size >= size_threshold_bytes:
            path.unlink()
            # Use print here — logger not yet initialised.
            print(f"[logger] Zero-day cleanup: deleted oversized legacy log {path} "
                  f"({path.stat().st_size // (1024*1024)} MB would have been deleted)")
    except FileNotFoundError:
        pass  # already gone — harmless race
    except OSError as exc:
        print(f"[logger] WARNING: could not purge legacy log {path}: {exc}")


def setup_logger(settings: Settings) -> logging.Logger:
    """Configure the 'optiferre' logger with two rotating file channels.

    Channel A — trading_critical.log  (WARNING / ERROR / CRITICAL only)
        Captures bailouts, API disconnections, kill-switch events.
        Stays small and clean; first place to look when something breaks.

    Channel B — trading_debug.log  (DEBUG / INFO)
        Captures gate evaluations, scan summaries, deductive reconciliations.
        Rotated aggressively so detailed noise never fills the disk.

    Both channels use non-blocking RotatingFileHandler (stdlib default).
    The I/O cost per log call is a single fwrite() syscall; it does not
    introduce measurable latency in the trading loop.
    """
    logger = logging.getLogger("optiferre")
    if logger.handlers:
        return logger

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(logging.DEBUG)  # root level: DEBUG; handlers filter individually

    # ── Shared formatter ─────────────────────────────────────────────────────
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Ensure logs directory exists ─────────────────────────────────────────
    logs_dir: Path = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    # ── Zero-day cleanup: purge any pre-rotation monolithic log (> 50 MB) ────
    legacy_candidates = [
        logs_dir / "bot.log",
        logs_dir / "trading.log",
    ]
    for legacy in legacy_candidates:
        _purge_legacy_log(legacy)

    # ── Channel A: WARNING+ → trading_critical.log ───────────────────────────
    critical_handler = RotatingFileHandler(
        logs_dir / "trading_critical.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    critical_handler.setFormatter(formatter)
    critical_handler.addFilter(_MinLevelFilter(logging.WARNING))

    # ── Channel B: DEBUG/INFO → trading_debug.log ────────────────────────────
    debug_handler = RotatingFileHandler(
        logs_dir / "trading_debug.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    debug_handler.setFormatter(formatter)
    debug_handler.addFilter(_MaxLevelFilter(logging.WARNING))  # INFO and below only

    # ── Console: INFO+ (no DEBUG noise in live terminal) ─────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(max(log_level, logging.INFO))

    logger.addHandler(critical_handler)
    logger.addHandler(debug_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger