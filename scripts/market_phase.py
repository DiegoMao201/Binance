"""
Market Phase intelligence for Deriv synthetic indices.

Concept
-------
The existing sidecar (``dynamic_ai_orchestrator``) already adapts thresholds
using a 2h-vs-6h tick acceleration ratio. That signal is purely *intra-symbol
and intra-session*: it cannot tell that ``CRASH900`` "usually" produces 18
spikes/hour at 19:00 UTC but only 4 spikes/hour at 02:00 UTC. As a result,
a symbol that decelerates because it just hit its natural quiet window keeps
trading aggressively until losses pile up.

This module adds a third axis: the **session baseline per (symbol, UTC hour)**.

For each symbol and each hour-of-day bucket (0..23) we maintain an
exponential moving average of *spikes per minute*. Each sidecar cycle:

1. We compute the *current* spikes-per-minute rate (window
   ``MARKET_PHASE_RECENT_WINDOW_SEC``, default 1800s).
2. We compare it against the EMA stored for the current UTC hour bucket.
3. We classify the symbol into a phase (``ACCEL``, ``NORMAL``, ``CAUTION``,
   ``DECEL``, ``DEAD``).
4. Once enough samples exist for that bucket, we update the EMA with the
   current rate so the baseline drifts slowly with real conditions.

Until a bucket has at least ``MARKET_PHASE_MIN_BUCKET_SAMPLES`` observations,
its phase is forced to ``NORMAL`` (the system stays neutral while it learns).

The classification is **only an advisory signal** — it is consumed by the
orchestrator to tighten/loosen ``score_min_override``, ``zero_peak_grace_sec``
and ``market_regime`` for that symbol. No bot-side code changes are required.

Persistence
-----------
Baseline is stored as a single JSON file (default
``$DYNAMIC_AI_LOGS_DIR/deriv_session_baseline.json``):

    {
      "version": 1,
      "updated_at": "2026-05-26T22:00:00Z",
      "symbols": {
        "BOOM900": {
          "00": {"ema_rate": 0.21, "samples": 14, "last_update_ts": 1716...},
          "01": {...},
          ...
        },
        ...
      }
    }

This file is safe to delete: the next sidecar cycle will start re-learning.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Tunables (all env-overridable; values chosen conservatively for live use).
# ---------------------------------------------------------------------------

def _f(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)) or default)
    except Exception:
        return float(default)


def _i(env: str, default: int) -> int:
    try:
        return int(os.getenv(env, str(default)) or default)
    except Exception:
        return int(default)


# Window used to compute the *current* spike rate per symbol.
RECENT_WINDOW_SEC: int = max(600, _i("MARKET_PHASE_RECENT_WINDOW_SEC", 1800))

# EMA smoothing factor for the baseline update. Lower = slower learning.
EMA_ALPHA: float = max(0.02, min(_f("MARKET_PHASE_EMA_ALPHA", 0.15), 0.50))

# Phase thresholds (ratio = recent_rate / baseline_rate).
ACCEL_RATIO: float = max(1.05, _f("MARKET_PHASE_ACCEL_RATIO", 1.30))
CAUTION_RATIO: float = min(0.95, _f("MARKET_PHASE_CAUTION_RATIO", 0.75))
DECEL_RATIO: float = min(CAUTION_RATIO - 0.05, _f("MARKET_PHASE_DECEL_RATIO", 0.55))
DEAD_RATIO: float = min(DECEL_RATIO - 0.05, _f("MARKET_PHASE_DEAD_RATIO", 0.25))

# Baseline must have at least this many observations before phases other
# than NORMAL are allowed to fire.
MIN_BUCKET_SAMPLES: int = max(3, _i("MARKET_PHASE_MIN_BUCKET_SAMPLES", 5))

# Minimum baseline rate (spikes/min) for the ratio to be meaningful. Below
# this floor we treat the bucket as inherently calm and stay NORMAL — we do
# not want a 0.01 baseline producing huge ratios on noise.
MIN_BASELINE_RATE: float = max(0.01, _f("MARKET_PHASE_MIN_BASELINE_RATE", 0.05))

# Confirmation streak (cycles) before DECEL/DEAD is reported. Prevents
# single-cycle noise from forcing a regime flip.
CONFIRM_CYCLES_DECEL: int = max(1, _i("MARKET_PHASE_CONFIRM_CYCLES_DECEL", 2))
CONFIRM_CYCLES_DEAD: int = max(1, _i("MARKET_PHASE_CONFIRM_CYCLES_DEAD", 3))

# Slope guard: compares a *fast* window (default 5min) vs a *slow* window
# (default 30min). When the fast rate drops by more than SLOPE_DROP_PCT
# relative to the slow rate we bypass the streak confirmation and escalate
# directly to CAUTION/DECEL on the same cycle. Symmetric for ACCEL.
SLOPE_FAST_SEC: int = max(120, _i("MARKET_PHASE_SLOPE_FAST_SEC", 300))
SLOPE_SLOW_SEC: int = max(SLOPE_FAST_SEC + 60, _i("MARKET_PHASE_SLOPE_SLOW_SEC", 1800))
SLOPE_DROP_PCT: float = max(0.10, min(0.90, _f("MARKET_PHASE_SLOPE_DROP_PCT", 0.40)))
SLOPE_DROP_HARD_PCT: float = max(SLOPE_DROP_PCT + 0.05, min(0.95, _f("MARKET_PHASE_SLOPE_DROP_HARD_PCT", 0.65)))
SLOPE_RISE_PCT: float = max(0.10, min(2.00, _f("MARKET_PHASE_SLOPE_RISE_PCT", 0.40)))
SLOPE_MIN_SLOW_RATE: float = max(0.02, _f("MARKET_PHASE_SLOPE_MIN_SLOW_RATE", 0.10))

# Multi-window trend vector (per-symbol behaviour over the last 6h).
# We compute rates at four nested windows and label each as up/down/flat
# vs the next-larger window. 2-of-3 sub-windows down forces at least CAUTION.
TREND_WINDOWS_SEC: tuple[int, ...] = (
    max(120, _i("MARKET_PHASE_TREND_W1_SEC", 300)),     # 5 min
    max(900, _i("MARKET_PHASE_TREND_W2_SEC", 1800)),    # 30 min
    max(3600, _i("MARKET_PHASE_TREND_W3_SEC", 7200)),   # 2 h
    max(10800, _i("MARKET_PHASE_TREND_W4_SEC", 21600)), # 6 h
)
TREND_LABELS: tuple[str, ...] = ("w5m", "w30m", "w2h", "w6h")
TREND_FLAT_BAND: float = max(0.05, min(0.40, _f("MARKET_PHASE_TREND_FLAT_BAND", 0.15)))
TREND_DOWN_MIN_RATE: float = max(0.02, _f("MARKET_PHASE_TREND_MIN_RATE", 0.05))

# Phases vocabulary (kept as plain strings to be JSON-safe).
PHASE_ACCEL = "ACCEL"
PHASE_NORMAL = "NORMAL"
PHASE_CAUTION = "CAUTION"
PHASE_DECEL = "DECEL"
PHASE_DEAD = "DEAD"

ALL_PHASES = (PHASE_ACCEL, PHASE_NORMAL, PHASE_CAUTION, PHASE_DECEL, PHASE_DEAD)


# ---------------------------------------------------------------------------
# Data containers.
# ---------------------------------------------------------------------------

@dataclass
class MarketPhase:
    symbol: str
    phase: str
    ratio: float | None
    recent_rate: float
    baseline_rate: float | None
    samples_recent: int
    samples_baseline: int
    hour_bucket: int
    streak: int  # consecutive cycles in this phase (after confirmation)
    reason: str
    # New (multi-window + slope) signals — optional for backward compat.
    windows: dict[str, float] | None = None
    slope_signal: str | None = None        # "DECEL_FAST" / "ACCEL_FAST" / None
    slope_drop_pct: float | None = None
    trend_down_count: int = 0
    trend_up_count: int = 0
    bypass_streak: bool = False


# ---------------------------------------------------------------------------
# Baseline store.
# ---------------------------------------------------------------------------

class SessionBaseline:
    """Per (symbol, UTC hour) EMA store of spikes-per-minute.

    The instance keeps a small in-memory streak counter per symbol so the
    orchestrator can require multiple confirming cycles before downgrading
    a symbol's phase. Streaks are NOT persisted (intentional — a sidecar
    restart resets confirmation but not the learned baseline).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._streaks: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        symbols = payload.get("symbols")
        if not isinstance(symbols, dict):
            return
        for sym, buckets in symbols.items():
            if not isinstance(buckets, dict):
                continue
            sym_u = str(sym or "").upper()
            if not sym_u:
                continue
            self._data[sym_u] = {}
            for hour_key, slot in buckets.items():
                if not isinstance(slot, dict):
                    continue
                try:
                    h = int(hour_key)
                except Exception:
                    continue
                if not 0 <= h <= 23:
                    continue
                try:
                    ema = float(slot.get("ema_rate") or 0.0)
                    samples = int(slot.get("samples") or 0)
                    last_ts = float(slot.get("last_update_ts") or 0.0)
                except Exception:
                    continue
                self._data[sym_u][f"{h:02d}"] = {
                    "ema_rate": max(0.0, ema),
                    "samples": max(0, samples),
                    "last_update_ts": last_ts,
                }

    def save(self) -> None:
        try:
            payload = {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "config": {
                    "recent_window_sec": RECENT_WINDOW_SEC,
                    "ema_alpha": EMA_ALPHA,
                    "min_bucket_samples": MIN_BUCKET_SAMPLES,
                    "min_baseline_rate": MIN_BASELINE_RATE,
                    "thresholds": {
                        "accel_ratio": ACCEL_RATIO,
                        "caution_ratio": CAUTION_RATIO,
                        "decel_ratio": DECEL_RATIO,
                        "dead_ratio": DEAD_RATIO,
                    },
                    "confirm_cycles": {
                        "decel": CONFIRM_CYCLES_DECEL,
                        "dead": CONFIRM_CYCLES_DEAD,
                    },
                },
                "symbols": self._data,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            # Best-effort: never break the sidecar loop on persistence failure.
            pass

    # -- queries ------------------------------------------------------------

    def get_bucket(self, symbol: str, hour: int) -> dict[str, Any]:
        sym = str(symbol or "").upper()
        return self._data.setdefault(sym, {}).setdefault(
            f"{int(hour):02d}",
            {"ema_rate": 0.0, "samples": 0, "last_update_ts": 0.0},
        )

    def baseline_rate(self, symbol: str, hour: int) -> tuple[float, int]:
        slot = self.get_bucket(symbol, hour)
        return float(slot.get("ema_rate") or 0.0), int(slot.get("samples") or 0)

    # -- updates ------------------------------------------------------------

    def update(self, symbol: str, hour: int, recent_rate: float, now_ts: float | None = None) -> None:
        slot = self.get_bucket(symbol, hour)
        prev_rate = float(slot.get("ema_rate") or 0.0)
        prev_samples = int(slot.get("samples") or 0)
        # Use a smaller alpha during the first few samples so we converge
        # quickly while warm, then settle into the configured EMA factor.
        alpha = EMA_ALPHA if prev_samples >= MIN_BUCKET_SAMPLES else max(EMA_ALPHA, 1.0 / max(1, prev_samples + 1))
        if prev_samples == 0:
            new_rate = max(0.0, float(recent_rate))
        else:
            new_rate = (1.0 - alpha) * prev_rate + alpha * max(0.0, float(recent_rate))
        slot["ema_rate"] = round(new_rate, 6)
        slot["samples"] = prev_samples + 1
        slot["last_update_ts"] = float(now_ts if now_ts is not None else time.time())

    # -- streak / confirmation ---------------------------------------------

    def streak_for(self, symbol: str) -> tuple[str, int]:
        st = self._streaks.get(str(symbol or "").upper())
        if not isinstance(st, dict):
            return (PHASE_NORMAL, 0)
        return (str(st.get("phase") or PHASE_NORMAL), int(st.get("count") or 0))

    def confirm(self, symbol: str, raw_phase: str) -> str:
        """Apply confirmation policy: return the *effective* phase the caller
        should act on. DECEL/DEAD require N consecutive cycles; ACCEL/CAUTION
        fire immediately; NORMAL resets streaks.
        """
        sym = str(symbol or "").upper()
        st = self._streaks.setdefault(sym, {"phase": PHASE_NORMAL, "count": 0})
        prev_phase = str(st.get("phase") or PHASE_NORMAL)
        if raw_phase == prev_phase:
            st["count"] = int(st.get("count") or 0) + 1
        else:
            st["phase"] = raw_phase
            st["count"] = 1

        count = int(st["count"])
        if raw_phase == PHASE_DEAD and count < CONFIRM_CYCLES_DEAD:
            # Soften to DECEL while confirming the death.
            return PHASE_DECEL
        if raw_phase == PHASE_DECEL and count < CONFIRM_CYCLES_DECEL:
            return PHASE_CAUTION
        return raw_phase


# ---------------------------------------------------------------------------
# Pure helpers (no I/O) — easy to unit test if needed.
# ---------------------------------------------------------------------------

def hour_bucket_of(ts: float | None = None) -> int:
    t = float(ts) if isinstance(ts, (int, float)) else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).hour


def spikes_per_min(
    spike_timestamps: Iterable[float],
    window_sec: int,
    now_ts: float | None = None,
) -> tuple[float, int]:
    """Return (rate_per_minute, sample_count) for the trailing window."""
    t_now = float(now_ts if now_ts is not None else time.time())
    cutoff = t_now - max(60, int(window_sec))
    count = 0
    for ts in spike_timestamps:
        try:
            tv = float(ts)
        except Exception:
            continue
        if tv >= cutoff:
            count += 1
    minutes = max(1.0, float(window_sec) / 60.0)
    return (count / minutes, count)


def classify(
    recent_rate: float,
    baseline_rate: float | None,
    samples_baseline: int,
) -> tuple[str, float | None, str]:
    """Return (raw_phase, ratio, reason).

    The caller is responsible for applying confirmation streaks.
    """
    if baseline_rate is None or samples_baseline < MIN_BUCKET_SAMPLES:
        return (PHASE_NORMAL, None, f"warming_up samples={samples_baseline}/{MIN_BUCKET_SAMPLES}")
    if baseline_rate < MIN_BASELINE_RATE:
        return (PHASE_NORMAL, None, f"baseline_too_low {baseline_rate:.4f}")
    ratio = float(recent_rate) / float(baseline_rate)
    if ratio >= ACCEL_RATIO:
        return (PHASE_ACCEL, ratio, f"ratio>={ACCEL_RATIO:.2f}")
    if ratio <= DEAD_RATIO:
        return (PHASE_DEAD, ratio, f"ratio<={DEAD_RATIO:.2f}")
    if ratio <= DECEL_RATIO:
        return (PHASE_DECEL, ratio, f"ratio<={DECEL_RATIO:.2f}")
    if ratio <= CAUTION_RATIO:
        return (PHASE_CAUTION, ratio, f"ratio<={CAUTION_RATIO:.2f}")
    return (PHASE_NORMAL, ratio, "in_band")


# ---------------------------------------------------------------------------
# Multi-window + slope helpers (new).
# ---------------------------------------------------------------------------

def compute_windows(
    spike_timestamps: Iterable[float],
    now_ts: float | None = None,
) -> dict[str, float]:
    """Return spikes-per-minute at each of the configured TREND_WINDOWS_SEC.

    The iterable is materialised once so callers that pass a generator do not
    run out on the second pass.
    """
    ts_list = [float(x) for x in spike_timestamps if isinstance(x, (int, float))]
    out: dict[str, float] = {}
    for label, win in zip(TREND_LABELS, TREND_WINDOWS_SEC):
        rate, _n = spikes_per_min(ts_list, win, now_ts)
        out[label] = round(rate, 4)
    return out


def slope_signal_from(
    windows: dict[str, float],
    spike_timestamps: Iterable[float] | None = None,
    now_ts: float | None = None,
) -> tuple[str | None, float | None]:
    """Detect a fast acceleration/deceleration vs the slow window.

    Returns ``(signal, drop_pct)``. signal is one of:
        * "DECEL_FAST_HARD" — fast window dropped >= SLOPE_DROP_HARD_PCT
        * "DECEL_FAST"      — fast window dropped >= SLOPE_DROP_PCT
        * "ACCEL_FAST"      — fast window rose    >= SLOPE_RISE_PCT
        * None              — within the band or slow-window too quiet
    """
    if spike_timestamps is not None:
        ts_list = [float(x) for x in spike_timestamps if isinstance(x, (int, float))]
        fast_rate, _ = spikes_per_min(ts_list, SLOPE_FAST_SEC, now_ts)
        slow_rate, _ = spikes_per_min(ts_list, SLOPE_SLOW_SEC, now_ts)
    else:
        # Map slope windows back onto the TREND vector when possible.
        fast_rate = float(windows.get("w5m", 0.0)) if SLOPE_FAST_SEC <= 600 else 0.0
        slow_rate = float(windows.get("w30m", 0.0)) if SLOPE_SLOW_SEC <= 2400 else 0.0

    if slow_rate < SLOPE_MIN_SLOW_RATE:
        return (None, None)
    delta = (fast_rate - slow_rate) / slow_rate
    if delta <= -SLOPE_DROP_HARD_PCT:
        return ("DECEL_FAST_HARD", round(-delta, 3))
    if delta <= -SLOPE_DROP_PCT:
        return ("DECEL_FAST", round(-delta, 3))
    if delta >= SLOPE_RISE_PCT:
        return ("ACCEL_FAST", round(delta, 3))
    return (None, None)


def trend_counts(windows: dict[str, float]) -> tuple[int, int]:
    """Return (down_count, up_count) across the 3 pair-wise window comparisons.

    Pairs compared: (w5m vs w30m), (w30m vs w2h), (w2h vs w6h).
    A pair is "down" when the shorter window's rate is < (1-band) * longer.
    A pair is "up" when it is > (1+band) * longer. The slower window must
    exceed TREND_DOWN_MIN_RATE for the comparison to count.
    """
    pairs = (("w5m", "w30m"), ("w30m", "w2h"), ("w2h", "w6h"))
    down = up = 0
    for short, long_ in pairs:
        s = float(windows.get(short, 0.0))
        l = float(windows.get(long_, 0.0))
        if l < TREND_DOWN_MIN_RATE:
            continue
        rel = s / l
        if rel <= (1.0 - TREND_FLAT_BAND):
            down += 1
        elif rel >= (1.0 + TREND_FLAT_BAND):
            up += 1
    return (down, up)


# ---------------------------------------------------------------------------
# High-level entry points used by the sidecar.
# ---------------------------------------------------------------------------

def evaluate_symbol(
    symbol: str,
    spike_timestamps: Iterable[float],
    baseline: SessionBaseline,
    now_ts: float | None = None,
) -> MarketPhase:
    """Compute the *effective* phase for ``symbol`` given recent spikes.

    Does NOT mutate the baseline — call ``baseline.update`` separately if you
    want this observation to feed the learning store.
    """
    t_now = float(now_ts if now_ts is not None else time.time())
    hour = hour_bucket_of(t_now)
    # Materialise once for both the legacy window and the new multi-window pass.
    ts_list = [float(x) for x in spike_timestamps if isinstance(x, (int, float))]
    recent_rate, samples_recent = spikes_per_min(ts_list, RECENT_WINDOW_SEC, t_now)
    base_rate, base_samples = baseline.baseline_rate(symbol, hour)
    raw_phase, ratio, reason = classify(recent_rate, base_rate if base_samples > 0 else None, base_samples)

    # Multi-window vector (last 5min / 30min / 2h / 6h).
    windows = compute_windows(ts_list, t_now)
    slope_sig, slope_drop = slope_signal_from(windows, spike_timestamps=ts_list, now_ts=t_now)
    down_count, up_count = trend_counts(windows)

    # Phase escalation rules — applied BEFORE confirmation streak so a sharp
    # intra-window drop reacts on the same cycle (bypass streak).
    bypass = False
    if slope_sig == "DECEL_FAST_HARD":
        raw_phase = PHASE_DECEL
        reason = f"slope_hard drop={slope_drop}"
        bypass = True
    elif slope_sig == "DECEL_FAST" and raw_phase in (PHASE_NORMAL, PHASE_ACCEL):
        raw_phase = PHASE_CAUTION
        reason = f"slope drop={slope_drop}"
        bypass = True
    elif slope_sig == "ACCEL_FAST" and raw_phase == PHASE_NORMAL:
        raw_phase = PHASE_ACCEL
        reason = f"slope rise={slope_drop}"
        bypass = True

    # Multi-window 2-of-3 down rule: harden NORMAL to CAUTION.
    if down_count >= 2 and raw_phase == PHASE_NORMAL:
        raw_phase = PHASE_CAUTION
        reason = f"trend_down={down_count}/3"
        # No bypass here: this is a slow rule, let it confirm naturally.

    if bypass:
        # Reset streak and act immediately.
        baseline._streaks[str(symbol or "").upper()] = {"phase": raw_phase, "count": CONFIRM_CYCLES_DEAD}
        effective = raw_phase
    else:
        effective = baseline.confirm(symbol, raw_phase)
    streak_phase, streak_count = baseline.streak_for(symbol)
    return MarketPhase(
        symbol=str(symbol or "").upper(),
        phase=effective,
        ratio=ratio,
        recent_rate=round(recent_rate, 4),
        baseline_rate=round(base_rate, 4) if base_samples > 0 else None,
        samples_recent=samples_recent,
        samples_baseline=base_samples,
        hour_bucket=hour,
        streak=streak_count if streak_phase == effective else 1,
        reason=reason,
        windows=windows,
        slope_signal=slope_sig,
        slope_drop_pct=slope_drop,
        trend_down_count=down_count,
        trend_up_count=up_count,
        bypass_streak=bypass,
    )


def phase_to_dict(phase: MarketPhase) -> dict[str, Any]:
    out = asdict(phase)
    return out


__all__ = [
    "MarketPhase",
    "SessionBaseline",
    "evaluate_symbol",
    "phase_to_dict",
    "spikes_per_min",
    "classify",
    "compute_windows",
    "slope_signal_from",
    "trend_counts",
    "hour_bucket_of",
    "PHASE_ACCEL",
    "PHASE_NORMAL",
    "PHASE_CAUTION",
    "PHASE_DECEL",
    "PHASE_DEAD",
    "ALL_PHASES",
    "RECENT_WINDOW_SEC",
    "EMA_ALPHA",
    "ACCEL_RATIO",
    "CAUTION_RATIO",
    "DECEL_RATIO",
    "DEAD_RATIO",
    "MIN_BUCKET_SAMPLES",
    "MIN_BASELINE_RATE",
    "CONFIRM_CYCLES_DECEL",
    "CONFIRM_CYCLES_DEAD",
    "SLOPE_FAST_SEC",
    "SLOPE_SLOW_SEC",
    "SLOPE_DROP_PCT",
    "SLOPE_DROP_HARD_PCT",
    "SLOPE_RISE_PCT",
    "TREND_WINDOWS_SEC",
    "TREND_LABELS",
    "TREND_FLAT_BAND",
]
