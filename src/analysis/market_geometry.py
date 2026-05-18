"""
src/analysis/market_geometry.py
─────────────────────────────────────────────────────────────────────────────
Vectorised multi-timeframe market geometry for Deriv synthetic indices.

Computes:
  • Linear regression channel (centre ± 2σ residual bands) on macro + micro
    tick windows equivalent to M15/M30 and M5/M1 timescales.
  • Dynamic pivot-based Support / Resistance cluster zones.
  • Hurst-adaptive confluence signal:
      H > 0.56  → trend-following (pullback to lower channel band = buy)
      H < 0.45  → counter-trend  (fade at upper channel band = sell)
      neutral   → mild penalty when price is overextended

All operations are NumPy-vectorised.
Target: < 2 ms per call on 1,000-tick buffers.

Public API
──────────
  compute_geometry(ticks, hurst=None) → GeometryResult
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

import numpy as np

# SMC bonus weight — configurable via env var.
# Default 3.0: FVG mitigation + BOS / divergence confluence → +3.0.
# At +3.0 a baseline score (4.0-5.0) + SMC = 7.0-8.0 → passes spike min_score.
# FVG alone (no secondary confirmation) → +1.0 (partial signal).
_SMC_BONUS = float(os.getenv("DERIV_SMC_BONUS", "3.0"))  # full confluence bonus
_SMC_PARTIAL = float(os.getenv("DERIV_SMC_PARTIAL", "1.0"))  # FVG alone

# ── Window sizes (ticks ≈ 1–2 ticks/s on Deriv synthetics) ──────────────────
WIN_MACRO = 500     # ~M15  (last 500 ticks)
WIN_MICRO = 150     # ~M5   (last 150 ticks)

# ── Channel bands: ±N residual std-deviations ────────────────────────────────
BAND_WIDTH = 2.0    # ± 2σ

# ── Pivot detection: local extrema neighbourhood ─────────────────────────────
PIVOT_ORDER = 5     # peak/trough must be extreme over ±5 ticks

# ── S/R cluster tolerance: group pivots within this fraction of price range ──
CLUSTER_TOL_PCT = 0.002   # 0.2 %


# ─── Result dataclasses ───────────────────────────────────────────────────────

@dataclass(slots=True)
class ChannelGeometry:
    """Linear regression channel snapshot for one tick window."""
    slope: float          # m  (> 0 uptrend, < 0 downtrend)
    intercept: float      # b
    upper: float          # mid + BAND_WIDTH * residual_std  at last tick
    mid: float            # centre value at last tick
    lower: float          # mid - BAND_WIDTH * residual_std  at last tick
    residual_std: float   # half-width of channel in price units
    r2: float             # R²  (0–1)
    n: int                # ticks used


@dataclass(slots=True)
class SRZones:
    """Nearest dynamic support / resistance from pivot clusters."""
    resistance: float           # nearest cluster ABOVE current price (inf if none)
    support: float              # nearest cluster BELOW current price (-inf if none)
    resistance_strength: float  # normalised density 0–1
    support_strength: float


@dataclass(slots=True)
class GeometryResult:
    """Full geometry snapshot for one symbol."""
    macro: ChannelGeometry | None   # ~M15 channel
    micro: ChannelGeometry | None   # ~M5  channel
    sr: SRZones | None
    geo_side: str | None            # "MULTUP" / "MULTDOWN" / None (advisory)
    geo_score_delta: float          # score bonus/penalty to apply
    geo_reason: str                 # human-readable label
    # ── SMC / Microstructure confluence (lazy-populated; default neutral) ──
    fvg_active: bool = False        # an unmitigated Fair Value Gap is open
    fvg_top: float = 0.0            # upper edge of the active FVG
    fvg_bottom: float = 0.0         # lower edge of the active FVG
    fvg_mid: float = 0.0            # 50 % mitigation level
    fvg_direction: str = ""         # "bullish" / "bearish" / ""
    gap_mitigated: bool = False     # current tick has reached the 50 % level
    divergence: str = ""            # "bullish" / "bearish" / ""
    bos_confirmed: bool = False     # Break of Structure confirmed
    bos_direction: str = ""         # "bullish" / "bearish" / ""
    smc_side: str | None = None     # composite SMC suggestion (MULTUP/MULTDOWN)
    smc_bonus: float = 0.0          # +3.0 (full) or +1.0 (partial FVG)
    smc_reason: str = ""            # human-readable label
    micro_band_signal: str | None = None  # micro-channel 1.5σ touch signal


# ── Microstructure parameters (SMC + Momentum) ───────────────────────────────
FVG_LOOKBACK      = 80        # ticks of recent history to scan for a fresh gap
FVG_MIN_GAP_PCT   = 0.00010   # 0.010 %  minimum gap width to be relevant
ROC_PERIOD        = 14        # ticks for momentum rate-of-change
DIV_PIVOT_RANGE   = 30        # ticks to evaluate price/momentum divergence
MICRO_BAND_SIGMA  = 1.5       # σ band of micro-channel for scalp trigger
BOS_LOOKBACK      = 60        # ticks to scan for swing pivots (BOS detection)
BOS_PIVOT_ORDER   = 4         # pivot neighbourhood for BOS extrema


# ─── Internal computations ────────────────────────────────────────────────────

def _regression_channel(prices: np.ndarray) -> ChannelGeometry:
    """Fit linear OLS + ±BAND_WIDTH σ residual bands."""
    n = len(prices)
    x = np.arange(n, dtype=np.float64)
    m, b = np.polyfit(x, prices, 1)

    fitted = m * x + b
    residuals = prices - fitted
    std = float(np.std(residuals, ddof=1)) if n > 2 else 0.0

    ss_res = float(np.dot(residuals, residuals))
    ss_tot = float(np.sum((prices - prices.mean()) ** 2))
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    last = n - 1
    centre = float(m * last + b)
    return ChannelGeometry(
        slope=float(m),
        intercept=float(b),
        upper=centre + BAND_WIDTH * std,
        mid=centre,
        lower=centre - BAND_WIDTH * std,
        residual_std=std,
        r2=r2,
        n=n,
    )


def _pivot_sr_zones(prices: np.ndarray, current_price: float) -> SRZones:
    """Detect local extrema, cluster them, return nearest S/R to current price."""
    n = len(prices)
    if n < PIVOT_ORDER * 2 + 1:
        return SRZones(float("inf"), float("-inf"), 0.0, 0.0)

    price_range = float(prices.max() - prices.min())
    if price_range == 0:
        return SRZones(float("inf"), float("-inf"), 0.0, 0.0)

    tol = price_range * CLUSTER_TOL_PCT
    order = PIVOT_ORDER

    peaks: list[float] = []
    troughs: list[float] = []

    # Vectorised extrema detection using stride-trick windows
    for i in range(order, n - order):
        window = prices[i - order: i + order + 1]
        p = prices[i]
        if p == window.max() and p > prices.mean():
            peaks.append(float(p))
        elif p == window.min() and p < prices.mean():
            troughs.append(float(p))

    def _cluster(levels: list[float]) -> list[tuple[float, float]]:
        if not levels:
            return []
        arr = np.sort(np.array(levels))
        result: list[tuple[float, float]] = []
        i = 0
        while i < len(arr):
            j = i
            while j < len(arr) and arr[j] - arr[i] <= tol:
                j += 1
            centre = float(arr[i:j].mean())
            strength = min(1.0, (j - i) / max(1, len(arr)))
            result.append((centre, strength))
            i = j
        return result

    res_clusters = sorted(
        [(c, s) for c, s in _cluster(peaks) if c > current_price],
        key=lambda x: x[0]
    )
    sup_clusters = sorted(
        [(c, s) for c, s in _cluster(troughs) if c < current_price],
        key=lambda x: -x[0]
    )

    r = res_clusters[0] if res_clusters else (float("inf"), 0.0)
    s = sup_clusters[0] if sup_clusters else (float("-inf"), 0.0)

    return SRZones(
        resistance=r[0], support=s[0],
        resistance_strength=r[1], support_strength=s[1],
    )


# ─── Public entry point ───────────────────────────────────────────────────────

# ── SMC / Microstructure detectors (vectorised, < 0.5 ms) ────────────────────

def _detect_fvg(prices: np.ndarray) -> tuple[bool, float, float, str]:
    """Detect the most recent unmitigated Fair Value Gap (3-tick imbalance).

    A bullish FVG is registered when prices[i+2].low > prices[i].high
    (we approximate low/high using rolling min/max over a 3-tick block).
    Conversely for bearish FVGs.

    Returns
    -------
    (active, top, bottom, direction)
        active     : True if a fresh, unmitigated FVG is still open
        top        : upper edge of the gap
        bottom     : lower edge of the gap
        direction  : "bullish" / "bearish" / ""
    """
    n = len(prices)
    if n < FVG_LOOKBACK + 3:
        return False, 0.0, 0.0, ""

    seg = prices[-FVG_LOOKBACK:]
    current = float(prices[-1])
    # Iterate newest → oldest so we return the most recent unmitigated gap
    for i in range(len(seg) - 3, 1, -1):
        a_high = float(max(seg[i - 1], seg[i]))
        a_low  = float(min(seg[i - 1], seg[i]))
        c_high = float(max(seg[i + 1], seg[i + 2]))
        c_low  = float(min(seg[i + 1], seg[i + 2]))

        # Bullish gap: candle C low strictly above candle A high
        gap = c_low - a_high
        if gap > a_high * FVG_MIN_GAP_PCT:
            top, bottom = c_low, a_high
            # Mitigation check: has price entered the gap since then?
            tail = seg[i + 2:]
            if tail.min() > bottom:        # never re-entered → still active
                return True, top, bottom, "bullish"
            continue                       # already mitigated → keep scanning

        # Bearish gap: candle A low strictly above candle C high
        gap = a_low - c_high
        if gap > a_low * FVG_MIN_GAP_PCT:
            top, bottom = a_low, c_high
            tail = seg[i + 2:]
            if tail.max() < top:
                return True, top, bottom, "bearish"
            continue

    return False, 0.0, 0.0, ""


def _detect_divergence(prices: np.ndarray) -> str:
    """Detect classic momentum divergence between price and ROC(14).

    Bullish: price prints a LOWER low while ROC prints a HIGHER low.
    Bearish: price prints a HIGHER high while ROC prints a LOWER high.

    Returns "bullish", "bearish" or "".
    """
    n = len(prices)
    if n < ROC_PERIOD + DIV_PIVOT_RANGE + 2:
        return ""

    # Rate of Change (vectorised)
    roc = (prices[ROC_PERIOD:] - prices[:-ROC_PERIOD]) / np.maximum(
        np.abs(prices[:-ROC_PERIOD]), 1e-12
    )
    if roc.size < DIV_PIVOT_RANGE + 2:
        return ""

    window_prices = prices[-DIV_PIVOT_RANGE:]
    window_roc    = roc[-DIV_PIVOT_RANGE:]

    # Compare the most recent half vs the prior half (two pivots)
    half = DIV_PIVOT_RANGE // 2
    p_prev, p_curr = window_prices[:half], window_prices[half:]
    r_prev, r_curr = window_roc[:half],   window_roc[half:]

    if p_curr.min() < p_prev.min() and r_curr.min() > r_prev.min():
        return "bullish"
    if p_curr.max() > p_prev.max() and r_curr.max() < r_prev.max():
        return "bearish"
    return ""


def _micro_band_signal(micro: ChannelGeometry, current_price: float) -> str | None:
    """Return MULTUP / MULTDOWN if price touches the micro 1.5σ band, else None."""
    if micro is None or micro.residual_std <= 0:
        return None
    upper_inner = micro.mid + MICRO_BAND_SIGMA * micro.residual_std
    lower_inner = micro.mid - MICRO_BAND_SIGMA * micro.residual_std
    if current_price <= lower_inner:
        return "MULTUP"
    if current_price >= upper_inner:
        return "MULTDOWN"
    return None


def _detect_bos(prices: np.ndarray) -> tuple[bool, str]:
    """Break of Structure (BOS) detector.

    Scans the last BOS_LOOKBACK ticks for swing pivots and checks if the
    current price has broken the most recent swing structure:

    Bullish BOS  — last confirmed Higher Low sequence (swing_lows[-1] >
                   swing_lows[-2]) AND current price > most recent swing high.
    Bearish BOS  — last confirmed Lower High sequence (swing_highs[-1] <
                   swing_highs[-2]) AND current price < most recent swing low.

    Returns (confirmed: bool, direction: str)  where direction is
    "bullish", "bearish", or "" when not confirmed.
    """
    n = len(prices)
    if n < BOS_LOOKBACK + BOS_PIVOT_ORDER * 2:
        return False, ""

    seg = prices[-BOS_LOOKBACK:]
    order = BOS_PIVOT_ORDER
    seg_n = len(seg)

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(order, seg_n - order):
        window = seg[i - order: i + order + 1]
        p = float(seg[i])
        if p == float(window.max()) and p > float(seg.mean()):
            swing_highs.append(p)
        elif p == float(window.min()) and p < float(seg.mean()):
            swing_lows.append(p)

    current = float(prices[-1])

    # ── Bullish BOS: Higher Lows structure + price above last swing high ──
    if len(swing_lows) >= 2 and len(swing_highs) >= 1:
        if swing_lows[-1] > swing_lows[-2]:          # Higher Low confirmed
            if current > swing_highs[-1]:            # breaks above last high
                return True, "bullish"

    # ── Bearish BOS: Lower Highs structure + price below last swing low ──
    if len(swing_highs) >= 2 and len(swing_lows) >= 1:
        if swing_highs[-1] < swing_highs[-2]:        # Lower High confirmed
            if current < swing_lows[-1]:             # breaks below last low
                return True, "bearish"

    # ── Fallback: simple new-high / new-low break in BOS_LOOKBACK window ──
    # Catches setups where clean swing structure is absent (low-volatility).
    half = seg_n // 2
    prior_high = float(seg[:half].max())
    prior_low  = float(seg[:half].min())
    if current > prior_high:
        return True, "bullish"
    if current < prior_low:
        return True, "bearish"

    return False, ""


# ─── Public entry point ───────────────────────────────────────────────────────

def compute_geometry(
    ticks: Sequence[float],
    hurst: float | None = None,
    fvg_mit_pct: float | None = None,
) -> GeometryResult:
    """
    Compute multi-timeframe channel geometry and Hurst-adaptive signal.

    Parameters
    ----------
    ticks : sequence of float
        Raw price buffer, most recent = last element.  Any length >= 20.
    hurst : float | None
        Hurst exponent (0–1) from the analyst:
          > 0.56  → trending  → pro-trend pullback logic
          < 0.45  → ranging   → mean-reversion fade logic
          else    → neutral
    fvg_mit_pct : float | None
        Override the FVG mitigation penetration threshold for this call.
        When None, falls back to DERIV_FVG_MIT_PCT env var (default 0.50).
        Spike markets (BOOM/CRASH) should pass 0.15 — spikes graze FVGs
        without fully penetrating them.
    """
    arr = np.asarray(ticks, dtype=np.float64)
    n = len(arr)
    current_price = float(arr[-1])

    # ── Channels ──────────────────────────────────────────────────────────────
    macro: ChannelGeometry | None = None
    micro: ChannelGeometry | None = None
    sr: SRZones | None = None

    macro_n = min(n, WIN_MACRO)
    if macro_n >= 30:
        macro = _regression_channel(arr[-macro_n:])

    micro_n = min(n, WIN_MICRO)
    if micro_n >= 20:
        micro = _regression_channel(arr[-micro_n:])

    if macro_n >= 30:
        sr = _pivot_sr_zones(arr[-macro_n:], current_price)

    if macro is None:
        return GeometryResult(
            macro=None, micro=None, sr=None,
            geo_side=None, geo_score_delta=0.0,
            geo_reason="insufficient_ticks",
        )

    # ── Position within macro channel ────────────────────────────────────────
    channel_width = macro.upper - macro.lower
    if channel_width > 0:
        channel_pos = (current_price - macro.mid) / (channel_width / 2.0)
    else:
        channel_pos = 0.0
    channel_pos = float(np.clip(channel_pos, -2.0, 2.0))

    geo_side: str | None = None
    geo_score_delta = 0.0
    reasons: list[str] = []

    if hurst is not None and hurst > 0.56:
        # ── TREND MODE: trade pullbacks within channel ─────────────────────
        slope_pct = macro.slope / current_price * 100.0

        if macro.slope > 0:
            # Uptrend — buy pullbacks near lower band
            if channel_pos < -0.40:
                geo_side = "MULTUP"
                geo_score_delta = 1.5
                reasons.append(
                    f"trend_pullback_buy: slope={slope_pct:+.4f}%/tick "
                    f"channel_pos={channel_pos:.2f}"
                )
            elif channel_pos > 0.70:
                # Near channel ceiling — reduce conviction to buy
                geo_score_delta = -1.0
                reasons.append(
                    f"trend_channel_ceiling: pos={channel_pos:.2f} → conviction-1.0"
                )
            else:
                geo_side = "MULTUP"
                geo_score_delta = 0.3
                reasons.append(f"trend_uptrend_mid: pos={channel_pos:.2f}")
        else:
            # Downtrend — sell bounces near upper band
            if channel_pos > 0.40:
                geo_side = "MULTDOWN"
                geo_score_delta = 1.5
                reasons.append(
                    f"trend_pullback_sell: slope={slope_pct:+.4f}%/tick "
                    f"channel_pos={channel_pos:.2f}"
                )
            elif channel_pos < -0.70:
                geo_score_delta = -1.0
                reasons.append(
                    f"trend_channel_floor: pos={channel_pos:.2f} → conviction-1.0"
                )
            else:
                geo_side = "MULTDOWN"
                geo_score_delta = 0.3
                reasons.append(f"trend_downtrend_mid: pos={channel_pos:.2f}")

    elif hurst is not None and hurst < 0.45:
        # ── MEAN-REVERSION MODE: fade extremes ────────────────────────────
        if channel_pos > 0.80:
            geo_side = "MULTDOWN"
            geo_score_delta = 2.0
            reasons.append(
                f"mr_fade_short: pos={channel_pos:.2f} (above upper band)"
            )
            # Bonus if also at static resistance cluster
            if sr and sr.resistance < float("inf"):
                dist_res = abs(sr.resistance - current_price) / current_price
                if dist_res < 0.003:
                    geo_score_delta += 0.5
                    reasons.append(
                        f"sr_res_confluence: res={sr.resistance:.5f} "
                        f"str={sr.resistance_strength:.2f}"
                    )

        elif channel_pos < -0.80:
            geo_side = "MULTUP"
            geo_score_delta = 2.0
            reasons.append(
                f"mr_fade_long: pos={channel_pos:.2f} (below lower band)"
            )
            if sr and sr.support > float("-inf"):
                dist_sup = abs(current_price - sr.support) / current_price
                if dist_sup < 0.003:
                    geo_score_delta += 0.5
                    reasons.append(
                        f"sr_sup_confluence: sup={sr.support:.5f} "
                        f"str={sr.support_strength:.2f}"
                    )

        elif channel_pos > 0.45:
            geo_side = "MULTDOWN"
            geo_score_delta = 0.8
            reasons.append(f"mr_lean_short: pos={channel_pos:.2f}")

        elif channel_pos < -0.45:
            geo_side = "MULTUP"
            geo_score_delta = 0.8
            reasons.append(f"mr_lean_long: pos={channel_pos:.2f}")

        else:
            # Price near mean — small bonus for being in the right direction
            geo_score_delta = 0.2
            reasons.append(f"mr_near_mean: pos={channel_pos:.2f}")

    else:
        # Neutral Hurst — mild signal from micro channel only
        if micro is not None:
            if channel_pos > 0.70:
                geo_score_delta = -0.3
                reasons.append(f"neutral_extended_up: pos={channel_pos:.2f}")
            elif channel_pos < -0.70:
                geo_score_delta = -0.3
                reasons.append(f"neutral_extended_down: pos={channel_pos:.2f}")

    # ── SMC / Microstructure layer (advisory) ────────────────────────────
    fvg_active, fvg_top, fvg_bot, fvg_dir = _detect_fvg(arr)
    fvg_mid = (fvg_top + fvg_bot) / 2.0 if fvg_active else 0.0
    gap_mitigated = False
    if fvg_active and fvg_top > fvg_bot:
        # Mitigation threshold: how far into the gap price must penetrate.
        # fvg_mit_pct param overrides the env var (spike markets pass 0.15).
        _fvg_mit_pct = fvg_mit_pct if fvg_mit_pct is not None else float(os.getenv("DERIV_FVG_MIT_PCT", "0.50"))
        gap_height = fvg_top - fvg_bot
        if fvg_dir == "bullish":
            # Price must have dropped at least _fvg_mit_pct into the gap from top
            _mit_level = fvg_top - _fvg_mit_pct * gap_height
            gap_mitigated = current_price <= _mit_level + gap_height * 0.10
        elif fvg_dir == "bearish":
            # Price must have risen at least _fvg_mit_pct into the gap from bottom
            _mit_level = fvg_bot + _fvg_mit_pct * gap_height
            gap_mitigated = current_price >= _mit_level - gap_height * 0.10

    divergence = _detect_divergence(arr)
    micro_band_sig = _micro_band_signal(micro, current_price) if micro else None

    # ── BOS detection ──────────────────────────────────────────────────────
    bos_ok, bos_dir = _detect_bos(arr)

    smc_side: str | None = None
    smc_bonus = 0.0
    smc_reason = ""

    if fvg_active and gap_mitigated:
        # Secondary confirmation: BOS OR momentum divergence (either suffices)
        _bull_secondary = (
            (bos_ok and bos_dir == "bullish") or divergence == "bullish"
        )
        _bear_secondary = (
            (bos_ok and bos_dir == "bearish") or divergence == "bearish"
        )
        _bull_tag = (
            "BOS" if (bos_ok and bos_dir == "bullish")
            else ("div" if divergence == "bullish" else "")
        )
        _bear_tag = (
            "BOS" if (bos_ok and bos_dir == "bearish")
            else ("div" if divergence == "bearish" else "")
        )

        if fvg_dir == "bullish" and _bull_secondary:
            smc_side, smc_bonus = "MULTUP", _SMC_BONUS
            smc_reason = (
                f"SMC_Liquidity_Inbound: bull_fvg mid={fvg_mid:.5f} "
                f"mitigated+{_bull_tag} → +{smc_bonus:.1f}"
            )
        elif fvg_dir == "bearish" and _bear_secondary:
            smc_side, smc_bonus = "MULTDOWN", _SMC_BONUS
            smc_reason = (
                f"SMC_Liquidity_Inbound: bear_fvg mid={fvg_mid:.5f} "
                f"mitigated+{_bear_tag} → +{smc_bonus:.1f}"
            )
        elif fvg_dir == "bullish":
            # Partial signal: FVG mitigated but no secondary confirmation yet
            smc_side, smc_bonus = "MULTUP", _SMC_PARTIAL
            smc_reason = (
                f"SMC_FVG_partial: bull_fvg mid={fvg_mid:.5f} mitigated "
                f"(no BOS/div yet) → +{smc_bonus:.1f}"
            )
        elif fvg_dir == "bearish":
            smc_side, smc_bonus = "MULTDOWN", _SMC_PARTIAL
            smc_reason = (
                f"SMC_FVG_partial: bear_fvg mid={fvg_mid:.5f} mitigated "
                f"(no BOS/div yet) → +{smc_bonus:.1f}"
            )

    return GeometryResult(
        macro=macro,
        micro=micro,
        sr=sr,
        geo_side=geo_side,
        geo_score_delta=geo_score_delta,
        geo_reason=" | ".join(reasons) if reasons else "geo_neutral",
        fvg_active=fvg_active,
        fvg_top=fvg_top,
        fvg_bottom=fvg_bot,
        fvg_mid=fvg_mid,
        fvg_direction=fvg_dir,
        gap_mitigated=gap_mitigated,
        divergence=divergence,
        bos_confirmed=bos_ok,
        bos_direction=bos_dir,
        smc_side=smc_side,
        smc_bonus=smc_bonus,
        smc_reason=smc_reason,
        micro_band_signal=micro_band_sig,
    )
