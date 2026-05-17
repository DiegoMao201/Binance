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


_LOGGER = logging.getLogger("deriv.analyst")

# ── Env knobs ────────────────────────────────────────────────────────────────
_AI_GATE_ENABLED    = os.getenv("DERIV_AI_GATE_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
_AI_MIN_CONFIDENCE  = float(os.getenv("DERIV_AI_MIN_CONFIDENCE", "0.55"))
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

# ── Smart cache invalidation thresholds ─────────────────────────────────────
_CACHE_SCORE_DRIFT_THRESHOLD   = float(os.getenv("DERIV_CACHE_SCORE_DRIFT", "0.5"))   # |Δscore| > 0.5
_CACHE_HURST_DRIFT_THRESHOLD   = float(os.getenv("DERIV_CACHE_HURST_DRIFT", "0.03"))  # |ΔH| > 0.03
_CACHE_ATR_DRIFT_THRESHOLD     = float(os.getenv("DERIV_CACHE_ATR_DRIFT", "0.15"))    # |ΔATRpct| > 15%
_CACHE_VETO_SCORE_IMPROVE      = float(os.getenv("DERIV_CACHE_VETO_IMPROVE", "0.5"))  # stale veto threshold

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
    return {
        "score": round(float(score), 3),
        "score_bucket": round(float(score) * 2) / 2,   # nearest 0.5 bucket
        "hurst": round(float(hurst), 4),
        "hurst_bucket": round(round(hurst * 20) / 20, 3),   # nearest 0.05 bucket
        "side": side or "",
        "regime": regime,
        "setup_type": setup_type,
        "atr_pct": round(atr_pct, 5),
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
) -> str:
    return f"""You are a quantitative trading assistant evaluating a trade signal on a Deriv synthetic volatility index.

SYMBOL: {symbol}
PROPOSED DIRECTION: {side} (MULTUP=long, MULTDOWN=short)

MATHEMATICAL SCORING (out of 10): {score:.2f}
Score breakdown: {json.dumps(breakdown)}

STATISTICAL ANALYSIS ({n_ticks} ticks):
- Hurst exponent: {hurst:.4f} (>0.5=trending, 0.5=random, <0.5=mean-reverting)
- Autocorrelation lag-1: {autocorr:.4f} (positive=momentum, negative=reversal)
- Volatility regime: {vol_regime}
- Rolling volatility (20t): {rolling_vol:.6f}
- OLS slope (normalised): {slope:.6f}
- R² of linear fit: {r2:.4f}

CONTEXT: This is a STATISTICAL synthetic index (not crypto, not forex). It has no macro exposure.
Edge comes from autocorrelation and short-term momentum patterns.

Respond ONLY with a JSON object:
{{"approved": true/false, "confidence": 0.0-1.0, "reason": "one sentence"}}

Approve (true) if: score>=7.5 AND Hurst>0.52 AND autocorr aligned with side AND regime not "volatile".
Do NOT approve if confidence <0.65 or if mathematical signals conflict."""


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
                **{k: v for k, v in context.items() if k in ("hurst", "autocorr", "score", "vol_regime")},
            }
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
        current_snapshot = _build_cache_snapshot(score, hurst, side, score_breakdown)

        # Check cache first (passes snapshot for drift detection)
        cached_ai = self._ai_cache_get(symbol, current_snapshot=current_snapshot)
        if cached_ai is not None:
            analysis.ai_approved   = bool(cached_ai.get("approved", False)) and cached_ai.get("confidence", 0.0) >= _AI_MIN_CONFIDENCE
            analysis.ai_confidence = float(cached_ai.get("confidence", 0.0))
            analysis.ai_reason     = str(cached_ai.get("reason", "")) + " [cached]"
            analysis.ai_model      = str(cached_ai.get("model", ""))
            return analysis

        # Cache miss — call the LLM and store result
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
        )

        try:
            _LOGGER.info(
                "[AI_CACHE] CACHE_REFRESH %s | reason=CACHE_MISS/INVALIDATED | "
                "score=%.2f hurst=%.3f regime=%s setup=%s side=%s",
                symbol, score, hurst, vr, current_snapshot.get("setup_type", "?"), side,
            )
            ai_result = await asyncio.wait_for(_call_openrouter(prompt), timeout=40.0)
            # No API key configured — skip gate entirely, don't veto the trade.
            if ai_result.get("reason") == "no_api_key":
                _LOGGER.warning(
                    "[deriv-analyst] No OPENROUTER_API_KEY set — AI gate bypassed for %s "
                    "(set key in Coolify to enable full AI filtering)", symbol,
                )
                analysis.ai_skipped  = True
                analysis.ai_approved = True
                analysis.ai_reason   = "no_api_key_skipped"
                return analysis
            # All models failed (HTTP 4xx/5xx) — treat same as timeout: don't veto.
            if ai_result.get("reason") == "all_models_failed":
                _LOGGER.warning(
                    "[deriv-analyst] All AI models failed for %s — gate bypassed (treating as skipped)",
                    symbol,
                )
                analysis.ai_skipped  = True
                analysis.ai_approved = True
                analysis.ai_reason   = "all_models_failed_skipped"
                return analysis
            analysis.ai_approved   = bool(ai_result.get("approved", False)) and ai_result.get("confidence", 0.0) >= _AI_MIN_CONFIDENCE
            analysis.ai_confidence = float(ai_result.get("confidence", 0.0))
            analysis.ai_reason     = str(ai_result.get("reason", ""))
            analysis.ai_model      = str(ai_result.get("model", ""))
            await self._ai_cache_set(
                symbol, ai_result,
                {"hurst": hurst, "autocorr": autocorr, "score": score, "vol_regime": vr},
                snapshot=current_snapshot,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("[deriv-analyst] AI gate timed out for %s — allowing trade", symbol)
            analysis.ai_skipped  = True
            analysis.ai_approved = True   # on timeout → don't block trading
            analysis.ai_reason   = "ai_timeout_allowed"
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-analyst] AI gate error for %s: %s — allowing", symbol, exc)
            analysis.ai_skipped  = True
            analysis.ai_approved = True
            analysis.ai_reason   = f"ai_error_allowed: {exc}"

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
