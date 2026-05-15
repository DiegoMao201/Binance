"""Per-market entry strategy profiles.

Each profile only overrides ENTRY GUARDRAILS (RSI scenarios, ATR ranges,
volume ratio, orderbook imbalance, trade flow, AI confidence, max spread,
guardrail relaxation). SL/TP/trailing tiers REMAIN GLOBAL — a winning trade
on BTC closes the same way as a winning trade on WIF.

The overlay function `apply_market_profile(settings, symbol)` returns a NEW
Settings instance with the per-market fields patched. Use it in `_scan_symbol`
so every guardrail call inside the symbol scan sees the right thresholds.

Design rationale
----------------
- BTC = slowest, most institutional → demand higher confidence, tighter spread.
- ETH = fast but liquid → near-default thresholds.
- SOL = explosive moves → permissive ATR ceiling, lower flow gate.
- BNB = native exchange asset → moderate everything.
- DOGE = meme momentum → lower confidence required, wider ATR, looser flow.
- WIF = ultra-volatile small-cap → loosest gates, highest ATR ceiling.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.utils.config import Settings


# Fields that may be overridden by a market profile. Anything NOT in this set
# is preserved from the global Settings unchanged.
_OVERRIDABLE_FIELDS: tuple[str, ...] = (
    "scenario_a_rsi_max",
    "scenario_b_rsi_max",
    "min_atr_pct",
    "max_atr_pct",
    "min_volume_ratio",
    "min_orderbook_imbalance",
    "min_trade_flow_score",
    "max_spread_pct",
    "ai_confidence_threshold",
    "guardrail_relaxation",
    "min_bb_width_pct",
)


# Per-market entry profiles. Keys are normalised symbols (uppercase, '/').
# Comments next to each value explain the empirical/structural reason.
MARKET_PROFILES: dict[str, dict[str, float]] = {
    "BTC/USDT": {
        # Oro digital. RSI rara vez sobreextiende como una alt — exigimos
        # pullback más limpio antes de entrar, y sobreventa real más profunda.
        "scenario_a_rsi_max": 48.0,
        "scenario_b_rsi_max": 32.0,
        # ATR de BTC en 5m vive entre 0.05% y 1.2% — recortamos los extremos.
        "min_atr_pct": 0.0008,
        "max_atr_pct": 0.0150,
        # Volumen institucional → exigimos consenso fuerte.
        "min_volume_ratio": 0.20,
        "min_orderbook_imbalance": 0.50,
        "min_trade_flow_score": 0.50,
        # Spread de BTC en Binance suele ser <0.05% → si se ensancha es señal mala.
        "max_spread_pct": 0.0008,
        # Convicción alta requerida — BTC no perdona setups borderline.
        "ai_confidence_threshold": 0.60,
        "guardrail_relaxation": 0.06,
        "min_bb_width_pct": 0.0,
    },
    "ETH/USDT": {
        # Plata líquida. Reactivo pero ordenado; defaults moderados.
        "scenario_a_rsi_max": 50.0,
        "scenario_b_rsi_max": 34.0,
        "min_atr_pct": 0.0010,
        "max_atr_pct": 0.0200,
        "min_volume_ratio": 0.15,
        "min_orderbook_imbalance": 0.46,
        "min_trade_flow_score": 0.46,
        "max_spread_pct": 0.0010,
        "ai_confidence_threshold": 0.55,
        "guardrail_relaxation": 0.08,
        "min_bb_width_pct": 0.0,
    },
    "SOL/USDT": {
        # Velocista. Pullbacks shallower y movimientos explosivos: aceptamos
        # ATR mucho mayor y flow_score más permisivo.
        "scenario_a_rsi_max": 52.0,
        "scenario_b_rsi_max": 36.0,
        "min_atr_pct": 0.0012,
        "max_atr_pct": 0.0300,
        "min_volume_ratio": 0.12,
        "min_orderbook_imbalance": 0.44,
        "min_trade_flow_score": 0.44,
        "max_spread_pct": 0.0012,
        "ai_confidence_threshold": 0.55,
        "guardrail_relaxation": 0.10,
        "min_bb_width_pct": 0.0,
    },
    "BNB/USDT": {
        # Activo nativo del exchange — buena ejecución, vol moderada.
        "scenario_a_rsi_max": 52.0,
        "scenario_b_rsi_max": 36.0,
        "min_atr_pct": 0.0010,
        "max_atr_pct": 0.0220,
        "min_volume_ratio": 0.15,
        "min_orderbook_imbalance": 0.45,
        "min_trade_flow_score": 0.45,
        "max_spread_pct": 0.0010,
        "ai_confidence_threshold": 0.53,
        "guardrail_relaxation": 0.08,
        "min_bb_width_pct": 0.0,
    },
    "DOGE/USDT": {
        # Scalper salvaje. Memes sobreextienden RSI sin ser techo;
        # rebotes desde 38 son violentos. Aceptamos volumen irregular.
        "scenario_a_rsi_max": 55.0,
        "scenario_b_rsi_max": 38.0,
        "min_atr_pct": 0.0015,
        "max_atr_pct": 0.0400,
        "min_volume_ratio": 0.10,
        "min_orderbook_imbalance": 0.42,
        "min_trade_flow_score": 0.42,
        "max_spread_pct": 0.0020,
        "ai_confidence_threshold": 0.52,
        "guardrail_relaxation": 0.12,
        "min_bb_width_pct": 0.0,
    },
    "WIF/USDT": {
        # Scalper extremo. Small-cap meme: RSI extendido antes de corregir,
        # ATR enorme, gates más laxos para no perdernos los rallys.
        "scenario_a_rsi_max": 56.0,
        "scenario_b_rsi_max": 38.0,
        "min_atr_pct": 0.0020,
        "max_atr_pct": 0.0600,
        "min_volume_ratio": 0.08,
        "min_orderbook_imbalance": 0.40,
        "min_trade_flow_score": 0.40,
        "max_spread_pct": 0.0025,
        "ai_confidence_threshold": 0.50,
        "guardrail_relaxation": 0.15,
        "min_bb_width_pct": 0.0,
    },
}


def _normalise(symbol: str | None) -> str:
    return (symbol or "").strip().upper()


def get_market_profile(symbol: str | None) -> dict[str, float] | None:
    """Return the raw profile dict for ``symbol`` (or None if no overrides)."""
    if not symbol:
        return None
    return MARKET_PROFILES.get(_normalise(symbol))


def apply_market_profile(settings: Settings, symbol: str | None) -> Settings:
    """Return a Settings clone with per-market entry overrides applied.

    If the symbol has no profile, the original settings instance is returned
    unchanged (no clone), which keeps the hot path cheap for unknown markets.
    """
    profile = get_market_profile(symbol)
    if not profile:
        return settings

    overrides: dict[str, Any] = {
        field: profile[field]
        for field in _OVERRIDABLE_FIELDS
        if field in profile
    }
    if not overrides:
        return settings
    # dataclasses.replace works for slots=True dataclasses on Py3.11+.
    return replace(settings, **overrides)


def market_profiles_summary(target_symbols: tuple[str, ...] | list[str]) -> dict[str, dict[str, Any]]:
    """Build the per-market summary published to the dashboard.

    Includes both the configured overrides AND a list of fields that are
    actually being overridden, so the frontend can render a clean table.
    """
    out: dict[str, dict[str, Any]] = {}
    for sym in target_symbols:
        profile = get_market_profile(sym) or {}
        out[_normalise(sym)] = {
            "symbol": _normalise(sym),
            "has_profile": bool(profile),
            "overrides": dict(profile),
        }
    return out


__all__ = [
    "MARKET_PROFILES",
    "apply_market_profile",
    "get_market_profile",
    "market_profiles_summary",
]
