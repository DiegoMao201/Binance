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


# ─── Symbol classification dictionaries ──────────────────────────────────────────────
SYMBOL_FAMILY: dict[str, str] = {
    # Volatility continuous indices
    "R_10":  "volatility", "R_25":  "volatility", "R_50":  "volatility",
    "R_75":  "volatility", "R_100": "volatility",
    # BOOM family — all variants
    "BOOM300":  "boom_crash", "BOOM500":  "boom_crash",
    "BOOM600":  "boom_crash", "BOOM900":  "boom_crash",
    "BOOM1000": "boom_crash",
    # CRASH family — all variants
    "CRASH300":  "boom_crash", "CRASH500":  "boom_crash",
    "CRASH600":  "boom_crash", "CRASH900":  "boom_crash",
    "CRASH1000": "boom_crash",
}

# Approximate spike interval in ticks (1 tick ≈ 1 s on Deriv synthetics).
# Used for max_hold and diagnostics: spike_family_interval field in logs.
SPIKE_INTERVAL_TICKS: dict[str, int] = {
    "BOOM300": 300,  "CRASH300": 300,
    "BOOM500": 500,  "CRASH500": 500,
    "BOOM600": 600,  "CRASH600": 600,
    "BOOM900": 900,  "CRASH900": 900,
    "BOOM1000": 1000, "CRASH1000": 1000,
}


def spike_interval_ticks(symbol: str) -> int:
    """Return the approximate tick interval between spikes for spike markets."""
    return SPIKE_INTERVAL_TICKS.get(symbol.upper(), 0)


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
        # ── Phase 28: ratio-corrected SL ──────────────────────────────────
        # 100-trade audit: SL=$1.00 vs avg_win=$0.19 = 5.3:1 adverse ratio.
        # New SL=$0.30 → ratio 1.6:1 → breakeven WR=38% (was 84%).
        # DERIV_R50_SL_USD=0.30 overrides at runtime (default in deriv_config.py).
        "stop_loss_pct_override": 0.20,    # 20% of $1.50 stake → SL $0.30
        "stake_max_usdt": 1.50,
        # Phase 29: DISABLED — 200-trade audit: R_50 destroyed -$4.19 in 50 trades.
        # GBM process by Deriv design — no technical signal works. Focus on BOOM/CRASH.
        "disabled": True,
    },
    # R_75: very erratic, false spikes → require high confluence, NO pure
    # breakouts when H<0.58, prefer reversals/sweeps with ATR expansion.
    # Phase 25: raised min_score 6.5→8.0. Data: 8 sl_inicial WR=0% in one night.
    # Root cause: scores 6.5-7.49 bypass AI gate (gate requires 7.5+) → bad entries
    # with no AI confirmation. Only 8.0+ setups (full AI confirmation + buffer) enter.
    "R_75":  {
        "type": "volatility",
        "strategy_mode": "hybrid",
        "min_hurst": 0.55,
        "use_mean_reversion": False,
        "allow_mean_reversion": True,
        "allow_breakout": False,
        "band_sigma": 2.00,
        "ema_trend_filter": True,
        "min_score": 8.0,
        "atr_min": 0.0,
        "cooldown_sec": 120,
        "sl_multiplier": 1.8,
        "tp_multiplier": 1.5,
        "trailing_mode": "atr_dynamic",
        # ── Phase 28: ratio-corrected SL ──────────────────────────────────
        # 100-trade audit: 1 SL of -$0.78 wiped 7 micro-wins of +$0.105 each.
        # New SL=$0.25 → ratio 1.7:1 vs TP $0.15 → breakeven WR=63% (actual 67%).
        # expectancy = 0.67×$0.15 - 0.33×$0.25 = +$0.018/trade
        # DERIV_R75_SL_USD=0.25 overrides at runtime.
        "stop_loss_pct_override": 0.1667,  # 16.7% of $1.50 stake → SL $0.25
        "stake_max_usdt": 1.50,
        # Phase 29: DISABLED — 200-trade audit: R_75 part of R_* group destroying -$4.19.
        # GBM process by Deriv design — no technical signal works. Focus on BOOM/CRASH.
        "disabled": True,
    },
    # R_100: Phase 28 — ONLY MULTDOWN. Data: MULTDOWN WR=73% PNL=+$0.66 vs
    # MULTUP WR=27% PNL=-$6.56. MULTDOWN+H>0.56: WR=100% PNL=+$1.60.
    "R_100": {
        "type": "volatility",
        "strategy_mode": "hybrid",
        "min_hurst": 0.52,              # Phase 28: was 0.45 — H>0.52 for MULTDOWN edge (H>0.56=WR100%)
        "use_mean_reversion": True,
        "allow_mean_reversion": True,
        "allow_breakout": True,
        "band_sigma": 2.10,
        "ema_trend_filter": True,
        "min_score": 7.5,              # Phase 28: was 6.0 — only high-conviction setups
        "atr_min": 0.0,
        "cooldown_sec": 150,
        "sl_multiplier": 1.5,
        "tp_multiplier": 3.0,
        "trailing_mode": "atr_wide",
        # ── Phase 28: MULTDOWN only + ratio-corrected SL ────────────────────
        # 100-trade audit: MULTUP WR=27% -$6.56 | MULTDOWN WR=73% +$0.66.
        # With MULTDOWN only: expectancy = 0.73×$0.40 - 0.27×$0.35 = +$0.198/trade.
        # side_allowed gate enforced in deriv_risk.py evaluate().
        "side_allowed": ["MULTDOWN"],  # ELIMINATE MULTUP — phase 28 hard rule
        "stop_loss_pct_override": 0.2333,  # 23.3% of $1.50 stake → SL $0.35
        "stake_max_usdt": 1.50,
        # Phase 29: DISABLED — 200-trade audit: R_100 part of R_* group destroying -$4.19.
        # GBM process by Deriv design — no technical signal works. Focus on BOOM/CRASH.
        "disabled": True,
    },
    # ── BOOM: asymmetric accumulation — BUY only / spike capture ─────────────
    # Spike markets: NO mean-reversion, NO breakout — only SMC + spike-hunter.
    "BOOM300": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 20,
        "max_hold_seconds": 270,        # Phase 31: BOOM300 spikes ~every 300 ticks (≈5min); 270s window captures 90%
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 4.5,             # Phase 32: 6.5→4.5 — calm scores 4.09-4.35; profile gate must sit BELOW effective_min
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,        # Phase 31: aligned with other BOOM profiles
        "fvg_tier_minimo": "fvg_detected",  # Phase 31: Tier 1 sufficient — generate data first
        # ── Geo gate: BOOM needs price below channel mid ──
        # BOOM spikes UP from accumulation zones. Extended prices = wrong entry timing.
        "geo_entry_max": 0.50,          # Phase 31: 0.40→0.50 to allow more setups while collecting data
        "stake_max_usdt": 5.00,         # Phase 31: DEMO $5.00 stakes
        "stop_loss_pct_override": 0.36, # Phase 31: align with other BOOM profiles → sl_usd > broker floor
        "spike_capture_tp_usdt": 0.15,  # Phase 31: small spikes still valid exits
        "spike_profit_delta_usdt": 0.10,
        # Phase 31: observation window — wait 60s after signal; if spike occurs during wait → cancel
        # (spike already happened = entry is late). If no spike → proceed (spike still pending).
        "observation_window_sec": 60,
    },
    "BOOM500": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 12,
        "max_hold_seconds": 600,        # Phase 19: MTBS≈500 ticks → 10 min (was 7.5)
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 6.5,
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
        "max_hold_seconds": 390,        # Phase 30: 50-trade audit — spikes occur up to 299s; 390s captures all valid spikes; expectancy +$0.140/trade
        "ema_distance_pct": 0.05,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 4.0,              # Phase 32: 5.0→4.0 — real scores 4.09-4.35; 5.0 was blocking 100% of entries in calm
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "stake_max_usdt": 5.00,         # Phase 31: DEMO $5.00 stakes (was 1.80)
        "stop_loss_pct_override": 0.36, # Phase 27: align with BOOM600/900 → sl_usd > broker floor
        # ── Batch analysis 2026-05-18 ──────────────────────────────────────
        # Sole loss had H=0.402 (below meaningful persistence for spike setup).
        # Wins had geo ∈ [-0.69, +1.04]; loss had geo +1.35 (extended).
        "hurst_min_spike": 0.43,        # veto if hurst < 0.43 even for spike markets
        # DEMO Phase 23: widened 0.40→0.60 (same as BOOM600/900 that trade successfully)
        "geo_entry_max": 0.60,          # DEMO: was 0.40
        "fvg_tier_minimo": "fvg_detected",  # DEMO Phase 23: Tier 1 sufficient (was default)
        # Phase 20: deterministic spike capture — Phase 24: thresholds lowered 0.40→0.15 / 0.22→0.10
        # CRASH600 spike peak +$0.21 didn't trigger $0.40 TP. Small spikes are still real spikes.
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
        # Phase 31: observation window — wait 90s after signal; if spike occurs during wait → cancel
        # (spike already happened = entry is late). If no spike → proceed (spike still pending).
        "observation_window_sec": 90,
    },
    # ── CRASH: asymmetric accumulation — SELL only / spike capture ────────────
    "CRASH300": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 20,
        "max_hold_seconds": 270,        # Phase 31: CRASH300 spikes ~every 300 ticks; 270s window captures 90%
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 4.5,             # Phase 32: 6.5→4.5 — profile gate below effective_min=3.50; let risk engine gate
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        # ── Geo gate: CRASH needs price below channel mid ──────────────────
        # Phase 32: relaxed -0.20→0.30 (aligned with CRASH500/CRASH1000)
        # Root cause: geo_pos=+0.20 → overshoot=0.40 → geo_penalty=-2.0 → score blocked
        # CRASH500 fix in Phase 26 proved +0.30 generates valid data without disabling edge.
        "geo_entry_max": 0.30,          # Phase 32: was -0.20
        # Phase 24: spike capture thresholds — small spikes still valid exits
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
        # Phase 31: full config additions
        "hurst_min_spike": 0.43,        # Phase 31: aligned with other CRASH profiles
        "fvg_tier_minimo": "fvg_detected",  # Phase 31: Tier 1 sufficient — generate data first
        "stake_max_usdt": 5.00,         # Phase 31: DEMO $5.00 stakes
        "stop_loss_pct_override": 0.36, # Phase 31: align with other CRASH profiles → sl_usd > broker floor
    },
    "CRASH500": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 12,
        "max_hold_seconds": 450,        # FIX-1: 150→450 — 200-trade audit: 99% of losses close at exactly 150s (timeout IS the bottleneck); CRASH500 cycle=500s so 450s is needed to capture spikes
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 6.0,              # Phase 26: was 6.5 — align with CRASH600/900
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,        # Phase 26: explicit — align with CRASH600/900
        "fvg_tier_minimo": "fvg_detected",  # Phase 26: explicit Tier 1 OK (was implicit)
        # ── Phase 26: geo_entry_max relaxed to +0.30 ──────────────────────
        # ROOT CAUSE: geo_pos was +0.013-0.038 (price near channel mid).
        # geo_entry_max=-0.20 → overshoot=+0.21 → geo_gate=-1.5 → score 4.15→2.65 < 4.50
        # With +0.30: price near mid passes with geo_gate=0.0, score stays at 4.15.
        # REQUIRES COOLIFY: DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN=3.80 (was 4.50)
        # so that score=4.00-4.15 passes effective_min=3.80.
        "geo_entry_max": 0.30,          # Phase 26: was -0.20 — DEMO data generation
        "trail_floor_min_usdt": 0.20,   # eliminate floor=-1.00 legacy issue
        "stake_max_usdt": 5.00,         # Phase 31: DEMO $5.00 stakes (was 1.80)
        "stop_loss_pct_override": 0.36, # Phase 27: align with BOOM600/900 → sl_usd > broker floor
        "min_score": 4.5,              # Phase 32: 5.0→4.5 — profile gate below effective_min=3.50; let risk engine be the gate
        # Phase 24: spike capture thresholds
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
    },
    "CRASH1000": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 18,
        "max_hold_seconds": 450,        # FIX-1: 150→450 — 200-trade audit: 99% losses hit timeout at 150s; CRASH1000 cycle=1000s so 450s needed
        "ema_distance_pct": 0.05,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 4.5,              # Phase 32: 6.0→4.5 — profile gate below effective_min=3.50
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,        # Phase 26: explicit — align with CRASH1000 batch data
        # ── Phase 26: geo_entry_max relaxed to +0.30 ──────────────────────
        # ROOT CAUSE: geo_pos was +0.55 (price above channel mid).
        # geo_entry_max=-0.20 → overshoot=+0.75 → geo_gate=-2.0 → score 4.21→2.21 < 4.50
        # With +0.30: overshoot=+0.25 → geo_gate=-1.5 → score 4.21-1.5=2.71 still blocked.
        # At geo_pos=+0.55 price is structurally wrong for CRASH. Score will be low.
        # REQUIRES COOLIFY: DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN=3.80
        # so baseline scores 4.0+ can pass when price near mid.
        "geo_entry_max": 0.30,          # Phase 26: was -0.20 — DEMO data generation
        "spike_family": "boom_crash",
        "spike_interval_ticks": 1000,
        "fvg_tier_minimo": "fvg_detected",  # DEMO Phase 23: allow Tier 1 (FVG detected)
        "stake_max_usdt": 5.00,         # Phase 31: DEMO $5.00 stakes (was 1.80)
        "stop_loss_pct_override": 0.36, # Phase 27: align with CRASH600/900 → sl_usd > broker floor
        # Phase 20/24: deterministic spike capture — thresholds lowered 0.40→0.15
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
    },
    # ── NEW: BOOM/CRASH 900 — active ───────────────────────────────────────────────
    "BOOM900": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 18,
        "max_hold_seconds": 1200,       # Phase 19: MTBS≈900 ticks → 20 min (was 450 s)
        "ema_distance_pct": 0.04,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 6.00,              # T5 2026-05-19: lowered 7.0→6.00 (in line with calm floor)
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,        # confirmed same as BOOM1000
        "geo_entry_min": -999,           # no lower bound — BOOM spikes can come from any depth
        "geo_entry_max": 0.50,          # T5: widened 0.40→0.50 to allow more setups
        "stake_max_usdt": 2.00,
        "stop_loss_pct_override": 0.36,
        "trail_stop_floor_min": 0.20,
        "ratchet_enabled": True,
        "spike_family": "boom_crash_new",
        "spike_interval_ticks": 900,
        "fvg_tier_minimo": "fvg_detected",
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
        # FIX-5: re-enabled — 178 spikes wasted in 7.5h session without trading BOOM900
        # BUG-1: removed min_hd_bonus=2.0 — FIX-9 neutralizes mild opposing slopes to hd=0.0,
        # which permanently triggers the >=2.0 gate. Phase 28 gate was designed for disabled state.
    },
    "CRASH900": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 18,
        "max_hold_seconds": 120,       # Phase 30: 50-trade audit — spikes at 36-92s; 120s vs 150s cuts timeout cost → expectancy +$0.021/trade
        "ema_distance_pct": 0.04,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 4.5,              # Phase 32: 6.0→4.5 — align profile gate below effective_min=3.50
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,
        "geo_entry_min": -999,
        # Phase 32: relaxed -0.20→0.30 — CRASH900 geo blocked by ATR+geo combo
        # Aligned with CRASH500/CRASH1000 which use +0.30 and were actually trading
        "geo_entry_max": 0.30,          # Phase 32: was -0.20
        "stake_max_usdt": 5.00,         # Phase 31
        "stop_loss_pct_override": 0.36,
        "trail_stop_floor_min": 0.20,
        "ratchet_enabled": True,
        "spike_family": "boom_crash_new",
        "spike_interval_ticks": 900,
        "fvg_tier_minimo": "fvg_detected",
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
        "max_hold_seconds": 200,       # Phase 32: 120→200 — give more time to capture spikes (120s too tight)
    },
    # ── NEW: BOOM/CRASH 600 — active, conservative until ≥20 trades confirm edge ──
    "BOOM600": {
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 15,
        "max_hold_seconds": 780,        # Phase 19: MTBS≈600 ticks → 13 min (was 450 s)
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 6.00,              # T5 2026-05-19: lowered 7.5→6.00
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,        # T5: aligned with BOOM1000 (was 0.47)
        "geo_entry_min": -999,           # no lower bound — BOOM spikes valid from any depth
        "geo_entry_max": 0.50,          # T5: widened 0.40→0.50
        "stake_max_usdt": 2.00,         # T5: conservative 6.0→2.00
        "stop_loss_pct_override": 0.36,  # same SL fix as R_50/R_75 → sl_usd > broker floor
        "trail_stop_floor_min": 0.20,   # DPM: lock profit above $0.20
        "ratchet_enabled": True,        # DPM ratchet active
        "spike_family": "boom_crash_new",
        "spike_interval_ticks": 600,
        "fvg_tier_minimo": "fvg_detected",  # FIX-5: Tier 1 sufficient (was fvg_mitigated — too strict)
        # Phase 20: deterministic spike capture — Phase 24: thresholds lowered
        "spike_capture_tp_usdt": 0.15,       # was 0.50
        "spike_profit_delta_usdt": 0.10,     # was 0.28
        # FIX-5: enabled — 178 spikes wasted in 7.5h session; collecting real data now
    },
    "CRASH600": {
        "type": "spike_crash",
        "strategy_mode": "spike",
        "forced_side": "MULTDOWN",
        "max_hold_ticks": 15,
        "max_hold_seconds": 780,        # Phase 19: MTBS≈600 ticks → 13 min (was 450 s)
        "ema_distance_pct": 0.03,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 6.00,              # T5 2026-05-19: lowered 7.5→6.00
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 300,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.43,        # T5: aligned with CRASH1000 (was 0.42)
        "geo_entry_min": -999,           # no upper floor — use geo_max only
        # FIX-5: geo_entry_max relaxed -0.20→0.30 (aligned with CRASH500/900/1000)
        # and disabled removed — 178 spikes wasted in 7.5h session with 0 entries.
        "geo_entry_max": 0.30,
        "stake_max_usdt": 2.00,
        "stop_loss_pct_override": 0.36,
        "trail_stop_floor_min": 0.20,
        "ratchet_enabled": True,
        "spike_family": "boom_crash_new",
        "spike_interval_ticks": 600,
        "fvg_tier_minimo": "fvg_detected",
        "spike_capture_tp_usdt": 0.15,
        "spike_profit_delta_usdt": 0.10,
        "max_hold_seconds": 450,        # FIX-1/5: 270→450 aligned with CRASH500/1000 cycle timing
    },
    # ── NEW: BOOM/CRASH 300 — DISABLED until ≥20 trades of real data ────────────
    # BOOM500 had WR=0% (worst performer). BOOM300 = even higher frequency = more noise.
    # Defined so DPM params are ready; active=False + score_min=8.50 = effective block.
    # To activate: set active=True + min_score=7.50 + hurst_min_spike=0.45 after
    # seeing ≥20 trades with geo_channel_pos<-0.70 for BOOM / <-1.40 for CRASH.
    "BOOM300_new": {
        # NOTE: registered as BOOM300_new to avoid conflict with existing BOOM300 profile.
        # Existing BOOM300 profile (above) inherited from original codebase with different
        # parameters. This profile is for the new 300-tick variants to be activated later.
        "type": "spike_boom",
        "strategy_mode": "spike",
        "forced_side": "MULTUP",
        "max_hold_ticks": 10,
        "max_hold_seconds": 270,        # 90 % × 300 ticks ≈ 270 s
        "ema_distance_pct": 0.02,
        "require_fvg_mitigation": True,
        "allow_mean_reversion": False,
        "allow_breakout": False,
        "min_score": 8.50,              # impossible threshold = effective block
        "min_hurst": 0.0,
        "atr_min": 0.0,
        "cooldown_sec": 240,
        "sl_multiplier": 3.0,
        "tp_multiplier": 6.0,
        "trailing_mode": "none",
        "hurst_min_spike": 0.99,        # impossible = extra block
        "geo_entry_max": 0.40,
        "stake_max_usdt": 4.0,
        "disabled": True,               # ← DISABLED
        "spike_family": "boom_crash_new",
        "spike_interval_ticks": 300,
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
_ATR_FILTER_SYMBOLS: set[str] = {
    "BOOM500", "BOOM600", "BOOM900", "BOOM1000",
    "CRASH500", "CRASH600", "CRASH900", "CRASH1000",
}


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

