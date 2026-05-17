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
      Controlled by env var BOOM_CRASH_SPIKE_TIMEOUT_SEC (default 10).
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
# Holding for 120s gives the spike a fair window to fire while capping the
# inter-spike drift loss.  Previous default of 10s was killing all BOOM/CRASH
# contracts before any spike could trigger.
# Set to 0 to disable the time-based force-close (rely on broker SL only).
_SPIKE_TIMEOUT_SEC: int = int(os.getenv("BOOM_CRASH_SPIKE_TIMEOUT_SEC", "120"))


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
    Reads BOOM_CRASH_SPIKE_TIMEOUT_SEC at call-time so it can be updated
    via environment without restarting (if dynamic env reloading is used).
    """
    if not is_spike_market(symbol):
        return 0
    # Re-read env each call so ops can tune it without a redeploy
    return int(os.getenv("BOOM_CRASH_SPIKE_TIMEOUT_SEC", str(_SPIKE_TIMEOUT_SEC)))


# ─── Per-asset intelligence profiles ─────────────────────────────────────────
# Each entry defines the operational ruleset for its symbol class.  The engine
# uses this dictionary to avoid cross-contaminating volatility vs spike logic.
#
# Volatility indices (R_*): mean-reversion / trend-following on Hurst exponent.
# Spike indices (BOOM/CRASH): forced directional asymmetry + FVG mitigation.
ASSET_INTEL_PROFILES: dict[str, dict] = {
    # ── Pure volatility (bidirectional, continuous) ───────────────────────────
    "R_10":  {
        "type": "volatility",
        "min_hurst": 0.52,
        "use_mean_reversion": True,
        "band_sigma": 1.90,
        "ema_trend_filter": False,
        "min_score": 5.5,
    },
    "R_25":  {
        "type": "volatility",
        "min_hurst": 0.53,
        "use_mean_reversion": True,
        "band_sigma": 1.92,
        "ema_trend_filter": False,
        "min_score": 5.5,
    },
    "R_50":  {
        "type": "volatility",
        "min_hurst": 0.55,
        "use_mean_reversion": True,
        "band_sigma": 1.95,
        "ema_trend_filter": False,
        "min_score": 5.5,
    },
    "R_75":  {
        "type": "volatility",
        "min_hurst": 0.58,
        "use_mean_reversion": False,
        "band_sigma": 2.00,
        "ema_trend_filter": True,
        "min_score": 5.5,
    },
    "R_100": {
        "type": "volatility",
        "min_hurst": 0.62,
        "use_mean_reversion": True,
        "band_sigma": 2.10,
        "ema_trend_filter": True,
        "min_score": 5.5,
    },
    # ── BOOM: asymmetric accumulation — BUY only / spike capture ─────────────
    "BOOM300": {
        "type": "spike_boom",
        "forced_side": "MULTUP",
        "max_hold_ticks": 20,
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "min_score": 7.0,
    },
    "BOOM500": {
        "type": "spike_boom",
        "forced_side": "MULTUP",
        "max_hold_ticks": 12,
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "min_score": 7.0,
    },
    "BOOM1000": {
        "type": "spike_boom",
        "forced_side": "MULTUP",
        "max_hold_ticks": 18,
        "ema_distance_pct": 0.05,
        "require_fvg_mitigation": True,
        "min_score": 7.0,
    },
    # ── CRASH: asymmetric accumulation — SELL only / spike capture ────────────
    "CRASH300": {
        "type": "spike_crash",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 20,
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "min_score": 7.0,
    },
    "CRASH500": {
        "type": "spike_crash",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 12,
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "min_score": 7.0,
    },
    "CRASH1000": {
        "type": "spike_crash",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 18,
        "ema_distance_pct": 0.05,
        "require_fvg_mitigation": True,
        "min_score": 7.0,
    },
}

# Default profile for symbols not explicitly listed.
_DEFAULT_VOLATILITY_PROFILE: dict = {
    "type": "volatility",
    "min_hurst": 0.55,
    "use_mean_reversion": True,
    "band_sigma": 2.00,
    "ema_trend_filter": False,
    "min_score": 5.5,
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
