"""
src/main_deriv.py
─────────────────────────────────────────────────────────────────────────────
Independent async daemon for the Deriv synthetic-indices pipeline.

This file runs in its OWN process. It NEVER imports anything that pulls the
Binance pipeline into memory at module level (only the tiny `config` module is
shared, purely for `python-dotenv`). Therefore the Deriv WebSocket cannot
contaminate the Binance Spot loop's latency budget.

Lifecycle
─────────
  1. Load DerivSettings from .env.
  2. Open the WS, authorize, subscribe to ticks for every symbol.
  3. For every incoming tick: feed the risk engine, evaluate, and if the
     score breaches `min_score`, place a multiplier contract via the
     OrderRouter → DerivTradeExecutor.
  4. A background reaper polls open contracts every few seconds and settles
     them via the PAMM webhook on close.
  5. SIGINT / SIGTERM trigger a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.deriv_analyst import DerivAnalyst
from src.analysis.tick_velocity import TickVelocityAnalyzer
from src.data.deriv_client import DerivClient, DerivClientError, NormalisedTick
from src.execution.deriv_mirror_client import DerivMirrorClient
from src.execution.deriv_trader import DerivTradeExecutor
from src.execution.order_router import OrderRouter, OrderRouterError
from src.safety.deriv_risk import (
    DerivRiskManager, HurstCalibrator, MacroHDCalibrator,
    EXTREME_TENSION_LIMITS_SEC,
)
from src.safety.ghost_logger import GhostLogger
from src.execution.position_manager import SYMBOL_RATCHET_PARAMS as _DPM_PARAMS
from src.strategies.deriv_signals import (
    adaptive_max_hold,
    get_asset_profile,
    is_spike_market,
    min_score_for,
    # min_score_for_regime REMOVED (T6): returned stale hardcoded 7.50 for calm
    # and was never actually called — _regime_min = snap.effective_min_score instead.
    passes_atr_volatility_filter,
    spike_timeout_sec,
)
from src.utils.deriv_config import DerivSettings, load_deriv_settings
from src.utils.telegram_telemetry import TelegramTelemetry


_LOGGER = logging.getLogger("deriv.daemon")


def _env_flag(name: str, default: str = "false") -> bool:
    raw = os.getenv(name, default)
    val = str(raw).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
        val = val[1:-1].strip()
    return val.lower() in {"1", "true", "yes", "on"}


def _env_symbol_set(name: str, default: str = "") -> set[str]:
    raw = os.getenv(name, default)
    txt = str(raw or "").strip()
    if len(txt) >= 2 and txt[0] == txt[-1] and txt[0] in {'"', "'"}:
        txt = txt[1:-1].strip()
    return {item.strip().upper() for item in txt.split(",") if item.strip()}

# ─── BOOM/CRASH structural stop-loss / take-profit ───────────────────────────
# Spike hunter trades use a wider initial SL so the contract survives the
# Structural SL/TP for BOOM/CRASH spike-hunter trades (stake-relative, not price-%).
# With a $3 hard cap and 200× multiplier, a price-% SL always hits the Deriv minimum
# ($0.50) and gets wicked out by 1–2 ticks of inter-spike noise.
# Stake-relative values:
#   SL 100% of stake  → full-stake protection — wider oxygen zone for pre-spike
#                       accumulation; DPM ratchets the floor once profit accrues.
#   TP 250% of stake  → $7.50 on $3 — realistic spike target at 200×.
# Both tunable via env vars without a code change.
_BOOM_CRASH_SL_PCT: float = float(os.getenv("DERIV_BOOM_CRASH_SL_PCT", "1.00"))
_BOOM_CRASH_TP_PCT: float = float(os.getenv("DERIV_BOOM_CRASH_TP_PCT", "2.50"))

# ─── Anti-slippage spread veto (institutional execution gate) ────────────────
# Stricter than the risk-engine spread veto (which is 0.0010 = 0.10%).
# This is the FINAL pre-execution check applied right before the broker WS
# transmits the buy. Default 0.0008 = 0.08% (8 bps).
_EXEC_MAX_SPREAD_PCT: float = float(os.getenv("DERIV_EXEC_MAX_SPREAD_PCT", "0.0008"))

# ─── Dynamic ATR hold extension ──────────────────────────────────────────────
# When ATR is expanding relative to the rolling median, CRASH/BOOM spikes may
# take longer to materialise.  Extend max_hold by a proportional amount.
# Cap: min(MAX_EXTENSION, base * RATIO_MULT * (atr_ratio - THRESHOLD)).
_ATR_HOLD_EXTENSION_THRESHOLD: float = float(os.getenv("DERIV_ATR_HOLD_EXT_THRESHOLD", "1.30"))
_ATR_HOLD_EXTENSION_MULTIPLIER: float = float(os.getenv("DERIV_ATR_HOLD_EXT_MULT", "0.15"))
_ATR_HOLD_EXTENSION_MAX_SEC: float = float(os.getenv("DERIV_ATR_HOLD_EXT_MAX", "150"))

# ─── Edge-quality adaptive sizing ───────────────────────────────────────────
# Stake multiplier based on score margin, AI confidence and regime quality.
_EDGE_SIZE_MIN_MULT: float = float(os.getenv("DERIV_EDGE_SIZE_MIN_MULT", "0.55"))
_EDGE_SIZE_MAX_MULT: float = float(os.getenv("DERIV_EDGE_SIZE_MAX_MULT", "1.30"))
_EDGE_SIZE_LOW_CONF: float = float(os.getenv("DERIV_EDGE_SIZE_LOW_CONF", "0.74"))
_EDGE_SIZE_HIGH_CONF: float = float(os.getenv("DERIV_EDGE_SIZE_HIGH_CONF", "0.90"))

# ─── Streak-aware fixed-base sizing (operator request: base=2, x1.3/x0.8) ───
_STREAK_SIZE_ENABLED: bool = _env_flag("DERIV_STREAK_SIZE_ENABLED", "true")
_STREAK_SIZE_FORCE_BASE: bool = _env_flag("DERIV_STREAK_SIZE_FORCE_BASE", "true")
_STREAK_SIZE_BASE_STAKE_USDT: float = float(os.getenv("DERIV_STREAK_BASE_STAKE_USDT", "2.0"))
_STREAK_SIZE_GOOD_MULT: float = float(os.getenv("DERIV_STREAK_GOOD_MULT", "1.30"))
_STREAK_SIZE_BAD_MULT: float = float(os.getenv("DERIV_STREAK_BAD_MULT", "0.80"))
_STREAK_SIZE_GOOD_PNL_WINDOW: float = float(os.getenv("DERIV_STREAK_GOOD_PNL_WINDOW", "0.20"))
_STREAK_SIZE_BAD_PNL_WINDOW: float = float(os.getenv("DERIV_STREAK_BAD_PNL_WINDOW", "-0.20"))
_STREAK_SIZE_BAD_GUARD_BONUS: float = float(os.getenv("DERIV_STREAK_BAD_GUARD_BONUS", "0.50"))

# ─── AI-veto recovery probe ───────────────────────────────────────────────
_AI_VETO_RECOVERY_PROBE_ENABLED: bool = _env_flag("DERIV_AI_VETO_RECOVERY_PROBE_ENABLED", "true")
_AI_VETO_RECOVERY_MARGIN_MIN: float = float(os.getenv("DERIV_AI_VETO_RECOVERY_MARGIN_MIN", "1.00"))
_AI_VETO_RECOVERY_MIN_CONF: float = float(os.getenv("DERIV_AI_VETO_RECOVERY_MIN_CONF", "0.25"))
_AI_VETO_RECOVERY_MIN_ATR_SCORE: float = float(os.getenv("DERIV_AI_VETO_RECOVERY_MIN_ATR_SCORE", "0.90"))
_AI_VETO_RECOVERY_MAX_GUARD_BONUS: float = float(os.getenv("DERIV_AI_VETO_RECOVERY_MAX_GUARD_BONUS", "0.75"))
_AI_VETO_RECOVERY_MIN_PNL_WINDOW: float = float(os.getenv("DERIV_AI_VETO_RECOVERY_MIN_PNL_WINDOW", "-0.60"))

# ─── DRY_EXIT_RELAX — re-entry bypass after zero-peak max_hold_timeout ───────
# When the last closed trade for a symbol was a dry exit (max_hold_timeout +
# peak_profit=0.0), the scarcity pressure has NOT reset — the symbol is still
# "overdue". This mechanism bypasses the trade_cooldown gate and MATURITY_GATE
# for up to MAX_CONSECUTIVE consecutive re-entries, using a shorter wall-clock
# cooldown of COOLDOWN_SEC instead of the tick-based gate.
_DRY_EXIT_RELAX_ENABLED: bool = _env_flag("DERIV_DRY_EXIT_RELAX_ENABLED", "true")
_DRY_EXIT_RELAX_COOLDOWN_SEC: float = max(
    30.0, float(os.getenv("DERIV_DRY_EXIT_RELAX_COOLDOWN_SEC", "60") or 60)
)
_DRY_EXIT_RELAX_MAX_CONSECUTIVE: int = max(
    1, int(os.getenv("DERIV_DRY_EXIT_RELAX_MAX_CONSECUTIVE", "2") or 2)
)
_DRY_EXIT_RELAX_SKIP_MATURITY: bool = _env_flag("DERIV_DRY_EXIT_RELAX_SKIP_MATURITY", "true")

# ─── Hard symbol disable list ─────────────────────────────────────────────
# Runtime-safe kill switch. Defaults align with current operator request.
_FORCED_DISABLED_SYMBOLS: set[str] = _env_symbol_set(
    "DERIV_FORCE_DISABLED_SYMBOLS",
    "BOOM1000,CRASH1000",
)

# Symbols the bot monitors (full score/Hurst/pipeline) but NEVER trades.
# Useful for manual-trading consoles where the human executes the entry.
_MANUAL_ONLY_SYMBOLS: set[str] = _env_symbol_set(
    "DERIV_MANUAL_ONLY_SYMBOLS",
    "",
)

# Dynamic inactive bypass (entry gate only): keeps dynamic score/regime logic
# active while avoiding hard symbol starvation for selected symbols.
_DYNAMIC_INACTIVE_BYPASS_ENABLE: bool = _env_flag(
    "DERIV_DYNAMIC_INACTIVE_BYPASS_ENABLE",
    "true",
)
_DYNAMIC_INACTIVE_BYPASS_SYMBOLS: set[str] = _env_symbol_set(
    "DERIV_DYNAMIC_INACTIVE_BYPASS_SYMBOLS",
    "CRASH300,CRASH500,CRASH600,CRASH900,CRASH1000,BOOM500,BOOM600,BOOM900,BOOM1000",
)

# ─── Dynamic score-gate shaping (sniper mode) ──────────────────────────────
# Risk-engine and AI sidecar must speak the same score language:
# - trending / FAST: lower ceiling to improve conversion when structure confirms.
# - calm / SLOW: keep a stricter ceiling to avoid low-quality drifts.
_DYNAMIC_GATE_SCORE_CEILING: float = float(os.getenv("DERIV_DYNAMIC_GATE_SCORE_CEILING", "7.6"))
_DYNAMIC_GATE_TRENDING_CEILING: float = float(os.getenv("DERIV_DYNAMIC_GATE_TRENDING_CEILING", "6.5"))
_DYNAMIC_GATE_FAST_CEILING: float = float(os.getenv("DERIV_DYNAMIC_GATE_FAST_CEILING", "6.5"))
_DYNAMIC_GATE_CALM_CEILING: float = float(os.getenv("DERIV_DYNAMIC_GATE_CALM_CEILING", "7.0"))
_DYNAMIC_GATE_SLOW_CEILING: float = float(os.getenv("DERIV_DYNAMIC_GATE_SLOW_CEILING", "7.0"))
_DYNAMIC_GATE_FAST_FLOOR: float = float(os.getenv("DERIV_DYNAMIC_GATE_FAST_FLOOR", "6.5"))


def _compute_atr_hold_extension(
    symbol: str,
    atr_abs: float,
    atr_history: list,
    base_hold_sec: float,
) -> float:
    """Return extra seconds to add to max_hold when ATR is expanding.

    Only applies to BOOM/CRASH spike markets.  Returns 0.0 for non-spike
    symbols or when ATR history is too thin to be meaningful.
    """
    if not is_spike_market(symbol):
        return 0.0
    if atr_abs <= 0 or len(atr_history) < 20:
        return 0.0
    sorted_hist = sorted(atr_history)
    median_atr = sorted_hist[len(sorted_hist) // 2]
    if median_atr <= 0:
        return 0.0
    atr_ratio = atr_abs / median_atr
    if atr_ratio <= _ATR_HOLD_EXTENSION_THRESHOLD:
        return 0.0
    extra = base_hold_sec * _ATR_HOLD_EXTENSION_MULTIPLIER * (atr_ratio - _ATR_HOLD_EXTENSION_THRESHOLD)
    return round(min(_ATR_HOLD_EXTENSION_MAX_SEC, extra), 1)


def _calculate_rng_probability(
    kinetic_score: float,
    fvg_bos_validated: bool,
    zona_liquidez_macro: bool,
    scarcity_state: str,
    scarcity_ratio: float,
    cooldown_active: bool,
    implied_structure: bool = False,
) -> tuple[int, list[str]]:
    """Probabilistic RNG alignment score (0-100).

    Weights:
      35  kinetic_score (0.0–1.0) — continuous compression: tight drift + deceleration
      30  fvg_bos_validated       — structural algorithm reset confirmed
              OR implied_structure (FASE 4 triple lock: extreme pressure + kinetic giro)
      20  zona_liquidez_macro     — macro liquidity zone (Vision LLM)
      15  scarcity (inverted)     — overdue = higher spike probability:
            FRESCO(<0.5×)→0  CARGANDO(<0.9×)→3  LISTO(<1.4×)→8
            VENCIDO(<2.2×)→12  SECO(≥2.2×)→15  extreme(≥2.8×)→+2

    Cooldown veto: if burst retrace < 50%, multiply score by 0.3 (energy not reset).
    implied_structure: FASE 4 — FVG absent but triple lock (extreme pressure + kinetic
        compression) authorises the structural bonus without real BOS confirmation.
    """
    score = 0
    missing: list[str] = []

    _kin_pts = round(kinetic_score * 35)
    score += _kin_pts
    if _kin_pts == 0:
        missing.append("compresión cinética ausente")

    if fvg_bos_validated:
        score += 30
    elif implied_structure:
        score += 30  # FASE 4: triple lock — physical tension replaces structural confirmation
    else:
        missing.append("FVG sin BOS estructural")

    if zona_liquidez_macro:
        score += 20
    else:
        missing.append("zona macro no confirmada")

    # FASE 1: inverted scarcity — overdue = higher spike pressure
    if scarcity_state == "SECO":
        score += 15 + (2 if scarcity_ratio >= 2.8 else 0)
    elif scarcity_state == "VENCIDO":
        score += 12
    elif scarcity_state == "LISTO":
        score += 8
    elif scarcity_state == "CARGANDO":
        score += 3
    else:
        _scar_label = scarcity_state if scarcity_state and scarcity_state not in ("SIN_DATOS", "") else "SIN_DATOS"
        missing.append(f"scarcity={_scar_label} ({scarcity_ratio:.2f}×)")

    if cooldown_active:
        score = round(score * 0.3)
        missing.append("COOLDOWN activo — retroceso <50%")

    return min(score, 100), missing


# ─── Per-symbol cooldown to prevent burst entries ────────────────────────────
class _CooldownGate:
    """Per-symbol trade cooldown measured in ingested ticks.

    Tick-domain cooldowns are consistent with the rest of the signal stack
    (Hurst, ATR, momentum scores — all computed over tick windows).  At roughly
    1 tick/s for BOOM/CRASH indices the numeric values stay equivalent to the
    previous seconds-based values, but the semantics correctly handle variable
    tick rates during volatile market periods.
    """

    def __init__(self, ticks: int, risk: "DerivRiskManager") -> None:
        self._ticks = ticks
        self._risk = risk
        self._last: dict[str, int | None] = {}  # None = never fired for this symbol

    def can_fire(self, symbol: str) -> bool:
        last = self._last.get(symbol)
        if last is None:
            return True  # never traded this symbol — gate is always open
        current = self._risk.get_tick_count(symbol)
        return (current - last) >= self._ticks

    def mark(self, symbol: str) -> None:
        self._last[symbol] = self._risk.get_tick_count(symbol)


# ─── Daemon orchestrator ─────────────────────────────────────────────────────
class DerivDaemon:
    def __init__(self, settings: DerivSettings) -> None:
        self._settings = settings
        _base_client = DerivClient(settings)
        self._client = DerivMirrorClient.from_settings(_base_client, settings)
        self._telemetry = self._build_telemetry()
        self._risk = DerivRiskManager(settings)
        self._executor = DerivTradeExecutor(settings, self._client, self._telemetry, risk_manager=self._risk)
        # Dynamic per-symbol runtime configuration (PostgreSQL bridge).
        # Refreshed asynchronously every DERIV_DYNAMIC_CONFIG_REFRESH_SEC seconds
        # and read from in-memory dict in the hot path.
        self._dynamic_db_url = os.getenv("DATABASE_URL", "").strip()
        _dyn_enabled_raw = os.getenv("DERIV_DYNAMIC_CONFIG_ENABLED", "true").strip().lower()
        self._dynamic_enabled = (
            _dyn_enabled_raw in {"1", "true", "yes", "on"}
            and bool(self._dynamic_db_url)
        )
        self._dynamic_refresh_sec = max(
            5,
            int(os.getenv("DERIV_DYNAMIC_CONFIG_REFRESH_SEC", "15") or 15),
        )
        self._dynamic_score_min_guardrail = max(
            3.0,
            min(float(os.getenv("DYNAMIC_AI_SCORE_MIN_GUARDRAIL", "5.5") or 5.5), 9.2),
        )
        self._dynamic_score_max_guardrail = max(
            self._dynamic_score_min_guardrail,
            min(float(os.getenv("DYNAMIC_AI_SCORE_MAX_GUARDRAIL", "9.2") or 9.2), 12.0),
        )
        self._dynamic_gate_score_ceiling_base = max(
            self._dynamic_score_min_guardrail,
            min(_DYNAMIC_GATE_SCORE_CEILING, self._dynamic_score_max_guardrail),
        )
        self._dynamic_gate_trending_ceiling = max(
            self._dynamic_score_min_guardrail,
            min(_DYNAMIC_GATE_TRENDING_CEILING, self._dynamic_score_max_guardrail),
        )
        self._dynamic_gate_fast_ceiling = max(
            self._dynamic_score_min_guardrail,
            min(_DYNAMIC_GATE_FAST_CEILING, self._dynamic_score_max_guardrail),
        )
        self._dynamic_gate_calm_ceiling = max(
            self._dynamic_score_min_guardrail,
            min(_DYNAMIC_GATE_CALM_CEILING, self._dynamic_score_max_guardrail),
        )
        self._dynamic_gate_slow_ceiling = max(
            self._dynamic_score_min_guardrail,
            min(_DYNAMIC_GATE_SLOW_CEILING, self._dynamic_score_max_guardrail),
        )
        self._dynamic_gate_neutral_ceiling = self._dynamic_gate_score_ceiling_base
        self._dynamic_gate_fast_floor = max(
            self._dynamic_score_min_guardrail,
            min(_DYNAMIC_GATE_FAST_FLOOR, self._dynamic_gate_fast_ceiling),
        )
        _entry_tick_only_raw = os.getenv("DERIV_ENTRY_TICK_ONLY", "true").strip().lower()
        # Entry decisions must be tick-driven; spike history is telemetry/tuning only.
        self._entry_tick_only = _entry_tick_only_raw in {"1", "true", "yes", "on"}
        # Static per-symbol chase maps can conflict with dynamic windows.
        # In tick-only mode we default to dynamic-only unless explicitly enabled.
        _use_static_chase_map_raw = os.getenv("DERIV_POST_SPIKE_CHASE_USE_STATIC_MAP", "false").strip().lower()
        self._post_spike_chase_use_static_map = _use_static_chase_map_raw in {"1", "true", "yes", "on"}
        # Tick-only anti-chase guard (fallback default): never enter right
        # after a materialized spike. Per-symbol values may override this.
        self._post_spike_chase_min_ticks = max(
            0.0,
            min(
                float(
                    os.getenv(
                        "DERIV_POST_SPIKE_CHASE_MIN_TICKS",
                        os.getenv("DERIV_POST_SPIKE_CHASE_MIN_SEC", "180"),
                    )
                    or 180
                ),
                5000.0,
            ),
        )
        self._post_spike_chase_block_ticks_default = max(
            self._post_spike_chase_min_ticks,
            float(
                os.getenv(
                    "DERIV_POST_SPIKE_CHASE_BLOCK_TICKS",
                    os.getenv("DERIV_POST_SPIKE_CHASE_BLOCK_SEC", "300"),
                )
                or 300
            ),
        )
        self._post_spike_chase_block_sec_map = self._load_post_spike_chase_block_map()
        # 2026-05-30 POST-SL ENTRY GATE: after a broker_sl_hit on a spike market,
        # most explosions arrive within 60-180s (forensic 28 SLs/12h: 36% within 120s).
        # During AWAIT_SEC, only enter when a fresh SPIKE_EVENT lands for the
        # same symbol (we are the legitimate spike-rider). If the await window
        # expires without spike, lock chase entries for CHASE_LOCK_SEC more
        # (typical chase entries 5-15min later lose money).
        self._post_sl_await_sec = max(
            0,
            int(os.getenv("DERIV_POST_SL_SPIKE_AWAIT_SEC", "180") or 180),
        )
        self._post_sl_chase_lock_sec = max(
            0,
            int(os.getenv("DERIV_POST_SL_CHASE_LOCK_SEC", "240") or 240),
        )
        self._post_sl_gate_enabled = os.getenv(
            "DERIV_POST_SL_GATE_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._post_sl_emit_throttle_sec = max(
            10,
            int(os.getenv("DERIV_POST_SL_EMIT_THROTTLE_SEC", "30") or 30),
        )
        # 2026-05-30 SPIKE-DENSITY GATE — "ya van X spikes esta hora, espera"
        # Lee del rolling window de risk._spike_recent_ts y compara con baseline
        # de las últimas N horas. Tres reglas:
        #   1) BURST: count_recent(BURST_WINDOW_SEC) >= BURST_THRESHOLD AND
        #      last_spike_age <= BURST_BLOCK_AFTER_SEC → BLOCK 'spike_burst_cooldown'
        #   2) SATURATED: count_60m / max(baseline_avg, MIN_BASELINE) >= BLOCK_RATIO
        #      AND samples >= 2 → BLOCK 'spike_density_saturated'
        #   3) ELEVATED: ratio >= WARN_RATIO → log only (AI orchestrator lo lee)
        # Memoria persistida en spike_density_memory.json para que el sidecar AI
        # pueda aprender el patrón.
        self._spike_density_gate_enabled = os.getenv(
            "DERIV_SPIKE_DENSITY_GATE_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._spike_density_burst_window_sec = max(
            60,
            int(os.getenv("DERIV_SPIKE_BURST_WINDOW_SEC", "300") or 300),
        )
        self._spike_density_burst_threshold = max(
            2,
            int(os.getenv("DERIV_SPIKE_BURST_THRESHOLD", "3") or 3),
        )
        self._spike_density_burst_block_after_sec = max(
            30,
            int(os.getenv("DERIV_SPIKE_BURST_BLOCK_AFTER_SEC", "180") or 180),
        )
        self._spike_density_block_ratio = max(
            1.0,
            float(os.getenv("DERIV_SPIKE_DENSITY_BLOCK_RATIO", "2.0") or 2.0),
        )
        self._spike_density_warn_ratio = max(
            1.0,
            float(os.getenv("DERIV_SPIKE_DENSITY_WARN_RATIO", "1.5") or 1.5),
        )
        self._spike_density_min_baseline = max(
            1.0,
            float(os.getenv("DERIV_SPIKE_DENSITY_MIN_BASELINE", "4.0") or 4.0),
        )
        self._spike_density_lookback_hours = max(
            2,
            int(os.getenv("DERIV_SPIKE_DENSITY_LOOKBACK_HOURS", "6") or 6),
        )
        self._spike_density_emit_throttle_sec = max(
            10,
            int(os.getenv("DERIV_SPIKE_DENSITY_EMIT_THROTTLE_SEC", "30") or 30),
        )
        self._spike_density_memory_path: Path | None = None
        self._spike_density_memory: dict[str, dict[str, Any]] = {}
        self._spike_density_memory_last_persist_ts: float = 0.0
        # 2026-05-30 REACTIVATION-AWARENESS GATE — "ojo, mientras descansabas
        # explotaron N spikes; espera confirmación fresca antes de entrar".
        # Cuando un símbolo sale de profit_locked / dynamic_inactive /
        # trade_cooldown, durante GRACE_SEC pedimos que haya un spike fresco
        # (>= reactivation_ts) si en los últimos EXTENDED_WINDOW hubo
        # >= BURST_THRESHOLD spikes. Si no hay spike fresco → BLOCK.
        # Esta información también se persiste para el LLM.
        self._reactivation_gate_enabled = os.getenv(
            "DERIV_REACTIVATION_GATE_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._reactivation_grace_sec = max(
            60,
            int(os.getenv("DERIV_REACTIVATION_GRACE_SEC", "600") or 600),
        )
        self._reactivation_extended_window_sec = max(
            300,
            int(os.getenv("DERIV_REACTIVATION_EXTENDED_WINDOW_SEC", "1800") or 1800),
        )
        self._reactivation_burst_threshold = max(
            2,
            int(os.getenv("DERIV_REACTIVATION_BURST_THRESHOLD", "3") or 3),
        )
        self._reactivation_emit_throttle_sec = max(
            10,
            int(os.getenv("DERIV_REACTIVATION_EMIT_THROTTLE_SEC", "30") or 30),
        )
        # tracking dicts
        self._sym_last_inactive_ts: dict[str, float] = {}
        self._sym_last_inactive_reason: dict[str, str] = {}
        self._sym_reactivation_ts: dict[str, float] = {}
        self._sym_inactive_streak: dict[str, int] = {}
        self._dynamic_configs: dict[str, dict[str, Any]] = {}
        self._dynamic_last_refresh: str | None = None
        self._dynamic_last_error_ts: float = 0.0
        # Vision LLM context: keyed by symbol.upper() → dict with zona_liquidez_macro etc.
        # Loaded from /data/logs/deriv_vision.json (written by dynamic_ai_orchestrator).
        self._last_vision_context: dict[str, dict] = {}
        self._vision_file_loaded_at: float = 0.0
        self._vision_file_ttl_sec: float = 60.0  # re-read file at most every 60s
        # 2026-05-29 v5: per-symbol spike_wait_timeout published by orchestrator.
        # File path resolved later from _ctx_state_dir; cached with TTL to avoid
        # I/O on every dynamic_config lookup.
        self._spike_wait_timeout_map: dict[str, float] = {}
        self._spike_wait_timeout_loaded_at: float = 0.0
        self._spike_wait_timeout_ttl_sec: float = 60.0
        _status_decisions_env = os.getenv(
            "DERIV_STATUS_DECISIONS_MAX",
            os.getenv("DERIV_AI_LOG_MAX", "500"),
        )
        self._status_decisions_max = max(30, min(int(_status_decisions_env or 500), 2000))
        # Keep a slightly wider in-memory ring than what we serialize to status,
        # so the UI has enough context even when one symbol emits bursts.
        self._last_decisions_ring_max = max(self._status_decisions_max, 120)
        self._analyst = DerivAnalyst(settings, self._client)
        self._router = OrderRouter(binance_executor=None, deriv_executor=self._executor)
        self._cooldown = _CooldownGate(ticks=max(60, int(settings.contract_duration_sec)), risk=self._risk)
        self._stop_event = asyncio.Event()
        self._velocity = TickVelocityAnalyzer()  # Module 2: tick acceleration detector
        self._ghost_logger = GhostLogger(
            log_dir=os.getenv("BOT_STATE_DIR", "/data/logs"),
            window_sec=float(os.getenv("DERIV_GHOST_WINDOW_SEC", "600") or 600),
            sl_pct=float(os.getenv("DERIV_GHOST_SL_PCT", "0.025") or 0.025),
        )
        # Telemetría in-memory (anillos) para que el frontend audite por qué
        # entra (o no entra) el bot. Se serializa junto al status cada 10s.
        self._last_ticks: dict[str, dict[str, Any]] = {}    # symbol → {price, ts}
        self._last_decisions: list[dict[str, Any]] = []
        self._dynamic_inactive_last_emit_ts: dict[str, float] = {}
        self._dynamic_inactive_decision_interval_sec = max(
            10,
            int(os.getenv("DERIV_DYNAMIC_INACTIVE_DECISION_INTERVAL_SEC", "45") or 45),
        )
        self._counters: dict[str, int] = {
            "ticks_total": 0,
            "decisions_total": 0,
            "orders_sent": 0,
            "orders_ok": 0,
            "orders_failed": 0,
        }
        # Balance cache — refreshed by _balance_refresh_loop every 30 s.
        self._balance_usd: float | None = None
        self._balance_currency: str = "USD"
        # Rolling equity snapshots (last 200) for the analytics page.
        self._equity_history: list[dict[str, Any]] = []
        # Per-symbol tick counter for periodic diagnostic logs
        self._diag_tick_count: dict[str, int] = {}
        # ── Signal cooldown (global anti-spam debounce for ALL HARD_MATH_OVERRIDE types) ──
        # Tracks last fired override per symbol. Checked FIRST in _pipeline() so
        # neither math nor AI evaluation runs on cooling-down symbols.
        self._signal_cooldown: dict[str, int | None] = {}  # symbol → tick count at last signal eval
        # ── Per-symbol evaluation lock (BUG-A fix) ──────────────────────────
        # Prevents concurrent pipeline runs for the same symbol when ticks fire
        # faster than one evaluation cycle. At most ONE pipeline runs per symbol
        # at any time; subsequent ticks for the same symbol are silently dropped
        # until the in-flight pipeline completes.
        self._sym_eval_locks: dict[str, asyncio.Lock] = {}
        # ── Market-context snapshot writer ────────────────────────────────
        # Tracks how many ticks each symbol has emitted since the last context
        # snapshot.  A snapshot is written when any symbol crosses 60 ticks,
        # so roughly every ~60 seconds under normal feed conditions.
        self._ctx_tick_count: dict[str, int] = {}
        # 2026-05-29: per-symbol previous write (ts, tick_count) to derive tick_rate.
        self._ctx_prev_write: dict[str, tuple[float, int]] = {}
        # Last-known FVG state per symbol — updated after risk.evaluate(), read on next ctx write.
        self._last_fvg_state: dict[str, dict] = {}
        # Burst mode spike-price tracking — records tick price at the moment each new spike
        # is detected so we can compute retroceso (price recovery from spike) in real time.
        self._last_burst_spike_ts: dict[str, float] = {}
        self._last_burst_spike_px: dict[str, float] = {}
        # Burst start timestamp — used to enforce DERIV_BURST_MAX_AGE_SEC safety valve.
        # Prevents POST_SPIKE_COOLDOWN from locking the bot permanently when price
        # lateralises without recovering 50% ATR and new spikes keep the burst window alive.
        self._burst_start_ts: dict[str, float] = {}
        self._ctx_state_dir = Path(
            os.environ.get("BOT_STATE_DIR",
                os.environ.get("LOGS_DIR", str(settings.closed_contracts_file.parent)))
        )
        self._executor.set_dynamic_config_provider(self.get_dynamic_config)

    def _spike_enrich(
        self,
        symbol: str,
        *,
        bot_entered: bool,
        block_reason: str | None = None,
        score: float | None = None,
    ) -> None:
        """Enrich the most-recent spike JSON record for BOOM/CRASH symbols.

        Only runs if the last spike for this symbol happened within 60 s, to
        avoid overwriting old records with unrelated evaluate() outcomes.
        """
        _su = symbol.upper()
        if "BOOM" not in _su and "CRASH" not in _su:
            return
        _last_spike = self._risk.get_last_spike_ts(symbol)
        if _last_spike <= 0 or (time.time() - _last_spike) > 300.0:
            return
        _open_contracts = getattr(self._executor, "open_contracts", {})
        _had_open = bool(_open_contracts.get(symbol) or _open_contracts.get(_su))
        self._risk.enrich_last_spike(
            symbol,
            bot_entered=bot_entered,
            block_reason=block_reason,
            score=score,
            had_open_pos=_had_open,
        )

    def _record_decision(self, *, symbol: str, allowed: bool, side: str | None,
                         score: float, reason: str,
                         extra: dict | None = None) -> None:
        rec: dict[str, Any] = {
            "symbol": symbol,
            "allowed": bool(allowed),
            "side": side,
            "score": round(float(score or 0.0), 3),
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            rec.update({k: v for k, v in extra.items() if v is not None})
        self._last_decisions.append(rec)
        if len(self._last_decisions) > self._last_decisions_ring_max:
            self._last_decisions = self._last_decisions[-self._last_decisions_ring_max:]
        self._counters["decisions_total"] += 1

    @staticmethod
    def _clamp_dynamic_values(
        symbol: str,
        spike_pre_filter_target: int,
        zero_peak_grace_sec: int,
        score_min_override: float,
    ) -> tuple[int, int, float]:
        """Clamp dynamic config values to hard safety guardrails."""
        _sym = str(symbol or "").upper()
        _sensitive_zero_peak_floor = 60 if _sym in {"BOOM500", "CRASH500", "CRASH600"} else 0
        _score_min = float(os.getenv("DYNAMIC_AI_SCORE_MIN_GUARDRAIL", "5.5") or 5.5)
        _score_max = float(os.getenv("DYNAMIC_AI_SCORE_MAX_GUARDRAIL", "9.2") or 9.2)
        # Per-symbol floor: DYNAMIC_AI_{SYM}_SCORE_MIN caps how high the LLM can raise the gate.
        # e.g. DYNAMIC_AI_CRASH900_SCORE_FLOOR=6.80 → score_min_override capped at 6.80 for CRASH900
        _sym_floor_env = os.getenv(f"DYNAMIC_AI_{_sym}_SCORE_FLOOR") or os.getenv(f"DYNAMIC_AI_{_sym}_SCORE_MIN")
        if _sym_floor_env:
            try:
                _score_max = min(_score_max, float(_sym_floor_env))
            except ValueError:
                pass
        _spike_max = max(
            500,
            int(
                float(
                    os.getenv(
                        "DERIV_SPIKE_PREFILTER_MAX_TICKS",
                        os.getenv("DERIV_SPIKE_PREFILTER_CAP_TICKS", "2500"),
                    )
                    or 2500
                )
            ),
        )
        _spike_floor = max(
            50,
            min(
                int(
                    float(
                        os.getenv(
                            "DERIV_SPIKE_PREFILTER_MIN_TICKS",
                            os.getenv(
                                "DERIV_SPIKE_PREFILTER_FLOOR_TICKS",
                                os.getenv("DERIV_SPIKE_PREFILTER_FLOOR_SEC", "90"),
                            ),
                        )
                        or 90
                    )
                ),
                _spike_max,
            ),
        )
        if is_spike_market(_sym):
            _spike_floor = max(50, _spike_floor)
        _spf = max(_spike_floor, min(int(spike_pre_filter_target), _spike_max))
        _grace = max(_sensitive_zero_peak_floor, min(int(zero_peak_grace_sec), 120))
        _score = max(_score_min, min(float(score_min_override), _score_max))
        return _spf, _grace, _score

    @staticmethod
    def _clamp_dep_values(
        dep_exit_policy: str,
        dep_min_hold_sec: int,
        dep_atr_decay_ratio: float,
        dep_loss_floor_usdt: float,
    ) -> tuple[str, int, float, float]:
        """Clamp DEP fields and enforce safe rollout defaults."""
        _policy = str(dep_exit_policy or "PASSIVE").strip().upper()
        if _policy not in {"PASSIVE", "SHADOW", "ACTIVE_DECAY"}:
            _policy = "PASSIVE"

        _allow_active_decay = str(
            os.getenv("DERIV_DEP_ALLOW_ACTIVE_DECAY", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if _policy == "ACTIVE_DECAY" and not _allow_active_decay:
            # Safety-first rollout: explicit env opt-in is required for live closes.
            _policy = "SHADOW"

        _min_hold = max(30, min(int(dep_min_hold_sec), 900))
        _decay_ratio = max(0.30, min(float(dep_atr_decay_ratio), 0.95))
        _loss_floor = max(-5.0, min(float(dep_loss_floor_usdt), 0.0))
        return _policy, _min_hold, _decay_ratio, _loss_floor

    @staticmethod
    def _parse_post_spike_map(raw: str) -> dict[str, float]:
        """Parse DERIV_POST_SPIKE_CHASE_BLOCK_*_MAP='SYM:ticks,SYM:ticks'."""
        out: dict[str, float] = {}
        for chunk in str(raw or "").split(","):
            item = chunk.strip()
            if not item or ":" not in item:
                continue
            sym_raw, sec_raw = item.split(":", 1)
            sym = sym_raw.strip().upper()
            if not sym:
                continue
            try:
                sec = max(0.0, float(sec_raw.strip()))
            except ValueError:
                continue
            out[sym] = sec
        return out

    def _load_post_spike_chase_block_map(self) -> dict[str, float]:
        """Load per-symbol anti-chase overrides from env once at startup."""
        # Dynamic-first policy: when tick-only is enabled, static per-symbol
        # maps stay disabled unless explicitly opted in.
        if self._entry_tick_only and not self._post_spike_chase_use_static_map:
            return {}

        out = self._parse_post_spike_map(os.getenv("DERIV_POST_SPIKE_CHASE_BLOCK_TICKS_MAP", ""))

        for key, value in os.environ.items():
            prefix = "DERIV_POST_SPIKE_CHASE_BLOCK_TICKS_"
            if not key.startswith(prefix):
                continue
            symbol = key[len(prefix):].strip().upper()
            if not symbol:
                continue
            try:
                out[symbol] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue

        # Backward compatibility: in tick-only mode, accept legacy *_SEC
        # overrides as tick values when no explicit *_TICKS override exists.
        # This prevents silent starvation when production only defines *_SEC.
        if self._entry_tick_only:
            sec_map = self._parse_post_spike_map(os.getenv("DERIV_POST_SPIKE_CHASE_BLOCK_SEC_MAP", ""))
            for sym, val in sec_map.items():
                out.setdefault(sym, val)
            for key, value in os.environ.items():
                prefix = "DERIV_POST_SPIKE_CHASE_BLOCK_SEC_"
                if not key.startswith(prefix):
                    continue
                symbol = key[len(prefix):].strip().upper()
                if not symbol or symbol in out:
                    continue
                try:
                    out[symbol] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
            return out

        out.update(self._parse_post_spike_map(os.getenv("DERIV_POST_SPIKE_CHASE_BLOCK_SEC_MAP", "")))
        for key, value in os.environ.items():
            prefix = "DERIV_POST_SPIKE_CHASE_BLOCK_SEC_"
            if not key.startswith(prefix):
                continue
            symbol = key[len(prefix):].strip().upper()
            if not symbol:
                continue
            try:
                out[symbol] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        return out

    def _post_spike_chase_block_ticks_for_symbol(
        self,
        symbol: str,
        profile: dict[str, Any] | None,
        dyn_cfg: dict[str, Any],
        dyn_active: bool,
    ) -> float:
        """Resolve anti-chase window per symbol with safe fallbacks."""
        sym = str(symbol or "").upper()
        explicit_override = False

        # Highest priority: explicit per-symbol env overrides.
        by_symbol = self._post_spike_chase_block_sec_map.get(sym)
        if by_symbol is not None:
            resolved = by_symbol
            explicit_override = True
        else:
            resolved = 0.0

        # Dynamic config stores per-symbol spike timing in tick domain.
        if resolved <= 0 and dyn_active:
            dyn_ticks = float(dyn_cfg.get("spike_pre_filter_target") or 0.0)
            if dyn_ticks > 0:
                # Safety cap: anti-chase window must never consume most of the
                # symbol cycle, otherwise the bot starves (no viable entry time).
                cycle_ticks = float((profile or {}).get("spike_interval_ticks") or 0.0)
                if cycle_ticks > 0 and is_spike_market(sym):
                    chase_cap_ratio = max(
                        0.20,
                        min(float(os.getenv("DERIV_POST_SPIKE_CHASE_MAX_CYCLE_RATIO", "0.60") or 0.60), 0.95),
                    )
                    chase_cap_ticks = max(self._post_spike_chase_min_ticks, cycle_ticks * chase_cap_ratio)
                    resolved = min(dyn_ticks, chase_cap_ticks)
                else:
                    resolved = dyn_ticks

        # Profile fallback: symbol-specific post-spike delay.
        prf = profile or {}
        if resolved <= 0:
            cycle_ticks = float(prf.get("spike_interval_ticks") or 0.0)
            if cycle_ticks > 0:
                resolved = max(30.0, min(1500.0, cycle_ticks * 0.30))

        # Legacy fallback: some profiles still expose *_sec keys only.
        if resolved <= 0:
            profile_sec = float(prf.get("spike_min_post_sec") or 0.0)
            if profile_sec > 0:
                resolved = profile_sec

        if resolved <= 0:
            resolved = self._post_spike_chase_block_ticks_default

        # Hourly spike cadence hardens anti-chase when market is slow and
        # relaxes slightly when cadence is clearly active.
        _spikes_1h = int(getattr(self._risk, "get_spike_count_last_hour", lambda _s: 0)(sym))
        if is_spike_market(sym) and not explicit_override:
            _low_spike_floor = max(
                self._post_spike_chase_min_ticks,
                float(os.getenv("DERIV_LOW_SPIKE_CHASE_BLOCK_TICKS", "240") or 240),
            )
            _high_spike_threshold = max(
                2,
                int(float(os.getenv("DERIV_HIGH_SPIKE_CHASE_THRESHOLD_1H", "5") or 5)),
            )
            _high_spike_relax_mult = max(
                0.70,
                min(float(os.getenv("DERIV_HIGH_SPIKE_CHASE_RELAX_MULT", "0.90") or 0.90), 1.0),
            )
            if _spikes_1h <= 1:
                resolved = max(resolved, _low_spike_floor)
            elif _spikes_1h >= _high_spike_threshold:
                resolved = max(self._post_spike_chase_min_ticks, resolved * _high_spike_relax_mult)

        # Sniper safety floor for spike markets.
        # Important: explicit per-symbol overrides MUST bypass this floor,
        # otherwise tuned maps (e.g. BOOM500:15) are silently ignored.
        if is_spike_market(sym):
            if explicit_override:
                return max(0.0, resolved)
            return max(self._post_spike_chase_min_ticks, resolved)

        return resolved

    # 2026-06-01 SNIPER MAX-WINDOW: empirical p75-p90 of ticks_between_spikes
    # per symbol (24h forensic 2026-06-01). Beyond this, statistical
    # probability of a near-term spike collapses → block entry to preserve $2 SL.
    _SPIKE_MAX_WINDOW_DEFAULTS: dict[str, float] = {
        "BOOM500":  700.0,   # p75=677
        "CRASH500": 700.0,   # p75=649
        "CRASH600": 950.0,   # p75=913
        "CRASH900": 1200.0,  # p75=1104
        "CRASH300": 420.0,   # conservative (no current data, ~75% of cycle 600)
        "BOOM600":  950.0,
        "BOOM900":  1200.0,
        "BOOM1000": 1300.0,
        "CRASH1000": 1300.0,
    }

    def _post_spike_max_ticks_for_symbol(self, symbol: str) -> float:
        """Resolve per-symbol max-window (sniper) — env override + defaults.

        Env: DERIV_POST_SPIKE_MAX_TICKS_MAP=BOOM500:700,CRASH900:1200
             DERIV_POST_SPIKE_MAX_TICKS_<SYMBOL>=N (single override)
             DERIV_POST_SPIKE_MAX_TICKS_DISABLE=true → returns 0 (off)
        Returns 0 to disable the gate.
        """
        sym = str(symbol or "").upper()
        if str(os.getenv("DERIV_POST_SPIKE_MAX_TICKS_DISABLE", "false") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return 0.0
        # Env single override has highest priority.
        env_single = os.getenv(f"DERIV_POST_SPIKE_MAX_TICKS_{sym}")
        if env_single:
            try:
                return max(0.0, float(env_single))
            except (TypeError, ValueError):
                pass
        # Env map second priority.
        env_map = self._parse_post_spike_map(os.getenv("DERIV_POST_SPIKE_MAX_TICKS_MAP", ""))
        if sym in env_map:
            return max(0.0, float(env_map[sym]))
        # Hard-coded defaults.
        return float(self._SPIKE_MAX_WINDOW_DEFAULTS.get(sym, 0.0))

    def _passes_post_spike_strength_gate(
        self,
        *,
        symbol: str,
        snap: Any,
        profile: dict[str, Any] | None,
        dyn_cfg: dict[str, Any],
        dyn_active: bool,
        score_floor: float,
        elastic_z2: bool = False,
    ) -> tuple[bool, str]:
        """Allow post-spike entries only when continuation strength is explicit.

        Intent:
          - Prefer NO entry right after a spike unless the continuation is strong.
          - Reduce late/chasing entries that often end in spike_timeout.
        """
        if not self._entry_tick_only or not is_spike_market(symbol):
            return True, ""

        # Elasticity Z2 (extreme tension: ratio≥2.0 OR elapsed≥100% symbol limit)
        # means the symbol has been compressed beyond normal Poisson expectations.
        # At this point the PSSV protection is less meaningful — the spike is
        # statistically overdue and we want to be positioned, not guarded.
        if elastic_z2:
            return True, ""

        _last_spike_tick = self._risk.get_last_spike_tick_count(symbol)
        if _last_spike_tick <= 0:
            return True, ""

        _elapsed = max(0.0, float(self._risk.get_tick_count(symbol) - _last_spike_tick))
        _chase_block = self._post_spike_chase_block_ticks_for_symbol(
            symbol,
            profile,
            dyn_cfg,
            dyn_active,
        )

        # ── 2026-06-02 HOT RE-ENTRY OVERRIDE (operator directive) ────────────
        # "esa reentrada con una buena confirmación score 8+ debería entrar: el
        # símbolo está calientico tirando spike y ya tenemos protecciones al SL".
        # When a HIGH-score (>=8) fresh post-spike confirmation appears while the
        # symbol is HOT (last spike very recent AND it just fired), we bypass the
        # anti-chase guard and the max-window expiry. This is a genuinely NEW
        # post-spike signal, not butterfly-chasing: the symbol is actively firing.
        # Safe because exits now protect capital (tier 30% <$1, post_spike_red_cut,
        # post_spike_no_followup, scarcity_dry_exit) — a bad re-entry is cut small,
        # not bled to the -$2 SL.
        _hot_reentry_enable = str(
            os.getenv("DERIV_HOT_REENTRY_ENABLE", "true") or "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if _hot_reentry_enable:
            _hot_min_score = float(
                os.getenv("DERIV_HOT_REENTRY_MIN_SCORE", "8.0") or 8.0
            )
            # Symbol is "hot" only while the last spike is still fresh in ticks
            # (it just fired) — beyond that the move already played out.
            _hot_fresh_ticks = float(
                os.getenv("DERIV_HOT_REENTRY_FRESH_TICKS", "300") or 300
            )
            _hot_win_sec = float(
                os.getenv("DERIV_HOT_REENTRY_WINDOW_SEC", "300") or 300
            )
            try:
                _hot_recent_spikes = int(
                    self._risk.get_spike_count_recent(symbol, _hot_win_sec)
                )
            except Exception:
                _hot_recent_spikes = 0
            _hot_min_spikes = max(
                1, int(float(os.getenv("DERIV_HOT_REENTRY_MIN_SPIKES", "1") or 1))
            )
            # Block HOT_REENTRY when spike cluster is too dense: 3+ spikes means the
            # RNG bucket is recently depleted — consecutive spikes inflate score/RNG
            # artificially but the next spike is NOT statistically imminent.
            _hot_max_spikes = int(float(os.getenv("DERIV_HOT_REENTRY_MAX_RECENT_SPIKES", "0") or 0))
            _hot_cluster_block = _hot_max_spikes > 0 and _hot_recent_spikes > _hot_max_spikes
            if (
                float(snap.score or 0.0) >= _hot_min_score
                and _elapsed <= _hot_fresh_ticks
                and _hot_recent_spikes >= _hot_min_spikes
                and not _hot_cluster_block
            ):
                _sb_hot = (
                    snap.score_breakdown
                    if isinstance(snap.score_breakdown, dict)
                    else {}
                )
                _sb_hot["hot_reentry_fired"] = round(float(snap.score or 0.0), 2)
                _sb_hot["hot_reentry_elapsed_ticks"] = round(_elapsed, 1)
                _sb_hot["hot_reentry_recent_spikes"] = int(_hot_recent_spikes)
                _LOGGER.info(
                    "[PIPELINE] HOT_REENTRY %s | score=%.2f≥%.1f since_last=%.0ft≤%.0ft "
                    "spikes=%d/%ds → bypass chase_guard (símbolo calientico, "
                    "protecciones SL activas)",
                    symbol, float(snap.score or 0.0), _hot_min_score,
                    _elapsed, _hot_fresh_ticks, _hot_recent_spikes, int(_hot_win_sec),
                )
                return True, ""
            elif _hot_cluster_block:
                _LOGGER.info(
                    "[POST_SPIKE_CHASE_GUARD] %s HOT_REENTRY blocked: spike_cluster "
                    "spikes=%d/%ds > max=%d (bucket depleted, score inflation risk)",
                    symbol, _hot_recent_spikes, int(_hot_win_sec), _hot_max_spikes,
                )

        # ── 2026-06-01 SPIKE-CLUSTER OVERRIDE ("se alinearon las estrellas") ──
        # Forensic over 1102 spikes: when >=3 spikes hit in the last 300s, the
        # NEXT spike's median gap collapses to 57-76s (vs 294-466s normal) =
        # 4-8x faster. In that state the chase_guard (180-250t) would block the
        # ONE moment we most want to position for the 4th spike. So when a
        # cluster is active we collapse the chase_block to a small floor — this
        # is the user's "cazar el 4to spike" and is NOT butterfly-chasing
        # (the spike is statistically imminent, not random).
        try:
            _cl_win = float(os.getenv("DERIV_SPIKE_CLUSTER_WINDOW_SEC", "300") or 300)
            _cl_min = int(float(os.getenv("DERIV_SPIKE_CLUSTER_MIN_SPIKES", "3") or 3))
            _cl_n = int(self._risk.get_spike_count_recent(symbol, _cl_win))
        except Exception:
            _cl_n, _cl_min = 0, 3
        # 2026-06-01 ANTI-BUTTERFLY recency gate (user directive): the cluster
        # "4th spike imminent" edge is ONLY valid while the LAST spike is still
        # recent. _elapsed is ticks-since-last-spike. If the burst already went
        # quiet for > FRESH_TICKS the spikes "ya salieron" — relaxing the chase
        # guard now would just let the bot enter 3-4 min later chasing
        # butterflies. Beyond the fresh window we KEEP the full chase guard so
        # the bot waits for a genuinely NEW setup (fresh imminence on a new gap).
        _cl_fresh = float(os.getenv("DERIV_SPIKE_CLUSTER_FRESH_TICKS", "150") or 150)
        if _cl_n >= _cl_min and _elapsed <= _cl_fresh:
            _cl_floor = float(os.getenv("DERIV_SPIKE_CLUSTER_CHASE_FLOOR_TICKS", "25") or 25)
            if _cl_floor < _chase_block:
                _LOGGER.info(
                    "[PIPELINE] SPIKE_CLUSTER_CHASE_RELAX %s | n=%d/%ds since_last=%.0ft<=%.0ft "
                    "chase_block %.0ft→%.0ft (4th spike imminent, fresh)",
                    symbol, _cl_n, int(_cl_win), _elapsed, _cl_fresh, _chase_block, _cl_floor,
                )
                _chase_block = _cl_floor
        elif _cl_n >= _cl_min and _elapsed > _cl_fresh:
            _LOGGER.info(
                "[PIPELINE] SPIKE_CLUSTER_STALE %s | n=%d/%ds since_last=%.0ft>%.0ft "
                "→ keep chase_guard (spikes already fired, no butterfly chasing)",
                symbol, _cl_n, int(_cl_win), _elapsed, _cl_fresh,
            )

        # Keep safety invariant even if caller path changes in the future.
        if _elapsed < _chase_block:
            return (
                False,
                f"post_spike_chase_guard:{_elapsed:.0f}t<{_chase_block:.0f}t",
            )

        # 2026-06-01 SNIPER MAX-WINDOW (user directive: "spike-timing math"):
        # Beyond p75-p90 of the symbol's empirical ticks-between-spikes
        # distribution, the statistical probability of a near-term spike
        # decays sharply. Entering in that "dry zone" wastes a $2 SL on a
        # setup that will likely just bleed via degrade. Block it.
        # Map source: env DERIV_POST_SPIKE_MAX_TICKS_MAP=SYM:N,SYM:M
        # (per-symbol). Hard-coded fallbacks derived from 24h forensic
        # 2026-06-01: BOOM500=700, CRASH500=700, CRASH600=950, CRASH900=1200,
        # CRASH300=420. Disabled if env var missing AND symbol not in defaults.
        _max_window_ticks = self._post_spike_max_ticks_for_symbol(symbol)
        if _max_window_ticks > 0 and _elapsed > _max_window_ticks:
            return (
                False,
                f"post_spike_window_expired:{_elapsed:.0f}t>{_max_window_ticks:.0f}t",
            )

        _window_default = max(180.0, _chase_block * 3.0)
        _strength_window = max(
            _chase_block,
            float(
                os.getenv(
                    "DERIV_POST_SPIKE_STRENGTH_WINDOW_TICKS",
                    os.getenv("DERIV_POST_SPIKE_STRENGTH_WINDOW_SEC", str(_window_default)),
                )
                or _window_default
            ),
        )
        if _elapsed > _strength_window:
            return True, ""

        _sb = snap.score_breakdown if isinstance(snap.score_breakdown, dict) else {}
        _score_margin = float(snap.score or 0.0) - float(score_floor or 0.0)
        _momentum = float(_sb.get("momentum") or 0.0)
        _atr_score = float(_sb.get("atr") or 0.0)
        _velocity_score = float(_sb.get("velocity_score") or 0.0)
        _hd_bonus = float(_sb.get("hd_bonus") or 0.0)
        _velocity_dir = str(_sb.get("velocity_dir") or "").upper()
        _spikes_1h = int(getattr(self._risk, "get_spike_count_last_hour", lambda _s: 0)(symbol))
        _cycle_ticks = int((profile or {}).get("spike_interval_ticks", 0) or 0)
        _expected_spikes_1h = int(round(3600.0 / float(_cycle_ticks))) if _cycle_ticks > 0 else 0
        _spike_density_1h = (
            float(_spikes_1h) / float(max(1, _expected_spikes_1h))
            if _expected_spikes_1h > 0
            else 0.0
        )

        _min_margin = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_SCORE_MARGIN", "0.60") or 0.60)
        _min_momentum = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_MOMENTUM_MIN", "1.10") or 1.10)
        _min_velocity = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_VELOCITY_MIN", "0.45") or 0.45)
        _min_atr = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_ATR_MIN", "1.00") or 1.00)
        _min_hd = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_HD_MIN", "0.50") or 0.50)
        _min_spikes_1h_signal = max(
            1,
            int(float(os.getenv("DERIV_POST_SPIKE_STRENGTH_SPIKES_1H_MIN", "3") or 3)),
        )
        _required_signals = max(
            1,
            min(
                5,
                int(os.getenv("DERIV_POST_SPIKE_STRENGTH_MIN_SIGNALS", "3") or 3),
            ),
        )

        _signals: list[str] = []
        if _score_margin >= _min_margin:
            _signals.append("score")
        if _momentum >= _min_momentum:
            _signals.append("momentum")
        if _velocity_score >= _min_velocity:
            _signals.append("velocity")
        if _atr_score >= _min_atr:
            _signals.append("atr")
        if _hd_bonus >= _min_hd:
            _signals.append("hd")
        if _spikes_1h >= _min_spikes_1h_signal:
            _signals.append("spikes_1h")

        _required_signals_effective = _required_signals
        if _spikes_1h <= 1:
            _required_signals_effective = min(5, _required_signals + 1)

        _dir_conflict = bool(
            _velocity_dir
            and snap.side
            and _velocity_dir != str(snap.side).upper()
        )
        _strong_enough = (len(_signals) >= _required_signals_effective) and not _dir_conflict

        _sb["post_spike_strength_elapsed_ticks"] = round(_elapsed, 1)
        _sb["post_spike_strength_window_ticks"] = round(_strength_window, 1)
        # Backward-compatible keys for existing dashboards/parsers.
        _sb["post_spike_strength_elapsed_sec"] = round(_elapsed, 1)
        _sb["post_spike_strength_window_sec"] = round(_strength_window, 1)
        _sb["post_spike_strength_spikes_1h"] = int(_spikes_1h)
        _sb["post_spike_strength_expected_spikes_1h"] = int(_expected_spikes_1h)
        _sb["post_spike_strength_spike_density_1h"] = round(_spike_density_1h, 3)
        _sb["post_spike_strength_signals"] = list(_signals)
        _sb["post_spike_strength_required"] = _required_signals_effective
        _sb["post_spike_strength_ok"] = _strong_enough

        if _strong_enough:
            return True, ""

        # ── Starvation override (2026-05-25) ──────────────────────────────
        # If this symbol has not entered a trade in N hours, allow ONE probe
        # entry so the bot can learn whether the gate is too strict. Capital
        # safety relies on stake caps and daily-DD lockout downstream.
        _starv_hours = float(os.getenv("DERIV_STARVATION_OVERRIDE_HOURS", "6") or 0.0)
        if _starv_hours > 0 and not _dir_conflict:
            _last_fire = self._cooldown._last.get(symbol)
            if _last_fire is None:
                _starv_ok = True
                _starv_ticks = float("inf")
            else:
                _now_tick = self._risk.get_tick_count(symbol)
                _starv_ticks = max(0.0, float(_now_tick - _last_fire))
                _starv_threshold = _starv_hours * 3600.0  # ~1 tick/sec
                _starv_ok = _starv_ticks >= _starv_threshold
            if _starv_ok:
                _LOGGER.warning(
                    "[POST_SPIKE_STARVATION_OVERRIDE] %s allowed probe entry | "
                    "ticks_since_last_fire=%s signals=%d/%d margin=%.2f mom=%.2f "
                    "vel=%.2f atr=%.2f hd=%.2f score=%.2f",
                    symbol,
                    f"{_starv_ticks:.0f}" if _starv_ticks != float("inf") else "never",
                    len(_signals), _required_signals_effective,
                    _score_margin, _momentum, _velocity_score, _atr_score, _hd_bonus,
                    float(snap.score or 0.0),
                )
                _sb["post_spike_strength_starvation_override"] = True
                return True, ""

        # ── Rejection telemetry (consumed by AI orchestrator) ─────────────
        # Structured single-line JSON-like log so the orchestrator can grep and
        # learn which gate thresholds are too tight for current market regime.
        _LOGGER.warning(
            "[POST_SPIKE_REJECT_TELEMETRY] symbol=%s elapsed=%.0ft window=%.0ft "
            "signals=%d/%d score=%.2f margin=%.2f momentum=%.2f velocity=%.2f "
            "atr=%.2f hd=%.2f spikes_1h=%d density=%.2f dir_conflict=%s side=%s vel_dir=%s regime=%s",
            symbol, _elapsed, _strength_window,
            len(_signals), _required_signals_effective,
            float(snap.score or 0.0), _score_margin, _momentum, _velocity_score,
            _atr_score, _hd_bonus, _spikes_1h, _spike_density_1h,
            _dir_conflict, snap.side, _velocity_dir,
            snap.regime,
        )

        _reason = (
            "post_spike_strength_veto:"
            f"{_elapsed:.0f}t<{_strength_window:.0f}t "
            f"signals={len(_signals)}/{_required_signals_effective} "
            f"margin={_score_margin:.2f} mom={_momentum:.2f} "
            f"vel={_velocity_score:.2f} atr={_atr_score:.2f}"
        )
        if _dir_conflict:
            _reason += f" vel_dir={_velocity_dir}!={snap.side}"
        return False, _reason

    @staticmethod
    def _multispike_policy_for_regime(regime: str) -> dict[str, Any]:
        """Resolve multispike ratchet policy from dynamic market regime.

        FAST  -> cluster mode (more patience, looser retention).
        SLOW  -> calm mode (short patience, tighter retention).
        NORMAL-> balanced defaults.
        """
        _regime = str(regime or "NORMAL").upper()
        _buf_default = int(float(os.getenv("DERIV_MULTISPIKE_BUFFER_TICKS_DEFAULT", "45") or 45))
        _buf_fast = int(float(os.getenv("DERIV_MULTISPIKE_BUFFER_TICKS_FAST", "60") or 60))
        _buf_slow = int(float(os.getenv("DERIV_MULTISPIKE_BUFFER_TICKS_SLOW", "10") or 10))

        _ret_default = float(os.getenv("DERIV_MULTISPIKE_RETENTION_PCT_DEFAULT", "0.70") or 0.70)
        _ret_fast = float(os.getenv("DERIV_MULTISPIKE_RETENTION_PCT_FAST", "0.50") or 0.50)
        _ret_slow = float(os.getenv("DERIV_MULTISPIKE_RETENTION_PCT_SLOW", "0.85") or 0.85)

        _floor_default = float(os.getenv("DERIV_MULTISPIKE_MIN_FLOOR_USDT", "0.15") or 0.15)

        _drift_default = int(float(os.getenv("DERIV_MULTISPIKE_DRIFT_TICKS_DEFAULT", str(_buf_default)) or _buf_default))
        _drift_fast = int(float(os.getenv("DERIV_MULTISPIKE_DRIFT_TICKS_FAST", str(_buf_fast)) or _buf_fast))
        _drift_slow = int(float(os.getenv("DERIV_MULTISPIKE_DRIFT_TICKS_SLOW", str(_buf_slow)) or _buf_slow))

        if _regime == "FAST":
            _buf = _buf_fast
            _ret = _ret_fast
            _drift = _drift_fast
        elif _regime == "SLOW":
            _buf = _buf_slow
            _ret = _ret_slow
            _drift = _drift_slow
        else:
            _buf = _buf_default
            _ret = _ret_default
            _drift = _drift_default

        return {
            "multispike_buffer_ticks": max(5, min(_buf, 300)),
            "multispike_retention_pct": max(0.30, min(_ret, 0.95)),
            "multispike_min_floor_usdt": max(0.01, min(_floor_default, 5.00)),
            "multispike_timeout_drift_ticks": max(5, min(_drift, 400)),
        }

    def _load_spike_wait_timeout_map(self) -> dict[str, float]:
        """Load /data/deriv-logs/deriv_spike_wait_timeout.json (orchestrator-published, 6h refresh).

        Returns the per-symbol timeout map (seconds). Cached with TTL to avoid I/O storms.
        Format: {"updated_at": "ISO", "window_hours": 6, "per_symbol": {"BOOM500": 380.0, ...}}.
        """
        now_ts = datetime.now(timezone.utc).timestamp()
        if (now_ts - self._spike_wait_timeout_loaded_at) < self._spike_wait_timeout_ttl_sec:
            return self._spike_wait_timeout_map
        self._spike_wait_timeout_loaded_at = now_ts
        try:
            fp = self._ctx_state_dir / "deriv_spike_wait_timeout.json"
            if not fp.exists():
                return self._spike_wait_timeout_map
            with fp.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            per_sym = payload.get("per_symbol") or {}
            new_map: dict[str, float] = {}
            for k, v in per_sym.items():
                try:
                    v_f = float(v)
                except Exception:  # noqa: BLE001
                    continue
                if 60.0 <= v_f <= 1800.0:
                    new_map[str(k).upper()] = v_f
            self._spike_wait_timeout_map = new_map
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("[dynamic-config] could not read deriv_spike_wait_timeout.json: %s", exc)
        return self._spike_wait_timeout_map

    def get_dynamic_config(self, symbol: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return in-memory dynamic config for a symbol (safe fallback if DB unavailable)."""
        _sym = symbol.upper()
        _profile = profile or get_asset_profile(_sym)
        _default_policy = self._multispike_policy_for_regime("NORMAL")
        _dep_policy, _dep_min_hold, _dep_decay_ratio, _dep_loss_floor = self._clamp_dep_values(
            os.getenv("DERIV_DEP_DEFAULT_POLICY", "PASSIVE"),
            int(float(os.getenv("DERIV_DEP_DEFAULT_MIN_HOLD_SEC", "120") or 120)),
            float(os.getenv("DERIV_DEP_DEFAULT_ATR_DECAY_RATIO", "0.70") or 0.70),
            float(os.getenv("DERIV_DEP_DEFAULT_LOSS_FLOOR_USDT", "-0.05") or -0.05),
        )
        _default = {
            "symbol": _sym,
            "market_regime": "NORMAL",
            "spike_pre_filter_target": int(_profile.get("spike_min_post_sec", 0) or 0),
            "zero_peak_grace_sec": 0,
            "score_min_override": float(min_score_for(_sym)),
            "dep_exit_policy": _dep_policy,
            "dep_min_hold_sec": _dep_min_hold,
            "dep_atr_decay_ratio": _dep_decay_ratio,
            "dep_loss_floor_usdt": _dep_loss_floor,
            "is_active": False,
            "size_multiplier": 1.0,
            **_default_policy,
            "source": "default",
            "last_updated": None,
        }
        _db_cfg = self._dynamic_configs.get(_sym)
        _swt_map = self._load_spike_wait_timeout_map()
        _swt_val = _swt_map.get(_sym)
        if _swt_val is not None:
            _default["spike_wait_timeout_sec"] = _swt_val
        if not _db_cfg:
            return _default
        _spf, _grace, _score = self._clamp_dynamic_values(
            _sym,
            int(_db_cfg.get("spike_pre_filter_target") or _default["spike_pre_filter_target"]),
            int(_db_cfg.get("zero_peak_grace_sec") or 0),
            float(_db_cfg.get("score_min_override") or _default["score_min_override"]),
        )
        _dep_policy, _dep_min_hold, _dep_decay_ratio, _dep_loss_floor = self._clamp_dep_values(
            str(_db_cfg.get("dep_exit_policy") or _default["dep_exit_policy"]),
            int(_db_cfg.get("dep_min_hold_sec") or _default["dep_min_hold_sec"]),
            float(_db_cfg.get("dep_atr_decay_ratio") or _default["dep_atr_decay_ratio"]),
            float(_db_cfg.get("dep_loss_floor_usdt") or _default["dep_loss_floor_usdt"]),
        )
        _regime = str(_db_cfg.get("market_regime") or "NORMAL").upper()
        _policy = self._multispike_policy_for_regime(_regime)

        return {
            "symbol": _sym,
            "market_regime": _regime,
            "spike_pre_filter_target": _spf,
            "zero_peak_grace_sec": _grace,
            "score_min_override": _score,
            "dep_exit_policy": _dep_policy,
            "dep_min_hold_sec": _dep_min_hold,
            "dep_atr_decay_ratio": _dep_decay_ratio,
            "dep_loss_floor_usdt": _dep_loss_floor,
            "is_active": bool(_db_cfg.get("is_active", True)),
            "size_multiplier": max(0.10, min(float(_db_cfg.get("size_multiplier") or 1.0), 2.00)),
            **_policy,
            "source": "dynamic_db",
            "last_updated": _db_cfg.get("last_updated"),
            "spike_wait_timeout_sec": _swt_val,
        }

    async def _refresh_dynamic_config_once(self) -> None:
        """Fetch dynamic per-symbol overrides from PostgreSQL into memory."""
        if not self._dynamic_enabled:
            return
        try:
            import asyncpg  # noqa: PLC0415
        except ImportError:
            _LOGGER.warning("[dynamic-config] asyncpg missing — dynamic layer disabled")
            self._dynamic_enabled = False
            return

        conn = await asyncio.wait_for(asyncpg.connect(self._dynamic_db_url), timeout=8.0)
        try:
            _query_with_dep = """
                SELECT symbol, market_regime, spike_pre_filter_target,
                       zero_peak_grace_sec, score_min_override,
                       dep_exit_policy, dep_min_hold_sec,
                       dep_atr_decay_ratio, dep_loss_floor_usdt,
                       is_active, last_updated,
                       COALESCE(size_multiplier, 1.0) AS size_multiplier
                FROM dynamic_symbol_config
            """
            _query_legacy = """
                SELECT symbol, market_regime, spike_pre_filter_target,
                       zero_peak_grace_sec, score_min_override,
                       is_active, last_updated,
                       COALESCE(size_multiplier, 1.0) AS size_multiplier
                FROM dynamic_symbol_config
            """
            _dep_columns_available = True
            _has_size_mult = True
            try:
                rows = await conn.fetch(_query_with_dep)
            except Exception as exc:  # noqa: BLE001
                _msg = str(exc).lower()
                _dep_hint = (
                    "dep_exit_policy" in _msg
                    or "dep_min_hold_sec" in _msg
                    or "dep_atr_decay_ratio" in _msg
                    or "dep_loss_floor_usdt" in _msg
                    or "undefined column" in _msg
                )
                if not _dep_hint:
                    raise
                _dep_columns_available = False
                try:
                    rows = await conn.fetch(_query_legacy)
                except Exception as exc2:  # noqa: BLE001
                    if "size_multiplier" in str(exc2).lower():
                        _has_size_mult = False
                        rows = await conn.fetch(
                            """
                            SELECT symbol, market_regime, spike_pre_filter_target,
                                   zero_peak_grace_sec, score_min_override,
                                   is_active, last_updated
                            FROM dynamic_symbol_config
                            """
                        )
                    else:
                        raise
                _LOGGER.warning(
                    "[dynamic-config] DEP columns unavailable (migration pending) — using PASSIVE defaults"
                )
        finally:
            await conn.close()

        _new: dict[str, dict[str, Any]] = {}
        for row in rows:
            _sym = str(row["symbol"] or "").upper().strip()
            if not _sym:
                continue
            _spf, _grace, _score = self._clamp_dynamic_values(
                _sym,
                int(row["spike_pre_filter_target"] or 280),
                int(row["zero_peak_grace_sec"] or 0),
                float(row["score_min_override"] or 6.0),
            )
            _dep_policy, _dep_min_hold, _dep_decay_ratio, _dep_loss_floor = self._clamp_dep_values(
                str(row["dep_exit_policy"] if _dep_columns_available else "PASSIVE"),
                int(row["dep_min_hold_sec"] if _dep_columns_available else 120),
                float(row["dep_atr_decay_ratio"] if _dep_columns_available else 0.70),
                float(row["dep_loss_floor_usdt"] if _dep_columns_available else -0.05),
            )
            try:
                _sm_raw = row["size_multiplier"] if _has_size_mult else 1.0
                _size_mult = float(_sm_raw) if _sm_raw is not None else 1.0
            except Exception:  # noqa: BLE001
                _size_mult = 1.0
            _size_mult = max(0.10, min(_size_mult, 2.00))
            _new[_sym] = {
                "market_regime": str(row["market_regime"] or "NORMAL").upper(),
                "spike_pre_filter_target": _spf,
                "zero_peak_grace_sec": _grace,
                "score_min_override": _score,
                "dep_exit_policy": _dep_policy,
                "dep_min_hold_sec": _dep_min_hold,
                "dep_atr_decay_ratio": _dep_decay_ratio,
                "dep_loss_floor_usdt": _dep_loss_floor,
                "is_active": bool(row["is_active"]),
                "size_multiplier": _size_mult,
                "last_updated": (
                    row["last_updated"].isoformat()
                    if row["last_updated"] is not None
                    else None
                ),
            }

        self._dynamic_configs = _new
        self._dynamic_last_refresh = datetime.now(timezone.utc).isoformat()
        _LOGGER.info(
            "[dynamic-config] refreshed %d symbols from PostgreSQL",
            len(self._dynamic_configs),
        )

    async def _try_load_vision_context(self) -> None:
        """Load deriv_vision.json written by the orchestrator (best-effort, TTL-cached).

        Runs the file I/O in a thread pool via asyncio.to_thread so the event loop
        is never blocked — even on TTL expiry the WS tick handler stays responsive.
        """
        now = time.time()
        if now - self._vision_file_loaded_at < self._vision_file_ttl_sec:
            return
        vision_path = os.getenv("DERIV_VISION_FILE", "/data/logs/deriv_vision.json")
        try:
            import json as _json  # noqa: PLC0415
            from pathlib import Path as _Path  # noqa: PLC0415
            _vf = _Path(vision_path)
            # asyncio.to_thread offloads the blocking read to a ThreadPoolExecutor,
            # keeping the event loop free to process ticks from other symbols.
            raw_text = await asyncio.to_thread(_vf.read_text)
            raw = _json.loads(raw_text)
            if isinstance(raw, dict):
                self._last_vision_context = {k.upper(): v for k, v in raw.items()}
                self._vision_file_loaded_at = now
        except FileNotFoundError:
            pass  # orchestrator hasn't written the file yet — use empty context
        except Exception as _ve:
            _LOGGER.debug("[vision-ctx] load failed: %s", _ve)

    async def _dynamic_config_refresh_loop(self) -> None:
        """Periodic refresh loop for dynamic symbol config overrides."""
        if not self._dynamic_enabled:
            _LOGGER.info(
                "[dynamic-config] disabled (DERIV_DYNAMIC_CONFIG_ENABLED=false or DATABASE_URL missing)"
            )
            await self._stop_event.wait()
            return

        _LOGGER.info(
            "[dynamic-config] starting refresh loop (interval=%ss)",
            self._dynamic_refresh_sec,
        )
        while not self._stop_event.is_set():
            try:
                await self._refresh_dynamic_config_once()
            except Exception as exc:  # noqa: BLE001
                _now = time.time()
                if (_now - self._dynamic_last_error_ts) > 30:
                    _LOGGER.warning(
                        "[dynamic-config] refresh failed: %s — keeping last in-memory snapshot",
                        exc,
                    )
                    self._dynamic_last_error_ts = _now
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._dynamic_refresh_sec)
            except asyncio.TimeoutError:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Market-context snapshot writer
    # ─────────────────────────────────────────────────────────────────────────
    def _maybe_write_market_context(self, symbol: str) -> None:
        """Write one market-context snapshot for *symbol* every ~60 ticks.

        Appends to deriv_market_context.json (capped at 10 000 records).
        Fields: ts, symbol, hurst, regime, atr, atr_percentile,
                ema200_distance_pct, ticks_since_last_spike,
                spike_cluster_active, open_positions.
        """
        self._ctx_tick_count[symbol] = self._ctx_tick_count.get(symbol, 0) + 1
        if self._ctx_tick_count[symbol] % 60 != 0:
            return
        try:
            from src.safety.deriv_risk import _ema200
            import json as _ctxjson
            _summary  = self._analyst.get_history_summary().get(symbol) or {}
            _hurst    = float(_summary.get("hurst") or 0.0) or None
            _regime   = str(_summary.get("vol_regime") or _summary.get("regime") or "?")
            _atr_hist = self._risk._atr_history.get(symbol, [])
            _cur_atr  = self._risk.get_current_atr(symbol)
            _atr_pct: int | None = None
            if _atr_hist and _cur_atr is not None:
                _window = _atr_hist[-100:]
                _atr_pct = round(sum(1 for v in _window if v <= _cur_atr) / len(_window) * 100)
            _ticks_buf  = self._risk._ticks.get(symbol, [])
            _ema200_val = _ema200(list(_ticks_buf)) if len(_ticks_buf) >= 200 else None
            _price      = self._last_ticks.get(symbol, {}).get("price")
            _ema200_dev: float | None = None
            if _ema200_val and _price and _ema200_val > 0:
                _ema200_dev = round((_price - _ema200_val) / _ema200_val, 5)
            _last_spike_ts = self._risk.get_last_spike_ts(symbol)
            _now = time.time()
            _ticks_since_spike: int | None = None
            if _last_spike_ts > 0:
                _ticks_since_spike = (
                    self._risk.get_tick_count(symbol)
                    - self._risk._last_spike_tick.get(symbol, 0)
                )
            _spike_cluster: bool | None = None
            try:
                _sf = self._ctx_state_dir / "deriv_spike_events.json"
                if _sf.exists() and _last_spike_ts > 0:
                    _evts = _ctxjson.loads(_sf.read_text())
                    _sym_evts = [e for e in _evts if e.get("symbol") == symbol]
                    if len(_sym_evts) >= 2:
                        _t1 = float(_sym_evts[-1].get("ts", 0))
                        _t2 = float(_sym_evts[-2].get("ts", 0))
                        _spike_cluster = bool(_t1 - _t2 < 400)
                    elif _sym_evts:
                        _spike_cluster = False
            except Exception:
                pass
            _open_pos = sum(
                1 for oc in getattr(self._executor, "_open", {}).values()
                if getattr(oc, "symbol", "") == symbol
            )
            # (prices only, no timestamps) plus a previous-write marker so we
            # can derive tick rate between snapshots.
            _tick_rate_5s: float | None = None
            _range_rolling_pct_60s: float | None = None
            _post_spike_decay_slope: float | None = None
            try:
                _tick_list = list(_ticks_buf) if _ticks_buf else []
                # tick_rate_5s is derived from the delta between the previous
                # snapshot's tick count and the current one, divided by elapsed
                # seconds and normalized to a 5s window for orchestrator math.
                _cur_count = self._risk.get_tick_count(symbol)
                _prev = self._ctx_prev_write.get(symbol)
                if _prev is not None:
                    _prev_ts, _prev_count = _prev
                    _elapsed = max(0.001, _now - float(_prev_ts))
                    _delta = max(0, int(_cur_count) - int(_prev_count))
                    if _elapsed > 0:
                        _tick_rate_5s = round((float(_delta) / _elapsed), 3)
                self._ctx_prev_write[symbol] = (float(_now), int(_cur_count))

                # range_rolling_pct over last ~60 ticks (proxy for 60s if
                # broker emits ~1 tick/s, which is typical for synthetic
                # indices). Always computable from prices alone.
                _last_60 = _tick_list[-60:] if len(_tick_list) >= 5 else []
                if len(_last_60) >= 5:
                    _hi = max(_last_60); _lo = min(_last_60); _mid = _last_60[-1]
                    if _mid > 0:
                        _range_rolling_pct_60s = round((_hi - _lo) / _mid * 100.0, 5)

                # post_spike_decay_slope: compare avg |Δprice| in first vs
                # second half of the tick window since the last spike.
                # Positive => re-expanding; negative => decaying.
                if _ticks_since_spike and _ticks_since_spike >= 6:
                    _n_post = min(int(_ticks_since_spike), len(_tick_list))
                    _post = _tick_list[-_n_post:]
                    if len(_post) >= 6:
                        _half = len(_post) // 2
                        _early = _post[:_half]
                        _late = _post[_half:]
                        def _avg_abs_delta(seq: list[float]) -> float:
                            if len(seq) < 2:
                                return 0.0
                            return sum(abs(seq[i] - seq[i - 1]) for i in range(1, len(seq))) / float(len(seq) - 1)
                        _e = _avg_abs_delta(_early)
                        _l = _avg_abs_delta(_late)
                        _ref = _post[-1] or 1.0
                        if _ref > 0:
                            _post_spike_decay_slope = round((_l - _e) / _ref, 7)
            except Exception:
                # Telemetry is best-effort; never let it break the tick loop.
                pass

            _fvg_cache = self._last_fvg_state.get(symbol.upper(), {})
            _snap = {
                "ts":                     round(_now, 3),
                "iso":                    datetime.fromtimestamp(_now, tz=timezone.utc).isoformat(),
                "symbol":                 symbol,
                "hurst":                  round(_hurst, 4) if _hurst else None,
                "regime":                 _regime,
                "atr":                    _cur_atr,
                "atr_percentile":         _atr_pct,
                "ema200_distance_pct":    _ema200_dev,
                "ticks_since_last_spike": _ticks_since_spike,
                "spike_cluster_active":   _spike_cluster,
                "open_positions":         _open_pos,
                "tick_rate_5s":           _tick_rate_5s,
                "range_rolling_pct_60s":  _range_rolling_pct_60s,
                "post_spike_decay_slope": _post_spike_decay_slope,
                "fvg_active":             _fvg_cache.get("fvg_active"),
                "fvg_direction":          _fvg_cache.get("fvg_direction") or None,
                "fvg_mid":                _fvg_cache.get("fvg_mid"),
                "smc_bonus":              _fvg_cache.get("smc_bonus"),
                # Entry quality indicators (operator console)
                "setup_type":            _fvg_cache.get("setup_type"),
                "execution_grade":       _fvg_cache.get("execution_grade"),
                "scarcity_state":        _fvg_cache.get("scarcity_state"),
                "scarcity_ratio":        _fvg_cache.get("scarcity_ratio"),
                "spike_imminence_state": _fvg_cache.get("spike_imminence_state"),
                "spike_imminence_score": _fvg_cache.get("spike_imminence_score"),
                "geo_channel_pos":       _fvg_cache.get("geo_channel_pos"),
                "fvg_tier":              _fvg_cache.get("fvg_tier"),
                "score_raw":             _fvg_cache.get("score_raw"),
                # Post-spike blind window: indicators computed in the 120s after a
                # spike are biased by the spike's price action (FVG, SMC, geo all
                # reflect the spike anomaly, not the next entry opportunity).
                # last_spike_wall_ts lets the frontend compute exact seconds live.
                "last_spike_wall_ts":    round(_last_spike_ts, 3) if _last_spike_ts > 0 else None,
                "post_spike_blind":      bool(_last_spike_ts > 0 and _now - _last_spike_ts < 120),
                # Burst mode fields — surfaced from spike density gate, no gate logic change
                "burst_depth":           _fvg_cache.get("burst_depth"),
                "burst_retroceso":       _fvg_cache.get("burst_retroceso"),
                "burst_active":          _fvg_cache.get("burst_active"),
                # FVG Anchor — True when evaluate() reinstated the spike's frozen FVG
                # instead of the dynamic rolling-window result (anti-retroceso-amnesia).
                "fvg_anchor_active":        _fvg_cache.get("fvg_anchor_active"),
                "fvg_anchor_age_s":         _fvg_cache.get("fvg_anchor_age_s"),
                # Dual-path structural fields (5m candle geometry)
                "structural_fvg_active":    _fvg_cache.get("structural_fvg_active"),
                "structural_fvg_direction": _fvg_cache.get("structural_fvg_direction"),
                "structural_fvg_confirm":   _fvg_cache.get("structural_fvg_confirm"),
                "structural_fvg_conflict":  _fvg_cache.get("structural_fvg_conflict"),
                "structural_fvg_absent":    _fvg_cache.get("structural_fvg_absent"),
                "atr_anchored":             _fvg_cache.get("atr_anchored"),
                "atr_pre_spike":            _fvg_cache.get("atr_pre_spike"),
                "geo_post_spike_nullified": _fvg_cache.get("geo_post_spike_nullified"),
                # Kinetic + RNG inputs
                "ema200_anchored":          _fvg_cache.get("ema200_anchored"),
                "kinetic_velocity":         _fvg_cache.get("kinetic_velocity"),
                "kinetic_acceleration":     _fvg_cache.get("kinetic_acceleration"),
                "kinetic_compressed":       _fvg_cache.get("kinetic_compressed"),
                "ghost_mad":                _fvg_cache.get("ghost_mad"),
                "fvg_bos_validated":        _fvg_cache.get("fvg_bos_validated"),
                "fvg_anchor_wick_survived": _fvg_cache.get("fvg_anchor_wick_survived"),
                "fvg_anchor_tol_penalty":   _fvg_cache.get("fvg_anchor_tol_penalty"),
                "fvg_anchor_mom_penalty":   _fvg_cache.get("fvg_anchor_mom_penalty"),
                # RNG probability (last known — persisted after each full pipeline run)
                "rng_probability":          _fvg_cache.get("rng_probability"),
                "rng_threshold":            _fvg_cache.get("rng_threshold", 65),
                "rng_missing":              _fvg_cache.get("rng_missing") or [],
                # Trifecta confirmations
                "trifecta_met":             _fvg_cache.get("trifecta_met"),
                "trifecta_vision":          _fvg_cache.get("trifecta_vision"),
                "trifecta_kinetic":         _fvg_cache.get("trifecta_kinetic"),
                "trifecta_scarcity":        _fvg_cache.get("trifecta_scarcity"),
                "trifecta_fvg_bos":         _fvg_cache.get("trifecta_fvg_bos"),
                # Master Key bypass indicator
                "master_key_bypass":        _fvg_cache.get("master_key_bypass"),
                "master_key_rng":           _fvg_cache.get("master_key_rng"),
            }
            _ctx_file = self._ctx_state_dir / "deriv_market_context.json"
            try:
                _existing: list = _ctxjson.loads(_ctx_file.read_text()) if _ctx_file.exists() else []
            except Exception:
                _existing = []
            _existing.append(_snap)
            if len(_existing) > 10_000:
                _existing = _existing[-10_000:]
            _ctx_file.write_text(_ctxjson.dumps(_existing))
        except Exception as _ctx_exc:
            _LOGGER.debug("[MARKET_CTX] write failed for %s: %s", symbol, _ctx_exc)

    def _log_entry_block(
        self,
        symbol: str,
        block_reason: str,
        *,
        score: float = 0.0,
        effective_min_score: float = 0.0,
        side: str | None = None,
        regime: str = "?",
        hurst: float = 0.0,
        ai_veto: bool = False,
        ai_confidence: float = 0.0,
        ai_reason: str = "",
        cooldown: bool = False,
        cooldown_elapsed: float = 0.0,
        cooldown_required: float = 0.0,
        score_breakdown: dict | None = None,
    ) -> None:
        """Emit a single unified INFO-level ENTRY_BLOCKED log line with full context.

        Provides complete pipeline observability in one line — no need to
        aggregate fragmented debug messages to understand why a trade was blocked.
        """
        bd = score_breakdown or {}
        _LOGGER.info(
            "[PIPELINE] ENTRY_BLOCKED %s | reason=%s | "
            "score=%.2f effective_min=%.2f | side=%s | regime=%s | H=%.3f | "
            "ai_veto=%s ai_conf=%.2f ai_reason=%s | "
            "cooldown=%s elapsed=%.0fs/%.0fs | "
            "score_breakdown={trend=%.2f atr=%.2f spread=%.2f stab=%.2f "
            "streak=%.2f cd=%.2f hd=%.2f smc=%s geo=%s geo_pos=%s}",
            symbol, block_reason,
            score, effective_min_score,
            side or "?", regime, hurst,
            ai_veto, ai_confidence, ai_reason or "—",
            cooldown, cooldown_elapsed, cooldown_required,
            bd.get("trend", 0), bd.get("atr", 0),
            bd.get("spread", 0), bd.get("stability", 0),
            bd.get("streak_penalty", 0), bd.get("cooldown", 0),
            bd.get("hd_bonus", 0),   # HD = Higher Direction (macro alignment)
            f"+{bd['smc_bonus']:.2f}" if bd.get("smc_bonus") else "—",
            # geo_gate: +1.0 optimal / 0.0 border / -1.5 slight / -2.0 hard penalty
            f"{bd['geo_gate']:+.1f}" if "geo_gate" in bd else "n/a",
            f"{bd['geo_channel_pos']:.3f}" if bd.get("geo_channel_pos") is not None else "—",
        )

    def _apply_dynamic_size_multiplier(
        self,
        *,
        stake: float,
        dynamic_cfg: dict[str, Any],
        dynamic_active: bool,
        profile_stake_cap: float,
        sb: dict[str, Any],
    ) -> float:
        """2026-05-29 (Fase D): apply orchestrator-driven size_multiplier.

        The LLM in scripts/dynamic_ai_orchestrator.py dials this per symbol per
        cycle in [0.10, 2.00]. 1.0 = neutral. <1.0 = soft quarantine (reduce
        size without disabling). >1.0 = high conviction (cap by profile).
        Skipped when dynamic_active=False (treated as 1.0).
        """
        if not dynamic_active:
            return float(stake)
        try:
            raw = dynamic_cfg.get("size_multiplier", 1.0)
            mult = float(raw) if raw is not None else 1.0
        except Exception:  # noqa: BLE001
            mult = 1.0
        mult = max(0.10, min(mult, 2.00))
        if abs(mult - 1.0) < 1e-6:
            sb["dyn_size_mult"] = 1.0
            return float(stake)
        adjusted = round(max(1.0, float(stake) * mult), 2)
        if profile_stake_cap > 0:
            adjusted = min(adjusted, round(profile_stake_cap, 2))
        sb["dyn_size_mult"] = round(mult, 3)
        sb["dyn_size_pre_stake"] = round(float(stake), 2)
        sb["dyn_size_adjusted_stake"] = round(float(adjusted), 2)
        return float(adjusted)

    def _enforce_profile_stake_target(
        self,
        *,
        symbol: str,
        stake: float,
        profile: dict[str, Any],
        sb: dict[str, Any],
    ) -> float:
        """Apply explicit profile-level final stake target when configured.

        This is intentionally scoped to spike profiles so volatility symbols
        keep their existing adaptive sizing behaviour.
        """
        try:
            target = float(profile.get("stake_target_usdt") or 0.0)
        except Exception:  # noqa: BLE001
            target = 0.0
        if target <= 0:
            return float(stake)

        _ptype = str(profile.get("type", "")).lower()
        if _ptype not in {"spike_crash", "spike_boom"}:
            return float(stake)

        fixed = round(max(1.0, target), 2)

        try:
            profile_cap = float(profile.get("stake_max_usdt") or 0.0)
        except Exception:  # noqa: BLE001
            profile_cap = 0.0
        if profile_cap > 0:
            fixed = min(fixed, round(profile_cap, 2))

        if abs(fixed - float(stake)) >= 0.01:
            sb["profile_stake_target_usdt"] = round(float(target), 2)
            sb["profile_stake_pre_target"] = round(float(stake), 2)
            sb["profile_stake_final"] = round(float(fixed), 2)
            _LOGGER.info(
                "[STAKE_POLICY] %s profile_target=%.2f pre=%.2f final=%.2f",
                symbol,
                float(target),
                float(stake),
                float(fixed),
            )
        return float(fixed)

    def _apply_edge_quality_sizing(
        self,
        symbol: str,
        snap: Any,
        *,
        analysis: Any | None,
        dynamic_cfg: dict[str, Any],
        dynamic_active: bool,
        profile_min_score: float,
        profile_stake_cap: float,
    ) -> float:
        """Apply conservative adaptive sizing by edge quality.

        The objective is asymmetric:
        - scale down aggressively when edge is uncertain,
        - scale up only when score/AI/regime confirm robust edge.
        """
        _engine_stake = float(getattr(snap, "suggested_stake_usdt", 0.0) or 0.0)
        base_stake = (
            float(_STREAK_SIZE_BASE_STAKE_USDT)
            if _STREAK_SIZE_FORCE_BASE
            else _engine_stake
        )
        if base_stake <= 0:
            base_stake = _engine_stake
        if base_stake <= 0:
            return base_stake

        sb = getattr(snap, "score_breakdown", {}) or {}
        if _STREAK_SIZE_ENABLED:
            _sym_pnl_window = 0.0
            _sym_guard_bonus = 0.0
            try:
                _sym_pnl_window = float(self._risk.symbol_pnl_window(symbol))
                _sym_guard_bonus = float(self._risk.symbol_score_floor_bonus(symbol))
            except Exception:  # noqa: BLE001
                pass

            _streak_mult = 1.0
            _streak_state = "neutral"
            if (
                _sym_guard_bonus >= _STREAK_SIZE_BAD_GUARD_BONUS
                or _sym_pnl_window <= _STREAK_SIZE_BAD_PNL_WINDOW
            ):
                _streak_mult = _STREAK_SIZE_BAD_MULT
                _streak_state = "bad"
            elif (
                _sym_guard_bonus <= 0.0
                and _sym_pnl_window >= _STREAK_SIZE_GOOD_PNL_WINDOW
            ):
                _streak_mult = _STREAK_SIZE_GOOD_MULT
                _streak_state = "good"

            adjusted = round(max(1.0, base_stake * _streak_mult), 2)
            if profile_stake_cap > 0:
                adjusted = min(adjusted, round(profile_stake_cap, 2))

            sb["streak_size_enabled"] = True
            sb["streak_size_base_stake"] = round(base_stake, 2)
            sb["streak_size_mult"] = round(float(_streak_mult), 4)
            sb["streak_size_state"] = _streak_state
            sb["streak_size_symbol_pnl_window"] = round(float(_sym_pnl_window), 4)
            sb["streak_size_symbol_guard_bonus"] = round(float(_sym_guard_bonus), 4)
            sb["streak_size_adjusted_stake"] = round(float(adjusted), 2)
            # 2026-05-29 (Fase D): apply orchestrator dynamic size_multiplier as
            # final, capped factor. Skipped when dynamic_active=False.
            adjusted = self._apply_dynamic_size_multiplier(
                stake=adjusted,
                dynamic_cfg=dynamic_cfg,
                dynamic_active=dynamic_active,
                profile_stake_cap=profile_stake_cap,
                sb=sb,
            )
            return adjusted

        mult = 1.0
        reasons: list[str] = []

        score_margin = float(getattr(snap, "score", 0.0) or 0.0) - float(profile_min_score)
        if score_margin < 0.25:
            mult *= 0.75
            reasons.append("score_margin_low")
        elif score_margin < 0.75:
            mult *= 0.90
            reasons.append("score_margin_mid")
        elif score_margin >= 1.80:
            mult *= 1.14
            reasons.append("score_margin_high")
        elif score_margin >= 1.20:
            mult *= 1.08
            reasons.append("score_margin_ok")

        ai_conf = None
        if analysis is not None and not bool(getattr(analysis, "ai_skipped", False)):
            try:
                ai_conf = float(getattr(analysis, "ai_confidence", 0.0) or 0.0)
            except Exception:
                ai_conf = None
        if isinstance(ai_conf, float):
            if ai_conf < _EDGE_SIZE_LOW_CONF:
                mult *= 0.90
                reasons.append("ai_conf_low")
            elif ai_conf >= _EDGE_SIZE_HIGH_CONF:
                mult *= 1.06
                reasons.append("ai_conf_high")

        regime = str(
            (dynamic_cfg.get("market_regime") if dynamic_active else getattr(snap, "regime", "NORMAL"))
            or "NORMAL"
        ).upper()
        if regime == "SLOW":
            mult *= 0.80
            reasons.append("regime_slow")
        elif regime == "FAST":
            mult *= 0.92
            reasons.append("regime_fast")

        if bool(sb.get("mean_rev_mode")):
            mult *= 0.88
            reasons.append("mean_rev_risk")
        if float(sb.get("geo_gate") or 0.0) < 0.0:
            mult *= 0.85
            reasons.append("geo_penalty")
        if float(sb.get("smc_bonus") or 0.0) >= 1.5 and score_margin > 0.8:
            mult *= 1.06
            reasons.append("smc_confirmed")
        if float(sb.get("hd_bonus") or 0.0) >= 1.0 and score_margin > 1.0:
            mult *= 1.04
            reasons.append("hd_confirmed")

        mult = max(_EDGE_SIZE_MIN_MULT, min(mult, _EDGE_SIZE_MAX_MULT))
        adjusted = round(max(1.0, base_stake * mult), 2)
        if profile_stake_cap > 0:
            adjusted = min(adjusted, round(profile_stake_cap, 2))

        if abs(adjusted - base_stake) >= 0.01:
            sb["edge_size_base_stake"] = round(base_stake, 2)
            sb["edge_size_mult"] = round(mult, 4)
            sb["edge_size_reasons"] = reasons
            sb["edge_size_score_margin"] = round(score_margin, 4)
            sb["edge_size_regime"] = regime
            if isinstance(ai_conf, float):
                sb["edge_size_ai_conf"] = round(ai_conf, 4)

        # 2026-05-29 (Fase D): apply orchestrator dynamic size_multiplier last.
        adjusted = self._apply_dynamic_size_multiplier(
            stake=adjusted,
            dynamic_cfg=dynamic_cfg,
            dynamic_active=dynamic_active,
            profile_stake_cap=profile_stake_cap,
            sb=sb,
        )
        return adjusted

    def _allow_ai_veto_recovery_probe(
        self,
        symbol: str,
        snap: Any,
        analysis: Any,
    ) -> tuple[bool, str]:
        """Allow a controlled probe when AI vetoes but math quality is strong."""
        if not _AI_VETO_RECOVERY_PROBE_ENABLED:
            return False, "probe_disabled"
        if analysis is None or bool(getattr(analysis, "ai_skipped", False)):
            return False, "no_ai_veto_context"
        if not is_spike_market(symbol):
            return False, "non_spike_symbol"

        try:
            ai_conf = float(getattr(analysis, "ai_confidence", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            ai_conf = 0.0

        sb = getattr(snap, "score_breakdown", {}) or {}
        score_margin = float(getattr(snap, "score", 0.0) or 0.0) - float(
            getattr(snap, "effective_min_score", 0.0) or 0.0
        )
        atr_score = float(sb.get("atr") or 0.0)
        guard_bonus = float(self._risk.symbol_score_floor_bonus(symbol))
        pnl_window = float(self._risk.symbol_pnl_window(symbol))

        if ai_conf < _AI_VETO_RECOVERY_MIN_CONF:
            return False, f"ai_conf_low:{ai_conf:.2f}"
        if score_margin < _AI_VETO_RECOVERY_MARGIN_MIN:
            return False, f"score_margin_low:{score_margin:.2f}"
        if atr_score < _AI_VETO_RECOVERY_MIN_ATR_SCORE:
            return False, f"atr_score_low:{atr_score:.2f}"
        if guard_bonus > _AI_VETO_RECOVERY_MAX_GUARD_BONUS:
            return False, f"guard_bonus_high:{guard_bonus:.2f}"
        if pnl_window < _AI_VETO_RECOVERY_MIN_PNL_WINDOW:
            return False, f"pnl_window_low:{pnl_window:.2f}"

        return (
            True,
            "recovery_probe"
            f":margin={score_margin:.2f}"
            f"|conf={ai_conf:.2f}"
            f"|atr={atr_score:.2f}"
            f"|guard={guard_bonus:.2f}"
            f"|pnlw={pnl_window:.2f}",
        )

    # ─────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        _LOGGER.info(
            "[deriv-daemon] starting | symbols=%s dry_run=%s bankroll=%.2f",
            self._settings.symbols, self._settings.dry_run, self._settings.bankroll_usdt,
        )
        _LOGGER.info(
            "[deriv-daemon] forced_disabled_symbols=%s",
            sorted(_FORCED_DISABLED_SYMBOLS),
        )

        # One-shot history reset: if DERIV_CLEAR_HISTORY_ON_START=true, truncate
        # closed contracts and open contracts files so the dashboard starts fresh.
        if os.getenv("DERIV_CLEAR_HISTORY_ON_START", "").lower() in {"1", "true", "yes"}:
            for _reset_path in (
                self._settings.closed_contracts_file,
                self._settings.open_contracts_file,
            ):
                try:
                    _reset_path.write_text("[]")
                    _LOGGER.warning(
                        "[deriv-daemon] DERIV_CLEAR_HISTORY_ON_START: cleared %s",
                        _reset_path.name,
                    )
                except OSError as _re:
                    _LOGGER.warning("[deriv-daemon] clear-history failed for %s: %s", _reset_path, _re)

        # One-shot DB purge: if DERIV_DB_PURGE_ON_START=true, TRUNCATE the
        # deriv_contracts + deriv_tick_snapshots tables so the analytics DB
        # starts fresh. Safe — only touches deriv_* tables, never PAMM /
        # user_trade_allocations / ledger_transactions. Wrapped in try/except
        # so a DB issue can never block the daemon from starting.
        if os.getenv("DERIV_DB_PURGE_ON_START", "").lower() in {"1", "true", "yes"}:
            try:
                import asyncpg  # type: ignore[import-not-found]
                _db_url = os.getenv("DATABASE_URL", "").strip()
                if _db_url:
                    _conn = await asyncpg.connect(_db_url, timeout=10.0)
                    try:
                        for _tbl in ("deriv_contracts", "deriv_tick_snapshots"):
                            try:
                                await _conn.execute(f"TRUNCATE TABLE {_tbl} RESTART IDENTITY CASCADE")
                                _LOGGER.warning(
                                    "[deriv-daemon] DERIV_DB_PURGE_ON_START: TRUNCATE %s OK", _tbl,
                                )
                            except Exception as _tex:  # noqa: BLE001
                                _LOGGER.warning(
                                    "[deriv-daemon] DERIV_DB_PURGE_ON_START: TRUNCATE %s failed: %s",
                                    _tbl, _tex,
                                )
                    finally:
                        await _conn.close()
                else:
                    _LOGGER.warning("[deriv-daemon] DERIV_DB_PURGE_ON_START: no DATABASE_URL")
            except Exception as _dex:  # noqa: BLE001
                _LOGGER.warning("[deriv-daemon] DERIV_DB_PURGE_ON_START error: %s", _dex)

        # Connect WS first so ticks_history calls (preload) have a live socket.
        # The OTP URL is the auth token — once connected we are fully authorised.
        # We wait 1.5 s after connect before the batch ticks_history requests so
        # the server-side session is fully initialised.
        try:
            await asyncio.wait_for(self._client.connect(), timeout=20.0)
            _LOGGER.info("[deriv-daemon] WS connected — waiting 1.5 s before history preload")
            await asyncio.sleep(1.5)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-daemon] WS pre-connect failed: %s — preload skipped", exc)

        # Preload tick history so the risk engine + analyst are warm from tick 1
        try:
            await asyncio.wait_for(self._analyst.preload_history(), timeout=30.0)
        except asyncio.TimeoutError:
            _LOGGER.warning("[deriv-daemon] history preload timed out — continuing cold")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-daemon] history preload error: %s — continuing cold", exc)

        # Seed the risk engine warmup counter with the preloaded ticks so the bot
        # does not start blind.  ingest_tick() is idempotent: it just appends to
        # the rolling buffer and increments _ingest_tick_count — calling it here
        # with historical prices gives the warmup the same guarantee as live ticks.
        _preload_summary: dict[str, int] = {}
        for _sym, _prices in self._analyst._history.items():
            for _p in _prices:
                self._risk.ingest_tick(_sym, float(_p))
            _preload_summary[_sym] = len(_prices)
        if _preload_summary:
            _LOGGER.info(
                "[deriv-daemon] risk-engine warmup seeded from preload: %s",
                {s: n for s, n in _preload_summary.items()},
            )

        # Spawn the reaper as a background task; cancel on shutdown.
        _LOGGER.info(
            "[R75_REACTIVACION] R_75 reactivado: sl_mult=1.8, stop_loss_pct_override=0.36 "
            "(SL=$0.54 en stake $1.50 — supera floor $0.50 del broker), stake_max=$1.50. "
            "Causa raíz anterior: SL floor dominaba en 1-2 min con ATR_abs≈4.1.",
        )
        _LOGGER.info(
            "[R50_SL_FIX] R_50 stop_loss_pct_override=0.36 stake_max=$1.50 aplicado. "
            "SL = 0.36 × $1.50 = $0.54 > $0.50 floor broker. "
            "Causa raíz: mean_rev stop_loss_pct=0.004 producía sl_usd≪$0.50 (floor) "
            "→ trade cerraba en SL a los 2-3 min por floor del broker.",
        )
        _LOGGER.info(
            "[R100_SL_FIX] R_100 stop_loss_pct_override=0.36 stake_max=$1.50 aplicado. "
            "Mismo fix que R_50/R_75: SL = 0.36 × $1.50 = $0.54 > $0.50 floor broker. "
            "R_100 score=8.83 puede entrar cuando hd suba → sin este fix cerraría en 2-3 min.",
        )
        # ── BOOM_CRASH_GATE — Phase 12 escape_valve config ──────────────────────
        import os as _os
        _LOGGER.info(
            "[BOOM_CRASH_GATE] Phase12 escape_valve=%s no_fvg_penalty=%.2f "
            "calm_effective_min=%.2f  "
            "(DERIV_BOOM_CRASH_ESCAPE_VALVE=%s DERIV_BOOM_CRASH_NO_FVG_PENALTY=%s "
            "DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN=%s)",
            self._settings.boom_crash_escape_valve,
            self._settings.boom_crash_no_fvg_penalty,
            self._settings.boom_crash_calm_effective_min,
            _os.getenv("DERIV_BOOM_CRASH_ESCAPE_VALVE", "unset"),
            _os.getenv("DERIV_BOOM_CRASH_NO_FVG_PENALTY", "unset"),
            _os.getenv("DERIV_BOOM_CRASH_CALM_EFFECTIVE_MIN", "unset"),
        )
        # ── DPM_CONFIG — log ratchet params for every DPM-registered symbol ─────
        # T4 2026-05-19: visible on every boot to confirm DPM is active.
        _LOGGER.info("[DPM_CONFIG] DynamicPositionManager activo para todos los símbolos:")
        for _dpm_sym, _dpm_p in sorted(_DPM_PARAMS.items()):
            _LOGGER.info(
                "[DPM_CONFIG] %s ratchet=True sl_inicial=%.0f%% step=%.0f%% "
                "ratio=%.2f window=%dt threshold=%.0f%% dur=%ds",
                _dpm_sym,
                _dpm_p["sl_inicial_pct"] * 100,
                _dpm_p["ratchet_step_pct"] * 100,
                _dpm_p["ratchet_ratio"],
                _dpm_p["momentum_window"],
                _dpm_p["agotamiento_threshold"] * 100,
                _dpm_p["max_duration_seg"],
            )
        # ── PROFILE_SCORE_MIN — log score_min for all active BOOM/CRASH + R_* profiles ─
        # Confirms which minimum is active in the deployed container vs what's coded.
        _SPIKE_SYMS = [
            "BOOM300", "BOOM500", "BOOM600", "BOOM900", "BOOM1000",
            "CRASH300", "CRASH500", "CRASH600", "CRASH900", "CRASH1000",
        ]
        for _psym in _SPIKE_SYMS:
            _prf = get_asset_profile(_psym)
            _LOGGER.info(
                "[PROFILE_SCORE_MIN] %s score_min=%.2f hurst_min_spike=%.3f "
                "stake_max=$%.2f geo_max=%s sl_override=%s",
                _psym,
                float(_prf.get("min_score", 0.0)),
                float(_prf.get("hurst_min_spike") or 0.0),
                float(_prf.get("stake_max_usdt") or 0.0),
                str(_prf.get("geo_entry_max", "n/a")),
                str(_prf.get("stop_loss_pct_override", "default")),
            )
        for _rsym in ("R_50", "R_75", "R_100"):
            _rprf = get_asset_profile(_rsym)
            _LOGGER.info(
                "[PROFILE_SCORE_MIN] %s score_min=%.2f stake_max=$%.2f sl_override=%s",
                _rsym,
                float(_rprf.get("min_score", 0.0)),
                float(_rprf.get("stake_max_usdt") or 0.0),
                str(_rprf.get("stop_loss_pct_override", "default")),
            )
        reaper_task      = asyncio.create_task(self._reaper_loop(), name="deriv-reaper")
        recon_task       = asyncio.create_task(self._executor.reconciliation_loop(), name="deriv-recon")
        timeout_task     = asyncio.create_task(self._executor.timeout_clock_loop(), name="deriv-timeout-clock")
        grim_reaper_task = asyncio.create_task(self._executor.verify_orphaned_contracts(), name="deriv-grim-reaper")
        heartbeat_task   = asyncio.create_task(self._executor.heartbeat_loop(), name="deriv-heartbeat")
        status_task      = asyncio.create_task(self._status_writer_loop(), name="deriv-status")
        balance_task     = asyncio.create_task(self._balance_refresh_loop(), name="deriv-balance")
        history_task     = asyncio.create_task(self._analyst.history_refresh_loop(), name="deriv-history")
        calibrator_task  = asyncio.create_task(
            HurstCalibrator().calibration_loop(), name="deriv-hurst-calib"
        )
        macro_hd_task    = asyncio.create_task(
            MacroHDCalibrator().calibration_loop(), name="deriv-macro-hd"
        )
        dynamic_cfg_task = asyncio.create_task(
            self._dynamic_config_refresh_loop(), name="deriv-dynamic-config"
        )
        ttl_task         = asyncio.create_task(self._snapshot_ttl_loop(), name="deriv-ttl")
        ws_task          = asyncio.create_task(
            self._client.run_forever(self._handle_tick), name="deriv-ws"
        )
        stop_task        = asyncio.create_task(self._stop_event.wait(), name="deriv-stop")

        all_tasks = {
            ws_task, reaper_task, recon_task, timeout_task, stop_task,
            status_task, balance_task, history_task, calibrator_task,
            macro_hd_task, dynamic_cfg_task, ttl_task, heartbeat_task,
        }
        try:
            done, _pending = await asyncio.wait(
                all_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                if t.exception() is not None:
                    _LOGGER.exception("[deriv-daemon] task crashed: %s", t.get_name(),
                                      exc_info=t.exception())
        finally:
            for t in all_tasks:
                t.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await t
            self._write_status(connected=False)
            await self._client.close()
            _LOGGER.info("[deriv-daemon] shutdown complete")

    def request_stop(self) -> None:
        if not self._stop_event.is_set():
            _LOGGER.info("[deriv-daemon] stop requested")
            self._stop_event.set()

    # ─────────────────────────────────────────────────────────────────────────
    # Tick handler — fan-out dispatcher
    # Each symbol's full pipeline is spawned as an independent asyncio.Task so
    # BOOM500 and CRASH500 (or any pair) process concurrently without blocking
    # each other during the AI cache-check or LLM call.
    # ───────────────────────────────────────────────────────────────────────────
    async def _handle_tick(self, tick: NormalisedTick) -> None:
        # Lightweight counters and last-price update execute inline (no I/O).
        self._counters["ticks_total"] += 1
        self._last_ticks[tick.symbol] = {
            "price": float(tick.price),
            "ts": datetime.now(timezone.utc).isoformat(),
            "spread": float(tick.metrics.get("spread") or 0.0),
        }
        # Feed ingest buffers inline — pure in-memory, μs cost.
        self._risk.ingest_tick(tick.symbol, tick.price)
        self._analyst.ingest_live_tick(tick.symbol, tick.price)
        self._velocity.ingest_tick(tick.symbol, tick.price)
        _ghost_spike_ts = float(self._risk.get_last_spike_ts(tick.symbol) or 0.0)
        self._ghost_logger.on_tick(tick.symbol, float(tick.price), _ghost_spike_ts)

        # Market-context snapshot (every 60 ticks ≈ 60 s)
        self._maybe_write_market_context(tick.symbol)

        # Periodic diagnostic log every 10 ticks per symbol (visible in Coolify).
        self._diag_tick_count[tick.symbol] = self._diag_tick_count.get(tick.symbol, 0) + 1
        if self._diag_tick_count[tick.symbol] % 10 == 0:
            _summary = self._analyst.get_history_summary().get(tick.symbol) or {}
            _hurst_val = float(_summary.get("hurst") or 0.0)
            _vol_regime = str(_summary.get("vol_regime") or _summary.get("regime") or "?")
            _LOGGER.info(
                "[DIAGNÓSTICO] Símbolo: %s | Hurst Actual: %.2f | Régimen: %s | "
                "Ticks_ingesta: %d",
                tick.symbol, _hurst_val, _vol_regime,
                self._diag_tick_count[tick.symbol],
            )

        # Dispatch the full evaluation pipeline as a per-symbol task so that
        # concurrent symbols (e.g. BOOM500 + CRASH500) never block each other.
        asyncio.create_task(
            self._evaluate_and_trade(tick),
            name=f"eval-{tick.symbol}",
        )

    async def _evaluate_and_trade(self, tick: NormalisedTick) -> None:
        """Full signal evaluation + order pipeline for one tick.  Runs concurrently
        per symbol; never called directly by the WS reader."""
        # BUG-A fix: per-symbol lock prevents two concurrent pipelines from
        # racing past the cooldown/open-contract guard on the same symbol.
        # If a pipeline is already in flight for this symbol we drop the tick
        # — the next tick will re-evaluate once the lock is released.
        lock = self._sym_eval_locks.setdefault(tick.symbol, asyncio.Lock())
        if lock.locked():
            _LOGGER.debug(
                "[deriv-daemon] SYMBOL_LOCKED %s — pipeline already in flight, dropping tick",
                tick.symbol,
            )
            return
        async with lock:
            try:
                await self._pipeline(tick)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("[deriv-daemon] pipeline error for %s (suppressed)", tick.symbol)

    def _is_dry_exit_relax(self, symbol: str) -> tuple[bool, int]:
        """Return (relax_active, consecutive_count) for *symbol*.

        relax_active is True when:
          - DRY_EXIT_RELAX_ENABLED is set
          - The last closed trade for the symbol was a dry exit
            (exit_reason=max_hold_timeout AND peak_profit==0.0)
          - Fewer than MAX_CONSECUTIVE consecutive dry exits have occurred
          - At least COOLDOWN_SEC seconds have elapsed since the last close
        """
        if not _DRY_EXIT_RELAX_ENABLED:
            return False, 0
        info = self._executor.get_dry_exit_info(symbol)
        if not info.get("is_dry"):
            return False, 0
        consec = int(info.get("consecutive") or 0)
        if consec > _DRY_EXIT_RELAX_MAX_CONSECUTIVE:
            return False, consec
        elapsed_since_close = time.time() - float(info.get("closed_at_ts") or 0)
        if elapsed_since_close < _DRY_EXIT_RELAX_COOLDOWN_SEC:
            return False, consec
        return True, consec

    async def _pipeline(self, tick: NormalisedTick) -> None:
        # Refresh vision context file (TTL-cached, offloaded to thread pool)
        await self._try_load_vision_context()

        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 1 — GATE: trade-level cooldown (one trade per symbol at a time)
        # ═══════════════════════════════════════════════════════════════════
        if not self._cooldown.can_fire(tick.symbol):
            _dry_relax_ok, _dry_consec = self._is_dry_exit_relax(tick.symbol)
            if _dry_relax_ok:
                _LOGGER.info(
                    "[DRY_EXIT_RELAX] %s bypassing trade_cooldown | consecutive=%d/%d",
                    tick.symbol, _dry_consec, _DRY_EXIT_RELAX_MAX_CONSECUTIVE,
                )
                # Continue pipeline — do NOT return
            else:
                # mark inactive transition source for reactivation gate
                self._sym_last_inactive_ts[tick.symbol.upper()] = time.time()
                self._sym_last_inactive_reason[tick.symbol.upper()] = "trade_cooldown"
                self._sym_inactive_streak[tick.symbol.upper()] = (
                    self._sym_inactive_streak.get(tick.symbol.upper(), 0) + 1
                )
                self._spike_enrich(tick.symbol, bot_entered=False, block_reason="trade_cooldown")
                return

        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 0b — GATE: disabled / suspended symbols
        # "disabled" = permanently removed from trading (evidenced zero edge).
        # "suspended" = temporarily paused for investigation; re-enable by
        #               removing the flag from ASSET_INTEL_PROFILES.
        # ═══════════════════════════════════════════════════════════════════
        _early_profile = get_asset_profile(tick.symbol)
        _dyn_cfg = self.get_dynamic_config(tick.symbol, _early_profile)
        _dyn_active = bool(_dyn_cfg.get("is_active", False))
        _dyn_source = str(_dyn_cfg.get("source") or "")
        _dyn_inactive_bypassed = False
        if tick.symbol.upper() in _FORCED_DISABLED_SYMBOLS:
            _LOGGER.info("[PIPELINE] SYMBOL_FORCED_DISABLED %s — skipping", tick.symbol)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="symbol_forced_disabled")
            return
        # 2026-05-30 tier-staircase v1: profit lock per UTC hour.
        # If the symbol already banked >= profit_lock_usdt this hour, skip new
        # entries until the next UTC hour boundary (don't risk back the gains).
        _pl_locked = False
        _pl_unlock_at = 0.0
        with suppress(Exception):
            _pl_locked, _pl_unlock_at = self._executor.is_symbol_profit_locked(tick.symbol)
        if _pl_locked:
            _now_pl = time.time()
            _remaining_min = max(0.0, (_pl_unlock_at - _now_pl) / 60.0)
            _last_emit = float(
                self._dynamic_inactive_last_emit_ts.get(f"{tick.symbol}:profit_lock") or 0.0
            )
            if (_now_pl - _last_emit) >= float(self._dynamic_inactive_decision_interval_sec):
                self._dynamic_inactive_last_emit_ts[f"{tick.symbol}:profit_lock"] = _now_pl
                _LOGGER.info(
                    "[PIPELINE] SYMBOL_PROFIT_LOCKED %s — banked quota this hour; "
                    "unlock in %.1f min",
                    tick.symbol,
                    _remaining_min,
                )
            self._sym_last_inactive_ts[tick.symbol.upper()] = time.time()
            self._sym_last_inactive_reason[tick.symbol.upper()] = "symbol_profit_locked"
            self._sym_inactive_streak[tick.symbol.upper()] = (
                self._sym_inactive_streak.get(tick.symbol.upper(), 0) + 1
            )
            self._spike_enrich(
                tick.symbol, bot_entered=False, block_reason="symbol_profit_locked"
            )
            return
        # 2026-05-30 POST-SL ENTRY GATE (spike markets only):
        # Forensic 12h pattern: 36% of broker_sl_hits are followed by a real
        # spike within 120s. Entering pre-spike again in that window almost
        # always SL'd again. Entering post-spike within ~90s captures the move.
        # Rule:
        #   age = now - last_sl_ts(symbol)
        #   spike_since_sl = (last_spike_ts > last_sl_ts)
        #   if 0 < age <= AWAIT_SEC and not spike_since_sl  → BLOCK post_sl_await
        #   if AWAIT_SEC < age <= AWAIT+CHASE_LOCK and not spike_since_sl → BLOCK post_sl_chase_lock
        #   else (spike landed OR window expired) → ALLOW
        if self._post_sl_gate_enabled and is_spike_market(tick.symbol):
            _sl_age = None
            with suppress(Exception):
                _sl_age = self._executor.last_sl_age_sec(tick.symbol)
            if _sl_age is not None and _sl_age > 0:
                _last_sl_ts = float(self._executor.last_sl_ts(tick.symbol) or 0.0)
                _last_spike_ts = 0.0
                with suppress(Exception):
                    _last_spike_ts = float(
                        self._risk._last_spike_ts.get(tick.symbol, 0.0) or 0.0  # noqa: SLF001
                    )
                _spike_since_sl = _last_spike_ts > _last_sl_ts
                _await_lim = float(self._post_sl_await_sec)
                _full_lim = _await_lim + float(self._post_sl_chase_lock_sec)
                _reason = None
                if _sl_age <= _await_lim and not _spike_since_sl:
                    _reason = "post_sl_spike_await"
                elif _sl_age <= _full_lim and not _spike_since_sl:
                    _reason = "post_sl_chase_lock"
                if _reason is not None:
                    _now_psl = time.time()
                    _last_emit = float(
                        self._dynamic_inactive_last_emit_ts.get(
                            f"{tick.symbol}:{_reason}"
                        ) or 0.0
                    )
                    if (_now_psl - _last_emit) >= self._post_sl_emit_throttle_sec:
                        self._dynamic_inactive_last_emit_ts[
                            f"{tick.symbol}:{_reason}"
                        ] = _now_psl
                        _LOGGER.info(
                            "[PIPELINE] %s %s — sl_age=%.0fs await=%ds chase_lock=%ds "
                            "spike_since_sl=%s (waiting for fresh spike to enter)",
                            _reason.upper(),
                            tick.symbol,
                            _sl_age,
                            self._post_sl_await_sec,
                            self._post_sl_chase_lock_sec,
                            _spike_since_sl,
                        )
                    self._spike_enrich(
                        tick.symbol, bot_entered=False, block_reason=_reason
                    )
                    return
        # 2026-05-30 SPIKE-DENSITY GATE — "ya van X spikes esta hora; espera"
        # Cuenta rolling 60m vs baseline horas previas; bloquea si saturado o burst.
        # Reglas (sólo en spike markets, sólo si gate enabled):
        #  1) BURST: count_recent(BURST_WINDOW) >= BURST_THRESHOLD AND
        #     last_spike_age <= BURST_BLOCK_AFTER_SEC → BLOCK 'spike_burst_cooldown'
        #  2) SATURATED: ratio = count60 / max(baseline_avg, MIN_BASELINE);
        #     ratio >= BLOCK_RATIO AND samples >= 2 → BLOCK 'spike_density_saturated'
        #  3) ELEVATED: ratio >= WARN_RATIO → log only (orchestrator AI lo lee)
        # Persiste memoria en spike_density_memory.json para el sidecar AI.
        if self._spike_density_gate_enabled and is_spike_market(tick.symbol):
            _sym_sd = tick.symbol.upper()
            _count60 = 0
            _count_recent = 0
            _curr_h = 0
            _avg_prev = 0.0
            _samples = 0
            _last_spike_age: float | None = None
            with suppress(Exception):
                _count60 = int(self._risk.get_spike_count_last_hour(_sym_sd))
            with suppress(Exception):
                _count_recent = int(
                    self._risk.get_spike_count_recent(
                        _sym_sd, float(self._spike_density_burst_window_sec)
                    )
                )
            with suppress(Exception):
                _curr_h, _avg_prev, _samples = self._risk.get_hourly_spike_buckets(
                    _sym_sd, lookback_hours=self._spike_density_lookback_hours
                )
            with suppress(Exception):
                _last_sp_ts = float(
                    self._risk._last_spike_ts.get(_sym_sd, 0.0) or 0.0  # noqa: SLF001
                )
                if _last_sp_ts > 0:
                    _last_spike_age = max(0.0, time.time() - _last_sp_ts)

            _baseline = max(_avg_prev, float(self._spike_density_min_baseline))
            _ratio = (_count60 / _baseline) if _baseline > 0 else 0.0
            _block_reason_sd: str | None = None
            _block_detail = ""

            # Rule 1 — BURST
            _is_burst = (
                _count_recent >= self._spike_density_burst_threshold
                and _last_spike_age is not None
                and _last_spike_age <= float(self._spike_density_burst_block_after_sec)
            )
            if _is_burst:
                _block_reason_sd = "spike_burst_cooldown"
                _block_detail = (
                    f"recent={_count_recent}≥{self._spike_density_burst_threshold} "
                    f"in {self._spike_density_burst_window_sec}s "
                    f"last_age={_last_spike_age:.0f}s"
                )
            # Rule 2 — SATURATED
            elif (
                _samples >= 2
                and _ratio >= self._spike_density_block_ratio
                and _count60 >= int(self._spike_density_min_baseline)
            ):
                _block_reason_sd = "spike_density_saturated"
                _block_detail = (
                    f"count60={_count60} baseline={_baseline:.2f} "
                    f"ratio={_ratio:.2f}≥{self._spike_density_block_ratio:.2f} "
                    f"samples={_samples}"
                )

            # ── Burst surface fields ──────────────────────────────────────────────
            # burst_depth = count of spikes seen in the burst window
            # burst_retroceso = price retracement ratio R (0–1); < 0.35 = momentum intact
            # burst_active = True while burst cooldown is active
            _burst_depth: int | None = None
            _burst_retroceso: float | None = None
            _burst_active: bool = False
            with suppress(Exception):
                _BURST_MAX_AGE_SEC = float(os.getenv("DERIV_BURST_MAX_AGE_SEC", "600") or "600")
                _now_burst = time.time()
                if _is_burst:
                    _burst_start = self._burst_start_ts.get(_sym_sd, 0.0)
                    if _burst_start == 0.0:
                        # Primer tick del burst — registrar inicio
                        self._burst_start_ts[_sym_sd] = _now_burst
                        _burst_active = True
                        _burst_depth = int(_count_recent)
                    elif _now_burst - _burst_start > _BURST_MAX_AGE_SEC:
                        # Safety valve: el burst lleva demasiado tiempo activo sin
                        # recuperación. Forzar reset para evitar POST_SPIKE_COOLDOWN perpetuo.
                        _burst_active = False
                        self._burst_start_ts[_sym_sd] = 0.0
                        _LOGGER.warning(
                            "[BURST_TIMEOUT] %s burst activo > %.0fs sin recuperación → "
                            "forzando reset (DERIV_BURST_MAX_AGE_SEC=%.0f)",
                            _sym_sd, _now_burst - _burst_start, _BURST_MAX_AGE_SEC,
                        )
                    else:
                        _burst_active = True
                        _burst_depth = int(_count_recent)
                else:
                    # Burst expiró por ventana natural — limpiar timestamp
                    self._burst_start_ts[_sym_sd] = 0.0
                    if _last_spike_age is not None and _last_spike_age <= float(
                        self._spike_density_burst_window_sec
                    ):
                        # Within the burst window but below threshold — surface depth anyway
                        _burst_depth = int(_count_recent)
            with suppress(Exception):
                # Retroceso: use daemon's _last_spike_px cache to compare current price.
                # Populated below when we detect a new spike_ts.
                _known_sp_ts = float(
                    self._risk._last_spike_ts.get(_sym_sd, 0.0) or 0.0  # noqa: SLF001
                )
                _cur_px = float(tick.price or 0.0)
                if _known_sp_ts > 0 and _cur_px > 0:
                    # Update stored spike-price when ts advanced (new spike)
                    _prev_sp_ts = float(self._last_burst_spike_ts.get(_sym_sd, 0.0) or 0.0)
                    if _known_sp_ts > _prev_sp_ts:
                        self._last_burst_spike_ts[_sym_sd] = _known_sp_ts
                        self._last_burst_spike_px[_sym_sd] = _cur_px
                    _sp_px = float(self._last_burst_spike_px.get(_sym_sd, 0.0) or 0.0)
                    if _sp_px > 0 and _burst_active:
                        # For CRASH: spike goes DOWN → retroceso = how far price recovered up
                        # For BOOM:  spike goes UP   → retroceso = how far price recovered down
                        # Simple ratio: distance recovered / spike magnitude (ATR-relative)
                        _sp_move = abs(_cur_px - _sp_px)
                        _atr_ref = float(self._risk.get_current_atr(_sym_sd) or 0.0)
                        if _atr_ref > 0:
                            _burst_retroceso = round(min(1.0, _sp_move / _atr_ref), 4)

            # ── Cascade detection ─────────────────────────────────────────────────
            # Cascade = 2+ spikes in rapid succession with NO measurable retroceso.
            # The price is in free-fall momentum; a retroceso-based BURST entry will
            # never trigger. Instead surface cascade_active + cascade_gap_ticks so
            # the entry gate and UI can react to continuous-momentum entries.
            _cascade_active: bool = False
            _cascade_gap_ticks: int | None = None
            with suppress(Exception):
                _casc_gap_thr = int(os.getenv("DERIV_CASCADE_GAP_TICKS", "60") or 60)
                if (_burst_depth is not None and _burst_depth >= 2
                        and _burst_retroceso is None):
                    # ticks_since_last_spike = distance from spike, not a retroceso gap
                    _cur_tick_n = self._risk.get_tick_count(_sym_sd)
                    _spk_tick_n = self._risk.get_last_spike_tick_count(_sym_sd)
                    _ticks_gap = max(0, _cur_tick_n - _spk_tick_n)
                    if _ticks_gap <= _casc_gap_thr:
                        _cascade_active = True
                        _cascade_gap_ticks = int(_ticks_gap)

            # Store in fvg_state cache so _maybe_write_market_context can surface them
            with suppress(Exception):
                _bsym = _sym_sd.upper()
                if _bsym not in self._last_fvg_state:
                    self._last_fvg_state[_bsym] = {}
                self._last_fvg_state[_bsym].update({
                    "burst_depth":        _burst_depth,
                    "burst_retroceso":    _burst_retroceso,
                    "burst_active":       _burst_active,
                    "cascade_active":     _cascade_active,
                    "cascade_gap_ticks":  _cascade_gap_ticks,
                })

            # Persist memory snapshot for AI sidecar to learn from
            with suppress(Exception):
                _now_sd = time.time()
                _pace_sd = {}
                try:
                    _pace_sd = self._risk.get_spike_pace_state(_sym_sd, 1200.0)
                except Exception:
                    _pace_sd = {}
                _imm_sd = {}
                try:
                    _imm_sd = self._risk.get_spike_imminence_state(_sym_sd)
                except Exception:
                    _imm_sd = {}
                _mem_entry = {
                    "ts": _now_sd,
                    "iso": datetime.fromtimestamp(_now_sd, tz=timezone.utc).isoformat(),
                    "count_60m": _count60,
                    "count_recent_window_sec": self._spike_density_burst_window_sec,
                    "count_recent": _count_recent,
                    "baseline_avg_prev_h": round(_avg_prev, 3),
                    "baseline_used": round(_baseline, 3),
                    "ratio": round(_ratio, 3),
                    "samples": _samples,
                    # 2026-06-01 cadence awareness: where are we in the per-symbol
                    # 20-min hot-window rhythm right now (HOT/NORMAL/COLD).
                    "pace_state": _pace_sd.get("state"),
                    "pace_ratio": _pace_sd.get("ratio"),
                    "pace_spikes_20m": _pace_sd.get("spikes_window"),
                    "pace_expected_20m": _pace_sd.get("expected_window"),
                    "pace_rate_per_hour": _pace_sd.get("rate_per_hour"),
                    # 2026-06-01 imminence ("malicia"): is the symbol loaded to fire?
                    "imminence_state": _imm_sd.get("state"),
                    "imminence_score": _imm_sd.get("score"),
                    "ticks_since_last_spike": _imm_sd.get("ticks_since_last"),
                    "last_spike_age_sec": (
                        round(_last_spike_age, 1) if _last_spike_age is not None else None
                    ),
                    "block_reason": _block_reason_sd,
                    "block_detail": _block_detail,
                    "elevated": (_ratio >= self._spike_density_warn_ratio),
                }
                _prev_mem = self._spike_density_memory.get(_sym_sd, {})
                _events_log = list(_prev_mem.get("recent_events") or [])
                # only append meaningful events (block or elevated) to keep file small
                if _block_reason_sd is not None or _mem_entry["elevated"]:
                    _events_log.append({
                        "ts": _now_sd,
                        "iso": _mem_entry["iso"],
                        "block_reason": _block_reason_sd,
                        "ratio": _mem_entry["ratio"],
                        "count_60m": _count60,
                        "count_recent": _count_recent,
                    })
                    if len(_events_log) > 40:
                        _events_log = _events_log[-40:]
                self._spike_density_memory[_sym_sd] = {
                    "last": _mem_entry,
                    "recent_events": _events_log,
                }
                # throttle disk writes to once every 30s
                if (_now_sd - self._spike_density_memory_last_persist_ts) >= 30.0:
                    self._spike_density_memory_last_persist_ts = _now_sd
                    if self._spike_density_memory_path is None:
                        self._spike_density_memory_path = (
                            self._ctx_state_dir / "spike_density_memory.json"
                        )
                    _payload = {
                        "updated_at": _mem_entry["iso"],
                        "config": {
                            "burst_window_sec": self._spike_density_burst_window_sec,
                            "burst_threshold": self._spike_density_burst_threshold,
                            "block_ratio": self._spike_density_block_ratio,
                            "warn_ratio": self._spike_density_warn_ratio,
                            "min_baseline": self._spike_density_min_baseline,
                            "lookback_hours": self._spike_density_lookback_hours,
                        },
                        "per_symbol": self._spike_density_memory,
                    }
                    _tmp = self._spike_density_memory_path.with_suffix(".json.tmp")
                    _tmp.parent.mkdir(parents=True, exist_ok=True)
                    _tmp.write_text(json.dumps(_payload, ensure_ascii=False))
                    _tmp.replace(self._spike_density_memory_path)

            if _block_reason_sd is not None:
                _now_emit = time.time()
                _emit_key = f"{_sym_sd}:{_block_reason_sd}"
                _last_emit_sd = float(
                    self._dynamic_inactive_last_emit_ts.get(_emit_key) or 0.0
                )
                if (_now_emit - _last_emit_sd) >= self._spike_density_emit_throttle_sec:
                    self._dynamic_inactive_last_emit_ts[_emit_key] = _now_emit
                    _LOGGER.info(
                        "[PIPELINE] %s %s — %s (ya van %d spikes esta hora; "
                        "espera mejor oportunidad)",
                        _block_reason_sd.upper(),
                        _sym_sd,
                        _block_detail,
                        _count60,
                    )
                self._spike_enrich(
                    tick.symbol, bot_entered=False, block_reason=_block_reason_sd
                )
                return
            elif _ratio >= self._spike_density_warn_ratio and _count60 >= 5:
                # Elevated — log throttled, do NOT block (AI orchestrator can use it)
                _now_emit = time.time()
                _emit_key = f"{_sym_sd}:spike_density_elevated"
                _last_emit_sd = float(
                    self._dynamic_inactive_last_emit_ts.get(_emit_key) or 0.0
                )
                if (_now_emit - _last_emit_sd) >= self._spike_density_emit_throttle_sec:
                    self._dynamic_inactive_last_emit_ts[_emit_key] = _now_emit
                    _LOGGER.info(
                        "[PIPELINE] SPIKE_DENSITY_ELEVATED %s — count60=%d "
                        "baseline=%.2f ratio=%.2f (no block, AI puede subir score)",
                        _sym_sd, _count60, _baseline, _ratio,
                    )
        # 2026-05-30 REACTIVATION-AWARENESS GATE
        # Cuando un símbolo sale de profit_locked / dynamic_inactive /
        # trade_cooldown, durante GRACE_SEC el bot DEBE saber qué pasó
        # mientras descansaba. Si hubo ≥BURST_THRESHOLD spikes en los últimos
        # EXTENDED_WINDOW_SEC y todavía NO ha llegado un spike fresco
        # post-reactivación, BLOQUEA hasta que ese spike fresco llegue.
        # Esto evita "entrar ciego" justo después de que el símbolo regresa.
        if self._reactivation_gate_enabled and is_spike_market(tick.symbol):
            _sym_rg = tick.symbol.upper()
            _streak = int(self._sym_inactive_streak.get(_sym_rg, 0) or 0)
            _last_inact = float(self._sym_last_inactive_ts.get(_sym_rg, 0.0) or 0.0)
            _react_known = float(self._sym_reactivation_ts.get(_sym_rg, 0.0) or 0.0)
            _now_rg = time.time()
            # Detect transition: previously had inactive streak ≥3 ticks AND
            # this tick is past the inactive blocks AND last_inactive > known react.
            # Suppress re-detection while we're still inside the GRACE window from
            # a previous detection (avoids log spam when symbol has high tick rate
            # and inactive returns fire many times between active ticks).
            _suppress_redetect = (
                _react_known > 0
                and (_now_rg - _react_known) < float(self._reactivation_grace_sec)
            )
            if (
                _streak >= 3
                and _last_inact > 0
                and _last_inact > _react_known
                and not _suppress_redetect
            ):
                # The symbol was inactive and now is active — record reactivation
                self._sym_reactivation_ts[_sym_rg] = _last_inact
                _react_known = _last_inact
                self._sym_inactive_streak[_sym_rg] = 0
                with suppress(Exception):
                    _LOGGER.info(
                        "[PIPELINE] REACTIVATION_DETECTED %s — was '%s', now active "
                        "(streak ticks reset)",
                        _sym_rg,
                        self._sym_last_inactive_reason.get(_sym_rg, "?"),
                    )
            # Apply gate only during GRACE window after a reactivation
            if _react_known > 0 and (_now_rg - _react_known) <= float(self._reactivation_grace_sec):
                _spikes_ext = 0
                with suppress(Exception):
                    _spikes_ext = int(
                        self._risk.get_spike_count_recent(
                            _sym_rg, float(self._reactivation_extended_window_sec)
                        )
                    )
                _last_spike_rg = 0.0
                with suppress(Exception):
                    _last_spike_rg = float(
                        self._risk._last_spike_ts.get(_sym_rg, 0.0) or 0.0  # noqa: SLF001
                    )
                _fresh_spike_since_react = _last_spike_rg > _react_known
                if (
                    _spikes_ext >= self._reactivation_burst_threshold
                    and not _fresh_spike_since_react
                ):
                    _emit_key = f"{_sym_rg}:reactivation_awareness"
                    _last_emit = float(
                        self._dynamic_inactive_last_emit_ts.get(_emit_key) or 0.0
                    )
                    if (_now_rg - _last_emit) >= self._reactivation_emit_throttle_sec:
                        self._dynamic_inactive_last_emit_ts[_emit_key] = _now_rg
                        _LOGGER.info(
                            "[PIPELINE] REACTIVATION_AWARENESS_NO_FRESH_SPIKE %s — "
                            "while inactive '%s' tuvo %d spikes en %ds; espera "
                            "spike fresco antes de entrar (react_age=%.0fs grace=%ds)",
                            _sym_rg,
                            self._sym_last_inactive_reason.get(_sym_rg, "?"),
                            _spikes_ext,
                            self._reactivation_extended_window_sec,
                            _now_rg - _react_known,
                            self._reactivation_grace_sec,
                        )
                    # Persist to density memory so LLM sees it
                    with suppress(Exception):
                        _mem = self._spike_density_memory.setdefault(_sym_rg, {})
                        _mem["reactivation_state"] = {
                            "ts": _react_known,
                            "iso": datetime.fromtimestamp(
                                _react_known, tz=timezone.utc
                            ).isoformat(),
                            "from_reason": self._sym_last_inactive_reason.get(_sym_rg),
                            "spikes_in_extended_window": _spikes_ext,
                            "extended_window_sec": self._reactivation_extended_window_sec,
                            "awaiting_fresh_spike": True,
                            "react_age_sec": round(_now_rg - _react_known, 1),
                        }
                    self._spike_enrich(
                        tick.symbol, bot_entered=False,
                        block_reason="reactivation_awareness_no_fresh_spike",
                    )
                    return
                elif _fresh_spike_since_react:
                    # Fresh spike landed → mark awareness satisfied
                    with suppress(Exception):
                        _mem = self._spike_density_memory.setdefault(_sym_rg, {})
                        if "reactivation_state" in _mem:
                            _mem["reactivation_state"]["awaiting_fresh_spike"] = False
                            _mem["reactivation_state"]["fresh_spike_ts"] = _last_spike_rg
        if _early_profile.get("disabled"):
            _LOGGER.debug("[PIPELINE] SYMBOL_DISABLED %s — skipping", tick.symbol)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="symbol_disabled")
            return
        if _early_profile.get("suspended"):
            _LOGGER.debug("[PIPELINE] SYMBOL_SUSPENDED %s — skipping", tick.symbol)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="symbol_suspended")
            return
        if _dyn_source == "dynamic_db" and not _dyn_active:
            _sym_u = tick.symbol.upper()
            if _DYNAMIC_INACTIVE_BYPASS_ENABLE and _sym_u in _DYNAMIC_INACTIVE_BYPASS_SYMBOLS:
                _dyn_inactive_bypassed = True
                _dyn_active = True
                _dyn_cfg = {
                    **_dyn_cfg,
                    "is_active": True,
                    "inactive_bypassed": True,
                    "source": "dynamic_db_bypass",
                }
                _bypass_now = time.time()
                _bypass_key = f"{tick.symbol}:dynamic_bypass"
                _bypass_last = float(self._dynamic_inactive_last_emit_ts.get(_bypass_key) or 0.0)
                if (_bypass_now - _bypass_last) >= float(self._dynamic_inactive_decision_interval_sec):
                    self._dynamic_inactive_last_emit_ts[_bypass_key] = _bypass_now
                    _LOGGER.info(
                        "[PIPELINE] SYMBOL_DYNAMIC_INACTIVE_BYPASS %s — continuing with dynamic config",
                        tick.symbol,
                    )
            else:
                _LOGGER.info("[PIPELINE] SYMBOL_DYNAMIC_INACTIVE %s — skipping", tick.symbol)
                _inactive_now = time.time()
                _inactive_last = float(self._dynamic_inactive_last_emit_ts.get(tick.symbol) or 0.0)
                if (_inactive_now - _inactive_last) >= float(self._dynamic_inactive_decision_interval_sec):
                    self._dynamic_inactive_last_emit_ts[tick.symbol] = _inactive_now
                    _dyn_score_min = float(_dyn_cfg.get("score_min_override") or 0.0)
                    _dyn_regime = str(_dyn_cfg.get("market_regime") or "NORMAL")
                    _dyn_spf = int(_dyn_cfg.get("spike_pre_filter_target") or 0)
                    self._record_decision(
                        symbol=tick.symbol,
                        allowed=False,
                        side=None,
                        score=_dyn_score_min,
                        reason=(
                            f"DYNAMIC_INACTIVE: regime={_dyn_regime} "
                            f"score_min={_dyn_score_min:.2f} spf={_dyn_spf}"
                        ),
                        extra={
                            "dynamic_inactive": True,
                            "dynamic_cfg_source": _dyn_source,
                            "dynamic_cfg_regime": _dyn_regime,
                            "dynamic_cfg_score_min": round(_dyn_score_min, 3),
                            "dynamic_cfg_spike_pre_filter": _dyn_spf,
                        },
                    )
                self._sym_last_inactive_ts[tick.symbol.upper()] = time.time()
                self._sym_last_inactive_reason[tick.symbol.upper()] = "dynamic_symbol_inactive"
                self._sym_inactive_streak[tick.symbol.upper()] = (
                    self._sym_inactive_streak.get(tick.symbol.upper(), 0) + 1
                )
                self._spike_enrich(tick.symbol, bot_entered=False, block_reason="dynamic_symbol_inactive")
                return

        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 0c — GATE: spike pre-filter (BOOM/CRASH only)
        # If a spike was detected within the last spike_min_post_sec seconds,
        # the market is in the POST-SPIKE drift / accumulation phase.
        # Entering from the eval loop NOW would:
        #   1. Block the spike engine via trade_cooldown for the entire hold window
        #   2. Enter at wrong timing — next spike is still cycle_sec away
        # Formula: min_post = profile.spike_min_post_sec (default: 0 = disabled)
        # Data basis: cycle=900s, hold=500s → need entry at t≥400s; for cycle=600s,
        # hold=350s → need entry at t≥250s.
        # ═══════════════════════════════════════════════════════════════════
        if is_spike_market(tick.symbol) and not self._entry_tick_only:
            _spf_source = "profile"
            _spf_expected = int(_early_profile.get("spike_interval_ticks", 0) or 0)
            _spf_min_post = 0
            if _dyn_active:
                _spf_min_post = int(_dyn_cfg.get("spike_pre_filter_target", 0) or 0)
                _spf_source = "dynamic"
            else:
                # Profile value reused as tick count (≈1 tick/s for BOOM/CRASH).
                _spf_min_post_profile = float(_early_profile.get("spike_min_post_sec", 0))
                if _spf_min_post_profile > 0:
                    _spf_base = int(_spf_min_post_profile)
                    _spf_min_post = self._risk.get_adaptive_spike_min_post_ticks(
                        tick.symbol,
                        _spf_base,
                        _spf_expected,
                    )
            if _spf_min_post > 0:
                _spf_last_tick = self._risk.get_last_spike_tick_count(tick.symbol)
                if _spf_last_tick > 0:
                    _spf_ticks_since = self._risk.get_tick_count(tick.symbol) - _spf_last_tick
                    if _spf_ticks_since < _spf_min_post:
                        if _spf_source == "dynamic":
                            _LOGGER.debug(
                                "[SPIKE_PRE_FILTER_DYNAMIC] %s %d ticks since spike < %d ticks target "
                                "(regime=%s) — blocking eval entry",
                                tick.symbol,
                                _spf_ticks_since,
                                int(_spf_min_post),
                                _dyn_cfg.get("market_regime", "NORMAL"),
                            )
                            _block_reason = (
                                f"spike_pre_filter_dynamic:{_spf_ticks_since}t<{int(_spf_min_post)}t"
                            )
                        else:
                            _LOGGER.debug(
                                "[SPIKE_PRE_FILTER] %s %d ticks since spike < %d ticks window "
                                "(expected=%d adaptive_profile) — blocking eval entry (post-spike drift phase)",
                                tick.symbol,
                                _spf_ticks_since,
                                int(_spf_min_post),
                                _spf_expected,
                            )
                            _block_reason = (
                                f"spike_pre_filter:{_spf_ticks_since}t<{int(_spf_min_post)}t"
                            )
                        self._spike_enrich(
                            tick.symbol,
                            bot_entered=False,
                            block_reason=_block_reason,
                        )
                        return

        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 0d — GATE: tick-only anti-chase (BOOM/CRASH only)
        # In tick-only mode we still veto entries that happen shortly AFTER
        # a detected spike, because those are typically pursuit entries.
        # This is a defensive veto only; it never triggers entries.
        # ═══════════════════════════════════════════════════════════════════
        if (
            self._entry_tick_only
            and is_spike_market(tick.symbol)
        ):
            _post_spike_block_sec = self._post_spike_chase_block_ticks_for_symbol(
                tick.symbol,
                _early_profile,
                _dyn_cfg,
                _dyn_active,
            )
            if _post_spike_block_sec <= 0:
                _post_spike_block_sec = 0.0
            _last_spike_tick = self._risk.get_last_spike_tick_count(tick.symbol)
            if _last_spike_tick > 0:
                _elapsed_post_spike = float(self._risk.get_tick_count(tick.symbol) - _last_spike_tick)
                if 0 <= _elapsed_post_spike < _post_spike_block_sec:
                    # ── 2026-06-02 HOT RE-ENTRY bypass (operator directive) ──
                    # If the symbol is HOT (just fired AND firing recently), do
                    # NOT hard-block pre-scoring. Defer to scoring + the post-
                    # spike strength gate, which only lets score>=8 fresh
                    # confirmations through (weak scores still get the chase
                    # guard there). SL protections cut any bad re-entry small.
                    _hot_ok = False
                    if str(os.getenv("DERIV_HOT_REENTRY_ENABLE", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}:
                        _hot_fresh = float(os.getenv("DERIV_HOT_REENTRY_FRESH_TICKS", "300") or 300)
                        _hot_win = float(os.getenv("DERIV_HOT_REENTRY_WINDOW_SEC", "300") or 300)
                        _hot_min_sp = max(1, int(float(os.getenv("DERIV_HOT_REENTRY_MIN_SPIKES", "1") or 1)))
                        try:
                            _hot_n = int(self._risk.get_spike_count_recent(tick.symbol, _hot_win))
                        except Exception:
                            _hot_n = 0
                        if _elapsed_post_spike <= _hot_fresh and _hot_n >= _hot_min_sp:
                            _hot_ok = True
                            _LOGGER.info(
                                "[POST_SPIKE_CHASE_GUARD] %s HOT bypass: elapsed=%.0ft "
                                "spikes=%d/%ds → defer to score>=8 strength gate",
                                tick.symbol, _elapsed_post_spike, _hot_n, int(_hot_win),
                            )
                    if not _hot_ok:
                        _block_reason = (
                            "post_spike_chase_guard:"
                            f"{_elapsed_post_spike:.0f}t<"
                            f"{_post_spike_block_sec:.0f}t"
                        )
                        _LOGGER.debug(
                            "[POST_SPIKE_CHASE_GUARD] %s blocked: elapsed=%.0ft < %.0ft",
                            tick.symbol,
                            _elapsed_post_spike,
                            _post_spike_block_sec,
                        )
                        self._spike_enrich(
                            tick.symbol,
                            bot_entered=False,
                            block_reason=_block_reason,
                        )
                        return

        # ═══════════════════════════════════════════════════════════════════
        # MATURITY_GATE — structural floor: first FRAC of the typical cycle
        # Blocks entry while elapsed wall-clock time < median_gap × FRAC.
        # Uses get_scarcity_state() (same source as the operator card "típico").
        # Env vars: DERIV_MATURITY_GATE_ENABLED (default true)
        #           DERIV_MATURITY_GATE_FRAC    (default 0.70)
        # ═══════════════════════════════════════════════════════════════════
        if (
            is_spike_market(tick.symbol)
            and str(os.getenv("DERIV_MATURITY_GATE_ENABLED", "true")).strip().lower()
               in ("1", "true", "yes", "on")
        ):
            _mat_scar = self._risk.get_scarcity_state(tick.symbol)
            _mat_median_s = _mat_scar.get("median_gap_s")
            _mat_elapsed_s = _mat_scar.get("elapsed_s")
            if _mat_median_s and _mat_elapsed_s is not None and _mat_median_s > 0:
                # Post-cluster: score is inflated by cluster bonus (spike_cluster_bonus).
                # Data: cluster entries WR=38.5% (-$0.287/trade) vs 53.5% (-$0.039) single spike.
                # When cluster_n>=2, require elapsed >= P50 (1.0×median) instead of 0.70.
                _mat_cl_n = int(self._risk.get_spike_count_recent(tick.symbol, 300) or 0)
                _mat_frac_base = float(os.getenv("DERIV_MATURITY_GATE_FRAC", "0.70"))
                _mat_frac_cluster = float(os.getenv("DERIV_MATURITY_GATE_CLUSTER_FRAC", "1.0"))
                _mat_frac = _mat_frac_cluster if _mat_cl_n >= 2 else _mat_frac_base
                _mat_threshold_s = _mat_median_s * _mat_frac
                if _mat_elapsed_s < _mat_threshold_s:
                    _mat_remain_s = _mat_threshold_s - _mat_elapsed_s
                    _mat_key = f"{tick.symbol}:early_entry_gate"
                    _mat_last_emit = float(
                        self._dynamic_inactive_last_emit_ts.get(_mat_key) or 0.0
                    )
                    _mat_now = time.time()
                    if (_mat_now - _mat_last_emit) >= 30.0:
                        self._dynamic_inactive_last_emit_ts[_mat_key] = _mat_now
                        _LOGGER.info(
                            "[MATURITY_GATE] EARLY_ENTRY_GATE %s | elapsed=%.0fs < "
                            "threshold=%.0fs (%.0f%% × median=%.0fs) | remain=%.0fs | cluster_n=%d",
                            tick.symbol,
                            _mat_elapsed_s,
                            _mat_threshold_s,
                            _mat_frac * 100,
                            _mat_median_s,
                            _mat_remain_s,
                            _mat_cl_n,
                        )
                        self._record_decision(
                            symbol=tick.symbol, allowed=False, side=None, score=0.0,
                            reason=(
                                f"EARLY_ENTRY_GATE: warmup_remain={_mat_remain_s:.0f}s "
                                f"(elapsed={_mat_elapsed_s:.0f}s < threshold={_mat_threshold_s:.0f}s)"
                            ),
                            extra={
                                "gate_name": "EARLY_ENTRY_GATE",
                                "remain_sec": max(0, int(_mat_remain_s)),
                            },
                        )
                    # DRY_EXIT_RELAX: if last close was a zero-peak timeout and the
                    # scarcity pressure has not reset, bypass the structural floor so
                    # the bot can re-position without waiting the full maturity window.
                    if _DRY_EXIT_RELAX_SKIP_MATURITY:
                        _dry_mat_ok, _dry_mat_consec = self._is_dry_exit_relax(tick.symbol)
                        if _dry_mat_ok:
                            _LOGGER.info(
                                "[DRY_EXIT_RELAX] %s bypassing MATURITY_GATE | "
                                "elapsed=%.0fs < threshold=%.0fs | consecutive=%d/%d",
                                tick.symbol, _mat_elapsed_s, _mat_threshold_s,
                                _dry_mat_consec, _DRY_EXIT_RELAX_MAX_CONSECUTIVE,
                            )
                            # Do NOT return — continue to score/RNG evaluation
                        else:
                            self._spike_enrich(
                                tick.symbol, bot_entered=False, block_reason="early_entry_gate"
                            )
                            return
                    else:
                        self._spike_enrich(
                            tick.symbol, bot_entered=False, block_reason="early_entry_gate"
                        )
                        return

        # types: trend_math, smc_confluence, micro_scalp_mr).
        # Checked BEFORE any scoring so zero CPU is wasted on cooling symbols.
        # Per-symbol override via ASSET_INTEL_PROFILES['cooldown_sec'] takes
        # precedence over the global DERIV_SIGNAL_COOLDOWN_SEC env var.
        # ═══════════════════════════════════════════════════════════════════
        # Tick-based signal cooldown: consistent with the tick-domain signal stack.
        _global_cd = int(os.getenv("DERIV_SIGNAL_COOLDOWN_TICKS", os.getenv("DERIV_SIGNAL_COOLDOWN_SEC", "180")))
        _profile_cd = int(get_asset_profile(tick.symbol).get("cooldown_ticks", 0) or get_asset_profile(tick.symbol).get("cooldown_sec", 0) or 0)
        _cd = _profile_cd if _profile_cd > 0 else _global_cd
        _sig_last = self._signal_cooldown.get(tick.symbol)
        if _sig_last is not None:
            _sig_elapsed_ticks = self._risk.get_tick_count(tick.symbol) - _sig_last
            if _sig_elapsed_ticks < _cd:
                _LOGGER.debug(
                    "[PIPELINE] COOLDOWN_ACTIVE %s elapsed=%d / %d ticks",
                    tick.symbol, _sig_elapsed_ticks, _cd,
                )
                self._spike_enrich(tick.symbol, bot_entered=False, block_reason="signal_cooldown")
                return

        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 2 — MATH: pure deterministic evaluation (Hurst + SMC + ATR).
        # AI confidence is intentionally NOT passed here so the risk engine
        # evaluates the full mathematical microstructure independently.
        # ═══════════════════════════════════════════════════════════════════
        spread_pct = float(tick.metrics.get("spread") or 0.0)
        pre_analysis = self._analyst.get_history_summary().get(tick.symbol) or {}
        # Use None when hurst hasn't been computed yet (e.g. cold-start or stale
        # analyst cache).  Defaulting to 0.5 would put the tick squarely in the
        # random-walk zone and trigger a false RANDOM_WALK_PREFILTER veto.
        _raw_hurst = pre_analysis.get("hurst")
        pre_hurst  = float(_raw_hurst) if _raw_hurst not in (None, 0) else None
        pre_autocorr = float(pre_analysis.get("autocorr_lag1") or 0.0)

        # ── Random-Walk pre-filter (R_* only) — regime-split logic ─────────
        # Classifies the Hurst zone and decides:
        #   H < 0.45  (mean_reverting) → allow, enforce score ≥ 9.0 downstream
        #   H ∈ [0.45,0.55] (random_walk) → veto (noise zone, no edge)
        #   H > 0.55  (trending)       → allow normally
        # Avoids wasted LLM tokens on R_* stuck in the noise band.
        _rw_info = self._risk.check_random_walk_prefilter(tick.symbol, pre_hurst)
        if _rw_info is not None:
            _rw_regime = _rw_info.get("regime", "random_walk")
            _rw_block  = bool(_rw_info.get("block", True))
            _LOGGER.info(
                "[PREFILTER_REGIME_SPLIT] symbol=%s H=%s regime=%s action=%s",
                tick.symbol,
                f"{pre_hurst:.3f}" if pre_hurst is not None else "?",
                _rw_regime,
                "block" if _rw_block else "allow",
            )
            if _rw_block:
                self._log_entry_block(
                    tick.symbol, "RANDOM_WALK_PREFILTER",
                    score=0.0, effective_min_score=0.0,
                    side=None, regime="random_walk",
                    hurst=pre_hurst if pre_hurst is not None else 0.5,
                )
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=None,
                    score=0.0,
                    reason=(
                        f"RANDOM_WALK_PREFILTER: H={pre_hurst:.3f} ∈ [0.45,0.55]"
                        if pre_hurst is not None
                        else "RANDOM_WALK_PREFILTER: hurst=?"
                    ),
                    extra={"prefilter": _rw_info, "hurst": pre_hurst},
                )
                return
            # mean_reverting or trending — pass through; remember regime for
            # the downstream score floor (mean_reverting requires score ≥ 9.0).
        else:
            _rw_regime = None

        # Fallback: when hurst was None we skipped the prefilter; use 0.5 only
        # for the downstream evaluate() call so it has a numeric value.
        _eval_hurst = pre_hurst if pre_hurst is not None else 0.5

        snap = self._risk.evaluate(
            tick.symbol, spread_pct,
            hurst=_eval_hurst,
            autocorr_lag1=pre_autocorr,
            dynamic_cfg=_dyn_cfg,
        )
        # Cache state for market context telemetry (written on next 60-tick cycle).
        _sb_now = snap.score_breakdown if isinstance(snap.score_breakdown, dict) else {}
        _cache_sym = tick.symbol.upper()
        if _cache_sym not in self._last_fvg_state:
            self._last_fvg_state[_cache_sym] = {}
        if "fvg_active" in _sb_now:
            self._last_fvg_state[_cache_sym].update({
                "fvg_active":    bool(_sb_now.get("fvg_active")),
                "fvg_direction": str(_sb_now.get("fvg_direction") or ""),
                "fvg_mid":       _sb_now.get("fvg_mid"),
                "fvg_top":       _sb_now.get("fvg_top"),
                "fvg_bottom":    _sb_now.get("fvg_bottom"),
                "smc_bonus":     float(_sb_now.get("smc_bonus") or 0.0),
            })
        # Inject burst/kinetic surface fields from fvg_state into score_breakdown
        # so downstream gates and the LLM can read them without extra lookups.
        _burst_state_now = self._last_fvg_state.get(_cache_sym, {})
        _burst_ret_val = _burst_state_now.get("burst_retroceso")
        _burst_active_val = bool(_burst_state_now.get("burst_active", False))
        if _burst_ret_val is not None:
            snap.score_breakdown["burst_retroceso"] = _burst_ret_val
            snap.score_breakdown["burst_active"]    = _burst_active_val

        # Cache entry quality fields for operator console indicators.
        # setup_type and fvg_tier always reflect current tick state; others are
        # preserved when None (hard-veto paths skip full scoring, carrying forward
        # the last computed value is more informative than overwriting with None).
        _qfields_always = {
            "setup_type":  _sb_now.get("setup_type"),
            "fvg_tier":    _sb_now.get("fvg_tier"),
        }
        self._last_fvg_state[_cache_sym].update(_qfields_always)
        _qfields_preserve = {
            "execution_grade":       _sb_now.get("execution_grade"),
            "geo_channel_pos":       _sb_now.get("geo_channel_pos"),
            "score_raw":             _sb_now.get("score_raw"),
        }
        self._last_fvg_state[_cache_sym].update(
            {k: v for k, v in _qfields_preserve.items() if v is not None}
        )
        # Eagerly cache scarcity/imminence so market_context always shows live data
        # regardless of which early-return gate fires first.  The late-stage cache
        # at line ~3807 still runs for ticks that pass all gates and adds refinements,
        # but this block guarantees non-null values after the first detected spike.
        _is_bc_early = any(k in tick.symbol.upper() for k in ("BOOM", "CRASH"))
        if _is_bc_early:
            try:
                _imm_tel = self._risk.get_spike_imminence_state(tick.symbol)
                _imm_s = str(_imm_tel.get("state") or "") or None
                if _imm_s:
                    self._last_fvg_state[_cache_sym]["spike_imminence_state"] = _imm_s
                    self._last_fvg_state[_cache_sym]["spike_imminence_score"] = round(
                        float(_imm_tel.get("score") or 0.0), 3
                    )
            except Exception:
                pass
            try:
                _scar_tel = self._risk.get_scarcity_state(tick.symbol)
                _scar_s = str(_scar_tel.get("state") or "") if isinstance(_scar_tel, dict) else ""
                if _scar_s and _scar_s != "SIN_DATOS":
                    self._last_fvg_state[_cache_sym]["scarcity_state"] = _scar_s
                    if isinstance(_scar_tel, dict) and _scar_tel.get("ratio") is not None:
                        self._last_fvg_state[_cache_sym]["scarcity_ratio"] = _scar_tel.get("ratio")
            except Exception:
                pass

        # ── BUG-C fix (2026-05-19 phase13): Geo channel position gate runs here ──
        # Moved from its original location (after Hurst/Strategy gates) so that
        # geo_gate is present in score_breakdown for ALL _log_entry_block calls.
        # Previously, geo_gate was computed too late → every ENTRY_BLOCKED log
        # showed geo=n/a even when geo_channel_pos was calculated.
        # Running it early also correctly applies the geo score modifier before
        # the regime/profile/Hurst score gates instead of after them.
        _profile_min_score_early = min_score_for(tick.symbol)
        _asset_profile_early = get_asset_profile(tick.symbol)
        _geo_pos = snap.score_breakdown.get("geo_channel_pos")
        if _geo_pos is not None:
            _geo_val  = float(_geo_pos)
            _geo_gate = 0.0
            _geo_gate_label = ""
            # 2026-06-01 DIRECTION-AWARE GEO GATE for spike markets.
            # ───────────────────────────────────────────────────────────────
            # BUG (confirmed): the old min/max gate penalised geo_pos>geo_entry_max
            # with -2.0. CRASH symbols (geo_entry_max=0.30) live at geo_pos 0.7-1.2
            # → -2.0 every tick → symbol dead. But that was mean-reversion logic
            # mis-applied to forced-direction spike entries.
            # TRADING REALITY:
            #   CRASH (MULTDOWN): price DRIFTS UP between crashes, then spikes DOWN.
            #     High geo_pos = price extended UP in its channel = LOADED for the
            #     down-spike = favourable, NOT a disqualifier. The exhaustion side
            #     is geo_pos very NEGATIVE (the crash ALREADY fired → late entry).
            #   BOOM (MULTUP): mirror — price drifts DOWN, spikes UP. Low geo_pos =
            #     loaded; the exhaustion side is geo_pos very POSITIVE.
            # So we penalise ONLY the side OPPOSITE the entry direction.
            _geo_dir_aware = str(
                os.getenv("DERIV_GEO_DIR_AWARE_SPIKE", "true") or "true"
            ).strip().lower() in {"1", "true", "yes", "on"}
            _is_spike_geo = is_spike_market(tick.symbol)
            _entry_dir = str(
                _asset_profile_early.get("forced_side") or snap.side or ""
            ).upper()
            if _geo_dir_aware and _is_spike_geo and _entry_dir in ("MULTUP", "MULTDOWN"):
                # Exhaustion thresholds on the WRONG side of the entry.
                _geo_late   = float(os.getenv("DERIV_GEO_SPIKE_LATE_THRESHOLD", "0.80") or 0.80)
                _geo_exhaust = float(os.getenv("DERIV_GEO_SPIKE_EXHAUST_THRESHOLD", "1.20") or 1.20)
                # Optional mild bonus on the loaded side (default 0.0 = neutral,
                # to avoid double-counting with the imminence bonus).
                _geo_loaded_bonus = float(os.getenv("DERIV_GEO_SPIKE_LOADED_BONUS", "0.0") or 0.0)
                _geo_loaded_min   = float(os.getenv("DERIV_GEO_SPIKE_LOADED_MIN", "0.80") or 0.80)
                if _entry_dir == "MULTDOWN":
                    # CRASH: penalise extended-DOWN (crash already fired = late).
                    if _geo_val <= -_geo_exhaust:
                        _geo_gate = -2.0
                        _geo_gate_label = (
                            f"geo_spike_exhausted_down: {_geo_val:.3f}≤-{_geo_exhaust:.2f} "
                            f"(crash already fired) →-2.0"
                        )
                    elif _geo_val <= -_geo_late:
                        _geo_gate = -1.0
                        _geo_gate_label = (
                            f"geo_spike_late_down: {_geo_val:.3f}≤-{_geo_late:.2f} →-1.0"
                        )
                    elif _geo_loaded_bonus > 0.0 and _geo_val >= _geo_loaded_min:
                        _geo_gate = _geo_loaded_bonus
                        _geo_gate_label = (
                            f"geo_spike_loaded_down: {_geo_val:.3f}≥{_geo_loaded_min:.2f} "
                            f"(extended up, crash loaded) →+{_geo_loaded_bonus:.2f}"
                        )
                    else:
                        _geo_gate = 0.0
                        _geo_gate_label = f"geo_spike_neutral_down: {_geo_val:.3f} →0.0"
                else:  # MULTUP — BOOM
                    if _geo_val >= _geo_exhaust:
                        _geo_gate = -2.0
                        _geo_gate_label = (
                            f"geo_spike_exhausted_up: {_geo_val:.3f}≥{_geo_exhaust:.2f} "
                            f"(boom already fired) →-2.0"
                        )
                    elif _geo_val >= _geo_late:
                        _geo_gate = -1.0
                        _geo_gate_label = (
                            f"geo_spike_late_up: {_geo_val:.3f}≥{_geo_late:.2f} →-1.0"
                        )
                    elif _geo_loaded_bonus > 0.0 and _geo_val <= -_geo_loaded_min:
                        _geo_gate = _geo_loaded_bonus
                        _geo_gate_label = (
                            f"geo_spike_loaded_up: {_geo_val:.3f}≤-{_geo_loaded_min:.2f} "
                            f"(extended down, boom loaded) →+{_geo_loaded_bonus:.2f}"
                        )
                    else:
                        _geo_gate = 0.0
                        _geo_gate_label = f"geo_spike_neutral_up: {_geo_val:.3f} →0.0"
                snap.score_breakdown["geo_dir_aware"] = _entry_dir
            else:
                # ── Legacy min/max gate (R_* and any non-directional symbol) ──
                _geo_min  = _asset_profile_early.get("geo_entry_min")
                _geo_max  = _asset_profile_early.get("geo_entry_max")
                # Per-symbol geo tolerance: soft→hard penalty boundary (default 0.30).
                # CRASH500 uses 0.25 to tighten overshoot zone (Muestra3).
                _geo_tol = float(_asset_profile_early.get("geo_penalty_tolerance", 0.30))
                # Per-symbol extended-down veto: when geo_pos < this floor, CRASH entries
                # that would normally receive geo_optimal +1.0 bonus are hard-vetoed (-2.0).
                _geo_veto_min = _asset_profile_early.get("geo_extended_veto_min")

                if _geo_max is not None:
                    _gmax     = float(_geo_max)
                    _overshoot = _geo_val - _gmax
                    if _geo_veto_min is not None and _geo_val < float(_geo_veto_min):
                        _geo_gate = -2.0
                        _geo_gate_label = (
                            f"geo_extended_down_veto: {_geo_val:.3f}<{float(_geo_veto_min):.3f} →-2.0"
                        )
                    elif _overshoot <= -0.30:
                        _geo_gate = +1.0
                        _geo_gate_label = f"geo_optimal: {_geo_val:.3f}≤{_gmax-0.30:.3f} →+1.0"
                    elif _overshoot <= 0.0:
                        _geo_gate = 0.0
                        _geo_gate_label = f"geo_border: {_geo_val:.3f}≤{_gmax:.3f} →0.0"
                    elif _overshoot <= _geo_tol:
                        _geo_gate = -1.5
                        _geo_gate_label = (
                            f"geo_penalty: {_geo_val:.3f}>{_gmax:.3f} "
                            f"(overshoot={_overshoot:.2f}) →-1.5"
                        )
                    else:
                        _geo_gate = -2.0
                        _geo_gate_label = (
                            f"geo_hard_penalty: {_geo_val:.3f}>>{_gmax:.3f} "
                            f"(overshoot={_overshoot:.2f}) →-2.0"
                        )

                elif _geo_min is not None:
                    _gmin       = float(_geo_min)
                    _undershoot = _gmin - _geo_val
                    if _undershoot <= -0.30:
                        _geo_gate = +1.0
                        _geo_gate_label = f"geo_optimal: {_geo_val:.3f}≥{_gmin+0.30:.3f} →+1.0"
                    elif _undershoot <= 0.0:
                        _geo_gate = 0.0
                        _geo_gate_label = f"geo_border: {_geo_val:.3f}≥{_gmin:.3f} →0.0"
                    elif _undershoot <= _geo_tol:
                        _geo_gate = -1.5
                        _geo_gate_label = (
                            f"geo_penalty: {_geo_val:.3f}<{_gmin:.3f} "
                            f"(undershoot={_undershoot:.2f}) →-1.5"
                        )
                    else:
                        _geo_gate = -2.0
                        _geo_gate_label = (
                            f"geo_hard_penalty: {_geo_val:.3f}<<{_gmin:.3f} "
                            f"(undershoot={_undershoot:.2f}) →-2.0"
                        )

            snap.score_breakdown["geo_gate"] = round(_geo_gate, 1)
            if _geo_gate != 0.0:
                snap.score = round(min(10.0, max(0.0, snap.score + _geo_gate)), 3)
                snap.reasons.append(_geo_gate_label)
                _LOGGER.info(
                    "[PIPELINE] GEO_GATE %s | %s | score→%.2f",
                    tick.symbol, _geo_gate_label, snap.score,
                )

        # ── 2026-06-01 SNIPER "LOADED" GATE (data-mined on 1240 CRASH trades) ──
        # Winners entered LOADED for the forced spike direction: price elevated
        # over EMA200 (ema200_dev_pct ↑) and — for CRASH500 — geo_channel_pos ↑.
        # In-sample backtest: ema200_dev_pct>=0.008 flips combined PnL -27→-2.4
        # (CRASH600 +12.2, CRASH900 +4.2); CRASH500 also needs geo>=0.10.
        # Hard-block entries NOT loaded in the entry direction → cuts the
        # never-green tail (the ~half of trades that never touch green).
        # All thresholds env-driven; per-symbol geo override supported.
        if (
            str(os.getenv("DERIV_SPIKE_LOADED_GATE", "true") or "true").strip().lower()
            in {"1", "true", "yes", "on"}
            and is_spike_market(tick.symbol)
        ):
            _ld_dir = str(
                get_asset_profile(tick.symbol).get("forced_side") or snap.side or ""
            ).upper()
            if _ld_dir in ("MULTUP", "MULTDOWN"):
                _ld_sym = tick.symbol.upper()
                _ld_min_dev = float(
                    os.getenv("DERIV_SPIKE_LOADED_MIN_DEV_PCT", "0.008") or 0.008
                )
                _ld_min_geo = float(
                    os.getenv(
                        f"DERIV_SPIKE_LOADED_MIN_GEO_{_ld_sym}",
                        os.getenv("DERIV_SPIKE_LOADED_MIN_GEO", "-999") or "-999",
                    )
                    or "-999"
                )
                _ld_sb = snap.score_breakdown
                _ld_dev = _ld_sb.get("ema200_dev_pct")
                if _ld_dev is None and _ld_sb.get("ema200_distance_pct") is not None:
                    _ld_dev = float(_ld_sb["ema200_distance_pct"]) * 100.0
                _ld_geo = _ld_sb.get("geo_channel_pos")
                _ld_ok = True
                _ld_reason = ""
                if _ld_dir == "MULTDOWN":  # CRASH: loaded = price ABOVE ema200, geo ↑
                    if (
                        _ld_min_dev > 0
                        and _ld_dev is not None
                        and float(_ld_dev) < _ld_min_dev
                    ):
                        _ld_ok = False
                        _ld_reason = (
                            f"ema200_dev={float(_ld_dev):.4f}<{_ld_min_dev:.4f} "
                            f"(not loaded up for CRASH)"
                        )
                    elif _ld_geo is not None and float(_ld_geo) < _ld_min_geo:
                        _ld_ok = False
                        _ld_reason = (
                            f"geo={float(_ld_geo):.3f}<{_ld_min_geo:.3f} (not loaded)"
                        )
                else:  # MULTUP — BOOM mirror: loaded = price BELOW ema200, geo ↓
                    if (
                        _ld_min_dev > 0
                        and _ld_dev is not None
                        and float(_ld_dev) > -_ld_min_dev
                    ):
                        _ld_ok = False
                        _ld_reason = (
                            f"ema200_dev={float(_ld_dev):.4f}>-{_ld_min_dev:.4f} "
                            f"(not loaded down for BOOM)"
                        )
                    elif _ld_geo is not None and float(_ld_geo) > -_ld_min_geo:
                        _ld_ok = False
                        _ld_reason = (
                            f"geo={float(_ld_geo):.3f}>-{_ld_min_geo:.3f} (not loaded)"
                        )
                snap.score_breakdown["spike_loaded_ok"] = _ld_ok
                if not _ld_ok:
                    self._log_entry_block(
                        tick.symbol, "SPIKE_NOT_LOADED",
                        score=snap.score, side=snap.side,
                        regime=snap.regime,
                        score_breakdown=snap.score_breakdown,
                    )
                    self._record_decision(
                        symbol=tick.symbol, allowed=False, side=snap.side,
                        score=snap.score,
                        reason=f"SPIKE_NOT_LOADED: {_ld_reason}",
                        extra={"spike_loaded": False},
                    )
                    _gl_setup = str(snap.score_breakdown.get("setup_type") or "")
                    _gl_grade = str(snap.score_breakdown.get("execution_grade") or "")
                    if _gl_setup in ("SMC_FVG", "EMA200_SPIKE") and _gl_grade in ("A", "B") and snap.score >= 7.0:
                        self._ghost_logger.add(
                            symbol=tick.symbol, price=float(tick.price), side=str(snap.side or ""),
                            score_raw=float(snap.score), gate="SPIKE_NOT_LOADED",
                            setup_type=_gl_setup, grade=_gl_grade,
                            scarcity=str(snap.score_breakdown.get("scarcity_state") or ""),
                            imm_state=str(snap.score_breakdown.get("spike_imminence_state") or ""),
                            imm_score=float(snap.score_breakdown.get("spike_imminence_score") or 0.0),
                        )
                    return


        # ── ATR volatility filter (BOOM500/1000 + CRASH500/1000) ──────────
        # Reject entries during low-volume sessions where the spike accumulation
        # will not have enough fuel. Uses live ATR vs the rolling 24h ATR history
        # maintained by DerivRiskManager.
        # EXCEPTION: if the risk engine already bypassed the ATR gate for a
        # spike market in calm regime (atr_calm_bypassed=True), we must NOT
        # re-apply the filter here — that would create a phantom double-gate.
        #
        # T2 FIX 2026-05-19: Lower percentile threshold from 40→28 (−30%) for
        # borderline-high-quality setups (score > 5.50) that are NOT already
        # atr_calm_bypassed.  BOOM/CRASH naturally compress ATR between spikes;
        # p40 was too aggressive for otherwise-valid institutional setups.
        _atr_calm_bypassed = bool(snap.score_breakdown.get("atr_calm_bypassed", False))
        _atr_current = float(snap.score_breakdown.get("atr_abs") or 0.0)
        _atr_hist = self._risk._atr_history.get(tick.symbol, [])
        # ATR percentile threshold — configurable via env vars:
        #   DERIV_ATR_PERCENTILE_THRESHOLD      base threshold (default 40)
        #   DERIV_ATR_PERCENTILE_CALM           calm-regime threshold (default 28 = 70% of base)
        # Lowering in calm is justified because BOOM/CRASH naturally compress
        # ATR between spikes; the default p40 was blocking valid setups.
        _atr_base_th = int(os.getenv("DERIV_ATR_PERCENTILE_THRESHOLD", "40"))
        _atr_calm_th = int(os.getenv("DERIV_ATR_PERCENTILE_CALM", str(max(10, round(_atr_base_th * 0.70)))))
        # ── Per-symbol ATR percentile overrides ──────────────────────────────
        # Spike markets naturally compress ATR between spikes; the default p40
        # is too aggressive for otherwise-valid institutional setups.
        # Per-symbol thresholds below the global calm threshold are configured
        # per telemetry of missed entries (19/5 diagnostic).
        # Also configurable via DERIV_ATR_TH_{SYMBOL} env vars for live tuning.
        _sym_upper = tick.symbol.upper()
        _per_sym_atr_defaults = {
            "BOOM600":  32,
            "BOOM900":  30,
            "BOOM1000": 20,   # Phase 26: was 30 — atr_abs=0.012 vs p30=0.013 (2-5% gap blocks valid entries)
            "CRASH500": 15,   # Phase 27: was 28 — no pipeline logs in 5000 lines; ATR too high for current calm
            "CRASH900": 20,   # Phase 32: atr_abs=0.022034 vs p40=0.022517 — blocked by 2.2%; p20 fixes this
            "CRASH1000": 20,  # Phase 32: align with BOOM1000; lower ATR gate to match actual market conditions
        }
        _env_sym_th = os.getenv(f"DERIV_ATR_TH_{_sym_upper}")
        if _env_sym_th and _env_sym_th.isdigit():
            _atr_threshold = int(_env_sym_th)
        elif _sym_upper in _per_sym_atr_defaults:
            _atr_threshold = _per_sym_atr_defaults[_sym_upper]
            # Phase 15 — score-based relaxation for BOOM/CRASH: when the score is
            # clearly above effective_min (≥ +0.50 margin), lower the ATR percentile
            # by 12pp (e.g. p30 → p18) to avoid blocking valid setups that are just
            # 5–10% below the ATR median on an otherwise strong signal.
            _is_bc_sym = any(k in _sym_upper for k in ("BOOM", "CRASH"))
            _eff_min = float(snap.effective_min_score or self._settings.min_score)
            if _is_bc_sym and snap.score >= (_eff_min + 0.50):
                _relaxed_th = max(15, _atr_threshold - 12)
                _LOGGER.info(
                    "[ATR_FILTER] %s score_boost_relax: p%d → p%d "
                    "(score=%.2f eff_min=%.2f margin=%.2f)",
                    tick.symbol, _atr_threshold, _relaxed_th,
                    snap.score, _eff_min, snap.score - _eff_min,
                )
                _atr_threshold = _relaxed_th
        else:
            _atr_threshold = _atr_calm_th if (snap.score > 5.50 and not _atr_calm_bypassed) else _atr_base_th
        _atr_ok, _atr_reason = passes_atr_volatility_filter(
            tick.symbol, _atr_current, _atr_hist,
            percentile_threshold=_atr_threshold,
        )
        # [ATR_FILTER_DETAIL] — always emit so exact values are visible in logs
        _atr_p_req = 0.0
        _sorted_atr = sorted(_atr_hist) if len(_atr_hist) >= 10 else []
        if _sorted_atr:
            _p_idx = max(0, min(len(_sorted_atr) - 1,
                                int(len(_sorted_atr) * _atr_threshold / 100)))
            _atr_p_req = _sorted_atr[_p_idx]
        _LOGGER.info(
            "[ATR_FILTER_DETAIL] %s atr_abs=%.6f required_p%d=%.6f pass=%s "
            "score=%.2f atr_bypassed=%s hist_n=%d",
            tick.symbol, _atr_current, _atr_threshold, _atr_p_req, _atr_ok,
            snap.score, _atr_calm_bypassed, len(_atr_hist),
        )
        _atr_hot_bypass = (
            bool(snap.score_breakdown.get("hot_reentry_fired"))
            and float(snap.score or 0.0) >= float(
                os.getenv("DERIV_ATR_HOT_BYPASS_MIN_SCORE", "8.0") or "8.0"
            )
        )
        if _atr_hot_bypass:
            _LOGGER.info(
                "[ATR_FILTER] HOT_BYPASS %s | score=%.2f atr_abs=%.6f "
                "hot_reentry_active → skip ATR check",
                tick.symbol, float(snap.score or 0.0), _atr_current,
            )
        # MASTER_KEY bypass: score ≥ 8.5 (MASTER_KEY confirmed) means all other
        # gates already validated this entry — ATR percentile must not veto it.
        _atr_master_key_min = float(
            os.getenv("DERIV_ATR_MASTER_KEY_BYPASS_MIN_SCORE", "8.5") or "8.5"
        )
        _atr_master_bypass = (
            is_spike_market(tick.symbol)
            and float(snap.score or 0.0) >= _atr_master_key_min
        )
        if _atr_master_bypass:
            _LOGGER.info(
                "[ATR_FILTER] MASTER_KEY_BYPASS %s | score=%.2f≥%.2f atr_abs=%.6f "
                "required_p%d=%.6f → ATR gate bypassed (high-conviction entry)",
                tick.symbol, float(snap.score or 0.0), _atr_master_key_min,
                _atr_current, _atr_threshold, _atr_p_req,
            )
        if not _atr_ok and not _atr_calm_bypassed and not _atr_hot_bypass and not _atr_master_bypass:
            self._log_entry_block(
                tick.symbol, "ATR_VOLATILITY_FILTER",
                score=snap.score, effective_min_score=snap.effective_min_score,
                side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                score_breakdown=snap.score_breakdown,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score, reason=_atr_reason,
                extra={"atr_current": _atr_current, "atr_hist_n": len(_atr_hist)},
            )
            return

        # ── Regime-aware min_score gate — delegate entirely to risk engine ──
        # snap.effective_min_score is already the authoritative threshold computed
        # by DerivRiskManager (applies calm-floor 5.80, DERIV_CALM_STRUCTURAL_MIN_SCORE,
        # spike-market overrides, etc.).  We must NOT shadow it with a second call
        # to min_score_for_regime() which returns a stale hardcoded 7.50.
        _dyn_score_min_raw = max(
            self._dynamic_score_min_guardrail,
            min(
                self._dynamic_score_max_guardrail,
                float(_dyn_cfg.get("score_min_override") or min_score_for(tick.symbol)),
            ),
        )
        _dyn_cfg_regime = str(_dyn_cfg.get("market_regime") or snap.regime or "NORMAL").upper()
        _risk_regime = str(snap.regime or "").strip().lower()
        _dyn_score_ceiling = self._dynamic_gate_neutral_ceiling
        _dyn_score_ceiling_source = "default"
        if _dyn_cfg_regime == "FAST":
            _dyn_score_ceiling = self._dynamic_gate_fast_ceiling
            _dyn_score_ceiling_source = "cfg:FAST"
        elif _dyn_cfg_regime == "SLOW":
            _dyn_score_ceiling = self._dynamic_gate_slow_ceiling
            _dyn_score_ceiling_source = "cfg:SLOW"
        elif _risk_regime == "trending":
            _dyn_score_ceiling = self._dynamic_gate_trending_ceiling
            _dyn_score_ceiling_source = "risk:trending"
        elif _risk_regime == "calm":
            _dyn_score_ceiling = self._dynamic_gate_calm_ceiling
            _dyn_score_ceiling_source = "risk:calm"

        _dyn_score_min = min(_dyn_score_min_raw, _dyn_score_ceiling)
        if _dyn_cfg_regime == "FAST":
            _dyn_score_min = max(_dyn_score_min, self._dynamic_gate_fast_floor)

        snap.score_breakdown["dynamic_cfg_active"] = _dyn_active
        snap.score_breakdown["dynamic_inactive_bypassed"] = _dyn_inactive_bypassed
        snap.score_breakdown["dynamic_score_raw"] = round(_dyn_score_min_raw, 3)
        snap.score_breakdown["dynamic_score_ceiling"] = round(_dyn_score_ceiling, 3)
        snap.score_breakdown["dynamic_score_ceiling_source"] = _dyn_score_ceiling_source
        snap.score_breakdown["dynamic_score_ceiling_trending"] = round(self._dynamic_gate_trending_ceiling, 3)
        snap.score_breakdown["dynamic_score_ceiling_fast"] = round(self._dynamic_gate_fast_ceiling, 3)
        snap.score_breakdown["dynamic_score_ceiling_calm"] = round(self._dynamic_gate_calm_ceiling, 3)
        snap.score_breakdown["dynamic_score_ceiling_slow"] = round(self._dynamic_gate_slow_ceiling, 3)
        snap.score_breakdown["dynamic_fast_floor"] = round(self._dynamic_gate_fast_floor, 3)
        snap.score_breakdown["dynamic_cfg_regime"] = _dyn_cfg_regime
        snap.score_breakdown["dynamic_score_min"] = round(_dyn_score_min, 3)
        _regime_min = _dyn_score_min if _dyn_active else snap.effective_min_score

        # ── Per-symbol PnL-driven score escalator ─────────────────────────
        # When a symbol is bleeding (rolling realized PnL ≤ threshold), the
        # risk engine raises a per-symbol score floor bonus that we add on
        # top of the regime gate so future entries on that symbol must be
        # noticeably stronger (≥ 7.5–8.0 depending on escalation steps).
        _sym_bleed_bonus = float(self._risk.symbol_score_floor_bonus(tick.symbol))
        _sym_bleed_pnl_window = float(self._risk.symbol_pnl_window(tick.symbol))
        snap.score_breakdown["symbol_bleed_bonus"] = round(_sym_bleed_bonus, 3)
        snap.score_breakdown["symbol_bleed_pnl_window"] = round(_sym_bleed_pnl_window, 3)
        snap.score_breakdown["symbol_bleed_threshold"] = round(
            float(self._risk.symbol_negative_threshold()), 3
        )
        if _sym_bleed_bonus > 0.0:
            _regime_min = min(10.0, _regime_min + _sym_bleed_bonus)

        # ── CASCADE momentum path ──────────────────────────────────────────────
        # When cascade_active (burst_depth≥2, no retroceso, gap<60t), the price
        # is in confirmed free-fall momentum — not the retroceso-recovery pattern.
        # DERIV_CASCADE_ENTRY_ENABLED=false by default; enable explicitly to trade cascades.
        # When enabled: lower regime_min by CASCADE_MIN_RELIEF (default 0.5).
        _is_bc_bias = any(k in tick.symbol.upper() for k in ("BOOM", "CRASH"))
        _casc_fvg_data = self._last_fvg_state.get(tick.symbol.upper(), {})
        _casc_active = bool(_casc_fvg_data.get("cascade_active", False))
        _casc_entry_en = (
            _is_bc_bias
            and str(os.getenv("DERIV_CASCADE_ENTRY_ENABLED", "false") or "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        snap.score_breakdown["cascade_active"] = _casc_active
        if _casc_active and _casc_entry_en:
            _casc_min_relief = float(os.getenv("DERIV_CASCADE_MIN_RELIEF", "0.5") or 0.5)
            _regime_min = max(0.0, _regime_min - _casc_min_relief)
            snap.score_breakdown["cascade_min_relief"] = round(_casc_min_relief, 2)

        # ── 2026-06-01 REGIME EDGE BIAS (full-history forensic, 439 trades) ──
        # TRENDING regime = -$38.59 realized (wr 32%); CALM = +$0.30 (wr 33%).
        # Per-symbol confirms across both closed contracts AND the LLM pattern
        # memory (n>700): BOOM500 CALM wr=64-65% vs TRENDING 31-43%; CRASH500
        # CALM 64% vs TRENDING 44%; CRASH600 CALM win vs TRENDING -$13. The
        # bot's #1 leak is entering during TRENDING. → raise the required score
        # hard in TRENDING (only A++ setups survive) and keep CALM permissive.
        _risk_regime_bias = str(snap.regime or "").strip().lower()
        # Compute spike imminence ONCE here so it can both (a) modulate the
        # trending penalty below and (b) feed the score bonus further down.
        _imm = {}
        if _is_bc_bias:
            try:
                _imm = self._risk.get_spike_imminence_state(tick.symbol)
            except Exception:
                _imm = {}
        _imm_state_early = str(_imm.get("state") or "")
        if _is_bc_bias:
            if _risk_regime_bias == "trending":
                _trend_pen = max(
                    0.0,
                    float(os.getenv("DERIV_REGIME_TRENDING_SCORE_PENALTY", "1.3") or 1.3),
                )
                # 2026-06-01 TRADING INTELLIGENCE: a TRENDING symbol that is
                # statistically LOADED to fire (imminence RIPE/BUILDING) is the
                # good case (normal drift right before the spike), not the
                # losing case. Soften the penalty so a loaded spike can enter;
                # keep the full penalty when NOT loaded (FRESH/OVERDUE/DRY =
                # the trending trap that bled -$38.59).
                _pen_mult = 1.0
                if _imm_state_early == "RIPE":
                    _pen_mult = float(os.getenv("DERIV_REGIME_TREND_PEN_RIPE_MULT", "0.25") or 0.25)
                elif _imm_state_early == "BUILDING":
                    _pen_mult = float(os.getenv("DERIV_REGIME_TREND_PEN_BUILDING_MULT", "0.60") or 0.60)
                # When spike is statistically overdue (Z2/SECO scarcity), the full
                # trending penalty is double-counting danger already handled by Z2
                # elasticity. Soften it so a high-pressure spike can enter.
                _scarcity_ratio_early = float(snap.score_breakdown.get("scarcity_ratio") or 0.0)
                _el_z2_early = float(os.getenv("DERIV_ELASTIC_Z2", "2.0") or 2.0)
                if _pen_mult == 1.0 and _scarcity_ratio_early >= _el_z2_early:
                    _pen_mult = float(os.getenv("DERIV_REGIME_TREND_PEN_SCARCE_MULT", "0.25") or 0.25)
                _trend_pen_eff = round(_trend_pen * max(0.0, min(1.0, _pen_mult)), 3)
                if _trend_pen_eff > 0.0:
                    _regime_min = min(10.0, _regime_min + _trend_pen_eff)
                    snap.score_breakdown["regime_trending_penalty"] = _trend_pen_eff
                    snap.score_breakdown["regime_trending_penalty_base"] = round(_trend_pen, 3)
                    if _imm_state_early:
                        snap.score_breakdown["regime_trending_imm_state"] = _imm_state_early
            elif _risk_regime_bias == "calm":
                _calm_bon = max(
                    0.0,
                    float(os.getenv("DERIV_REGIME_CALM_SCORE_BONUS", "0.3") or 0.3),
                )
                if _calm_bon > 0.0:
                    _regime_min = max(0.0, _regime_min - _calm_bon)
                    snap.score_breakdown["regime_calm_bonus"] = round(_calm_bon, 3)

        # ── 2026-06-01 SPIKE-CLUSTER ALIGNMENT BONUS ("stars aligned") ──────
        # Same 1102-spike forensic: >=3 spikes in last 300s → next spike median
        # 57-76s away (4-8x faster than normal). Grant a score bonus so a
        # borderline A-/B+ setup can pass the gate to position for the 4th
        # spike. Scales mildly with cluster size (cap 2x). BOOM/CRASH only.
        if _is_bc_bias:
            _cl_win2 = float(os.getenv("DERIV_SPIKE_CLUSTER_WINDOW_SEC", "300") or 300)
            _cl_min2 = int(float(os.getenv("DERIV_SPIKE_CLUSTER_MIN_SPIKES", "3") or 3))
            try:
                _cl_n2 = int(self._risk.get_spike_count_recent(tick.symbol, _cl_win2))
            except Exception:
                _cl_n2 = 0
            # 2026-06-01 ANTI-BUTTERFLY recency gate (user directive): the
            # cluster bonus represents an IMMINENT 4th spike ONLY while the last
            # spike is recent. Once the burst has been quiet for > FRESH_TICKS
            # the spikes "ya salieron" and this bonus would just be chasing
            # butterflies. Drop it and let the forward-looking imminence
            # predictor (which self-resets to FRESH after each spike) decide.
            _cl_fresh2 = float(os.getenv("DERIV_SPIKE_CLUSTER_FRESH_TICKS", "150") or 150)
            _cl_last_tick2 = int(self._risk.get_last_spike_tick_count(tick.symbol) or 0)
            try:
                _cl_since2 = max(
                    0.0,
                    float(self._risk.get_tick_count(tick.symbol) - _cl_last_tick2),
                )
            except Exception:
                _cl_since2 = 0.0
            _cl_fresh_ok2 = (_cl_last_tick2 > 0 and _cl_since2 <= _cl_fresh2)
            if _cl_n2 >= _cl_min2 and not _cl_fresh_ok2:
                snap.score_breakdown["spike_cluster_stale"] = round(_cl_since2, 0)
                _LOGGER.info(
                    "[PIPELINE] SPIKE_CLUSTER_STALE %s | n=%d/%ds since_last=%.0ft>%.0ft "
                    "→ no bonus (spikes already fired, waiting fresh setup)",
                    tick.symbol, _cl_n2, int(_cl_win2), _cl_since2, _cl_fresh2,
                )
            if _cl_n2 >= _cl_min2 and _cl_fresh_ok2:
                _cl_base = max(
                    0.0,
                    float(os.getenv("DERIV_SPIKE_CLUSTER_SCORE_BONUS", "1.0") or 1.0),
                )
                _cl_scale = min(2.0, 1.0 + 0.25 * (_cl_n2 - _cl_min2))
                _cl_boost = round(_cl_base * _cl_scale, 2)
                if _cl_boost > 0.0:
                    snap.score = round(min(10.0, snap.score + _cl_boost), 3)
                    snap.score_breakdown["spike_cluster_n"] = _cl_n2
                    snap.score_breakdown["spike_cluster_bonus"] = _cl_boost
                    snap.reasons.append(
                        f"spike_cluster_aligned: {_cl_n2} spikes/{_cl_win2:.0f}s → +{_cl_boost:.2f}"
                    )
                    _LOGGER.info(
                        "[PIPELINE] SPIKE_CLUSTER_ALIGNED %s | n=%d/%ds → +%.2f score→%.2f "
                        "(4th spike imminent ~57-76s)",
                        tick.symbol, _cl_n2, int(_cl_win2), _cl_boost, snap.score,
                    )

        # ── CASCADE score bonus ────────────────────────────────────────────────
        # Confirmed cascade momentum adds a score bonus to help pass the gate.
        # Only fires when DERIV_CASCADE_ENTRY_ENABLED=true.
        if _casc_active and _casc_entry_en and "cascade_bonus" not in snap.score_breakdown:
            _casc_bonus = float(os.getenv("DERIV_CASCADE_SCORE_BONUS", "1.0") or 1.0)
            _casc_gap = int(_casc_fvg_data.get("cascade_gap_ticks") or 0)
            _casc_depth = int(_casc_fvg_data.get("burst_depth") or 0)
            snap.score = round(min(10.0, snap.score + _casc_bonus), 3)
            snap.score_breakdown["cascade_bonus"] = round(_casc_bonus, 2)
            snap.score_breakdown["cascade_gap_ticks"] = _casc_gap
            snap.reasons.append(
                f"cascade_momentum: depth={_casc_depth} gap={_casc_gap}t → +{_casc_bonus:.1f}"
            )
            _LOGGER.info(
                "[PIPELINE] CASCADE_MOMENTUM %s | depth=%d gap=%dt → +%.1f score→%.2f",
                tick.symbol, _casc_depth, _casc_gap, _casc_bonus, snap.score,
            )

        # ── 2026-06-01 SPIKE-PACE / HOT-WINDOW AWARENESS ────────────────────
        # Forensic: ~50-60% of an hour's spikes land in one 20-min window, then
        # the market goes dry. CRASH500/600 deliver ~5-7/h, CRASH900 ~2-5/h,
        # BOOM500 is erratic (2-45/h). We track WHERE we are in that rhythm and
        # surface it to the LLM via score_breakdown + spike_density_memory.json.
        # During a HOT window we grant a small readiness bonus so the bot does
        # NOT miss the burst; during a COLD/dry zone we stay patient (the
        # sniper MAX-window + chase-guard already block dry-zone butterflies).
        if _is_bc_bias:
            try:
                _pace = self._risk.get_spike_pace_state(tick.symbol, 1200.0)
            except Exception:
                _pace = {}
            _pace_state = str(_pace.get("state") or "")
            if _pace_state:
                snap.score_breakdown["spike_pace_state"] = _pace_state
                snap.score_breakdown["spike_pace_ratio"] = _pace.get("ratio")
                snap.score_breakdown["spike_pace_20m"] = _pace.get("spikes_window")
                snap.score_breakdown["spike_pace_expected_20m"] = _pace.get("expected_window")
            if _pace_state == "HOT":
                _hot_bon = max(
                    0.0,
                    float(os.getenv("DERIV_SPIKE_PACE_HOT_BONUS", "0.5") or 0.5),
                )
                # Avoid double-rewarding when the tighter cluster bonus already fired.
                if _hot_bon > 0.0 and "spike_cluster_bonus" not in snap.score_breakdown:
                    snap.score = round(min(10.0, snap.score + _hot_bon), 3)
                    snap.score_breakdown["spike_pace_hot_bonus"] = round(_hot_bon, 3)
                    snap.reasons.append(
                        f"spike_pace_HOT: {_pace.get('spikes_window')}/20m "
                        f"(exp {_pace.get('expected_window')}) → +{_hot_bon:.2f}"
                    )
                    _LOGGER.info(
                        "[PIPELINE] SPIKE_PACE_HOT %s | %s/20m exp=%s ratio=%s → +%.2f score→%.2f",
                        tick.symbol, _pace.get("spikes_window"),
                        _pace.get("expected_window"), _pace.get("ratio"),
                        _hot_bon, snap.score,
                    )

        # ── MASTER KEY — pre-gate RNG preview ────────────────────────────────
        # Si score ≥ DERIV_MASTER_KEY_SCORE (default 6.25) Y rng_prob ≥
        # DERIV_MASTER_KEY_RNG (default 65), cualquier gate que exija un
        # effective_min mayor es bypasseado y el trade avanza al LLM/ejecución.
        # El gate final RNG_PROBABILITY_GATE sigue siendo el filtro autoritativo.
        # Scarcity puede no estar resuelta aquí; RNG será conservador (sin componente
        # de escasez = underestimate): kinetic(35)+fvg_bos(30)+vision(20)=85 max.
        _mk_kinetic = float(snap.score_breakdown.get("kinetic_compressed") or 0.0)
        _mk_fvg_bos = bool(snap.score_breakdown.get("fvg_bos_validated", False))
        _mk_vision  = bool(
            (self._last_vision_context or {})
            .get(tick.symbol.upper(), {})
            .get("zona_liquidez_macro", False)
        )
        # FASE 3: fallback to eager _last_fvg_state cache — scarcity loading at
        # line ~3978 runs AFTER MASTER KEY, so score_breakdown may not have it yet.
        _mk_scar_raw   = (
            snap.score_breakdown.get("scarcity_state")
            or (self._last_fvg_state.get(_cache_sym) or {}).get("scarcity_state")
        )
        _mk_scar       = str(_mk_scar_raw or "")
        _mk_scar_ratio_raw = snap.score_breakdown.get("scarcity_ratio")
        if _mk_scar_ratio_raw is None:
            _mk_scar_ratio_raw = (self._last_fvg_state.get(_cache_sym) or {}).get("scarcity_ratio")
        _mk_scar_ratio = float(_mk_scar_ratio_raw or 0.0)
        _mk_rng, _mk_missing = _calculate_rng_probability(
            kinetic_score=_mk_kinetic,
            fvg_bos_validated=_mk_fvg_bos,
            zona_liquidez_macro=_mk_vision,
            scarcity_state=_mk_scar,
            scarcity_ratio=_mk_scar_ratio,
            cooldown_active=False,
        )
        _mk_score_thr = float(os.getenv("DERIV_MASTER_KEY_SCORE", "6.25") or "6.25")
        _mk_rng_thr   = int(float(os.getenv("DERIV_MASTER_KEY_RNG", "65") or "65"))
        _master_key   = (
            is_spike_market(tick.symbol)
            and snap.score >= _mk_score_thr
            and _mk_rng >= _mk_rng_thr
        )
        # Persist preliminary RNG estimate immediately — so market context always
        # has a value even when the pipeline blocks at a downstream gate.
        # The full _rng_prob at line ~4710 overwrites this with cooldown-adjusted value.
        if _cache_sym in self._last_fvg_state:
            self._last_fvg_state[_cache_sym].update({
                "rng_probability": _mk_rng,
                "rng_threshold":   _mk_rng_thr,
                "rng_missing":     _mk_missing,
            })
        if _master_key:
            _LOGGER.debug(
                "[PIPELINE] MASTER_KEY armed %s | score=%.2f≥%.2f rng=%d≥%d"
                " kinetic=%s fvg=%s scar=%s",
                tick.symbol, snap.score, _mk_score_thr, _mk_rng, _mk_rng_thr,
                _mk_kinetic, _mk_fvg_bos, _mk_scar or "unknown",
            )

        # ── 2026-06-01 SPIKE-IMMINENCE ("malicia": ¿esta cargado para tirar?) ─
        # Predictive layer: when the live ticks_since_last_spike sits in the
        # modal firing band (RIPE p50..p75) the symbol is statistically loaded.
        # This is the trader instinct "lleva rato sin salir y tiene numeros de
        # cuando tira". We surface it to the LLM (prompt + score_breakdown) and
        # grant a readiness bonus in BUILDING/RIPE so a high-probability spike
        # setup is NOT missed just because the regime label says trending.
        if _is_bc_bias:
            # _imm already computed above (regime-penalty modulation); reuse it
            # to avoid a second get_spike_imminence_state() call per tick.
            _imm_state = str(_imm.get("state") or "")
            _imm_score = float(_imm.get("score") or 0.0)
            if _imm_state and _imm_state != "UNKNOWN":
                snap.score_breakdown["spike_imminence_state"] = _imm_state
                snap.score_breakdown["spike_imminence_score"] = round(_imm_score, 3)
                snap.score_breakdown["spike_ticks_since_last"] = _imm.get("ticks_since_last")
            # ── 2026-06-08: Imminencia corregida por datos cuantitativos ────────
            # Análisis 172 trades: BUILDING(0.30-0.60)=61%WR, RIPE(>0.80)=30%WR.
            # El edge real está en BUILDING (cañón cargándose), NO en RIPE
            # (donde el retroceso post-spike ya es inminente).
            # BUILDING: bonus completo (captura el movimiento mientras se construye)
            # OVERDUE:  bonus reducido 40% (estadísticamente tarde, pero aún válido)
            # RIPE:     sin bonus + gate de score mínimo (30%WR no merece premium)
            if _imm_state in ("BUILDING", "OVERDUE", "RIPE") and "spike_cluster_bonus" not in snap.score_breakdown:
                _imm_max = max(
                    0.0,
                    float(os.getenv("DERIV_SPIKE_IMMINENCE_MAX_BONUS", "1.5") or 1.5),
                )
                if _imm_state == "RIPE":
                    # RIPE: inminencia alta → spike próximo en ~3 min (ghost WR 86.7% para
                    # SMC_FVG Grade A/B score≥7.0). Umbral bajado 8.5→7.5 por evidencia ghost.
                    # DERIV_IMMINENCE_RIPE_MIN_SCORE controla (default 7.5).
                    # Ceiling-aware: si el orquestador IA fijó un ceiling < 7.5
                    # para este símbolo/régimen, bajamos _ripe_min al ceiling - margen
                    # para evitar el bloqueo matemático (score máximo < gate mínimo).
                    _imm_boost = 0.0
                    _ripe_min_global = float(os.getenv("DERIV_IMMINENCE_RIPE_MIN_SCORE", "7.5") or 7.5)
                    _ripe_ceiling_margin = float(os.getenv("DERIV_IMMINENCE_RIPE_CEILING_MARGIN", "0.10") or 0.10)
                    _ripe_dyn_ceiling = float(snap.score_breakdown.get("dynamic_score_ceiling") or 99.0)
                    _ripe_min = min(_ripe_min_global, max(6.0, _ripe_dyn_ceiling - _ripe_ceiling_margin))
                    # LISTO_RIPE_BYPASS: cuando scarcity=LISTO/CARGANDO Y spike RIPE, el mercado
                    # está estadísticamente cargado para disparar el PRIMER spike. Entramos
                    # ANTES del spike (no después como HOT_REENTRY). Disabled by default (0).
                    # Set DERIV_LISTO_RIPE_BYPASS_MIN_SCORE=6.0 to enable.
                    _lrb_min = float(os.getenv("DERIV_LISTO_RIPE_BYPASS_MIN_SCORE", "0") or 0)
                    _lrb_scar = str(snap.score_breakdown.get("scarcity_state") or "")
                    if (_lrb_min > 0 and _lrb_scar in ("LISTO", "CARGANDO")
                            and float(snap.score) >= _lrb_min):
                        snap.score_breakdown["listo_ripe_bypass_fired"] = round(float(snap.score), 2)
                        _ripe_min = _lrb_min
                        _LOGGER.info(
                            "[PIPELINE] LISTO_RIPE_BYPASS %s | score=%.2f scar=%s imm=%.2f"
                            " → lowering RIPE gate %.1f→%.1f (pre-spike entry)",
                            tick.symbol, float(snap.score), _lrb_scar,
                            _imm_score, 7.5, _lrb_min,
                        )
                    if snap.score < _ripe_min:
                        snap.score_breakdown["imminence_ripe_block"] = True
                        self._log_entry_block(
                            tick.symbol, "IMMINENCE_RIPE_GATE",
                            score=snap.score, effective_min_score=_ripe_min,
                            side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                            score_breakdown=snap.score_breakdown,
                        )
                        self._record_decision(
                            symbol=tick.symbol, allowed=False, side=snap.side,
                            score=snap.score,
                            reason=(
                                f"IMMINENCE_RIPE_GATE: RIPE state (imm={_imm_score:.2f}) "
                                f"requires score≥{_ripe_min:.1f} got={snap.score:.2f} "
                                f"(datos: RIPE=30%WR)"
                            ),
                            extra={
                                "gate_name": "IMMINENCE_RIPE",
                                "effective_min": round(_ripe_min, 2),
                                "fvg_bos_validated": bool(snap.score_breakdown.get("fvg_bos_validated", False)),
                            },
                        )
                        self._spike_enrich(
                            tick.symbol, bot_entered=False,
                            block_reason="imminence_ripe_gate", score=snap.score,
                        )
                        _gl_setup = str(snap.score_breakdown.get("setup_type") or "")
                        _gl_grade = str(snap.score_breakdown.get("execution_grade") or "")
                        if _gl_setup in ("SMC_FVG", "EMA200_SPIKE") and _gl_grade in ("A", "B") and snap.score >= 7.0:
                            self._ghost_logger.add(
                                symbol=tick.symbol, price=float(tick.price), side=str(snap.side or ""),
                                score_raw=float(snap.score), gate="IMMINENCE_RIPE_GATE",
                                setup_type=_gl_setup, grade=_gl_grade,
                                scarcity=str(snap.score_breakdown.get("scarcity_state") or ""),
                                imm_state=str(snap.score_breakdown.get("spike_imminence_state") or ""),
                                imm_score=float(snap.score_breakdown.get("spike_imminence_score") or 0.0),
                            )
                        _bc_z2_ripe_bypass = (
                            _is_bc_bias
                            and _scarcity_ratio_early >= _el_z2_early
                        )
                        if _master_key:
                            _LOGGER.info(
                                "[PIPELINE] GATE_BYPASS MASTER_KEY %s | gate=IMMINENCE_RIPE_GATE"
                                " score=%.2f≥%.2f AND rng=%d≥%d → bypassing effective_min=%.2f",
                                tick.symbol, snap.score, _mk_score_thr,
                                _mk_rng, _mk_rng_thr, _ripe_min,
                            )
                            snap.score_breakdown["master_key_bypass"] = "IMMINENCE_RIPE_GATE"
                            snap.score_breakdown["master_key_rng"] = _mk_rng
                        elif _bc_z2_ripe_bypass:
                            # BOOM/CRASH in Z2 extreme scarcity: spike is BOTH imminent
                            # AND statistically overdue. RIPE + overdue = best setup.
                            # The 30%WR stat was computed without the Z2 filter.
                            _LOGGER.info(
                                "[PIPELINE] BC_Z2_RIPE_BYPASS %s | score=%.2f"
                                " scarcity_ratio=%.2f ≥ Z2=%.1f → bypass IMMINENCE_RIPE_GATE",
                                tick.symbol, snap.score, _scarcity_ratio_early, _el_z2_early,
                            )
                            snap.score_breakdown["bc_z2_ripe_bypass"] = "IMMINENCE_RIPE_GATE"
                        else:
                            return
                    # Passed RIPE gate: tag but no bonus
                    snap.score_breakdown["spike_imminence_bonus"] = 0.0
                    snap.reasons.append(
                        f"spike_imminence_RIPE: gap={_imm.get('ticks_since_last')}t "
                        f"score={_imm_score:.2f} → no bonus (RIPE gate passed, score≥{_ripe_min:.1f})"
                    )
                elif _imm_state == "BUILDING" and 0.25 <= _imm_score <= 0.65:
                    # Sweet spot: bonus completo escalado por score de inminencia
                    _imm_boost = round(_imm_max * _imm_score, 2)
                    snap.score = round(min(10.0, snap.score + _imm_boost), 3)
                    snap.score_breakdown["spike_imminence_bonus"] = _imm_boost
                    snap.reasons.append(
                        f"spike_imminence_BUILDING: gap={_imm.get('ticks_since_last')}t "
                        f"score={_imm_score:.2f} → +{_imm_boost:.2f} (61%WR sweet spot)"
                    )
                    _LOGGER.info(
                        "[PIPELINE] SPIKE_IMMINENCE_BUILDING %s gap=%st imm=%.2f → +%.2f score→%.2f",
                        tick.symbol, _imm.get("ticks_since_last"),
                        _imm_score, _imm_boost, snap.score,
                    )
                elif _imm_state == "OVERDUE":
                    # Pasó el p75 — spike overdue, bonus reducido 40%
                    _imm_boost = round(_imm_max * _imm_score * 0.40, 2)
                    if _imm_boost > 0.0:
                        snap.score = round(min(10.0, snap.score + _imm_boost), 3)
                        snap.score_breakdown["spike_imminence_bonus"] = _imm_boost
                        snap.reasons.append(
                            f"spike_imminence_OVERDUE: gap={_imm.get('ticks_since_last')}t "
                            f"score={_imm_score:.2f} → +{_imm_boost:.2f} (overdue 40%)"
                        )

        # ── 2026-06-06 HURST × IMMINENCIA — bonus mean-reversion para CRASH ─
        # Insight cuantitativo: cuando Hurst < 0.42 el mercado es mean-reverting
        # fuerte — el precio NO puede seguir subiendo indefinidamente y un spike
        # CRASH (bajada) es cada vez más probable.  Combinado con el estado de
        # imminencia (≥ BUILDING), esto es la señal de "cañón cargado": baja
        # volatilidad direccional + muchos ticks acumulados = spike inminente.
        # El bonus escala linealmente: H=0.42 → 0, H=0.30 → máximo.
        # Controlable vía env: DERIV_CRASH_HURST_MR_BONUS_MAX (default 1.2)
        if _is_bc_bias and "CRASH" in tick.symbol.upper():
            _h_local = float(_eval_hurst if _eval_hurst is not None else 0.5)
            _h_threshold = float(os.getenv("DERIV_CRASH_HURST_MR_THRESHOLD", "0.42"))
            if _h_local < _h_threshold and _imm_state not in ("", "UNKNOWN", "FRESH"):
                _h_mr_max = float(os.getenv("DERIV_CRASH_HURST_MR_BONUS_MAX", "1.2"))
                # Escala 0→1 entre threshold y 0.30 (Hurst muy bajo = señal fuerte)
                _h_mr_factor = min(1.0, max(0.0, (_h_threshold - _h_local) / (_h_threshold - 0.30)))
                _h_mr_boost = round(_h_mr_max * _h_mr_factor, 2)
                if _h_mr_boost >= 0.1:
                    snap.score = round(min(10.0, snap.score + _h_mr_boost), 3)
                    snap.score_breakdown["crash_hurst_mr_bonus"] = _h_mr_boost
                    snap.score_breakdown["crash_hurst_mr_h"] = round(_h_local, 3)
                    snap.reasons.append(
                        f"crash_hurst_mr: H={_h_local:.3f}<{_h_threshold} + {_imm_state} "
                        f"→ +{_h_mr_boost:.2f}"
                    )
                    _LOGGER.info(
                        "[PIPELINE] CRASH_HURST_MR %s | H=%.3f<%s + %s gap=%st → +%.2f score→%.2f",
                        tick.symbol, _h_local, _h_threshold, _imm_state,
                        _imm.get("ticks_since_last", 0), _h_mr_boost, snap.score,
                    )

        # ── Scarcity state — inverted scoring feeds _calculate_rng_probability ─
        # SECO/VENCIDO states now reward the RNG (overdue = spike more likely).
        # Hard gates removed (FASE 1): SECO gets 15 pts, VENCIDO 12, LISTO 8.
        if _is_bc_bias:
            try:
                _scar = self._risk.get_scarcity_state(tick.symbol)
            except Exception:
                _scar = {}
            _scar_state = str(_scar.get("state") or "")
            if _scar_state and _scar_state != "SIN_DATOS":
                snap.score_breakdown["scarcity_state"]     = _scar_state
                snap.score_breakdown["scarcity_ratio"]     = _scar.get("ratio")
                snap.score_breakdown["scarcity_elapsed_s"] = _scar.get("elapsed_s")
            else:
                # SIN_DATOS or exception path (ratio=None): fall back to last known
                # from eager cache (set at line ~3171 with the SIN_DATOS guard).
                _fb_scar = self._last_fvg_state.get(_cache_sym) or {}
                _fb_st   = str(_fb_scar.get("scarcity_state") or "")
                _fb_rt   = _fb_scar.get("scarcity_ratio")
                if _fb_st and _fb_st != "SIN_DATOS":
                    snap.score_breakdown["scarcity_state"] = _fb_st
                    if _fb_rt is not None:
                        snap.score_breakdown["scarcity_ratio"] = _fb_rt
                    _LOGGER.debug(
                        "[SCARCITY_FALLBACK] %s live=%s→SIN_DATOS, using cached %s ratio=%.2f",
                        tick.symbol, _scar_state or "?", _fb_st, float(_fb_rt or 0.0),
                    )

        # ── LEY 2: arm SECO/VENCIDO regime-gate bypass ───────────────────────
        # scarcity_dry_override_gate enables the bypass at ~line 4258:
        # when score < regime_min AND scarcity is SECO/VENCIDO, entry is allowed
        # if score >= this gate (DERIV_DRY_OVERRIDE_SCORE or per-symbol map).
        # Without this block the gate key is never set and the bypass is dead code.
        if _is_bc_bias:
            _seco_resolved = str(snap.score_breakdown.get("scarcity_state") or "")
            if _seco_resolved in ("SECO", "VENCIDO"):
                _dry_gate_score = float(os.getenv("DERIV_DRY_OVERRIDE_SCORE", "8.5") or "8.5")
                _dry_map_str = os.getenv("DERIV_DRY_OVERRIDE_SCORE_MAP", "") or ""
                for _dry_entry in _dry_map_str.split(","):
                    _dry_entry = _dry_entry.strip()
                    if ":" in _dry_entry:
                        _dry_sym_k, _dry_val_k = _dry_entry.split(":", 1)
                        if _dry_sym_k.strip().upper() == tick.symbol.upper():
                            try:
                                _dry_gate_score = float(_dry_val_k.strip())
                            except Exception:
                                pass
                snap.score_breakdown["scarcity_dry_override_gate"] = round(_dry_gate_score, 2)
                _LOGGER.debug(
                    "[LEY2] %s %s → dry_override_gate=%.2f (regime gate bypass armed)",
                    tick.symbol, _seco_resolved, _dry_gate_score,
                )

        # ── Late-stage telemetry cache: imminence and scarcity are added to
        # score_breakdown AFTER risk.evaluate() (lines ~3395 and ~3468).
        # Update here so operator console sees live SCAR/IMM indicators even
        # when the tick exits early through the TREND gate below.
        if _cache_sym in self._last_fvg_state:
            _sb_tel = snap.score_breakdown if isinstance(snap.score_breakdown, dict) else {}
            # Only overwrite when value is non-None — preserves last-known state
            # across ticks where scarcity/imminence modules don't run (hard-veto paths).
            _tel_fields = {
                "scarcity_state":          _sb_tel.get("scarcity_state"),
                "scarcity_ratio":          _sb_tel.get("scarcity_ratio"),
                "spike_imminence_state":   _sb_tel.get("spike_imminence_state"),
                "spike_imminence_score":   _sb_tel.get("spike_imminence_score"),
                "execution_grade":         _sb_tel.get("execution_grade"),
                "score_raw":               _sb_tel.get("score_raw"),
                "fvg_anchor_active":       _sb_tel.get("fvg_anchor_active"),
                "fvg_anchor_age_s":        _sb_tel.get("fvg_anchor_age_s"),
                "fvg_anchor_wick_survived":_sb_tel.get("fvg_anchor_wick_survived"),
                "fvg_anchor_tol_penalty":  _sb_tel.get("fvg_anchor_tol_penalty"),
                "fvg_anchor_mom_penalty":  _sb_tel.get("fvg_anchor_mom_penalty"),
                # Dual-path structural fields
                "structural_fvg_active":   _sb_tel.get("structural_fvg_active"),
                "structural_fvg_direction":_sb_tel.get("structural_fvg_direction"),
                "structural_fvg_confirm":  _sb_tel.get("structural_fvg_confirm"),
                "structural_fvg_conflict": _sb_tel.get("structural_fvg_conflict"),
                "structural_fvg_absent":   _sb_tel.get("structural_fvg_absent"),
                "atr_anchored":            _sb_tel.get("atr_anchored"),
                "atr_pre_spike":           _sb_tel.get("atr_pre_spike"),
                "geo_post_spike_nullified":_sb_tel.get("geo_post_spike_nullified"),
                # Kinetic / RNG inputs (set by deriv_risk before this block)
                "kinetic_compressed":      _sb_tel.get("kinetic_compressed"),
                "kinetic_velocity":        _sb_tel.get("kinetic_velocity"),
                "kinetic_acceleration":    _sb_tel.get("kinetic_acceleration"),
                "ghost_mad":               _sb_tel.get("ghost_mad"),
                "fvg_bos_validated":       _sb_tel.get("fvg_bos_validated"),
                "ema200_anchored":         _sb_tel.get("ema200_anchored"),
                # Master Key bypass (set at individual gate bypasses, always before this point)
                "master_key_bypass":       _sb_tel.get("master_key_bypass"),
                "master_key_rng":          _sb_tel.get("master_key_rng"),
            }
            self._last_fvg_state[_cache_sym].update(
                {k: v for k, v in _tel_fields.items() if v is not None}
            )


        # ── ELASTIC THRESHOLDS: Overpressure Discount ────────────────────────
        # FASE 3 enhanced: two axes of pressure detection:
        #   Axis A (Z-score proxy): scarcity_ratio ≥ 1.5 / 2.0
        #   Axis B (time pressure): elapsed_s ≥ EXTREME_TENSION_LIMITS_SEC[symbol]
        # Either axis alone triggers the discount. Combined, they stack.
        #   Z1 / time≥70%: partial discount (14%) on regime effective_min
        #   Z2 / time≥100%: full discount (28%) + structure bypass + fast-track
        # Floor DERIV_ELASTIC_FLOOR (5.50) prevents infinite collapse.
        _elasticity_z = 0.0
        _elasticity_fast_track = False
        _is_z1 = False
        _is_z2 = False
        if _is_bc_bias:
            _op_ratio       = float(snap.score_breakdown.get("scarcity_ratio") or 0.0)
            _op_kinetic     = float(snap.score_breakdown.get("kinetic_compressed") or 0.0)
            _op_elapsed_s   = float(snap.score_breakdown.get("scarcity_elapsed_s") or 0.0)
            _op_sym_limit   = float(EXTREME_TENSION_LIMITS_SEC.get(tick.symbol.upper(), 2000))
            _el_z1          = float(os.getenv("DERIV_ELASTIC_Z1", "1.5") or 1.5)
            _el_z2          = float(os.getenv("DERIV_ELASTIC_Z2", "2.0") or 2.0)
            # Time pressure: full when elapsed ≥ limit, partial at ≥ 70%
            _time_pressure_full    = _op_elapsed_s > 0 and _op_elapsed_s >= _op_sym_limit
            _time_pressure_partial = _op_elapsed_s > 0 and _op_elapsed_s >= _op_sym_limit * 0.70
            # Choose the stronger signal between Z-score and time pressure
            _is_z2 = _op_ratio >= _el_z2 or _time_pressure_full
            _is_z1 = _op_ratio >= _el_z1 or _time_pressure_partial
            # Effective Z for logging (use ratio if higher, else normalize time)
            _elasticity_z = max(
                _op_ratio,
                round(_op_elapsed_s / _op_sym_limit * _el_z2, 2) if _op_sym_limit > 0 else 0.0,
            )
            snap.score_breakdown["elasticity_time_pressure"] = round(_op_elapsed_s / _op_sym_limit, 3) if _op_sym_limit > 0 else 0.0
            if _is_z2:
                snap.score_breakdown["market_context"] = "EXTREME_OVERPRESSURE"
                snap.score_breakdown["elasticity_z"]   = round(_elasticity_z, 2)
                if _time_pressure_full:
                    snap.score_breakdown["elasticity_time_triggered"] = True
                if _op_kinetic > 0:
                    _elasticity_fast_track = True
                    snap.score_breakdown["elasticity_fast_track"] = True
                    if snap.score_breakdown.get("structural_fvg_conflict"):
                        snap.score_breakdown["elasticity_struct_bypass"] = True
                        snap.score_breakdown.pop("structural_fvg_conflict", None)
                        _LOGGER.info(
                            "[ELASTICITY] %s Z=+%.2f fast-track: structural conflict"
                            " bypassed (kinetic=%.2f)",
                            tick.symbol, _elasticity_z, _op_kinetic,
                        )
            elif _is_z1:
                snap.score_breakdown["elasticity_z"] = round(_elasticity_z, 2)
                if _time_pressure_partial:
                    snap.score_breakdown["elasticity_time_triggered"] = True

        # ── Structural FVG conflict gate ──────────────────────────────────────
        # Si el FVG de ticks apunta en dirección opuesta a la del candle de 5m,
        # tenemos micro vs macro en conflicto. El bot puede entrar, pero solo con
        # estructura sobresaliente (≥9.0). Cualquier cosa menor es ruido de ticks.
        if snap.score_breakdown.get("structural_fvg_conflict") and _is_bc_bias:
            _struct_min = float(
                os.getenv("DERIV_STRUCTURAL_CONFLICT_MIN_SCORE", "9.0") or 9.0
            )
            if snap.score < _struct_min:
                snap.score_breakdown["structural_conflict_block"] = True
                self._log_entry_block(
                    tick.symbol, "STRUCTURAL_CONFLICT_GATE",
                    score=snap.score, effective_min_score=_struct_min,
                    side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                    score_breakdown=snap.score_breakdown,
                )
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score,
                    reason=(
                        f"STRUCTURAL_CONFLICT_GATE: tick FVG ≠ 5m candle FVG "
                        f"requires score≥{_struct_min:.1f} got={snap.score:.2f}"
                    ),
                )
                self._spike_enrich(
                    tick.symbol, bot_entered=False,
                    block_reason="structural_conflict_gate", score=snap.score,
                )
                _gl_setup = str(snap.score_breakdown.get("setup_type") or "")
                _gl_grade = str(snap.score_breakdown.get("execution_grade") or "")
                if _gl_setup in ("SMC_FVG", "EMA200_SPIKE") and _gl_grade in ("A", "B") and snap.score >= 7.0:
                    self._ghost_logger.add(
                        symbol=tick.symbol, price=float(tick.price), side=str(snap.side or ""),
                        score_raw=float(snap.score), gate="STRUCTURAL_CONFLICT_GATE",
                        setup_type=_gl_setup, grade=_gl_grade,
                        scarcity=str(snap.score_breakdown.get("scarcity_state") or ""),
                        imm_state=str(snap.score_breakdown.get("spike_imminence_state") or ""),
                        imm_score=float(snap.score_breakdown.get("spike_imminence_score") or 0.0),
                    )
                return

        # ── Module 2: Velocity-confluence override (tick acceleration + HD) ──
        # When the TickVelocityAnalyzer detects exponential tick-delta acceleration
        # AND the macro Higher-Direction is aligned (+1.5 hd_bonus already in score),
        # the combination strongly suggests an imminent spike.  Grant a +1.0 bonus
        # to push borderline scores over the effective_min gate.
        # Only applied for spike markets (BOOM/CRASH) to avoid false triggers on R_*.
        _vel_acc, _vel_score, _vel_dir = self._velocity.check_acceleration(tick.symbol)
        _hd_bonus_val = float(snap.score_breakdown.get("hd_bonus", 0.0))
        _is_bc_vel = any(k in tick.symbol.upper() for k in ("BOOM", "CRASH"))
        if (
            _vel_acc
            and _hd_bonus_val >= 1.5
            and _is_bc_vel
            and snap.score >= (_regime_min - 1.5)
        ):
            _vel_boost = round(1.0 * _vel_score, 2)  # up to +1.0 scaled by accel_score
            snap.score = round(min(10.0, snap.score + _vel_boost), 3)
            snap.score_breakdown["velocity_boost"] = _vel_boost
            snap.score_breakdown["velocity_score"] = round(_vel_score, 3)
            snap.score_breakdown["velocity_dir"] = _vel_dir or "?"
            snap.reasons.append(
                f"velocity_confluence: acc_score={_vel_score:.2f} dir={_vel_dir} "
                f"hd={_hd_bonus_val:+.1f} → +{_vel_boost:.2f}"
            )
            _LOGGER.info(
                "[PIPELINE] VELOCITY_CONFLUENCE %s | acc=%.2f dir=%s hd=%+.1f "
                "boost=+%.2f score→%.2f",
                tick.symbol, _vel_score, _vel_dir or "?", _hd_bonus_val,
                _vel_boost, snap.score,
            )

        # ── Apply elasticity discount to regime gate threshold ───────────────
        # Uses pre-computed _is_z2/_is_z1 booleans (FASE 3) that combine both
        # Z-score and time-pressure axes instead of raw ratio thresholds.
        _original_regime_min = _regime_min
        _el_floor = float(os.getenv("DERIV_ELASTIC_FLOOR", "5.50") or 5.50)
        if _is_bc_bias and _is_z2:
            _el_pct     = float(os.getenv("DERIV_ELASTIC_DISCOUNT_PCT", "0.28") or 0.28)
            _regime_min = max(_el_floor, _regime_min * (1.0 - _el_pct))
            snap.score_breakdown["elasticity_regime_min_original"] = round(_original_regime_min, 2)
            snap.score_breakdown["elasticity_regime_min_elastic"]  = round(_regime_min, 2)
            _LOGGER.info(
                "[ELASTICITY] %s Z=+%.2f elapsed=%.0fs/%.0fs -> Required Score lowered from %.2f to %.2f.%s",
                tick.symbol, _elasticity_z,
                snap.score_breakdown.get("scarcity_elapsed_s") or 0.0,
                EXTREME_TENSION_LIMITS_SEC.get(tick.symbol.upper(), 2000),
                _original_regime_min, _regime_min,
                " ENTRY AUTHORIZED." if snap.score >= _regime_min else "",
            )
        elif _is_bc_bias and _is_z1:
            _el_pct     = float(os.getenv("DERIV_ELASTIC_DISCOUNT_PCT", "0.28") or 0.28) * 0.5
            _regime_min = max(_el_floor, _regime_min * (1.0 - _el_pct))
            snap.score_breakdown["elasticity_regime_min_original"] = round(_original_regime_min, 2)
            snap.score_breakdown["elasticity_regime_min_elastic"]  = round(_regime_min, 2)
            _LOGGER.info(
                "[ELASTICITY] %s Z=+%.2f elapsed=%.0fs/%.0fs (partial) -> Required Score lowered from %.2f to %.2f",
                tick.symbol, _elasticity_z,
                snap.score_breakdown.get("scarcity_elapsed_s") or 0.0,
                EXTREME_TENSION_LIMITS_SEC.get(tick.symbol.upper(), 2000),
                _original_regime_min, _regime_min,
            )

        if snap.score < _regime_min:
            # SECO-override bypass: the SECO gate already validated the score at a
            # lower threshold. In SECO state, scores are structurally depressed below
            # the regime min (e.g. CRASH900 peaks at ~6.3 in deep SECO). The SECO gate
            # is the authority for these entries; the regime gate would double-block.
            _seco_override_gate = snap.score_breakdown.get("scarcity_dry_override_gate")
            if _seco_override_gate is not None and snap.score >= float(_seco_override_gate):
                snap.score_breakdown["regime_gate_bypassed_by_seco"] = True
            else:
                _regime_gate_name = (
                    f"REGIME_SCORE_GATE_{snap.regime}_dynamic"
                    if _dyn_active else
                    f"REGIME_SCORE_GATE_{snap.regime}"
                )
                self._log_entry_block(
                    tick.symbol, _regime_gate_name,
                    score=snap.score, effective_min_score=_regime_min,
                    side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                    score_breakdown=snap.score_breakdown,
                )
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score,
                    reason=(
                        f"REGIME_SCORE_GATE_DYNAMIC: requires≥{_regime_min:.2f} got={snap.score:.2f}"
                        if _dyn_active else
                        f"REGIME_SCORE_GATE: {snap.regime} requires≥{_regime_min:.2f} got={snap.score:.2f}"
                    ),
                    extra={
                        "regime": snap.regime,
                        "gate_name": "REGIME",
                        "effective_min": round(_regime_min, 2),
                        "fvg_bos_validated": bool(snap.score_breakdown.get("fvg_bos_validated", False)),
                    },
                )
                if _master_key:
                    _LOGGER.info(
                        "[PIPELINE] GATE_BYPASS MASTER_KEY %s | gate=REGIME_SCORE_GATE"
                        " score=%.2f≥%.2f AND rng=%d≥%d → bypassing effective_min=%.2f",
                        tick.symbol, snap.score, _mk_score_thr,
                        _mk_rng, _mk_rng_thr, _regime_min,
                    )
                    snap.score_breakdown["master_key_bypass"] = "REGIME_SCORE_GATE"
                    snap.score_breakdown["master_key_rng"] = _mk_rng
                else:
                    return

        # BOOM600 safety override: trending regime needs extra score conviction.
        # BYPASS when scarcity is SECO/VENCIDO or Z2 pressure — high-tension states
        # mean the spike is statistically overdue; the trending gate would double-block
        # what ELASTICITY + scoring already validated.
        _b600_scarcity = str(snap.score_breakdown.get("scarcity_state") or "")
        _b600_dry_bypass = (
            tick.symbol.upper() == "BOOM600"
            and (_b600_scarcity in ("SECO", "VENCIDO") or _is_z2)
        )
        if tick.symbol.upper() == "BOOM600" and str(snap.regime or "").lower() == "trending" and not _b600_dry_bypass:
            # 2026-05-29: gate is now IA-managed via dynamic_symbol_config.
            # Floor at 8.5 to prevent IA from collapsing the gate too low.
            # Ceiling at 9.5 to stay within sane bounds. IA tunes inside that band
            # by adjusting BOOM600.score_min_override.
            _boom600_dyn_cfg = self._dynamic_configs.get("BOOM600") or {}
            try:
                _boom600_dyn_min = float(_boom600_dyn_cfg.get("score_min_override") or 8.5)
            except Exception:
                _boom600_dyn_min = 8.5
            _boom600_trending_min = max(8.5, min(9.5, _boom600_dyn_min))
            snap.score_breakdown["boom600_trending_min_score"] = _boom600_trending_min
            snap.score_breakdown["boom600_trending_min_source"] = (
                "dynamic_cfg" if _boom600_dyn_cfg else "default"
            )
            if snap.score < _boom600_trending_min:
                self._log_entry_block(
                    tick.symbol,
                    "BOOM600_TRENDING_SCORE_GATE",
                    score=snap.score,
                    effective_min_score=_boom600_trending_min,
                    side=snap.side,
                    regime=snap.regime,
                    hurst=_eval_hurst,
                    score_breakdown=snap.score_breakdown,
                )
                self._record_decision(
                    symbol=tick.symbol,
                    allowed=False,
                    side=snap.side,
                    score=snap.score,
                    reason=(
                        f"BOOM600_TRENDING_SCORE_GATE: requires>={_boom600_trending_min:.2f} "
                        f"got={snap.score:.2f}"
                    ),
                    extra={"regime": snap.regime},
                )
                return

        # Per-symbol minimum score gate (ASSET_INTEL_PROFILES)
        _profile_min_score = _dyn_score_min if _dyn_active else min_score_for(tick.symbol)
        if _sym_bleed_bonus > 0.0:
            _profile_min_score = min(10.0, _profile_min_score + _sym_bleed_bonus)
        # Mirror calm-regime bonus on profile gate so it stays coherent with regime gate.
        # Without this, regime_min gets the -0.30 calm discount but profile_min keeps the
        # higher raw value — profile becomes the tighter gate on calm-regime symbols (CRASH900).
        _profile_calm_bon = float(snap.score_breakdown.get("regime_calm_bonus") or 0.0)
        if _profile_calm_bon > 0.0:
            _profile_min_score = max(0.0, _profile_min_score - _profile_calm_bon)
        # FASE 3 elastic: also discount the PROFILE gate under extreme tension so
        # it doesn't remain a hard floor when the REGIME gate was already relaxed.
        if _is_bc_bias and _is_z2:
            _el_pct_prof = float(os.getenv("DERIV_ELASTIC_DISCOUNT_PCT", "0.28") or 0.28) * 0.50
            _profile_min_score = max(
                float(os.getenv("DERIV_ELASTIC_FLOOR", "5.50") or 5.50),
                _profile_min_score * (1.0 - _el_pct_prof),
            )
            snap.score_breakdown["elasticity_profile_min_elastic"] = round(_profile_min_score, 2)
        elif _is_bc_bias and _is_z1:
            _el_pct_prof = float(os.getenv("DERIV_ELASTIC_DISCOUNT_PCT", "0.28") or 0.28) * 0.25
            _profile_min_score = max(
                float(os.getenv("DERIV_ELASTIC_FLOOR", "5.50") or 5.50),
                _profile_min_score * (1.0 - _el_pct_prof),
            )
            snap.score_breakdown["elasticity_profile_min_elastic"] = round(_profile_min_score, 2)

        # Keep a single authoritative effective_min for every downstream gate/log.
        # This removes visual drift where AI_VETO/ENTRY_ALLOWED showed the base
        # floor while dynamic/profile/symbol hardening was already active.
        _effective_pipeline_min = max(
            float(snap.effective_min_score or 0.0),
            float(_regime_min),
            float(_profile_min_score),
        )
        snap.effective_min_score = round(_effective_pipeline_min, 3)
        snap.score_breakdown["effective_min_base"] = round(float(snap.score_breakdown.get("effective_min") or 0.0), 3)
        snap.score_breakdown["effective_min_regime"] = round(float(_regime_min), 3)
        snap.score_breakdown["effective_min_profile"] = round(float(_profile_min_score), 3)
        snap.score_breakdown["effective_min_pipeline"] = round(float(_effective_pipeline_min), 3)

        _asset_profile = get_asset_profile(tick.symbol)
        if snap.allowed and snap.score < _profile_min_score:
            self._log_entry_block(
                tick.symbol, "PROFILE_SCORE_GATE_DYNAMIC" if _dyn_active else "PROFILE_SCORE_GATE",
                score=snap.score, effective_min_score=_profile_min_score,
                side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                score_breakdown=snap.score_breakdown,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score,
                reason=(
                    f"SCORE_TOO_LOW_DYNAMIC: requires>={_profile_min_score:.2f} got={snap.score:.2f}"
                    if _dyn_active else
                    f"PROFILE_SCORE_GATE: {_asset_profile.get('type','?')} "
                    f"requires>={_profile_min_score:.1f} got={snap.score:.2f}"
                ),
                extra={
                    "score_breakdown": snap.score_breakdown,
                    "regime": snap.regime,
                    "profile": _asset_profile,
                    "dynamic_cfg": _dyn_cfg,
                },
            )
            if _master_key:
                _LOGGER.info(
                    "[PIPELINE] GATE_BYPASS MASTER_KEY %s | gate=PROFILE_SCORE_GATE"
                    " score=%.2f≥%.2f AND rng=%d≥%d → bypassing effective_min=%.2f",
                    tick.symbol, snap.score, _mk_score_thr,
                    _mk_rng, _mk_rng_thr, _profile_min_score,
                )
                snap.score_breakdown["master_key_bypass"] = "PROFILE_SCORE_GATE"
                snap.score_breakdown["master_key_rng"] = _mk_rng
            else:
                return

        # Post-spike intelligent gate: after the anti-chase window, only allow
        # continuation entries when multi-signal strength is explicit.
        _post_spike_ok, _post_spike_reason = self._passes_post_spike_strength_gate(
            symbol=tick.symbol,
            snap=snap,
            profile=_asset_profile,
            dyn_cfg=_dyn_cfg,
            dyn_active=_dyn_active,
            score_floor=_profile_min_score,
            elastic_z2=_is_z2,
        )
        if not _post_spike_ok:
            self._log_entry_block(
                tick.symbol,
                "POST_SPIKE_STRENGTH_VETO",
                score=snap.score,
                effective_min_score=_profile_min_score,
                side=snap.side,
                regime=snap.regime,
                hurst=_eval_hurst,
                score_breakdown=snap.score_breakdown,
            )
            self._record_decision(
                symbol=tick.symbol,
                allowed=False,
                side=snap.side,
                score=snap.score,
                reason=_post_spike_reason,
                extra={
                    "score_breakdown": snap.score_breakdown,
                    "regime": snap.regime,
                    "dynamic_cfg": _dyn_cfg,
                },
            )
            self._spike_enrich(
                tick.symbol,
                bot_entered=False,
                block_reason=_post_spike_reason,
                score=snap.score,
            )
            _pssv_setup = str(snap.score_breakdown.get("setup_type") or "")
            _pssv_grade = str(snap.score_breakdown.get("execution_grade") or "")
            if _pssv_setup in ("SMC_FVG", "EMA200_SPIKE") and _pssv_grade in ("A", "B") and snap.score >= 7.0:
                self._ghost_logger.add(
                    symbol=tick.symbol, price=float(tick.price), side=str(snap.side or ""),
                    score_raw=float(snap.score), gate="POST_SPIKE_STRENGTH_VETO",
                    setup_type=_pssv_setup, grade=_pssv_grade,
                    scarcity=str(snap.score_breakdown.get("scarcity_state") or ""),
                    imm_state=str(snap.score_breakdown.get("spike_imminence_state") or ""),
                    imm_score=float(snap.score_breakdown.get("spike_imminence_score") or 0.0),
                )
            return

        # Per-profile stake cap — overrides global DERIV_MAX_STAKE_USDT for symbols
        # that require reduced exposure (e.g. R_75 during re-validation).
        _profile_stake_max = float(_asset_profile.get("stake_max_usdt", float("inf")))
        if snap.suggested_stake_usdt > _profile_stake_max:
            _LOGGER.debug(
                "[deriv-daemon] %s: stake capped by profile %.2f → %.2f (stake_max_usdt)",
                tick.symbol, snap.suggested_stake_usdt, _profile_stake_max,
            )
            snap.suggested_stake_usdt = round(_profile_stake_max, 2)

        # Per-symbol Hurst regime gate — 3 zones (ASSET_INTEL_PROFILES)
        # Spike markets (BOOM/CRASH) are exempt — their edge is structural, not
        # Hurst-based.  For volatility indices:
        #   Zone A  H < (min_hurst − 0.08)    hard reject (no statistical edge)
        #   Zone B  H ∈ [floor, min_hurst)     soft: linear score penalty + size cut
        #   Zone C  H ≥ min_hurst + 0.06       high-confidence: mild size boost (+15%)
        _profile_min_hurst = float(_asset_profile.get("min_hurst", 0.0))
        _asset_type = _asset_profile.get("type", "volatility")
        if _profile_min_hurst > 0 and _asset_type not in ("spike_boom", "spike_crash"):
            _obs_hurst = float(snap.score_breakdown.get("hurst", 0.5))
            _h_hard_floor = max(0.0, _profile_min_hurst - 0.08)
            if _obs_hurst < _h_hard_floor:
                # Zone A — far below any useful regime, hard reject
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score,
                    reason=f"HURST_GATE[A-hard]: H={_obs_hurst:.3f} < floor={_h_hard_floor:.3f}",
                    extra={"score_breakdown": snap.score_breakdown, "regime": snap.regime},
                )
                return
            elif _obs_hurst < _profile_min_hurst:
                # Zone B — borderline: graduated penalty so we don't hard-kill
                # setups that are close to the threshold, but we de-risk them.
                # At the hard floor  → −1.5 score pts, size×0.60
                # At the threshold   → 0 penalty,       size×1.00
                _zone_width = max(0.001, _profile_min_hurst - _h_hard_floor)
                _zone_pos   = (_profile_min_hurst - _obs_hurst) / _zone_width  # 1→floor, 0→threshold
                _h_score_pen = round(-1.5 * _zone_pos, 3)
                _h_size_mult = round(1.0 - 0.40 * _zone_pos, 3)
                snap.score = round(max(0.0, snap.score + _h_score_pen), 3)
                snap.suggested_stake_usdt = round(
                    max(1.00, snap.suggested_stake_usdt * _h_size_mult), 2
                )
                snap.score_breakdown["hurst_zone"] = "B-soft"
                snap.score_breakdown["hurst_zone_pen"] = _h_score_pen
                snap.score_breakdown["hurst_zone_size"] = _h_size_mult
                # Re-check allowance: penalty may push score below profile minimum
                if snap.score < snap.effective_min_score:
                    snap.allowed = False
                    self._log_entry_block(
                        tick.symbol, "HURST_GATE_B_SOFT",
                        score=snap.score, effective_min_score=snap.effective_min_score,
                        side=snap.side, regime=snap.regime, hurst=_obs_hurst,
                        score_breakdown=snap.score_breakdown,
                    )
                    self._record_decision(
                        symbol=tick.symbol, allowed=False, side=snap.side,
                        score=snap.score,
                        reason=(
                            f"HURST_GATE[B-soft]: H={_obs_hurst:.3f} pen={_h_score_pen:.2f}"
                            f" → score={snap.score:.2f} < min={snap.effective_min_score:.2f}"
                        ),
                        extra={"score_breakdown": snap.score_breakdown, "regime": snap.regime},
                    )
                    return
                _LOGGER.debug(
                    "[deriv-daemon] %s HURST_ZONE_B: H=%.3f pen=%.2f size×%.2f score→%.2f",
                    tick.symbol, _obs_hurst, _h_score_pen, _h_size_mult, snap.score,
                )
            elif _obs_hurst >= _profile_min_hurst + 0.06:
                # Zone C — high-persistence: reward with up to +15% size
                _h_boost = min(1.15, 1.0 + 0.10 * min(1.5, (_obs_hurst - (_profile_min_hurst + 0.06)) / 0.10))
                snap.suggested_stake_usdt = round(snap.suggested_stake_usdt * _h_boost, 2)
                snap.score_breakdown["hurst_zone"] = "C-boost"
                snap.score_breakdown["hurst_zone_size"] = round(_h_boost, 3)

        # ── Strategy-mode gate (ASSET_INTEL_PROFILES) ─────────────────────
        # Reject setups whose mode mismatches the profile's allowed mode:
        #   • "trend"       → block mean-reversion entries
        #   • "mean_revert" → block trend-only setups (but breakouts still
        #                     OK on hybrid). Trend gives a strong score so we
        #                     gate on the explicit mean_rev_mode flag instead.
        #   • "spike"       → only SMC or spike_hunter (already enforced by
        #                     the structural veto inside deriv_risk).
        #   • "hybrid"      → no extra gate.
        _strat = str(_asset_profile.get("strategy_mode", "hybrid")).lower()
        _mr_active = bool(snap.score_breakdown.get("mean_rev_mode"))
        _spike_entry = bool(snap.score_breakdown.get("spike_entry"))
        _allow_mr = bool(_asset_profile.get("allow_mean_reversion", True))
        if snap.allowed and _mr_active and not _allow_mr:
            self._log_entry_block(
                tick.symbol, f"STRATEGY_GATE_mean_rev_rejected_mode={_strat}",
                score=snap.score, effective_min_score=_profile_min_score,
                side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                score_breakdown=snap.score_breakdown,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score,
                reason=f"STRATEGY_GATE: mean_rev rejected (mode={_strat})",
                extra={
                    "score_breakdown": snap.score_breakdown,
                    "regime": snap.regime,
                    "profile": _asset_profile,
                },
            )
            self._spike_enrich(tick.symbol, bot_entered=False,
                               block_reason=f"strategy_gate_mean_rev:mode={_strat}")
            return
        # spike strategy gate REMOVED (2026-05-19, Phase9):
        # DerivRiskManager now handles no-FVG entries with Momentum Escape Valve
        # (C1 — Phase8).  A hard boolean block here was shadow-blocking valid
        # Tier-0 entries that the risk engine scored above effective_min after
        # applying the weighted 0.50/1.00 penalty.  The risk engine is the sole
        # authority on structural viability for BOOM/CRASH.  Removed to eliminate
        # the shadow-blocking desynchronisation described in Phase9 directive.

        # (Geo channel position gate was moved earlier — see BUG-C fix above,
        #  right after snap = self._risk.evaluate() — so that geo_gate is in
        #  score_breakdown for all _log_entry_block calls.)


        decision_extra = {
            "score_breakdown": snap.score_breakdown,
            "regime": snap.regime,
            "hurst_delta": snap.hurst_score_delta,
            "effective_min_score": snap.effective_min_score,
        }
        if not snap.allowed or snap.side is None:
            self._log_entry_block(
                tick.symbol,
                "; ".join(snap.reasons) if snap.reasons else "MATH_REJECTED",
                score=getattr(snap, "score", 0.0),
                effective_min_score=snap.effective_min_score,
                side=snap.side, regime=snap.regime, hurst=_eval_hurst,
                score_breakdown=snap.score_breakdown,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=getattr(snap, "score", 0.0),
                reason="; ".join(snap.reasons) if snap.reasons else "risk_rejected",
                extra=decision_extra,
            )
            self._spike_enrich(
                tick.symbol, bot_entered=False,
                block_reason="; ".join(snap.reasons) if snap.reasons else "risk_rejected",
                score=getattr(snap, "score", 0.0),
            )
            return

        # ═══════════════════════════════════════════════════════════════════
        # EXHAUSTION_TRIGGER — micro-momentum confirmation for FVG entries
        # After maturity opens, requires price momentum to have reversed
        # in the trade direction while inside the FVG zone. Prevents entering
        # the instant price touches the FVG (manipulation wick capture).
        # Skipped for math-override fast path (structural certainty signals).
        # Env vars: DERIV_EXHAUSTION_GATE_ENABLED  (default true)
        #           DERIV_EXHAUSTION_GATE_ATR_FRAC (default 0.10 = 10% of ATR)
        # ═══════════════════════════════════════════════════════════════════
        if (
            is_spike_market(tick.symbol)
            and not snap.hurst_ai_override
            and str(os.getenv("DERIV_EXHAUSTION_GATE_ENABLED", "true")).strip().lower()
               in ("1", "true", "yes", "on")
            and bool(snap.score_breakdown.get("fvg_active"))
            and str(snap.score_breakdown.get("setup_type") or "") == "SMC_FVG"
        ):
            _ex_fvg_top = snap.score_breakdown.get("fvg_top")
            _ex_fvg_bot = snap.score_breakdown.get("fvg_bottom")
            _ex_price = float(tick.price or 0.0)
            _ex_side = snap.side
            _ex_ticks = list(self._risk._ticks.get(tick.symbol, []))
            _ex_atr = float(snap.score_breakdown.get("atr_abs") or 0.0)
            if (
                _ex_fvg_top is not None and _ex_fvg_bot is not None
                and _ex_fvg_top > 0 and _ex_fvg_bot > 0
                and len(_ex_ticks) >= 6
                and _ex_atr > 0
                and _ex_price > 0
            ):
                _ex_zone_tol = _ex_atr * 0.5
                _ex_in_zone = (
                    (_ex_fvg_bot - _ex_zone_tol) <= _ex_price <= (_ex_fvg_top + _ex_zone_tol)
                )
                if _ex_in_zone:
                    _ex_delta_5t = _ex_ticks[-1] - _ex_ticks[-6]
                    _ex_min_move = _ex_atr * float(
                        os.getenv("DERIV_EXHAUSTION_GATE_ATR_FRAC", "0.10")
                    )
                    # Deriv sides: MULTDOWN (CRASH/SELL) and MULTUP (BOOM/BUY)
                    _ex_momentum_ok = (
                        (_ex_side == "MULTDOWN" and _ex_delta_5t <= -_ex_min_move)
                        or (_ex_side == "MULTUP" and _ex_delta_5t >= +_ex_min_move)
                    )
                    # HOT_REENTRY bypass: when the symbol just spiked (≤200t ago)
                    # and score is very high, BOOM spikes come from flat/declining
                    # delta — waiting for reversal means missing the spike entirely.
                    # Env: DERIV_EXHAUSTION_HOT_BYPASS_MIN_SCORE (default 9.0)
                    _ex_hot_bypass = (
                        bool(snap.score_breakdown.get("hot_reentry_fired"))
                        and float(snap.score or 0.0) >= float(
                            os.getenv("DERIV_EXHAUSTION_HOT_BYPASS_MIN_SCORE", "9.0") or "9.0"
                        )
                    )
                    if _ex_hot_bypass:
                        _LOGGER.info(
                            "[EXHAUSTION_GATE] HOT_BYPASS %s | score=%.2f delta5t=%.6f "
                            "hot_reentry_active → skip delta check",
                            tick.symbol, float(snap.score or 0.0), _ex_delta_5t,
                        )
                    if not _ex_momentum_ok and not _ex_hot_bypass:
                        _ex_key = f"{tick.symbol}:exhaustion_gate"
                        _ex_last = float(
                            self._dynamic_inactive_last_emit_ts.get(_ex_key) or 0.0
                        )
                        _ex_now = time.time()
                        if (_ex_now - _ex_last) >= 10.0:
                            self._dynamic_inactive_last_emit_ts[_ex_key] = _ex_now
                            _LOGGER.info(
                                "[EXHAUSTION_GATE] WAITING_REVERSAL %s | price=%.5f "
                                "zone=[%.5f,%.5f] side=%s delta5t=%.6f need%s%.6f",
                                tick.symbol,
                                _ex_price,
                                _ex_fvg_bot,
                                _ex_fvg_top,
                                _ex_side,
                                _ex_delta_5t,
                                "≤−" if _ex_side == "MULTDOWN" else "≥+",
                                _ex_min_move,
                            )
                        self._spike_enrich(
                            tick.symbol, bot_entered=False,
                            block_reason="exhaustion_gate_pending",
                        )
                        return

        # ═══════════════════════════════════════════════════════════════════
        # ANTI_RETRACE_GUARD — block entry when price is actively bouncing
        # against the trade direction post-spike (prevents eating the retrace).
        # Applies to ALL setups (TREND, EMA200_SPIKE, SMC_FVG).
        # Uses a self-normalizing range-based threshold: computes the 20-tick
        # price range and blocks only if net directional move > 50% of that range.
        # This avoids the ATR unit-scale mismatch and only blocks meaningful bounces.
        # Env: DERIV_ANTI_RETRACE_ENABLED (default true)
        #      DERIV_ANTI_RETRACE_RANGE_FRAC (default 0.50)
        # ═══════════════════════════════════════════════════════════════════
        if (
            is_spike_market(tick.symbol)
            and not snap.hurst_ai_override
            and str(os.getenv("DERIV_ANTI_RETRACE_ENABLED", "true")).strip().lower()
               in ("1", "true", "yes", "on")
            and snap.side
        ):
            _ar_ticks = list(self._risk._ticks.get(tick.symbol, []))
            if len(_ar_ticks) >= 21:
                _ar_window = _ar_ticks[-21:]
                _ar_delta_20t = _ar_window[-1] - _ar_window[0]
                _ar_range_20t = max(_ar_window) - min(_ar_window)
                _ar_frac = float(
                    os.getenv("DERIV_ANTI_RETRACE_RANGE_FRAC", "0.50") or "0.50"
                )
                _ar_threshold = _ar_range_20t * _ar_frac if _ar_range_20t > 0 else 0.0
                _ar_bounce = (
                    _ar_threshold > 0
                    and (
                        (snap.side == "MULTDOWN" and _ar_delta_20t > +_ar_threshold)
                        or (snap.side == "MULTUP" and _ar_delta_20t < -_ar_threshold)
                    )
                )
                _ar_master_key_min = float(
                    os.getenv("DERIV_ANTI_RETRACE_MASTER_KEY_MIN_SCORE", "8.5") or "8.5"
                )
                _ar_hot_bypass = (
                    (
                        bool(snap.score_breakdown.get("hot_reentry_fired"))
                        and float(snap.score or 0.0) >= float(
                            os.getenv("DERIV_ANTI_RETRACE_HOT_BYPASS_MIN_SCORE", "8.0") or "8.0"
                        )
                    )
                    or (
                        is_spike_market(tick.symbol)
                        and float(snap.score or 0.0) >= _ar_master_key_min
                    )
                    or (
                        # For BOOM/CRASH in Z2 extreme scarcity, price retracing
                        # against trade direction IS the expected pre-spike signature.
                        # Anti-retrace would block every good BOOM setup.
                        _is_bc_bias
                        and _is_z2
                    )
                )
                if _ar_bounce:
                    _ar_key = f"{tick.symbol}:anti_retrace"
                    _ar_last = float(
                        self._dynamic_inactive_last_emit_ts.get(_ar_key) or 0.0
                    )
                    _ar_now = time.time()
                    if (_ar_now - _ar_last) >= 10.0:
                        self._dynamic_inactive_last_emit_ts[_ar_key] = _ar_now
                        _LOGGER.info(
                            "[ANTI_RETRACE_GUARD] %s %s side=%s "
                            "delta20t=%+.6f threshold=%.6f (range=%.6f×%.2f)",
                            "HOT_BYPASS" if _ar_hot_bypass else "BOUNCE_ACTIVE",
                            tick.symbol, snap.side,
                            _ar_delta_20t, _ar_threshold, _ar_range_20t, _ar_frac,
                        )
                    if _ar_hot_bypass:
                        pass  # allow entry — BOOM/CRASH spikes come FROM a down/up move
                    else:
                        _ar_setup = str(snap.score_breakdown.get("setup_type") or "")
                        _ar_grade = str(snap.score_breakdown.get("execution_grade") or "")
                        if (
                            _ar_setup in ("SMC_FVG", "EMA200_SPIKE")
                            and _ar_grade in ("A", "B")
                            and snap.score >= 7.0
                        ):
                            self._ghost_logger.add(
                                symbol=tick.symbol, price=float(tick.price),
                                side=str(snap.side or ""),
                                score_raw=float(snap.score),
                                gate="ANTI_RETRACE_GUARD",
                                setup_type=_ar_setup, grade=_ar_grade,
                                scarcity=str(snap.score_breakdown.get("scarcity_state") or ""),
                                imm_state=str(snap.score_breakdown.get("spike_imminence_state") or ""),
                                imm_score=float(snap.score_breakdown.get("spike_imminence_score") or 0.0),
                            )
                        self._spike_enrich(
                            tick.symbol, bot_entered=False,
                            block_reason="anti_retrace_bounce",
                        )
                        return


        # ── RNG PROBABILITY SCORE ─────────────────────────────────────────────────
        # Replaces binary Trifecta gate.  Deriv synthetics are Poisson-distributed
        # RNG: no entry is ever "perfect" — we need statistical tilt, not certainty.
        # Weighted scoring: kinetic=35 + fvg_bos=30 + vision=20 + scarcity=15 = 100.
        # Cooldown multiplier: ×0.3 when burst retrace < 50% (energy not reset).
        # Gate: DERIV_PROBABILITY_THRESHOLD (default 65) — blocks low-conviction setups.
        _rng_vision = bool(
            (self._last_vision_context or {})
            .get(tick.symbol.upper(), {})
            .get("zona_liquidez_macro", False)
        )
        _rng_cooldown_active = (
            str(snap.score_breakdown.get("setup_type", "")) == "POST_SPIKE_COOLDOWN"
        )

        # ── FASE 2: POST_CLUSTER_EXHAUSTION — 3+ spikes in 300s + overextension ─
        # When the market just fired a spike cluster AND price is overextended,
        # kinetic energy is spent — suppress kinetic contribution to RNG.
        _cluster_exhausted = False
        try:
            _ce_spikes = self._risk.get_spike_count_recent(tick.symbol, 300.0)
            _ce_ch_pos = abs(float(snap.score_breakdown.get("geo_channel_pos") or 0.0))
            if _ce_spikes >= 3 and _ce_ch_pos > 0.7:
                _cluster_exhausted = True
                snap.score_breakdown["post_cluster_exhaustion"] = True
                snap.score_breakdown["post_cluster_spike_count"] = _ce_spikes
                _LOGGER.info(
                    "[PIPELINE] POST_CLUSTER_EXHAUSTION %s | spikes_300s=%d ch_pos=%.3f"
                    " → kinetic suppressed in RNG",
                    tick.symbol, _ce_spikes, _ce_ch_pos,
                )
        except Exception:
            pass

        _rng_kinetic = float(snap.score_breakdown.get("kinetic_compressed") or 0.0)
        if _cluster_exhausted:
            _rng_kinetic = 0.0

        # ── FASE 4: TRIPLE LOCK — Implied Structure Bonus ──────────────────────
        # Substitutes the missing +30 FVG pts ONLY when three independent
        # conditions simultaneously confirm that physical RNG tension is extreme.
        # This prevents gifting structural points just because time passed.
        #
        #   Condition A: FVG truly absent in the (now dynamic) scan window.
        #   Condition B: Extreme pressure — Z≥2.5 (deep SECO) OR elapsed_s ≥ limit.
        #   Condition C: Kinetic giro is active (compression score > 0).
        #                If price is free-falling fast, kinetic=0 → candado OFF.
        _tl_fvg_absent  = not bool(snap.score_breakdown.get("fvg_active", False))
        _tl_z_val       = float(snap.score_breakdown.get("scarcity_ratio") or 0.0)
        _tl_elapsed_s   = float(snap.score_breakdown.get("scarcity_elapsed_s") or 0.0)
        _tl_sym_limit   = float(EXTREME_TENSION_LIMITS_SEC.get(tick.symbol.upper(), 2000))
        _tl_kinetic     = float(snap.score_breakdown.get("kinetic_compressed") or 0.0)
        _tl_cond_a      = _tl_fvg_absent
        _tl_cond_b      = _tl_z_val >= 2.5 or (_tl_sym_limit > 0 and _tl_elapsed_s >= _tl_sym_limit)
        _tl_cond_c      = _tl_kinetic > 0 and not _cluster_exhausted
        _triple_lock_fired = _is_bc_bias and _tl_cond_a and _tl_cond_b and _tl_cond_c
        if _triple_lock_fired:
            snap.score_breakdown["market_context"]     = "EXTREME_OVERPRESSURE_KINETIC_LOCKED"
            snap.score_breakdown["triple_lock_fired"]  = True
            snap.score_breakdown["triple_lock_z"]      = round(_tl_z_val, 2)
            snap.score_breakdown["triple_lock_elapsed"] = round(_tl_elapsed_s, 0)
            snap.score_breakdown["triple_lock_kinetic"] = round(_tl_kinetic, 3)
            _LOGGER.info(
                "[TRIPLE_LOCK] %s FIRED — implied +30 structural pts | "
                "Z=%.2f elapsed=%.0fs/%.0fs kinetic=%.3f | "
                "condA(fvg_absent)=%s condB(pressure)=%s condC(kinetic)=%s",
                tick.symbol, _tl_z_val, _tl_elapsed_s, _tl_sym_limit, _tl_kinetic,
                _tl_cond_a, _tl_cond_b, _tl_cond_c,
            )
        elif _is_bc_bias and _tl_cond_a and (_tl_cond_b or _tl_cond_c):
            _tl_missing = []
            if not _tl_cond_b:
                _tl_missing.append(f"pressure_low(Z={_tl_z_val:.2f} elapsed={_tl_elapsed_s:.0f}s/{_tl_sym_limit:.0f}s)")
            if not _tl_cond_c:
                _tl_missing.append(f"kinetic_absent(score={_tl_kinetic:.3f})")
            snap.score_breakdown["triple_lock_blocked"] = _tl_missing

        _rng_prob, _rng_missing = _calculate_rng_probability(
            kinetic_score=_rng_kinetic,
            fvg_bos_validated=bool(snap.score_breakdown.get("fvg_bos_validated", False)),
            zona_liquidez_macro=_rng_vision,
            scarcity_state=str(snap.score_breakdown.get("scarcity_state") or ""),
            scarcity_ratio=float(snap.score_breakdown.get("scarcity_ratio") or 0.0),
            cooldown_active=_rng_cooldown_active,
            implied_structure=_triple_lock_fired,
        )
        _rng_threshold = int(float(os.getenv("DERIV_PROBABILITY_THRESHOLD", "65") or "65"))

        snap.score_breakdown["rng_probability"]  = _rng_prob
        snap.score_breakdown["rng_missing"]      = _rng_missing
        snap.score_breakdown["rng_threshold"]    = _rng_threshold

        # Keep trifecta fields for UI chip display
        _trifecta_vision   = _rng_vision
        _trifecta_kinetic  = float(snap.score_breakdown.get("kinetic_compressed") or 0.0) >= 0.5
        _trifecta_scarcity = str(snap.score_breakdown.get("scarcity_state") or "") in ("LISTO", "VENCIDO", "SECO")
        _trifecta_fvg_bos  = bool(snap.score_breakdown.get("fvg_bos_validated", False))
        _trifecta_met      = _trifecta_vision and _trifecta_kinetic and _trifecta_scarcity and _trifecta_fvg_bos
        snap.score_breakdown["trifecta_met"]      = _trifecta_met
        snap.score_breakdown["trifecta_vision"]   = _trifecta_vision
        snap.score_breakdown["trifecta_kinetic"]  = _trifecta_kinetic
        snap.score_breakdown["trifecta_scarcity"] = _trifecta_scarcity
        snap.score_breakdown["trifecta_fvg_bos"]  = _trifecta_fvg_bos

        # Persist rng + trifecta to _last_fvg_state so market context file picks them up.
        if _cache_sym in self._last_fvg_state:
            self._last_fvg_state[_cache_sym].update({
                "rng_probability":  _rng_prob,
                "rng_threshold":    _rng_threshold,
                "rng_missing":      _rng_missing,
                "trifecta_met":     _trifecta_met,
                "trifecta_vision":  _trifecta_vision,
                "trifecta_kinetic": _trifecta_kinetic,
                "trifecta_scarcity":_trifecta_scarcity,
                "trifecta_fvg_bos": _trifecta_fvg_bos,
            })

        _LOGGER.debug(
            "[RNG_PROB] %s prob=%d/100 threshold=%d trifecta=%s missing=%s",
            tick.symbol, _rng_prob, _rng_threshold,
            "✓" if _trifecta_met else "✗",
            _rng_missing or "none",
        )

        if is_spike_market(tick.symbol) and _rng_prob < _rng_threshold:
            _sb_rng = snap.score_breakdown if isinstance(snap.score_breakdown, dict) else {}
            self._log_entry_block(
                tick.symbol, "RNG_PROBABILITY_GATE",
                score=snap.score, effective_min_score=snap.effective_min_score,
                side=snap.side, regime=snap.regime,
                hurst=_eval_hurst,
                score_breakdown=_sb_rng,
            )
            _LOGGER.info(
                "[RNG_PROBABILITY_GATE] %s BLOCKED prob=%d/100 < threshold=%d | missing=%s",
                tick.symbol, _rng_prob, _rng_threshold, _rng_missing,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score,
                reason=(
                    f"RNG_PROBABILITY_GATE: {_rng_prob}/100 < {_rng_threshold}"
                    f" | missing={_rng_missing}"
                ),
                extra={**decision_extra, "rng_probability": _rng_prob, "rng_missing": _rng_missing},
            )
            self._spike_enrich(
                tick.symbol, bot_entered=False,
                block_reason=f"rng_probability:{_rng_prob}/100<{_rng_threshold}",
            )
            # MASTER_KEY: preview RNG ≥ threshold means the setup WAS validated earlier
            # in this same tick cycle (FVG may have expired milliseconds later).
            # Allow bypass — the full score ≥ threshold AND structural check already passed.
            if _master_key and _mk_rng >= _rng_threshold:
                _LOGGER.info(
                    "[PIPELINE] GATE_BYPASS MASTER_KEY %s | gate=RNG_PROBABILITY_GATE"
                    " preview_rng=%d≥%d actual=%d → continuing to LLM",
                    tick.symbol, _mk_rng, _rng_threshold, _rng_prob,
                )
                snap.score_breakdown["master_key_bypass"] = "RNG_PROBABILITY_GATE"
                snap.score_breakdown["master_key_rng"] = _mk_rng
                snap.score_breakdown["rng_actual_at_bypass"] = _rng_prob
            else:
                return

        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 2b — HARD MATH OVERRIDE FAST PATH
        # If the risk engine flagged a mathematical certainty (SMC FVG mitigation,
        # Hurst-trend confluence, or micro-scalp band touch), we BYPASS the AI
        # entirely. The order fires immediately and the signal cooldown is stamped.
        # The AI veto can NEVER block this path — that is intentional by design.
        # ═══════════════════════════════════════════════════════════════════
        if snap.hurst_ai_override:
            # Stamp signal cooldown for ALL override types (trend_math,
            # smc_confluence, micro_scalp_mr) to prevent tick-by-tick spam.
            self._signal_cooldown[tick.symbol] = self._risk.get_tick_count(tick.symbol)
            self._cooldown.mark(tick.symbol)

            _is_mean_rev_ov = bool(snap.score_breakdown.get("mean_rev_mode"))
            _is_boom_crash_ov = any(k in tick.symbol.upper() for k in ("BOOM", "CRASH"))
            snap.suggested_stake_usdt = self._apply_edge_quality_sizing(
                tick.symbol,
                snap,
                analysis=None,
                dynamic_cfg=_dyn_cfg,
                dynamic_active=_dyn_active,
                profile_min_score=_profile_min_score,
                profile_stake_cap=_profile_stake_max,
            )
            _sb_ov = snap.score_breakdown if isinstance(snap.score_breakdown, dict) else {}
            snap.suggested_stake_usdt = self._enforce_profile_stake_target(
                symbol=tick.symbol,
                stake=snap.suggested_stake_usdt,
                profile=_asset_profile,
                sb=_sb_ov,
            )
            # BOOM/CRASH spike-hunter: use a wide structural SL so the position
            # survives the accumulation window before the spike hits.
            _sl_pct_ov = (
                _BOOM_CRASH_SL_PCT if _is_boom_crash_ov
                else (0.004 if _is_mean_rev_ov else self._settings.stop_loss_pct)
            )
            # Per-profile override: some symbols need a wider SL to clear the
            # broker minimum floor (e.g. R_75 with stop_loss_pct_override=0.36).
            _sl_pct_ov = float(_asset_profile.get("stop_loss_pct_override") or _sl_pct_ov)
            # Phase 16: env-var USD-based SL override for R_50/R_75/R_100.
            # Converts $USD target → pct so deriv_client.buy() gets the right
            # absolute SL regardless of how stop_loss_pct_override is set.
            _sym_sl_ov = tick.symbol.upper()
            _sl_usd_cfg_ov = (
                self._settings.r50_sl_usd  if "R_50"  in _sym_sl_ov else
                self._settings.r75_sl_usd  if "R_75"  in _sym_sl_ov else
                self._settings.r100_sl_usd if "R_100" in _sym_sl_ov else 0.0
            )
            if not _is_boom_crash_ov and _sl_usd_cfg_ov > 0:
                _eff_stake_ov = float(snap.suggested_stake_usdt or 1.50)
                _sl_pct_ov = round(_sl_usd_cfg_ov / max(_eff_stake_ov, 0.01), 4)
                _LOGGER.info(
                    "[SL_WIDE] %s sl_usd=%.2f → sl_pct=%.4f (stake=%.2f) [override path]",
                    tick.symbol, _sl_usd_cfg_ov, _sl_pct_ov, _eff_stake_ov,
                )
            _tp_pct_ov = (
                _BOOM_CRASH_TP_PCT if _is_boom_crash_ov
                else (0.004 if _is_mean_rev_ov else self._settings.take_profit_pct)
            )
            _is_spike_ov = bool(snap.score_breakdown.get("spike_entry"))
            # Adaptive timeout: trending BOOM/CRASH gets 600s, others 450s.
            # Profile enforcement in deriv_trader overrides this; ATR extension
            # is stored in score_breakdown so deriv_trader can add it on top.
            _max_hold_ov = (
                float(adaptive_max_hold(tick.symbol, snap.regime))
                if (_is_spike_ov or _is_boom_crash_ov)
                else 0.0
            )
            # ATR dynamic extension (stored in score_breakdown; applied in deriv_trader)
            _atr_ext_ov = _compute_atr_hold_extension(
                tick.symbol,
                float(snap.score_breakdown.get("atr_abs", 0.0)),
                list(self._risk._atr_history.get(tick.symbol, [])),
                _max_hold_ov,
            )
            if _atr_ext_ov > 0:
                snap.score_breakdown["atr_hold_extension"] = _atr_ext_ov
                _LOGGER.info(
                    "[ATR_HOLD_EXT] %s atr_abs=%.5f ext=+%.0fs (override path)",
                    tick.symbol, snap.score_breakdown.get("atr_abs", 0), _atr_ext_ov,
                )
            # Telemetry: stamp entry tick count and spread so closed records can
            # compute ticks_held and expose spread_at_entry without re-parsing logs.
            snap.score_breakdown.setdefault("entry_tick_count", self._risk.get_tick_count(tick.symbol))
            snap.score_breakdown.setdefault("spread_pct_at_entry", round(spread_pct, 6))
            payload_override: dict[str, Any] = {
                "broker": "deriv",
                "symbol": tick.symbol,
                "side": snap.side,
                "stake_usdt": snap.suggested_stake_usdt,
                "multiplier": snap.suggested_multiplier,
                "stop_loss_pct": _sl_pct_ov,
                "take_profit_pct": _tp_pct_ov,
                "score_breakdown": snap.score_breakdown,
                "max_hold_seconds": _max_hold_ov,
                "_analyst_context": {"hurst_ai_override": True},
            }
            self._record_decision(
                symbol=tick.symbol, allowed=True, side=snap.side,
                score=snap.score,
                reason=f"GO [MATH_OVERRIDE] score={snap.score:.2f} regime={snap.regime}",
                extra={**decision_extra, "hurst_ai_override": True},
            )
            # ── Anti-slippage: final spread veto before broker WS ──────────
            if spread_pct > _EXEC_MAX_SPREAD_PCT:
                _LOGGER.warning(
                    "[deriv-daemon] ENTRY_VETO: Spread of %.5f exceeds limit of %.5f (override path)",
                    spread_pct, _EXEC_MAX_SPREAD_PCT,
                )
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score,
                    reason=f"SPREAD_VETO: {spread_pct:.5f} > {_EXEC_MAX_SPREAD_PCT:.5f}",
                    extra={**decision_extra, "spread_pct": spread_pct},
                )
                self._spike_enrich(tick.symbol, bot_entered=False,
                                   block_reason=f"spread_veto:{spread_pct:.5f}")
                return
            # ── Observation window (BOOM markets only, Phase 31) ───────────
            _obs_sec_ov = int(_asset_profile.get("observation_window_sec", 0))
            if _obs_sec_ov > 0:
                _entry_ts_ov = time.monotonic()
                if await self._obs_window_check(tick.symbol, _obs_sec_ov, _entry_ts_ov):
                    self._record_decision(
                        symbol=tick.symbol, allowed=False, side=snap.side,
                        score=snap.score, reason=f"OBS_WINDOW_BLOCKED[override]: spike during {_obs_sec_ov}s",
                        extra={**decision_extra, "obs_sec": _obs_sec_ov},
                    )
                    self._spike_enrich(tick.symbol, bot_entered=False,
                                       block_reason=f"obs_window_blocked:{_obs_sec_ov}s")
                    return
            self._counters["orders_sent"] += 1
            try:
                result = await self._router.route_order(payload_override)
                # Phase 36: symbol_already_open means execute() blocked the buy
                # (pending entry or open contract) — not an actual order.
                if (result or {}).get("status") == "symbol_already_open":
                    self._counters["orders_sent"] -= 1
                    _LOGGER.info(
                        "[deriv-daemon] ORDER BLOCKED %s [MATH_OVERRIDE]: symbol_already_open",
                        tick.symbol,
                    )
                    self._spike_enrich(tick.symbol, bot_entered=False,
                                       block_reason="symbol_already_open")
                else:
                    self._counters["orders_ok"] += 1
                    _LOGGER.info(
                        "[deriv-daemon] ORDER %s | score=%.2f [MATH_OVERRIDE] | %s",
                        tick.symbol, snap.score, result,
                    )
                    self._spike_enrich(tick.symbol, bot_entered=True, score=snap.score)
            except OrderRouterError as exc:
                self._counters["orders_failed"] += 1
                _LOGGER.warning("[deriv-daemon] router rejected (override): %s", exc)
                self._spike_enrich(tick.symbol, bot_entered=False, block_reason="order_router_error")
            except DerivClientError as exc:
                self._counters["orders_failed"] += 1
                _LOGGER.warning("[deriv-daemon] broker rejected order %s (override): %s", tick.symbol, exc)
                self._spike_enrich(tick.symbol, bot_entered=False, block_reason="broker_rejected")
            except Exception:  # noqa: BLE001
                self._counters["orders_failed"] += 1
                _LOGGER.exception("[deriv-daemon] order pipeline crashed (override, suppressed)")
                self._spike_enrich(tick.symbol, bot_entered=False, block_reason="order_crashed")
        # BLOCK 3 — AI GATE (only reached when NO hard math override fired)
        # Runs cached LLM analysis (TTL 15 min). If AI vetoes, reject.
        # If AI approves (or is skipped/errored), execute the order.
        # ═══════════════════════════════════════════════════════════════════
        try:
            analysis = await self._analyst.analyze(
                symbol=tick.symbol,
                score=snap.score,
                side=snap.side,
                score_breakdown=snap.score_breakdown,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-daemon] analyst error for %s: %s — proceeding", tick.symbol, exc)
            analysis = None

        if analysis is not None and not analysis.ai_approved and not analysis.ai_skipped:
            _probe_ok, _probe_reason = self._allow_ai_veto_recovery_probe(
                tick.symbol,
                snap,
                analysis,
            )
            if not _probe_ok:
                reason = f"AI_VETO: {analysis.ai_reason} (conf={analysis.ai_confidence:.2f})"
                self._log_entry_block(
                    tick.symbol, "AI_VETO",
                    score=snap.score, effective_min_score=snap.effective_min_score,
                    side=snap.side, regime=snap.regime,
                    hurst=analysis.hurst if analysis else _eval_hurst,
                    ai_veto=True, ai_confidence=analysis.ai_confidence,
                    ai_reason=analysis.ai_reason,
                    score_breakdown=snap.score_breakdown,
                )
                _LOGGER.warning(
                    "[AI VETO] Símbolo: %s | Score: %.2f | Conf: %.2f | Razón: %s",
                    tick.symbol, snap.score, analysis.ai_confidence, analysis.ai_reason,
                )
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score, reason=reason,
                    extra={
                        **decision_extra,
                        "hurst": analysis.hurst,
                        "autocorr": analysis.autocorr_lag1,
                        "vol_regime": analysis.vol_regime,
                        "ai_model": analysis.ai_model,
                    },
                )
                self._spike_enrich(
                    tick.symbol, bot_entered=False,
                    block_reason=f"AI_VETO: {analysis.ai_reason}",
                    score=snap.score,
                )
                return

            snap.score_breakdown["ai_veto_recovery_probe"] = True
            snap.score_breakdown["ai_veto_recovery_reason"] = _probe_reason
            snap.score_breakdown["ai_veto_reason"] = str(analysis.ai_reason or "")
            snap.score_breakdown["ai_veto_confidence"] = round(
                float(analysis.ai_confidence or 0.0),
                4,
            )
            _LOGGER.info(
                "[AI-RECOVERY-PROBE] %s bypassing AI veto (%s)",
                tick.symbol,
                _probe_reason,
            )

        # ── Build order payload (AI-approved path) ─────────────────────────
        _is_mean_rev = bool(snap.score_breakdown.get("mean_rev_mode"))
        _is_spike_entry = bool(snap.score_breakdown.get("spike_entry"))
        _is_boom_crash = any(k in tick.symbol.upper() for k in ("BOOM", "CRASH"))
        snap.suggested_stake_usdt = self._apply_edge_quality_sizing(
            tick.symbol,
            snap,
            analysis=analysis,
            dynamic_cfg=_dyn_cfg,
            dynamic_active=_dyn_active,
            profile_min_score=_profile_min_score,
            profile_stake_cap=_profile_stake_max,
        )
        _sb_ai = snap.score_breakdown if isinstance(snap.score_breakdown, dict) else {}
        snap.suggested_stake_usdt = self._enforce_profile_stake_target(
            symbol=tick.symbol,
            stake=snap.suggested_stake_usdt,
            profile=_asset_profile,
            sb=_sb_ai,
        )
        # BOOM/CRASH spike-hunter: wide structural SL (same logic as override path)
        _sl_pct = (
            _BOOM_CRASH_SL_PCT if _is_boom_crash
            else (0.004 if _is_mean_rev else self._settings.stop_loss_pct)
        )
        # Per-profile override: some symbols need a wider SL to clear the
        # broker minimum floor (e.g. R_75 with stop_loss_pct_override=0.36).
        _sl_pct = float(_asset_profile.get("stop_loss_pct_override") or _sl_pct)
        # Phase 16: env-var USD-based SL override for R_50/R_75/R_100.
        _sym_sl = tick.symbol.upper()
        _sl_usd_cfg = (
            self._settings.r50_sl_usd  if "R_50"  in _sym_sl else
            self._settings.r75_sl_usd  if "R_75"  in _sym_sl else
            self._settings.r100_sl_usd if "R_100" in _sym_sl else 0.0
        )
        if not _is_boom_crash and _sl_usd_cfg > 0:
            _eff_stake = float(snap.suggested_stake_usdt or 1.50)
            _sl_pct = round(_sl_usd_cfg / max(_eff_stake, 0.01), 4)
            _LOGGER.info(
                "[SL_WIDE] %s sl_usd=%.2f → sl_pct=%.4f (stake=%.2f) [AI path]",
                tick.symbol, _sl_usd_cfg, _sl_pct, _eff_stake,
            )
        _tp_pct = (
            _BOOM_CRASH_TP_PCT if _is_boom_crash
            else (0.004 if _is_mean_rev else self._settings.take_profit_pct)
        )
        # Adaptive timeout: trending BOOM/CRASH gets 600s, others 450s.
        # Profile enforcement in deriv_trader overrides this; ATR extension
        # is stored in score_breakdown so deriv_trader can add it on top.
        _max_hold_sec = (
            float(adaptive_max_hold(tick.symbol, snap.regime))
            if (_is_spike_entry or _is_boom_crash)
            else 0.0
        )
        # ATR dynamic extension (stored in score_breakdown; applied in deriv_trader)
        _atr_ext = _compute_atr_hold_extension(
            tick.symbol,
            float(snap.score_breakdown.get("atr_abs", 0.0)),
            list(self._risk._atr_history.get(tick.symbol, [])),
            _max_hold_sec,
        )
        if _atr_ext > 0:
            snap.score_breakdown["atr_hold_extension"] = _atr_ext
            _LOGGER.info(
                "[ATR_HOLD_EXT] %s atr_abs=%.5f ext=+%.0fs (AI path)",
                tick.symbol, snap.score_breakdown.get("atr_abs", 0), _atr_ext,
            )
        # Telemetry: stamp entry tick count and spread so closed records can
        # compute ticks_held and expose spread_at_entry without re-parsing logs.
        snap.score_breakdown.setdefault("entry_tick_count", self._risk.get_tick_count(tick.symbol))
        snap.score_breakdown.setdefault("spread_pct_at_entry", round(spread_pct, 6))
        payload: dict[str, Any] = {
            "broker": "deriv",
            "symbol": tick.symbol,
            "side": snap.side,
            "stake_usdt": snap.suggested_stake_usdt,
            "multiplier": snap.suggested_multiplier,
            "stop_loss_pct": _sl_pct,
            "take_profit_pct": _tp_pct,
            "score_breakdown": snap.score_breakdown,
            "max_hold_seconds": _max_hold_sec,
            "_analyst_context": {
                "hurst": analysis.hurst if analysis else None,
                "autocorr_lag1": analysis.autocorr_lag1 if analysis else None,
                "vol_regime": analysis.vol_regime if analysis else None,
                "rolling_vol": analysis.rolling_vol if analysis else None,
                "trend_slope": analysis.trend_slope_1000 if analysis else None,
                "r_squared": analysis.r_squared_1000 if analysis else None,
                "ai_approved": analysis.ai_approved if analysis else None,
                "ai_confidence": analysis.ai_confidence if analysis else None,
                "ai_model": analysis.ai_model if analysis else None,
                "ai_reason": analysis.ai_reason if analysis else None,
                "hurst_ai_override": False,
            } if analysis else {},
        }
        # Manual-only symbols: run full pipeline (score, Hurst, AI) so the
        # operator console has live data, but never execute an actual trade.
        if tick.symbol.upper() in _MANUAL_ONLY_SYMBOLS:
            _LOGGER.info(
                "[PIPELINE] MANUAL_ONLY %s | score=%.2f effective_min=%.2f | side=%s — skipping entry",
                tick.symbol, snap.score, snap.effective_min_score, snap.side,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score,
                reason=f"MANUAL_ONLY: score={snap.score:.2f} gate={snap.effective_min_score:.2f}",
                extra={
                    **{k: v for k, v in (decision_extra or {}).items()},
                    "manual_only": True,
                },
            )
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="manual_only")
            return
        self._cooldown.mark(tick.symbol)

        ai_note = "" if analysis is None or analysis.ai_skipped else f" ai={analysis.ai_confidence:.2f}"
        hurst_note = f" H={analysis.hurst:.3f}" if analysis and analysis.hurst != 0.5 else ""
        _LOGGER.info(
            "[PIPELINE] ENTRY_ALLOWED %s | score=%.2f effective_min=%.2f | side=%s | "
            "regime=%s | H=%.3f | ai_conf=%.2f ai_model=%s | "
            "stake=%.2f mult=%s | geo=%s geo_pos=%s hd=%.2f smc=%s",
            tick.symbol, snap.score, snap.effective_min_score, snap.side,
            snap.regime, analysis.hurst if analysis else _eval_hurst,
            analysis.ai_confidence if analysis else 0.0,
            analysis.ai_model if analysis else "none",
            snap.suggested_stake_usdt, snap.suggested_multiplier,
            # geo_gate: +1.0 optimal / 0.0 border / -1.5 slight / -2.0 hard penalty
            f"{snap.score_breakdown['geo_gate']:+.1f}" if "geo_gate" in snap.score_breakdown else "n/a",
            f"{snap.score_breakdown['geo_channel_pos']:.3f}" if snap.score_breakdown.get("geo_channel_pos") is not None else "—",
            snap.score_breakdown.get("hd_bonus", 0.0),
            f"+{snap.score_breakdown['smc_bonus']:.2f}" if snap.score_breakdown.get("smc_bonus") else "—",
        )
        self._record_decision(
            symbol=tick.symbol, allowed=True, side=snap.side,
            score=snap.score,
            reason=f"GO{ai_note}{hurst_note} score={snap.score:.2f} regime={snap.regime}",
            extra={
                **decision_extra,
                "hurst": analysis.hurst if analysis else None,
                "autocorr": analysis.autocorr_lag1 if analysis else None,
                "vol_regime": analysis.vol_regime if analysis else None,
                "ai_confidence": analysis.ai_confidence if analysis else None,
                "hurst_ai_override": False,
            },
        )
        # ── Anti-slippage: final spread veto before broker WS ──────────────
        if spread_pct > _EXEC_MAX_SPREAD_PCT:
            _LOGGER.warning(
                "[deriv-daemon] ENTRY_VETO: Spread of %.5f exceeds limit of %.5f (AI path)",
                spread_pct, _EXEC_MAX_SPREAD_PCT,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score,
                reason=f"SPREAD_VETO: {spread_pct:.5f} > {_EXEC_MAX_SPREAD_PCT:.5f}",
                extra={**decision_extra, "spread_pct": spread_pct},
            )
            self._spike_enrich(tick.symbol, bot_entered=False,
                               block_reason=f"spread_veto:{spread_pct:.5f}")
            return
        # ── Observation window (BOOM markets only, Phase 31) ─────────────
        _obs_sec = int(_asset_profile.get("observation_window_sec", 0))
        if _obs_sec > 0:
            _entry_ts = time.monotonic()
            if await self._obs_window_check(tick.symbol, _obs_sec, _entry_ts):
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score, reason=f"OBS_WINDOW_BLOCKED: spike during {_obs_sec}s",
                    extra={**decision_extra, "obs_sec": _obs_sec},
                )
                self._spike_enrich(tick.symbol, bot_entered=False,
                                   block_reason=f"obs_window_blocked:{_obs_sec}s")
                return
        self._counters["orders_sent"] += 1
        try:
            result = await self._router.route_order(payload)
            # Phase 36: symbol_already_open means execute() blocked the buy
            # (pending entry or open contract) — not an actual order.
            if (result or {}).get("status") == "symbol_already_open":
                self._counters["orders_sent"] -= 1
                _LOGGER.info(
                    "[deriv-daemon] ORDER BLOCKED %s: symbol_already_open",
                    tick.symbol,
                )
                self._spike_enrich(tick.symbol, bot_entered=False,
                                   block_reason="symbol_already_open")
            else:
                self._counters["orders_ok"] += 1
                _LOGGER.info(
                    "[deriv-daemon] ORDER %s | score=%.2f%s%s | %s",
                    tick.symbol, snap.score, ai_note, hurst_note, result,
                )
                self._spike_enrich(tick.symbol, bot_entered=True, score=snap.score)
        except OrderRouterError as exc:
            self._counters["orders_failed"] += 1
            _LOGGER.warning("[deriv-daemon] router rejected: %s", exc)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="order_router_error")
        except DerivClientError as exc:
            self._counters["orders_failed"] += 1
            _LOGGER.warning("[deriv-daemon] broker rejected order %s: %s", tick.symbol, exc)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="broker_rejected")
        except Exception:  # noqa: BLE001
            self._counters["orders_failed"] += 1
            _LOGGER.exception("[deriv-daemon] order pipeline crashed (suppressed)")
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="order_crashed")

    # ─────────────────────────────────────────────────────────────────────────
    # Observation window — Phase 31
    # After signal fires for BOOM markets, wait obs_window_sec before placing
    # the order. If a spike is detected DURING the wait, the opportunity has
    # already materialised → cancel entry (we'd be entering after the fact).
    # If no spike occurs → proceed (spike still pending, good entry timing).
    # CRASH symbols deliberately have no obs_window (spikes too rapid).
    # ─────────────────────────────────────────────────────────────────────────
    async def _obs_window_check(self, symbol: str, obs_sec: int, entry_ts: float) -> bool:
        """Wait *obs_sec* seconds monitoring for a spike.

        Returns True (entry blocked) if a spike fires during the window.
        Returns False (proceed) if the window closes cleanly.
        """
        _end = time.monotonic() + obs_sec
        while time.monotonic() < _end:
            await asyncio.sleep(1)
            if self._risk.get_last_spike_ts(symbol) > entry_ts:
                _LOGGER.info(
                    "[OBS_WINDOW] %s spike detected during obs window (%ds) → entry cancelled"
                    " (spike already materialised)",
                    symbol, obs_sec,
                )
                return True  # blocked — too late
        _LOGGER.info(
            "[OBS_WINDOW] %s no spike in %ds → proceeding with entry",
            symbol, obs_sec,
        )
        return False  # clean — go ahead

    # ─────────────────────────────────────────────────────────────────────────
    # Balance refresh — polls Deriv API every 30 s and caches the result so
    # _write_status() can include it without blocking the sync writer.
    # ─────────────────────────────────────────────────────────────────────────
    async def _balance_refresh_loop(self) -> None:
        # Wait briefly for the WS to be established before the first call.
        await asyncio.sleep(5)
        while not self._stop_event.is_set():
            try:
                resp = await self._client.balance()
                # Deriv WS returns: {"balance": {"balance": 10000.0, "currency": "USD", ...}, ...}
                bal_obj = resp.get("balance") or {}
                if isinstance(bal_obj, dict):
                    self._balance_usd = float(bal_obj.get("balance") or 0.0)
                    self._balance_currency = str(bal_obj.get("currency") or "USD")
                    # Snapshot for rolling equity history
                    self._equity_history.append({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "balance": self._balance_usd,
                        "currency": self._balance_currency,
                    })
                    if len(self._equity_history) > 200:
                        self._equity_history = self._equity_history[-200:]
            except Exception:  # noqa: BLE001
                _LOGGER.debug("[deriv-daemon] balance fetch failed (non-fatal, will retry in 30s)")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Status writer — writes deriv_status.json every 10s so the frontend panel
    # can show connection status, account, balance, and PnL.
    # ─────────────────────────────────────────────────────────────────────────
    async def _status_writer_loop(self) -> None:
        self._write_status(connected=True)   # immediate first write
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            self._write_status(connected=not self._stop_event.is_set())

    def _write_status(self, *, connected: bool) -> None:
        path: Path = self._settings.status_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Per-symbol stats from the executor (closed contract history)
            per_sym = self._executor.get_per_symbol_stats()
            open_contracts = self._executor.get_open_contracts_for_status()
            symbol_tick_context = {}
            for _sym in self._settings.symbols:
                _tick_count = int(self._risk.get_tick_count(_sym) or 0)
                _last_spike_tick = int(self._risk.get_last_spike_tick_count(_sym) or 0)
                _ticks_since_spike = None
                if _tick_count > 0 and _last_spike_tick > 0 and _tick_count >= _last_spike_tick:
                    _ticks_since_spike = _tick_count - _last_spike_tick
                symbol_tick_context[_sym] = {
                    "tick_count": _tick_count,
                    "last_spike_tick_count": _last_spike_tick,
                    "ticks_since_last_spike": _ticks_since_spike,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            _dyn_symbols = set(self._settings.symbols) | set(self._dynamic_configs.keys())
            _export_cfg = {
                _sym: self.get_dynamic_config(_sym)
                for _sym in sorted(_dyn_symbols)
            }
            data = {
                "status": "running" if connected else "stopped",
                "connected": connected,
                "account_id": self._settings.account_id,
                "dry_run": self._settings.dry_run,
                "symbols": list(self._settings.symbols),
                "bankroll_usdt": self._settings.bankroll_usdt,
                "balance": self._balance_usd,
                "balance_currency": self._balance_currency,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                # ── Telemetría rica para auditoría visual ─────────────────
                "counters": dict(self._counters),
                "last_ticks": dict(self._last_ticks),
                "last_decisions": list(self._last_decisions[-self._status_decisions_max:]),
                "per_symbol_stats": per_sym,
                "symbol_tick_context": symbol_tick_context,
                "open_contracts_live": open_contracts,
                "equity_history": list(self._equity_history[-50:]),  # last 50 snapshots
                # ── Analyst statistics (Hurst, vol regime, AI gate) ───────
                "analyst_summary": self._analyst.get_history_summary(),
                # ── Dynamic runtime config (PostgreSQL bridge) ─────────────
                "dynamic_config": {
                    "enabled": self._dynamic_enabled,
                    "refresh_sec": self._dynamic_refresh_sec,
                    "entry_tick_only": self._entry_tick_only,
                    "last_refresh": self._dynamic_last_refresh,
                    "symbols_loaded": sorted(_export_cfg.keys()),
                    "configs": _export_cfg,
                },
                "multi_account": {
                    "mirror_enabled": bool(getattr(self._client, "mirror_enabled", False)),
                    "mirror_count": int(getattr(self._client, "mirror_count", 0)),
                    "mode": "principal_tracking_mirror_execution",
                },
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=None, separators=(",", ":")))
            tmp.replace(path)
            # BUG-B fix: also refresh deriv_open_contracts.json every cycle so the
            # frontend's derivOpenContracts has live floating_pnl (every 10s).
            oc_path = self._settings.open_contracts_file
            oc_tmp  = oc_path.with_suffix(oc_path.suffix + ".tmp")
            oc_tmp.write_text(json.dumps(open_contracts, indent=None, separators=(",", ":")))
            oc_tmp.replace(oc_path)
            # Export 5m OHLC candles for vision LLM analysis
            _cpath = path.parent / "deriv_candles_5m.json"
            _ctmp  = _cpath.with_suffix(".tmp")
            _cexp: dict = {}
            _all_syms = set(self._risk._candles_5m.keys()) | set(self._risk._candle_current.keys())
            for _csym in list(_all_syms):
                _closed = list(self._risk._candles_5m.get(_csym, []))
                _cur    = self._risk._candle_current.get(_csym)
                _all    = _closed + ([_cur] if _cur else [])
                if _all:
                    _cexp[_csym] = _all[-50:]
            if _cexp:
                _ctmp.write_text(json.dumps(_cexp, separators=(",", ":")))
                _ctmp.replace(_cpath)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[deriv-daemon] failed to write status file")

    # ─────────────────────────────────────────────────────────────────────────
    # Tick snapshot TTL purge — deletes rows from deriv_tick_snapshots older
    # than DERIV_SNAPSHOT_RETENTION_DAYS (default 7) once per day.
    # Prevents unbounded disk growth: R_100 at 1 tick/s = 86400 rows/day/symbol.
    # With 3 symbols + 7 days = ~1.8M rows — this purge keeps it at ~600k max.
    # ─────────────────────────────────────────────────────────────────────────
    async def _snapshot_ttl_loop(self) -> None:
        import os as _os
        _db_url = _os.getenv("DATABASE_URL", "")
        _retain = int(_os.getenv("DERIV_SNAPSHOT_RETENTION_DAYS", "7"))
        _batch  = int(_os.getenv("DERIV_SNAPSHOT_PURGE_BATCH", "5000"))
        if not _db_url:
            _LOGGER.debug("[deriv-ttl] DATABASE_URL not set — snapshot TTL loop idle")
            return

        # Wait 30s for WS to establish before first purge
        await asyncio.sleep(30)
        while not self._stop_event.is_set():
            try:
                import asyncpg  # optional dep
                conn = await asyncio.wait_for(asyncpg.connect(_db_url), timeout=8.0)
                try:
                    total = 0
                    # Batch deletion: delete _batch rows at a time with a brief yield
                    # between each batch to prevent PostgreSQL from holding a long-lived
                    # exclusive lock that blocks inserts from the live tick writer.
                    while True:
                        status = await conn.execute(
                            "DELETE FROM deriv_tick_snapshots "
                            "WHERE id IN ("
                            "    SELECT id FROM deriv_tick_snapshots "
                            "    WHERE captured_at < NOW() - INTERVAL '1 day' * $1 "
                            "    LIMIT $2"
                            ")",
                            _retain, _batch,
                        )
                        # asyncpg execute() returns a CommandComplete tag: "DELETE N"
                        n = int(status.split()[-1]) if status else 0
                        total += n
                        if n < _batch:
                            break  # last batch — done
                        # Yield briefly so other queries can proceed between batches
                        await asyncio.sleep(0.2)
                    _LOGGER.info(
                        "[deriv-ttl] purged %d tick_snapshots older than %d days (batch=%d)",
                        total, _retain, _batch,
                    )
                finally:
                    await conn.close()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[deriv-ttl] snapshot purge failed (non-fatal): %s", exc)
            # Run once per day
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=86400)
            except asyncio.TimeoutError:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Reaper — periodically settle closed contracts
    # ─────────────────────────────────────────────────────────────────────────
    async def _reaper_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                closed = await self._executor.reap_closed()
                for rec in closed:
                    # ── Slippage interceptor (TASK 4.1) ─────────────────────
                    # If a contract closed with zero duration AND negative pnl,
                    # treat it as a structural slippage event. Log critically
                    # and force-route via register_close so streak/lockout
                    # protection still applies.
                    _opened_ts = float(rec.get("opened_at_ts") or 0)
                    _closed_ts = float(rec.get("closed_at_ts") or 0)
                    _duration = max(0.0, _closed_ts - _opened_ts)
                    _pnl = float(rec.get("realized_pnl_usdt") or 0)
                    if _duration < 1.0 and _pnl < 0:
                        _LOGGER.critical(
                            "[SLIPPAGE_EXIT] symbol=%s contract=%s duration=%.2fs "
                            "pnl=%.4f side=%s — structural slippage event",
                            rec.get("symbol"), rec.get("contract_id"),
                            _duration, _pnl, rec.get("side"),
                        )
                    self._risk.register_close(_pnl, symbol=str(rec.get("symbol") or ""))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("[deriv-daemon] reaper iteration failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._settings.poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    def _build_telemetry(self) -> TelegramTelemetry | None:
        if not self._settings.telegram_enabled:
            return None
        try:
            return TelegramTelemetry(
                enabled=self._settings.telegram_enabled,
                logger=_LOGGER,
                bot_token=self._settings.telegram_bot_token,
                chat_id=self._settings.telegram_chat_id,
            )
        except Exception:  # noqa: BLE001 — telemetry must never crash the daemon
            _LOGGER.exception("[deriv-daemon] telegram telemetry init failed (continuing without)")
            return None


# ─── Entry point ─────────────────────────────────────────────────────────────
def _install_signal_handlers(daemon: DerivDaemon) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            # Windows fallback — handlers added via signal.signal().
            signal.signal(sig, lambda *_: daemon.request_stop())


async def _async_main() -> int:
    settings = load_deriv_settings()
    if not settings.api_token:
        _LOGGER.error(
            "[deriv-daemon] DERIV_API_TOKEN is missing in the .env — refusing to start."
        )
        return 2
    daemon = DerivDaemon(settings)
    _install_signal_handlers(daemon)
    await daemon.run()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        return asyncio.run(_async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
