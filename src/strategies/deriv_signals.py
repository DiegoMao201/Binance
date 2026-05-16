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

# Number of seconds (≈ ticks at 1 tick/s) to hold a BOOM/CRASH contract.
# If the spike has not fired within this window the bot force-closes to cap
# the inter-spike drift loss.  Set to 0 to disable time-based force-close.
_SPIKE_TIMEOUT_SEC: int = int(os.getenv("BOOM_CRASH_SPIKE_TIMEOUT_SEC", "10"))


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
