"""
src/analysis/deriv_analyst.py
─────────────────────────────────────────────────────────────────────────────
Pandas-powered statistical analysis + AI gate for the Deriv synthetic-indices
pipeline.

Why this module exists
──────────────────────
Deriv's synthetic indices (R_50, R_75, R_100) are pure stochastic processes —
they don't react to news, whales or geopolitical events. This means standard
statistical mathematics can find *real edge* that humans cannot compute
manually in real-time.

This module does three things on every evaluation cycle:
  1. Statistical analysis (pandas + numpy):
       - Hurst exponent  — is the series trending (H>0.5) or mean-reverting?
       - Autocorrelation lag-1 — is there momentum in returns?
       - Rolling volatility regime — expanding or compressing?
       - Synthetic OHLCV candles from raw ticks
       - Linear regression strength (slope + R²) — already in risk engine, but
         used here with more ticks for a higher-confidence view
  2. AI gate (OpenRouter — DeepSeek V3 / GPT-4o-mini):
       - Sends a condensed statistical summary as a JSON prompt
       - Receives: approved, confidence (0-1), reason
       - Acts as an independent second opinion on the risk engine's score
  3. Preload service:
       - On daemon startup: fetches 1000 ticks per symbol from `ticks_history`
         so the risk engine is warm from tick 1 (instead of cold for 30+ ticks)
       - Refreshes every 5 minutes to keep context current

Usage (called from main_deriv.py)
──────────────────────────────────
    analyst = DerivAnalyst(settings, deriv_client)
    await analyst.preload_history()   # call once at startup

    # In the evaluation loop (after risk engine score ≥ min_score):
    analysis = await analyst.analyze(symbol, ticks_buffer, score_snapshot)
    if analysis.ai_approved and analysis.ai_confidence >= 0.70:
        # fire order
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.data.deriv_client import DerivClient, DerivClientError
from src.utils.deriv_config import DerivSettings
from src.analysis.pattern_memory_query import query_pattern_memory
from src.analysis.geometric_levels import get_active_structure


_LOGGER = logging.getLogger("deriv.analyst")

# ── Env knobs ────────────────────────────────────────────────────────────────
_AI_GATE_ENABLED    = os.getenv("DERIV_AI_GATE_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
_AI_MIN_CONFIDENCE  = float(os.getenv("DERIV_AI_MIN_CONFIDENCE", "0.70"))
_AI_FAIL_OPEN       = os.getenv("DERIV_AI_FAIL_OPEN", "false").strip().lower() in {"1", "true", "yes", "on"}
_AI_HARD_SCORE_FLOOR = float(os.getenv("DERIV_AI_HARD_SCORE_FLOOR", "6.9"))
_OPENROUTER_KEY     = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
_HISTORY_COUNT      = int(os.getenv("DERIV_HISTORY_TICKS", "1000"))  # ticks fetched on startup
_REFRESH_INTERVAL   = int(os.getenv("DERIV_HISTORY_REFRESH_SEC", "300"))  # 5 min

# ── AI call rate limiting ────────────────────────────────────────────────────
# Synthetic indices are driven by math, not news. The AI gate is used as a
# second-opinion filter, NOT as a real-time market reader.
#
# Cache strategy: snapshot-aware invalidation prevents stale vetoes.
# A cached AI decision is INVALIDATED (even within TTL) if the market state
# has changed significantly: score drift >±0.5, Hurst drift >±0.03, regime
# change, direction change, setup-type change, or ATR drift >15%.
# Special rule: a cached VETO is always invalidated when the current score
# improves by > +0.5 vs the score at veto time.
#
# TTL per setup type (max reuse window even without snapshot drift):
#   micro_scalp : DERIV_AI_CACHE_TTL_SCALP_SEC (default 15 s)
#   smc/trend   : DERIV_AI_CACHE_TTL_TREND_SEC  (default 45 s)
#   default     : DERIV_AI_CACHE_TTL_SEC         (default 60 s)

# ── AI circuit breaker ───────────────────────────────────────────────────────
# If ALL models respond with 403/404 (credential/route failures), disable AI
# calls for 30 minutes to avoid blocking the tick pipeline with I/O wait.
_AI_CIRCUIT_BREAKER_DURATION = int(os.getenv("DERIV_AI_CB_DURATION_SEC", "1800"))  # 30 min
_ai_circuit_open_until: float = 0.0  # module-level; set to time.time()+1800 on trip
# to a JSON log so decisions are auditable and survive restarts.
_AI_CACHE_TTL_SEC       = int(os.getenv("DERIV_AI_CACHE_TTL_SEC", "60"))       # default 60 s
_AI_CACHE_TTL_SCALP_SEC = int(os.getenv("DERIV_AI_CACHE_TTL_SCALP_SEC", "15")) # scalping
_AI_CACHE_TTL_TREND_SEC = int(os.getenv("DERIV_AI_CACHE_TTL_TREND_SEC", "45")) # trend/SMC
_AI_LOG_MAX_ENTRIES = int(os.getenv("DERIV_AI_LOG_MAX", "500"))        # rolling log
_AI_ADAPTIVE_CONFIDENCE_ENABLED = os.getenv(
    "DERIV_AI_ADAPTIVE_CONFIDENCE_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
_AI_ADAPTIVE_REFRESH_SEC = max(
    10,
    int(os.getenv("DERIV_AI_ADAPTIVE_REFRESH_SEC", "30") or 30),
)
_AI_ADAPTIVE_TRADES_LOOKBACK = max(
    8,
    int(os.getenv("DERIV_AI_ADAPTIVE_TRADES_LOOKBACK", "20") or 20),
)
_AI_ADAPTIVE_DECISIONS_LOOKBACK = max(
    20,
    int(os.getenv("DERIV_AI_ADAPTIVE_DECISIONS_LOOKBACK", "120") or 120),
)
_AI_ADAPTIVE_MAX_CONFIDENCE = min(
    0.95,
    max(_AI_MIN_CONFIDENCE, float(os.getenv("DERIV_AI_ADAPTIVE_MAX_CONFIDENCE", "0.92"))),
)
_AI_QUALITY_VETO_ENABLED = os.getenv(
    "DERIV_AI_QUALITY_VETO_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
_AI_QUALITY_VETO_MIN_TRADES = max(
    4,
    int(os.getenv("DERIV_AI_QUALITY_VETO_MIN_TRADES", "8") or 8),
)
_AI_QUALITY_VETO_MIN_DECISIONS = max(
    6,
    int(os.getenv("DERIV_AI_QUALITY_VETO_MIN_DECISIONS", "12") or 12),
)
_AI_QUALITY_VETO_APPROVAL_RATE = max(
    70.0,
    min(float(os.getenv("DERIV_AI_QUALITY_VETO_APPROVAL_RATE", "90") or 90), 100.0),
)
_AI_QUALITY_VETO_WIN_RATE = max(
    0.0,
    min(float(os.getenv("DERIV_AI_QUALITY_VETO_WIN_RATE", "35") or 35), 100.0),
)
_AI_QUALITY_VETO_AVG_PNL = float(os.getenv("DERIV_AI_QUALITY_VETO_AVG_PNL", "0.0") or 0.0)
_AI_QUALITY_VETO_TAIL_TRADES = max(
    3,
    int(os.getenv("DERIV_AI_QUALITY_VETO_TAIL_TRADES", "5") or 5),
)
_AI_QUALITY_VETO_TAIL_WIN_RATE = max(
    0.0,
    min(float(os.getenv("DERIV_AI_QUALITY_VETO_TAIL_WIN_RATE", "25") or 25), 100.0),
)
_AI_QUALITY_VETO_TAIL_AVG_PNL = float(os.getenv("DERIV_AI_QUALITY_VETO_TAIL_AVG_PNL", "-0.03") or -0.03)
_AI_QUALITY_RECOVERY_ENABLED = os.getenv(
    "DERIV_AI_QUALITY_RECOVERY_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
_AI_QUALITY_RECOVERY_LOOKBACK_SEC = max(
    1800,
    int(os.getenv("DERIV_AI_QUALITY_RECOVERY_LOOKBACK_SEC", "7200") or 7200),
)
_AI_QUALITY_RECOVERY_MIN_SPIKES = max(
    2,
    int(os.getenv("DERIV_AI_QUALITY_RECOVERY_MIN_SPIKES", "4") or 4),
)
_AI_QUALITY_RECOVERY_MIN_ENTRY_RATE = max(
    0.0,
    min(float(os.getenv("DERIV_AI_QUALITY_RECOVERY_MIN_ENTRY_RATE", "25") or 25), 100.0),
)
_AI_QUALITY_RECOVERY_MIN_DECISIONS = max(
    2,
    int(os.getenv("DERIV_AI_QUALITY_RECOVERY_MIN_DECISIONS", "5") or 5),
)
_AI_QUALITY_RECOVERY_MIN_AI_APPROVAL = max(
    0.0,
    min(float(os.getenv("DERIV_AI_QUALITY_RECOVERY_MIN_AI_APPROVAL", "68") or 68), 100.0),
)
_AI_QUALITY_RECOVERY_MIN_RECENT_TRADES = max(
    2,
    int(os.getenv("DERIV_AI_QUALITY_RECOVERY_MIN_RECENT_TRADES", "3") or 3),
)
_AI_QUALITY_RECOVERY_RECENT_WIN_RATE = max(
    0.0,
    min(float(os.getenv("DERIV_AI_QUALITY_RECOVERY_RECENT_WIN_RATE", "45") or 45), 100.0),
)
_AI_QUALITY_RECOVERY_RECENT_AVG_PNL = float(
    os.getenv("DERIV_AI_QUALITY_RECOVERY_RECENT_AVG_PNL", "-0.005") or -0.005
)

# ── Market-phase aware override (read sidecar hints file) ───────────────────
# When the per-symbol market-phase guard (computed by the IA sidecar) flags a
# symbol as ACCEL — i.e. its short-window spike rate is materially above its
# own historical baseline for the current UTC hour — we relax the AI-quality
# tail veto and slightly lower the adaptive confidence floor so the symbol can
# trade through a short bleed once the market is genuinely heating up.
_AI_MARKET_PHASE_HINTS_PATH = os.getenv(
    "DERIV_AI_MARKET_PHASE_HINTS_PATH",
    "/data/deriv-logs/deriv_market_phase_hints.json",
)
_AI_MARKET_PHASE_HINTS_TTL_SEC = max(
    30, int(os.getenv("DERIV_AI_MARKET_PHASE_HINTS_TTL_SEC", "180") or 180)
)
_AI_ACCEL_BYPASS_TAIL_VETO = os.getenv(
    "DERIV_AI_ACCEL_BYPASS_TAIL_VETO", "true"
).strip().lower() in {"1", "true", "yes", "on"}
_AI_ACCEL_CONF_RELAX = max(
    0.0,
    min(float(os.getenv("DERIV_AI_ACCEL_CONF_RELAX", "0.05") or 0.05), 0.20),
)

_MARKET_PHASE_HINTS_CACHE: dict[str, Any] = {"loaded_at": 0.0, "symbols": {}}


def _load_market_phase_hints() -> dict[str, dict[str, Any]]:
    """Load the per-symbol market-phase hint file written by the IA sidecar.

    Returns ``{symbol: payload}`` (possibly empty). Uses a small TTL cache so
    the analyst does not hit the disk on every decision.
    """
    now = time.time()
    cached_at = float(_MARKET_PHASE_HINTS_CACHE.get("loaded_at") or 0.0)
    if (now - cached_at) < _AI_MARKET_PHASE_HINTS_TTL_SEC and _MARKET_PHASE_HINTS_CACHE.get("symbols"):
        return _MARKET_PHASE_HINTS_CACHE["symbols"]  # type: ignore[return-value]
    try:
        path = _AI_MARKET_PHASE_HINTS_PATH
        if not path:
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        symbols = body.get("symbols") or {}
        if not isinstance(symbols, dict):
            symbols = {}
        _MARKET_PHASE_HINTS_CACHE["symbols"] = symbols
        _MARKET_PHASE_HINTS_CACHE["loaded_at"] = now
        return symbols
    except FileNotFoundError:
        _MARKET_PHASE_HINTS_CACHE["loaded_at"] = now
        return {}
    except Exception:
        _MARKET_PHASE_HINTS_CACHE["loaded_at"] = now
        return _MARKET_PHASE_HINTS_CACHE.get("symbols") or {}


def _market_phase_for(symbol: str) -> dict[str, Any] | None:
    hints = _load_market_phase_hints()
    payload = hints.get(symbol)
    if isinstance(payload, dict):
        return payload
    return None

# ── Smart cache invalidation thresholds ─────────────────────────────────────
_CACHE_SCORE_DRIFT_THRESHOLD   = float(os.getenv("DERIV_CACHE_SCORE_DRIFT", "0.5"))   # |Δscore| > 0.5
_CACHE_HURST_DRIFT_THRESHOLD   = float(os.getenv("DERIV_CACHE_HURST_DRIFT", "0.03"))  # |ΔH| > 0.03
_CACHE_ATR_DRIFT_THRESHOLD     = float(os.getenv("DERIV_CACHE_ATR_DRIFT", "0.15"))    # |ΔATRpct| > 15%
_CACHE_VETO_SCORE_IMPROVE      = float(os.getenv("DERIV_CACHE_VETO_IMPROVE", "0.5"))  # stale veto threshold
# 2026-06-02 operator directive: a cached confirmation generated BEFORE a spike
# is stale once the spike fires (the ticks-since-last-spike counter resets from
# a large value to ~0). When the live gap drops by more than this many ticks vs
# the cached snapshot, a NEW spike happened → invalidate so the bot must hunt a
# fresh post-spike confirmation instead of entering on the pre-spike one.
_CACHE_SPIKE_GAP_RESET_MIN     = int(os.getenv("DERIV_CACHE_SPIKE_GAP_RESET_MIN", "30"))

# Model preference order (all via OpenRouter — verified production model IDs)
# google/gemini-2.5-flash               — fastest, cheapest (~$0.15/M); stable ID (preview-05-20 is deprecated)
# openai/gpt-4o-mini                    — reliable backup (~$0.15/M)
# anthropic/claude-3-5-haiku            — third fallback (~$0.80/M)
_AI_MODELS: list[str] = [
    m.strip()
    for m in os.getenv(
        "DERIV_AI_MODELS",
        "google/gemini-2.5-flash,openai/gpt-4o-mini,anthropic/claude-3-5-haiku",
    ).split(",")
    if m.strip()
]


# ── Output dataclass ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class DerivAnalysis:
    symbol: str
    # Statistical fields
    hurst: float                    # Hurst exponent (0-1). >0.5 = trending
    autocorr_lag1: float            # autocorrelation of returns at lag 1
    vol_regime: str                 # 'expanding' | 'compressing' | 'normal'
    rolling_vol: float              # last rolling std of returns (20-tick window)
    trend_slope_1000: float         # OLS slope over the full history window
    r_squared_1000: float           # R² of linear fit over full window
    n_ticks: int                    # number of ticks used
    candles: list[dict] = field(default_factory=list)  # synthetic OHLCV candles
    # AI gate fields
    ai_approved: bool = False
    ai_confidence: float = 0.0
    ai_reason: str = "ai_gate_disabled"
    ai_model: str = ""
    ai_skipped: bool = False        # True if AI gate disabled or timed out
    pattern_memory_context: dict | None = None
    geometric_structure_context: dict | None = None


def _safe_ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            pass
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


# ── Helpers: Hurst exponent via log-prices variance-at-scale method ─────────
def _hurst_rs(prices: np.ndarray, min_n: int = 30) -> float:
    """Estimate the Hurst exponent via variance-of-aggregated-returns scaling.

    For a fractional Brownian motion with Hurst exponent H:
        Var( sum_{i=1..k} r_i )  ~  k^(2H)
    => std( aggregated_k )  ~  k^H
    => slope of log(std) vs log(k) gives H directly.

    Implementation: build the cumulative return series, then for each lag k
    compute ``cum[k:] - cum[:-k]`` which equals the sum of ``k`` consecutive
    log-returns at every starting index — the proper aggregated-return sample.

    Returns H in [0.20, 0.80]:
      H ≈ 0.5  → random walk / no edge
      H > 0.55 → persistent trend (momentum)
      H < 0.45 → mean-reverting

    NOTE: Previous implementation used ``log_returns[lag:] - log_returns[:-lag]``
    (pairwise return difference) whose std DECREASES with lag, producing a
    persistently negative slope clamped to 0.38 on every symbol. Fixed here.
    """
    import random as _random
    N = len(prices)
    # Warmup guard: fewer than 200 ticks → return neutral transactional value
    if N < 200:
        return float(_random.uniform(0.49, 0.52))

    log_returns = np.diff(np.log(np.array(prices, dtype=float) + 1e-12))
    if log_returns.size < 64 or np.std(log_returns) == 0:
        return 0.50

    # FIX PASO 2.3e-C: spike tick creates extreme log-return → inflates variance
    # at short lags → Hurst rises artificially (+0.107 contamination measured 2.3d-B).
    _lr_std = float(np.std(log_returns))
    if _lr_std > 0:
        _mask = np.abs(log_returns) <= 3.0 * _lr_std
        if int(np.sum(_mask)) >= min_n:
            log_returns = log_returns[_mask]

    # Cumulative log-returns: cum[i] = sum(log_returns[0..i-1])
    cum = np.concatenate(([0.0], np.cumsum(log_returns)))

    max_lag = min(128, log_returns.size // 4)
    if max_lag < 8:
        return 0.50

    # Geometric-ish lag grid covers short + long horizons evenly in log space.
    lag_candidates = [2, 4, 8, 16, 32, 64, max_lag]
    lags: list[int] = []
    tau: list[float] = []
    seen: set[int] = set()
    for lag in lag_candidates:
        if lag in seen or lag <= 1 or lag >= cum.size:
            continue
        seen.add(lag)
        agg = cum[lag:] - cum[:-lag]   # aggregated k-step returns
        s = float(np.std(agg))
        if not np.isfinite(s) or s <= 0:
            continue
        lags.append(lag)
        tau.append(s)

    if len(lags) < 3:
        return 0.50

    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    hurst = float(poly[0])  # slope of log(std) vs log(k) IS H

    # Wide clamp — only guard against pathological numerical blowups.
    return float(max(0.20, min(hurst, 0.80)))


def _autocorr_lag1(prices: np.ndarray) -> float:
    """Autocorrelation of log-returns at lag 1."""
    if len(prices) < 5:
        return 0.0
    returns = np.diff(np.log(prices + 1e-12))
    if len(returns) < 2:
        return 0.0
    r0 = returns[:-1] - returns[:-1].mean()
    r1 = returns[1:]  - returns[1:].mean()
    denom = np.sqrt((r0 ** 2).sum() * (r1 ** 2).sum())
    if denom == 0:
        return 0.0
    return float(np.clip(np.dot(r0, r1) / denom, -1.0, 1.0))


def _ols_slope_r2(prices: np.ndarray) -> tuple[float, float]:
    """OLS linear regression — returns (slope_normalised, R²)."""
    n = len(prices)
    if n < 5:
        return 0.0, 0.0
    x = np.arange(n, dtype=float)
    y = prices
    x_mean = x.mean()
    y_mean = y.mean()
    ss_xy  = np.dot(x - x_mean, y - y_mean)
    ss_xx  = np.dot(x - x_mean, x - x_mean)
    if ss_xx == 0:
        return 0.0, 0.0
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    ss_res = np.sum((y - (slope * x + intercept)) ** 2)
    ss_tot = np.sum((y - y_mean) ** 2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0
    norm_slope = slope / y_mean if y_mean != 0 else 0.0
    return float(norm_slope), float(max(0.0, r2))


def _build_candles(prices: pd.Series, window: int = 50) -> list[dict]:
    """Aggregate tick prices into synthetic OHLC candles."""
    if len(prices) < window:
        return []
    out = []
    groups = prices.groupby(prices.index // window)
    for _, g in groups:
        out.append({
            "open":  float(g.iloc[0]),
            "high":  float(g.max()),
            "low":   float(g.min()),
            "close": float(g.iloc[-1]),
            "n":     len(g),
        })
    return out[-20:]  # keep last 20 candles


def _vol_regime(prices: np.ndarray) -> tuple[str, float]:
    """Classify current volatility regime based on rolling std trend."""
    if len(prices) < 40:
        return "normal", 0.0
    returns = pd.Series(prices).pct_change().dropna()
    rv = returns.rolling(20).std()
    rv_clean = rv.dropna()
    if len(rv_clean) < 2:
        return "normal", 0.0
    last = float(rv_clean.iloc[-1])
    avg  = float(rv_clean.mean())
    if last > avg * 1.3:
        return "expanding", last
    if last < avg * 0.7:
        return "compressing", last
    return "normal", last


# ── AI gate ──────────────────────────────────────────────────────────────────
async def _call_openrouter(prompt: str) -> dict[str, Any]:
    """Call OpenRouter with model cascade. Returns parsed AI result dict.

    Includes a circuit breaker: if all models return 403/404 (credential or
    routing failures), the breaker trips for _AI_CIRCUIT_BREAKER_DURATION
    seconds to avoid burning I/O budget on every tick.
    """
    global _ai_circuit_open_until

    if not _OPENROUTER_KEY:
        return {"approved": False, "confidence": 0.0, "reason": "no_api_key", "model": ""}

    # Circuit breaker: skip remote calls if tripped
    if time.time() < _ai_circuit_open_until:
        remaining = int(_ai_circuit_open_until - time.time())
        _LOGGER.debug("[deriv-analyst] AI circuit open — skipping remote call (%ds remaining)", remaining)
        return {"approved": False, "confidence": 0.0, "reason": "circuit_breaker_open", "model": ""}

    import aiohttp  # inline import — avoids hard dep at module level

    headers = {
        "Authorization": f"Bearer {_OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://optiferre.app",
        "X-Title": "OptiFerre-Deriv",
    }
    hard_fail_count = 0  # 403 / 404 — credential/route failures
    for model in _AI_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 200,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=12.0),
                ) as resp:
                    if resp.status == 429:
                        _LOGGER.warning("[deriv-analyst] AI 429 on %s — skip", model)
                        continue
                    if resp.status in (403, 404):
                        _LOGGER.warning("[deriv-analyst] AI %s HTTP %s (hard fail)", model, resp.status)
                        hard_fail_count += 1
                        continue
                    if resp.status != 200:
                        _LOGGER.warning("[deriv-analyst] AI %s HTTP %s", model, resp.status)
                        continue
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    # Strip markdown code fences if present
                    raw = raw.strip()
                    if raw.startswith("```"):
                        raw = raw.split("```")[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                    parsed = json.loads(raw)
                    parsed["model"] = model
                    # Successful call — ensure circuit is closed
                    _ai_circuit_open_until = 0.0
                    return _normalize_ai(parsed)
        except asyncio.TimeoutError:
            _LOGGER.warning("[deriv-analyst] AI timeout on %s", model)
            continue
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-analyst] AI error on %s: %s", model, exc)
            continue

    # All models failed — check if all failures were hard (403/404)
    if hard_fail_count == len(_AI_MODELS):
        _ai_circuit_open_until = time.time() + _AI_CIRCUIT_BREAKER_DURATION
        _LOGGER.warning(
            "[deriv-analyst] All AI models returned 403/404 — circuit breaker OPEN for %d min",
            _AI_CIRCUIT_BREAKER_DURATION // 60,
        )
    return {"approved": False, "confidence": 0.0, "reason": "all_models_failed", "model": ""}


def _normalize_ai(d: dict) -> dict:
    d.setdefault("approved", False)
    d.setdefault("confidence", 0.0)
    d.setdefault("reason", "")
    d.setdefault("model", "")
    try:
        d["confidence"] = float(d["confidence"])
    except (TypeError, ValueError):
        d["confidence"] = 0.0
    d["approved"] = bool(d.get("approved", False))
    return d


# ── Smart cache snapshot helpers ─────────────────────────────────────────────

def _setup_type_from_breakdown(breakdown: dict) -> str:
    """Classify the setup type from score_breakdown flags."""
    if breakdown.get("micro_scalp"):
        return "micro_scalp"
    if breakdown.get("spike_entry"):
        return "spike"
    if breakdown.get("fvg_active"):
        return "smc"
    if breakdown.get("mean_rev_mode"):
        return "mean_rev"
    return "trend"


def _cache_ttl_for_setup(setup_type: str) -> int:
    """Return the max cache TTL (seconds) for the given setup type."""
    if setup_type == "micro_scalp":
        return _AI_CACHE_TTL_SCALP_SEC
    if setup_type in ("smc", "trend", "spike"):
        return _AI_CACHE_TTL_TREND_SEC
    return _AI_CACHE_TTL_SEC


def _build_cache_snapshot(
    score: float,
    hurst: float,
    side: str | None,
    score_breakdown: dict,
) -> dict:
    """Build a compact snapshot dict representing the market state at cache time."""
    atr_pct = float(score_breakdown.get("atr_pct") or score_breakdown.get("atr") or 0.0)
    regime = str(score_breakdown.get("regime") or "unknown")
    setup_type = _setup_type_from_breakdown(score_breakdown)
    _spike_gap_raw = score_breakdown.get("spike_ticks_since_last")
    try:
        spike_gap = int(_spike_gap_raw) if _spike_gap_raw is not None else None
    except (TypeError, ValueError):
        spike_gap = None
    return {
        "score": round(float(score), 3),
        "score_bucket": round(float(score) * 2) / 2,   # nearest 0.5 bucket
        "hurst": round(float(hurst), 4),
        "hurst_bucket": round(round(hurst * 20) / 20, 3),   # nearest 0.05 bucket
        "side": side or "",
        "regime": regime,
        "setup_type": setup_type,
        "atr_pct": round(atr_pct, 5),
        "spike_gap": spike_gap,
    }


def _should_invalidate_cache(
    cached_snapshot: dict,
    current_snapshot: dict,
    cached_result: dict,
) -> tuple[bool, str]:
    """Return (should_invalidate, reason_string).

    Called BEFORE returning a cached AI result. If True, the caller must
    re-query the AI with the fresh market state.
    """
    cs = current_snapshot
    ps = cached_snapshot  # 'p' = previous (cached)

    # 1. Score drift > threshold
    score_delta = cs["score"] - ps["score"]
    if abs(score_delta) > _CACHE_SCORE_DRIFT_THRESHOLD:
        return True, (
            f"SCORE_DRIFT: snapshot={ps['score']:.2f} live={cs['score']:.2f} "
            f"Δ={score_delta:+.2f} (thresh±{_CACHE_SCORE_DRIFT_THRESHOLD})"
        )

    # 2. Hurst drift > threshold
    hurst_delta = cs["hurst"] - ps["hurst"]
    if abs(hurst_delta) > _CACHE_HURST_DRIFT_THRESHOLD:
        return True, (
            f"HURST_DRIFT: snapshot={ps['hurst']:.3f} live={cs['hurst']:.3f} "
            f"Δ={hurst_delta:+.3f} (thresh±{_CACHE_HURST_DRIFT_THRESHOLD})"
        )

    # 3. Regime changed
    if cs["regime"] != ps["regime"] and ps["regime"] not in ("unknown", ""):
        return True, f"REGIME_CHANGE: {ps['regime']}→{cs['regime']}"

    # 4. Direction changed
    if cs["side"] != ps["side"] and ps["side"] not in ("", None):
        return True, f"DIRECTION_CHANGE: {ps['side']}→{cs['side']}"

    # 5. Setup type changed
    if cs["setup_type"] != ps["setup_type"]:
        return True, f"SETUP_CHANGE: {ps['setup_type']}→{cs['setup_type']}"

    # 6. ATR changed > 15%
    if ps["atr_pct"] > 1e-8:
        atr_change = abs(cs["atr_pct"] - ps["atr_pct"]) / ps["atr_pct"]
        if atr_change > _CACHE_ATR_DRIFT_THRESHOLD:
            return True, (
                f"ATR_DRIFT: snapshot={ps['atr_pct']:.4f} live={cs['atr_pct']:.4f} "
                f"Δ={atr_change:.0%} (thresh={_CACHE_ATR_DRIFT_THRESHOLD:.0%})"
            )

    # 7. VETO stale: cached was rejected but score improved significantly
    if not cached_result.get("approved", False):
        if cs["score"] > ps["score"] + _CACHE_VETO_SCORE_IMPROVE:
            return True, (
                f"VETO_STALE_SCORE_IMPROVED: veto_score={ps['score']:.2f} "
                f"live_score={cs['score']:.2f} Δ={cs['score']-ps['score']:+.2f} "
                f"(crossed threshold +{_CACHE_VETO_SCORE_IMPROVE})"
            )

    # 8. SPIKE FIRED since the cached confirmation (2026-06-02 operator directive):
    #    the ticks-since-last-spike counter RESET (live gap dropped sharply vs the
    #    cached snapshot). The cached confirmation belonged to the PRE-spike setup
    #    and is now spent — invalidate so the bot re-evaluates from scratch and
    #    only enters on a genuinely NEW post-spike confirmation.
    cur_gap = cs.get("spike_gap")
    prev_gap = ps.get("spike_gap")
    if (
        cur_gap is not None
        and prev_gap is not None
        and prev_gap > 0
        and cur_gap < prev_gap - _CACHE_SPIKE_GAP_RESET_MIN
    ):
        return True, (
            f"SPIKE_FIRED: gap reset {prev_gap}t→{cur_gap}t "
            f"(pre-spike confirmation spent, hunt fresh post-spike signal)"
        )

    return False, ""  # cache still valid


def _build_ai_prompt(
    symbol: str,
    side: str,
    score: float,
    breakdown: dict,
    hurst: float,
    autocorr: float,
    vol_regime: str,
    rolling_vol: float,
    slope: float,
    r2: float,
    n_ticks: int,
    ai_threshold: float = 7.5,
    ai_min_confidence: float | None = None,
    symbol_guard_threshold: float | None = None,
    pattern_memory: dict | None = None,
    geometric_structure: dict | None = None,
    maturity_info: dict | None = None,
) -> str:
    # Spike markets (BOOM/CRASH) don't require trending Hurst — spikes are stochastic.
    # Use the hurst_min_spike value (0.43) for those; trend requirement only for R_*.
    _is_spike = any(x in symbol.upper() for x in ("BOOM", "CRASH"))
    if _is_spike:
        _approve_cond = (
            f"Approve (true) if: score>={ai_threshold} AND Hurst>={0.43:.2f} "
            f"AND regime not 'volatile'. "
            f"NOTE: This is a SPIKE market — Hurst>0.52 (trending) is NOT required. "
            f"Spikes occur at any Hurst value. Focus on score, geo position, and regime."
        )
    else:
        _approve_cond = (
            f"Approve (true) if: score>={ai_threshold} AND Hurst>0.52 "
            f"AND autocorr aligned with side AND regime not 'volatile'."
        )
    _min_conf = max(0.65, float(ai_min_confidence if ai_min_confidence is not None else _AI_MIN_CONFIDENCE))
    _bleed_bonus = float((breakdown or {}).get("symbol_bleed_bonus") or 0.0)
    _bleed_pnl_window = float((breakdown or {}).get("symbol_bleed_pnl_window") or 0.0)
    _bleed_threshold = float(
        symbol_guard_threshold
        if symbol_guard_threshold is not None
        else (breakdown or {}).get("symbol_bleed_threshold")
        or -2.0
    )
    _effective_floor = max(float(ai_threshold), float(ai_threshold) + _bleed_bonus)
    _bleed_rule = (
        f"If symbol_bleed_bonus>0, only approve when score>={_effective_floor:.2f} "
        f"and confidence>={max(_min_conf, 0.75):.2f}."
    )
    # 2026-06-01 ANTI-BUTTERFLY (user directive): the edge is the spike that has
    # NOT happened yet. Confirmations are spent once the spike fires — then the
    # bot must wait for a NEW setup, not chase the burst that already played out.
    _spike_timing_rule = ""
    if _is_spike:
        _spike_timing_rule = (
            "\nSPIKE-TIMING DISCIPLINE (this is a BOOM/CRASH spike market): the only edge is the\n"
            "spike that has NOT fired yet. Read score_breakdown timing fields:\n"
            " - spike_imminence_state=RIPE or BUILDING => loaded to fire soon (good entry window).\n"
            " - spike_imminence_state=FRESH => a spike JUST fired and the move is spent; do NOT chase, "
            "approved=false unless brand-new independent momentum is explicit.\n"
            " - spike_imminence_state=OVERDUE or DRY => statistically late; lower confidence.\n"
            " - spike_cluster_stale present (any value) => the recent burst already fired and went quiet; "
            "entering now is 'chasing butterflies' 3-4 min after the seguidilla. Do NOT approve on the basis "
            "of spike_cluster_n alone when spike_cluster_stale is set.\n"
            "Never approve an entry justified only by spikes that ALREADY occurred. Confirmations are for the "
            "NEXT spike; if the spike already passed, wait for another.\n"
        )
    # ── Dual-path structural context string ───────────────────────────────
    _struct_ctx = ""
    if breakdown:
        _sf_active  = breakdown.get("structural_fvg_active")
        _sf_dir     = breakdown.get("structural_fvg_direction", "")
        _sf_confirm = breakdown.get("structural_fvg_confirm")
        _sf_conflict= breakdown.get("structural_fvg_conflict")
        _sf_absent  = breakdown.get("structural_fvg_absent")
        _fvg_anchor = breakdown.get("fvg_anchor_active")
        _atr_anch   = breakdown.get("atr_anchored")
        _geo_nullif = breakdown.get("geo_post_spike_nullified")
        _parts = []
        if _sf_confirm:
            _parts.append(f"structural_fvg_CONFIRMED: 5m candle FVG agrees with tick FVG (dir={_sf_dir}) → HIGH CONVICTION")
        elif _sf_conflict:
            _tick_dir = breakdown.get("fvg_direction", "?")
            _parts.append(f"structural_fvg_CONFLICT: tick FVG dir={_tick_dir} but 5m candle FVG dir={_sf_dir} → REDUCE CONVICTION")
        elif _sf_absent:
            _parts.append("structural_fvg_ABSENT: tick FVG detected but no matching 5m candle FVG → TICK-ONLY signal")
        elif _sf_active is False:
            _parts.append("structural_fvg_NONE: no FVG visible on 5m candle structure")
        if _fvg_anchor:
            _age = breakdown.get("fvg_anchor_age_s", 0)
            _parts.append(f"fvg_anchor_ACTIVE: spike FVG coordinates frozen ({_age:.0f}s ago) — retroceso window filling but structural level still valid")
        if _atr_anch:
            _parts.append(f"atr_ANCHORED: rolling ATR suppressed by retroceso — using pre-spike ATR blend (pre={breakdown.get('atr_pre_spike','?')}, blended={breakdown.get('atr_blended','?')})")
        if _geo_nullif is not None:
            _parts.append(f"geo_post_spike_NULLIFIED: Hurst/slope delta of {_geo_nullif:+.2f} suppressed (retroceso bias, wrong direction)")
        if _parts:
            _struct_ctx = (
                "\nDUAL-PATH STRUCTURAL ANALYSIS:\n"
                + "\n".join(f"  - {p}" for p in _parts)
                + "\nNOTE: Structural path uses 5m candle closes (immune to tick retroceso). "
                + "Always weight structural_fvg_CONFIRMED above tick-level FVG alone."
            )

    # ── RNG probability context (probabilistic scoring engine) ────────────────
    _rng_prob = int((breakdown or {}).get("rng_probability", -1))
    _rng_missing = list((breakdown or {}).get("rng_missing") or [])
    _rng_threshold = int((breakdown or {}).get("rng_threshold", 65))
    _has_rng = _rng_prob >= 0
    if _has_rng:
        _rng_pct_str = f"{_rng_prob}/100"
        _rng_missing_str = ", ".join(_rng_missing) if _rng_missing else "ninguna"
        _rng_section = (
            f"\nRNG ALIGNMENT PROBABILITY: {_rng_pct_str} (threshold={_rng_threshold})\n"
            f"Missing confirmations: {_rng_missing_str}\n"
            f"This score reached you because {_rng_prob}>={_rng_threshold} — the math already\n"
            f"validated sufficient asymmetry. Your role is NOT to require perfection.\n"
            f"Your role: evaluate whether the MISSING confirmations are CRITICAL in this\n"
            f"micro-context, or whether the present confirmations provide enough kinetic\n"
            f"inertia to justify the entry. Imperfect setups with strong kinetic compression\n"
            f"and structural BOS are statistically valid — Deriv RNG operates on Poisson\n"
            f"intervals and does not require macro alignment to spike.\n"
            f"Only veto if the missing items represent a DIRECT contradiction (e.g. price\n"
            f"in wrong zone, active COOLDOWN, or score breakdown shows structural conflict).\n"
        )
        if _is_spike:
            _approve_cond = (
                f"Approve (true) if: score>={ai_threshold} AND the present confirmations\n"
                f"provide meaningful kinetic or structural edge.\n"
                f"NOTE: {_rng_prob}/100 RNG alignment already passed math gate ({_rng_threshold} threshold).\n"
                f"MISSING: {_rng_missing_str}. Only veto if these missing items are\n"
                f"DIRECTLY contradicted by other signals in the breakdown (not just absent).\n"
                f"Absence ≠ veto. Contradiction = veto."
            )
    else:
        _rng_section = ""

    # ── EXTREME_OVERPRESSURE context (FASE 2) ────────────────────────────────
    # Injected when elasticity logic lowered the regime threshold due to high
    # scarcity ratio (spike very overdue). The LLM must NOT veto on missing
    # structure when overpressure justifies the entry.
    _el_z_bd   = float((breakdown or {}).get("elasticity_z") or 0.0)
    _mkt_ctx   = str((breakdown or {}).get("market_context") or "")
    _op_ctx    = ""
    if _mkt_ctx == "EXTREME_OVERPRESSURE_KINETIC_LOCKED":
        # FASE 4: Triple lock fired — implied structure approved, FVG absent but
        # physical kinetic compression + extreme pressure justify the entry.
        _tl_z      = float((breakdown or {}).get("triple_lock_z") or _el_z_bd)
        _tl_el     = float((breakdown or {}).get("triple_lock_elapsed") or 0.0)
        _tl_kin    = float((breakdown or {}).get("triple_lock_kinetic") or 0.0)
        _tl_orig   = float((breakdown or {}).get("elasticity_regime_min_original") or 0.0)
        _tl_new    = float((breakdown or {}).get("elasticity_regime_min_elastic") or 0.0)
        _op_ctx    = (
            f"\n🔒 EXTREME_OVERPRESSURE_KINETIC_LOCKED (Z=+{_tl_z:.2f} elapsed={_tl_el:.0f}s):\n"
            "The TRIPLE LOCK has FIRED. Three independent conditions were verified simultaneously:\n"
            "  A) No real FVG structure found in the dynamic anchor window.\n"
            f"  B) Extreme RNG tension — Z={_tl_z:.2f} OR elapsed {_tl_el:.0f}s vs symbol limit.\n"
            f"  C) Active kinetic compression (score={_tl_kin:.3f}) — price is NOT free-falling.\n"
            f"Threshold lowered: {_tl_orig:.2f} → {_tl_new:.2f} (implied structure bonus +30 pts applied).\n"
            "MANDATORY: DO NOT veto for absence of FVG, BOS, or below-normal score.\n"
            "The physical kinetic state REPLACES the structural requirement.\n"
            "APPROVE if: no active COOLDOWN AND no visible post-spike retrace.\n"
            "DENY ONLY if: COOLDOWN is active OR kinetic is decelerating into a knife-catch.\n"
        )
    elif _el_z_bd >= 2.0 or _mkt_ctx == "EXTREME_OVERPRESSURE":
        _el_orig = float((breakdown or {}).get("elasticity_regime_min_original") or 0.0)
        _el_new  = float((breakdown or {}).get("elasticity_regime_min_elastic") or 0.0)
        _op_ctx  = (
            f"\n⚠️ EXTREME_OVERPRESSURE (Z=+{_el_z_bd:.2f}): "
            f"regime threshold lowered {_el_orig:.2f}→{_el_new:.2f}.\n"
            "The spike is statistically VERY OVERDUE. Deriv RNG accumulates internal\n"
            "tension during droughts that compensates for missing structural confirmation.\n"
            "DO NOT veto for missing FVG or below-normal score alone.\n"
            "APPROVE if: any kinetic compression is visible AND no active COOLDOWN.\n"
            "Absence of FVG ≠ bad entry. Extreme temporal tension = high spike probability.\n"
        )

    # ── V2 Scarcity cycle context (PASO 2.3e-G 2026-06-16) ──────────────────────
    # Previously the LLM had no visibility into the V2 ceiling/bucket/compound system.
    # Now we inject the cycle position so the LLM can weight entries by scarcity maturity.
    _sb          = breakdown or {}
    _v2_ceiling  = _sb.get("ceiling_value_s")
    _v2_src      = str(_sb.get("ceiling_source") or "unknown")
    _v2_ratio    = _sb.get("progressive_imminence_ratio")
    _v2_bucket   = str(_sb.get("progressive_imminence_bucket") or "")
    _v2_compound = str(_sb.get("hurst_time_compound_reason") or "neutral")
    _v2_elapsed  = _sb.get("scarcity_elapsed_s")
    _v2_ctx = ""
    if _v2_bucket and _v2_ceiling is not None and _v2_ratio is not None:
        _v2_ctx = (
            f"\nSCARCITY_CYCLE (V2 system):\n"
            f"- Ceiling (expected max spike interval): {_v2_ceiling:.0f}s (source={_v2_src})\n"
            f"- Elapsed since last spike: {_v2_elapsed:.0f}s\n"
            f"- Ratio elapsed/ceiling: {_v2_ratio:.2f}\n"
            f"- Bucket: {_v2_bucket}\n"
            "  Bucket guide: fresh(0-20%%)=spike very recent; warming(20-40%%)=early;"
            " medium(40-60%%)=reasonable; high(60-80%%)=good; very_high(80-100%%)=imminent;"
            " overdue_extreme(>100%%)=spike statistically overdue\n"
            f"- Compound Hurst+Time signal: {_v2_compound}\n"
            "  Signals: agotado_cerca_techo=STRONG spike imminent;"
            " trending_spike_lejano=spike unlikely soon;"
            " trending_cerca_techo=ambiguous; sin_fuerza_muy_pronto=premature; neutral=no signal\n"
            "\nV2 OPERATIONAL RULES (bucket = confidence factor, not binary filter):\n"
            '- bucket="fresh" (0-20%%): DO NOT APPROVE unless score>=8.0 AND compound=="agotado_cerca_techo".'
            ' (spike very recent, retrace expected)\n'
            '- bucket="warming" (20-40%%): APPROVE CAUTIOUSLY — require score>=7.0 OR confidence>=0.85.'
            ' (still early in cycle)\n'
            '- bucket="medium" (40-60%%): APPROVE NORMALLY — apply usual threshold.\n'
            '- bucket="high" (60-80%%): APPROVE WITH BONUS — reduce effective score threshold by 0.5.'
            ' (high probability zone)\n'
            '- bucket="very_high" (80-100%%): APPROVE WITH LARGE BONUS — reduce effective score threshold by 1.0.'
            ' (spike imminent)\n'
            '- bucket="overdue_extreme" (>100%%): EVALUATE CAREFULLY — approve but consider anomalous cluster risk.\n'
            '\nCOMPOUND MODIFIER:\n'
            '- compound=="agotado_cerca_techo": +1 confidence level (strong spike imminent signal).\n'
            '- compound=="trending_spike_lejano": -1 confidence level (spike unlikely soon).\n'
        )

    # ── PASO 2.3h-prep Fase C: Pattern Memory section ────────────────────────
    _pm_ctx = ""
    if pattern_memory and pattern_memory.get("enough_data"):
        _pm_wr = float(pattern_memory.get("wr", 0))
        _pm_pnl = float(pattern_memory.get("avg_pnl", 0))
        _pm_n = pattern_memory.get("n_trades", 0)
        _pm_key = pattern_memory.get("pattern_key", "?")
        if _pm_wr >= 60 and _pm_pnl > 0:
            _pm_signal = "PATRÓN GANADOR HISTÓRICO — favorece entrada"
        elif _pm_wr < 45 or _pm_pnl < -0.05:
            _pm_signal = "PATRÓN PERDEDOR HISTÓRICO — desfavorece entrada"
        else:
            _pm_signal = "marginal — requiere confluencia adicional"
        _pm_ctx = (
            f"\n\nPATTERN MEMORY (histórico real de este patrón exacto):\n"
            f"- Patrón: {_pm_key}\n"
            f"- N trades históricos: {_pm_n}\n"
            f"- WR histórico: {_pm_wr:.0f}%%\n"
            f"- Avg PnL: ${_pm_pnl:+.3f}\n"
            f"- Señal: {_pm_signal}\n"
            "USA ESTO COMO FACTOR PRINCIPAL. Es data REAL de cómo ha funcionado este patrón.\n"
        )
    elif pattern_memory:
        _pm_ctx = (
            f"\n\nPATTERN MEMORY: insuficiente data "
            f"({pattern_memory.get('reason', 'unknown')}) — no usar como factor.\n"
        )

    # ── PASO 2.3h-prep Fase D: Geometric structure section ───────────────────
    _geom_ctx = ""
    if geometric_structure:
        _h_levels = geometric_structure.get("horizontal_levels") or []
        _d_lines = geometric_structure.get("diagonal_trendlines") or []
        if _h_levels or _d_lines:
            _h_txt = "\n".join(
                f"  - {h['type']} en {h['price']:.4f} "
                f"({h['distance_pct']:+.2f}%% del precio actual) — {h['n_touches']} toques"
                for h in _h_levels
            ) or "  (ninguno)"
            _d_txt = "\n".join(
                f"  - {d['type']} proyectada en {d['projected_price_now']:.4f} "
                f"({d['distance_pct']:+.2f}%% del precio actual) — {d['n_touches']} toques objetivos"
                for d in _d_lines
            ) or "  (ninguna)"
            _geom_ctx = (
                "\n\nESTRUCTURA GEOMÉTRICA (auto-detectada):\n"
                f"Niveles horizontales:\n{_h_txt}\n"
                f"Trendlines diagonales:\n{_d_txt}\n"
                "INTERPRETACIÓN:\n"
                "- <0.3%% de un nivel con 3+ toques = ZONA CRÍTICA (posible rebote o ruptura con spike)\n"
                "- Más toques = nivel más significativo\n"
                "- Usa como confirmación estructural del timing de entrada.\n"
            )
        else:
            # G.2 fix: always include section so LLM knows analysis ran
            _n_h = geometric_structure.get("n_total_horizontals", 0)
            _n_d = geometric_structure.get("n_total_diagonals", 0)
            _geom_ctx = (
                "\n\nESTRUCTURA GEOMÉTRICA (auto-detectada):\n"
                f"No se detectaron niveles con N≥3 toques en la ventana actual "
                f"(horizontales encontrados: {_n_h}, diagonales: {_n_d}).\n"
                "INTERPRETACIÓN: Precio sin zonas S/R estructurales claras — precio en zona libre.\n"
            )
    elif n_ticks is not None and n_ticks < 100:
        _geom_ctx = (
            "\n\nESTRUCTURA GEOMÉTRICA: Insuficientes ticks para análisis "
            f"({n_ticks}<100) — no disponible.\n"
        )

    # ── PASO 2.3h-prep Fase E: Maturity informative section ──────────────────
    _mat_ctx = ""
    if maturity_info and maturity_info.get("elapsed_s") is not None:
        _me = float(maturity_info.get("elapsed_s") or 0)
        _mt = float(maturity_info.get("threshold_s") or 1)
        _mr = float(maturity_info.get("ratio") or (_me / _mt if _mt else 0))
        _ms = str(maturity_info.get("status") or "unknown")
        _mat_ctx = (
            "\n\nMATURITY STATUS (informativo — NO bloqueante salvo ratio<0.10):\n"
            f"- Tiempo desde último spike: {_me:.0f}s\n"
            f"- Threshold de maduración: {_mt:.0f}s\n"
            f"- Ratio actual: {_mr:.2f} ({_ms})\n"
            "  extremely_early(<0.30): muy prematuro, requiere confluencia EXCEPCIONAL\n"
            "  early(0.30-0.60): prematuro, requiere confluencia ALTA\n"
            "  ready(0.60-1.0): zona madura, criterios habituales\n"
            "  mature(>1.0): maduro\n"
            "RECUERDA: spike PRNG no tiene memoria. Si hay confluencia FUERTE (Pattern Memory "
            "+ V2 + estructura + score alto), puedes aprobar incluso en early.\n"
        )

    return f"""You are a quantitative trading assistant evaluating a trade signal on a Deriv synthetic volatility index.

SYMBOL: {symbol}
PROPOSED DIRECTION: {side} (MULTUP=long, MULTDOWN=short)

MATHEMATICAL SCORING (out of 10): {score:.2f}
Score breakdown: {json.dumps(breakdown)}{_op_ctx}{_struct_ctx}{_rng_section}{_v2_ctx}{_pm_ctx}{_geom_ctx}{_mat_ctx}

SYMBOL_GUARDRAIL:
- symbol_bleed_bonus: {_bleed_bonus:.2f}
- symbol_bleed_pnl_window: {_bleed_pnl_window:.3f}
- symbol_bleed_threshold: {_bleed_threshold:.2f}

STATISTICAL ANALYSIS ({n_ticks} ticks):
- Hurst exponent: {hurst:.4f} (>0.5=trending, 0.5=random, <0.5=mean-reverting)
- Autocorrelation lag-1: {autocorr:.4f} (positive=momentum, negative=reversal)
- Volatility regime: {vol_regime}
- Rolling volatility (20t): {rolling_vol:.6f}
- OLS slope (normalised): {slope:.6f}
- R² of linear fit: {r2:.4f}

CONTEXT: This is a STATISTICAL synthetic index (not crypto, not forex). It has no macro exposure.
Deriv spike indices are Poisson-distributed RNG — no entry is ever "perfect." Edge comes
from statistical tilt: kinetic compression, structural BOS, cycle maturity, scarcity.
The goal is asymmetric probability, not certainty.

Respond ONLY with a JSON object:
{{"approved": true/false, "confidence": 0.0-1.0, "reason": "one sentence"}}

{_approve_cond}
{_bleed_rule}
{_spike_timing_rule}
Do NOT approve if confidence <{_min_conf:.2f} or if mathematical signals conflict."""


# ── Main analyst class ───────────────────────────────────────────────────────
class DerivAnalyst:
    """Pandas + AI analysis engine for Deriv synthetic indices."""

    def __init__(self, settings: DerivSettings, client: DerivClient) -> None:
        self._settings = settings
        self._client   = client
        # Per-symbol tick history cache (populated by preload_history)
        self._history: dict[str, list[float]] = {}
        self._history_ts: dict[str, float] = {}
        # ── AI response cache ─────────────────────────────────────────────
        # Per-symbol: {ts: float, result: dict}
        # The AI is consulted at most once per _AI_CACHE_TTL_SEC window per symbol.
        # Between consultations, the cached result is returned immediately.
        self._ai_cache: dict[str, dict[str, Any]] = {}
        # Path to the AI decision log JSON (last N entries, rolling).
        self._ai_log_path: Path = settings.logs_dir / "deriv_ai_decisions.json"
        self._closed_log_path: Path = settings.logs_dir / "deriv_closed_contracts.json"
        self._spike_log_path: Path = settings.logs_dir / "deriv_spike_events.json"
        # PASO 2.3h-prep: DB URL para Pattern Memory + prompt audit
        self._db_url: str = os.getenv("DATABASE_URL", "").strip()
        self._prompt_audit_enabled: bool = os.getenv(
            "DERIV_PROMPT_AUDIT_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._prompt_audit_path: Path = settings.logs_dir / "llm_prompts_audit.jsonl"
        self._ai_quality_cache_ts: float = 0.0
        self._ai_quality_cache: dict[str, dict[str, Any]] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Startup preload + background refresh
    # ─────────────────────────────────────────────────────────────────────────
    async def preload_history(self) -> None:
        """Fetch ticks_history for all configured symbols.

        Called once at daemon startup so the risk engine is warm from tick 1.
        """
        tasks = [self._fetch_and_cache(sym) for sym in self._settings.symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, result in zip(self._settings.symbols, results):
            if isinstance(result, Exception):
                _LOGGER.warning("[deriv-analyst] preload failed for %s: %s", sym, result)
            else:
                _LOGGER.info("[deriv-analyst] preloaded %d ticks for %s", len(self._history.get(sym, [])), sym)

    async def history_refresh_loop(self) -> None:
        """Background task: re-fetch tick history every `_REFRESH_INTERVAL` seconds."""
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL)
            for sym in self._settings.symbols:
                try:
                    await self._fetch_and_cache(sym)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("[deriv-analyst] refresh failed for %s: %s", sym, exc)

    async def _fetch_and_cache(self, symbol: str) -> None:
        try:
            data = await self._client.ticks_history(symbol, count=_HISTORY_COUNT)
            prices = data.get("prices") or []
            if len(prices) >= 30:
                self._history[symbol] = prices
                self._history_ts[symbol] = time.time()
        except DerivClientError as exc:
            _LOGGER.warning("[deriv-analyst] ticks_history %s: %s", symbol, exc)

    def ingest_live_tick(self, symbol: str, price: float) -> None:
        """Append a live tick to the symbol's history buffer.

        Called from the daemon on every on_tick event so the history
        stays current even between refresh cycles.
        """
        buf = self._history.setdefault(symbol, [])
        buf.append(price)
        # Keep at most 2× the requested history to bound memory
        if len(buf) > _HISTORY_COUNT * 2:
            del buf[: len(buf) - _HISTORY_COUNT]

    # ─────────────────────────────────────────────────────────────────────────
    # AI cache helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _ai_cache_get(
        self,
        symbol: str,
        current_snapshot: dict | None = None,
    ) -> dict[str, Any] | None:
        """Return cached AI result if still fresh AND snapshot is still valid.

        Implements snapshot-aware cache invalidation:
        - If TTL expired for the setup type → miss
        - If snapshot drifted beyond thresholds → INVALIDATED (logs reason)
        - If cached was VETO and score improved > threshold → INVALIDATED
        """
        entry = self._ai_cache.get(symbol)
        if entry is None:
            return None
        age = time.time() - entry["ts"]
        # TTL based on setup type of the CACHED result
        cached_result = entry["result"]
        cached_snapshot = entry.get("snapshot") or {}
        setup_type = cached_snapshot.get("setup_type", "trend")
        ttl = _cache_ttl_for_setup(setup_type)
        if age > ttl:
            _LOGGER.debug(
                "[deriv-analyst] CACHE_MISS %s age=%.0fs > ttl=%.0fs setup=%s",
                symbol, age, ttl, setup_type,
            )
            return None
        # Snapshot drift check (only if current snapshot provided)
        if current_snapshot and cached_snapshot:
            should_invalidate, reason = _should_invalidate_cache(
                cached_snapshot, current_snapshot, cached_result,
            )
            if should_invalidate:
                _LOGGER.info(
                    "[AI_CACHE] CACHE_INVALIDATED %s | age=%.0fs | reason=%s | "
                    "cached_score=%.2f live_score=%.2f cached_hurst=%.3f live_hurst=%.3f",
                    symbol, age, reason,
                    cached_snapshot.get("score", 0), current_snapshot.get("score", 0),
                    cached_snapshot.get("hurst", 0), current_snapshot.get("hurst", 0),
                )
                del self._ai_cache[symbol]  # evict invalidated entry
                return None
        _LOGGER.info(
            "[AI_CACHE] CACHE_HIT %s | age=%.0fs/ttl=%.0fs | approved=%s conf=%.2f "
            "snapshot_score=%.2f live_score=%.2f snapshot_hurst=%.3f live_hurst=%.3f",
            symbol, age, ttl,
            cached_result.get("approved"), cached_result.get("confidence", 0),
            cached_snapshot.get("score", 0), current_snapshot.get("score", 0) if current_snapshot else 0,
            cached_snapshot.get("hurst", 0), current_snapshot.get("hurst", 0) if current_snapshot else 0,
        )
        return cached_result

    async def _ai_cache_set(
        self,
        symbol: str,
        result: dict[str, Any],
        context: dict[str, Any],
        snapshot: dict | None = None,
    ) -> None:
        """Store AI result in memory cache with snapshot state and dispatch log write.

        The in-memory update is instant (μs). The disk write is offloaded to the
        thread-pool executor so synchronous file I/O never blocks the event loop.
        """
        self._ai_cache[symbol] = {
            "ts": time.time(),
            "result": result,
            "snapshot": snapshot or {},
        }
        _LOGGER.info(
            "[AI_CACHE] CACHE_REFRESH %s | approved=%s conf=%.2f model=%s | "
            "CACHE_REASON=fresh_query | score=%.2f hurst=%.3f regime=%s setup=%s",
            symbol,
            result.get("approved"), result.get("confidence", 0), result.get("model", "?"),
            (snapshot or {}).get("score", 0), (snapshot or {}).get("hurst", 0),
            (snapshot or {}).get("regime", "?"), (snapshot or {}).get("setup_type", "?"),
        )
        # Fire-and-forget: schedule disk write without blocking the tick loop.
        asyncio.get_event_loop().run_in_executor(
            None, self._append_ai_log, symbol, result, context, snapshot,
        )

    def _append_ai_log(
        self,
        symbol: str,
        result: dict[str, Any],
        context: dict[str, Any],
        snapshot: dict | None = None,
    ) -> None:
        """Append one AI decision to the rolling JSON log (bounded at _AI_LOG_MAX_ENTRIES)."""
        try:
            self._ai_log_path.parent.mkdir(parents=True, exist_ok=True)
            existing: list[dict] = []
            if self._ai_log_path.exists():
                try:
                    existing = json.loads(self._ai_log_path.read_text())
                except (json.JSONDecodeError, OSError):
                    existing = []
            entry: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "approved": result.get("approved"),
                "confidence": result.get("confidence"),
                "reason": result.get("reason"),
                "model": result.get("model"),
                **{k: v for k, v in context.items() if k in ("hurst", "autocorr", "score", "vol_regime", "regime", "setup_type")},
            }
            # 2026-05-29: ensure `regime` is always present (alias from vol_regime
            # or snapshot) so feedback_24h/audits don't see "?".
            if "regime" not in entry or entry.get("regime") in (None, "", "?"):
                _reg = context.get("regime") or context.get("vol_regime")
                if (_reg in (None, "", "?")) and snapshot:
                    _reg = snapshot.get("regime") or snapshot.get("vol_regime")
                entry["regime"] = _reg if _reg not in (None, "") else "?"
            if snapshot:
                entry["snapshot"] = snapshot
            existing.append(entry)
            # Rolling: keep last N
            if len(existing) > _AI_LOG_MAX_ENTRIES:
                existing = existing[-_AI_LOG_MAX_ENTRIES:]
            tmp = self._ai_log_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(existing, indent=None, separators=(",", ":")))
            tmp.replace(self._ai_log_path)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("[deriv-analyst] ai_log write failed: %s", exc)

    def _load_json_list(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _refresh_ai_quality_cache(self) -> None:
        if not _AI_ADAPTIVE_CONFIDENCE_ENABLED:
            self._ai_quality_cache = {}
            self._ai_quality_cache_ts = time.time()
            return

        now = time.time()
        if (now - self._ai_quality_cache_ts) < _AI_ADAPTIVE_REFRESH_SEC:
            return

        decisions = self._load_json_list(self._ai_log_path)
        closed_rows = self._load_json_list(self._closed_log_path)
        spike_rows = self._load_json_list(self._spike_log_path)
        recovery_cutoff = now - _AI_QUALITY_RECOVERY_LOOKBACK_SEC

        by_symbol_decisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in decisions:
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            by_symbol_decisions[sym].append(row)

        by_symbol_closed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in closed_rows:
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            by_symbol_closed[sym].append(row)

        by_symbol_spikes_recent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in spike_rows:
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            ts = _safe_ts(row.get("ts") or row.get("timestamp") or row.get("iso"))
            if ts is None or ts < recovery_cutoff:
                continue
            by_symbol_spikes_recent[sym].append(row)

        out: dict[str, dict[str, Any]] = {}
        symbols = set(self._settings.symbols) | set(by_symbol_decisions.keys()) | set(by_symbol_closed.keys())
        for sym in symbols:
            dec_rows = by_symbol_decisions.get(sym, [])[-_AI_ADAPTIVE_DECISIONS_LOOKBACK:]
            close_rows_sym = by_symbol_closed.get(sym, [])[-_AI_ADAPTIVE_TRADES_LOOKBACK:]
            spike_rows_recent = by_symbol_spikes_recent.get(sym, [])

            dec_rows_recent = []
            for r in by_symbol_decisions.get(sym, []):
                r_ts = _safe_ts(r.get("ts") or r.get("timestamp") or r.get("iso"))
                if r_ts is not None and r_ts >= recovery_cutoff:
                    dec_rows_recent.append(r)

            close_rows_recent: list[dict[str, Any]] = []
            for c in by_symbol_closed.get(sym, []):
                c_ts = _safe_ts(c.get("closed_at_ts") or c.get("closed_at") or c.get("opened_at_ts") or c.get("opened_at"))
                if c_ts is not None and c_ts >= recovery_cutoff:
                    close_rows_recent.append(c)

            approvals = sum(1 for r in dec_rows if bool(r.get("approved")))
            decisions_n = len(dec_rows)
            approval_rate = (approvals / decisions_n * 100.0) if decisions_n else 0.0

            pnl_values: list[float] = []
            wins = 0
            weak_exits = 0
            for c in close_rows_sym:
                pnl = c.get("realized_pnl_usdt")
                try:
                    pnl_f = float(pnl)
                except Exception:
                    pnl_f = 0.0
                pnl_values.append(pnl_f)
                if pnl_f > 0.0:
                    wins += 1
                exit_reason = str(c.get("exit_reason") or c.get("close_reason") or "").strip().lower()
                if exit_reason in {"zero_peak_exit", "spike_timeout", "sl_inicial"}:
                    weak_exits += 1

            trades_n = len(close_rows_sym)
            win_rate = (wins / trades_n * 100.0) if trades_n else 0.0
            avg_pnl = (sum(pnl_values) / trades_n) if trades_n else 0.0
            weak_exit_rate = (weak_exits / trades_n * 100.0) if trades_n else 0.0

            tail_n = min(_AI_QUALITY_VETO_TAIL_TRADES, trades_n)
            tail_rows = close_rows_sym[-tail_n:] if tail_n > 0 else []
            tail_pnls: list[float] = []
            tail_wins = 0
            for c in tail_rows:
                try:
                    pnl_tail = float(c.get("realized_pnl_usdt") or 0.0)
                except Exception:
                    pnl_tail = 0.0
                tail_pnls.append(pnl_tail)
                if pnl_tail > 0.0:
                    tail_wins += 1
            tail_win_rate = (tail_wins / tail_n * 100.0) if tail_n else 0.0
            tail_avg_pnl = (sum(tail_pnls) / tail_n) if tail_n else 0.0

            spikes_recent_n = len(spike_rows_recent)
            spikes_recent_entered = sum(1 for s in spike_rows_recent if bool(s.get("bot_entered")))
            spikes_recent_entry_rate = (
                spikes_recent_entered / spikes_recent_n * 100.0
            ) if spikes_recent_n else 0.0

            decisions_recent_n = len(dec_rows_recent)
            approvals_recent = sum(1 for r in dec_rows_recent if bool(r.get("approved")))
            approval_rate_recent = (
                approvals_recent / decisions_recent_n * 100.0
            ) if decisions_recent_n else 0.0

            recent_trades_n = len(close_rows_recent)
            recent_wins = 0
            recent_pnl_values: list[float] = []
            for c in close_rows_recent:
                try:
                    recent_pnl = float(c.get("realized_pnl_usdt") or 0.0)
                except Exception:
                    recent_pnl = 0.0
                recent_pnl_values.append(recent_pnl)
                if recent_pnl > 0.0:
                    recent_wins += 1
            recent_win_rate = (recent_wins / recent_trades_n * 100.0) if recent_trades_n else 0.0
            recent_avg_pnl = (sum(recent_pnl_values) / recent_trades_n) if recent_trades_n else 0.0

            adaptive_conf = float(_AI_MIN_CONFIDENCE)
            reasons: list[str] = []

            if decisions_n >= 12 and approval_rate >= 90.0:
                adaptive_conf += 0.08
                reasons.append(f"approval_rate={approval_rate:.1f}%")
            elif decisions_n >= 12 and approval_rate >= 82.0:
                adaptive_conf += 0.05
                reasons.append(f"approval_rate={approval_rate:.1f}%")

            if trades_n >= 8:
                if win_rate < 35.0:
                    adaptive_conf += 0.08
                    reasons.append(f"win_rate={win_rate:.1f}%")
                if win_rate < 25.0:
                    adaptive_conf += 0.05
                if avg_pnl < 0.0:
                    adaptive_conf += 0.05
                    reasons.append(f"avg_pnl={avg_pnl:.4f}")
                if weak_exit_rate >= 55.0:
                    adaptive_conf += 0.03
                    reasons.append(f"weak_exits={weak_exit_rate:.1f}%")

            if trades_n >= 8 and win_rate >= 55.0 and avg_pnl > 0.0 and approval_rate <= 70.0:
                adaptive_conf -= 0.03

            adaptive_conf = max(_AI_MIN_CONFIDENCE, min(adaptive_conf, _AI_ADAPTIVE_MAX_CONFIDENCE))

            quality_veto = False
            veto_reasons: list[str] = []
            if _AI_QUALITY_VETO_ENABLED:
                if (
                    decisions_n >= _AI_QUALITY_VETO_MIN_DECISIONS
                    and approval_rate >= _AI_QUALITY_VETO_APPROVAL_RATE
                    and trades_n >= _AI_QUALITY_VETO_MIN_TRADES
                    and (win_rate <= _AI_QUALITY_VETO_WIN_RATE or avg_pnl <= _AI_QUALITY_VETO_AVG_PNL)
                ):
                    quality_veto = True
                    veto_reasons.append(
                        f"macro_veto:approval={approval_rate:.1f}% wr={win_rate:.1f}% avg={avg_pnl:.4f}"
                    )
                if (
                    tail_n >= 4
                    and tail_win_rate <= _AI_QUALITY_VETO_TAIL_WIN_RATE
                    and tail_avg_pnl <= _AI_QUALITY_VETO_TAIL_AVG_PNL
                ):
                    quality_veto = True
                    veto_reasons.append(
                        f"tail_veto:n={tail_n} wr={tail_win_rate:.1f}% avg={tail_avg_pnl:.4f}"
                    )

                if quality_veto and _AI_QUALITY_RECOVERY_ENABLED:
                    market_activity_ok = (
                        spikes_recent_n >= _AI_QUALITY_RECOVERY_MIN_SPIKES
                        and spikes_recent_entry_rate >= _AI_QUALITY_RECOVERY_MIN_ENTRY_RATE
                    )
                    ai_flow_ok = (
                        decisions_recent_n >= _AI_QUALITY_RECOVERY_MIN_DECISIONS
                        and approval_rate_recent >= _AI_QUALITY_RECOVERY_MIN_AI_APPROVAL
                    )
                    recent_trade_quality_ok = (
                        recent_trades_n == 0
                        or (
                            recent_trades_n >= _AI_QUALITY_RECOVERY_MIN_RECENT_TRADES
                            and (
                                recent_win_rate >= _AI_QUALITY_RECOVERY_RECENT_WIN_RATE
                                or recent_avg_pnl >= _AI_QUALITY_RECOVERY_RECENT_AVG_PNL
                            )
                        )
                    )

                    if market_activity_ok and ai_flow_ok and recent_trade_quality_ok:
                        quality_veto = False
                        veto_reasons.append(
                            "recovery_override:"
                            f"spikes={spikes_recent_n} entry={spikes_recent_entry_rate:.1f}% "
                            f"ai={approval_rate_recent:.1f}% recent_n={recent_trades_n} "
                            f"recent_wr={recent_win_rate:.1f}% recent_avg={recent_avg_pnl:.4f}"
                        )

                # Market-phase override: when the IA sidecar marks this symbol
                # as ACCEL (short-window spike rate well above its own hourly
                # baseline) we let it trade through a temporary bleed.
                _phase_payload = _market_phase_for(sym) or {}
                _phase_label = str(_phase_payload.get("phase") or "").upper()
                _slope_signal = str(_phase_payload.get("slope_signal") or "").upper()
                if quality_veto and _AI_ACCEL_BYPASS_TAIL_VETO and (
                    _phase_label == "ACCEL" or _slope_signal == "ACCEL_FAST"
                ):
                    quality_veto = False
                    veto_reasons.append(
                        f"market_phase_override:phase={_phase_label or 'n/a'} "
                        f"slope={_slope_signal or 'n/a'}"
                    )
                if _AI_ACCEL_CONF_RELAX > 0.0 and (
                    _phase_label == "ACCEL" or _slope_signal == "ACCEL_FAST"
                ):
                    adaptive_conf = max(
                        _AI_MIN_CONFIDENCE, adaptive_conf - _AI_ACCEL_CONF_RELAX
                    )
                    reasons.append(
                        f"accel_relax=-{_AI_ACCEL_CONF_RELAX:.2f}"
                    )

            out[sym] = {
                "ai_min_confidence": adaptive_conf,
                "quality_note": "adaptive:" + (", ".join(reasons) if reasons else "baseline"),
                "approval_rate": approval_rate,
                "win_rate": win_rate,
                "avg_pnl": avg_pnl,
                "trades_n": trades_n,
                "decisions_n": decisions_n,
                "tail_n": tail_n,
                "tail_win_rate": tail_win_rate,
                "tail_avg_pnl": tail_avg_pnl,
                "spikes_recent_n": spikes_recent_n,
                "spikes_recent_entry_rate": spikes_recent_entry_rate,
                "decisions_recent_n": decisions_recent_n,
                "approval_rate_recent": approval_rate_recent,
                "recent_trades_n": recent_trades_n,
                "recent_win_rate": recent_win_rate,
                "recent_avg_pnl": recent_avg_pnl,
                "quality_veto": quality_veto,
                "quality_veto_note": "; ".join(veto_reasons),
            }

        self._ai_quality_cache = out
        self._ai_quality_cache_ts = now

    def _dynamic_ai_min_confidence(self, symbol: str) -> tuple[float, str]:
        if not _AI_ADAPTIVE_CONFIDENCE_ENABLED:
            return _AI_MIN_CONFIDENCE, "adaptive:disabled"
        self._refresh_ai_quality_cache()
        row = self._ai_quality_cache.get(str(symbol or "").upper())
        if not row:
            return _AI_MIN_CONFIDENCE, "adaptive:baseline"
        return float(row.get("ai_min_confidence", _AI_MIN_CONFIDENCE)), str(
            row.get("quality_note", "adaptive:baseline")
        )

    def _quality_veto_for_symbol(self, symbol: str) -> tuple[bool, str]:
        if not _AI_QUALITY_VETO_ENABLED:
            return False, "quality_veto:disabled"
        self._refresh_ai_quality_cache()
        row = self._ai_quality_cache.get(str(symbol or "").upper())
        if not row:
            return False, "quality_veto:none"
        if bool(row.get("quality_veto")):
            note = str(row.get("quality_veto_note") or "quality_veto")
            return True, note
        return False, "quality_veto:none"

    # ─────────────────────────────────────────────────────────────────────────
    # Core analysis method
    # ─────────────────────────────────────────────────────────────────────────
    async def analyze(
        self,
        symbol: str,
        score: float,
        side: str | None,
        score_breakdown: dict,
    ) -> DerivAnalysis:
        """Run statistical analysis and (optionally) AI gate.

        Returns a DerivAnalysis dataclass with all computed fields.
        """
        prices_list = self._history.get(symbol) or []
        if len(prices_list) < 30:
            # Not enough data — return a neutral result that won't block trading
            return DerivAnalysis(
                symbol=symbol,
                hurst=0.5,
                autocorr_lag1=0.0,
                vol_regime="normal",
                rolling_vol=0.0,
                trend_slope_1000=0.0,
                r_squared_1000=0.0,
                n_ticks=len(prices_list),
                ai_skipped=True,
                ai_reason="insufficient_history",
            )

        prices_np = np.array(prices_list, dtype=float)
        prices_pd = pd.Series(prices_list)

        # ── Statistical computation (pure numpy / pandas, < 10 ms) ──────────
        hurst       = _hurst_rs(prices_np)
        autocorr    = _autocorr_lag1(prices_np)
        vr, rv      = _vol_regime(prices_np)
        slope, r2   = _ols_slope_r2(prices_np)
        candles     = _build_candles(prices_pd)

        analysis = DerivAnalysis(
            symbol=symbol,
            hurst=round(hurst, 5),
            autocorr_lag1=round(autocorr, 5),
            vol_regime=vr,
            rolling_vol=round(rv, 8),
            trend_slope_1000=round(slope, 8),
            r_squared_1000=round(r2, 5),
            n_ticks=len(prices_list),
            candles=candles,
        )

        # ── AI gate ──────────────────────────────────────────────────────────
        # Rate-limited via snapshot-aware cache. Cache is invalidated when the
        # market state drifts significantly (score, hurst, regime, side, setup, ATR).
        # Stale vetoes are evicted immediately when score crosses the improve threshold.
        if not _AI_GATE_ENABLED or not side:
            analysis.ai_skipped = True
            analysis.ai_reason  = "ai_gate_disabled" if not _AI_GATE_ENABLED else "no_side"
            analysis.ai_approved = True   # gate disabled → don't block
            return analysis

        # Build current snapshot for cache invalidation comparison
        _is_boom_crash = any(k in symbol.upper() for k in ("BOOM", "CRASH"))
        _ai_threshold = (
            self._settings.boom_crash_ai_min_score
            if _is_boom_crash
            else self._settings.r_indices_ai_min_score
        )
        _symbol_bleed_bonus = float(score_breakdown.get("symbol_bleed_bonus") or 0.0)
        _hard_score_floor = max(_AI_HARD_SCORE_FLOOR, float(_ai_threshold))
        _symbol_hard_score_floor = max(
            _hard_score_floor,
            float(_ai_threshold) + max(0.0, _symbol_bleed_bonus),
        )
        _symbol_min_conf, _quality_note = self._dynamic_ai_min_confidence(symbol)
        _quality_veto, _quality_veto_note = self._quality_veto_for_symbol(symbol)

        current_snapshot = _build_cache_snapshot(score, hurst, side, score_breakdown)

        # Check cache first (passes snapshot for drift detection)
        cached_ai = self._ai_cache_get(symbol, current_snapshot=current_snapshot)
        if cached_ai is not None:
            _cached_conf = float(cached_ai.get("confidence", 0.0) or 0.0)
            _cached_approved = bool(cached_ai.get("approved", False))
            analysis.ai_approved = (
                _cached_approved
                and _cached_conf >= _symbol_min_conf
                and score >= _symbol_hard_score_floor
                and not _quality_veto
            )
            analysis.ai_confidence = _cached_conf
            _cached_reason = str(cached_ai.get("reason", ""))
            if _cached_approved and not analysis.ai_approved and _cached_conf < _symbol_min_conf:
                _cached_reason = f"{_cached_reason} | conf_floor:{_cached_conf:.2f}<{_symbol_min_conf:.2f}"
            if _cached_approved and not analysis.ai_approved and score < _symbol_hard_score_floor:
                _cached_reason = (
                    f"{_cached_reason} | score_floor:{score:.2f}<{_symbol_hard_score_floor:.2f}"
                )
            if _cached_approved and not analysis.ai_approved and _quality_veto:
                _cached_reason = f"{_cached_reason} | {_quality_veto_note}"
            analysis.ai_reason = f"{_cached_reason} | {_quality_note} [cached]"
            analysis.ai_model      = str(cached_ai.get("model", ""))
            return analysis

        # Cache miss — call the LLM and store result
        # Phase 15: use per-symbol-type AI threshold so BOOM/CRASH aren't
        # blocked by the R_* threshold of 7.5 (DERIV_BOOM_CRASH_AI_MIN_SCORE).
        _LOGGER.info(
            "[AI_GATE] %s using threshold=%.2f (is_boom_crash=%s)",
            symbol, _ai_threshold, _is_boom_crash,
        )
        # PASO 2.3h-prep Fase C: Pattern Memory v2 query
        _pm_result: dict | None = None
        if self._db_url and side:
            try:
                _pm_result = await asyncio.wait_for(
                    query_pattern_memory(
                        symbol=symbol,
                        side=side,
                        fvg_tier_raw=str(score_breakdown.get("fvg_tier") or ""),
                        imminence_state_raw=str(
                            score_breakdown.get("spike_imminence_state")
                            or score_breakdown.get("imminence_state") or ""
                        ),
                        hurst=hurst,
                        score=score,
                        db_url=self._db_url,
                    ),
                    timeout=3.0,
                )
            except Exception:
                _pm_result = None

        # PASO 2.3h-prep Fase D: Geometric structure from tick history
        _geo_result: dict | None = None
        if prices_list and len(prices_list) >= 100:
            try:
                _geo_result = get_active_structure(symbol, prices_list)
            except Exception:
                _geo_result = None

        # PASO 2.3h-prep Fase E: Maturity info from score_breakdown
        _maturity_info: dict | None = None
        _mat_elapsed = score_breakdown.get("maturity_elapsed_s")
        _mat_threshold = score_breakdown.get("maturity_threshold_s")
        _mat_ratio = score_breakdown.get("maturity_ratio")
        _mat_status = score_breakdown.get("maturity_status")
        if _mat_elapsed is not None and _mat_threshold is not None:
            _maturity_info = {
                "elapsed_s": _mat_elapsed,
                "threshold_s": _mat_threshold,
                "ratio": _mat_ratio,
                "status": _mat_status,
            }

        prompt = _build_ai_prompt(
            symbol=symbol,
            side=side,
            score=score,
            breakdown=score_breakdown,
            hurst=hurst,
            autocorr=autocorr,
            vol_regime=vr,
            rolling_vol=rv,
            slope=slope,
            r2=r2,
            n_ticks=len(prices_list),
            ai_threshold=_ai_threshold,
            ai_min_confidence=_symbol_min_conf,
            symbol_guard_threshold=float(score_breakdown.get("symbol_bleed_threshold") or -2.0),
            pattern_memory=_pm_result,
            geometric_structure=_geo_result,
            maturity_info=_maturity_info,
        )

        # PASO 2.3h-prep Fase B: Prompt audit logging
        if self._prompt_audit_enabled:
            try:
                import json as _json_mod
                _audit_entry = {
                    "ts": time.time(),
                    "symbol": symbol,
                    "prompt": prompt,
                    "context_fields": {
                        "score": score,
                        "bucket": score_breakdown.get("progressive_imminence_bucket"),
                        "ceiling_s": score_breakdown.get("ceiling_value_s"),
                        "ratio": score_breakdown.get("progressive_imminence_ratio"),
                        "compound_reason": score_breakdown.get("hurst_time_compound_reason"),
                        "hurst": hurst,
                        "rng": score_breakdown.get("rng_probability"),
                        "fvg_tier": score_breakdown.get("fvg_tier"),
                        "pattern_memory": _pm_result,
                        "n_ticks": len(prices_list),
                        "has_geometry": bool(_geo_result and (_geo_result.get("horizontal_levels") or _geo_result.get("diagonal_trendlines"))),
                        "n_geo_horizontals": len((_geo_result or {}).get("horizontal_levels") or []),
                        "n_geo_diagonals": len((_geo_result or {}).get("diagonal_trendlines") or []),
                        "maturity_status": _mat_status,
                    },
                }
                with open(self._prompt_audit_path, "a") as _f:
                    _f.write(_json_mod.dumps(_audit_entry, default=str) + "\n")
            except Exception:
                pass

        try:
            _LOGGER.info(
                "[AI_CACHE] CACHE_REFRESH %s | reason=CACHE_MISS/INVALIDATED | "
                "score=%.2f hurst=%.3f regime=%s setup=%s side=%s",
                symbol, score, hurst, vr, current_snapshot.get("setup_type", "?"), side,
            )
            ai_result = await asyncio.wait_for(_call_openrouter(prompt), timeout=40.0)
            # No API key configured — skip gate entirely, don't veto the trade.
            if ai_result.get("reason") == "no_api_key":
                if _AI_FAIL_OPEN:
                    _LOGGER.warning(
                        "[deriv-analyst] No OPENROUTER_API_KEY set — AI gate bypassed for %s "
                        "(DERIV_AI_FAIL_OPEN=true)",
                        symbol,
                    )
                    analysis.ai_skipped = True
                    analysis.ai_approved = True
                    analysis.ai_reason = "no_api_key_skipped"
                else:
                    _LOGGER.error(
                        "[deriv-analyst] No OPENROUTER_API_KEY set — FAIL-CLOSED veto for %s",
                        symbol,
                    )
                    analysis.ai_skipped = False
                    analysis.ai_approved = False
                    analysis.ai_confidence = 0.0
                    analysis.ai_reason = "no_api_key_fail_closed"
                return analysis
            # All models failed (HTTP 4xx/5xx) — treat same as timeout: don't veto.
            if ai_result.get("reason") == "all_models_failed":
                if _AI_FAIL_OPEN:
                    _LOGGER.warning(
                        "[deriv-analyst] All AI models failed for %s — gate bypassed (DERIV_AI_FAIL_OPEN=true)",
                        symbol,
                    )
                    analysis.ai_skipped = True
                    analysis.ai_approved = True
                    analysis.ai_reason = "all_models_failed_skipped"
                else:
                    _LOGGER.warning(
                        "[deriv-analyst] All AI models failed for %s — FAIL-CLOSED veto",
                        symbol,
                    )
                    analysis.ai_skipped = False
                    analysis.ai_approved = False
                    analysis.ai_confidence = 0.0
                    analysis.ai_reason = "all_models_failed_fail_closed"
                return analysis
            # Circuit breaker open (API credential failures) — fail-open if configured.
            # PASO 2.3h-prep: circuit_breaker_open was previously missing from fail-open
            # checks, causing permanent veto for the 30-min breaker window even with
            # DERIV_AI_FAIL_OPEN=true. Now properly covered.
            if ai_result.get("reason") == "circuit_breaker_open":
                if _AI_FAIL_OPEN:
                    _LOGGER.warning(
                        "[deriv-analyst] AI circuit breaker open for %s — gate bypassed "
                        "(DERIV_AI_FAIL_OPEN=true)",
                        symbol,
                    )
                    analysis.ai_skipped = True
                    analysis.ai_approved = True
                    analysis.ai_reason = "circuit_breaker_bypass"
                    return analysis
                # else: fall through — approved=False, conf=0.0 → VETO (default behavior)
            _ai_conf = float(ai_result.get("confidence", 0.0) or 0.0)
            _model_approved = bool(ai_result.get("approved", False))
            analysis.ai_approved = (
                _model_approved
                and _ai_conf >= _symbol_min_conf
                and score >= _symbol_hard_score_floor
                and not _quality_veto
            )
            analysis.ai_confidence = _ai_conf
            analysis.ai_reason = str(ai_result.get("reason", ""))
            if _model_approved and not analysis.ai_approved and _ai_conf < _symbol_min_conf:
                analysis.ai_reason = (
                    f"{analysis.ai_reason} | conf_floor:{_ai_conf:.2f}<{_symbol_min_conf:.2f}"
                )
            if _model_approved and not analysis.ai_approved and score < _symbol_hard_score_floor:
                analysis.ai_reason = (
                    f"{analysis.ai_reason} | score_floor:{score:.2f}<{_symbol_hard_score_floor:.2f}"
                )
            if _model_approved and not analysis.ai_approved and _quality_veto:
                analysis.ai_reason = f"{analysis.ai_reason} | {_quality_veto_note}"
            analysis.ai_reason = f"{analysis.ai_reason} | {_quality_note}"
            analysis.ai_model      = str(ai_result.get("model", ""))
            await self._ai_cache_set(
                symbol, ai_result,
                {"hurst": hurst, "autocorr": autocorr, "score": score, "vol_regime": vr},
                snapshot=current_snapshot,
            )
        except asyncio.TimeoutError:
            if _AI_FAIL_OPEN:
                _LOGGER.warning("[deriv-analyst] AI gate timed out for %s — allowing trade (DERIV_AI_FAIL_OPEN=true)", symbol)
                analysis.ai_skipped  = True
                analysis.ai_approved = True
                analysis.ai_reason   = "ai_timeout_allowed"
            else:
                _LOGGER.warning("[deriv-analyst] AI gate timed out for %s — FAIL-CLOSED veto", symbol)
                analysis.ai_skipped  = False
                analysis.ai_approved = False
                analysis.ai_confidence = 0.0
                analysis.ai_reason   = "ai_timeout_fail_closed"
        except Exception as exc:  # noqa: BLE001
            if _AI_FAIL_OPEN:
                _LOGGER.warning("[deriv-analyst] AI gate error for %s: %s — allowing (DERIV_AI_FAIL_OPEN=true)", symbol, exc)
                analysis.ai_skipped  = True
                analysis.ai_approved = True
                analysis.ai_reason   = f"ai_error_allowed: {exc}"
            else:
                _LOGGER.warning("[deriv-analyst] AI gate error for %s: %s — FAIL-CLOSED veto", symbol, exc)
                analysis.ai_skipped  = False
                analysis.ai_approved = False
                analysis.ai_confidence = 0.0
                analysis.ai_reason   = f"ai_error_fail_closed: {exc}"

        # H.2: propagate PM + geometry context to caller (for decision_logger)
        analysis.pattern_memory_context = _pm_result
        analysis.geometric_structure_context = _geo_result
        return analysis

    def get_history_summary(self) -> dict[str, Any]:
        """Return a summary of cached history (for status JSON telemetry)."""
        out = {}
        for sym, buf in self._history.items():
            if not buf:
                continue
            arr = np.array(buf)
            h = round(_hurst_rs(arr), 4)
            # Regime mapping based on corrected Hurst (replaces '?' in diagnostic logs)
            if h > 0.57:
                regime = "trending"
            elif h < 0.43:
                regime = "mean_reverting"
            else:
                regime = "normal"
            out[sym] = {
                "n_ticks": len(buf),
                "last_price": round(float(buf[-1]), 6),
                "fetched_at": self._history_ts.get(sym),
                "hurst": h,
                "autocorr_lag1": round(_autocorr_lag1(arr), 4),
                "vol_regime": regime,
            }
        return out
