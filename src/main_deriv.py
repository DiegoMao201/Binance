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
from src.execution.deriv_trader import DerivTradeExecutor
from src.execution.order_router import OrderRouter, OrderRouterError
from src.safety.deriv_risk import DerivRiskManager, HurstCalibrator, MacroHDCalibrator
from src.execution.position_manager import SYMBOL_RATCHET_PARAMS as _DPM_PARAMS
from src.strategies.deriv_signals import (
    adaptive_max_hold,
    extreme_mr_penalty,
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
        self._client = DerivClient(settings)
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
        _entry_tick_only_raw = os.getenv("DERIV_ENTRY_TICK_ONLY", "true").strip().lower()
        # Entry decisions must be tick-driven; spike history is telemetry/tuning only.
        self._entry_tick_only = _entry_tick_only_raw in {"1", "true", "yes", "on"}
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
        self._dynamic_configs: dict[str, dict[str, Any]] = {}
        self._dynamic_last_refresh: str | None = None
        self._dynamic_last_error_ts: float = 0.0
        self._analyst = DerivAnalyst(settings, self._client)
        self._router = OrderRouter(binance_executor=None, deriv_executor=self._executor)
        self._cooldown = _CooldownGate(ticks=max(60, int(settings.contract_duration_sec)), risk=self._risk)
        self._stop_event = asyncio.Event()
        self._velocity = TickVelocityAnalyzer()  # Module 2: tick acceleration detector
        # Telemetría in-memory (anillos) para que el frontend audite por qué
        # entra (o no entra) el bot. Se serializa junto al status cada 10s.
        self._last_ticks: dict[str, dict[str, Any]] = {}    # symbol → {price, ts}
        self._last_decisions: list[dict[str, Any]] = []     # ring (max 30)
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
        if len(self._last_decisions) > 30:
            self._last_decisions = self._last_decisions[-30:]
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

        # Highest priority: explicit per-symbol env overrides.
        by_symbol = self._post_spike_chase_block_sec_map.get(sym)
        if by_symbol is not None:
            resolved = by_symbol
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

        # Sniper safety floor for spike markets: never allow very-short
        # anti-chase windows that lead to late pursuit entries.
        if is_spike_market(sym):
            return max(self._post_spike_chase_min_ticks, resolved)

        return resolved

    def _passes_post_spike_strength_gate(
        self,
        *,
        symbol: str,
        snap: Any,
        profile: dict[str, Any] | None,
        dyn_cfg: dict[str, Any],
        dyn_active: bool,
        score_floor: float,
    ) -> tuple[bool, str]:
        """Allow post-spike entries only when continuation strength is explicit.

        Intent:
          - Prefer NO entry right after a spike unless the continuation is strong.
          - Reduce late/chasing entries that often end in spike_timeout.
        """
        if not self._entry_tick_only or not is_spike_market(symbol):
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

        # Keep safety invariant even if caller path changes in the future.
        if _elapsed < _chase_block:
            return (
                False,
                f"post_spike_chase_guard:{_elapsed:.0f}t<{_chase_block:.0f}t",
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

        _min_margin = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_SCORE_MARGIN", "0.60") or 0.60)
        _min_momentum = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_MOMENTUM_MIN", "1.10") or 1.10)
        _min_velocity = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_VELOCITY_MIN", "0.45") or 0.45)
        _min_atr = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_ATR_MIN", "1.00") or 1.00)
        _min_hd = float(os.getenv("DERIV_POST_SPIKE_STRENGTH_HD_MIN", "0.50") or 0.50)
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

        _dir_conflict = bool(
            _velocity_dir
            and snap.side
            and _velocity_dir != str(snap.side).upper()
        )
        _strong_enough = (len(_signals) >= _required_signals) and not _dir_conflict

        _sb["post_spike_strength_elapsed_ticks"] = round(_elapsed, 1)
        _sb["post_spike_strength_window_ticks"] = round(_strength_window, 1)
        # Backward-compatible keys for existing dashboards/parsers.
        _sb["post_spike_strength_elapsed_sec"] = round(_elapsed, 1)
        _sb["post_spike_strength_window_sec"] = round(_strength_window, 1)
        _sb["post_spike_strength_signals"] = list(_signals)
        _sb["post_spike_strength_required"] = _required_signals
        _sb["post_spike_strength_ok"] = _strong_enough

        if _strong_enough:
            return True, ""

        _reason = (
            "post_spike_strength_veto:"
            f"{_elapsed:.0f}t<{_strength_window:.0f}t "
            f"signals={len(_signals)}/{_required_signals} "
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

    def get_dynamic_config(self, symbol: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return in-memory dynamic config for a symbol (safe fallback if DB unavailable)."""
        _sym = symbol.upper()
        _profile = profile or get_asset_profile(_sym)
        _default_policy = self._multispike_policy_for_regime("NORMAL")
        _default = {
            "symbol": _sym,
            "market_regime": "NORMAL",
            "spike_pre_filter_target": int(_profile.get("spike_min_post_sec", 0) or 0),
            "zero_peak_grace_sec": 0,
            "score_min_override": float(min_score_for(_sym)),
            "is_active": False,
            **_default_policy,
            "source": "default",
            "last_updated": None,
        }
        _db_cfg = self._dynamic_configs.get(_sym)
        if not _db_cfg:
            return _default
        _spf, _grace, _score = self._clamp_dynamic_values(
            _sym,
            int(_db_cfg.get("spike_pre_filter_target") or _default["spike_pre_filter_target"]),
            int(_db_cfg.get("zero_peak_grace_sec") or 0),
            float(_db_cfg.get("score_min_override") or _default["score_min_override"]),
        )
        _regime = str(_db_cfg.get("market_regime") or "NORMAL").upper()
        _policy = self._multispike_policy_for_regime(_regime)

        return {
            "symbol": _sym,
            "market_regime": _regime,
            "spike_pre_filter_target": _spf,
            "zero_peak_grace_sec": _grace,
            "score_min_override": _score,
            "is_active": bool(_db_cfg.get("is_active", True)),
            **_policy,
            "source": "dynamic_db",
            "last_updated": _db_cfg.get("last_updated"),
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
            rows = await conn.fetch(
                """
                SELECT symbol, market_regime, spike_pre_filter_target,
                       zero_peak_grace_sec, score_min_override,
                       is_active, last_updated
                FROM dynamic_symbol_config
                """
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
            _new[_sym] = {
                "market_regime": str(row["market_regime"] or "NORMAL").upper(),
                "spike_pre_filter_target": _spf,
                "zero_peak_grace_sec": _grace,
                "score_min_override": _score,
                "is_active": bool(row["is_active"]),
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
            "score_breakdown={trend=%.2f mom=%.2f atr=%.2f spread=%.2f stab=%.2f "
            "streak=%.2f cd=%.2f hd=%.2f smc=%s geo=%s geo_pos=%s}",
            symbol, block_reason,
            score, effective_min_score,
            side or "?", regime, hurst,
            ai_veto, ai_confidence, ai_reason or "—",
            cooldown, cooldown_elapsed, cooldown_required,
            bd.get("trend", 0), bd.get("momentum", 0), bd.get("atr", 0),
            bd.get("spread", 0), bd.get("stability", 0),
            bd.get("streak_penalty", 0), bd.get("cooldown", 0),
            bd.get("hd_bonus", 0),   # HD = Higher Direction (macro alignment)
            f"+{bd['smc_bonus']:.2f}" if bd.get("smc_bonus") else "—",
            # geo_gate: +1.0 optimal / 0.0 border / -1.5 slight / -2.0 hard penalty
            f"{bd['geo_gate']:+.1f}" if "geo_gate" in bd else "n/a",
            f"{bd['geo_channel_pos']:.3f}" if bd.get("geo_channel_pos") is not None else "—",
        )

    # ─────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        _LOGGER.info(
            "[deriv-daemon] starting | symbols=%s dry_run=%s bankroll=%.2f",
            self._settings.symbols, self._settings.dry_run, self._settings.bankroll_usdt,
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
        self._velocity.ingest_tick(tick.symbol, tick.price)

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

    async def _pipeline(self, tick: NormalisedTick) -> None:
        # ═══════════════════════════════════════════════════════════════════
        # BLOCK 1 — GATE: trade-level cooldown (one trade per symbol at a time)
        # ═══════════════════════════════════════════════════════════════════
        if not self._cooldown.can_fire(tick.symbol):
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
        if _early_profile.get("disabled"):
            _LOGGER.debug("[PIPELINE] SYMBOL_DISABLED %s — skipping", tick.symbol)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="symbol_disabled")
            return
        if _early_profile.get("suspended"):
            _LOGGER.debug("[PIPELINE] SYMBOL_SUSPENDED %s — skipping", tick.symbol)
            self._spike_enrich(tick.symbol, bot_entered=False, block_reason="symbol_suspended")
            return
        if _dyn_source == "dynamic_db" and not _dyn_active:
            _LOGGER.info("[PIPELINE] SYMBOL_DYNAMIC_INACTIVE %s — skipping", tick.symbol)
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
            _geo_min  = _asset_profile_early.get("geo_entry_min")
            _geo_max  = _asset_profile_early.get("geo_entry_max")
            _geo_gate = 0.0
            _geo_gate_label = ""
            # Per-symbol geo tolerance: soft→hard penalty boundary (default 0.30).
            # CRASH500 uses 0.25 to tighten overshoot zone (Muestra3).
            _geo_tol = float(_asset_profile_early.get("geo_penalty_tolerance", 0.30))
            # Per-symbol extended-down veto: when geo_pos < this floor, CRASH entries
            # that would normally receive geo_optimal +1.0 bonus are hard-vetoed (-2.0).
            # Fixes Grade A WR 14% issue where deeply negative geo positions were getting
            # bonus score and entering as Grade A despite poor empirical WR (Muestra3).
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
                _LOGGER.debug(
                    "[PIPELINE] GEO_GATE %s | %s | score→%.2f",
                    tick.symbol, _geo_gate_label, snap.score,
                )

        # ── Mean-reverting R_* score floor (H < 0.45 → require ≥ 6.0) ──────
        # evaluate() sets effective_min=6.0 for mean_rev setups (+3.0 bonus
        # applied inside evaluate).  This outer gate mirrors that floor so that
        # snap.allowed=False setups are surfaced in _log_entry_block.
        # Changed 9.0 → 6.0 (Phase9): requiring 9.0 was blocking every valid
        # Grade A mean-rev setup (e.g. R_100 H=0.41 scoring 8.72 after the
        # +3.0 mean-rev bonus — the bonus already compensates for absent trend).
        if _rw_regime == "mean_reverting" and snap.score < 6.0:
            self._log_entry_block(
                tick.symbol, "MEAN_REV_SCORE_GATE",
                score=snap.score, effective_min_score=6.0,
                side=snap.side, regime=snap.regime,
                hurst=_eval_hurst,
                score_breakdown=snap.score_breakdown,
            )
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=snap.score,
                reason=f"MEAN_REV_SCORE_GATE: H={_eval_hurst:.3f} mean_reverting requires≥6.00 got={snap.score:.2f}",
                extra={"hurst": _eval_hurst, "regime": "mean_reverting"},
            )
            return

        # ── Extreme MR exhaustion penalty (BOOM/CRASH only) ───────────────
        _geo_label = str(snap.score_breakdown.get("geo_layout") or "")
        _mr_pen = extreme_mr_penalty(tick.symbol, _eval_hurst, _geo_label)
        if _mr_pen != 0.0:
            snap.score = round(max(0.0, snap.score + _mr_pen), 3)
            snap.score_breakdown["hurst_mr_penalty"] = _mr_pen
            _LOGGER.info(
                "[PIPELINE] EXTREME_MR_PENALTY %s | H=%.3f geo=%s pen=%.2f score→%.2f",
                tick.symbol, _eval_hurst, _geo_label, _mr_pen, snap.score,
            )

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
        if not _atr_ok and not _atr_calm_bypassed:
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
        _dyn_score_min = max(
            self._dynamic_score_min_guardrail,
            min(
                self._dynamic_score_max_guardrail,
                float(_dyn_cfg.get("score_min_override") or min_score_for(tick.symbol)),
            ),
        )
        snap.score_breakdown["dynamic_cfg_active"] = _dyn_active
        snap.score_breakdown["dynamic_score_min"] = round(_dyn_score_min, 3)
        _regime_min = _dyn_score_min if _dyn_active else snap.effective_min_score

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

        if snap.score < _regime_min:
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
                extra={"regime": snap.regime},
            )
            return

        # Per-symbol minimum score gate (ASSET_INTEL_PROFILES)
        _profile_min_score = _dyn_score_min if _dyn_active else min_score_for(tick.symbol)
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

        # ── hurst_min_spike gate (spike markets that have a strict Hurst floor) ──
        # BOOM1000/CRASH1000 etc. can declare hurst_min_spike: 0.43 to filter out
        # entries where the underlying Hurst is too low (no persistence).
        _hms = _asset_profile.get("hurst_min_spike")
        if _hms is not None and is_spike_market(tick.symbol):
            if _eval_hurst < float(_hms):
                _hms_reason = (
                    f"HURST_SPIKE_VETO: {tick.symbol} H={_eval_hurst:.3f} < strict_min={_hms:.3f}"
                )
                self._record_decision(
                    symbol=tick.symbol, allowed=False, side=snap.side,
                    score=snap.score, reason=_hms_reason,
                    extra={"hurst": _eval_hurst, "hurst_min_spike": _hms},
                )
                self._spike_enrich(tick.symbol, bot_entered=False,
                                   block_reason=_hms_reason)
                return

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

        # ── Build order payload (AI-approved path) ─────────────────────────
        _is_mean_rev = bool(snap.score_breakdown.get("mean_rev_mode"))
        _is_spike_entry = bool(snap.score_breakdown.get("spike_entry"))
        _is_boom_crash = any(k in tick.symbol.upper() for k in ("BOOM", "CRASH"))
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
                "last_decisions": list(self._last_decisions[-15:]),
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
