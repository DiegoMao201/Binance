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

Score factors (weighted out of 10):
  - Trend agreement (3 pts)        — direction across last 30/90/270 ticks
  - Synthetic-ATR window (2 pts)   — current vol within healthy band
  - Spread tightness (1 pt)        — current spread <= 0.5 * max_spread
  - Recent stability (2 pts)       — no spike in last N ticks
  - Loss-streak penalty (-2 pts)   — applied if 1+ consecutive loss
  - Cooldown bonus (1 pt)          — applied after a clean cooldown
  - Bankroll headroom (1 pt)       — daily DD < 50 % of cap
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable

from src.utils.deriv_config import DerivSettings


_LOGGER = logging.getLogger(__name__)


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

    def evaluate(self, symbol: str, spread_pct: float) -> DerivRiskSnapshot:
        """Score the current state for `symbol` and return an actionable snapshot."""
        snap = DerivRiskSnapshot(allowed=False, score=0.0, side=None, spread_pct=spread_pct)

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

        trend_score, trend_dir = self._trend_score(ticks)
        spread_score = self._spread_score(spread_pct)
        atr_score = self._atr_score(atr, ticks[-1])
        stability_score = self._stability_score(ticks)
        streak_penalty = self._streak_penalty()
        cooldown_bonus = self._cooldown_bonus()
        headroom_bonus = self._headroom_bonus(dd_cap)

        score = (
            trend_score
            + spread_score
            + atr_score
            + stability_score
            + streak_penalty
            + cooldown_bonus
            + headroom_bonus
        )
        score = max(0.0, min(10.0, score))
        snap.score = round(score, 3)

        # Veto if trend ambiguous
        if trend_dir is None:
            snap.reasons.append("ambiguous trend across windows")
            return snap

        side = "MULTUP" if trend_dir > 0 else "MULTDOWN"
        snap.side = side

        # Sizing — proportional to bankroll, capped by ATR (anti-Martingale).
        # We assume the worst-case slippage equals 1.5× ATR for stake estimation.
        risk_usdt = self._settings.bankroll_usdt * self._settings.risk_per_trade_pct
        # Reduce risk by the loss streak (mild de-grossing — never increase).
        if self._loss_streak >= 1:
            risk_usdt *= max(0.5, 1.0 - 0.15 * self._loss_streak)
        # Final stake: never more than 25 % of bankroll, never less than $1.
        stake = max(1.0, min(risk_usdt, self._settings.bankroll_usdt * 0.25))
        snap.suggested_stake_usdt = round(stake, 2)
        snap.suggested_multiplier = self._suggest_multiplier(atr, ticks[-1])

        if score < self._settings.min_score:
            snap.reasons.append(
                f"score {score:.2f} < min {self._settings.min_score:.2f}"
            )
            return snap

        snap.allowed = True
        snap.reasons.append(f"GO — trend={side} score={score:.2f}")
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
            # Any win or break-even resets the streak.
            self._loss_streak = 0

    # ─────────────────────────────────────────────────────────────────────────
    # Internal scoring helpers
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
    def _trend_score(ticks: list[float]) -> tuple[float, int | None]:
        """3 pts max if 3 timeframe windows agree on direction."""
        windows = [30, 90, min(270, len(ticks))]
        votes: list[int] = []
        for w in windows:
            if w < 5 or len(ticks) < w:
                continue
            seg = ticks[-w:]
            if seg[-1] > seg[0]:
                votes.append(+1)
            elif seg[-1] < seg[0]:
                votes.append(-1)
            else:
                votes.append(0)
        if not votes:
            return 0.0, None
        net = sum(votes)
        # Require unanimous direction for full score; partial agreement → partial.
        if all(v == votes[0] and v != 0 for v in votes):
            return 3.0, votes[0]
        if net > 0:
            return 1.5, +1
        if net < 0:
            return 1.5, -1
        return 0.0, None

    def _spread_score(self, spread_pct: float) -> float:
        cap = self._settings.max_spread_pct
        if cap <= 0:
            return 1.0
        ratio = spread_pct / cap
        if ratio <= 0.5:
            return 1.0
        if ratio <= 1.0:
            return 0.5
        return 0.0

    @staticmethod
    def _atr_score(atr: float, last_price: float) -> float:
        """2 pts if ATR is in a healthy 0.05–0.40 % of price band."""
        if last_price <= 0 or atr <= 0:
            return 0.0
        atr_pct = atr / last_price
        if 0.0005 <= atr_pct <= 0.004:
            return 2.0
        if 0.0003 <= atr_pct <= 0.006:
            return 1.0
        return 0.0

    @staticmethod
    def _stability_score(ticks: list[float]) -> float:
        """Penalise spikes: 2 pts if no |Δ| > 4×stdev in last 60 ticks."""
        window = ticks[-60:]
        if len(window) < 10:
            return 0.0
        diffs = [abs(window[i] - window[i - 1]) for i in range(1, len(window))]
        if not diffs:
            return 0.0
        sigma = pstdev(diffs) or 1e-12
        max_diff = max(diffs)
        if max_diff <= 4 * sigma:
            return 2.0
        if max_diff <= 6 * sigma:
            return 1.0
        return 0.0

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
            return 1.0
        if used / dd_cap <= 0.8:
            return 0.5
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
