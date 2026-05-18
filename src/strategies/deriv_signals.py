"""
src/strategies/deriv_signals.py
─────────────────────────────────────────────────────────────────────────────
Deterministic direction filter for Deriv Boom & Crash synthetic indices.

Boom indices (BOOM500, BOOM1000, BOOM300 …) produce discrete upward spikes
on average every N ticks.  Crash indices produce discrete downward spikes.
The statistically correct posture is therefore:

  BOOM  → only LONG (MULTUP).   Being short on a Boom means the next spike
          fires directly against the position — instant partial wipeout with
          no time to react.

  CRASH → only SHORT (MULTDOWN). Same asymmetry inverted.

Any technical signal pointing the wrong way is a HARD VETO.  The trend
calculation may be temporarily fooled by the inter-spike drift; that signal
must never be used to enter against the spike direction.

Public API
──────────
  is_spike_market(symbol)  → bool
      True for any Boom / Crash synthetic index.

  forced_side(symbol)  → "MULTUP" | "MULTDOWN" | None
      Returns the mandatory Deriv contract type for spike markets, or None
      when the symbol has no directional restriction.

  direction_veto(symbol, computed_side)  → (vetoed: bool, reason: str)
      Hard veto when the computed direction contradicts the spike asymmetry
      rule.  vetoed=True means the caller MUST NOT enter.

  spike_timeout_sec(symbol)  → int
      Maximum seconds to hold an open BOOM/CRASH contract before force-close.
      Controlled by per-profile max_hold_seconds (default 450) or env var
      BOOM_CRASH_SPIKE_TIMEOUT_SEC as a global override.
      Non-spike markets return 0 (= no timeout).
"""

from __future__ import annotations

import os

# ─── Constants ────────────────────────────────────────────────────────────────
_BOOM_SIDE:  str = "MULTUP"     # spike fires UP   → only long
_CRASH_SIDE: str = "MULTDOWN"   # spike fires DOWN  → only short

# Number of seconds to hold a BOOM/CRASH contract before force-closing.
# BOOM500/CRASH500 spike frequency: ~1 spike per 500 ticks ≈ 500s.
# BOOM1000/CRASH1000: ~1 spike per 1000 ticks ≈ 1000s.
# 450 s (7.5 min) gives the spike a generous accumulation window while the
# structural SL ($1.50-$1.80 on a $3 stake) protects against runaway losses.
# Previous 120 s was killing trades before the FVG zone could deliver the spike.
# Set to 0 to disable the time-based force-close (rely on broker SL only).
_SPIKE_TIMEOUT_SEC: int = int(os.getenv("BOOM_CRASH_SPIKE_TIMEOUT_SEC", "450"))


# ─── Public helpers ───────────────────────────────────────────────────────────

def is_spike_market(symbol: str) -> bool:
    """True for Boom / Crash synthetic indices (case-insensitive)."""
    su = symbol.upper()
    return "BOOM" in su or "CRASH" in su


def forced_side(symbol: str) -> str | None:
    """Return the mandatory Deriv contract type for spike markets, else None.

    BOOM  → "MULTUP"    (only longs; upward spikes)
    CRASH → "MULTDOWN"  (only shorts; downward spikes)
    Other → None        (no directional restriction)
    """
    su = symbol.upper()
    if "BOOM" in su:
        return _BOOM_SIDE
    if "CRASH" in su:
        return _CRASH_SIDE
    return None


def direction_veto(symbol: str, computed_side: str) -> tuple[bool, str]:
    """Hard veto when the signal direction contradicts the spike asymmetry rule.

    Returns (vetoed: bool, reason: str).
    When vetoed=True the caller MUST NOT enter regardless of score.

    Examples
    ────────
    direction_veto("BOOM500",  "MULTDOWN") → (True,  "SPIKE_DIRECTION_VETO: ...")
    direction_veto("BOOM500",  "MULTUP")   → (False, "")
    direction_veto("CRASH500", "MULTUP")   → (True,  "SPIKE_DIRECTION_VETO: ...")
    direction_veto("EURUSD",   "MULTDOWN") → (False, "")  ← not a spike market
    """
    required = forced_side(symbol)
    if required is None:
        return False, ""   # not a spike market — no restriction

    if computed_side != required:
        # Describe the wrong and correct directions in human-readable terms
        wrong_label   = "BUY/MULTUP"   if computed_side == "MULTUP"   else "SELL/MULTDOWN"
        correct_label = "BUY/MULTUP"   if required       == "MULTUP"  else "SELL/MULTDOWN"
        market_type   = "BOOM (spike UP)" if "BOOM" in symbol.upper() else "CRASH (spike DOWN)"
        reason = (
            f"SPIKE_DIRECTION_VETO: {symbol} is a {market_type} index — "
            f"only {correct_label} allowed. "
            f"Strategy generated {wrong_label}; inter-spike drift signal rejected."
        )
        return True, reason

    return False, ""


def spike_timeout_sec(symbol: str) -> int:
    """Seconds to hold a BOOM/CRASH contract before force-closing.

    Returns the configured timeout for spike markets, 0 for all others.

    Priority (highest to lowest):
      1. Per-profile ``max_hold_seconds`` in ASSET_INTEL_PROFILES — baked into
         the codebase so a stale Coolify env var can never override it.
      2. ``BOOM_CRASH_SPIKE_TIMEOUT_SEC`` env var — global runtime tuning.
      3. Module-level default ``_SPIKE_TIMEOUT_SEC`` (450 s).
    """
    if not is_spike_market(symbol):
        return 0
    # Per-profile value beats everything else — protects against stale env vars.
    profile_hold = int(get_asset_profile(symbol).get("max_hold_seconds", 0))
    if profile_hold > 0:
        return profile_hold
    # Global env var fallback for symbols not in the profile dict.
    return int(os.getenv("BOOM_CRASH_SPIKE_TIMEOUT_SEC", str(_SPIKE_TIMEOUT_SEC)))


# ─── Per-asset intelligence profiles ─────────────────────────────────────────
# Each entry defines the operational ruleset for its symbol class.  The engine
# uses this dictionary to avoid cross-contaminating volatility vs spike logic.
#
# Volatility indices (R_*): mean-reversion / trend-following on Hurst exponent.
# Spike indices (BOOM/CRASH): forced directional asymmetry + FVG mitigation.
ASSET_INTEL_PROFILES: dict[str, dict] = {
    # ── Pure volatility (bidirectional, continuous) ───────────────────────────
    # R_10/R_25/R_50: fast, noisy, low persistence → mean-reversion + scalping.
    "R_10":  {
        "type": "volatility",
        "strategy_mode": "mean_revert",
        "min_hurst": 0.48,
        "use_mean_reversion": True,
        "allow_mean_reversion": True,
        "allow_breakout": False,
        "band_sigma": 1.90,
        "ema_trend_filter": False,
        "min_score": 5.5,
        "atr_min": 0.0,
        "cooldown_sec": 90,
        "sl_multiplier": 1.0,
        "tp_multiplier": 1.0,
        "trailing_mode": "aggressive",
    },
    "R_25":  {
        "type": "volatility",
        "strategy_mode": "mean_revert",
        "min_hurst": 0.52,
        "use_mean_reversion": True,
        "allow_mean_reversion": True,
        "allow_breakout": False,
        "band_sigma": 1.92,
        "ema_trend_filter": False,
        "min_score": 9.0,
        "atr_min": 0.0,
        "cooldown_sec": 90,
        "sl_multiplier": 1.0,
        "tp_multiplier": 1.0,
        "trailing_mode": "aggressive",
        "geo_entry_min": -0.8,
        "geo_entry_max":  0.3,
        "trail_floor_min_usdt": 0.25,
        "stake_max_usdt": 1.50,
        "max_hold_seconds": 120,
        "setup_allowed": ["MEAN_REV"],
        # MODO OBSERVACIÓN 48h: suspended=True bloquea ejecución de trades.
        # Hurst≥0.52 + score≥9.0 + geo ∈ [-0.8, +0.3] calibrados para validar
        # edge antes de activar. Diagnósticos (Hurst + régimen) visibles en logs
        # vía [DIAGNÓSTICO] cada 10 ticks — no requiere pipeline activo.
        # Activar retirando "suspended" cuando WR observado ≥ 55% en 20+ setups.
        "suspended": True,
    },
    "R_50":  {
        "type": "volatility",
        "strategy_mode": "mean_revert",
        "min_hurst": 0.43,              # lowered from 0.48 (wins had H≥0.43)
        "use_mean_reversion": True,
        "allow_mean_reversion": True,
        "allow_breakout": False,
        "band_sigma": 1.95,
        "ema_trend_filter": False,
        "min_score": 9.0,              # raised from 5.5 (all wins had score_raw≥9.5)
        "atr_min": 0.0,
        "cooldown_sec": 90,
        "sl_multiplier": 1.0,
        "tp_multiplier": 1.0,
        "trailing_mode": "aggressive",
        # ── Batch analysis 2026-05-18: pattern confirmed ───────────────────
        # 4 wins: score_raw≥9.5, mean_rev_mode=true, SMC_FVG, geo ∈ [-1.0, +0.5]
        # Losses had geo>0.5 (price extended away from mean). floor=0.00 was tóxico.
        "geo_entry_min": -1.0,          # reject if geo < -1.0 (too extended down)
        "geo_entry_max":  0.5,          # reject if geo >  0.5 (too extended up)
        "trail_floor_min_usdt": 0.30,   # T1 activates at ≥$0.30 locked profit
    },
    # R_75: very erratic, false spikes → require high confluence, NO pure
    # breakouts when H<0.58, prefer reversals/sweeps with ATR expansion.
    "R_75":  {
        "type": "volatility",
        "strategy_mode": "hybrid",
        "min_hurst": 0.55,
        "use_mean_reversion": False,
        "allow_mean_reversion": True,
        "allow_breakout": False,        # breakout only when H>=0.58 (handled in pipeline)
        "band_sigma": 2.00,
        "ema_trend_filter": True,
        "min_score": 6.5,
        "atr_min": 0.0,
        "cooldown_sec": 120,
        "sl_multiplier": 1.8,
        "tp_multiplier": 1.5,
        "trailing_mode": "atr_dynamic",
        # ── Reactivado 2026-05-18 tras auditoría SL vs ATR ─────────────────
        # Causa raíz: stop_loss_pct=0.004 (mean_rev) × stake producía sl_usd=$0.026
        # → flooreado a $0.50 mínimo Deriv = 33% del stake en cada trade.
        # Con ATR_abs≈4.1 y 200× apalancamiento, el floor era alcanzable en 1-2 min.
        # Fix: stop_loss_pct_override=0.36 → sl_usd=$0.54 en stake $1.50 (supera floor).
        # stake_max_usdt=1.50 reduce exposición mientras se valida el nuevo SL.
        "stop_loss_pct_override": 0.36,  # 36% de stake → SL $0.54 en stake $1.50
        "stake_max_usdt": 1.50,
    },
    # R_100: trending, long moves, better persistence → trend following.
    "R_100": {
        "type": "volatility",
        "strategy_mode": "trend",
        "min_hurst": 0.58,
        "use_mean_reversion": False,
        "allow_mean_reversion": False,
        "allow_breakout": True,
        "band_sigma": 2.10,
        "ema_trend_filter": True,
        "min_score": 6.0,
        "atr_min": 0.0,
        "cooldown_sec": 150,
        "sl_multiplier": 1.5,
        "tp_multiplier": 3.0,
        "trailing_mode": "atr_wide",
    },
    # ── BOOM: asymmetric accumulation — BUY only / spike capture ─────────────
    # Spike markets: NO mean-reversion, NO breakout — only SMC + spike-hunter.
    "BOOM300": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 20,
        "max_hold_seconds": 450,        # 7.5 min — overrides BOOM_CRASH_SPIKE_TIMEOUT_SEC
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 7.0,
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
    },
    "BOOM500": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 12,
        "max_hold_seconds": 450,        # 7.5 min — overrides BOOM_CRASH_SPIKE_TIMEOUT_SEC
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 7.0,
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        # ── Batch analysis 2026-05-18: 10 trades, -$2.96, geo always extended ──
        # All 9 losses had avg geo +1.48 (price overextended on entry).
        # Even the sole win had geo +1.31. Zero structural edge found.
        # DISABLED until further evidence of consistent positive expectancy.
        "disabled": True,
    },
    "BOOM1000": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 18,
        "max_hold_seconds": 450,        # 7.5 min — overrides BOOM_CRASH_SPIKE_TIMEOUT_SEC
        "ema_distance_pct": 0.05,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 7.0,
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        # ── Batch analysis 2026-05-18 ──────────────────────────────────────
        # Sole loss had H=0.402 (below meaningful persistence for spike setup).
        # Wins had geo ∈ [-0.69, +1.04]; loss had geo +1.35 (extended).
        "hurst_min_spike": 0.43,        # veto if hurst < 0.43 even for spike markets
        "geo_entry_max": 0.50,          # don't enter when price is too extended upward
    },
    # ── CRASH: asymmetric accumulation — SELL only / spike capture ────────────
    "CRASH300": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 20,
        "max_hold_seconds": 450,        # 7.5 min — overrides BOOM_CRASH_SPIKE_TIMEOUT_SEC
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 7.0,
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
    },
    "CRASH500": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 12,
        "max_hold_seconds": 450,        # 7.5 min — overrides BOOM_CRASH_SPIKE_TIMEOUT_SEC
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 7.0,
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        # ── Batch analysis 2026-05-18 ──────────────────────────────────────
        # Wins: geo -1.23 and -1.61 | Losses: geo -0.84 avg.
        # Confirmed: no structural edge when price is not well below channel mid.
        "geo_entry_max": -1.20,         # only enter when price ≤ -1.20σ below mid
        "trail_floor_min_usdt": 0.20,   # eliminate floor=-1.00 legacy issue
    },
    "CRASH1000": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 18,
        "max_hold_seconds": 450,        # 7.5 min — overrides BOOM_CRASH_SPIKE_TIMEOUT_SEC
        "ema_distance_pct": 0.05,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 7.0,
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        # ── Batch analysis 2026-05-18 ──────────────────────────────────────
        # Wins: geo -1.09 (both) | Losses: geo -0.77 avg.
        # Confirmed: edge only when price is well below channel midpoint.
        "geo_entry_max": -1.00,         # only enter when price ≤ -1.00σ below mid
    },
}

# Default profile for symbols not explicitly listed.
_DEFAULT_VOLATILITY_PROFILE: dict = {
    "type": "volatility",
    "strategy_mode": "hybrid",
    "min_hurst": 0.55,
    "use_mean_reversion": True,
    "allow_mean_reversion": True,
    "allow_breakout": True,
    "band_sigma": 2.00,
    "ema_trend_filter": False,
    "min_score": 5.5,
    "atr_min": 0.0,
    "cooldown_sec": 120,
    "sl_multiplier": 1.0,
    "tp_multiplier": 1.5,
    "trailing_mode": "aggressive",
    # Global floor minimum — no trail_stop_floor below this value once trailing
    # is active. Eliminates 'break-even noise' closes and legacy floor=-1.00.
    "trail_floor_min_usdt": 0.20,
}


def get_asset_profile(symbol: str) -> dict:
    """Return the intelligence profile for *symbol*.

    Normalises the symbol key so that e.g. "1HZ100V" and "R_100" both hit
    the R_100 profile.  Falls back to the default volatility profile for
    unknown symbols so the bot never silently drops a market.
    """
    su = symbol.upper()
    # Direct match first
    if su in ASSET_INTEL_PROFILES:
        return ASSET_INTEL_PROFILES[su]
    # Canonical short-name match (BOOM/CRASH numeric suffix)
    for key in ASSET_INTEL_PROFILES:
        if key in su:
            return ASSET_INTEL_PROFILES[key]
    return _DEFAULT_VOLATILITY_PROFILE


def min_score_for(symbol: str) -> float:
    """Return the minimum entry score required to trade *symbol*.

    Spike markets (BOOM/CRASH) require ≥ 7.0 — no guessing allowed.
    Volatility markets use their profile value (default 5.5).
    """
    profile = get_asset_profile(symbol)
    return float(profile.get("min_score", 5.5))


def get_eval_mode(symbol: str) -> str:
    """Return the evaluation pipeline mode for *symbol*.

    "smc_fvg"    — BOOM/CRASH spike markets: skip Hurst/mean_rev/random_walk;
                   use only SMC+FVG mitigation + EMA-200 spike-hunter.
    "stochastic" — R_* volatility indices: full stochastic pipeline
                   (Hurst, mean-reversion, random-walk veto, geometry).
    """
    return "smc_fvg" if is_spike_market(symbol) else "stochastic"


def spike_contract_type(symbol: str, multside: str) -> str:
    """Map a MULTUP/MULTDOWN side to the correct Deriv contract type for BOOM/CRASH.

    Boom/Crash do NOT accept MULTUP/MULTDOWN — they only accept RISE/FALL
    duration-based contracts.  Non-spike markets return the original multside
    unchanged so the multiplier path is unaffected.

    BOOM  + MULTUP   → "RISE"
    CRASH + MULTDOWN → "FALL"
    Other            → multside as-is (MULTUP/MULTDOWN for R_* indices)
    """
    su = symbol.upper()
    ms = multside.upper()
    if "BOOM" in su:
        return "RISE"
    if "CRASH" in su:
        return "FALL"
    return ms


# ─── Institutional volatility / regime filters (added 2026-05-18) ───────────

# Symbols subject to the ATR-percentile gate. BOOM300/CRASH300 are excluded
# because their accumulation windows are short and the ATR filter would
# starve them of valid setups.
_ATR_FILTER_SYMBOLS: set[str] = {"BOOM500", "BOOM1000", "CRASH500", "CRASH1000"}


def passes_atr_volatility_filter(
    symbol: str,
    current_atr: float,
    atr_history: list[float],
    percentile_threshold: int = 40,
) -> tuple[bool, str]:
    """Institutional ATR volatility gate for BOOM500/1000 + CRASH500/1000.

    Rejects entries whose current ATR is below the *percentile_threshold*-th
    percentile of the rolling 24-h ATR history.  This filters out dead-volume
    sessions where the spike accumulation simply will not occur.

    Returns (allowed: bool, reason: str). Symbols outside the filter set
    always return (True, "").
    """
    su = symbol.upper()
    if su not in _ATR_FILTER_SYMBOLS:
        return True, ""
    # Need at least 10 historical ATR samples to compute a meaningful percentile.
    if not atr_history or len(atr_history) < 10:
        return True, ""
    if current_atr <= 0:
        return False, "ENTRY_BLOCKED: Insufficient ATR Volatility (atr=0)"
    try:
        # Lightweight percentile (no numpy dep required at call site).
        sorted_hist = sorted(atr_history)
        idx = max(0, min(len(sorted_hist) - 1,
                         int(len(sorted_hist) * percentile_threshold / 100.0)))
        p_threshold = sorted_hist[idx]
    except Exception:  # noqa: BLE001
        return True, ""
    if current_atr < p_threshold:
        return False, (
            f"ENTRY_BLOCKED: Insufficient ATR Volatility "
            f"(current={current_atr:.6f} < p{percentile_threshold}={p_threshold:.6f})"
        )
    return True, ""


def min_score_for_regime(symbol: str, regime: str) -> float:
    """Regime-aware minimum score override.

    • calm regime  → 7.50  (only ultra-high-conviction setups in flat markets)
    • trending / volatile → 6.00 baseline
    • other (ranging, unknown) → falls back to the symbol's profile minimum.
    """
    r = (regime or "").lower()
    if r == "calm":
        return 7.50
    if r in ("trending", "volatile"):
        return 6.00
    return min_score_for(symbol)


def adaptive_max_hold(symbol: str, regime: str) -> int:
    """Macro-timeout: BOOM/CRASH trending markets get 600s, others 450s.

    Non-spike markets return 0 (no timeout).
    """
    if not is_spike_market(symbol):
        return 0
    r = (regime or "").lower()
    return 600 if r == "trending" else 450


def extreme_mr_penalty(symbol: str, hurst: float | None, geo_label: str | None) -> float:
    """Extreme mean-reversion exhaustion penalty for BOOM/CRASH.

    Triggers when:
      • symbol is a spike market (BOOM/CRASH)
      • hurst < 0.30 (deep mean-reverting exhaustion)
      • geo_label == "mr_near_mean" (price is hugging the macro mean)

    Returns -1.5 if all conditions match, 0.0 otherwise. Caller is expected
    to add the return value to score and to record
    score_breakdown["hurst_mr_penalty"] = returned_value when non-zero.
    """
    if not is_spike_market(symbol):
        return 0.0
    if hurst is None:
        return 0.0
    try:
        h = float(hurst)
    except (TypeError, ValueError):
        return 0.0
    if h >= 0.30:
        return 0.0
    if (geo_label or "").lower() != "mr_near_mean":
        return 0.0
    return -1.5

