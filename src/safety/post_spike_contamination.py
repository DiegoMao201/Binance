"""
Post-Spike Contamination Detector — PASO 2.5 L.6.1

Diego observed (2026-06-17) on BOOM900 and CRASH500:
  "si el bot y el orquestador se comen una señal inflada por el spike
   estamos jodidos perseguimos spikes solo sera eso"

After a spike, score and RNG components can be inflated by the spike itself:
  - geo_channel_pos    → spike pushes price to channel edge
  - kinetic_compressed → spike elevates kinetic reading
  - score_atr_bonus    → ATR rises post-spike
  - hurst momentarily  → spike biases regime detection

maturity_ratio (progressive_imminence_ratio) is the ONLY contamination-proof
signal: it resets to 0.0 with each spike.

Contamination levels:
  score >= 4  → high   → blocked
  score >= 2  → medium → blocked
  score >= 1  → low    → not blocked, reported only
  score == 0  → none
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

_LOGGER = logging.getLogger(__name__)


def detect_contamination(
    symbol: str,
    score_breakdown: Dict,
    last_spike_ts: Optional[float] = None,
) -> Dict:
    """
    Detect if current signal is contaminated by a recent spike.

    Uses score_breakdown keys available from both snap.score_breakdown (post-evaluate)
    and from the _last_fvg_state cache (pre-evaluate, previous cycle).

    Returns:
        contaminated      : bool — True if confidence is medium or high
        confidence        : 'high' | 'medium' | 'low' | 'none'
        contamination_score: int
        reasons           : list[str]
        safe_to_enter     : bool — equivalent to NOT contaminated
        maturity_ratio    : float — the primary probe value
    """
    reasons: list[str] = []
    contamination_score = 0

    # Primary probe: maturity_ratio (progressive_imminence_ratio is the same field
    # in both snap.score_breakdown and _last_fvg_state cache)
    maturity_ratio = float(
        score_breakdown.get("maturity_ratio")
        or score_breakdown.get("progressive_imminence_ratio")
        or 0.0
    )

    if maturity_ratio < 0.10:
        contamination_score += 3
        reasons.append(f"maturity={maturity_ratio:.2f}<0.10(very_recent_spike)")
    elif maturity_ratio < 0.25:
        contamination_score += 2
        reasons.append(f"maturity={maturity_ratio:.2f}<0.25(recent_spike)")
    elif maturity_ratio < 0.40:
        contamination_score += 1
        reasons.append(f"maturity={maturity_ratio:.2f}<0.40(somewhat_recent)")

    # Indicator 2: geo_channel_pos extreme with early maturity
    geo_pos = float(score_breakdown.get("geo_channel_pos") or 0.0)
    if abs(geo_pos) > 0.85 and maturity_ratio < 0.40:
        contamination_score += 2
        reasons.append(f"geo_pos={geo_pos:+.2f}>0.85_and_early_maturity")

    # Indicator 3: kinetic_compressed high with early maturity
    # Key name is 'kinetic_compressed' (0.0-1.0 float) in deriv_risk evaluate()
    kinetic = float(score_breakdown.get("kinetic_compressed") or 0.0)
    if kinetic > 0.7 and maturity_ratio < 0.30:
        contamination_score += 2
        reasons.append(f"kinetic={kinetic:.2f}>0.7_and_early_maturity")

    # Indicator 4: Hurst extreme with early maturity (spike biases regime)
    hurst = float(score_breakdown.get("hurst") or 0.5)
    if (hurst < 0.35 or hurst > 0.65) and maturity_ratio < 0.30:
        contamination_score += 1
        reasons.append(f"hurst={hurst:.2f}_extreme_and_early_maturity")

    # Indicator 5: spike timestamp proximity (direct check)
    if last_spike_ts and last_spike_ts > 0:
        seconds_since_spike = time.time() - last_spike_ts
        if seconds_since_spike < 60:
            contamination_score += 3
            reasons.append(f"spike_{int(seconds_since_spike)}s_ago(<60)")
        elif seconds_since_spike < 180:
            contamination_score += 2
            reasons.append(f"spike_{int(seconds_since_spike)}s_ago(<180)")

    # Verdict
    if contamination_score >= 4:
        confidence = "high"
        contaminated = True
    elif contamination_score >= 2:
        confidence = "medium"
        contaminated = True
    elif contamination_score >= 1:
        confidence = "low"
        contaminated = False
    else:
        confidence = "none"
        contaminated = False

    result = {
        "contaminated": contaminated,
        "confidence": confidence,
        "contamination_score": contamination_score,
        "reasons": reasons,
        "safe_to_enter": not contaminated,
        "maturity_ratio": maturity_ratio,
    }

    if contamination_score >= 1:
        _LOGGER.debug(
            "[CONTAMINATION_DETECT] %s confidence=%s score=%d reasons=%s",
            symbol, confidence, contamination_score, reasons,
        )

    return result
