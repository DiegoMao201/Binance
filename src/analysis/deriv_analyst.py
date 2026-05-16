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
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
# Calling the LLM on every qualifying tick would:
#   (a) cost thousands of USD/month at retail API pricing
#   (b) add 3-8 s of latency to every order attempt
#   (c) provide minimal additional value because the regime doesn't change tick-by-tick
#
# Solution: the AI analyses each symbol once every 15 minutes (configurable).
# Between AI cycles we reuse the last result. The cached result is also persisted
# to a JSON log so decisions are auditable and survive restarts.
_AI_CACHE_TTL_SEC = int(os.getenv("DERIV_AI_CACHE_TTL_SEC", "900"))   # 15 min default
_AI_LOG_MAX_ENTRIES = int(os.getenv("DERIV_AI_LOG_MAX", "500"))        # rolling log

# Model preference order (all via OpenRouter)
_AI_MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "openai/gpt-4o-mini",
    "anthropic/claude-haiku-20240307",
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


# ── Helpers: Hurst exponent via R/S analysis ────────────────────────────────
def _hurst_rs(series: np.ndarray, min_n: int = 10) -> float:
    """Estimate the Hurst exponent via Rescaled Range (R/S) analysis.

    Returns a value in [0, 1]:
      H ≈ 0.5  → random walk (no edge)
      H > 0.5  → persistent trend (positive autocorrelation)
      H < 0.5  → mean-reverting (negative autocorrelation)

    Uses log-log regression of R/S on lag sizes.
    """
    n = len(series)
    if n < 20:
        return 0.5  # insufficient data → assume random walk

    lags = []
    rs_vals = []
    for lag in [max(min_n, n // 10), max(min_n, n // 5), max(min_n, n // 3), n // 2, n]:
        if lag > n:
            continue
        # Compute R/S over sub-series of length `lag`
        segments = n // lag
        if segments == 0:
            continue
        rs_list = []
        for i in range(segments):
            sub = series[i * lag: (i + 1) * lag]
            m   = sub.mean()
            dev = np.cumsum(sub - m)
            r   = dev.max() - dev.min()
            s   = sub.std(ddof=0)
            if s > 0:
                rs_list.append(r / s)
        if rs_list:
            lags.append(math.log(lag))
            rs_vals.append(math.log(np.mean(rs_list)))

    if len(lags) < 2:
        return 0.5

    # Linear regression in log-log space
    x = np.array(lags)
    y = np.array(rs_vals)
    coef = np.polyfit(x, y, 1)
    return float(np.clip(coef[0], 0.0, 1.0))


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
    """Call OpenRouter with model cascade. Returns parsed AI result dict."""
    if not _OPENROUTER_KEY:
        return {"approved": False, "confidence": 0.0, "reason": "no_api_key", "model": ""}

    import aiohttp  # inline import — avoids hard dep at module level

    headers = {
        "Authorization": f"Bearer {_OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://optiferre.app",
        "X-Title": "OptiFerre-Deriv",
    }
    for model in _AI_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=6.0),
                ) as resp:
                    if resp.status == 429:
                        _LOGGER.warning("[deriv-analyst] AI 429 on %s — skip", model)
                        continue
                    if resp.status != 200:
                        _LOGGER.warning("[deriv-analyst] AI %s HTTP %s", model, resp.status)
                        continue
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    parsed = json.loads(raw)
                    parsed["model"] = model
                    return _normalize_ai(parsed)
        except asyncio.TimeoutError:
            _LOGGER.warning("[deriv-analyst] AI timeout on %s", model)
            continue
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-analyst] AI error on %s: %s", model, exc)
            continue
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
    def _ai_cache_get(self, symbol: str) -> dict[str, Any] | None:
        """Return cached AI result if still fresh, else None."""
        entry = self._ai_cache.get(symbol)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _AI_CACHE_TTL_SEC:
            return None
        return entry["result"]

    async def _ai_cache_set(self, symbol: str, result: dict[str, Any], context: dict[str, Any]) -> None:
        """Store AI result in memory cache and dispatch log write to a thread.

        The in-memory update is instant (μs). The disk write is offloaded to the
        thread-pool executor so synchronous file I/O never blocks the event loop.
        """
        self._ai_cache[symbol] = {"ts": time.time(), "result": result}
        # Fire-and-forget: schedule disk write without blocking the tick loop.
        asyncio.get_event_loop().run_in_executor(
            None, self._append_ai_log, symbol, result, context
        )

    def _append_ai_log(self, symbol: str, result: dict[str, Any], context: dict[str, Any]) -> None:
        """Append one AI decision to the rolling JSON log (bounded at _AI_LOG_MAX_ENTRIES)."""
        try:
            self._ai_log_path.parent.mkdir(parents=True, exist_ok=True)
            existing: list[dict] = []
            if self._ai_log_path.exists():
                try:
                    existing = json.loads(self._ai_log_path.read_text())
                except (json.JSONDecodeError, OSError):
                    existing = []
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "approved": result.get("approved"),
                "confidence": result.get("confidence"),
                "reason": result.get("reason"),
                "model": result.get("model"),
                **{k: v for k, v in context.items() if k in ("hurst", "autocorr", "score", "vol_regime")},
            }
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
        # Rate-limited: consult the LLM at most once per _AI_CACHE_TTL_SEC (15 min)
        # per symbol. Intermediate signals reuse the cached result.
        # This prevents $$$-cost real-time AI calls on a fast synthetic index feed.
        if not _AI_GATE_ENABLED or not side:
            analysis.ai_skipped = True
            analysis.ai_reason  = "ai_gate_disabled" if not _AI_GATE_ENABLED else "no_side"
            analysis.ai_approved = True   # gate disabled → don't block
            return analysis

        # Check cache first
        cached_ai = self._ai_cache_get(symbol)
        if cached_ai is not None:
            analysis.ai_approved   = bool(cached_ai.get("approved", False)) and cached_ai.get("confidence", 0.0) >= _AI_MIN_CONFIDENCE
            analysis.ai_confidence = float(cached_ai.get("confidence", 0.0))
            analysis.ai_reason     = str(cached_ai.get("reason", "")) + " [cached]"
            analysis.ai_model      = str(cached_ai.get("model", ""))
            _LOGGER.debug("[deriv-analyst] AI cache HIT for %s (age %.0fs)", symbol,
                          time.time() - self._ai_cache[symbol]["ts"])
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
            _LOGGER.info("[deriv-analyst] consulting AI for %s (cache expired / first call)", symbol)
            ai_result = await asyncio.wait_for(_call_openrouter(prompt), timeout=8.0)
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
            analysis.ai_approved   = bool(ai_result.get("approved", False)) and ai_result.get("confidence", 0.0) >= _AI_MIN_CONFIDENCE
            analysis.ai_confidence = float(ai_result.get("confidence", 0.0))
            analysis.ai_reason     = str(ai_result.get("reason", ""))
            analysis.ai_model      = str(ai_result.get("model", ""))
            await self._ai_cache_set(symbol, ai_result, {
                "hurst": hurst, "autocorr": autocorr, "score": score, "vol_regime": vr,
            })
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
            arr = np.array(buf[-100:])
            out[sym] = {
                "n_ticks": len(buf),
                "last_price": round(float(buf[-1]), 6),
                "fetched_at": self._history_ts.get(sym),
                "hurst": round(_hurst_rs(np.array(buf)), 4),
                "autocorr_lag1": round(_autocorr_lag1(np.array(buf)), 4),
            }
        return out
