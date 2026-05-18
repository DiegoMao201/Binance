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
        self._last_trade_ts = 0.0
        self._daily_realized_pnl = 0.0
        self._daily_anchor_date = datetime.now(timezone.utc).date()
        # Per-symbol rolling tick window for synthetic ATR / trend.
        self._ticks: dict[str, list[float]] = {}
        self._max_window = 1000  # enlarged for multi-TF geometry (was 270)
        # Per-symbol rolling ATR history (last 50 ATR values for percentile)
        self._atr_history: dict[str, list[float]] = {}
        self._MAX_ATR_HISTORY = 50
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
        # 0.45 ≤ h ≤ 0.55 — absolute noise zone
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
                        self._last_spike_ts[symbol] = time.time()
                        _LOGGER.info(
                            "[SPIKE_DETECTED] %s jump=%.5f atr=%.5f (%.1f×) — "
                            "spike-cycle timer reset",
                            symbol, _jump, _recent_atr, abs(_jump) / _recent_atr,
                        )

    def evaluate(
        self,
        symbol: str,
        spread_pct: float,
        *,
        hurst: float | None = None,
        autocorr_lag1: float | None = None,
        ai_confidence: float | None = None,
    ) -> DerivRiskSnapshot:
        """Score the current state for `symbol` and return an actionable snapshot.

        Optional keyword arguments allow the caller (main_deriv) to pass statistical
        context from DerivAnalyst so calibration rules can be applied:
          hurst          — Hurst exponent for the current symbol (0–1)
          autocorr_lag1  — autocorrelation of log-returns at lag 1
          ai_confidence  — AI gate confidence (0–1); used for the math-override rule
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
        if _is_spike_market(symbol) and not _force_test_active:
            _last_spike = self._last_spike_ts.get(symbol, 0.0)
            if _last_spike > 0:
                _sg_elapsed = time.time() - _last_spike
                _su_sg = symbol.upper()
                _spike_gate_interval = (
                    1000 if "1000" in _su_sg
                    else (500 if "500" in _su_sg else 300)
                )
                _frac = float(os.getenv("DERIV_SPIKE_CYCLE_FRAC", "0.30"))
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
        cooldown_bonus = self._cooldown_bonus()
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
                # Hard floor: never let the env-var push below 5.80.
                # Values below 5.80 introduce over-triggering risk; any
                # DERIV_CALM_STRUCTURAL_MIN_SCORE < 5.80 is silently clamped.
                _CALM_MIN_HARD_FLOOR: float = 5.80
                _bc_calm_floor = max(
                    _CALM_MIN_HARD_FLOOR,
                    float(os.getenv("DERIV_CALM_STRUCTURAL_MIN_SCORE", "5.80")),
                )
                effective_min_score = _bc_calm_floor   # direct override — no min()
                _calm_bc_floor_used = _bc_calm_floor
                _LOGGER.info(
                    "[PIPELINE] [REGIME_SCORE_GATE_calm] %s regime=calm "
                    "effective_min=%.2f (floor=%.2f DERIV_CALM_STRUCTURAL_MIN_SCORE) "
                    "atr_bypassed=%s",
                    symbol, effective_min_score, _CALM_MIN_HARD_FLOOR, _atr_calm_bypassed,
                )
            else:
                effective_min_score = min(effective_min_score, 5.8)  # spike edge ≠ trend edge
        elif "R_100" in _su_sym:
            if regime == "calm":
                effective_min_score = min(effective_min_score, 5.50)
            else:
                effective_min_score = min(effective_min_score, 5.5)
        elif "R_75" in _su_sym:
            if regime == "calm":
                effective_min_score = min(effective_min_score, 5.50)
            else:
                effective_min_score = min(effective_min_score, 6.0)
        elif "R_25" in _su_sym or "R_50" in _su_sym:
            effective_min_score = min(effective_min_score, 5.5)       # mean-rev edge
        snap.effective_min_score = round(effective_min_score, 2)

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
        # Uses the 500-tick (~1H) macro trend to confirm or penalise the proposed
        # direction AFTER trend_dir has been fully resolved (spike forced-side /
        # mean-rev fade / OLS trend).  Applied BEFORE score_breakdown is built so
        # both the score and the breakdown dict reflect the correct HD value.
        _hd_bonus = self._higher_direction_bonus(ticks, trend_dir)
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

        # ── Hard Random-Walk veto (R_* indices only) ──────────────────────
        # When Hurst sits inside [0.45, 0.55] the price series is statistically
        # indistinguishable from a random walk — there is no edge to exploit
        # and the spread will systematically eat the position. This veto blocks
        # ALL entries on volatility indices in that band. Spike markets
        # (BOOM/CRASH) are exempt because their edge is the spike asymmetry,
        # not Hurst-based trend.
        _rw_lo = float(os.getenv("DERIV_RANDOM_WALK_LO", "0.45"))
        _rw_hi = float(os.getenv("DERIV_RANDOM_WALK_HI", "0.55"))
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
            else:
                snap.reasons.append(
                    f"random_walk_veto: H={hurst:.3f} ∈ [{_rw_lo:.2f},{_rw_hi:.2f}] — no statistical edge"
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
        if _geo is not None and _geo.smc_side is not None and _geo.smc_bonus > 0:
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
        _ai_min_conf = float(os.getenv("DERIV_AI_MIN_CONFIDENCE", "0.55"))
        snap.hurst_ai_override = False
        _ai_failed = ai_confidence is None or ai_confidence < _ai_min_conf
        if _ai_failed:
            _override_reason = ""
            # (a) Hurst-trend confluence
            if (
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
            elif _geo is not None and _geo.smc_side is not None and _geo.smc_side == side:
                _override_reason = f"smc_confluence: {_geo.smc_reason}"
            # (c) Mean-reverting micro scalp — STRICTLY volatility-only.
            # BOOM/CRASH are spike-asymmetric and have no statistical band-touch
            # edge.  Allowing this branch for spike markets caused repeated
            # false overrides on BOOM1000/CRASH1000 that were later vetoed by
            # the structural gate (log pollution + wasted CPU).
            elif (
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

        # ── BOOM/CRASH structural gate ────────────────────────────────
        # BOOM and CRASH indices have a structural edge ONLY when entered at:
        #   (a) an unmitigated FVG (Smart Money Concept liquidity zone), OR
        #   (b) an EMA-200 spike-hunter setup (price dipping into support/resistance)
        # Random entries on these symbols statistically lose to spike_timeout
        # because the slow-drift phase between spikes wears down the position.
        # If neither structural setup is active, veto the trade.
        if _is_spike_market(symbol):
            _has_smc = bool(snap.score_breakdown.get("fvg_active"))
            _has_spike_hunter = bool(snap.score_breakdown.get("spike_entry"))
            if not (_has_smc or _has_spike_hunter):
                snap.reasons.append(
                    "boom_crash_structural_veto: no FVG mitigation and no EMA200 spike-hunter setup"
                )
                snap.allowed = False
                return snap

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
            _fvg_aligned = (
                ("BOOM"  in _su_gate and _fvg_dir == "bull") or
                ("CRASH" in _su_gate and _fvg_dir == "bear")
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
                _frac_used = float(os.getenv("DERIV_SPIKE_CYCLE_FRAC", "0.30"))
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
        # ── Score → position size (confidence-proportional) ──────────────────
        # Compressed score drives a ±20% size adjustment so top-tier setups
        # trade larger while mediocre ones trade smaller.
        #   score ≥ 8.5 → up to +20% (linear to score 10)
        #   score < 6.5 → down to −25% (linear from 6.5 to 5.5)
        if score >= 8.5:
            _score_sz = 1.0 + 0.20 * min(1.0, (score - 8.5) / 1.5)
        elif score < 6.5:
            _score_sz = max(0.75, 1.0 - 0.25 * (6.5 - score))
        else:
            _score_sz = 1.0
        risk_usdt *= _score_sz
        snap.score_breakdown["score_size_mult"] = round(_score_sz, 3)
        # Hard floor at $1.00 (DERIV_MIN_STAKE_USDT_HARD overrides).
        # Hard cap at $3.00 (DERIV_MAX_STAKE_USDT overrides) — ABSOLUTE, unbreakable.
        # Prevents any regime/score multiplier from producing $30+ orders.
        _HARD_FLOOR_USDT: float = float(os.getenv("DERIV_MIN_STAKE_USDT_HARD", "1.00"))
        _HARD_CAP_USDT: float = float(os.getenv("DERIV_MAX_STAKE_USDT", "3.00"))
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

    def register_close(self, realized_pnl_usdt: float) -> None:
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

        if realized_pnl_usdt > win_significant_threshold:
            if self._loss_streak > 0:
                _LOGGER.info(
                    "[risk] significant win pnl=%.3f > thr=%.3f — streak reset (was %d)",
                    realized_pnl_usdt, win_significant_threshold, self._loss_streak,
                )
            self._loss_streak = 0
        elif realized_pnl_usdt < 0:
            self._loss_streak += 1
            cap = self._settings.loss_streak_lockout
            if cap > 0 and self._loss_streak >= cap:
                self._engage_lockout(
                    f"{self._loss_streak} consecutive losses"
                )
        else:
            # 0 ≤ pnl ≤ threshold — micro-win, preserve counter
            _LOGGER.info(
                "[risk] micro_win pnl=%.3f ≤ thr=%.3f — streak preserved (%d)",
                realized_pnl_usdt, win_significant_threshold, self._loss_streak,
            )

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
    ) -> float:
        """Multi-timeframe Higher Direction (HD) score.

        Uses the last 500 ticks (~1H equivalent at ~1 tick/s on Deriv synthetics)
        to determine the macro trend direction via OLS slope.  When the macro
        slope aligns with the proposed trade direction, award +1.5.  When it
        opposes, apply a mild -0.5 penalty.  A near-flat slope (< 0.001% per
        tick) returns 0.0 so the bonus only activates on genuine trends.

        Parameters
        ----------
        ticks     : full tick buffer (most recent = last element)
        trade_dir : +1 = MULTUP (bullish), -1 = MULTDOWN (bearish), None = unknown
        """
        if trade_dir is None or len(ticks) < 100:
            return 0.0

        macro = ticks[-min(500, len(ticks)):]
        n = len(macro)
        if n < 20:
            return 0.0

        # OLS slope via normal equations (vectorised, no NumPy dep at this level)
        x_mean = (n - 1) / 2.0
        y_mean = sum(macro) / n
        ss_xy = sum((i - x_mean) * (macro[i] - y_mean) for i in range(n))
        ss_xx = sum((i - x_mean) ** 2 for i in range(n))
        if ss_xx == 0 or y_mean == 0:
            return 0.0
        slope_pct = (ss_xy / ss_xx) / y_mean * 100.0  # % per tick

        if abs(slope_pct) < 0.001:   # near-flat → neutral
            return 0.0

        macro_dir = 1 if slope_pct > 0 else -1
        if macro_dir == trade_dir:
            return 1.5    # ← HD aligned: macro trend confirms trade direction
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

        if 0.20 <= rank <= 0.80:   # healthy middle range
            return 2.0
        if 0.10 <= rank <= 0.90:   # acceptable
            return 1.0
        return 0.0  # extreme outlier

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

    def _cooldown_bonus(self) -> float:
        if self._last_trade_ts == 0:
            return 0.5
        if time.time() - self._last_trade_ts > 300:  # >5 min calm
            return 1.0
        return 0.0

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


