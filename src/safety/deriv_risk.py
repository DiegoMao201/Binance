"""
src/safety/deriv_risk.py
─────────────────────────────────────────────────────────────────────────────
Multi-factor scoring + anti-overtrading guardrails for the Deriv pipeline.

Hard rules (NEVER bypass):
  1. Score must be >= settings.min_score (default 7.5/10) before any order.
  2. After N consecutive losses (default 3) → 12 h global lockout.
  3. If daily realized DD reaches max_daily_dd_pct of bankroll → 12 h lockout.
  4. Spread > settings.max_spread_pct → veto.
  5. Detected spike on Boom/Crash without warning → veto.
  6. NO Martingale: position size is always proportional to bankroll
     and synthetic-ATR; doubling-down on losses is prohibited.

Score factors v2 (weighted out of 10):
  - Trend + linear regression (3 pts)  — multi-window + slope strength
  - Momentum quality       (1.5 pts)   — rate-of-change acceleration
  - Adaptive ATR           (2 pts)     — current ATR vs rolling percentile
  - Spread tightness       (0.5 pts)   — current spread <= 0.5 * max_spread
  - Stability              (1.5 pts)   — spike/noise filter
  - Loss-streak penalty   (-2 pts)     — applied if 1+ consecutive loss
  - Cooldown bonus         (1 pt)      — applied after a clean cooldown
  - Bankroll headroom      (0.5 pts)   — daily DD < 50 % of cap
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable

import numpy as np

from src.strategies.deriv_signals import (
    direction_veto as _spike_direction_veto,
    forced_side as _spike_forced_side,
    get_eval_mode as _get_eval_mode,
    is_spike_market as _is_spike_market,
    spike_interval_ticks as _spike_interval_ticks,
    get_asset_profile as _get_asset_profile,
)
from src.utils.deriv_config import DerivSettings
from src.analysis.market_geometry import compute_geometry as _compute_geometry


_LOGGER = logging.getLogger(__name__)


# ── EMA-200 Spike Hunter helper ───────────────────────────────────────────────
def _ema200(prices: list[float]) -> float | None:
    """Compute EMA-200 from the last 200 ticks. Returns None if insufficient data."""
    if len(prices) < 200:
        return None
    arr = np.asarray(prices[-200:], dtype=np.float64)
    alpha = 2.0 / 201.0
    ema = float(arr[:20].mean())
    for p in arr[20:]:
        ema = p * alpha + ema * (1.0 - alpha)
    return ema

# ─── Hurst Calibration from PostgreSQL ────────────────────────────────────────
# Reads v_deriv_hurst_buckets every hour (async, decoupled from WS hot path).
# Updates a shared dict that DerivRiskManager reads on every evaluate() call.
# No asyncpg pool is kept open between calibrations — single short-lived conn.

_HURST_CALIBRATION_INTERVAL = int(os.getenv("DERIV_HURST_CALIBRATION_SEC", "3600"))  # 1 h
_DERIV_DATABASE_URL          = os.getenv("DATABASE_URL", "")

# Shared calibration state (written by HurstCalibrator, read by DerivRiskManager)
# Structure:  {round(hurst_bucket,1): {"win_rate": float, "profit_factor": float, "trades": int}}
_hurst_bucket_stats: dict[float, dict[str, float]] = {}
_hurst_calibration_ts: float = 0.0   # epoch of last successful refresh

# ── Fine-grained view: win_rate per 0.05-step bucket (computed from DB rows) ──
# If the DB view only returns 0.1-step buckets we still get useful granularity.
# Populated alongside _hurst_bucket_stats.
_hurst_bucket_stats_fine: dict[float, dict[str, float]] = {}

# Profit-factor helper: needed because v_deriv_hurst_buckets does not include
# a profit_factor column by default (only added if migration is updated).
# We compute a synthetic proxy: if win_rate >= 0.5 → PF = win_rate / (1 - win_rate).
def _synthetic_profit_factor(win_rate: float) -> float:
    if win_rate <= 0:
        return 0.0
    if win_rate >= 1.0:
        return 99.0
    return round(win_rate / (1.0 - win_rate), 3)


class HurstCalibrator:
    """Background hourly loop that fetches v_deriv_hurst_buckets from PostgreSQL
    and updates the global _hurst_bucket_stats dict.

    Runs as an independent asyncio Task in main_deriv.py.
    Uses a single short-lived asyncpg connection per refresh cycle — no
    persistent pool to maintain, and zero latency impact on the WS hot path.
    """

    def __init__(self) -> None:
        self._last_run: float = 0.0

    async def calibration_loop(self) -> None:
        """Long-running task: calibrate immediately on start, then every hour."""
        _LOGGER.info("[hurst-calib] starting calibration loop (interval=%ss)", _HURST_CALIBRATION_INTERVAL)
        while True:
            try:
                await self._run_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[hurst-calib] refresh failed: %s — using stale cache", exc)
            await asyncio.sleep(_HURST_CALIBRATION_INTERVAL)

    async def _run_once(self) -> None:
        global _hurst_bucket_stats, _hurst_calibration_ts, _hurst_bucket_stats_fine

        if not _DERIV_DATABASE_URL:
            _LOGGER.debug("[hurst-calib] DATABASE_URL not set — calibration skipped")
            return

        try:
            import asyncpg  # optional dep — already in requirements.txt
        except ImportError:
            _LOGGER.warning("[hurst-calib] asyncpg not installed — calibration skipped")
            return

        conn = await asyncio.wait_for(
            asyncpg.connect(_DERIV_DATABASE_URL), timeout=8.0
        )
        try:
            rows = await conn.fetch(
                "SELECT hurst_bucket, win_rate, profit_factor, trades "
                "FROM v_deriv_hurst_buckets"
            )
        except Exception:
            # v_deriv_hurst_buckets may not have profit_factor yet — fall back
            rows = await conn.fetch(
                "SELECT hurst_bucket, win_rate, trades "
                "FROM v_deriv_hurst_buckets"
            )
        finally:
            await conn.close()

        new_stats: dict[float, dict[str, float]] = {}
        for row in rows:
            bucket = float(row["hurst_bucket"] or 0)
            win_rate = float(row["win_rate"] or 0)
            trades = int(row["trades"] or 0)
            pf = float(row.get("profit_factor") or _synthetic_profit_factor(win_rate))
            new_stats[round(bucket, 1)] = {
                "win_rate": win_rate,
                "profit_factor": pf,
                "trades": trades,
            }

        _hurst_bucket_stats = new_stats
        _hurst_calibration_ts = time.time()
        _LOGGER.info(
            "[hurst-calib] refreshed %d buckets | ts=%s",
            len(new_stats),
            datetime.now(timezone.utc).strftime("%H:%M UTC"),
        )

    @staticmethod
    def get_bucket_stats(hurst: float) -> dict[str, float] | None:
        """Return stats for the 0.1-step bucket containing `hurst`. Thread-safe read."""
        bucket = round(round(hurst * 10) / 10, 1)   # snap to nearest 0.1
        return _hurst_bucket_stats.get(bucket)

    @staticmethod
    def calibration_age_seconds() -> float:
        if _hurst_calibration_ts == 0:
            return float("inf")
        return time.time() - _hurst_calibration_ts


# ─── Macro HD Slope calibrator from PostgreSQL ────────────────────────────────
# Fetches the last N snapshots from deriv_tick_snapshots and runs OLS on
# last_price to compute a macro trend slope equivalent to 3000-5000 ticks of
# history — much more stable than the 500-tick in-memory OLS fallback.
# Runs as an independent asyncio Task every 5 minutes (configurable).
#
# Cached structure: {SYMBOL: (slope_pct_per_snapshot, epoch_ts)}
_macro_hd_slope_cache: dict[str, tuple[float, float]] = {}
_MACRO_HD_REFRESH_SEC     = int(os.getenv("DERIV_MACRO_HD_REFRESH_SEC", "300"))   # 5 min
_MACRO_HD_SNAPSHOTS       = int(os.getenv("DERIV_MACRO_HD_SNAPSHOTS", "150"))    # ~4500t equiv
_MACRO_HD_FLAT_THRESHOLD  = float(os.getenv("DERIV_MACRO_HD_FLAT_PCT", "0.0001"))  # % per step


class MacroHDCalibrator:
    """Background task that computes per-symbol macro slope from PostgreSQL.

    Every `_MACRO_HD_REFRESH_SEC` seconds it fetches the last
    `_MACRO_HD_SNAPSHOTS` rows of `deriv_tick_snapshots` (one price point ~
    every 30 live ticks) and runs a simple OLS regression over `last_price`
    to determine the macro trend direction.  The result is stored in
    `_macro_hd_slope_cache` so `DerivRiskManager._higher_direction_bonus()`
    can use a genuinely long-memory view instead of the capped 500-tick
    in-memory buffer.

    Falls back gracefully when DATABASE_URL is not set or asyncpg is absent.
    """

    async def calibration_loop(self) -> None:
        """Long-running task: calibrate immediately, then every _MACRO_HD_REFRESH_SEC."""
        _LOGGER.info(
            "[macro-hd] starting calibration loop (snapshots=%d interval=%ds)",
            _MACRO_HD_SNAPSHOTS, _MACRO_HD_REFRESH_SEC,
        )
        while True:
            try:
                await self._run_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[macro-hd] refresh failed: %s — using in-memory fallback", exc)
            await asyncio.sleep(_MACRO_HD_REFRESH_SEC)

    async def _run_once(self) -> None:
        global _macro_hd_slope_cache

        if not _DERIV_DATABASE_URL:
            _LOGGER.debug("[macro-hd] DATABASE_URL not set — calibration skipped")
            return

        try:
            import asyncpg  # optional dep
        except ImportError:
            _LOGGER.warning("[macro-hd] asyncpg not installed — calibration skipped")
            return

        conn = await asyncio.wait_for(
            asyncpg.connect(_DERIV_DATABASE_URL), timeout=8.0
        )
        try:
            rows = await conn.fetch(
                """
                SELECT symbol, last_price
                FROM (
                    SELECT symbol, last_price,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol ORDER BY captured_at DESC
                           ) AS rn
                    FROM deriv_tick_snapshots
                    WHERE last_price > 0
                ) ranked
                WHERE rn <= $1
                ORDER BY symbol, rn DESC
                """,
                _MACRO_HD_SNAPSHOTS,
            )
        finally:
            await conn.close()

        # Group prices per symbol (rows come back newest-first per symbol,
        # but we ordered rn DESC so they are in chronological order: oldest first
        # when rn=N is oldest, rn=1 is newest — we reverse back below).
        by_symbol: dict[str, list[float]] = {}
        for row in rows:
            sym = str(row["symbol"]).upper()
            p = float(row["last_price"])
            if p > 0:
                by_symbol.setdefault(sym, []).append(p)

        new_cache: dict[str, tuple[float, float]] = {}
        ts_now = time.time()
        for sym, prices_desc in by_symbol.items():
            # prices_desc is newest→oldest (rn DESC); reverse for OLS x-axis = time
            prices = list(reversed(prices_desc))
            n = len(prices)
            if n < 20:
                continue
            x_mean = (n - 1) / 2.0
            y_mean = sum(prices) / n
            if y_mean == 0:
                continue
            ss_xy = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
            ss_xx = sum((i - x_mean) ** 2 for i in range(n))
            if ss_xx == 0:
                continue
            slope_pct = (ss_xy / ss_xx) / y_mean * 100.0
            new_cache[sym] = (slope_pct, ts_now)

        _macro_hd_slope_cache = new_cache
        _LOGGER.info(
            "[macro-hd] refreshed %d symbols | slopes=%s | %s",
            len(new_cache),
            {s: f"{v[0]:+.5f}%" for s, v in new_cache.items()},
            datetime.now(timezone.utc).strftime("%H:%M UTC"),
        )

    @staticmethod
    def get_slope(symbol: str) -> float | None:
        """Return cached macro slope_pct for symbol, or None if missing/stale."""
        entry = _macro_hd_slope_cache.get(symbol.upper())
        if entry is None:
            return None
        slope_pct, ts = entry
        # Treat as stale after 3 refresh intervals (15 min default)
        if time.time() - ts > _MACRO_HD_REFRESH_SEC * 3:
            return None
        return slope_pct



# ─── Macro HD Slope calibrator from PostgreSQL ────────────────────────────────
# Fetches the last N snapshots from deriv_tick_snapshots and runs OLS on
# last_price to compute a macro trend slope equivalent to 3000-5000 ticks of
# history — much more stable than the 500-tick in-memory OLS fallback.
# Runs as an independent asyncio Task every 5 minutes (configurable).
#
# Cached structure: {SYMBOL: (slope_pct_per_snapshot, epoch_ts)}
_macro_hd_slope_cache: dict[str, tuple[float, float]] = {}
_MACRO_HD_REFRESH_SEC     = int(os.getenv("DERIV_MACRO_HD_REFRESH_SEC", "300"))   # 5 min
_MACRO_HD_SNAPSHOTS       = int(os.getenv("DERIV_MACRO_HD_SNAPSHOTS", "150"))    # ~4500t equiv
_MACRO_HD_FLAT_THRESHOLD  = float(os.getenv("DERIV_MACRO_HD_FLAT_PCT", "0.0001"))  # % per step


class MacroHDCalibrator:
    """Background task that computes per-symbol macro slope from PostgreSQL.

    Every `_MACRO_HD_REFRESH_SEC` seconds it fetches the last
    `_MACRO_HD_SNAPSHOTS` rows of `deriv_tick_snapshots` (one price point ~
    every 30 live ticks) and runs a simple OLS regression over `last_price`
    to determine the macro trend direction.  The result is stored in
    `_macro_hd_slope_cache` so `DerivRiskManager._higher_direction_bonus()`
    can use a genuinely long-memory view instead of the capped 500-tick
    in-memory buffer.

    Falls back gracefully when DATABASE_URL is not set or asyncpg is absent.
    """

    async def calibration_loop(self) -> None:
        """Long-running task: calibrate immediately, then every _MACRO_HD_REFRESH_SEC."""
        _LOGGER.info(
            "[macro-hd] starting calibration loop (snapshots=%d interval=%ds)",
            _MACRO_HD_SNAPSHOTS, _MACRO_HD_REFRESH_SEC,
        )
        while True:
            try:
                await self._run_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[macro-hd] refresh failed: %s — using in-memory fallback", exc)
            await asyncio.sleep(_MACRO_HD_REFRESH_SEC)

    async def _run_once(self) -> None:
        global _macro_hd_slope_cache

        if not _DERIV_DATABASE_URL:
            _LOGGER.debug("[macro-hd] DATABASE_URL not set — calibration skipped")
            return

        try:
            import asyncpg  # optional dep
        except ImportError:
            _LOGGER.warning("[macro-hd] asyncpg not installed — calibration skipped")
            return

        conn = await asyncio.wait_for(
            asyncpg.connect(_DERIV_DATABASE_URL), timeout=8.0
        )
        try:
            rows = await conn.fetch(
                """
                SELECT symbol, last_price
                FROM (
                    SELECT symbol, last_price,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol ORDER BY captured_at DESC
                           ) AS rn
                    FROM deriv_tick_snapshots
                    WHERE last_price > 0
                ) ranked
                WHERE rn <= $1
                ORDER BY symbol, rn DESC
                """,
                _MACRO_HD_SNAPSHOTS,
            )
        finally:
            await conn.close()

        # Group prices per symbol (rows come back newest-first per symbol,
        # but we ordered rn DESC so they are in chronological order: oldest first
        # when rn=N is oldest, rn=1 is newest — we reverse back below).
        by_symbol: dict[str, list[float]] = {}
        for row in rows:
            sym = str(row["symbol"]).upper()
            p = float(row["last_price"])
            if p > 0:
                by_symbol.setdefault(sym, []).append(p)

        new_cache: dict[str, tuple[float, float]] = {}
        ts_now = time.time()
        for sym, prices_desc in by_symbol.items():
            # prices_desc is newest→oldest (rn DESC); reverse for OLS x-axis = time
            prices = list(reversed(prices_desc))
            n = len(prices)
            if n < 20:
                continue
            x_mean = (n - 1) / 2.0
            y_mean = sum(prices) / n
            if y_mean == 0:
                continue
            ss_xy = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
            ss_xx = sum((i - x_mean) ** 2 for i in range(n))
            if ss_xx == 0:
                continue
            slope_pct = (ss_xy / ss_xx) / y_mean * 100.0
            new_cache[sym] = (slope_pct, ts_now)

        _macro_hd_slope_cache = new_cache
        _LOGGER.info(
            "[macro-hd] refreshed %d symbols | slopes=%s | %s",
            len(new_cache),
            {s: f"{v[0]:+.5f}%" for s, v in new_cache.items()},
            datetime.now(timezone.utc).strftime("%H:%M UTC"),
        )

    @staticmethod
    def get_slope(symbol: str) -> float | None:
        """Return cached macro slope_pct for symbol, or None if missing/stale."""
        entry = _macro_hd_slope_cache.get(symbol.upper())
        if entry is None:
            return None
        slope_pct, ts = entry
        # Treat as stale after 3 refresh intervals (15 min default)
        if time.time() - ts > _MACRO_HD_REFRESH_SEC * 3:
            return None
        return slope_pct



# ─── Snapshot returned to the trader ──────────────────────────────────────────
@dataclass(slots=True)
class DerivRiskSnapshot:
    allowed: bool
    score: float
    side: str | None  # "MULTUP" | "MULTDOWN" | None
    reasons: list[str] = field(default_factory=list)
    suggested_stake_usdt: float = 0.0
    suggested_multiplier: int = 100
    spread_pct: float = 0.0
    synthetic_atr: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    regime: str = "unknown"   # "trending" | "ranging" | "volatile" | "calm"
    # Hurst calibration telemetry
    hurst_score_delta: float = 0.0        # penalty/bonus applied to raw score
    effective_min_score: float = 0.0      # actual threshold used (may be lowered by H>0.62)
    hurst_ai_override: bool = False       # True when AI was overridden by strong Hurst math


@dataclass(slots=True)
class _LockoutState:
    locked: bool
    reason: str
    until_ts: float

    @classmethod
    def empty(cls) -> "_LockoutState":
        return cls(locked=False, reason="", until_ts=0.0)


class DerivRiskManager:
    """Multi-factor scoring + lockout guardrails for Deriv synthetics."""

    def __init__(self, settings: DerivSettings) -> None:
        self._settings = settings
        self._loss_streak = 0
        # PATCH 2026-05-25: consecutive_wins enables auto-unlock when the bot
        # recovers (any winning trade after being locked clears the lockout
        # immediately; N consecutive wins also forces a hard reset).
        self._consecutive_wins = 0
        self._last_trade_ts = 0.0
        # Per-symbol trade timestamp for independent cooldown_bonus tracking.
        # Prevents R_50/R_75 active trading from stealing BOOM/CRASH cooldown bonus.
        self._last_trade_ts_per_symbol: dict[str, float] = {}
        # ── FVG credit cache ───────────────────────────────────────────────
        # When a BOOM/CRASH symbol detects an active FVG with score >= threshold,
        # we store a credit for N cycles so that if ATR passes within that window
        # the entry is still valid (fixes the race condition where SMC fires in
        # cycle K but ATR only passes in cycle K+3).
        # Format: {symbol: {"cycles_remaining": int, "score_at_detection": float}}
        self._fvg_credit: dict[str, dict] = {}
        self._daily_realized_pnl = 0.0
        self._daily_anchor_date = datetime.now(timezone.utc).date()
        # Per-symbol rolling tick window for synthetic ATR / trend.
        self._ticks: dict[str, list[float]] = {}
        self._max_window = 1000  # enlarged for multi-TF geometry (was 270)
        # Per-symbol rolling ATR history (FIX-8: 50→500 for accurate percentile over days not hours)
        self._atr_history: dict[str, list[float]] = {}
        self._MAX_ATR_HISTORY = 500
        # Force-test-trades tick counter (DERIV_FORCE_TEST_TRADES mode)
        self._force_tick_count: dict[str, int] = {}
        # ── Spike-cycle tracker (BOOM/CRASH only) ─────────────────────────
        # Records the wall-clock time of the last detected spike per symbol.
        # A "spike" is any single tick whose absolute jump exceeds 3× recent ATR.
        # Used in evaluate() to gate entries: we only allow entry when enough
        # time has elapsed since the last spike (the market needs to re-accumulate
        # before the next spike is statistically due).
        self._last_spike_ts: dict[str, float] = {}
        # Running per-symbol tick counter — incremented in ingest_tick.
        self._ingest_tick_count: dict[str, int] = {}
        # ── Spike-cycle tracker (BOOM/CRASH only) ─────────────────────────
        # Records the wall-clock time of the last detected spike per symbol.
        # A "spike" is any single tick whose absolute jump exceeds 3× recent ATR.
        # Used in evaluate() to gate entries: we only allow entry when enough
        # time has elapsed since the last spike (the market needs to re-accumulate
        # before the next spike is statistically due).
        self._last_spike_ts: dict[str, float] = {}
        # Running per-symbol tick counter — incremented in ingest_tick.
        self._ingest_tick_count: dict[str, int] = {}
        # Tick count at the moment of the last detected spike (for ticks_since_last_spike).
        self._last_spike_tick: dict[str, int] = {}
        # Rolling inter-spike intervals in tick domain (noise-filtered).
        # Used by main_deriv spike_pre_filter to adapt min_post window per symbol.
        self._spike_intervals: dict[str, list[int]] = {}
        self._MAX_SPIKE_INTERVALS = 80
        _entry_tick_only_raw = os.getenv("DERIV_ENTRY_TICK_ONLY", "true").strip().lower()
        # Tick-only entry mode: spike events remain for telemetry and dynamic tuning,
        # but cannot directly gate or force entry decisions.
        self._entry_tick_only = _entry_tick_only_raw in {"1", "true", "yes", "on"}
        self._allow_spike_history_entry_gates = not self._entry_tick_only
        _allow_spike_ai_override_raw = os.getenv(
            "DERIV_ALLOW_SPIKE_ACTIVE_AI_OVERRIDE",
            "false",
        ).strip().lower()
        self._allow_spike_active_ai_override = _allow_spike_ai_override_raw in {
            "1",
            "true",
            "yes",
            "on",
        }
        # Honor DERIV_RESET_LOCKOUT=true env var — clears stale lockout on restart
        if os.getenv("DERIV_RESET_LOCKOUT", "").lower() in ("1", "true", "yes"):
            lf = settings.lockout_file
            if lf.exists():
                try:
                    lf.unlink()
                    _LOGGER.warning("[risk] DERIV_RESET_LOCKOUT=true — lockout file cleared on startup")
                except Exception:  # noqa: BLE001
                    pass

        # ── State hydration: restore consecutive_losses + bankroll from disk ──
        # Survives daemon/deploy restarts so loss-streak protection isn't lost
        # to amnesia. Only loads fields that exist; missing fields are ignored.
        try:
            lf = settings.lockout_file
            if lf.exists():
                _raw = json.loads(lf.read_text())
                if isinstance(_raw, dict):
                    _ls = int(_raw.get("consecutive_losses", 0) or 0)
                    if _ls > 0:
                        self._loss_streak = _ls
                        _LOGGER.info(
                            "[risk] hydrated consecutive_losses=%d from lockout file",
                            _ls,
                        )
                    _bk = float(_raw.get("bankroll", 0) or 0)
                    if _bk > 0 and abs(_bk - settings.bankroll_usdt) > 0.01:
                        _LOGGER.info(
                            "[risk] persisted bankroll=%.2f differs from config=%.2f (using config)",
                            _bk, settings.bankroll_usdt,
                        )
        except Exception as _hyd_exc:  # noqa: BLE001
            _LOGGER.warning("[risk] state hydration failed: %s — starting fresh", _hyd_exc)

        # ── Demo-mode consecutive_losses auto-reset ────────────────────────
        # In demo (DERIV_DRY_RUN=true) an accumulated streak should never
        # permanently block trading.  If the streak is already ≥ the lockout
        # cap we zero it on startup AND clear any stale lockout timer so the
        # demo session starts clean without manual intervention.
        _is_demo = os.getenv("DERIV_DRY_RUN", "true").lower() in ("1", "true", "yes")
        _cap = settings.loss_streak_lockout
        if _is_demo and self._loss_streak >= _cap:
            _LOGGER.warning(
                "[LOCKOUT_STATUS] DEMO MODE: consecutive_losses=%d >= cap=%d — "
                "auto-resetting streak and clearing lockout timer for demo session",
                self._loss_streak, _cap,
            )
            self._loss_streak = 0
            # Patch the lockout file in-place: zero the streak and expire the
            # lockout timer so _read_lockout() returns empty on first evaluate().
            try:
                lf = settings.lockout_file
                _existing: dict = {}
                if lf.exists():
                    try:
                        _existing = json.loads(lf.read_text()) or {}
                    except Exception:  # noqa: BLE001
                        _existing = {}
                _existing.update({
                    "consecutive_losses": 0,
                    "locked": False,
                    "until_ts": 0.0,
                    "reason": "demo_auto_reset",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                lf.parent.mkdir(parents=True, exist_ok=True)
                _tmp = lf.with_suffix(lf.suffix + ".tmp")
                _tmp.write_text(json.dumps(_existing, indent=2))
                _tmp.replace(lf)
                _LOGGER.warning(
                    "[LOCKOUT_STATUS] lockout file patched: consecutive_losses=0, locked=False",
                )
            except Exception as _rst_exc:  # noqa: BLE001
                _LOGGER.warning("[LOCKOUT_STATUS] could not patch lockout file: %s", _rst_exc)

        # ── Startup lockout status — visible in every boot log ─────────────
        _lockout_on_start = self._read_lockout()
        _LOGGER.warning(
            "[LOCKOUT_STATUS] consecutive_losses=%d max=%d locked=%s%s",
            self._loss_streak,
            _cap,
            _lockout_on_start.locked,
            f" until={datetime.fromtimestamp(_lockout_on_start.until_ts, timezone.utc):%Y-%m-%dT%H:%MZ} reason={_lockout_on_start.reason!r}"
            if _lockout_on_start.locked else "",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public surface called by the daemon / trader / order router
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def check_random_walk_prefilter(symbol: str, hurst: float | None) -> dict | None:
        """Pre-pipeline Hurst regime classifier for R_* volatility indices.

        Classifies the symbol into one of three Hurst zones and returns a
        dict with ``block`` / ``regime`` so the daemon can act accordingly:

        • H < 0.45  → mean_reverting  → ``block=False``  (MEAN_REV setups allowed)
        • 0.45 ≤ H ≤ 0.55 → random_walk → ``block=True``  (noise zone, veto)
        • H > 0.55  → trending         → ``block=False``  (TREND setups allowed)

        Returns None for non-R_* symbols (BOOM/CRASH exempt — structural edge).
        Returns None when hurst is None/invalid — caller should skip prefilter.
        """
        if not symbol.startswith("R_"):
            return None
        if hurst is None:
            return None
        try:
            h = float(hurst)
        except (TypeError, ValueError):
            return None
        if h < 0.45:
            return {
                "block": False,
                "regime": "mean_reverting",
                "hurst": h,
            }
        if h > 0.55:
            return {
                "block": False,
                "regime": "trending",
                "hurst": h,
            }
        # 0.45 ≤ h ≤ 0.55 — split into inner neutral zone and outer hard veto.
        # Inner [DERIV_NEUTRAL_ZONE_LO, DERIV_NEUTRAL_ZONE_HI] = [0.47, 0.53]:
        #   no hard block — delegate -0.5 penalty to evaluate() (C5 logic).
        # Outer [0.45, 0.47) and (0.53, 0.55]: hard veto (no statistical edge).
        _neutral_lo_pre = float(os.getenv("DERIV_NEUTRAL_ZONE_LO", "0.47"))
        _neutral_hi_pre = float(os.getenv("DERIV_NEUTRAL_ZONE_HI", "0.53"))
        if _neutral_lo_pre <= h <= _neutral_hi_pre:
            # Inner neutral zone: passes to evaluate() which applies -0.5 penalty
            # and tags regime=neutral.  Macro/SMC/geo can still provide edge.
            return {
                "block": False,
                "regime": "neutral",
                "hurst": h,
            }
        # Outer random-walk band — still hard veto
        return {
            "block": True,
            "regime": "random_walk",
            "hurst": h,
            "approved": False,
            "reason": "random_walk_zone_prefilter",
            "score": None,
            "ai_called": False,
        }

    def ingest_tick(self, symbol: str, price: float) -> None:
        if price <= 0 or not math.isfinite(price):
            return
        buf = self._ticks.setdefault(symbol, [])
        buf.append(price)
        if len(buf) > self._max_window:
            del buf[: len(buf) - self._max_window]
        # Increment per-symbol tick counter
        self._ingest_tick_count[symbol] = self._ingest_tick_count.get(symbol, 0) + 1
        # ── FEED_QUALITY: duplicate/stale tick detector ────────────────────
        # H=0.200 anomalies (BOOM1000 18/5, BOOM900 19/5) are caused by the
        # broker feed repeating the same price for many consecutive ticks.
        # Detect this and log a WARNING every 100 ticks when quality is poor.
        _tick_n = self._ingest_tick_count[symbol]
        if _tick_n % 100 == 0 and len(buf) >= 50:
            _last50 = buf[-50:]
            _dup_count = sum(1 for i in range(1, len(_last50)) if _last50[i] == _last50[i - 1])
            _dup_pct = _dup_count / (len(_last50) - 1)
            _quality = "poor" if _dup_pct > 0.30 else ("degraded" if _dup_pct > 0.10 else "good")
            if _quality != "good":
                _LOGGER.warning(
                    "[FEED_QUALITY] %s duplicate_ticks=%d/50 (%.0f%%) quality=%s "
                    "— may cause H anomaly; tick_n=%d",
                    symbol, _dup_count, _dup_pct * 100, _quality, _tick_n,
                )
            else:
                _LOGGER.debug(
                    "[FEED_QUALITY] %s duplicate_ticks=%d/50 (%.0f%%) quality=%s tick_n=%d",
                    symbol, _dup_count, _dup_pct * 100, _quality, _tick_n,
                )
        # Update ATR history every 30 ticks
        if len(buf) >= 30 and len(buf) % 10 == 0:
            atr = self._synthetic_atr(buf)
            hist = self._atr_history.setdefault(symbol, [])
            hist.append(atr)
            if len(hist) > self._MAX_ATR_HISTORY:
                del hist[: len(hist) - self._MAX_ATR_HISTORY]
        # ── Spike-cycle detection (BOOM/CRASH only) ────────────────────────
        # A spike is a single-tick jump > 3× recent ATR.
        # Record wall-clock time so evaluate() can gate premature re-entries.
        if len(buf) >= 2 and _is_spike_market(symbol):
            _atr_hist = self._atr_history.get(symbol, [])
            if _atr_hist:
                _recent_atr = mean(_atr_hist[-5:]) if len(_atr_hist) >= 5 else _atr_hist[-1]
                if _recent_atr > 0:
                    _jump = buf[-1] - buf[-2]
                    _su = symbol.upper()
                    _is_boom_spike  = "BOOM"  in _su and _jump >  3.0 * _recent_atr
                    _is_crash_spike = "CRASH" in _su and _jump < -3.0 * _recent_atr
                    if _is_boom_spike or _is_crash_spike:
                        _direction = "UP" if _is_boom_spike else "DOWN"
                        # Capture pre-update values for enrichment fields
                        _prev_spike_ts   = self._last_spike_ts.get(symbol, 0.0)
                        _spike_ts        = time.time()
                        _spike_cluster   = _prev_spike_ts > 0 and (_spike_ts - _prev_spike_ts < 400)
                        _cur_tick_n      = self._ingest_tick_count[symbol]
                        _ticks_since_sp  = _cur_tick_n - self._last_spike_tick.get(symbol, 0)
                        _ema200_at_spike = _ema200(list(buf))
                        _ema200_dev: float | None = None
                        if _ema200_at_spike is not None and _ema200_at_spike > 0:
                            _ema200_dev = round(
                                (buf[-1] - _ema200_at_spike) / _ema200_at_spike, 5
                            )
                        # Update trackers
                        self._last_spike_ts[symbol]   = _spike_ts
                        self._last_spike_tick[symbol] = _cur_tick_n
                        # Persist inter-spike intervals for adaptive pre-filter.
                        # Ignore ultra-short clusters (same impulse burst).
                        _min_real_interval = int(os.getenv("DERIV_SPIKE_INTERVAL_MIN_TICKS", "25"))
                        if _ticks_since_sp >= _min_real_interval:
                            _hist = self._spike_intervals.setdefault(symbol, [])
                            _hist.append(int(_ticks_since_sp))
                            if len(_hist) > self._MAX_SPIKE_INTERVALS:
                                del _hist[: len(_hist) - self._MAX_SPIKE_INTERVALS]
                        _LOGGER.info(
                            "[SPIKE_DETECTED] %s direction=%s jump=%.5f atr=%.5f ratio=%.1f× — "
                            "spike-cycle timer reset",
                            symbol, _direction, _jump, _recent_atr, abs(_jump) / _recent_atr,
                        )
                        # Structured spike event log — captured by log parser and spike_events table
                        _LOGGER.info(
                            "[SPIKE_EVENT] symbol=%s direction=%s jump=%.5f atr=%.5f "
                            "ratio=%.2f ts=%.3f",
                            symbol, _direction, _jump, _recent_atr,
                            abs(_jump) / _recent_atr, _spike_ts,
                        )
                        # Persist spike event to JSON for frontend consumption
                        try:
                            _state_dir = Path(
                                os.environ.get("BOT_STATE_DIR",
                                    os.environ.get("LOGS_DIR", Path(__file__).parents[2] / "logs"))
                            )
                            _state_dir.mkdir(parents=True, exist_ok=True)
                            _spike_file = _state_dir / "deriv_spike_events.json"
                            # --- enriched context available at spike detection time ---
                            _price_at_spike = round(buf[-1], 5)
                            _since_last_trade = round(
                                _spike_ts - self._last_trade_ts_per_symbol.get(symbol.upper(), 0), 1
                            )
                            _lockout_active = False
                            try:
                                _lockout_active = bool(self._read_lockout().locked)
                            except Exception:
                                pass
                            _spike_record = {
                                "ts": _spike_ts,
                                "iso": datetime.fromtimestamp(_spike_ts, tz=timezone.utc).isoformat(),
                                "symbol": symbol,
                                "direction": _direction,
                                "jump": round(_jump, 5),
                                "atr": round(_recent_atr, 5),
                                "ratio": round(abs(_jump) / _recent_atr, 2),
                                "price": _price_at_spike,
                                "loss_streak": int(self._loss_streak),
                                "since_last_trade_s": _since_last_trade,
                                "lockout_active": _lockout_active,
                                # ── Telemetría enriquecida ───────────────
                                "ticks_since_last_spike": _ticks_since_sp,
                                "price_vs_ema200_pct": _ema200_dev,
                                "spike_cluster": _spike_cluster,
                                # bot_entered / block_reason are written post-evaluate
                                # via enrich_last_spike() called from the trading loop
                                "bot_entered": None,
                                "block_reason": None,
                            }
                            _existing: list = []
                            if _spike_file.exists():
                                try:
                                    _existing = json.loads(_spike_file.read_text())
                                except Exception:
                                    _existing = []
                            _existing.append(_spike_record)
                            # Keep last 2000 spike events to avoid unbounded growth
                            if len(_existing) > 2000:
                                _existing = _existing[-2000:]
                            _spike_file.write_text(json.dumps(_existing))
                        except Exception as _e:
                            _LOGGER.debug("[SPIKE_EVENT] failed to persist to JSON: %s", _e)

    def get_last_spike_ts(self, symbol: str) -> float:
        """Return wall-clock time of the last spike detected for *symbol*.

        Returns 0.0 if no spike has been observed yet.  Used by the
        observation-window logic in main_deriv to detect spikes that occur
        DURING the wait period and cancel stale entries.
        """
        return self._last_spike_ts.get(symbol, 0.0)

    def get_last_spike_tick_count(self, symbol: str) -> int:
        """Return the ingest-tick count at which the last spike for *symbol* was detected.

        Returns 0 if no spike has been observed yet.  Used by the spike pre-filter
        gate in main_deriv to compute ticks_since_last_spike in tick-domain instead
        of wall-clock seconds, keeping cooldown semantics consistent with the rest
        of the signal stack (Hurst, ATR, momentum — all tick-indexed).
        """
        return self._last_spike_tick.get(symbol, 0)

    def get_current_atr(self, symbol: str) -> float | None:
        """Return the most-recent synthetic ATR for *symbol* (mean of last 5 ATR samples).

        Returns None if there is no ATR history yet (cold start).
        Used by DerivTradeExecutor to record atr_at_entry in closed contracts
        and by the market-context snapshot loop in main_deriv.
        """
        hist = self._atr_history.get(symbol, [])
        if not hist:
            return None
        val = mean(hist[-5:]) if len(hist) >= 5 else hist[-1]
        return round(val, 6)

    def get_tick_count(self, symbol: str) -> int:
        """Return the cumulative ingest tick count for *symbol*.

        Used to compute ticks_held (entry_tick_count → close_tick_count) and
        ticks_since_last_spike in per-trade and per-spike telemetry.
        """
        return self._ingest_tick_count.get(symbol, 0)

    def get_adaptive_spike_min_post_ticks(
        self,
        symbol: str,
        base_min_post_ticks: int,
        expected_cycle_ticks: int,
    ) -> int:
        """Return adaptive post-spike wait window in ticks for spike markets.

        The adaptive window only relaxes (never increases) the profile baseline.
        It is derived from recent real inter-spike intervals and bounded by
        expected_cycle_ticks so noisy bursts cannot collapse the gate.
        """
        base = int(base_min_post_ticks or 0)
        if base <= 0:
            return 0

        _enabled = os.getenv("DERIV_SPIKE_PREFILTER_ADAPTIVE", "true").lower() in (
            "1", "true", "yes"
        )
        if not _enabled:
            return base

        _hist = self._spike_intervals.get(symbol, [])
        _min_samples = int(os.getenv("DERIV_SPIKE_PREFILTER_MIN_SAMPLES", "6"))
        if len(_hist) < _min_samples:
            return base

        _obs = list(_hist[-40:])
        _obs.sort()
        _median_obs = int(_obs[len(_obs) // 2])

        _expected = int(expected_cycle_ticks or _spike_interval_ticks(symbol) or 0)
        if _expected <= 0:
            _expected = max(base * 2, _median_obs)

        _frac = float(os.getenv("DERIV_SPIKE_PREFILTER_ADAPTIVE_FRAC", "0.55"))
        _min_frac = float(os.getenv("DERIV_SPIKE_PREFILTER_MIN_FLOOR_FRAC", "0.35"))
        _max_frac = float(os.getenv("DERIV_SPIKE_PREFILTER_MAX_CAP_FRAC", "0.85"))

        _candidate = int(_median_obs * _frac)
        _floor = int(_expected * _min_frac)
        _cap = int(_expected * _max_frac)
        _adaptive = max(_floor, min(_candidate, _cap))

        # Never tighten more than profile baseline; only relax when justified.
        return max(25, min(base, _adaptive))

    def enrich_last_spike(
        self,
        symbol: str,
        *,
        bot_entered: bool,
        block_reason: str | None = None,
        score: float | None = None,
        had_open_pos: bool = False,
    ) -> None:
        """Enrich the most-recent spike record for *symbol* with post-evaluate context.

        Called from main_deriv after evaluate() decides whether to enter or block.
        Updates bot_entered, block_reason, score in the last spike JSON record.
        """
        try:
            _state_dir = Path(
                os.environ.get("BOT_STATE_DIR",
                    os.environ.get("LOGS_DIR", Path(__file__).parents[2] / "logs"))
            )
            _spike_file = _state_dir / "deriv_spike_events.json"
            if not _spike_file.exists():
                return
            _existing: list = json.loads(_spike_file.read_text())
            # Prefer the latest unresolved spike so a later event does not
            # overwrite a previously resolved capture for the same symbol.
            _target_idx: int | None = None
            for i in range(len(_existing) - 1, -1, -1):
                if _existing[i].get("symbol") == symbol and _existing[i].get("bot_entered") is None:
                    _target_idx = i
                    break
            if _target_idx is None:
                for i in range(len(_existing) - 1, -1, -1):
                    if _existing[i].get("symbol") == symbol:
                        _target_idx = i
                        break
            if _target_idx is not None:
                _existing[_target_idx]["bot_entered"] = bot_entered
                _existing[_target_idx]["block_reason"] = block_reason
                if score is not None:
                    _existing[_target_idx]["score"] = round(score, 2)
                _existing[_target_idx]["had_open_pos"] = had_open_pos
            _spike_file.write_text(json.dumps(_existing))
        except Exception as _e:
            _LOGGER.debug("[SPIKE_EVENT] enrich_last_spike failed: %s", _e)

    def evaluate(
        self,
        symbol: str,
        spread_pct: float,
        *,
        hurst: float | None = None,
        autocorr_lag1: float | None = None,
        ai_confidence: float | None = None,
        dynamic_cfg: dict[str, Any] | None = None,
    ) -> DerivRiskSnapshot:
        """Score the current state for `symbol` and return an actionable snapshot.

        Optional keyword arguments allow the caller (main_deriv) to pass statistical
        context from DerivAnalyst so calibration rules can be applied:
          hurst          — Hurst exponent for the current symbol (0–1)
          autocorr_lag1  — autocorrelation of log-returns at lag 1
          ai_confidence  — AI gate confidence (0–1); used for the math-override rule
                    dynamic_cfg    — runtime per-symbol config from dynamic_symbol_config
        """
        snap = DerivRiskSnapshot(allowed=False, score=0.0, side=None, spread_pct=spread_pct)
        snap.effective_min_score = self._settings.min_score

        # ── FORCE-TEST-TRADES bypass (demo / integration testing only) ─────────
        # Set DERIV_FORCE_TEST_TRADES=true to skip ALL scoring gates and inject a
        # forced test order every 60 ticks on ANY symbol.  This is purely for
        # verifying the WS buy path works on a virtual (VRTC) account — it
        # must NEVER be enabled in live environments.
        _force_test = os.getenv("DERIV_FORCE_TEST_TRADES", "").lower() in ("1", "true", "yes")
        if _force_test:
            self._force_tick_count[symbol] = self._force_tick_count.get(symbol, 0) + 1
            tick_n = self._force_tick_count[symbol]
            if tick_n % 60 == 0:
                _fs = _spike_forced_side(symbol) or "MULTUP"
                snap.allowed = True
                snap.side = _fs
                snap.score = 9.9
                snap.effective_min_score = 0.0
                snap.suggested_stake_usdt = max(1.0, self._settings.min_stake_usdt if hasattr(self._settings, "min_stake_usdt") else 1.0)
                snap.suggested_multiplier = 1
                snap.reasons.append(f"FORCE_TEST_TRADE tick={tick_n} side={_fs}")
                _LOGGER.warning(
                    "[force-test] AUTO-APPROVE %s | side=%s | tick=%d (DERIV_FORCE_TEST_TRADES=true)",
                    symbol, _fs, tick_n,
                )
                return snap
            snap.reasons.append(f"FORCE_TEST waiting tick {tick_n % 60}/60")
            return snap

        # Hard veto: lockout active?
        lockout = self._read_lockout()
        if lockout.locked:
            snap.reasons.append(
                f"LOCKOUT active ({lockout.reason}) until {datetime.fromtimestamp(lockout.until_ts, timezone.utc):%Y-%m-%d %H:%M UTC}"
            )
            return snap

        # Daily DD reset
        self._roll_daily_anchor_if_needed()
        dd_cap = self._settings.bankroll_usdt * self._settings.max_daily_dd_pct
        if self._daily_realized_pnl <= -dd_cap:
            self._engage_lockout(
                f"daily DD {self._daily_realized_pnl:.2f} USD ≤ -{dd_cap:.2f}"
            )
            snap.reasons.append("Daily drawdown cap reached → lockout engaged")
            return snap

        # Hard veto: spread too wide
        if spread_pct > self._settings.max_spread_pct:
            snap.reasons.append(
                f"spread {spread_pct:.5f} > {self._settings.max_spread_pct:.5f}"
            )
            return snap

        ticks = self._ticks.get(symbol, [])
        _force_test_active = os.getenv("DERIV_FORCE_TEST_TRADES", "").lower() in ("1", "true", "yes")
        # DERIV_MIN_WARMUP_TICKS: minimum tick history before any evaluation.
        # Hard floor of 500 ticks enforced via max() to prevent analysis on
        # thin buffers.  force-test mode uses 5 ticks regardless.
        # Default env value: 600 (overrideable, but always >= 500 in prod).
        _warmup_needed: int
        if _force_test_active:
            _warmup_needed = 5
        else:
            _env_warmup = int(os.getenv("DERIV_MIN_WARMUP_TICKS", "600"))
            _warmup_needed = max(500, _env_warmup)
        if len(ticks) < _warmup_needed:
            snap.reasons.append(f"insufficient ticks ({len(ticks)}/{_warmup_needed})")
            return snap

        # ── Hurst anomaly detector ─────────────────────────────────────────
        # H < 0.30 is structurally impossible for real price series and is a
        # sign of a stale/thin tick buffer (e.g. CRASH600 showed H=0.200 after
        # cold-start — same pattern as BOOM1000 on 2026-05-18 which normalized
        # after the window filled).  Log it to track recovery.
        if hurst is not None and hurst < 0.30:
            _LOGGER.warning(
                "[HURST_ANOMALY] %s H=%.3f ticks=%d — anomalously low Hurst; "
                "buffer may be thin or feed stale.  Score will be suppressed.",
                symbol, hurst, len(ticks),
            )

        # ── Spike-cycle gate (BOOM/CRASH only) ────────────────────────────
        # After a spike fires the market needs to re-accumulate before the
        # next spike is statistically due.  Minimum safe wait =
        # DERIV_SPIKE_CYCLE_FRAC × expected inter-spike interval
        # (BOOM/CRASH 300≈300s, 500≈500s, 1000≈1000s at ~1 tick/s).
        #
        # Default fraction is 0.30 (lowered from 0.40 to reduce execution
        # starvation while still protecting against immediate re-entry).
        #
        # DEFERRED veto: if the gate fires we do NOT return immediately.
        # Instead we let the full scoring pipeline run so that the
        # structural-override check (below) can allow high-quality setups
        # through.  The final resolution happens after EMA200 / SMC scoring.
        _spike_gate_active   = False   # resolved after full pipeline
        _spike_gate_elapsed  = 0.0
        _spike_gate_min_wait = 0.0
        _spike_gate_interval = 300     # updated from symbol name below
        if (
            _is_spike_market(symbol)
            and not _force_test_active
            and self._allow_spike_history_entry_gates
        ):
            _last_spike = self._last_spike_ts.get(symbol, 0.0)
            if _last_spike > 0:
                _sg_elapsed = time.time() - _last_spike
                _su_sg = symbol.upper()
                _spike_gate_interval = (
                    1000 if "1000" in _su_sg
                    else 900 if "900" in _su_sg
                    else 600 if "600" in _su_sg
                    else (500 if "500" in _su_sg else 300)
                )
                _frac = float(os.getenv("DERIV_SPIKE_CYCLE_FRAC", "0.08"))
                _sg_min_wait = _spike_gate_interval * _frac
                if _sg_elapsed < _sg_min_wait:
                    _spike_gate_active   = True
                    _spike_gate_elapsed  = _sg_elapsed
                    _spike_gate_min_wait = _sg_min_wait

        # ── Compute factors ────────────────────────────────────────────────
        atr = self._synthetic_atr(ticks)
        # [ATR_ZERO_FALLBACK]: synthetic indices can freeze (all ticks identical)
        # or the window may be too thin, yielding ATR=0.  When this happens, use
        # the most recent cached non-zero ATR from _atr_history so that ATR-dependent
        # scores (_atr_adaptive_score, _detect_regime) don't collapse silently.
        if atr == 0.0:
            _atr_hist = self._atr_history.get(symbol, [])
            _atr_cached = next((v for v in reversed(_atr_hist) if v > 0.0), 0.0)
            if _atr_cached > 0.0:
                _LOGGER.warning(
                    "[ATR_ZERO_FALLBACK] %s: ATR=0 (window=%d ticks) — "
                    "using cached ATR=%.6f from history",
                    symbol, len(ticks), _atr_cached,
                )
                atr = _atr_cached
        snap.synthetic_atr = round(atr, 8)

        # Detect market regime first (influences score weights)
        regime = self._detect_regime(ticks, atr)
        snap.regime = regime

        trend_score, trend_dir = self._trend_score_v2(ticks)
        momentum_score = self._momentum_score(ticks)
        spread_score = self._spread_score(spread_pct)
        atr_score = self._atr_adaptive_score(atr, ticks[-1], symbol)
        stability_score = self._stability_score(ticks)
        streak_penalty = self._streak_penalty()
        cooldown_bonus = self._cooldown_bonus(symbol)
        headroom_bonus = self._headroom_bonus(dd_cap)

        # ── ATR bypass for spike markets in calm regime ─────────────────────
        # BOOM/CRASH naturally compress ATR between spikes — the tick series
        # is flat by design until the next spike fires.  Penalising ATR in
        # this regime cross-contaminates the SMC/EMA200 signal path.
        # FIX: instead of a multiplier (0×0.50 = 0 still), apply a floor of
        # 1.0 so the component contributes neutrally and stops depressing the
        # total score.  'BYPASS' in log = full neutral contribution restored.
        # For non-calm or non-spike markets, atr_score is unchanged.
        _atr_calm_bypassed = False
        if regime == "calm" and _is_spike_market(symbol):
            if atr_score < 1.0:
                atr_score = 1.0
                _atr_calm_bypassed = True

        # ── FIX-3: Spike structural trend credit ────────────────────────────
        # OLS slope threshold (5e-5) is too coarse for calm synthetic markets:
        # typical calm ATR/price ≈ 0.000176 → tick-slope ≈ 7e-6 → trend_score=0.
        # BOOM/CRASH have an inherent directional asymmetry (forced_side) that IS
        # a structural "trend" the OLS misses.  Award minimum credit so the score
        # is not systematically under-counted by up to 3.0 pts in calm regime.
        if _is_spike_market(symbol) and trend_score == 0.0:
            trend_score = float(os.getenv("DERIV_SPIKE_TREND_CREDIT", "1.5"))
            _LOGGER.debug(
                "[SPIKE_TREND_CREDIT] %s trend_score was 0.0 → applied structural credit=%.1f",
                symbol, trend_score,
            )

        # In volatile regime, require a stronger trend signal
        if regime == "volatile" and trend_dir is not None:
            trend_score = min(trend_score, 2.0)

        score = (
            trend_score
            + momentum_score
            + spread_score
            + atr_score
            + stability_score
            + streak_penalty
            + cooldown_bonus
            + headroom_bonus
        )
        score = max(0.0, min(10.0, score))

        # ── Asset routing: determine eval mode (spike vs stochastic) ──────────
        # BOOM/CRASH: skip Hurst/mean_rev/random_walk — edge is spike asymmetry,
        # not Hurst persistence. R_*: full stochastic pipeline.
        _eval_mode = _get_eval_mode(symbol)
        _is_spike = _eval_mode == "smc_fvg"

        # ── Hurst dynamic calibration ──────────────────────────────────────
        # Applies score delta and/or threshold adjustment based on the hourly
        # snapshot from v_deriv_hurst_buckets.  Zero-cost: reads in-process dict.
        # SPIKE MARKETS: Hurst pipeline is bypassed — their edge is not Hurst-based
        # and Hurst penalties/boosts cross-contaminate the SMC+FVG signal path.
        hurst_delta = 0.0
        effective_min_score = self._settings.min_score

        if not _is_spike and hurst is not None and math.isfinite(hurst):
            bucket_stats = HurstCalibrator.get_bucket_stats(hurst)

            # Rule 1: penalise low-edge bucket (win_rate < 45%)
            if bucket_stats and int(bucket_stats.get("trades", 0)) >= 5:
                wr = float(bucket_stats.get("win_rate", 0.5))
                if wr < 0.45:
                    hurst_delta = -3.0
                    snap.reasons.append(
                        f"hurst_bucket_penalty: H={hurst:.3f} bucket WR={wr:.0%} < 45%"
                    )

            # Rule 2: lower threshold for high-persistence + confirmed edge
            if hurst > 0.62 and bucket_stats and int(bucket_stats.get("trades", 0)) >= 5:
                wr = float(bucket_stats.get("win_rate", 0))
                if wr > 0.60:
                    effective_min_score = min(effective_min_score, 7.0)
                    snap.reasons.append(
                        f"hurst_high_persist_boost: H={hurst:.3f} WR={wr:.0%} → min_score→7.0"
                    )

        score = max(0.0, min(10.0, score + hurst_delta))
        snap.hurst_score_delta = round(hurst_delta, 2)
        snap.effective_min_score = round(effective_min_score, 2)

        # ── Per-symbol min_score calibration (regime-aware) ─────────────────
        # In CALM regime each symbol family gets a dedicated lower floor because
        # composite scores naturally deflate (ATR, momentum, trend all compress)
        # while the institutional setup can still be structurally sound.
        # BOOM/CRASH: DIRECT assignment (not min()) — enforce the env-var value
        # unconditionally so no stale 7.5 remnant can override calm behaviour.
        # Structural gate (EMA200_SPIKE / FVG) still enforced downstream.
        _su_sym = symbol.upper()
        _is_boom_crash_sym = "BOOM" in _su_sym or "CRASH" in _su_sym
        _calm_bc_floor_used: float | None = None
        if _is_boom_crash_sym:
            if regime == "calm":
                # REGIME_SCORE_GATE_calm: determine floor based on current score.
                # Grade C territory (score < GRADE_B threshold = 6.20) → floor = 5.75
                # to allow DPM aggressive_trailing execution (micro-stake, tight ratchet).
                # Grade B/A territory (score >= 6.20) → floor = max(env, 6.00) as before.
                # 'not high_spread' guard: only drop to 5.75 when spread is < 75% of veto.
                _grade_b_th_local = float(os.getenv("DERIV_GRADE_B_SCORE", "6.20"))
                _high_spread = spread_pct > self._settings.max_spread_pct * 0.75
                _allow_grade_c_floor = score < _grade_b_th_local and not _high_spread
                if _allow_grade_c_floor:
                    # Phase 17: default lowered 5.25→4.50, floor lowered 5.25→4.00.
                    # Phase 32: 4.50→3.50 / floor 4.00→3.00.
                    # Diagnosis: BOOM1000 real scores 4.09-4.35 in calm = 100% ENTRY_BLOCKED
                    # with old floor=4.00+env=4.50. All BOOM/CRASH blocked at this threshold.
                    # CRASH entries also self-gated by geo — floor lowered to generate data.
                    _bc_calm_floor = max(
                        float(os.getenv("DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN", "3.50")),
                        3.00,   # absolute safety floor — never below 3.00
                    )
                else:
                    _bc_calm_floor = max(
                        float(os.getenv("DERIV_CALM_STRUCTURAL_MIN_SCORE", "5.80")),
                        6.00,   # hard cap for normal B/A: never require more than 6.00
                    )
                effective_min_score = _bc_calm_floor   # direct override — no min()
                _calm_bc_floor_used = _bc_calm_floor
                _LOGGER.info(
                    "[PIPELINE] [REGIME_SCORE_GATE_calm] %s regime=calm "
                    "effective_min=%.2f (grade_c=%s high_spread=%s) "
                    "atr_bypassed=%s",
                    symbol, effective_min_score, _allow_grade_c_floor,
                    _high_spread, _atr_calm_bypassed,
                )
            else:
                effective_min_score = min(effective_min_score, 5.8)  # spike edge ≠ trend edge
        elif not _is_boom_crash_sym:
            # ── R_* and other non-spike symbols: Hurst-based dynamic effective_min ──
            # Dynamic scaling: the Hurst regime directly informs the confidence bar.
            # H < 0.44  → strong mean-reversion → lower bar (fade edge is clear)
            # 0.46-0.54 → transitional / neutral → moderate bar
            # H > 0.56  → confirmed trend       → slightly higher bar (trend needs more confirmation)
            # No Hurst  → conservative fallback
            if hurst is not None and math.isfinite(hurst):
                if hurst < 0.44:
                    _hurst_eff_min = 5.50   # strong mean-rev: lowest bar
                elif hurst <= 0.54:
                    _hurst_eff_min = 5.75   # neutral/transitional zone
                elif hurst <= 0.56:
                    _hurst_eff_min = 5.80   # 0.54-0.56 transition band
                else:
                    _hurst_eff_min = 6.10   # strong trend: requires more conviction
            else:
                _hurst_eff_min = 5.80  # no Hurst data — conservative fallback
            effective_min_score = min(effective_min_score, _hurst_eff_min)
            snap.score_breakdown["hurst_eff_min"] = _hurst_eff_min
        snap.effective_min_score = round(effective_min_score, 2)
        _LOGGER.debug(
            "[EFFECTIVE_MIN_CALC] %s regime=%s profile_min=%.2f settings_min=%.2f "
            "→ effective_min=%.2f (calm_floor=%s)",
            symbol, regime,
            float(_get_asset_profile(symbol).get("min_score", 0.0)),
            self._settings.min_score,
            effective_min_score,
            f"{_calm_bc_floor_used:.2f}" if _calm_bc_floor_used is not None else "n/a",
        )

        # ── Mean-reversion dual router ────────────────────────────────────
        # When H < 0.45 (mean_reverting / ranging), the market oscillates around
        # a mean.  The classical trend detector produces ambiguous signals because
        # there is NO macro trend to detect — that is the point.  Instead we:
        #   1. Resolve direction from the SHORT-window mean-reversion spike:
        #      if recent price > short-term mean  → fade UP  → MULTDOWN
        #      if recent price < short-term mean  → fade DOWN → MULTUP
        #   2. Apply +3.0 score bonus to compensate for absent trend factors.
        #   3. Override effective_min_score to 6.0 so the bonus can push through.
        # CALIBRATION SAMPLING: Also route CALM regime through mean-rev when Hurst
        # supports it (CALM 0.65x size is already enforced in _regime_mult).
        # R_100/R_25/R_50 allow mean-reversion up to hurst_max=0.51.
        # SPIKE MARKETS: mean_rev_mode is always False — direction comes from
        # forced_side / SMC alone; mean-rev routing produces cross-contamination.
        _r_star = not _is_spike_market(symbol)
        if _is_spike:
            _mean_rev_mode = False
            # Resolve direction from the mandatory spike side when trend is ambiguous
            if trend_dir is None:
                _fs = _spike_forced_side(symbol)
                if _fs:
                    trend_dir = 1 if _fs == "MULTUP" else -1
                    snap.reasons.append(f"spike_forced_dir: {_fs} (Hurst/trend bypassed)")
        else:
            _hurst_mean_rev_ok = (
                hurst is not None
                and (
                    hurst < 0.45
                    or (_r_star and hurst <= 0.51)  # R_* mean-rev: hurst_max=0.51
                )
            )
            _mean_rev_mode = (
                regime in ("ranging", "mean_reverting")
                or _hurst_mean_rev_ok
                or (regime == "calm" and hurst is not None and hurst < 0.51)  # CALM: allow if Hurst favors
            )
        if _mean_rev_mode and trend_dir is None:
            # Direction: fade the micro-deviation from the 20-tick mean
            window_mr = ticks[-20:] if len(ticks) >= 20 else ticks
            mean_mr = sum(window_mr) / len(window_mr)
            if ticks[-1] > mean_mr:
                mr_dir = -1   # price above mean → fade → MULTDOWN
            else:
                mr_dir = +1   # price below mean → fade → MULTUP
            trend_dir = mr_dir
            # Score boost: compensates missing trend/momentum factors
            mr_bonus = 3.0
            score = max(0.0, min(10.0, score + mr_bonus))
            hurst_delta += mr_bonus
            effective_min_score = min(effective_min_score, 6.0)
            snap.reasons.append(
                f"mean_rev_spike: H={hurst:.3f} regime={regime} "
                f"dev={ticks[-1]-mean_mr:+.5f} → fade score+{mr_bonus}"
            )

        # Derive setup_type from active strategy signals for telemetry
        _setup_type = "TREND"
        if _mean_rev_mode:
            _setup_type = "MEAN_REV"

        # ── Higher-Direction (HD) bonus — multi-timeframe macro alignment ──────
        # Preferred source: PostgreSQL macro slope (3000-5000 tick equivalent).
        # Fallback: in-memory 500-tick OLS slope.
        # Applied AFTER trend_dir is fully resolved; BEFORE score_breakdown is built.
        _macro_slope = MacroHDCalibrator.get_slope(symbol)
        _hd_bonus = self._higher_direction_bonus(ticks, trend_dir, macro_slope_pct=_macro_slope)
        # FIX-9: Spike market HD neutralization
        # During accumulation, CRASH markets drift UP (opposing MULTDOWN = trade_dir=-1),
        # and BOOM markets may drift DOWN (opposing MULTUP).  This is structurally CORRECT
        # and normal — macro slope opposes trade direction BY DESIGN during accumulation.
        # A mild opposing slope should be neutral (0.0), not penalising (-0.5).
        # Only a STRONG opposing slope (>5× flat threshold) warrants the penalty.
        if _is_spike and _hd_bonus < 0.0:
            _spike_slope_abs = abs(_macro_slope) if _macro_slope is not None else 0.0
            if _spike_slope_abs < _MACRO_HD_FLAT_THRESHOLD * 5.0:
                _hd_bonus = 0.0  # mild opposing slope = neutral for spike accumulation
        # FIX (2026-05-18 quant audit): For CRASH spike markets, positive hd_bonus means
        # macro slope is ALREADY DOWN — the accumulation phase is over or the spike
        # already happened. Quant data: CRASH500 WIN avg_score=5.41 (no hd_bonus) vs
        # LOSS avg_score=8.85 (hd_bonus=+2.0). Grade A entries (hd_bonus inflated) had
        # 12.1% WR (-$7.78 total). Zero out positive hd_bonus for all CRASH symbols
        # so score reflects pure microstructure, not a lagging macro confirmation.
        if _is_spike and _hd_bonus > 0.0 and "CRASH" in symbol.upper():
            _hd_bonus = 0.0  # macro confirming CRASH = already falling = bad entry timing
        if _hd_bonus != 0.0:
            score = max(0.0, min(10.0, score + _hd_bonus))
            if _hd_bonus > 0:
                snap.reasons.append(
                    f"hd_aligned: macro_500t slope confirms {trend_dir:+d} → +{_hd_bonus:.1f}"
                )
            else:
                snap.reasons.append(
                    f"hd_opposed: macro_500t slope contradicts {trend_dir:+d} → {_hd_bonus:.1f}"
                )

        snap.score_breakdown = {
            "trend": round(trend_score, 2),
            "momentum": round(momentum_score, 2),
            "spread": round(spread_score, 2),
            "atr": round(atr_score, 2),
            "stability": round(stability_score, 2),
            "streak_penalty": round(streak_penalty, 2),
            "cooldown": round(cooldown_bonus, 2),
            "headroom": round(headroom_bonus, 2),
            "hd_bonus": round(_hd_bonus, 2),
            "regime": regime,
            "hurst_delta": round(hurst_delta, 2),
            "hurst": round(hurst, 3) if (hurst is not None and math.isfinite(hurst)) else 0.5,
            "effective_min_score": round(effective_min_score, 2),
            "mean_rev_mode": _mean_rev_mode,
            # ── Spike-family diagnostics (Bloque 7) ────────────────────────
            # Allows post-trade analysis to split 600/900/300 vs 1000/500.
            "spike_family_interval": _spike_interval_ticks(symbol) if _is_spike else 0,
            "spike_family": _get_asset_profile(symbol).get("spike_family", ""),
            # ── Telemetry (CALIBRATION SAMPLING MODE) ──────────────────────
            "setup_type": _setup_type,  # overridden below if SMC/spike/scalp fires
            "symbol": symbol,
            "atr_abs": round(atr, 8),
            "atr_pct": round(atr / ticks[-1], 6) if ticks[-1] > 0 else 0.0,
            # ── Calm-regime bypass telemetry ────────────────────────────────
            "atr_calm_bypassed": _atr_calm_bypassed,
            "calm_bc_floor": _calm_bc_floor_used,
        }
        snap.score = round(score, 3)
        snap.effective_min_score = round(effective_min_score, 2)
        snap.hurst_score_delta = round(hurst_delta, 2)

        # ── min_hd_bonus gate (per-profile, spike markets) ─────────────────────
        # Phase 28: CRASH600/BOOM900 require min_hd_bonus=2.0 to enter.
        # Data: hd=+2 → 40% spike rate +$0.13/trade; hd<2 → 20% spike rate (negative).
        _min_hd_req = _get_asset_profile(symbol).get("min_hd_bonus", 0.0)
        if _min_hd_req > 0.0 and _hd_bonus < _min_hd_req:
            snap.reasons.append(
                f"min_hd_bonus_veto: hd={_hd_bonus:.1f} < required={_min_hd_req:.1f}"
            )
            snap.allowed = False
            return snap

        # Ambiguous trend: CALIBRATION SAMPLING — downgrade to soft penalty instead
        # of hard block. Apply score penalty and 20% size reduction; attempt
        # mean-rev direction resolution. Only hard-block if geo conflict is severe
        # AND score still < effective_min_score after resolution attempt.
        _ambiguous_trend_penalty = False
        if trend_dir is None:
            # Try to resolve direction from mean-reversion (20-tick mean fade)
            window_amb = ticks[-20:] if len(ticks) >= 20 else ticks
            mean_amb = sum(window_amb) / len(window_amb)
            if ticks[-1] > mean_amb:
                trend_dir = -1   # price above mean → MULTDOWN
            else:
                trend_dir = +1   # price below mean → MULTUP
            # Apply calibration penalty: -1.0 score, 20% size reduction
            score = max(0.0, score - 1.0)
            _ambiguous_trend_penalty = True
            snap.reasons.append(
                f"ambiguous_trend_penalty: dir resolved via 20t-mean dev={ticks[-1]-mean_amb:+.5f} "
                f"→ score-1.0, size-20%"
            )
            snap.score_breakdown["ambiguous_trend_penalty"] = True
            snap.score = round(score, 3)

        side = "MULTUP" if trend_dir > 0 else "MULTDOWN"
        snap.side = side

        # ── side_allowed gate (per-profile, R_* only) ─────────────────────────
        # Phase 28: R_100 side_allowed=["MULTDOWN"] — MULTUP WR=27% destroyed -$6.56 PnL
        # in 100-trade sample. MULTDOWN WR=73% with +$0.66. No MULTUP under any condition.
        _side_allowed = _get_asset_profile(symbol).get("side_allowed")
        if _side_allowed and side not in _side_allowed:
            snap.reasons.append(
                f"side_allowed_veto: side={side} not in {_side_allowed}"
            )
            snap.allowed = False
            return snap

        # ── Hard Random-Walk veto (R_* indices only) ──────────────────────
        # When Hurst sits inside [0.45, 0.55] the price series is statistically
        # indistinguishable from a random walk — there is no edge to exploit
        # and the spread will systematically eat the position. This veto blocks
        # ALL entries on volatility indices in that band. Spike markets
        # (BOOM/CRASH) are exempt because their edge is the spike asymmetry,
        # not Hurst-based trend.
        _rw_lo = float(os.getenv("DERIV_RANDOM_WALK_LO", "0.45"))
        _rw_hi = float(os.getenv("DERIV_RANDOM_WALK_HI", "0.55"))
        # Neutral zone [0.47, 0.53]: no hard block — apply -0.5 penalty and tag
        # regime=neutral, then continue with structural processing (macro/SMC/geo).
        # Outer edges [0.45, 0.47) and (0.53, 0.55] remain a hard veto unless
        # the z-score bypass (deep mean-reversion) is active.
        _neutral_lo = float(os.getenv("DERIV_NEUTRAL_ZONE_LO", "0.47"))
        _neutral_hi = float(os.getenv("DERIV_NEUTRAL_ZONE_HI", "0.53"))
        if (
            hurst is not None
            and _rw_lo <= hurst <= _rw_hi
            and not _is_spike_market(symbol)
        ):
            # CALIBRATION: bypass veto for deep mean-reversion setups.
            # A random walk WITHOUT trend is the ideal environment for fade trades
            # when price is sufficiently displaced (|z-score| >= 2.0 from 30t mean).
            _z_window = ticks[-30:] if len(ticks) >= 30 else ticks
            _z_mean = sum(_z_window) / len(_z_window)
            _z_std = (sum((x - _z_mean) ** 2 for x in _z_window) / len(_z_window)) ** 0.5
            _z_score = (ticks[-1] - _z_mean) / _z_std if _z_std > 0 else 0.0
            _bypass_rw = _mean_rev_mode and abs(_z_score) >= 2.0
            snap.score_breakdown["rw_z_score"] = round(_z_score, 2)
            if _bypass_rw:
                snap.reasons.append(
                    f"rw_veto_bypassed: H={hurst:.3f} MEAN_REV z={_z_score:+.2f} (|z|≥2.0) — deep reversion valid"
                )
            elif _neutral_lo <= hurst <= _neutral_hi:
                # ── NEUTRAL zone: no hard block, apply -0.5 confidence penalty ──
                # H in [0.47, 0.53] is the inner noise band — pseudo-random walk
                # but macro/SMC/geometry can still provide a structural edge.
                # Tag regime=neutral so effective_min is tuned lower (C3) and
                # the pipeline continues with full scoring.
                score = max(0.0, score - 0.5)
                snap.score = round(score, 3)
                regime = "neutral"
                snap.regime = "neutral"
                snap.score_breakdown["neutral_zone_penalty"] = -0.5
                snap.reasons.append(
                    f"neutral_zone: H={hurst:.3f} ∈ [{_neutral_lo:.2f},{_neutral_hi:.2f}] "
                    f"— noise penalty=-0.5 regime=neutral pipeline_continues"
                )
                _LOGGER.info(
                    "[NEUTRAL_ZONE] %s H=%.3f penalty=-0.5 regime=neutral "
                    "score_after=%.2f z=%.2f (structural processing continues)",
                    symbol, hurst, score, _z_score,
                )
            else:
                # Outer random-walk zone [0.45, 0.47) or (0.53, 0.55] — hard veto
                snap.reasons.append(
                    f"random_walk_veto: H={hurst:.3f} ∈ [{_rw_lo:.2f},{_rw_hi:.2f}] outer zone — no statistical edge"
                )
                snap.allowed = False
                return snap

        # ── Spike asymmetry veto (BOOM/CRASH hard rule) ───────────────────
        # BOOM indices only allow MULTUP; CRASH indices only allow MULTDOWN.
        # This is a deterministic hard gate — no score can override it.
        _spike_vetoed, _spike_reason = _spike_direction_veto(symbol, side)
        if _spike_vetoed:
            snap.reasons.append(_spike_reason)
            snap.allowed = False
            return snap

        # ── Multi-timeframe channel geometry (advisory, additive) ──────────
        # Vectorised in NumPy — adds/removes score based on price position
        # within the macro regression channel.  Wrapped in try/except so
        # any calculation error cannot crash the live pipeline.
        _geo = None
        try:
            # Spike markets use 15% FVG mitigation — spikes graze FVGs without
            # deep penetration. R_* use env-var default (50%).
            _fvg_mit_override = 0.15 if _is_spike else None
            _geo = _compute_geometry(ticks, hurst=hurst, fvg_mit_pct=_fvg_mit_override)
            if _geo.geo_score_delta != 0.0:
                score = float(np.clip(score + _geo.geo_score_delta, 0.0, 10.0))
                snap.reasons.append(f"geo: {_geo.geo_reason}")
            # If geometry proposes a direction that conflicts with risk-engine
            # side → apply a modest conviction penalty (not a hard veto)
            if _geo.geo_side is not None and _geo.geo_side != side:
                score = max(0.0, score - 1.0)
                snap.reasons.append(
                    f"geo_conflict: geo={_geo.geo_side} vs risk={side} → -1.0"
                )
            # Store telemetry
            if _geo.macro:
                ch_w = _geo.macro.upper - _geo.macro.lower
                snap.score_breakdown["geo_channel_pos"] = round(
                    (ticks[-1] - _geo.macro.mid) / (ch_w / 2.0)
                    if ch_w > 0 else 0.0, 3
                )
                snap.score_breakdown["geo_slope_pct"] = round(
                    _geo.macro.slope / ticks[-1] * 100.0, 5
                )
            snap.score_breakdown["geo_delta"] = round(_geo.geo_score_delta, 2)
            snap.score = round(score, 3)
        except Exception as _geo_exc:  # noqa: BLE001
            _LOGGER.debug("[deriv-risk] geometry skipped: %s", _geo_exc)

        # ── SMC: FVG mitigation + Momentum divergence (institutional confluence) ──
        # Spike markets enforce their direction (BOOM=bull only, CRASH=bear only).
        # For R_* indices we accept either direction as long as FVG_dir == DIV_dir.
        #
        # DATA FINDING (120 real trades, 2026-05-20):
        #   R_75 with FVG active → WR=12%  (net -$4.68)
        #   R_75 without FVG    → WR=54%  (winner)
        # For R_75: FVG signals RESISTANCE, not support. When the SMC engine
        # detects a mitigated FVG on R_75, the price is already reversing against
        # our direction. Neutralize the smc_bonus entirely for R_75 to avoid
        # inflating score on setups that statistically destroy capital.
        _r75_fvg_neutralize = (
            symbol.upper() == "R_75"
            and _geo is not None
            and _geo.smc_bonus > 0
        )
        if _r75_fvg_neutralize:
            snap.score_breakdown["fvg_neutralized_r75"] = True
            snap.score_breakdown["fvg_active"] = False   # suppress FVG for gate checks
            _LOGGER.info(
                "[deriv-risk] R_75 FVG neutralized: smc_bonus=%.2f suppressed (data: WR=12%% with FVG)",
                _geo.smc_bonus,
            )
        if _geo is not None and _geo.smc_side is not None and _geo.smc_bonus > 0 and not _r75_fvg_neutralize:
            _smc_dir_ok = True
            _su = symbol.upper()
            if "BOOM" in _su and _geo.smc_side != "MULTUP":
                _smc_dir_ok = False
            elif "CRASH" in _su and _geo.smc_side != "MULTDOWN":
                _smc_dir_ok = False
            if _smc_dir_ok:
                side = _geo.smc_side
                snap.side = side
                score = float(np.clip(score + _geo.smc_bonus, 0.0, 10.0))
                snap.reasons.append(_geo.smc_reason)
                snap.score_breakdown["strategy"] = "SMC_Liquidity_Inbound"
                snap.score_breakdown["fvg_active"] = True
                snap.score_breakdown["fvg_direction"] = _geo.fvg_direction
                snap.score_breakdown["fvg_top"] = round(_geo.fvg_top, 5)
                snap.score_breakdown["fvg_bottom"] = round(_geo.fvg_bottom, 5)
                snap.score_breakdown["fvg_mid"] = round(_geo.fvg_mid, 5)
                snap.score_breakdown["gap_mitigated"] = True
                snap.score_breakdown["divergence"] = _geo.divergence
                snap.score_breakdown["smc_bonus"] = round(_geo.smc_bonus, 2)
                snap.score_breakdown["smc_strength"] = round(_geo.smc_bonus, 2)  # telemetry alias
                snap.score_breakdown["setup_type"] = "SMC_FVG"
                snap.score = round(score, 3)
                _LOGGER.info(
                    "[deriv-risk] [SMC ENGINE] %s gap_mitigated dir=%s div=%s side=%s "
                    "+%.2f score=%.2f", symbol, _geo.fvg_direction,
                    _geo.divergence, side, _geo.smc_bonus, score,
                )

        # ── Micro-channel scalp trigger (Hurst<0.40 + 1.5σ band touch) ───────
        # When the market is strongly mean-reverting (H<0.40) we don't wait for
        # the macro 500-tick extreme: a touch of the inner 1.5σ band of the
        # 150-tick channel combined with the geo direction is enough to inject
        # a scalp order. Tightened from 0.45 to 0.40 to avoid the random-walk
        # band (0.45-0.55) where the spread eats the edge.
        if (
            _geo is not None
            and _geo.micro_band_signal is not None
            and hurst is not None
            and hurst < 0.40
            and not _is_spike_market(symbol)
        ):
            _scalp_bonus = 1.5
            _scalp_side = _geo.micro_band_signal
            # Only boost if the scalp side matches the geo bias (or geo neutral)
            if _geo.geo_side is None or _geo.geo_side == _scalp_side:
                if side != _scalp_side:
                    side = _scalp_side
                    snap.side = side
                score = float(np.clip(score + _scalp_bonus, 0.0, 10.0))
                snap.reasons.append(
                    f"micro_scalp: H={hurst:.3f}<0.45 + 1.5σ band touch ({_scalp_side}) "
                    f"+{_scalp_bonus}"
                )
                snap.score_breakdown["micro_scalp"] = True
                snap.score_breakdown["micro_scalp_side"] = _scalp_side
                snap.score_breakdown["setup_type"] = "MICRO_SCALP"
                snap.score = round(score, 3)
                snap.reasons[-1] = snap.reasons[-1].replace("H={:.3f}<0.45".format(hurst), f"H={hurst:.3f}<0.40")

        # ── EMA-200 Spike Hunter (BOOM/CRASH only) ────────────────────────
        # BOOM spikes up → enter when price dips 0.02–0.10% below EMA200 (support).
        # CRASH spikes down → enter when price rises 0.02–0.10% above EMA200 (resistance).
        # Score bonus decays linearly: max +3.5 at the inner edge, 0 at the outer edge.
        if _is_spike_market(symbol) and len(ticks) >= 200:
            try:
                _ema200_val = _ema200(list(ticks))
                if _ema200_val is not None and _ema200_val > 0:
                    _price = ticks[-1]
                    _dev = (_price - _ema200_val) / _ema200_val  # signed % deviation
                    _su = symbol.upper()
                    _sh_bonus = 0.0
                    if "BOOM" in _su and -0.0010 <= _dev <= -0.0002:
                        _sh_bonus = 3.5 * (1.0 - abs(_dev) / 0.0010)
                    elif "CRASH" in _su and 0.0002 <= _dev <= 0.0010:
                        _sh_bonus = 3.5 * (1.0 - _dev / 0.0010)

                    # ── Adverse-momentum deceleration (exhaustion filter) ─────────────
                    # Compare the average tick-delta of the last 5 ticks vs the prior 5.
                    # BOOM:  adverse direction is DOWN. Decel = recent avg_Δ > prior avg_Δ
                    #        (selling pace slowing → buyers absorbing → spike imminent).
                    # CRASH: adverse direction is UP.  Decel = recent avg_Δ < prior avg_Δ
                    #        (buying pace slowing → sellers absorbing → spike imminent).
                    # Confirmed deceleration → +20% on bonus (high-quality exhaustion).
                    # Adverse momentum still accelerating → halve bonus (poor timing).
                    if _sh_bonus > 0.0:
                        _rd = [ticks[-i] - ticks[-(i + 1)] for i in range(1, 6)]
                        _pd = [ticks[-(5 + i)] - ticks[-(6 + i)] for i in range(1, 6)]
                        _avg_r = sum(_rd) / 5.0
                        _avg_p = sum(_pd) / 5.0
                        _decel_ok = (_avg_r > _avg_p) if "BOOM" in _su else (_avg_r < _avg_p)
                        if _decel_ok:
                            _sh_bonus = round(_sh_bonus * 1.20, 2)
                            snap.score_breakdown["momentum_decel"] = True
                        else:
                            _sh_bonus = round(_sh_bonus * 0.50, 2)
                            snap.score_breakdown["momentum_decel"] = False
                            _LOGGER.debug(
                                "[deriv-risk] %s adverse_momentum_active: "
                                "avg_rec=%.6f avg_prv=%.6f — sh_bonus halved to %.2f",
                                symbol, _avg_r, _avg_p, _sh_bonus,
                            )

                    _decel_tag = (
                        "✓" if snap.score_breakdown.get("momentum_decel") is True
                        else "✗" if snap.score_breakdown.get("momentum_decel") is False
                        else "?"
                    )
                    if "BOOM" in _su and -0.0010 <= _dev <= -0.0002:
                        snap.reasons.append(
                            f"ema200_spike_hunter: BOOM dip={_dev*100:.4f}% "
                            f"ema={_ema200_val:.5f} decel={_decel_tag} +{_sh_bonus:.2f}"
                        )
                    elif "CRASH" in _su and 0.0002 <= _dev <= 0.0010:
                        snap.reasons.append(
                            f"ema200_spike_hunter: CRASH rise={_dev*100:.4f}% "
                            f"ema={_ema200_val:.5f} decel={_decel_tag} +{_sh_bonus:.2f}"
                        )
                    if _sh_bonus > 0.0:
                        score = float(np.clip(score + _sh_bonus, 0.0, 10.0))
                        # Tag as spike_entry so the execution layer applies spike_timeout.
                        # Non-spike entries (SMC, Hurst, micro-scalp) must NOT carry this.
                        snap.score_breakdown["spike_entry"] = True
                        snap.score_breakdown["setup_type"] = "EMA200_SPIKE"
                    snap.score_breakdown["ema200"] = round(_ema200_val, 5)
                    snap.score_breakdown["ema200_dev_pct"] = round(_dev * 100, 4)
                    snap.score = round(score, 3)
            except Exception as _sh_exc:  # noqa: BLE001
                _LOGGER.debug("[deriv-risk] ema200_spike_hunter skipped: %s", _sh_exc)

        # If AI confidence is low / missing AND the geometric stack agrees,
        # the mathematical microstructure signal is considered more reliable
        # than the qualitative LLM opinion.  This is the "Hard Math Override".
        # Triggers when:
        #   • AI returned no answer (timeout / 4xx / circuit-open) OR
        #   • AI returned confidence below the per-trade threshold
        # AND any of:
        #   (a) Hurst > 0.62 with autocorr aligned   (trend continuation)
        #   (b) SMC FVG-mitigation confluence active (geo.smc_side aligned)
        #   (c) micro-channel scalp with Hurst < 0.45 (mean-reversion fade)
        _ai_min_conf = float(os.getenv("DERIV_AI_MIN_CONFIDENCE", "0.70"))
        _allow_pre_ai_override = os.getenv(
            "DERIV_ALLOW_PRE_AI_MATH_OVERRIDE",
            "false",
        ).strip().lower() in ("1", "true", "yes", "on")
        snap.hurst_ai_override = False
        _ai_failed = (
            (ai_confidence is not None and ai_confidence < _ai_min_conf)
            or (ai_confidence is None and _allow_pre_ai_override)
        )
        if _ai_failed:
            _override_reason = ""
            # (d) Spike-active override: a real spike was JUST detected for this
            # symbol (within 90s) AND the escape valve fired (no FVG but live spike
            # momentum IS the structural confirmation). This allows CRASH900 841x /
            # CRASH600 305x type massive spikes to enter instead of being AI-vetoed.
            # Only fires when the last spike for THIS symbol was recent — not a
            # cross-symbol event — so we use per-symbol _last_spike_ts.
            _spike_active_bypass = float(os.getenv("DERIV_SPIKE_ACTIVE_OVERRIDE_SEC", "90"))
            _last_spike_for_sym = self._last_spike_ts.get(symbol, 0.0)
            _spike_just_fired = (
                _spike_active_bypass > 0
                and _last_spike_for_sym > 0
                and (time.time() - _last_spike_for_sym) <= _spike_active_bypass
            )
            _bc_escape_active = bool(snap.score_breakdown.get("bc_escape_env"))
            if (
                self._allow_spike_active_ai_override
                and _is_spike
                and _spike_just_fired
                and _bc_escape_active
                and not _override_reason
            ):
                _elapsed_spike = time.time() - _last_spike_for_sym
                _override_reason = (
                    f"spike_active_override: spike {_elapsed_spike:.0f}s ago "
                    f"+ bc_escape_env → bypass AI"
                )
            # (a) Hurst-trend confluence
            if not _override_reason and (
                hurst is not None and hurst > 0.62
                and autocorr_lag1 is not None
                and (
                    (trend_dir > 0 and autocorr_lag1 >  0.04) or
                    (trend_dir < 0 and autocorr_lag1 < -0.04)
                )
            ):
                _override_reason = (
                    f"trend_math: H={hurst:.3f} autocorr={autocorr_lag1:+.3f}"
                )
            # (b) SMC confluence
            elif not _override_reason and _geo is not None and _geo.smc_side is not None and _geo.smc_side == side:
                _override_reason = f"smc_confluence: {_geo.smc_reason}"
            # (c) Mean-reverting micro scalp — STRICTLY volatility-only.
            # BOOM/CRASH are spike-asymmetric and have no statistical band-touch
            # edge.  Allowing this branch for spike markets caused repeated
            # false overrides on BOOM1000/CRASH1000 that were later vetoed by
            # the structural gate (log pollution + wasted CPU).
            elif not _override_reason and (
                _geo is not None and _geo.micro_band_signal is not None
                and hurst is not None and hurst < 0.40
                and _geo.micro_band_signal == side
                and not _is_spike_market(symbol)
            ):
                _override_reason = f"micro_scalp_mr: H={hurst:.3f}<0.40 band_touch={side}"

            if _override_reason:
                snap.hurst_ai_override = True
                _ai_disp = "n/a" if ai_confidence is None else f"{ai_confidence:.2f}"
                snap.reasons.append(
                    f"hard_math_override: {_override_reason} "
                    f"(ai_conf={_ai_disp} < {_ai_min_conf}) — math > LLM"
                )
                # Demoted to DEBUG: the daemon emits an INFO ORDER log only when
                # the override actually executes (after cooldown + structural
                # gates).  Keeping this at INFO produced spam on every tick
                # where math fired but the trade was later blocked downstream.
                _LOGGER.debug(
                    "[deriv-risk] HARD_MATH_OVERRIDE %s side=%s reason=%s ai=%s",
                    symbol, side, _override_reason, _ai_disp,
                )

        # ── BOOM/CRASH FVG 4-tier gate (replaces hard structural veto) ──────
        # Graduated entry permission based on FVG quality.
        # Tier 0: No FVG at all → only allow if score is exceptional (profile_min + premium)
        # Tier 1: FVG detected but NOT yet mitigated → allow at normal effective_min
        # Tier 2: FVG mitigated, no EMA200 → smc_bonus already applied (+1.00 equivalent)
        # Tier 3: FVG mitigated + EMA200 spike-hunter → full smc_bonus (+3.00)
        #
        # Key change vs prior hard veto: a BOOM1000 with score=8.39 and FVG_DETECTED
        # (Tier 1) now passes instead of being vetoed. Only truly structureless entries
        # (Tier 0, no FVG at all) are hard-blocked unless score is exceptional.
        if _is_spike_market(symbol):
            _has_fvg_active    = bool(snap.score_breakdown.get("fvg_active"))
            _has_fvg_mitigated = bool(snap.score_breakdown.get("gap_mitigated"))
            _has_spike_hunter  = bool(snap.score_breakdown.get("spike_entry"))
            _profile_min       = float(_get_asset_profile(symbol).get("min_score", snap.effective_min_score))

            # ── FVG credit check and update ────────────────────────────────
            # When FVG is active NOW with score >= 7.0: issue/refresh a 5-cycle credit.
            # When FVG is absent but credit remains: treat as FVG active (fvg_credit window).
            # This fixes the race: CRASH600 had score=8.50+SMC in cycle K but
            # ATR failed → 3 cycles later ATR passes but SMC gone.  Credit bridges the gap.
            _FVG_CREDIT_CYCLES = int(os.getenv("DERIV_FVG_CREDIT_CYCLES", "5"))
            _FVG_CREDIT_SCORE_MIN = float(os.getenv("DERIV_FVG_CREDIT_SCORE_MIN", "7.0"))
            _su_credit = symbol.upper()
            _credit = self._fvg_credit.get(_su_credit)
            if _has_fvg_active and snap.score >= _FVG_CREDIT_SCORE_MIN:
                # Issue or refresh credit
                self._fvg_credit[_su_credit] = {
                    "cycles_remaining": _FVG_CREDIT_CYCLES,
                    "score_at_detection": snap.score,
                    "was_mitigated": bool(_has_fvg_mitigated),
                }
                _LOGGER.info(
                    "[FVG_CREDIT] %s ISSUED cycles=%d score_at_detection=%.2f "
                    "mitigated=%s (fvg_active=True score>=%.1f)",
                    symbol, _FVG_CREDIT_CYCLES, snap.score,
                    bool(_has_fvg_mitigated), _FVG_CREDIT_SCORE_MIN,
                )
            elif not _has_fvg_active and _credit and _credit.get("cycles_remaining", 0) > 0:
                # Consume credit cycle — preserve the last observed FVG tier.
                _credit["cycles_remaining"] -= 1
                _has_fvg_active = True
                _has_fvg_mitigated = bool(_credit.get("was_mitigated", False))
                _LOGGER.info(
                    "[FVG_CREDIT] %s ACTIVE cycles_remaining=%d score_at_detection=%.2f "
                    "mitigated=%s — bridging ATR race (fvg_active reinstated for this cycle)",
                    symbol, _credit["cycles_remaining"],
                    _credit.get("score_at_detection", 0),
                    _has_fvg_mitigated,
                )
                snap.score_breakdown["fvg_credit_active"] = True
                snap.score_breakdown["fvg_credit_cycles"] = _credit["cycles_remaining"]
                snap.score_breakdown["fvg_credit_was_mitigated"] = _has_fvg_mitigated
            elif _credit and _credit.get("cycles_remaining", 0) <= 0:
                # Credit expired — remove
                del self._fvg_credit[_su_credit]

            _dyn_cfg = dynamic_cfg if isinstance(dynamic_cfg, dict) else {}
            _dyn_is_active = bool(_dyn_cfg.get("is_active", False))
            _dyn_regime = str(_dyn_cfg.get("market_regime") or "NORMAL").upper()
            _dyn_relax_enabled = (
                os.getenv("DERIV_DYNAMIC_STRUCTURAL_RELAX_ENABLED", "true").strip().lower()
                in ("1", "true", "yes", "on")
            )
            _dyn_relax_symbols = {
                s.strip().upper()
                for s in os.getenv(
                    "DERIV_DYNAMIC_STRUCTURAL_RELAX_SYMBOLS",
                    "BOOM600,BOOM900,CRASH600,CRASH900",
                ).split(",")
                if s.strip()
            }
            _dyn_relax_regimes = {
                s.strip().upper()
                for s in os.getenv(
                    "DERIV_DYNAMIC_STRUCTURAL_RELAX_REGIMES",
                    "FAST,NORMAL",
                ).split(",")
                if s.strip()
            }
            _dyn_relax_score_margin = float(
                os.getenv("DERIV_DYNAMIC_STRUCTURAL_RELAX_SCORE_MARGIN", "0.40") or 0.40
            )
            _dyn_relax_no_fvg_penalty = float(
                os.getenv("DERIV_DYNAMIC_STRUCTURAL_NO_FVG_PENALTY", "1.10") or 1.10
            )
            _dyn_relax_tier1_penalty = float(
                os.getenv("DERIV_DYNAMIC_STRUCTURAL_TIER1_PENALTY", "0.75") or 0.75
            )
            _dyn_relax_structural = (
                _dyn_relax_enabled
                and _dyn_is_active
                and symbol.upper() in _dyn_relax_symbols
                and _dyn_regime in _dyn_relax_regimes
                and score >= (effective_min_score + _dyn_relax_score_margin)
            )

            if not _has_fvg_active and not _has_spike_hunter:
                # ── Tier 0: No FVG detected, no EMA200 spike-hunter ──────────
                # FIX-10: Hard structural veto — no institutional confirmation.
                # Empirical evidence: 75%+ losses on BOOM/CRASH entries without
                # FVG or EMA200 spike-hunter structural setup.
                # Exception: DERIV_BOOM_CRASH_ESCAPE_VALVE=true (debug/research only).
                _bc_escape_env = (
                    os.getenv("DERIV_BOOM_CRASH_ESCAPE_VALVE", "").lower()
                    in ("1", "true", "yes")
                )
                # Per-profile override: block_bc_escape_env=True → hard veto even if escape valve open.
                # Muestra02: BOOM600 bc_escape_env 32t → WR=25%, PnL=-$3.15; score does NOT predict.
                _profile_blocks_bc = bool(_get_asset_profile(symbol).get("block_bc_escape_env", False))
                _bc_env_key = f"DERIV_BLOCK_BC_ESCAPE_{str(symbol).upper()}"
                _bc_env_raw = os.getenv(_bc_env_key, "").strip().lower()
                if _bc_env_raw in ("1", "true", "yes"):
                    _profile_blocks_bc = True
                elif _bc_env_raw in ("0", "false", "no"):
                    _profile_blocks_bc = False
                if not _bc_escape_env or _profile_blocks_bc:
                    if _dyn_relax_structural:
                        score = max(0.0, score - _dyn_relax_no_fvg_penalty)
                        snap.score = round(score, 3)
                        snap.score_breakdown["fvg_tier"] = "dynamic_soft_veto_no_fvg"
                        snap.score_breakdown["dynamic_structural_relax"] = True
                        snap.score_breakdown["dynamic_structural_penalty"] = round(
                            _dyn_relax_no_fvg_penalty, 2
                        )
                        snap.reasons.append(
                            f"dynamic_structural_relax_no_fvg: regime={_dyn_regime} "
                            f"penalty={_dyn_relax_no_fvg_penalty:.2f} score_after={score:.2f}"
                        )
                        _LOGGER.info(
                            "[STRUCTURAL_RELAX_DYNAMIC] %s no_fvg_hard_veto→soft "
                            "regime=%s penalty=%.2f score_after=%.2f",
                            symbol,
                            _dyn_regime,
                            _dyn_relax_no_fvg_penalty,
                            score,
                        )
                    else:
                        _block_reason = (
                            f"boom_crash_bc_escape_blocked_by_profile: {symbol} no active FVG + block_bc_escape_env=True"
                            if _profile_blocks_bc else
                            f"boom_crash_structural_veto: {symbol} no active FVG + no EMA200 spike-hunter → hard veto"
                        )
                        snap.score_breakdown["fvg_tier"] = "no_fvg_hard_veto"
                        snap.reasons.append(_block_reason)
                        snap.allowed = False
                        return snap
                else:
                    # Debug escape valve: apply mild penalty instead of hard block.
                    _no_fvg_penalty = float(
                        os.getenv("DERIV_BOOM_CRASH_NO_FVG_PENALTY", "0.20")
                    )
                    score = max(0.0, score - _no_fvg_penalty)
                    snap.score = round(score, 3)
                    snap.score_breakdown["fvg_tier"] = "bc_escape_env"
                    snap.score_breakdown["bc_escape_env"] = True
                    snap.score_breakdown["no_fvg_penalty"] = round(_no_fvg_penalty, 2)
                    snap.reasons.append(
                        f"no_fvg_escape_env: penalty={_no_fvg_penalty:.2f} score_after={score:.2f}"
                    )
                    _LOGGER.info(
                        "[STRUCTURAL_VETO_ESCAPE] %s bc_escape_env penalty=%.2f "
                        "score_after=%.2f (effective_min=%.2f)",
                        symbol, _no_fvg_penalty, score, snap.effective_min_score,
                    )
            elif _has_fvg_active and not _has_fvg_mitigated and not _has_spike_hunter:
                # ── Tier 1: FVG detected but not yet mitigated ───────────────
                # Check if the profile requires mitigated FVG (e.g. BOOM600/CRASH600).
                _fvg_tier_min = _get_asset_profile(symbol).get("fvg_tier_minimo", "fvg_detected")
                if _fvg_tier_min == "fvg_mitigated":
                    if _dyn_relax_structural:
                        score = max(0.0, score - _dyn_relax_tier1_penalty)
                        snap.score = round(score, 3)
                        snap.score_breakdown["fvg_tier"] = "dynamic_soft_veto_tier1"
                        snap.score_breakdown["dynamic_structural_relax"] = True
                        snap.score_breakdown["dynamic_structural_penalty"] = round(
                            _dyn_relax_tier1_penalty, 2
                        )
                        snap.reasons.append(
                            f"dynamic_structural_relax_tier1: regime={_dyn_regime} "
                            f"penalty={_dyn_relax_tier1_penalty:.2f} score_after={score:.2f}"
                        )
                        _LOGGER.info(
                            "[STRUCTURAL_RELAX_DYNAMIC] %s tier1_fvg_detected→soft "
                            "regime=%s penalty=%.2f score_after=%.2f",
                            symbol,
                            _dyn_regime,
                            _dyn_relax_tier1_penalty,
                            score,
                        )
                    else:
                        snap.score_breakdown["fvg_tier"] = "fvg_detected_insufficient"
                        snap.reasons.append(
                            f"boom_crash_structural_veto: {symbol} requires fvg_tier=fvg_mitigated, "
                            f"only fvg_detected active — awaiting mitigation"
                        )
                        snap.allowed = False
                        return snap
                # Tier 1 OK — allow at normal effective_min (no extra SMC bonus).
                snap.score_breakdown["fvg_tier"] = "fvg_detected"
                _LOGGER.debug(
                    "[PIPELINE] FVG_TIER1 %s score=%.2f — FVG detected, not mitigated "
                    "— allowing at effective_min=%.2f",
                    symbol, snap.score, snap.effective_min_score,
                )
            elif _has_fvg_mitigated and not _has_spike_hunter:
                # ── Tier 2: FVG mitigated (smc_bonus already applied ~+1.0 to +3.0) ──
                snap.score_breakdown["fvg_tier"] = "fvg_mitigated"
            else:
                # ── Tier 3: EMA200 spike-hunter active (full confluence) ──────
                snap.score_breakdown["fvg_tier"] = "fvg_full_confluence"

        # ── Spike-cycle gate resolution ────────────────────────────────────
        # Now that the full scoring pipeline has run (EMA200 hunter + SMC FVG)
        # we know the setup_type and whether an aligned FVG is active.
        # Two possible outcomes per symbol:
        #   A) STRUCTURAL_OVERRIDE: setup is EMA200_SPIKE + aligned FVG active
        #      → bypass the timer; the institutional confluence math > time.
        #   B) No override: emit the SPIKE_CYCLE_GATE veto and return.
        #
        # FVG direction alignment is evaluated per-symbol:
        #   BOOM  (forced_side=MULTUP)   → FVG must be bullish  ("bull")
        #   CRASH (forced_side=MULTDOWN) → FVG must be bearish  ("bear")
        # This prevents a bullish FVG from clearing a CRASH spike gate and
        # vice-versa — the engine must understand which index it is evaluating.
        if _spike_gate_active:
            _su_gate = symbol.upper()
            _override_enabled = os.getenv(
                "DERIV_STRUCTURAL_OVERRIDE_ENABLED", "true"
            ).lower() in ("1", "true", "yes")
            _setup_now  = snap.score_breakdown.get("setup_type", "")
            _fvg_active = bool(snap.score_breakdown.get("fvg_active"))
            _fvg_dir    = snap.score_breakdown.get("fvg_direction", "")
            # Per-symbol FVG direction check (BOOM=bull, CRASH=bear)
            # FIX: fvg_direction is stored as "bullish"/"bearish" — accept both forms
            _fvg_aligned = (
                ("BOOM"  in _su_gate and _fvg_dir in ("bull", "bullish")) or
                ("CRASH" in _su_gate and _fvg_dir in ("bear", "bearish"))
            )
            _structural_ok = (
                _override_enabled
                and _setup_now == "EMA200_SPIKE"
                and _fvg_active
                and _fvg_aligned
            )
            if _structural_ok:
                _LOGGER.info(
                    "[PIPELINE] [STRUCTURAL_OVERRIDE] Symbol: %s | "
                    "Reason: Spike gate bypassed by structural confluence "
                    "(EMA200_SPIKE + aligned_FVG=%s elapsed=%.0fs/%.0fs)",
                    symbol, _fvg_dir, _spike_gate_elapsed, _spike_gate_min_wait,
                )
                snap.score_breakdown["spike_gate_override"]  = True
                snap.score_breakdown["spike_gate_elapsed_s"] = round(_spike_gate_elapsed, 1)
            else:
                _frac_used = float(os.getenv("DERIV_SPIKE_CYCLE_FRAC", "0.08"))
                snap.reasons.append(
                    f"SPIKE_CYCLE_GATE: {symbol} last_spike={_spike_gate_elapsed:.0f}s ago "
                    f"< {_spike_gate_min_wait:.0f}s "
                    f"({_frac_used*100:.0f}% of {_spike_gate_interval}s expected)"
                )
                _LOGGER.debug(
                    "[SPIKE_CYCLE_GATE] %s vetoed: spike %.0fs ago need %.0fs "
                    "(override_enabled=%s setup=%s fvg_active=%s fvg_aligned=%s)",
                    symbol, _spike_gate_elapsed, _spike_gate_min_wait,
                    _override_enabled, _setup_now, _fvg_active, _fvg_aligned,
                )
                return snap

        # ── Calm-regime R_* mean-rev z-score boost ───────────────────────
        # In calm markets R_* oscillates around mean with high predictability.
        # Deep z-score displacement (±2.0σ) signals a fade-trade edge.
        # Uses NumPy for vectorised std; div-by-zero guarded at 1e-8.
        # Direction coherence enforced: bonus ONLY when trade direction aligns
        # with the fade (MULTDOWN if z≥2.0, MULTUP if z≤-2.0).
        if regime == "calm" and not _is_spike_market(symbol):
            _z_win: np.ndarray = np.asarray(
                ticks[-30:] if len(ticks) >= 30 else ticks, dtype=np.float64
            )
            _z_mean: float = float(_z_win.mean())
            _z_std: float  = float(_z_win.std())
            if _z_std > 1e-8:
                _z_calm: float = (ticks[-1] - _z_mean) / _z_std
            else:
                _z_calm = 0.0
            # Direction coherence: only boost when the score is for the fade side
            _z_fade_side: str | None = (
                "MULTDOWN" if _z_calm >= 2.0
                else ("MULTUP" if _z_calm <= -2.0 else None)
            )
            if _z_fade_side is not None and snap.side == _z_fade_side:
                score = float(np.clip(score + 3.0, 0.0, 10.0))
                snap.score = round(score, 3)
                snap.score_breakdown["calm_mr_z_bonus"] = round(_z_calm, 2)
                snap.reasons.append(
                    f"calm_mr_z_boost: regime=calm z={_z_calm:+.2f} "
                    f"fade={_z_fade_side} (coherent) +3.0"
                )
            elif _z_fade_side is not None:
                # Deep displacement but direction conflict — log and skip
                _LOGGER.debug(
                    "[calm_mr_z] %s z=%.2f fade=%s side=%s — direction conflict, no bonus",
                    symbol, _z_calm, _z_fade_side, snap.side,
                )

        # ── Discriminatory score compression (tanh soft-cap) ─────────────────
        # Bonuses from SMC, spike-hunter, mean-rev, and scalp can stack to push
        # the raw score above 10 where it clips and loses discriminatory power.
        # A tanh above the hinge (default 7.5) compresses the surplus smoothly:
        #   raw 8.0  → 7.99  (negligible change near hinge)
        #   raw 9.0  → 8.79
        #   raw 10.0 → 9.27
        #   raw 12.0 → 9.75
        #   raw 15+  → 9.9   (ceiling — practically unreachable)
        # Score=10 now requires ~15 raw points of aligned confluence.
        # Parameters are env-var tunable for live calibration.
        _sc_hinge = float(os.getenv("DERIV_SCORE_SOFTCAP_HINGE", "7.5"))
        _sc_range = float(os.getenv("DERIV_SCORE_SOFTCAP_RANGE", "2.4"))
        _raw_score = score  # pre-compression for telemetry
        if score > _sc_hinge and _sc_range > 0:
            score = _sc_hinge + _sc_range * math.tanh((score - _sc_hinge) / _sc_range)
        score = round(score, 3)
        snap.score = score
        snap.score_breakdown["score_raw"] = round(_raw_score, 3)

        # Sizing — proportional to bankroll, regime-adjusted, score-proportional.
        risk_usdt = self._settings.bankroll_usdt * self._settings.risk_per_trade_pct
        # Reduce risk by the loss streak (mild de-grossing — never increase).
        if self._loss_streak >= 1:
            risk_usdt *= max(0.5, 1.0 - 0.15 * self._loss_streak)
        # Ambiguous-trend penalty: reduce size by 20% when direction was inferred
        if _ambiguous_trend_penalty:
            risk_usdt *= 0.80
        # ── Regime → position size ─────────────────────────────────────────────
        # Each regime carries a different level of directional confidence.
        # PositionSize ∝ RegimeConfidence (not just entry gate).
        #   trending  — confirmed linear structure → full size
        #   ranging   — partial mean-rev edge     → -10 %
        #   volatile  — elevated realised vol     → -25 %
        #   calm      — low ATR, spread dominates → -35 %
        #   unknown   — insufficient ticks         → -20 %
        _regime_mult = {
            "trending": 1.00,
            "ranging":  0.90,
            "volatile": 0.75,
            "calm":     0.65,
            "unknown":  0.80,
        }.get(regime, 1.00)
        risk_usdt *= _regime_mult
        snap.score_breakdown["regime_size_mult"] = _regime_mult
        # ── Graded execution A / B / C — DPM hook ────────────────────────────
        # Replaces the ±20% linear size adjustment with three discrete grades
        # that map directly to the DPM's position sizing and trailing behaviour.
        #
        # Grade A (score >= 7.50): full stake (1.0×)  — high-confidence institutional
        # Grade B (6.20 <= s < 7.50): 60% stake (0.60×) — valid but moderate setup
        # Grade C (s < 6.20): micro stake (0.30×) + aggressive_trailing=True
        #   Grade C only reaches execution when effective_min_score <= 5.75
        #   (Hurst neutral/mean-rev path or per-symbol calibrated floor).
        # Thresholds are configurable via env vars for live tuning without redeploy.
        _grade_a_th = float(os.getenv("DERIV_GRADE_A_SCORE", "7.50"))
        _grade_b_th = float(os.getenv("DERIV_GRADE_B_SCORE", "6.20"))
        if score >= _grade_a_th:
            _exec_grade = "A"
            _score_sz = 1.00
        elif score >= _grade_b_th:
            _exec_grade = "B"
            _score_sz = 0.60
        else:
            _exec_grade = "C"
            _score_sz = 0.40  # FIX-6: 0.30→0.40 — too punishing; 0.30 makes min stakes unviable
            # DPM hook: aggressive_trailing=True tells DynamicPositionManager
            # to use tightest ratchet (smallest ratchet_step, highest ratchet_ratio)
            # ensuring any Grade C profit is protected immediately.
            snap.score_breakdown["aggressive_trailing"] = True
        risk_usdt *= _score_sz
        snap.score_breakdown["execution_grade"] = _exec_grade
        snap.score_breakdown["score_size_mult"] = round(_score_sz, 3)
        _LOGGER.info(
            "[EXEC_GRADE] %s grade=%s score=%.2f stake_mult=%.2f regime=%s",
            symbol, _exec_grade, score, _score_sz, regime,
        )
        # Hard floor at $1.00 (DERIV_MIN_STAKE_USDT_HARD overrides).
        # Hard cap at $5.00 (DERIV_MAX_STAKE_USDT overrides) — ABSOLUTE, unbreakable.
        # Prevents any regime/score multiplier from producing $30+ orders.
        _HARD_FLOOR_USDT: float = float(os.getenv("DERIV_MIN_STAKE_USDT_HARD", "1.00"))
        _HARD_CAP_USDT: float = float(os.getenv("DERIV_MAX_STAKE_USDT", "5.00"))
        min_stake = max(_HARD_FLOOR_USDT, float(self._settings.min_stake_usdt))
        stake = max(min_stake, min(risk_usdt, self._settings.bankroll_usdt * 0.25))
        stake = min(stake, _HARD_CAP_USDT)   # ABSOLUTE CAP — overrides all multipliers
        snap.suggested_stake_usdt = round(stake, 2)
        # Mark mean-rev trades in score_breakdown so pipeline can apply dynamic TP
        if _mean_rev_mode:
            snap.score_breakdown["mean_rev_mode"] = True
        snap.suggested_multiplier = self._suggest_multiplier(atr, ticks[-1], symbol)

        if score < effective_min_score:
            snap.reasons.append(
                f"score {score:.2f} < min {effective_min_score:.2f}"
            )
            return snap

        snap.allowed = True
        snap.reasons.append(f"GO — trend={side} score={score:.2f} regime={regime}")
        return snap

    def register_close(self, realized_pnl_usdt: float, symbol: str = "") -> None:
        """Update streak + daily DD after a contract closes.

        Streak logic (institutional refactor 2026-05-18):
          • significant win  (pnl > sl_typical × 0.30)  → reset streak to 0
          • loss              (pnl < 0)                  → increment streak
          • micro-win         (0 ≤ pnl ≤ threshold)      → preserve counter

        After every call the risk state (consecutive_losses, lockout_until,
        bankroll) is serialized to logs/deriv_lockout.json so that the
        protective state survives daemon/deploy restarts.
        """
        self._roll_daily_anchor_if_needed()
        self._daily_realized_pnl += realized_pnl_usdt
        self._last_trade_ts = time.time()
        # Also update per-symbol tracker so BOOM/CRASH cooldown_bonus is
        # independent of R_50/R_75 trade frequency.
        if symbol:
            self._last_trade_ts_per_symbol[symbol.upper()] = self._last_trade_ts

        # ── Significance threshold (anti-noise) ───────────────────────────
        # sl_typical: representative dollar-loss for a BOOM/CRASH contract.
        # win_significant_threshold = 30% of sl_typical. A win below this is
        # classified as a micro-win (noise) and does NOT reset the streak.
        _boom_crash_sl_pct = float(os.getenv("DERIV_BOOM_CRASH_SL_PCT", "0.60"))
        sl_typical = (
            self._settings.bankroll_usdt
            * self._settings.risk_per_trade_pct
            * _boom_crash_sl_pct
        )
        win_significant_threshold = max(0.0, sl_typical * 0.30)

        # PATCH 2026-05-25: auto-unlock on first win after lockout to break the
        # 12 h purgatory cycle. We trust the loss-streak engine as the primary
        # capital safety; if the bot recovers a trade, the lockout is no longer
        # needed. Daily-DD lockouts are separate and remain in force.
        _was_locked = self._read_lockout().locked
        _is_loss_streak_lockout = _was_locked and self._loss_streak > 0

        if realized_pnl_usdt > win_significant_threshold:
            if self._loss_streak > 0:
                _LOGGER.warning(
                    "[risk] significant win pnl=%.3f > thr=%.3f — streak reset (was %d)",
                    realized_pnl_usdt, win_significant_threshold, self._loss_streak,
                )
            self._loss_streak = 0
            self._consecutive_wins += 1
            if _is_loss_streak_lockout:
                self._clear_lockout("win_after_lockout")
        elif realized_pnl_usdt < 0:
            self._consecutive_wins = 0
            self._loss_streak += 1
            cap = self._settings.loss_streak_lockout
            if cap > 0 and self._loss_streak >= cap:
                self._engage_lockout(
                    f"{self._loss_streak} consecutive losses"
                )
        else:
            # 0 ≤ pnl ≤ threshold — micro-win
            # Counts towards consecutive_wins so 2 micro-wins also unlock.
            self._consecutive_wins += 1
            _LOGGER.info(
                "[risk] micro_win pnl=%.3f ≤ thr=%.3f — streak preserved (%d) wins=%d",
                realized_pnl_usdt, win_significant_threshold, self._loss_streak,
                self._consecutive_wins,
            )
            # Two consecutive wins (of any kind) also clear a stuck lockout.
            if _is_loss_streak_lockout and self._consecutive_wins >= 2:
                self._loss_streak = 0
                self._clear_lockout("two_consecutive_wins")

        # ── State serialization (persistence across restarts) ─────────────
        try:
            lf = self._settings.lockout_file
            lf.parent.mkdir(parents=True, exist_ok=True)
            # Read existing lockout state (if any) so we don't clobber it.
            existing: dict[str, Any] = {}
            if lf.exists():
                try:
                    existing = json.loads(lf.read_text()) or {}
                except Exception:  # noqa: BLE001
                    existing = {}
            existing.update({
                "consecutive_losses": int(self._loss_streak),
                "lockout_until": float(existing.get("until_ts", 0) or 0),
                "bankroll": float(self._settings.bankroll_usdt),
                "daily_realized_pnl": round(self._daily_realized_pnl, 4),
                "last_trade_ts": self._last_trade_ts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            tmp = lf.with_suffix(lf.suffix + ".tmp")
            tmp.write_text(json.dumps(existing, indent=2))
            tmp.replace(lf)
        except Exception as _ser_exc:  # noqa: BLE001
            _LOGGER.warning("[risk] state serialization failed: %s", _ser_exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal scoring helpers — v2 (smarter math)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _higher_direction_bonus(
        ticks: list[float],
        trade_dir: int | None,
        macro_slope_pct: float | None = None,
    ) -> float:
        """Multi-timeframe Higher Direction (HD) score.

        Uses the macro OLS slope to confirm (+1.5) or oppose (-0.5) the
        proposed trade direction.  Slope source priority:

        1. `macro_slope_pct` from MacroHDCalibrator (PostgreSQL last 150
           snapshots ≈ 3000–5000 tick equivalent).  This is the primary
           source — provides genuine long-memory view.
        2. In-memory OLS over the last 500 ticks (fallback at startup or
           when DATABASE_URL is not set).

        Flat threshold is DERIV_MACRO_HD_FLAT_PCT (default 0.0001 % per step)
        — corrects the prior 0.001 % threshold that caused hd=0.00 on
        Deriv synthetics where price moves are ≪ 0.001 %/tick.

        Parameters
        ----------
        ticks            : in-memory tick buffer (most recent = last)
        trade_dir        : +1 = MULTUP, -1 = MULTDOWN, None = unknown
        macro_slope_pct  : pre-computed macro slope from MacroHDCalibrator;
                           when provided, the in-memory OLS is skipped.
        """
        if trade_dir is None:
            return 0.0

        # ── Source 1: PostgreSQL macro slope (preferred) ──────────────────
        if macro_slope_pct is not None:
            slope_pct = macro_slope_pct
        elif len(ticks) >= 100:
            # ── Source 2: in-memory OLS fallback ─────────────────────────
            macro = ticks[-min(500, len(ticks)):]
            n = len(macro)
            if n < 20:
                return 0.0
            x_mean = (n - 1) / 2.0
            y_mean = sum(macro) / n
            if y_mean == 0:
                return 0.0
            ss_xy = sum((i - x_mean) * (macro[i] - y_mean) for i in range(n))
            ss_xx = sum((i - x_mean) ** 2 for i in range(n))
            if ss_xx == 0:
                return 0.0
            slope_pct = (ss_xy / ss_xx) / y_mean * 100.0
        else:
            return 0.0

        # Flat threshold: 0.0001 %/step — permissive enough to catch genuine
        # macro trends on Deriv synthetics (price moves ≪ 0.001 %/tick).
        if abs(slope_pct) < _MACRO_HD_FLAT_THRESHOLD:
            return 0.0

        macro_dir = 1 if slope_pct > 0 else -1
        if macro_dir == trade_dir:
            return 2.0    # ← HD aligned: macro trend confirms trade direction (+2.0, boosted from 1.5)
        return -0.5       # ← HD opposed: macro trend contradicts trade direction

    @staticmethod
    def _synthetic_atr(ticks: list[float]) -> float:
        """Mean absolute first-difference over the last 30 ticks."""
        window = ticks[-30:]
        if len(window) < 2:
            return 0.0
        diffs = [abs(window[i] - window[i - 1]) for i in range(1, len(window))]
        return mean(diffs)

    @staticmethod
    def _linear_regression_slope(prices: list[float]) -> float:
        """Compute OLS slope of prices vs index, normalised by price level."""
        n = len(prices)
        if n < 5:
            return 0.0
        x_mean = (n - 1) / 2.0
        y_mean = mean(prices)
        if y_mean == 0:
            return 0.0
        num = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        slope = num / den
        return slope / y_mean  # normalised: slope per step / price level

    @staticmethod
    def _r_squared(prices: list[float]) -> float:
        """R² of linear fit — 1.0 = perfect trend, 0.0 = random noise."""
        n = len(prices)
        if n < 5:
            return 0.0
        y_mean = mean(prices)
        ss_tot = sum((p - y_mean) ** 2 for p in prices)
        if ss_tot == 0:
            return 1.0
        x_mean = (n - 1) / 2.0
        num = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        slope = num / den
        intercept = y_mean - slope * x_mean
        ss_res = sum((prices[i] - (slope * i + intercept)) ** 2 for i in range(n))
        return max(0.0, 1.0 - ss_res / ss_tot)

    @classmethod
    def _trend_score_v2(cls, ticks: list[float]) -> tuple[float, int | None]:
        """Enhanced trend scoring using multi-window votes + linear regression strength.

        Returns (score 0–3, direction +1/-1/None).
        """
        windows = [20, 60, min(180, len(ticks))]
        votes: list[int] = []
        for w in windows:
            if w < 5 or len(ticks) < w:
                continue
            seg = ticks[-w:]
            slope = cls._linear_regression_slope(seg)
            threshold = 0.00005  # normalised slope threshold (5e-5 per tick / price)
            if slope > threshold:
                votes.append(+1)
            elif slope < -threshold:
                votes.append(-1)
            else:
                votes.append(0)

        if not votes:
            return 0.0, None

        net = sum(votes)

        # Require at least 2 windows agreeing on the same non-zero direction
        if all(v == votes[0] and v != 0 for v in votes):
            direction = votes[0]
        elif net >= 2:
            direction = +1
        elif net <= -2:
            direction = -1
        else:
            return 0.0, None

        # Weight by regression quality of the main window
        r2 = cls._r_squared(ticks[-min(60, len(ticks)):])
        trend_pts = 1.5 + 1.5 * r2  # 1.5 to 3.0 based on trend clarity
        return min(3.0, round(trend_pts, 2)), direction

    @staticmethod
    def _momentum_score(ticks: list[float]) -> float:
        """Rate-of-change acceleration: 1.5 pts if recent price is accelerating in one direction.

        Compares the slope of the last 10 ticks vs the slope of the 10 before that.
        Consistent acceleration = momentum confirmation = 1.5 pts.
        """
        if len(ticks) < 25:
            return 0.0
        recent = ticks[-10:]
        prior  = ticks[-20:-10]
        recent_mean = mean(recent)
        prior_mean  = mean(prior)
        if recent_mean == 0 or prior_mean == 0:
            return 0.0
        # Net change in each window, normalised by price
        delta_recent = (recent[-1] - recent[0]) / prior_mean
        delta_prior  = (prior[-1]  - prior[0])  / prior_mean
        # Acceleration: both windows moving in same direction
        if delta_recent > 0 and delta_prior > 0:
            accel = delta_recent / (abs(delta_prior) + 1e-12)
            return min(1.5, 1.0 * accel)
        if delta_recent < 0 and delta_prior < 0:
            accel = abs(delta_recent) / (abs(delta_prior) + 1e-12)
            return min(1.5, 1.0 * accel)
        # Deceleration / reversal
        return 0.0

    def _spread_score(self, spread_pct: float) -> float:
        cap = self._settings.max_spread_pct
        if cap <= 0:
            return 0.5
        ratio = spread_pct / cap
        if ratio <= 0.4:
            return 0.5
        if ratio <= 0.7:
            return 0.25
        return 0.0

    def _atr_adaptive_score(self, atr: float, last_price: float, symbol: str) -> float:
        """2 pts — adaptive: compares current ATR vs rolling ATR history.

        Better than fixed thresholds because synthetic index vol can drift.
        - If current ATR is in the 30th–70th percentile of recent ATR: 2 pts (ideal)
        - If too calm or too wild: reduced score
        """
        if last_price <= 0 or atr <= 0:
            return 0.0
        atr_pct = atr / last_price

        hist = self._atr_history.get(symbol, [])
        if len(hist) < 5:
            # Fallback to fixed bands until we have enough history
            if 0.0005 <= atr_pct <= 0.004:
                return 2.0
            if 0.0003 <= atr_pct <= 0.006:
                return 1.0
            return 0.0

        # Compute percentile rank of current ATR in history
        sorted_hist = sorted(hist)
        rank = sum(1 for h in sorted_hist if h <= atr) / len(sorted_hist)

        # FIX-7: Continuous scoring — replace step function (1.0/2.0) with
        # linear interpolation.  Peak 2.0 at rank=0.50, decays to 0.0 at edges.
        # rank=0.50→2.0, rank=0.30/0.70→1.0, rank=0.20/0.80→0.5, rank≤0.10/≥0.90→0.0
        if rank < 0.10 or rank > 0.90:
            return 0.0
        center_dist = abs(rank - 0.50)  # 0.0 at center, 0.40 at rank=0.10/0.90
        return round(max(0.0, 2.0 * (1.0 - center_dist / 0.40)), 2)

    @staticmethod
    def _stability_score(ticks: list[float]) -> float:
        """1.5 pts if no |Δ| > 4×stdev in last 60 ticks (spike filter)."""
        window = ticks[-60:]
        if len(window) < 10:
            return 0.0
        diffs = [abs(window[i] - window[i - 1]) for i in range(1, len(window))]
        if not diffs:
            return 0.0
        sigma = pstdev(diffs) or 1e-12
        max_diff = max(diffs)
        if max_diff <= 3 * sigma:
            return 1.5
        if max_diff <= 5 * sigma:
            return 0.75
        return 0.0

    @staticmethod
    def _detect_regime(ticks: list[float], atr: float) -> str:
        """Classify market as trending / ranging / volatile / calm."""
        if len(ticks) < 30:
            return "unknown"
        last_price = ticks[-1]
        if last_price <= 0:
            return "unknown"
        atr_pct = atr / last_price

        # Check recent linear regression quality
        r2 = DerivRiskManager._r_squared(ticks[-60:] if len(ticks) >= 60 else ticks)

        if atr_pct > 0.005:
            return "volatile"
        # Calm threshold halved (env-var tunable): the old 0.0003 was too aggressive —
        # R_100 at price 1700 with typical ATR 0.3 gives atr_pct ≈ 0.000176 which is
        # below 0.0003 and was incorrectly classified as 'calm' instead of 'ranging'.
        _calm_thr = float(os.getenv("DERIV_CALM_ATR_PCT", "0.00015"))
        if atr_pct < _calm_thr:
            return "calm"
        if r2 >= 0.55:
            return "trending"
        return "ranging"

    def _streak_penalty(self) -> float:
        if self._loss_streak <= 0:
            return 0.0
        raw = -min(2.0, 0.7 * self._loss_streak)
        return max(-0.20, raw)  # CALIBRATION SAMPLING: clamp — never freeze below -0.20

    def _cooldown_bonus(self, symbol: str = "") -> float:
        """Per-symbol cooldown bonus.

        Uses the per-symbol trade timestamp when available so that
        high-frequency R_50/R_75 trades don't starve BOOM/CRASH of their
        cooldown bonus (which counts as +0.5→+1.0 in the total score).
        Falls back to the global _last_trade_ts for backward compatibility.
        """
        _su = symbol.upper() if symbol else ""
        _ts = self._last_trade_ts_per_symbol.get(_su, 0.0) if _su else self._last_trade_ts
        # If no per-symbol record yet, treat as fresh start → 0.5
        if _ts == 0.0:
            _cd = 0.5
        elif time.time() - _ts > 300:  # >5 min since last trade on this symbol
            _cd = 1.0
        else:
            _cd = 0.0
        if _su:
            _elapsed = round(time.time() - _ts, 1) if _ts > 0 else 9999.0
            _LOGGER.debug(
                "[CD_STATUS] %s cd_bonus=%.1f last_trade_ts=%s elapsed=%.0fs "
                "(global_ts=%.0f per_sym_ts=%.0f)",
                _su, _cd,
                "never" if _ts == 0.0 else datetime.fromtimestamp(_ts).strftime("%H:%M:%S"),
                _elapsed,
                self._last_trade_ts,
                self._last_trade_ts_per_symbol.get(_su, 0.0),
            )
        return _cd

    def _headroom_bonus(self, dd_cap: float) -> float:
        if dd_cap <= 0:
            return 0.0
        used = max(0.0, -self._daily_realized_pnl)
        if used / dd_cap <= 0.5:
            return 0.5
        if used / dd_cap <= 0.8:
            return 0.25
        return 0.0

    def _suggest_multiplier(self, atr: float, last_price: float, symbol: str = "") -> int:
        """Pick a conservative multiplier respecting each symbol's valid range.

        BOOM/CRASH multiplier contracts have a restricted set of valid values
        enforced by the broker:
          BOOM1000 / CRASH1000 : 100, 200, 300, 400, 500
          BOOM500  / CRASH500  : 100, 150, 200, 300, 400
        Returning an out-of-range value causes an immediate InvalidtoBuy rejection.

        For R_* indices the ATR-based conservative capping still applies.
        """
        _su = symbol.upper()
        _is_boom_crash = "BOOM" in _su or "CRASH" in _su

        if _is_boom_crash:
            # Broker-validated multiplier sets for spike markets
            if "1000" in _su:
                _valid = [100, 200, 300, 400, 500]
            else:
                _valid = [100, 150, 200, 300, 400]
            target = self._settings.multiplier
            # Largest valid multiplier that does not exceed the configured target
            candidates = [v for v in _valid if v <= target]
            return max(candidates) if candidates else min(_valid)

        # R_* indices: use ATR-based conservative capping
        if last_price <= 0 or atr <= 0:
            return min(self._settings.multiplier, 30)
        atr_pct = atr / last_price
        if atr_pct >= 0.003:
            return min(self._settings.multiplier, 30)
        if atr_pct >= 0.0015:
            return min(self._settings.multiplier, 50)
        return self._settings.multiplier

    # ─────────────────────────────────────────────────────────────────────────
    # Lockout persistence (atomic JSON file in logs/)
    # ─────────────────────────────────────────────────────────────────────────
    def _engage_lockout(self, reason: str) -> None:
        until = time.time() + self._settings.lockout_hours * 3600
        state = _LockoutState(locked=True, reason=reason, until_ts=until)
        self._write_lockout(state)
        _LOGGER.error(
            "[deriv-risk] LOCKOUT engaged for %.1f h | reason=%s",
            self._settings.lockout_hours, reason,
        )

    def _clear_lockout(self, reason: str) -> None:
        """Force-clear an active lockout (auto-unlock after winning trade).

        Daily-DD lockouts are NOT cleared here \u2014 only loss-streak lockouts.
        We detect daily-DD by checking the persisted reason; if it starts with
        'daily_dd' we keep the lockout intact (capital protection wins).
        """
        existing = self._read_lockout()
        if existing.locked and existing.reason.startswith("daily_dd"):
            _LOGGER.warning(
                "[LOCKOUT_STATUS] skip auto-clear: daily_dd lockout retained \u2014 trigger=%s",
                reason,
            )
            return
        empty = _LockoutState.empty()
        self._write_lockout(empty)
        _LOGGER.warning(
            "[LOCKOUT_STATUS] AUTO-UNLOCK trigger=%s previous_reason=%r consecutive_wins=%d",
            reason, existing.reason, self._consecutive_wins,
        )

    def _read_lockout(self) -> _LockoutState:
        path = self._settings.lockout_file
        if not path.exists():
            return _LockoutState.empty()
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return _LockoutState.empty()
        until_ts = float(data.get("until_ts", 0) or 0)
        if until_ts <= time.time():
            return _LockoutState.empty()
        return _LockoutState(
            locked=bool(data.get("locked", False)),
            reason=str(data.get("reason", "")),
            until_ts=until_ts,
        )

    def _write_lockout(self, state: _LockoutState) -> None:
        path = self._settings.lockout_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"locked": state.locked, "reason": state.reason, "until_ts": state.until_ts},
                indent=2,
            )
        )
        tmp.replace(path)

    def _roll_daily_anchor_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_anchor_date:
            self._daily_anchor_date = today
            self._daily_realized_pnl = 0.0


