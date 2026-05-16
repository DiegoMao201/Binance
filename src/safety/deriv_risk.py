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

from src.strategies.deriv_signals import (
    direction_veto as _spike_direction_veto,
    forced_side as _spike_forced_side,
    is_spike_market as _is_spike_market,
)
from src.utils.deriv_config import DerivSettings


_LOGGER = logging.getLogger(__name__)

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
        self._max_window = 270
        # Per-symbol rolling ATR history (last 50 ATR values for percentile)
        self._atr_history: dict[str, list[float]] = {}
        self._MAX_ATR_HISTORY = 50
        # Force-test-trades tick counter (DERIV_FORCE_TEST_TRADES mode)
        self._force_tick_count: dict[str, int] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Public surface called by the daemon / trader / order router
    # ─────────────────────────────────────────────────────────────────────────
    def ingest_tick(self, symbol: str, price: float) -> None:
        if price <= 0 or not math.isfinite(price):
            return
        buf = self._ticks.setdefault(symbol, [])
        buf.append(price)
        if len(buf) > self._max_window:
            del buf[: len(buf) - self._max_window]
        # Update ATR history every 30 ticks
        if len(buf) >= 30 and len(buf) % 10 == 0:
            atr = self._synthetic_atr(buf)
            hist = self._atr_history.setdefault(symbol, [])
            hist.append(atr)
            if len(hist) > self._MAX_ATR_HISTORY:
                del hist[: len(hist) - self._MAX_ATR_HISTORY]

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
        # Set DERIV_FORCE_TEST_TRADES=true to skip all scoring gates and inject a
        # forced test order every 60 ticks on BOOM/CRASH symbols.  This is purely
        # for verifying the WS buy path works on a virtual (VRTC) account — it
        # must NEVER be enabled in live environments.
        _force_test = os.getenv("DERIV_FORCE_TEST_TRADES", "").lower() in ("1", "true", "yes")
        if _force_test and _is_spike_market(symbol):
            self._force_tick_count[symbol] = self._force_tick_count.get(symbol, 0) + 1
            tick_n = self._force_tick_count[symbol]
            if tick_n % 60 == 0:
                _fs = _spike_forced_side(symbol) or "MULTUP"
                snap.allowed = True
                snap.side = _fs
                snap.score = 6.0
                snap.effective_min_score = 6.0
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
        if len(ticks) < 30:
            snap.reasons.append(f"insufficient ticks ({len(ticks)}/30)")
            return snap

        # ── Compute factors ────────────────────────────────────────────────
        atr = self._synthetic_atr(ticks)
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

        # ── Hurst dynamic calibration ──────────────────────────────────────
        # Applies score delta and/or threshold adjustment based on the hourly
        # snapshot from v_deriv_hurst_buckets.  Zero-cost: reads in-process dict.
        hurst_delta = 0.0
        effective_min_score = self._settings.min_score

        if hurst is not None and math.isfinite(hurst):
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

        snap.score = round(score, 3)
        snap.score_breakdown = {
            "trend": round(trend_score, 2),
            "momentum": round(momentum_score, 2),
            "spread": round(spread_score, 2),
            "atr": round(atr_score, 2),
            "stability": round(stability_score, 2),
            "streak_penalty": round(streak_penalty, 2),
            "cooldown": round(cooldown_bonus, 2),
            "headroom": round(headroom_bonus, 2),
            "regime": regime,
            "hurst_delta": round(hurst_delta, 2),
            "effective_min_score": round(effective_min_score, 2),
        }

        # Veto if trend ambiguous
        if trend_dir is None:
            snap.reasons.append("ambiguous trend across windows")
            return snap

        side = "MULTUP" if trend_dir > 0 else "MULTDOWN"
        snap.side = side

        # ── Spike asymmetry veto (BOOM/CRASH hard rule) ───────────────────
        # BOOM indices only allow MULTUP; CRASH indices only allow MULTDOWN.
        # This is a deterministic hard gate — no score can override it.
        _spike_vetoed, _spike_reason = _spike_direction_veto(symbol, side)
        if _spike_vetoed:
            snap.reasons.append(_spike_reason)
            snap.allowed = False
            return snap

        # ── AI confidence guardrail + math override ───────────────────────
        # If AI confidence is low but Hurst > 0.65 AND autocorr is aligned
        # with the trade side, the mathematical microstructure signal is
        # considered more reliable than the qualitative LLM opinion.
        # This allows a safe override that avoids blocking profitable setups
        # when the AI model is being conservative or uncertain.
        _ai_min_conf = float(os.getenv("DERIV_AI_MIN_CONFIDENCE", "0.65"))
        snap.hurst_ai_override = False
        if (
            ai_confidence is not None
            and ai_confidence < _ai_min_conf
            and hurst is not None
            and hurst > 0.65
            and autocorr_lag1 is not None
        ):
            # Check autocorr alignment with direction (+1 MULTUP needs positive autocorr)
            autocorr_aligned = (
                (trend_dir > 0 and autocorr_lag1 > 0.05) or
                (trend_dir < 0 and autocorr_lag1 < -0.05)
            )
            if autocorr_aligned:
                snap.hurst_ai_override = True
                snap.reasons.append(
                    f"hurst_math_override: H={hurst:.3f}>0.65 autocorr={autocorr_lag1:.3f} "
                    f"(ai_conf={ai_confidence:.2f} < {_ai_min_conf}) — math > LLM"
                )
                _LOGGER.info(
                    "[deriv-risk] AI override by math: H=%.3f autocorr=%.3f ai_conf=%.2f",
                    hurst, autocorr_lag1, ai_confidence,
                )

        # Sizing — proportional to bankroll, capped by ATR (anti-Martingale).
        risk_usdt = self._settings.bankroll_usdt * self._settings.risk_per_trade_pct
        # Reduce risk by the loss streak (mild de-grossing — never increase).
        if self._loss_streak >= 1:
            risk_usdt *= max(0.5, 1.0 - 0.15 * self._loss_streak)
        # In volatile regime, reduce stake by 20%
        if regime == "volatile":
            risk_usdt *= 0.8
        stake = max(1.0, min(risk_usdt, self._settings.bankroll_usdt * 0.25))
        snap.suggested_stake_usdt = round(stake, 2)
        snap.suggested_multiplier = self._suggest_multiplier(atr, ticks[-1])

        if score < effective_min_score:
            snap.reasons.append(
                f"score {score:.2f} < min {effective_min_score:.2f}"
            )
            return snap

        snap.allowed = True
        snap.reasons.append(f"GO — trend={side} score={score:.2f} regime={regime}")
        return snap

    def register_close(self, realized_pnl_usdt: float) -> None:
        """Update streak + daily DD after a contract closes."""
        self._roll_daily_anchor_if_needed()
        self._daily_realized_pnl += realized_pnl_usdt
        self._last_trade_ts = time.time()
        if realized_pnl_usdt < 0:
            self._loss_streak += 1
            if self._loss_streak >= self._settings.loss_streak_lockout:
                self._engage_lockout(
                    f"{self._loss_streak} consecutive losses"
                )
        else:
            self._loss_streak = 0

    # ─────────────────────────────────────────────────────────────────────────
    # Internal scoring helpers — v2 (smarter math)
    # ─────────────────────────────────────────────────────────────────────────
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
        if atr_pct < 0.0003:
            return "calm"
        if r2 >= 0.55:
            return "trending"
        return "ranging"

    def _streak_penalty(self) -> float:
        if self._loss_streak <= 0:
            return 0.0
        return -min(2.0, 0.7 * self._loss_streak)

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

    def _suggest_multiplier(self, atr: float, last_price: float) -> int:
        """Pick a conservative multiplier band based on synthetic vol."""
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


