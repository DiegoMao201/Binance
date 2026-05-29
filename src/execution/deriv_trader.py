"""
src/execution/deriv_trader.py
─────────────────────────────────────────────────────────────────────────────
Async executor for Deriv multiplier contracts.

Responsibilities:
  • Translate a normalised order payload into a Deriv `buy` request.
  • Track every open contract in `logs/deriv_open_contracts.json`.
  • Poll open contracts until they are closed (by SL, TP or manual sell).
  • On close: append to `logs/deriv_closed_contracts.json` AND fire the
    PAMM webhook so the user balance is settled in PostgreSQL with broker='deriv'.
  • Surface fail-safes (Telegram alerts) on every error path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from src.data.deriv_client import DerivClient, DerivClientError
from src.execution.position_manager import DynamicPositionManager
from src.strategies.deriv_signals import get_asset_profile, is_spike_market
from src.utils.deriv_config import DerivSettings
from src.utils.deriv_multi_accounts import load_multi_accounts_config, resolve_multi_accounts_path
from src.utils.telegram_telemetry import TelegramTelemetry


_LOGGER = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "false") -> bool:
    raw = os.getenv(name, default)
    val = str(raw).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
        val = val[1:-1].strip()
    return val.lower() in {"1", "true", "yes", "on"}


# 2026-05-27 overhaul "quality>quantity": all early-exit paths are now OFF by default.
# Premature closes (spike_timeout, zero_peak_exit, open_prob_exit, DEP-ACTIVE) were
# cutting BOOM/CRASH positions seconds before the spike arrived. Only the broker
# native SL/TP and the DPM trailing ratchet remain in charge of exiting trades.
_DISABLE_SPIKE_TIMEOUT = _env_flag("DERIV_DISABLE_SPIKE_TIMEOUT", "true")
_OPEN_PROB_EXIT_ENABLED = _env_flag("DERIV_OPEN_PROB_EXIT_ENABLED", "false")
_OPEN_PROB_MIN_HOLD_SEC = max(
    30.0,
    min(float(os.getenv("DERIV_OPEN_PROB_MIN_HOLD_SEC", "150") or 150.0), 1200.0),
)
_OPEN_PROB_CLOSE_THRESHOLD = max(
    0.05,
    min(float(os.getenv("DERIV_OPEN_PROB_CLOSE_THRESHOLD", "0.22") or 0.22), 0.90),
)
_OPEN_PROB_CLOSE_PNL_CEIL_USDT = max(
    -2.0,
    min(float(os.getenv("DERIV_OPEN_PROB_CLOSE_PNL_CEIL_USDT", "0.02") or 0.02), 2.0),
)
_OPEN_PROB_SHADOW_LOG_EVERY_SEC = max(
    10.0,
    min(float(os.getenv("DERIV_OPEN_PROB_SHADOW_LOG_EVERY_SEC", "45") or 45.0), 300.0),
)


def _symbol_from_shortcode(shortcode: str) -> str:
    """Extract the underlying symbol from a Deriv contract shortcode.

    Deriv shortcodes follow the pattern:
        <CONTRACT_TYPE>_<SYMBOL>_<STAKE>_<MULTIPLIER>_...
    where STAKE is always a decimal (e.g. "3.00") and SYMBOL may contain
    underscores (e.g. "R_100", "R_75").  We accumulate segments after the
    contract type until we hit the first segment containing a decimal point.

    Examples:
        "MULTUP_BOOM500_3.00_200_..."  →  "BOOM500"
        "MULTUP_R_100_3.00_200_..."   →  "R_100"
        "MULTDOWN_R_75_3.00_200_..."  →  "R_75"
        "CALL_BOOM500_3.00_0_..."     →  "BOOM500"
    """
    if not shortcode:
        return ""
    parts = shortcode.split("_")
    sym_parts: list[str] = []
    for part in parts[1:]:  # skip contract type (MULTUP, MULTDOWN, CALL, PUT…)
        if "." in part:     # stake is always decimal — marks end of symbol
            break
        sym_parts.append(part)
    return "_".join(sym_parts)


# ─── Order payload contract (broker-agnostic) ────────────────────────────────
@dataclass(slots=True)
class DerivOrder:
    symbol: str
    side: str                # 'MULTUP' | 'MULTDOWN'
    stake_usdt: float
    multiplier: int
    stop_loss_pct: float
    take_profit_pct: float
    intent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    score_breakdown: dict | None = field(default=None)
    # Time-based spike guardrail: force-close after N seconds (0 = disabled).
    # Set to BOOM_CRASH_SPIKE_TIMEOUT_SEC for BOOM/CRASH symbols.
    max_hold_seconds: float = field(default=0.0)


# ─── Trailing SL configuration ──────────────────────────────────────────────
# Tiered trailing stop — thresholds expressed as a % of stake_usdt so the
# same env-var set works for any stake size without manual tuning.
#
#   Tier 1 (peak >= T1%  of stake) → floor = 0.00      (break-even)
#   Tier 2 (peak >= T2%  of stake) → floor = +T2_LOCK% (profit lock)
#   Tier 3 (peak >= T3%  of stake) → floor = peak − T3_STEP%  (aggressive trail)
#
# Legacy flat vars kept for fallback when stake is unavailable.
_TRAIL_INIT_SL:   float = float(os.getenv("DERIV_TRAIL_INIT_SL",  "-1.0"))
_TRAIL_START:     float = float(os.getenv("DERIV_TRAIL_START",    "0.5"))
_TRAIL_STEP:      float = float(os.getenv("DERIV_TRAIL_STEP",     "0.5"))
# Tiered percentages (of stake_usdt)
# Tiered trailing stop — thresholds scaled to 200× leverage and a $1–$3 stake.
# At 200×, a 0.01% price move = 2% stake move, so the old 1%/2.5%/4% tiers were
# triggered by 1–2 ticks of noise before the spike had time to develop.
# New tiers require a meaningful profit before any floor is ratcheted:
#   T1 (BE):        15% of stake  → $0.45 on $3  (not reached before major move)
#   T2 (profit lock): 35% of stake → $1.05 on $3  → locks +15% of stake
#   T3 (tight trail): 60% of stake → $1.80 on $3  → trails at peak − 5% of stake
_T1_PCT:          float = float(os.getenv("DERIV_TRAIL_T1_PCT",        "0.15"))   # 15% → BE
_T2_PCT:          float = float(os.getenv("DERIV_TRAIL_T2_PCT",        "0.35"))   # 35% → lock
_T3_PCT:          float = float(os.getenv("DERIV_TRAIL_T3_PCT",        "0.60"))   # 60% → tight
_T2_LOCK_PCT:     float = float(os.getenv("DERIV_TRAIL_T2_LOCK_PCT",   "0.20"))   # lock = +20% (institutional spec)
_T3_STEP_PCT:     float = float(os.getenv("DERIV_TRAIL_T3_STEP_PCT",   "0.05"))   # trail gap 5%
# Strict safety boundary: maximum loss allowed as a fraction of stake.
# Default 1.00 (the full stake — Deriv contracts can never lose more than stake).
_TRAIL_MAX_LOSS_PCT: float = float(os.getenv("DERIV_TRAIL_MAX_LOSS_PCT", "1.00"))
# Minimum locked floor once trailing is active (applied per-symbol via profile;
# this constant enforces the global baseline). Eliminates break-even noise closes
# and the recovered-on-boot legacy floor=-1.00 issue.
_TRAIL_FLOOR_GLOBAL_MIN: float = float(os.getenv("DERIV_TRAIL_FLOOR_MIN_USDT", "0.20"))


def _compute_trail_floor(
    peak: float,
    stake: float = 0.0,
    spike_market: bool = False,
    trail_floor_min: float = 0.0,
) -> float | None:
    """Institutional tiered trailing stop floor (refactored 2026-05-18).

    Strict tier semantics based on peak PnL as a fraction of stake:

      • Below T1  (peak_pnl_pct < T1_PCT)           → None (trailing inactive)
      • T1 ≤ pct < T2                                → 0.0  (break-even lock)
      • T2 ≤ pct < T3                                → stake × T2_LOCK_PCT (+20%)
      • pct ≥ T3                                     → (pct − T3_STEP_PCT) × stake

    Final value is clamped by the strict safety boundary:
        max(floor, -sl_max_loss)
    so any anomalous computation can never force an invalid SL beyond the
    structural maximum loss (stake × DERIV_TRAIL_MAX_LOSS_PCT).

    spike_market=True suppresses Tier 1 (BE) to avoid premature exits on
    inter-spike noise — trailing only activates at Tier 2 (genuine profit).

    Returns None when no floor is active (caller must skip ratchet logic).
    Falls back to the legacy flat ratchet only when stake is unavailable.
    """
    if stake > 0:
        peak_pnl_pct = peak / stake
        sl_max_loss = stake * _TRAIL_MAX_LOSS_PCT
        _floor_min = max(trail_floor_min, _TRAIL_FLOOR_GLOBAL_MIN)

        # Tier 3: strong run → tight trail (peak − step) × stake
        if peak_pnl_pct >= _T3_PCT:
            floor = (peak_pnl_pct - _T3_STEP_PCT) * stake
            return round(max(floor, _floor_min, -sl_max_loss), 4)

        # Tier 2: decent gain → lock at +20% of stake
        if peak_pnl_pct >= _T2_PCT:
            floor = stake * _T2_LOCK_PCT
            return round(max(floor, _floor_min, -sl_max_loss), 4)

        # Tier 1: first profit → floor at minimum (skipped for spike markets)
        # floor_min replaces the old 0.0 break-even to prevent noise-close losses.
        if not spike_market and peak_pnl_pct >= _T1_PCT:
            return round(max(_floor_min, 0.0), 4)

        # Below T1 → trailing inactive (caller must not ratchet)
        return None

    # Legacy flat ratchet (stake unknown) — kept for backwards compatibility
    if peak < _TRAIL_START:
        return _TRAIL_INIT_SL
    return math.floor(peak / _TRAIL_STEP) * _TRAIL_STEP - _TRAIL_STEP


@dataclass(slots=True)
class DerivOpenContract:
    contract_id: int
    intent_id: str
    symbol: str
    side: str
    stake_usdt: float
    multiplier: int
    entry_price: float
    opened_at_ts: float
    score_breakdown: dict | None = field(default=None)
    max_hold_seconds: float = field(default=0.0)
    # Trailing SL state (updated each poll cycle)
    peak_profit: float = field(default=0.0)
    trail_sl_locked: float = field(default=_TRAIL_INIT_SL)
    # BUG-B fix: live floating PnL — updated on every WS tick, exposed to
    # the dashboard via get_open_contracts_for_status() and _persist_open().
    floating_pnl: float = field(default=0.0)
    # BUG-B fix: True once we have updated the broker-side SL to breakeven ($0.01)
    # after DPM reaches Phase 2 — gives a server-level backstop that fires even
    # when the client-side tick stream misses the exact peak tick.
    broker_be_locked: bool = field(default=False)
    # DPM-initiated close reason — set BEFORE sell() is called so the exit
    # classification uses the real reason instead of broker "sold" → "manual_close".
    # Ephemeral: not persisted to disk (safe to lose on restart).
    pending_close_reason: str | None = field(default=None)
    # Phase 20: previous-tick profit for spike-delta detection.
    # Default -1.0 so the first tick never triggers a false delta.
    last_profit: float = field(default=-1.0)
    # Telemetry: tick count at the moment of entry (for ticks_held in closed record).
    entry_tick_count: int = field(default=0)
    # Telemetry: ATR snapshots sampled every 30s during the trade lifetime.
    # Format: list of (elapsed_seconds, atr_value) rounded to 5 dp.
    atr_samples: list = field(default_factory=list)
    # DEP telemetry: rolling peak ATR seen during this contract lifetime.
    dep_peak_atr: float = field(default=0.0)
    # DEP shadow telemetry throttle (epoch seconds).
    dep_shadow_last_log_ts: float = field(default=0.0)
    # Probability-exit shadow telemetry throttle (epoch seconds).
    prob_shadow_last_log_ts: float = field(default=0.0)
    # Live audit snapshots for frontend visual forensics.
    dep_last_eval: dict[str, Any] = field(default_factory=dict)
    prob_last_eval: dict[str, Any] = field(default_factory=dict)


class DerivTradeExecutor:
    """Sequencer that owns the lifecycle of every Deriv contract."""

    _POLL_INTERVAL_SECONDS = 2.0  # how often we re-check open contracts

    def __init__(
        self,
        settings: DerivSettings,
        client: DerivClient,
        telemetry: TelegramTelemetry | None = None,
        risk_manager: Any = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._telemetry = telemetry
        # Optional reference to DerivRiskManager — used to read current ATR and
        # tick counts for ticks_held / atr_trajectory telemetry in closed records.
        self._risk = risk_manager
        # Optional callback injected by DerivDaemon:
        #     provider(symbol) -> dynamic config dict from PostgreSQL cache.
        self._dynamic_config_provider: Callable[[str], dict[str, Any]] | None = None
        self._open: dict[int, DerivOpenContract] = {}
        self._lock = asyncio.Lock()
        # Phase 35: close-lock guard — prevents duplicate sell() calls when WS
        # ticks and/or the reaper fire on the same contract simultaneously.
        # asyncio is single-threaded so check+add before any `await` is atomic.
        self._closing: set[int] = set()
        # Phase 36: entry-lock guard — prevents TOCTOU race where two pipeline
        # tasks both pass the open_on_symbol check before the first buy
        # completes (~300 ms window).  Checked+set before any `await`.
        self._pending_entries: set[str] = set()
        # Running per-symbol stats (updated on each contract close)
        self._sym_stats: dict[str, dict[str, Any]] = {}
        self._session_pnl: float = 0.0
        self._session_trades: int = 0
        # DynamicPositionManager — replaces static tiered trailing stop.
        # Manages ratchet SL + momentum-based exit for every open contract.
        self._dpm = DynamicPositionManager()
        self._disable_spike_timeout = _DISABLE_SPIKE_TIMEOUT
        # Phase 37+: multispike buffer state (tick-domain ratchet).
        # dict[contract_id, {peak_pnl, entry_pnl, start_tick, buffer_ticks,
        #                    retention_pct, min_floor_usdt, drift_ticks, regime}]
        self._spike_buffer: dict[int, dict[str, Any]] = {}
        # Phase 5D: zero_peak emergency cuts are disabled by default while we
        # stabilize spike timing. Timeout/SL/ratchet remain active.
        self._zero_peak_exit_enabled = os.getenv(
            "DERIV_ZERO_PEAK_EXIT_ENABLED",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Spike-wait timeout (2026-05-29):
        # Forensic over 962 closed contracts:
        #   - wins hold p50=400s, p75=773s
        #   - losses hold p50=1048s, p75=1621s
        #   - hold buckets >=600s collapse to <=51% win rate and net negative pnl
        # This exit cuts spike trades early when the spike clearly didn't show up:
        # past T seconds, with very low MFE and meaningful floating loss, and not
        # right before an expected spike (re-uses _should_defer_zero_peak_exit).
        # After the close the per-symbol cooldown keeps re-entry possible if a
        # real new signal arrives.
        self._spike_wait_timeout_enabled = os.getenv(
            "DERIV_SPIKE_WAIT_TIMEOUT_ENABLED",
            "true",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._spike_wait_timeout_sec = max(
            120.0,
            min(float(os.getenv("DERIV_SPIKE_WAIT_TIMEOUT_SEC", "480") or 480), 1200.0),
        )
        self._spike_wait_timeout_mfe_frac = max(
            0.0,
            min(float(os.getenv("DERIV_SPIKE_WAIT_TIMEOUT_MFE_FRAC", "0.15") or 0.15), 1.0),
        )
        self._spike_wait_timeout_loss_frac = max(
            0.05,
            min(float(os.getenv("DERIV_SPIKE_WAIT_TIMEOUT_LOSS_FRAC", "0.25") or 0.25), 1.5),
        )
        # Hard-cut variant: same loss threshold as soft (default 0.25 stake) but
        # bypasses the defer guard, so we don't wait for the spike-cycle defer
        # to clear if it never does. CRITICAL: must be < broker SL (typically
        # ~36% of stake) or the broker hits SL first and the cut never fires.
        # Per-symbol soft T + hardcut_margin (default +120s).
        self._spike_wait_hardcut_loss_frac = max(
            self._spike_wait_timeout_loss_frac,
            min(
                float(os.getenv("DERIV_SPIKE_WAIT_HARDCUT_LOSS_FRAC", "0.25") or 0.25),
                2.0,
            ),
        )
        # Emergency cut: if at T_soft the floating loss is already deep (close
        # to broker SL), bypass the defer even if the spike-cycle says one is
        # coming. Default 0.32 stake (= -$1.60 on $5 stake), well under the
        # broker SL at -$1.80 (~36% stake). Set to >=1.0 to disable.
        self._spike_wait_emergency_loss_frac = max(
            self._spike_wait_timeout_loss_frac,
            min(
                float(os.getenv("DERIV_SPIKE_WAIT_EMERGENCY_LOSS_FRAC", "0.32") or 0.32),
                2.0,
            ),
        )
        # Per-symbol soft-timeout overrides (dynamic per-symbol patience window).
        # Derived from 962-trade audit: win_hold p75 per symbol.
        # Format: "BOOM500:720,BOOM900:900,CRASH600:540,..."
        # Falls back to DERIV_SPIKE_WAIT_TIMEOUT_SEC for symbols not in the map.
        _swt_default_map = (
            "BOOM300:300,CRASH300:300,"
            "BOOM500:720,CRASH500:480,"
            "BOOM600:540,CRASH600:540,"
            "BOOM900:900,CRASH900:720,"
            "BOOM1000:900,CRASH1000:720"
        )
        self._spike_wait_timeout_sec_map = self._parse_symbol_seconds_map(
            os.getenv("DERIV_SPIKE_WAIT_TIMEOUT_SEC_MAP", _swt_default_map),
            max_value=1800.0,
        )
        # Hard-cut margin added on top of the per-symbol soft T (default 120s).
        self._spike_wait_hardcut_margin_sec = max(
            30.0,
            min(float(os.getenv("DERIV_SPIKE_WAIT_HARDCUT_MARGIN_SEC", "120") or 120), 900.0),
        )
        # Optional operator override map: "BOOM600:90,CRASH900:120".
        self._spike_hold_bonus_map = self._parse_symbol_seconds_map(
            os.getenv("DERIV_SPIKE_HOLD_BONUS_SEC_MAP", "")
        )
        # Hook the WS client so open-contract subscriptions are automatically
        # re-established after every WS reconnect.
        self._client.set_reconnect_callback(self._on_ws_reconnect)

        _LOGGER.info(
            "[deriv-trader] zero_peak_exit_enabled=%s (Phase5D)",
            self._zero_peak_exit_enabled,
        )
        _LOGGER.info(
            "[deriv-trader] spike_wait_timeout_enabled=%s default_T=%.0fs mfe<=stake*%.2f loss>=stake*%.2f hardcut_margin=%.0fs hardcut_loss>=stake*%.2f emergency_loss>=stake*%.2f per_sym=%s",
            self._spike_wait_timeout_enabled,
            self._spike_wait_timeout_sec,
            self._spike_wait_timeout_mfe_frac,
            self._spike_wait_timeout_loss_frac,
            self._spike_wait_hardcut_margin_sec,
            self._spike_wait_hardcut_loss_frac,
            self._spike_wait_emergency_loss_frac,
            self._spike_wait_timeout_sec_map,
        )
        _LOGGER.info(
            "[deriv-trader] disable_spike_timeout=%s (DERIV_DISABLE_SPIKE_TIMEOUT)",
            self._disable_spike_timeout,
        )
        _LOGGER.info(
            "[deriv-trader] open_prob_exit_enabled=%s min_hold=%.0fs threshold=%.2f pnl_ceil=%.2f",
            _OPEN_PROB_EXIT_ENABLED,
            _OPEN_PROB_MIN_HOLD_SEC,
            _OPEN_PROB_CLOSE_THRESHOLD,
            _OPEN_PROB_CLOSE_PNL_CEIL_USDT,
        )

        # ── Restore open contracts from previous session (disk → memory) ─────
        # The process was restarted (e.g. Coolify deploy).  Re-hydrate
        # self._open from the persisted JSON so the boot-sync reconciliation
        # has a complete local snapshot to diff against broker portfolio,
        # and the reaper/trailing-stop loops can resume without an amnesia gap.
        _disk = self._settings.open_contracts_file
        if _disk.exists():
            try:
                _raw: list[dict[str, Any]] = json.loads(_disk.read_text())
                for _r in _raw:
                    _cid = int(_r["contract_id"])
                    self._open[_cid] = DerivOpenContract(
                        contract_id=_cid,
                        intent_id=_r.get("intent_id", ""),
                        symbol=_r["symbol"],
                        side=_r["side"],
                        stake_usdt=float(_r["stake_usdt"]),
                        multiplier=int(_r.get("multiplier", 200)),
                        entry_price=float(_r.get("entry_price", 0.0)),
                        opened_at_ts=float(_r["opened_at_ts"]),
                        score_breakdown=_r.get("score_breakdown"),
                        max_hold_seconds=float(_r.get("max_hold_seconds", 0.0)),
                        peak_profit=float(_r.get("peak_profit", 0.0)),
                        trail_sl_locked=float(_r.get("trail_sl_locked", _TRAIL_INIT_SL)),
                        # Phase 15 fix: restore broker BE and floating PnL state
                        broker_be_locked=bool(_r.get("broker_be_locked", False)),
                        floating_pnl=float(_r.get("floating_pnl", 0.0)),
                        dep_peak_atr=float(_r.get("dep_peak_atr", 0.0)),
                        dep_last_eval=dict(_r.get("dep_last_eval") or {}),
                        prob_last_eval=dict(_r.get("prob_last_eval") or {}),
                    )
                if self._open:
                    _LOGGER.info(
                        "[deriv-trader] restored %d open contract(s) from disk: %s",
                        len(self._open),
                        list(self._open.keys()),
                    )
            except Exception as _disk_exc:  # noqa: BLE001
                _LOGGER.warning(
                    "[deriv-trader] could not restore open contracts from disk: %s — starting fresh",
                    _disk_exc,
                )

    def set_dynamic_config_provider(
        self,
        provider: Callable[[str], dict[str, Any]] | None,
    ) -> None:
        """Inject runtime dynamic config provider from daemon."""
        self._dynamic_config_provider = provider

    @staticmethod
    def _parse_symbol_seconds_map(raw: str, max_value: float = 300.0) -> dict[str, float]:
        """Parse map strings like 'BOOM600:90,CRASH900:120'.

        max_value caps each parsed value (defaults to 300 for the legacy
        spike_hold_bonus map). New callers can lift this cap.
        """
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
            out[sym] = min(sec, max_value)
        return out

    def _spike_hold_bonus_sec(self, symbol: str) -> float:
        """Extra patience window per spike symbol with opt-in global extra hold."""
        if not is_spike_market(symbol):
            return 0.0

        _sym = str(symbol or "").upper()
        _extra = max(
            0.0,
            min(float(os.getenv("DERIV_SPIKE_HOLD_EXTRA_SEC", "0") or 0), 180.0),
        )
        _env_direct = os.getenv(f"DERIV_SPIKE_HOLD_BONUS_SEC_{_sym}")
        if _env_direct not in (None, ""):
            try:
                _base = max(0.0, min(float(_env_direct), 300.0))
                return min(_base + _extra, 420.0)
            except ValueError:
                pass

        if _sym in self._spike_hold_bonus_map:
            return min(self._spike_hold_bonus_map[_sym] + _extra, 420.0)

        _profile = get_asset_profile(_sym)
        _cycle = int(_profile.get("spike_interval_ticks", 0) or 0)
        if _cycle <= 0:
            _default_base = max(
                0.0,
                min(float(os.getenv("DERIV_SPIKE_HOLD_BONUS_DEFAULT_SEC", "90") or 90), 300.0),
            )
            return min(_default_base + _extra, 420.0)
        if _cycle <= 600:
            return min(90.0 + _extra, 420.0)
        return min(120.0 + _extra, 420.0)

    def _multispike_policy(self, symbol: str) -> dict[str, Any]:
        """Resolve multispike ratchet policy (dynamic config + env fallback)."""
        cfg: dict[str, Any] = {}
        if self._dynamic_config_provider is not None:
            try:
                cfg = self._dynamic_config_provider(symbol) or {}
            except Exception:  # noqa: BLE001
                cfg = {}

        regime = str(cfg.get("market_regime") or "NORMAL").upper()

        def _env_float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)) or default)
            except Exception:
                return float(default)

        def _env_int(name: str, default: int) -> int:
            try:
                return int(float(os.getenv(name, str(default)) or default))
            except Exception:
                return int(default)

        buf_default = _env_int("DERIV_MULTISPIKE_BUFFER_TICKS_DEFAULT", 45)
        buf_fast = _env_int("DERIV_MULTISPIKE_BUFFER_TICKS_FAST", 60)
        buf_slow = _env_int("DERIV_MULTISPIKE_BUFFER_TICKS_SLOW", 10)

        ret_default = _env_float("DERIV_MULTISPIKE_RETENTION_PCT_DEFAULT", 0.70)
        ret_fast = _env_float("DERIV_MULTISPIKE_RETENTION_PCT_FAST", 0.50)
        ret_slow = _env_float("DERIV_MULTISPIKE_RETENTION_PCT_SLOW", 0.85)

        floor_default = _env_float("DERIV_MULTISPIKE_MIN_FLOOR_USDT", 0.15)

        drift_default = _env_int("DERIV_MULTISPIKE_DRIFT_TICKS_DEFAULT", buf_default)
        drift_fast = _env_int("DERIV_MULTISPIKE_DRIFT_TICKS_FAST", buf_fast)
        drift_slow = _env_int("DERIV_MULTISPIKE_DRIFT_TICKS_SLOW", buf_slow)

        if regime == "FAST":
            buffer_ticks = buf_fast
            retention_pct = ret_fast
            drift_ticks = drift_fast
        elif regime == "SLOW":
            buffer_ticks = buf_slow
            retention_pct = ret_slow
            drift_ticks = drift_slow
        else:
            buffer_ticks = buf_default
            retention_pct = ret_default
            drift_ticks = drift_default

        if cfg.get("multispike_buffer_ticks") is not None:
            try:
                buffer_ticks = int(float(cfg.get("multispike_buffer_ticks")))
            except Exception:
                pass
        if cfg.get("multispike_retention_pct") is not None:
            try:
                retention_pct = float(cfg.get("multispike_retention_pct"))
            except Exception:
                pass
        if cfg.get("multispike_min_floor_usdt") is not None:
            try:
                floor_default = float(cfg.get("multispike_min_floor_usdt"))
            except Exception:
                pass
        if cfg.get("multispike_timeout_drift_ticks") is not None:
            try:
                drift_ticks = int(float(cfg.get("multispike_timeout_drift_ticks")))
            except Exception:
                pass

        return {
            "regime": regime,
            "buffer_ticks": max(5, min(int(buffer_ticks), 300)),
            "retention_pct": max(0.30, min(float(retention_pct), 0.95)),
            "min_floor_usdt": max(0.01, min(float(floor_default), 5.00)),
            "drift_ticks": max(5, min(int(drift_ticks), 400)),
        }

    def _current_tick_count(self, symbol: str) -> int:
        if self._risk is None:
            return 0
        try:
            return int(self._risk.get_tick_count(symbol) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _ticks_since_last_spike(self, symbol: str, fallback_start_tick: int = 0) -> int:
        """Return ticks elapsed since last detected spike for symbol."""
        cur_tick = self._current_tick_count(symbol)
        if self._risk is None:
            return max(0, cur_tick - int(fallback_start_tick or 0))
        try:
            last_spike_tick = int(self._risk.get_last_spike_tick_count(symbol) or 0)
        except Exception:  # noqa: BLE001
            last_spike_tick = 0
        if cur_tick <= 0:
            return 0
        if last_spike_tick > 0 and cur_tick >= last_spike_tick:
            return max(0, cur_tick - last_spike_tick)
        return max(0, cur_tick - int(fallback_start_tick or 0))

    @staticmethod
    def _dpm_timeout_buffer_sec() -> float:
        return max(0.0, float(os.getenv("DERIV_DPM_TIMEOUT_BUFFER_SEC", "30") or 30.0))

    @staticmethod
    def _symbol_zero_peak_floor(symbol: str) -> int:
        _sym = str(symbol or "").upper()
        if _sym in {"BOOM500", "CRASH500", "CRASH600"}:
            return 60
        return 0

    def _dynamic_zero_peak_grace_sec(self, symbol: str) -> int:
        """Read zero_peak_grace_sec override with per-symbol safety floor."""
        _floor = self._symbol_zero_peak_floor(symbol)
        if self._dynamic_config_provider is None:
            return _floor
        try:
            cfg = self._dynamic_config_provider(symbol) or {}
        except Exception:  # noqa: BLE001
            return _floor
        if not bool(cfg.get("is_active", False)):
            return _floor
        try:
            val = int(cfg.get("zero_peak_grace_sec") or 0)
        except Exception:  # noqa: BLE001
            return _floor
        return max(_floor, min(val, 120))

    def _zero_peak_grace_ticks(self, symbol: str) -> int:
        """Grace ticks near expected spike to avoid premature zero_peak exits."""
        _profile = get_asset_profile(symbol)
        _cycle = int(_profile.get("spike_interval_ticks", 0) or 0)
        if _cycle <= 0:
            return self._dynamic_zero_peak_grace_sec(symbol)
        _frac = float(os.getenv("DERIV_ZERO_PEAK_GRACE_FRAC", "0.35"))
        _min_ticks = int(os.getenv("DERIV_ZERO_PEAK_GRACE_MIN_TICKS", "45"))
        _max_ticks = int(os.getenv("DERIV_ZERO_PEAK_GRACE_MAX_TICKS", str(max(120, _cycle - 1))))
        _raw = int(_cycle * _frac)
        _cycle_grace = max(_min_ticks, min(_raw, _max_ticks))
        _dyn_bonus_mult = max(1, int(os.getenv("DERIV_ZERO_PEAK_DYNAMIC_BONUS_MULT", "2") or 2))
        _dyn_bonus = self._dynamic_zero_peak_grace_sec(symbol) * _dyn_bonus_mult
        return min(_cycle - 1, _cycle_grace + _dyn_bonus)

    def _dynamic_zero_peak_wait_limit_sec(self, symbol: str, base_limit_sec: float) -> float:
        """Compute IA-driven minimum wait before zero_peak_exit can trigger."""
        _profile = get_asset_profile(symbol)
        _cycle = int(_profile.get("spike_interval_ticks", 0) or 0)
        if _cycle <= 0:
            return max(0.0, float(base_limit_sec))

        _base_frac = float(os.getenv("DERIV_ZERO_PEAK_WAIT_FRAC_BASE", "0.55"))
        _dyn_grace = self._dynamic_zero_peak_grace_sec(symbol)
        # IA controls zero_peak_grace_sec; convert it into extra hold-time weight.
        _dyn_frac_boost = min(0.30, float(_dyn_grace) / 300.0)
        _wait_frac = min(0.90, max(0.35, _base_frac + _dyn_frac_boost))
        _cycle_wait = float(int(_cycle * _wait_frac))
        return max(float(base_limit_sec), _cycle_wait)

    def _dep_config(self, symbol: str) -> dict[str, Any]:
        """Resolve DEP config for a symbol from dynamic settings."""
        default_policy = str(os.getenv("DERIV_DEP_DEFAULT_POLICY", "PASSIVE") or "PASSIVE").upper()
        default_hold = int(float(os.getenv("DERIV_DEP_DEFAULT_MIN_HOLD_SEC", "120") or 120))
        default_ratio = float(os.getenv("DERIV_DEP_DEFAULT_ATR_DECAY_RATIO", "0.70") or 0.70)
        default_loss_floor = float(os.getenv("DERIV_DEP_DEFAULT_LOSS_FLOOR_USDT", "-0.05") or -0.05)

        cfg: dict[str, Any] = {}
        if self._dynamic_config_provider is not None:
            try:
                cfg = self._dynamic_config_provider(symbol) or {}
            except Exception:  # noqa: BLE001
                cfg = {}

        policy = str(cfg.get("dep_exit_policy") or default_policy).strip().upper()
        if policy not in {"PASSIVE", "SHADOW", "ACTIVE_DECAY"}:
            policy = "PASSIVE"

        allow_active = str(os.getenv("DERIV_DEP_ALLOW_ACTIVE_DECAY", "false") or "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if policy == "ACTIVE_DECAY" and not allow_active:
            policy = "SHADOW"

        try:
            min_hold = int(cfg.get("dep_min_hold_sec") or default_hold)
        except Exception:  # noqa: BLE001
            min_hold = default_hold
        min_hold = max(30, min(min_hold, 900))

        try:
            decay_ratio = float(cfg.get("dep_atr_decay_ratio") or default_ratio)
        except Exception:  # noqa: BLE001
            decay_ratio = default_ratio
        decay_ratio = max(0.30, min(decay_ratio, 0.95))

        try:
            loss_floor = float(cfg.get("dep_loss_floor_usdt") or default_loss_floor)
        except Exception:  # noqa: BLE001
            loss_floor = default_loss_floor
        loss_floor = max(-5.0, min(loss_floor, 0.0))

        return {
            "policy": policy,
            "min_hold_sec": min_hold,
            "atr_decay_ratio": decay_ratio,
            "loss_floor_usdt": loss_floor,
        }

    def _dep_decay_signal(
        self,
        oc: DerivOpenContract,
        held_s: float,
        current_profit: float,
    ) -> tuple[bool, bool, str, dict[str, Any]]:
        """Evaluate DEP decay trigger.

        Returns (should_close, should_shadow_log, reason, details).
        """
        if self._risk is None or not is_spike_market(oc.symbol):
            return False, False, "", {}

        dep = self._dep_config(oc.symbol)
        policy = str(dep.get("policy") or "PASSIVE").upper()
        if policy == "PASSIVE":
            return False, False, "", {}

        min_hold_sec = float(dep.get("min_hold_sec") or 120.0)
        if held_s < min_hold_sec:
            return False, False, "", {}

        atr_now = self._risk.get_current_atr(oc.symbol)
        if atr_now is None or atr_now <= 0:
            return False, False, "", {}

        oc.dep_peak_atr = max(float(oc.dep_peak_atr or 0.0), float(atr_now))
        peak_atr = float(oc.dep_peak_atr or 0.0)
        if peak_atr <= 0:
            return False, False, "", {}

        atr_ratio = float(atr_now) / peak_atr
        decay_threshold = float(dep.get("atr_decay_ratio") or 0.70)
        loss_floor = float(dep.get("loss_floor_usdt") or -0.05)
        hit = atr_ratio <= decay_threshold and current_profit <= loss_floor
        if not hit:
            return False, False, "", {
                "policy": policy,
                "atr_now": round(float(atr_now), 6),
                "atr_peak": round(float(peak_atr), 6),
                "atr_ratio": round(float(atr_ratio), 4),
                "decay_threshold": round(float(decay_threshold), 4),
                "loss_floor_usdt": round(float(loss_floor), 4),
            }

        reason = (
            "dep_decay_exit:"
            f"atr_ratio={atr_ratio:.3f}<={decay_threshold:.3f}|"
            f"pnl={current_profit:.4f}<={loss_floor:.4f}"
        )
        details = {
            "policy": policy,
            "atr_now": round(float(atr_now), 6),
            "atr_peak": round(float(peak_atr), 6),
            "atr_ratio": round(float(atr_ratio), 4),
            "decay_threshold": round(float(decay_threshold), 4),
            "loss_floor_usdt": round(float(loss_floor), 4),
            "held_sec": round(float(held_s), 1),
            "current_pnl": round(float(current_profit), 4),
        }
        if policy == "SHADOW":
            return False, True, reason, details
        if policy == "ACTIVE_DECAY":
            return True, False, reason, details
        return False, False, "", details

    def _open_probability_signal(
        self,
        oc: DerivOpenContract,
        held_s: float,
        current_profit: float,
        current_price: float,
    ) -> tuple[bool, bool, str, dict[str, Any]]:
        """Evaluate open-trade spike probability and trigger proactive close.

        Returns (should_close, should_shadow_log, reason, details).
        """
        if not _OPEN_PROB_EXIT_ENABLED:
            return False, False, "", {}
        if self._risk is None or not is_spike_market(oc.symbol):
            return False, False, "", {}
        if held_s < _OPEN_PROB_MIN_HOLD_SEC:
            return False, False, "", {}

        atr_now = self._risk.get_current_atr(oc.symbol)
        if atr_now is None or atr_now <= 0:
            return False, False, "", {}

        oc.dep_peak_atr = max(float(oc.dep_peak_atr or 0.0), float(atr_now))
        peak_atr = float(oc.dep_peak_atr or 0.0)
        if peak_atr <= 0:
            return False, False, "", {}

        atr_ratio = float(atr_now) / peak_atr
        profile = get_asset_profile(oc.symbol)
        cycle_ticks = int(profile.get("spike_interval_ticks", 0) or 0)
        ticks_since_spike = self._ticks_since_last_spike(oc.symbol, int(oc.entry_tick_count or 0))

        if cycle_ticks > 0:
            cycle_progress = min(2.0, ticks_since_spike / max(1.0, float(cycle_ticks)))
        else:
            cycle_progress = min(2.0, held_s / 300.0)

        if cycle_progress <= 0.55:
            time_health = 1.0
        elif cycle_progress <= 1.05:
            time_health = max(0.35, 1.0 - ((cycle_progress - 0.55) / 0.50) * 0.65)
        else:
            time_health = max(0.0, 0.35 - ((cycle_progress - 1.05) / 0.35) * 0.35)

        atr_health = max(0.0, min((atr_ratio - 0.35) / 0.65, 1.0))
        pnl_ratio = float(current_profit) / max(float(oc.stake_usdt or 0.0), 0.01)
        pnl_health = max(0.0, min((pnl_ratio + 0.20) / 0.80, 1.0))

        direction_health = 0.5
        if oc.entry_price > 0 and current_price > 0:
            signed_move = (float(current_price) - float(oc.entry_price)) / float(oc.entry_price)
            if str(oc.side).upper() == "MULTDOWN":
                signed_move *= -1.0
            direction_health = max(0.0, min(1.0, 0.5 + signed_move * 250.0))

        probability = (
            0.50 * atr_health
            + 0.30 * time_health
            + 0.15 * pnl_health
            + 0.05 * direction_health
        )

        details = {
            "held_sec": round(float(held_s), 1),
            "current_pnl": round(float(current_profit), 4),
            "atr_now": round(float(atr_now), 6),
            "atr_peak": round(float(peak_atr), 6),
            "atr_ratio": round(float(atr_ratio), 4),
            "time_health": round(float(time_health), 4),
            "pnl_health": round(float(pnl_health), 4),
            "dir_health": round(float(direction_health), 4),
            "cycle_progress": round(float(cycle_progress), 4),
            "ticks_since_spike": int(ticks_since_spike),
            "probability": round(float(probability), 4),
            "close_threshold": round(float(_OPEN_PROB_CLOSE_THRESHOLD), 4),
            "close_pnl_ceil": round(float(_OPEN_PROB_CLOSE_PNL_CEIL_USDT), 4),
        }

        should_close = (
            probability <= _OPEN_PROB_CLOSE_THRESHOLD
            and current_profit <= _OPEN_PROB_CLOSE_PNL_CEIL_USDT
        )
        if not should_close:
            return False, True, "", details

        reason = (
            "open_prob_exit:"
            f"prob={probability:.3f}<={_OPEN_PROB_CLOSE_THRESHOLD:.3f}|"
            f"pnl={current_profit:.4f}<={_OPEN_PROB_CLOSE_PNL_CEIL_USDT:.4f}|"
            f"atr_ratio={atr_ratio:.3f}|cycle={cycle_progress:.2f}"
        )
        return True, False, reason, details

    def _should_defer_zero_peak_exit(self, symbol: str, held_s: float) -> tuple[bool, str]:
        """Return True when the trade is close to expected spike timing.

        This keeps zero_peak_exit enabled, but defers it briefly when the symbol
        is inside a short pre-spike window where late acceleration is common.
        """
        if self._risk is None or held_s <= 0:
            return False, ""
        _profile = get_asset_profile(symbol)
        _cycle = int(_profile.get("spike_interval_ticks", 0) or 0)
        if _cycle <= 0:
            return False, ""

        _cur_tick = int(self._risk.get_tick_count(symbol) or 0)
        _last_spike_tick = int(self._risk.get_last_spike_tick_count(symbol) or 0)
        if _cur_tick <= 0 or _last_spike_tick <= 0 or _cur_tick <= _last_spike_tick:
            return False, ""

        _elapsed = _cur_tick - _last_spike_tick
        _remaining = _cycle - _elapsed
        _grace = self._zero_peak_grace_ticks(symbol)
        if _grace <= 0:
            return False, ""

        if 0 < _remaining <= _grace:
            return True, f"remaining={_remaining}t<=grace={_grace}t cycle={_cycle}t"
        return False, ""

    # ─────────────────────────────────────────────────────────────────────────
    # Public API consumed by the OrderRouter
    # ─────────────────────────────────────────────────────────────────────────
    async def execute(self, order: DerivOrder) -> dict[str, Any]:
        """Place a Deriv contract. Returns a dict ready for the audit trail."""
        if self._settings.dry_run:
            payload = {
                "broker": "deriv",
                "status": "dry_run",
                "intent_id": order.intent_id,
                "symbol": order.symbol,
                "side": order.side,
                "stake_usdt": order.stake_usdt,
                "multiplier": order.multiplier,
                "ts": int(time.time() * 1000),
            }
            _LOGGER.info("[deriv-trader] DRY-RUN order accepted: %s", payload)
            self._notify("paper_buy", payload)
            return payload

        if len(self._open) >= self._settings.max_open_contracts:
            raise DerivClientError(
                f"max_open_contracts={self._settings.max_open_contracts} reached"
            )

        # Phase 36: entry-lock guard (belt-and-suspenders alongside _sym_eval_locks).
        # asyncio is single-threaded: check+add here is atomic — no await between them.
        # This closes the TOCTOU window where open_on_symbol is empty but a buy is
        # already in-flight (self._open is only updated AFTER the broker ack, ~300 ms).
        if order.symbol in self._pending_entries:
            _LOGGER.info(
                "[deriv-trader] ENTRY_GUARD: %s buy already in-flight — blocked duplicate entry",
                order.symbol,
            )
            return {"status": "symbol_already_open", "symbol": order.symbol,
                    "open_contracts": [], "reason": "pending_entry"}
        self._pending_entries.add(order.symbol)
        try:
            return await self._execute_locked(order)
        finally:
            self._pending_entries.discard(order.symbol)

    async def _execute_locked(self, order: "DerivOrder") -> dict[str, Any]:
        """Inner execute: called only when no pending entry exists for the symbol."""
        # BUG-A fix: hard block — one position per symbol, no stale bypass.
        # The per-symbol asyncio lock in _evaluate_and_trade prevents the race
        # but this is the final backstop in execute_order itself.
        # Previous "stale bypass" allowed a second trade when the first contract
        # looked old — removed because it caused opposing positions.
        open_on_symbol = [oc for oc in self._open.values() if oc.symbol == order.symbol]
        if open_on_symbol:
            _LOGGER.info(
                "[deriv-trader] SYMBOL_ALREADY_OPEN: blocked duplicate entry on %s "
                "(open contract(s)=%s) — waiting for close before re-evaluating",
                order.symbol,
                [oc.contract_id for oc in open_on_symbol],
            )
            return {"status": "symbol_already_open", "symbol": order.symbol,
                    "open_contracts": [oc.contract_id for oc in open_on_symbol]}

        # All synthetic indices (R_*, BOOM*, CRASH*) use MULTUP/MULTDOWN
        # multiplier contracts.  BOOM/CRASH directional restriction is enforced
        # upstream by direction_veto in the risk engine (BOOM→MULTUP only,
        # CRASH→MULTDOWN only).  The old RISE/FALL path was rejected by the broker.
        # ABSOLUTE HARD CAP — final guard before any order touches the broker.
        # DERIV_MAX_STAKE_USDT (default $5.00) is unbreakable: no regime/score
        # multiplier or Hurst boost can send a larger stake. Logged as a warning
        # so calibration samples are always within the configured ceiling.
        _final_hard_cap = float(os.getenv("DERIV_MAX_STAKE_USDT", "5.00"))
        if order.stake_usdt > _final_hard_cap:
            _LOGGER.warning(
                "[deriv-trader] stake capped %.2f → %.2f (DERIV_MAX_STAKE_USDT hard cap)",
                order.stake_usdt, _final_hard_cap,
            )
            order.stake_usdt = _final_hard_cap

        # Enforce per-profile max_hold_seconds for spike markets so that a stale
        # BOOM_CRASH_SPIKE_TIMEOUT_SEC env var in Coolify can never override the
        # profile value baked into ASSET_INTEL_PROFILES.
        _profile_mh = float(get_asset_profile(order.symbol).get("max_hold_seconds", 0.0))
        if is_spike_market(order.symbol) and _profile_mh > 0:
            if self._disable_spike_timeout:
                if order.max_hold_seconds > 0:
                    _LOGGER.info(
                        "[deriv-trader] spike_timeout disabled: forcing max_hold_seconds=0 for %s",
                        order.symbol,
                    )
                order.max_hold_seconds = 0.0
            else:
                if order.max_hold_seconds != _profile_mh:
                    _LOGGER.info(
                        "[deriv-trader] max_hold_seconds enforced from profile: %.0fs → %.0fs (%s)",
                        order.max_hold_seconds, _profile_mh, order.symbol,
                    )
                # Apply ATR dynamic extension stored in score_breakdown
                _atr_ext = float((order.score_breakdown or {}).get("atr_hold_extension", 0.0))
                _hold_bonus = self._spike_hold_bonus_sec(order.symbol)
                order.max_hold_seconds = _profile_mh + _atr_ext + _hold_bonus
                if _atr_ext > 0:
                    _LOGGER.info(
                        "[deriv-trader] ATR_HOLD_EXT %s +%.0fs → total=%.0fs",
                        order.symbol, _atr_ext, order.max_hold_seconds,
                    )
                if _hold_bonus > 0:
                    _LOGGER.info(
                        "[deriv-trader] SPIKE_HOLD_BONUS %s +%.0fs → total=%.0fs",
                        order.symbol, _hold_bonus, order.max_hold_seconds,
                    )
        result = await self._client.buy(
            symbol=order.symbol,
            contract_type=order.side,
            stake_usdt=order.stake_usdt,
            multiplier=order.multiplier,
            stop_loss_pct=order.stop_loss_pct,
            take_profit_pct=order.take_profit_pct,
        )
        # `result` IS the buy dict (has contract_id, buy_price, etc.)
        contract_id = int(result.get("contract_id") or 0)
        if contract_id <= 0:
            raise DerivClientError(f"buy returned no contract_id: {result}")

        # Actual stake may differ from intended when the broker capped it.
        # deriv_client injects _actual_stake_usdt when a cap/retry happened.
        actual_stake = float(result.get("_actual_stake_usdt") or order.stake_usdt)

        # Entry price: the buy_price field from Deriv's buy response is the
        # *underlying spot price* when the contract was purchased — NOT the stake.
        # However when the broker retries at a capped stake, buy_price may reflect
        # the new stake rather than the spot. We therefore do a single
        # proposal_open_contract poll immediately after the buy to get the
        # definitive entry_tick_display_value (the broker's own record of the
        # underlying spot at entry). Falls back to buy_price if unavailable.
        entry_price = float(result.get("buy_price") or 0)
        try:
            _poc0 = (
                await self._client.proposal_open_contract(contract_id)
            ).get("proposal_open_contract") or {}
            _spot = float(
                _poc0.get("entry_tick_display_value")
                or _poc0.get("entry_spot")
                or _poc0.get("current_spot")
                or 0
            )
            if _spot > 0:
                entry_price = _spot
        except Exception as _ep_exc:
            _LOGGER.debug(
                "[deriv-trader] entry-spot POC failed for %s: %s", contract_id, _ep_exc
            )

        oc = DerivOpenContract(
            contract_id=contract_id,
            intent_id=order.intent_id,
            symbol=order.symbol,
            side=order.side,
            stake_usdt=actual_stake,
            multiplier=order.multiplier,
            entry_price=entry_price,
            opened_at_ts=time.time(),
            score_breakdown=order.score_breakdown,
            max_hold_seconds=order.max_hold_seconds,
            entry_tick_count=int((order.score_breakdown or {}).get("entry_tick_count", 0)),
        )
        _entry_atr = 0.0
        if self._risk is not None:
            _atr_now = self._risk.get_current_atr(order.symbol)
            if _atr_now is not None and _atr_now > 0:
                _entry_atr = float(_atr_now)
        if _entry_atr <= 0:
            try:
                _entry_atr = float((order.score_breakdown or {}).get("atr_abs") or 0.0)
            except Exception:  # noqa: BLE001
                _entry_atr = 0.0
        oc.dep_peak_atr = max(0.0, _entry_atr)
        async with self._lock:
            self._open[contract_id] = oc
            self._persist_open()

        # Register with DynamicPositionManager for ratchet SL + momentum tracking.
        # Grade C setups carry aggressive_trailing=True from risk engine → tighter ratchet.
        _aggressive_trail = bool((order.score_breakdown or {}).get("aggressive_trailing", False))
        self._dpm.register(
            contract_id=contract_id,
            symbol=order.symbol,
            stake=actual_stake,
            entry_price=entry_price,
            entry_ts=oc.opened_at_ts,
            aggressive_trailing=_aggressive_trail,
            max_duration_override_sec=(order.max_hold_seconds + self._dpm_timeout_buffer_sec()),
        )

        # Subscribe to live broker stream for this contract so we settle
        # immediately on is_sold=1 without waiting for the next reap_closed cycle.
        asyncio.create_task(
            self._client.subscribe_contract(contract_id, self._on_ws_contract_update),
            name=f"poc-sub-{contract_id}",
        )

        _LOGGER.info(
            "[deriv-trader] LIVE buy ok | contract_id=%s symbol=%s side=%s "
            "stake_intended=%.2f stake_actual=%.2f entry=%.5f",
            contract_id, order.symbol, order.side,
            order.stake_usdt, actual_stake, entry_price,
        )
        self._notify("live_buy", {
            "contract_id": contract_id,
            "symbol": order.symbol,
            "side": order.side,
            "stake_usdt": actual_stake,
            "intent_id": order.intent_id,
        })

        return {
            "broker": "deriv",
            "status": "live",
            "contract_id": contract_id,
            "intent_id": order.intent_id,
            "symbol": order.symbol,
            "side": order.side,
            "stake_usdt": actual_stake,
            "multiplier": order.multiplier,
            "entry_price": entry_price,
            "ts": int(time.time() * 1000),
        }

    async def close_contract(self, contract_id: int) -> dict[str, Any]:
        """Manually close (SELL) an open contract — used by the daemon's reaper."""
        if self._settings.dry_run:
            _LOGGER.info("[deriv-trader] DRY-RUN close requested for %s", contract_id)
            return {"status": "dry_run", "contract_id": contract_id}
        return await self._client.sell(contract_id)

    async def _notify_mirror_settlement(self, contract_id: int, reason: str) -> None:
        """Best-effort mirror-close hook when principal settles without explicit sell path.

        Explicit principal sell() calls already trigger mirror closes in the
        DerivMirrorClient wrapper. This hook covers broker-driven settlements
        (TP/SL/forced close) that bypass local sell().
        """
        hook = getattr(self._client, "notify_principal_settled", None)
        if hook is None:
            return
        try:
            await hook(int(contract_id), reason=reason)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[deriv-trader] mirror settlement notify failed contract=%s reason=%s: %s",
                contract_id,
                reason,
                exc,
            )

    async def _boot_sync_mirror_followers(self) -> None:
        """Best-effort boot sync for follower accounts after deploy/restart.

        Delegates to DerivMirrorClient when available. It rebuilds missing
        principal->mirror links and closes follower orphans that no longer have
        a principal counterpart after boot reconciliation.
        """
        hook = getattr(self._client, "boot_sync_followers", None)
        if hook is None:
            return

        async with self._lock:
            principal_open = [
                {
                    "contract_id": cid,
                    "symbol": oc.symbol,
                    "side": oc.side,
                    "stake_usdt": oc.stake_usdt,
                    "opened_at_ts": oc.opened_at_ts,
                }
                for cid, oc in self._open.items()
            ]

        try:
            summary = await hook(principal_open)
            _LOGGER.info("[deriv-recon] BOOT-SYNC mirror summary: %s", summary)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-recon] BOOT-SYNC mirror sync failed: %s", exc)

    async def reap_closed(self) -> list[dict[str, Any]]:
        """
        Poll every open contract; if any has been closed by Deriv (SL/TP hit),
        record it, fire the PAMM webhook, and remove from the open set.
        Returns the list of newly-closed contract dicts.
        """
        if not self._open or self._settings.dry_run:
            return []

        closed_now: list[dict[str, Any]] = []
        async with self._lock:
            ids = list(self._open.keys())

        for cid in ids:
            # ── Spike timeout: force-close BOOM/CRASH contracts past their max hold ──
            # This caps inter-spike drift losses. The sell() call makes Deriv close
            # the contract; the subsequent proposal_open_contract() poll then sees
            # is_sold=True and records the final outcome normally.
            async with self._lock:
                oc_check = self._open.get(cid)
            if oc_check is not None and oc_check.max_hold_seconds > 0:
                _timeout_enabled = not (
                    self._disable_spike_timeout and is_spike_market(oc_check.symbol)
                )
                if not _timeout_enabled:
                    continue
                held = time.time() - oc_check.opened_at_ts
                _base_zero_peak_sec = float(os.getenv("DERIV_ZERO_PEAK_BASE_SEC", "150"))
                _dyn_zero_peak_grace = self._dynamic_zero_peak_grace_sec(oc_check.symbol)
                _dynamic_hold_limit = _base_zero_peak_sec + float(_dyn_zero_peak_grace)
                if is_spike_market(oc_check.symbol):
                    _dynamic_hold_limit = self._dynamic_zero_peak_wait_limit_sec(
                        oc_check.symbol,
                        _dynamic_hold_limit,
                    )
                    # zero_peak_exit is emergency-only; never fire too far ahead of timeout.
                    _pre_timeout_margin = max(
                        10.0,
                        float(os.getenv("DERIV_ZERO_PEAK_PRE_TIMEOUT_MARGIN_SEC", "20") or 20),
                    )
                    _max_emergency_limit = max(0.0, oc_check.max_hold_seconds - _pre_timeout_margin)
                    _dynamic_hold_limit = min(_dynamic_hold_limit, _max_emergency_limit)
                if held >= oc_check.max_hold_seconds:
                    # Phase 35: close-lock guard
                    if cid in self._closing:
                        pass  # sell already in-flight; fall through to poll
                    else:
                        self._closing.add(cid)
                        _LOGGER.info(
                            "[deriv-trader] spike_timeout: force-selling %s (%s held=%.1fs limit=%.0fs)",
                            cid, oc_check.symbol, held, oc_check.max_hold_seconds,
                        )
                        try:
                            await self._client.sell(cid)
                        except DerivClientError as exc:
                            _LOGGER.warning(
                                "[deriv-trader] spike_timeout sell failed for %s: %s", cid, exc
                            )
                            self._closing.discard(cid)  # allow retry next reap
                        # Contract may already be closed; continue to poll below

                # Prueba4-restart: zero_peak_exit — if after 150s the trade NEVER turned
                # profitable (peak_profit=0.0) and is currently negative, cut the loss now.
                # Data basis: BOOM900 62% zero_peak, BOOM600 60% zero_peak — these trades
                # NEVER recover; waiting the full 480-600s hold just multiplies the loss.
                # Condition: held>=150s, peak=0.0, floating<-0.05 (to avoid spread noise).
                elif (
                    self._zero_peak_exit_enabled
                    and is_spike_market(oc_check.symbol)
                    and held >= _dynamic_hold_limit
                    and oc_check.peak_profit == 0.0
                    and oc_check.floating_pnl < -0.05
                    and cid not in self._closing
                ):
                    _defer_zero_peak, _defer_reason = self._should_defer_zero_peak_exit(
                        oc_check.symbol,
                        held,
                    )
                    if _defer_zero_peak:
                        _LOGGER.info(
                            "[deriv-trader] zero_peak_exit deferred: %s (%s) held=%.1fs limit=%.1fs pnl=%.4f %s",
                            cid,
                            oc_check.symbol,
                            held,
                            _dynamic_hold_limit,
                            oc_check.floating_pnl,
                            _defer_reason,
                        )
                        continue
                    self._closing.add(cid)
                    oc_check.pending_close_reason = "zero_peak_exit"
                    _LOGGER.info(
                        "[deriv-trader] zero_peak_exit: %s (%s) held=%.1fs limit=%.1fs peak=0.0 pnl=%.4f — cutting stagnant loss",
                        cid, oc_check.symbol, held, _dynamic_hold_limit, oc_check.floating_pnl,
                    )
                    try:
                        await self._client.sell(cid)
                    except DerivClientError as exc:
                        _LOGGER.warning(
                            "[deriv-trader] zero_peak_exit sell failed for %s: %s", cid, exc
                        )
                        self._closing.discard(cid)
                        oc_check.pending_close_reason = None

                # Spike-wait timeout (2026-05-29 forensic):
                # Cut loss early when the expected spike clearly did not show up.
                # Conditions: spike market, held>=T, peak_profit small relative to
                # stake AND floating loss is meaningful. Defers near an expected
                # spike (re-uses chase/zero_peak defer). Hard-cut variant fires
                # without deferral when far beyond T with severe loss.
                if (
                    self._spike_wait_timeout_enabled
                    and is_spike_market(oc_check.symbol)
                    and cid not in self._closing
                ):
                    _stake = max(0.0, float(oc_check.stake_usdt))
                    _sym_u = str(oc_check.symbol or "").upper()
                    # Per-symbol soft T (derived from real win-hold p75 audit);
                    # default to env DERIV_SPIKE_WAIT_TIMEOUT_SEC when symbol
                    # has no explicit override.
                    _T_soft = float(
                        self._spike_wait_timeout_sec_map.get(
                            _sym_u, self._spike_wait_timeout_sec
                        )
                    )
                    _T_hard = _T_soft + self._spike_wait_hardcut_margin_sec
                    _mfe_cap = _stake * self._spike_wait_timeout_mfe_frac
                    _soft_loss_cap = -_stake * self._spike_wait_timeout_loss_frac
                    _hard_loss_cap = -_stake * self._spike_wait_hardcut_loss_frac
                    _emergency_loss_cap = -_stake * self._spike_wait_emergency_loss_frac
                    _peak_v = float(oc_check.peak_profit)
                    _float_v = float(oc_check.floating_pnl)
                    _soft_hit = (
                        held >= _T_soft
                        and _peak_v <= _mfe_cap
                        and _float_v <= _soft_loss_cap
                    )
                    # Emergency: at T_soft, if loss is already near broker SL,
                    # bypass defer even if the spike-cycle says one is coming.
                    _emergency_hit = (
                        held >= _T_soft
                        and _peak_v <= _mfe_cap
                        and _float_v <= _emergency_loss_cap
                    )
                    _hard_hit = (
                        held >= _T_hard
                        and _float_v <= _hard_loss_cap
                    )
                    if _soft_hit or _hard_hit:
                        if _hard_hit or _emergency_hit:
                            _defer = False
                            _defer_reason = (
                                "hardcut_bypass_defer" if _hard_hit
                                else "emergency_bypass_defer"
                            )
                        else:
                            _defer, _defer_reason = self._should_defer_zero_peak_exit(
                                oc_check.symbol,
                                held,
                            )
                        if _defer:
                            _LOGGER.info(
                                "[deriv-trader] spike_wait_timeout deferred: %s (%s) "
                                "held=%.1fs T=%.0fs peak=%.4f floating=%.4f stake=%.2f %s",
                                cid,
                                oc_check.symbol,
                                held,
                                _T_soft,
                                _peak_v,
                                _float_v,
                                _stake,
                                _defer_reason,
                            )
                        else:
                            self._closing.add(cid)
                            oc_check.pending_close_reason = "spike_wait_timeout"
                            _trigger = (
                                "HARDCUT" if _hard_hit
                                else ("EMERGENCY" if _emergency_hit else "SOFT")
                            )
                            _LOGGER.info(
                                "[deriv-trader] spike_wait_timeout: %s (%s) "
                                "held=%.1fs T_soft=%.0fs T_hard=%.0fs trigger=%s "
                                "peak=%.4f<=%.4f floating=%.4f stake=%.2f "
                                "— closing stagnant trade",
                                cid,
                                oc_check.symbol,
                                held,
                                _T_soft,
                                _T_hard,
                                _trigger,
                                _peak_v,
                                _mfe_cap,
                                _float_v,
                                _stake,
                            )
                            try:
                                await self._client.sell(cid)
                            except DerivClientError as exc:
                                _LOGGER.warning(
                                    "[deriv-trader] spike_wait_timeout sell failed for %s: %s",
                                    cid,
                                    exc,
                                )
                                self._closing.discard(cid)
                                oc_check.pending_close_reason = None

            try:
                resp = await self._client.proposal_open_contract(cid)
            except DerivClientError as exc:
                _LOGGER.warning("[deriv-trader] poll failed for %s: %s", cid, exc)
                continue

            poc = resp.get("proposal_open_contract") or {}
            _poc_status = (poc.get("status") or "").lower()
            # is_sold must be True AND the contract must carry a definitive final
            # status. is_settleable can be True while the contract is still
            # open (just eligible for settlement), which caused ghost-closes with
            # exit_reason='open'. We require a known terminal status.
            _TERMINAL_STATUSES = {"won", "lost", "sold", "cancelled", "expired"}
            is_sold = bool(poc.get("is_sold")) and _poc_status in _TERMINAL_STATUSES

            # ── DynamicPositionManager: ratchet SL + momentum exit ───────────────
            # Replaces the static tiered trailing stop (_compute_trail_floor).
            # DPM tracks per-contract state (fase, sl_ratchet, momentum) and
            # returns a close reason when any condition fires.  The WS callback
            # (_on_ws_contract_update) also calls DPM on every tick for real-time
            # response; this poll-path (every 2s) is the safety-net fallback.
            if not is_sold and oc_check is not None:
                _current_profit = float(poc.get("profit") or 0)
                _current_price  = float(poc.get("current_spot") or 0)
                # BUG-B fix: update floating PnL on poll path (safety-net for WS gaps)
                oc_check.floating_pnl = _current_profit
                _held_sec = time.time() - oc_check.opened_at_ts

                _dep_close, _dep_shadow, _dep_reason, _dep_details = self._dep_decay_signal(
                    oc_check,
                    _held_sec,
                    _current_profit,
                )
                oc_check.dep_last_eval = {
                    "ts": round(time.time(), 3),
                    "should_close": bool(_dep_close),
                    "shadow": bool(_dep_shadow),
                    "reason": _dep_reason or "",
                    **(_dep_details or {}),
                }
                if _dep_shadow:
                    _shadow_every = max(
                        15.0,
                        float(os.getenv("DERIV_DEP_SHADOW_LOG_EVERY_SEC", "60") or 60.0),
                    )
                    _now = time.time()
                    if (_now - float(oc_check.dep_shadow_last_log_ts or 0.0)) >= _shadow_every:
                        oc_check.dep_shadow_last_log_ts = _now
                        _LOGGER.info(
                            "[DEP-SHADOW] cid=%s sym=%s held=%.1fs pnl=%.4f atr_now=%.6f atr_peak=%.6f "
                            "ratio=%.3f thr=%.3f floor=%.4f",
                            cid,
                            oc_check.symbol,
                            _dep_details.get("held_sec", _held_sec),
                            _dep_details.get("current_pnl", _current_profit),
                            _dep_details.get("atr_now", 0.0),
                            _dep_details.get("atr_peak", 0.0),
                            _dep_details.get("atr_ratio", 0.0),
                            _dep_details.get("decay_threshold", 0.0),
                            _dep_details.get("loss_floor_usdt", 0.0),
                        )

                if _dep_close and cid not in self._closing:
                    self._closing.add(cid)
                    oc_check.pending_close_reason = _dep_reason
                    _LOGGER.info(
                        "[DEP-ACTIVE] %s closing symbol=%s reason=%s",
                        cid,
                        oc_check.symbol,
                        _dep_reason,
                    )
                    try:
                        await self._client.sell(cid)
                    except DerivClientError as exc:
                        _LOGGER.warning(
                            "[DEP-ACTIVE] sell failed for %s: %s",
                            cid,
                            exc,
                        )
                        self._closing.discard(cid)
                        oc_check.pending_close_reason = None

                _prob_close, _prob_shadow, _prob_reason, _prob_details = self._open_probability_signal(
                    oc_check,
                    _held_sec,
                    _current_profit,
                    _current_price,
                )
                oc_check.prob_last_eval = {
                    "ts": round(time.time(), 3),
                    "should_close": bool(_prob_close),
                    "shadow": bool(_prob_shadow),
                    "reason": _prob_reason or "",
                    **(_prob_details or {}),
                }
                if _prob_shadow:
                    _now = time.time()
                    if (_now - float(oc_check.prob_shadow_last_log_ts or 0.0)) >= _OPEN_PROB_SHADOW_LOG_EVERY_SEC:
                        oc_check.prob_shadow_last_log_ts = _now
                        _LOGGER.info(
                            "[OPEN-PROB-SHADOW] cid=%s sym=%s held=%.1fs pnl=%.4f prob=%.1f%% thr=%.1f%% "
                            "atr_ratio=%.3f cycle=%.2f",
                            cid,
                            oc_check.symbol,
                            _prob_details.get("held_sec", _held_sec),
                            _prob_details.get("current_pnl", _current_profit),
                            100.0 * float(_prob_details.get("probability", 0.0)),
                            100.0 * float(_prob_details.get("close_threshold", _OPEN_PROB_CLOSE_THRESHOLD)),
                            float(_prob_details.get("atr_ratio", 0.0)),
                            float(_prob_details.get("cycle_progress", 0.0)),
                        )

                if _prob_close and cid not in self._closing:
                    self._closing.add(cid)
                    oc_check.pending_close_reason = _prob_reason
                    _LOGGER.info(
                        "[OPEN-PROB-ACTIVE] %s closing symbol=%s reason=%s",
                        cid,
                        oc_check.symbol,
                        _prob_reason,
                    )
                    try:
                        await self._client.sell(cid)
                    except DerivClientError as exc:
                        _LOGGER.warning(
                            "[OPEN-PROB-ACTIVE] sell failed for %s: %s",
                            cid,
                            exc,
                        )
                        self._closing.discard(cid)
                        oc_check.pending_close_reason = None

                # Telemetry: sample ATR every ~30s to build atr_trajectory
                if self._risk is not None:
                    _sample_interval = 30.0
                    _expected_samples = int(_held_sec / _sample_interval)
                    if _expected_samples > len(oc_check.atr_samples):
                        _atr_now = self._risk.get_current_atr(oc_check.symbol)
                        if _atr_now is not None:
                            oc_check.atr_samples.append(
                                [round(_held_sec, 1), _atr_now]
                            )
                # Sync peak directly so ratchet can never lag behind dashboard PnL.
                if _current_profit > 0:
                    self._dpm.sync_external_peak(cid, _current_profit)
                # Sync peak directly so ratchet can never lag behind dashboard PnL.
                if _current_profit > 0:
                    self._dpm.sync_external_peak(cid, _current_profit)
                _close_reason   = self._dpm.on_tick(cid, _current_profit, _current_price)
                # Sync DPM state back to oc for dashboard/persistence
                _snap = self._dpm.get_state_snapshot(cid)
                if _snap:
                    _old_peak = oc_check.peak_profit
                    _old_sl   = oc_check.trail_sl_locked
                    oc_check.peak_profit     = _snap["peak_profit"]
                    oc_check.trail_sl_locked = _snap["sl_ratchet"]
                    if oc_check.peak_profit != _old_peak or oc_check.trail_sl_locked != _old_sl:
                        async with self._lock:
                            self._persist_open()
                # BUG-B fix: broker-level BE lock when DPM reaches Phase 2
                if (
                    _snap
                    and _snap.get("dpm_fase") == 2
                    and not oc_check.broker_be_locked
                    and not self._settings.dry_run
                ):
                    oc_check.broker_be_locked = True
                    asyncio.create_task(
                        self._client.contract_update(cid, stop_loss=0.01),
                        name=f"be-lock-reaper-{cid}",
                    )
                    _LOGGER.info(
                        "[DPM-BE] broker BE locked via reaper contract_id=%s "
                        "symbol=%s pnl=%.4f floor=%.4f",
                        cid, oc_check.symbol, _current_profit,
                        oc_check.trail_sl_locked,
                    )
                if _close_reason:
                    # Phase 35: close-lock guard
                    if cid not in self._closing:
                        self._closing.add(cid)
                        _LOGGER.info(
                            "[DPM-REAPER] %s closing symbol=%s reason=%s pnl=%.4f",
                            cid, oc_check.symbol, _close_reason, _current_profit,
                        )
                        oc_check.pending_close_reason = _close_reason
                        try:
                            await self._client.sell(cid)
                        except DerivClientError as exc:
                            _LOGGER.warning(
                                "[DPM-REAPER] sell failed for %s: %s — poll will detect close",
                                cid, exc,
                            )
                            self._closing.discard(cid)  # allow retry next reap
                    # Fall through — next poll will see is_sold=True

            if not is_sold:
                continue

            realized = float(poc.get("profit") or 0)
            # Exit price: prefer the underlying spot at the time of the sell/settlement
            # (sell_spot or exit_tick_display_value), not the USD sell_price which is
            # the money received back (stake ± P&L) — a completely different value.
            exit_price = float(
                poc.get("sell_spot")
                or poc.get("exit_tick_display_value")
                or poc.get("exit_spot")
                or poc.get("current_spot")
                or poc.get("sell_price")
                or 0
            )
            # ── Exit reason classification (DPM-aware) ─────────────────────────
            # Priority order:
            #   1. spike_timeout (max_hold_seconds breached)
            #   2. pending_close_reason (DPM-set before sell() call — real reason)
            #   3. ratchet_sl_alcanzado (legacy ratchet floor detection)
            #   4. broker-classified exit (won/lost/sold → manual_close only if none above)
            _was_ratchet_reaper = (
                oc_check is not None
                and oc_check.trail_sl_locked > _TRAIL_INIT_SL   # DPM Phase 2 activated
                and realized <= oc_check.trail_sl_locked + 0.01  # hit the floor
            )
            if (
                oc_check is not None
                and oc_check.max_hold_seconds > 0
                and not (
                    self._disable_spike_timeout and is_spike_market(oc_check.symbol)
                )
            ):
                held = time.time() - oc_check.opened_at_ts
                if held >= oc_check.max_hold_seconds:
                    exit_reason = "spike_timeout"
                elif oc_check.pending_close_reason:
                    exit_reason = oc_check.pending_close_reason
                elif _was_ratchet_reaper:
                    exit_reason = f"ratchet_sl_alcanzado(floor={oc_check.trail_sl_locked:.4f})"
                else:
                    exit_reason = self._classify_exit(poc)
            elif oc_check is not None and oc_check.pending_close_reason:
                exit_reason = oc_check.pending_close_reason
            elif _was_ratchet_reaper:
                # Non-spike DPM symbol (R_50/R_75/R_100) — ratchet hit
                exit_reason = f"ratchet_sl_alcanzado(floor={oc_check.trail_sl_locked:.4f})"
            else:
                exit_reason = self._classify_exit(poc)

            async with self._lock:
                oc = self._open.pop(cid, None)
                self._closing.discard(cid)  # Phase 35: release close-lock on reaper settlement
                self._spike_buffer.pop(cid, None)  # Phase 37: clear buffer on close
                self._persist_open()
            if oc is None:
                continue

            await self._notify_mirror_settlement(cid, "reaper_settled")

            _dpm_stats = self._dpm.get_close_stats(cid, realized)
            self._dpm.unregister(cid)
            record = {
                "broker": "deriv",
                "contract_id": cid,
                "intent_id": oc.intent_id,
                "symbol": oc.symbol,
                "side": oc.side,
                "stake_usdt": oc.stake_usdt,
                "entry_price": oc.entry_price,
                "exit_price": exit_price,
                "realized_pnl_usdt": realized,
                "exit_reason": exit_reason,
                "closed_by": "reaper_poll",
                "opened_at_ts": oc.opened_at_ts,
                "closed_at_ts": time.time(),
                "score_breakdown": oc.score_breakdown,
                "max_hold_seconds": oc.max_hold_seconds,
                **_dpm_stats,
            }
            self._append_closed(record, oc)
            await self._post_pamm_webhook(record)
            self._notify("close", record)
            self._update_sym_stats(record)
            closed_now.append(record)

        return closed_now

    def _update_sym_stats(self, record: dict[str, Any]) -> None:
        sym = record.get("symbol", "unknown")
        pnl = float(record.get("realized_pnl_usdt") or 0)
        if sym not in self._sym_stats:
            self._sym_stats[sym] = {
                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "best": None, "worst": None,
                "grades": {
                    "A": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                    "B": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                    "C": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                },
            }
        s = self._sym_stats[sym]
        # Ensure legacy entries (pre-phase9) have the grades subdict
        if "grades" not in s:
            s["grades"] = {
                "A": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                "B": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                "C": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            }
        s["trades"] += 1
        s["pnl"] = round(s["pnl"] + pnl, 6)
        if pnl > 0:
            s["wins"] += 1
        elif pnl < 0:
            s["losses"] += 1
        if s["best"] is None or pnl > s["best"]:
            s["best"] = pnl
        if s["worst"] is None or pnl < s["worst"]:
            s["worst"] = pnl
        # Per-grade tracking (execution_grade injected into record by _append_closed)
        _grade = str(record.get("execution_grade") or "").upper()
        if _grade in ("A", "B", "C"):
            _gs = s["grades"][_grade]
            _gs["trades"] += 1
            _gs["pnl"] = round(_gs["pnl"] + pnl, 6)
            if pnl > 0:
                _gs["wins"] += 1
            elif pnl < 0:
                _gs["losses"] += 1
        self._session_pnl = round(self._session_pnl + pnl, 6)
        self._session_trades += 1

    def get_per_symbol_stats(self) -> dict[str, Any]:
        """Return a snapshot of per-symbol performance and session totals."""
        result: dict[str, Any] = {}
        for sym, s in self._sym_stats.items():
            wr = round(s["wins"] / s["trades"], 4) if s["trades"] > 0 else 0.0
            # Compute per-grade win-rates inline so the frontend can display EV by grade
            grades_out: dict[str, Any] = {}
            for grade, gdata in s.get("grades", {}).items():
                g_wr = round(gdata["wins"] / gdata["trades"], 4) if gdata["trades"] > 0 else 0.0
                grades_out[grade] = {**gdata, "win_rate": g_wr}
            result[sym] = {**s, "win_rate": wr, "grades": grades_out}
        result["_session"] = {
            "pnl": self._session_pnl,
            "trades": self._session_trades,
        }
        return result

    def get_open_contracts_for_status(self) -> list[dict[str, Any]]:
        """Return a safe serialisable snapshot of all open contracts."""
        _now = time.time()
        return [
            {
                "contract_id": oc.contract_id,
                "symbol": oc.symbol,
                "side": oc.side,
                "stake_usdt": oc.stake_usdt,
                "multiplier": oc.multiplier,
                "entry_price": oc.entry_price,
                "opened_at_ts": oc.opened_at_ts,
                # BUG-B fix: live floating PnL for dashboard display
                "floating_pnl": round(oc.floating_pnl, 4),
                "peak_profit": round(oc.peak_profit, 4),
                "trail_sl": round(oc.trail_sl_locked, 4),
                "duration_sec": round(_now - oc.opened_at_ts, 1),
                "broker_be_locked": oc.broker_be_locked,
                "max_hold_seconds": round(float(oc.max_hold_seconds or 0.0), 2),
                "entry_tick_count": int(oc.entry_tick_count or 0),
                "dep_peak_atr": round(float(oc.dep_peak_atr or 0.0), 6),
                "pending_close_reason": oc.pending_close_reason,
                "score_breakdown": oc.score_breakdown or {},
                "dep_last_eval": oc.dep_last_eval or {},
                "prob_last_eval": oc.prob_last_eval or {},
            }
            for oc in self._open.values()
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # Persistence (atomic writes, broker-scoped)
    # ─────────────────────────────────────────────────────────────────────────
    def _persist_open(self) -> None:
        path = self._settings.open_contracts_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        body = [
            {
                "contract_id": oc.contract_id,
                "intent_id": oc.intent_id,
                "symbol": oc.symbol,
                "side": oc.side,
                "stake_usdt": oc.stake_usdt,
                "multiplier": oc.multiplier,
                "entry_price": oc.entry_price,
                "opened_at_ts": oc.opened_at_ts,
                "score_breakdown": oc.score_breakdown,
                "max_hold_seconds": oc.max_hold_seconds,
                "peak_profit": oc.peak_profit,
                "trail_sl_locked": oc.trail_sl_locked,
                # BUG-B fix: include live floating PnL so deriv_open_contracts.json
                # always has the last-known PnL when the file is written
                "floating_pnl": round(oc.floating_pnl, 4),
                # Phase 15 fix: persist broker_be_locked so restarts can re-apply
                # the SL=$0.01 without waiting for Phase 2 to fire again.
                "broker_be_locked": oc.broker_be_locked,
                "dep_peak_atr": round(oc.dep_peak_atr, 6),
                "dep_last_eval": oc.dep_last_eval or {},
                "prob_last_eval": oc.prob_last_eval or {},
            }
            for oc in self._open.values()
        ]
        tmp.write_text(json.dumps(body, indent=2))
        tmp.replace(path)

    def _append_closed(self, record: dict[str, Any], oc: "DerivOpenContract | None" = None) -> None:
        # ── Telemetry injection: surface grade-level audit fields as top-level keys ──
        # Allows post-trade analysis to filter deriv_closed_contracts.json by grade
        # without parsing score_breakdown JSONB.  setdefault() prevents overwrite
        # when caller already populated the field (ghost/reconciliation paths).
        _sb = record.get("score_breakdown") or {}
        record.setdefault("execution_grade", _sb.get("execution_grade", "?"))
        _fvg_tier = str(_sb.get("fvg_tier", ""))
        record.setdefault("fvg_bypassed", _fvg_tier in ("no_fvg_escape_valve", "no_fvg_penalized"))
        record.setdefault("hurst_penalty_applied", "neutral_zone_penalty" in _sb)
        # ── Rich telemetry injected at close time ─────────────────────────
        # These fields enable per-trade forensics without reprocessing score_breakdown.
        record.setdefault("spread_at_entry", round(float(_sb.get("spread_pct_at_entry", 0)), 5))
        record.setdefault("ema200_distance_at_entry_pct", _sb.get("ema200_dev_pct"))
        # ticks_held: entry_tick_count is stored in score_breakdown by main_deriv;
        # close_tick_count is fetched from risk manager if available.
        _entry_ticks = int(_sb.get("entry_tick_count", 0))
        _sym = str(record.get("symbol", ""))
        _close_ticks = self._risk.get_tick_count(_sym) if self._risk is not None else 0
        if _entry_ticks > 0 and _close_ticks >= _entry_ticks:
            record.setdefault("ticks_held", _close_ticks - _entry_ticks)
        # atr_trajectory: sampled every 30s during the trade by reap_closed loop
        if oc is not None and oc.atr_samples:
            record.setdefault("atr_trajectory", oc.atr_samples)
        # ── Append to JSON file ────────────────────────────────────────────
        path = self._settings.closed_contracts_file
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing: list[Any] = json.loads(path.read_text()) if path.exists() else []
        except (OSError, json.JSONDecodeError):
            existing = []
        if not isinstance(existing, list):
            existing = []
        existing.append(record)
        # Trim to the last 5 000 closes to keep the file bounded.
        existing = existing[-5000:]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2, default=str))
        tmp.replace(path)
        # Enrich spike record when an existing position captured the spike
        if record.get("exit_reason") in (
            "spike_tp",
            "spike_capture",
            "ratchet_hit",
            "timeout_multispike",
        ):
            self._enrich_spike_captured_by_pos(
                record.get("symbol", ""),
                float(record.get("realized_pnl_usdt", 0)),
            )

    def _enrich_spike_captured_by_pos(self, symbol: str, pnl: float) -> None:
        """Mark the most-recent spike record for *symbol* as captured by an existing position.

        Called whenever a contract closes with spike_tp / spike_capture.  When
        the spike fired the bot may have had an open position for that symbol
        (block_reason='trade_cooldown', had_open_pos=True).  That position later
        exited via spike_tp, so the spike WAS in fact captured — just not by a
        new entry.  Setting captured_by_existing_pos=True allows the frontend to
        count this as a «caught» spike instead of a «blocked / missed» one.
        """
        try:
            _state_dir = Path(
                os.environ.get(
                    "BOT_STATE_DIR",
                    os.environ.get("LOGS_DIR", str(self._settings.closed_contracts_file.parent)),
                )
            )
            _spike_file = _state_dir / "deriv_spike_events.json"
            if not _spike_file.exists():
                return
            _existing: list = json.loads(_spike_file.read_text())
            _now = time.time()
            for i in range(len(_existing) - 1, -1, -1):
                ev = _existing[i]
                if ev.get("symbol") != symbol:
                    continue
                _ev_ts = float(ev.get("ts", 0))
                if _now - _ev_ts > 3600:   # ignore spikes older than 1 hour
                    break
                if ev.get("had_open_pos") and ev.get("bot_entered") is False:
                    _existing[i]["captured_by_existing_pos"] = True
                    _existing[i]["spike_tp_pnl"] = round(pnl, 4)
                    _spike_file.write_text(json.dumps(_existing))
                    _LOGGER.debug(
                        "[deriv-trader] spike captured_by_existing_pos enriched: "
                        "symbol=%s pnl=%.4f", symbol, pnl,
                    )
                    break
        except Exception as _e:
            _LOGGER.debug("[deriv-trader] _enrich_spike_captured_by_pos failed: %s", _e)
        # Enrich spike record when an existing position captured the spike
        if record.get("exit_reason") in ("spike_tp", "spike_capture"):
            self._enrich_spike_captured_by_pos(
                record.get("symbol", ""),
                float(record.get("realized_pnl_usdt", 0)),
            )

    def _enrich_spike_captured_by_pos(self, symbol: str, pnl: float) -> None:
        """Mark the most-recent spike record for *symbol* as captured by an existing position.

        Called whenever a contract closes with spike_tp / spike_capture.  When
        the spike fired the bot may have had an open position for that symbol
        (block_reason='trade_cooldown', had_open_pos=True).  That position later
        exited via spike_tp, so the spike WAS in fact captured — just not by a
        new entry.  Setting captured_by_existing_pos=True allows the frontend to
        count this as a «caught» spike instead of a «blocked / missed» one.
        """
        try:
            _state_dir = Path(
                os.environ.get(
                    "BOT_STATE_DIR",
                    os.environ.get("LOGS_DIR", str(self._settings.closed_contracts_file.parent)),
                )
            )
            _spike_file = _state_dir / "deriv_spike_events.json"
            if not _spike_file.exists():
                return
            _existing: list = json.loads(_spike_file.read_text())
            _now = time.time()
            for i in range(len(_existing) - 1, -1, -1):
                ev = _existing[i]
                if ev.get("symbol") != symbol:
                    continue
                _ev_ts = float(ev.get("ts", 0))
                if _now - _ev_ts > 3600:   # ignore spikes older than 1 hour
                    break
                if ev.get("had_open_pos") and ev.get("bot_entered") is False:
                    _existing[i]["captured_by_existing_pos"] = True
                    _existing[i]["spike_tp_pnl"] = round(pnl, 4)
                    _spike_file.write_text(json.dumps(_existing))
                    _LOGGER.debug(
                        "[deriv-trader] spike captured_by_existing_pos enriched: "
                        "symbol=%s pnl=%.4f", symbol, pnl,
                    )
                    break
        except Exception as _e:
            _LOGGER.debug("[deriv-trader] _enrich_spike_captured_by_pos failed: %s", _e)

    # ─────────────────────────────────────────────────────────────────────────
    # PAMM webhook (broker='deriv')
    # ─────────────────────────────────────────────────────────────────────────
    def _resolve_webhook_user_ids(self) -> list[str]:
        """Resolve unique recipient user_ids for Deriv PAMM settlements.

        Priority order:
          1) DERIV_USER_ID from runtime settings (backward compatibility)
          2) enabled entries from DERIV_MULTI_ACCOUNTS_FILE
        """
        candidates: list[str] = []

        primary_user_id = str(self._settings.user_id or "").strip()
        if primary_user_id:
            candidates.append(primary_user_id)

        path = resolve_multi_accounts_path()
        if path is not None and path.exists():
            try:
                cfg = load_multi_accounts_config(path)
                for account in cfg.accounts:
                    if not account.enabled:
                        continue
                    user_id = str(account.user_id or "").strip()
                    if user_id:
                        candidates.append(user_id)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "[deriv-trader] webhook recipients fallback to DERIV_USER_ID only: "
                    "cannot read multi-account file %s (%s)",
                    path,
                    exc,
                )

        unique: list[str] = []
        seen: set[str] = set()
        for user_id in candidates:
            if user_id in seen:
                continue
            seen.add(user_id)
            unique.append(user_id)
        return unique

    async def _post_pamm_webhook(self, record: dict[str, Any]) -> None:
        url = self._settings.pamm_webhook_url
        secret = self._settings.webhook_secret
        if not url or not secret:
            _LOGGER.warning(
                "[deriv-trader] webhook skipped — missing PAMM_WEBHOOK_URL/WEBHOOK_SECRET",
            )
            return

        webhook_user_ids = self._resolve_webhook_user_ids()
        if not webhook_user_ids:
            _LOGGER.warning(
                "[deriv-trader] webhook skipped — no recipient user_ids (DERIV_USER_ID or multi-account file)",
            )
            return

        # ── Ghost-close bypass ────────────────────────────────────────────────
        # Contracts whose exit couldn't be classified (no broker history found)
        # carry zero PnL and would cause noisy HTTP 500s in the PAMM webhook.
        # Classified ghost exits (broker_sl, broker_tp, broker_trailing_stop) DO
        # fire the webhook so the PAMM allocation engine records the actual PnL.
        _NO_WEBHOOK_REASONS = {"lost_or_ghost_closed", "ghost_unknown", "broker_closed_zero"}
        if str(record.get("exit_reason")) in _NO_WEBHOOK_REASONS:
            _LOGGER.info(
                "[deriv-trader] webhook skipped (%s, no reliable PnL) "
                "contract_id=%s — local JSON purge already syncs UI",
                record.get("exit_reason"), record.get("contract_id"),
            )
            return

        base_payload = {
            "tradeId": f"deriv:{record['contract_id']}",
            "rawPnl": float(record["realized_pnl_usdt"]),
            "binanceFee": 0.0,    # Deriv fees already netted into realized PnL
            "symbol": record["symbol"],
            "side": "BUY" if record["side"] == "MULTUP" else "SELL",
            "exitReason": record["exit_reason"],
            "broker": "deriv",
            # Entry context — backend stores in deriv_contracts.score_breakdown JSONB
            "score_breakdown": record.get("score_breakdown"),
        }
        timeout = aiohttp.ClientTimeout(total=8)
        semaphore = asyncio.Semaphore(32)

        async def _post_one_user(session: aiohttp.ClientSession, user_id: str) -> bool:
            payload = {**base_payload, "userId": user_id}
            short_uid = user_id[:8]
            async with semaphore:
                try:
                    async with session.post(
                        url,
                        json=payload,
                        headers={"Authorization": f"Bearer {secret}"},
                    ) as resp:
                        text = await resp.text()
                        if resp.status == 500:
                            _LOGGER.warning(
                                "[deriv-trader] webhook user=%s -> HTTP 500 "
                                "contract_id=%s (trade kept locally)",
                                short_uid,
                                record["contract_id"],
                            )
                            return False
                        if resp.status >= 300:
                            _LOGGER.warning(
                                "[deriv-trader] webhook user=%s -> HTTP %s: %s",
                                short_uid,
                                resp.status,
                                text[:200],
                            )
                            return False
                        _LOGGER.info(
                            "[deriv-trader] webhook OK user=%s contract_id=%s pnl=%.4f",
                            short_uid,
                            record["contract_id"],
                            base_payload["rawPnl"],
                        )
                        return True
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "[deriv-trader] webhook timeout user=%s contract_id=%s (>8s)",
                        short_uid,
                        record["contract_id"],
                    )
                    return False
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "[deriv-trader] webhook POST failed user=%s contract_id=%s: %s",
                        short_uid,
                        record["contract_id"],
                        exc,
                    )
                    return False

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                tasks = [
                    asyncio.create_task(
                        _post_one_user(session, user_id),
                        name=f"pamm-webhook-{user_id[:8]}",
                    )
                    for user_id in webhook_user_ids
                ]
                outcomes = await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[deriv-trader] webhook batch failed contract_id=%s users=%d: %s",
                record["contract_id"],
                len(webhook_user_ids),
                exc,
            )
            return

        ok_count = sum(1 for ok in outcomes if ok)
        fail_count = len(outcomes) - ok_count
        _LOGGER.info(
            "[deriv-trader] webhook batch done contract_id=%s users=%d ok=%d fail=%d",
            record["contract_id"],
            len(webhook_user_ids),
            ok_count,
            fail_count,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _classify_exit(poc: dict[str, Any]) -> str:
        """Best-effort mapping of Deriv close codes to our internal vocabulary.

        Phase10 FIX (2026-05-19): expanded classification — previous version
        returned 'unknown' for any status not in {won, lost, sold}, which made
        ~50% of close reasons opaque (expired, cancelled, settled, missing).
        Now ALL terminal statuses get a specific label.
        """
        status = (poc.get("status") or "").lower().strip()
        if status == "won":
            return "take_profit"
        if status == "lost":
            return "stop_loss"
        if status == "sold":
            return "manual_close"
        if status in ("expired", "settled"):
            return "contract_expired"
        if status == "cancelled":
            return "broker_cancelled"
        if status == "open":
            # Bug detector: settled-but-status-open should never reach here
            return "settled_open_bug"
        if not status:
            # BUG-A fix (2026-05-19 phase13): broker sends close events without
            # a status field on forced multiplier-SL closes via WS push.
            # Infer exit reason from PnL context so closed trades carry a
            # meaningful label instead of opaque "unknown_no_status".
            _pnl   = float(poc.get("profit") or 0)
            _stake = float(poc.get("buy_price") or poc.get("ask_price") or 0)
            # Prefer broker-set SL amount from limit_order (present in REST responses)
            _sl_amount = float(
                ((poc.get("limit_order") or {}).get("stop_loss") or {}).get(
                    "order_amount", 0
                )
            )
            if _sl_amount > 0 and _pnl <= -(_sl_amount * 0.85):
                return "broker_sl_hit"
            if _stake > 0:
                if _pnl <= -(_stake * 0.28):
                    return "broker_sl_hit"
                if _pnl >= _stake * 0.08:
                    return "tp_or_ratchet"
            elif _pnl <= -0.35:
                return "broker_sl_hit"
            elif _pnl >= 0.05:
                return "tp_or_ratchet"
            if abs(_pnl) <= 0.05:
                return "breakeven_or_zero"
            return f"unknown_pnl_{_pnl:+.2f}"
        return f"broker_{status}"

    # ─────────────────────────────────────────────────────────────────────────
    # Live WS update callback
    # Fired on EVERY broker POC push (tick updates + is_sold=1 close events).
    # • For ongoing ticks: evaluates DynamicPositionManager; sells if triggered.
    # • For is_sold=1: immediate settlement without waiting for reap_closed().
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_ws_contract_update(self, poc: dict[str, Any]) -> None:
        """WS callback: handles both tick updates and final settlement."""
        cid = int(poc.get("contract_id") or 0)
        if cid <= 0:
            return

        # ── Ongoing tick: run DPM evaluation ─────────────────────────────────
        if not poc.get("is_sold"):
            async with self._lock:
                oc_check = self._open.get(cid)
            if oc_check is None:
                return
            current_profit = float(poc.get("profit") or 0)
            current_price  = float(poc.get("current_spot") or 0)
            # BUG-B fix: persist live PnL on every tick so dashboard shows real value
            oc_check.floating_pnl = current_profit
            # Phase 18: force-sync DPM peak so ratchet never lags behind floating_pnl
            if current_profit > 0:
                self._dpm.sync_external_peak(cid, current_profit)

            # ── Multispike ratchet (tick-domain) for BOOM/CRASH ─────────────
            # We do not close immediately at spike_tp. Instead we open a short
            # observation window measured in ticks and protect gains with a
            # ratchet floor:
            #   close if pnl <= max(min_floor, peak * retention_pct)
            #   OR if ticks_since_last_spike > drift_ticks.
            # This captures clustered spikes while avoiding long drift givebacks.

            if is_spike_market(oc_check.symbol) and current_profit > 0.10:

                # ── Check active spike buffer first ──────────────────────────
                _sbuf = self._spike_buffer.get(cid)
                if _sbuf is not None:
                    # Update peak while observing potential multi-spike cluster.
                    if current_profit > _sbuf["peak_pnl"]:
                        _sbuf["peak_pnl"] = current_profit

                    _ticks_since_spike = self._ticks_since_last_spike(
                        oc_check.symbol,
                        int(_sbuf.get("start_tick") or 0),
                    )
                    _retention_pct = float(_sbuf.get("retention_pct") or 0.70)
                    _min_floor = float(_sbuf.get("min_floor_usdt") or 0.15)
                    _drift_ticks = int(_sbuf.get("drift_ticks") or _sbuf.get("buffer_ticks") or 45)

                    _ratchet_floor = max(_min_floor, _sbuf["peak_pnl"] * _retention_pct)
                    _ratchet_hit = current_profit <= _ratchet_floor
                    _timeout_hit = _ticks_since_spike > _drift_ticks
                    _close_reason = "ratchet_hit" if _ratchet_hit else "timeout_multispike"

                    if _ratchet_hit or _timeout_hit:
                        if cid not in self._closing:
                            self._closing.add(cid)
                            _LOGGER.info(
                                "[SPIKE-BUFFER] cid=%s sym=%s CLOSE reason=%s "
                                "pnl=%.4f peak=%.4f ratchet=%.4f ticks_since_spike=%dt drift_ticks=%dt regime=%s",
                                cid, oc_check.symbol, _close_reason,
                                current_profit, _sbuf["peak_pnl"],
                                _ratchet_floor,
                                _ticks_since_spike,
                                _drift_ticks,
                                str(_sbuf.get("regime") or "NORMAL"),
                            )
                            oc_check.pending_close_reason = _close_reason
                            del self._spike_buffer[cid]
                            oc_check.last_profit = current_profit
                            try:
                                await self._client.sell(cid)
                            except DerivClientError as exc:
                                _LOGGER.warning(
                                    "[SPIKE-BUFFER] sell failed cid=%s: %s", cid, exc)
                                self._closing.discard(cid)
                        return
                    # Buffer still active — keep waiting, skip DPM this tick
                    oc_check.last_profit = current_profit
                    return

                # ── Evaluate spike triggers (buffer not yet active) ──────────
                _sc_prof  = get_asset_profile(oc_check.symbol)
                _sc_tp    = float(_sc_prof.get(
                    "spike_capture_tp_usdt", oc_check.stake_usdt * 0.25))
                _sc_delta = float(_sc_prof.get(
                    "spike_profit_delta_usdt", oc_check.stake_usdt * 0.15))
                _tp_hit    = current_profit >= _sc_tp
                _delta_hit = (
                    oc_check.last_profit >= 0.0
                    and (current_profit - oc_check.last_profit) >= _sc_delta
                )
                if _tp_hit or _delta_hit:
                    if cid in self._closing:
                        _LOGGER.debug(
                            "[SPIKE-BUFFER] cid=%s already closing — skipped", cid)
                        return
                    _policy = self._multispike_policy(oc_check.symbol)
                    _start_tick = self._current_tick_count(oc_check.symbol)
                    # Enter spike buffer instead of selling immediately
                    self._spike_buffer[cid] = {
                        "peak_pnl": current_profit,
                        "entry_pnl": current_profit,
                        "start_tick": _start_tick,
                        "buffer_ticks": int(_policy["buffer_ticks"]),
                        "retention_pct": float(_policy["retention_pct"]),
                        "min_floor_usdt": float(_policy["min_floor_usdt"]),
                        "drift_ticks": int(_policy["drift_ticks"]),
                        "regime": str(_policy["regime"]),
                    }
                    _sc_why = "spike_tp" if _tp_hit else "spike_capture"
                    _LOGGER.info(
                        "[SPIKE-BUFFER] cid=%s sym=%s ENTER reason=%s pnl=%.4f "
                        "jump=%.4f tp_thresh=%.4f delta_thresh=%.4f "
                        "buffer_ticks=%dt retention=%.2f floor=%.2f drift_ticks=%dt regime=%s",
                        cid, oc_check.symbol, _sc_why, current_profit,
                        current_profit - max(oc_check.last_profit, 0.0),
                        _sc_tp,
                        _sc_delta,
                        int(_policy["buffer_ticks"]),
                        float(_policy["retention_pct"]),
                        float(_policy["min_floor_usdt"]),
                        int(_policy["drift_ticks"]),
                        str(_policy["regime"]),
                    )
                    oc_check.last_profit = current_profit
                    return  # Don't close yet — buffer will decide
            oc_check.last_profit = current_profit
            # ─────────────────────────────────────────────────────────────────

            _held_sec = time.time() - oc_check.opened_at_ts
            _prob_close, _prob_shadow, _prob_reason, _prob_details = self._open_probability_signal(
                oc_check,
                _held_sec,
                current_profit,
                current_price,
            )
            oc_check.prob_last_eval = {
                "ts": round(time.time(), 3),
                "should_close": bool(_prob_close),
                "shadow": bool(_prob_shadow),
                "reason": _prob_reason or "",
                **(_prob_details or {}),
            }
            if _prob_shadow:
                _now = time.time()
                if (_now - float(oc_check.prob_shadow_last_log_ts or 0.0)) >= _OPEN_PROB_SHADOW_LOG_EVERY_SEC:
                    oc_check.prob_shadow_last_log_ts = _now
                    _LOGGER.info(
                        "[OPEN-PROB-SHADOW] cid=%s sym=%s held=%.1fs pnl=%.4f prob=%.1f%% thr=%.1f%% "
                        "atr_ratio=%.3f cycle=%.2f (ws)",
                        cid,
                        oc_check.symbol,
                        _prob_details.get("held_sec", _held_sec),
                        _prob_details.get("current_pnl", current_profit),
                        100.0 * float(_prob_details.get("probability", 0.0)),
                        100.0 * float(_prob_details.get("close_threshold", _OPEN_PROB_CLOSE_THRESHOLD)),
                        float(_prob_details.get("atr_ratio", 0.0)),
                        float(_prob_details.get("cycle_progress", 0.0)),
                    )
            if _prob_close:
                if cid in self._closing:
                    _LOGGER.debug(
                        "[OPEN-PROB-WS] cid=%s already closing — skipped duplicate sell", cid)
                    return
                self._closing.add(cid)
                oc_check.pending_close_reason = _prob_reason
                _LOGGER.info(
                    "[OPEN-PROB-WS] %s closing symbol=%s reason=%s pnl=%.4f",
                    cid,
                    oc_check.symbol,
                    _prob_reason,
                    current_profit,
                )
                try:
                    await self._client.sell(cid)
                except DerivClientError as exc:
                    _LOGGER.warning("[OPEN-PROB-WS] sell failed for %s: %s", cid, exc)
                    self._closing.discard(cid)
                    oc_check.pending_close_reason = None
                return

            close_reason   = self._dpm.on_tick(cid, current_profit, current_price)
            # Sync DPM state to oc for dashboard visibility
            _snap = self._dpm.get_state_snapshot(cid)
            if _snap:
                oc_check.peak_profit     = _snap["peak_profit"]
                oc_check.trail_sl_locked = _snap["sl_ratchet"]
            # BUG-B fix: when DPM reaches Phase 2, push broker-level BE lock
            # ($0.01 stop_loss) so a missed-peak tick can't result in a full
            # loss.  Fire-and-forget task; logged below.  Only once per contract.
            if (
                _snap
                and _snap.get("dpm_fase") == 2
                and not oc_check.broker_be_locked
                and not self._settings.dry_run
            ):
                oc_check.broker_be_locked = True
                asyncio.create_task(
                    self._client.contract_update(cid, stop_loss=0.01),
                    name=f"be-lock-ws-{cid}",
                )
                _LOGGER.info(
                    "[DPM-BE] broker BE locked via WS contract_id=%s "
                    "symbol=%s pnl=%.4f floor=%.4f",
                    cid, oc_check.symbol, current_profit,
                    oc_check.trail_sl_locked,
                )
            if close_reason:
                # Phase 35: close-lock guard — bail if sell already in-flight
                if cid in self._closing:
                    _LOGGER.debug(
                        "[DPM-WS] cid=%s already closing — skipped duplicate sell", cid)
                    return
                self._closing.add(cid)
                _LOGGER.info(
                    "[DPM-WS] %s closing symbol=%s reason=%s pnl=%.4f "
                    "fase=%s sl=%.4f",
                    cid, oc_check.symbol, close_reason, current_profit,
                    _snap.get("dpm_fase", "?"), _snap.get("sl_ratchet", 0),
                )
                oc_check.pending_close_reason = close_reason
                try:
                    await self._client.sell(cid)
                except DerivClientError as exc:
                    _LOGGER.warning("[DPM-WS] sell failed for %s: %s", cid, exc)
                    self._closing.discard(cid)  # allow retry on next tick
            return

        # ── is_sold=True: immediate settlement ───────────────────────────────
        async with self._lock:
            oc = self._open.pop(cid, None)
            if oc is None:
                self._closing.discard(cid)
                return  # already settled by reaper or reconciliation
            self._closing.discard(cid)  # Phase 35: release close-lock on final settlement
            self._spike_buffer.pop(cid, None)  # Phase 37: clear buffer on close
            self._persist_open()

        await self._notify_mirror_settlement(cid, "ws_settled")

        realized   = float(poc.get("profit") or 0)
        exit_price = float(
            poc.get("sell_spot")
            or poc.get("exit_tick_display_value")
            or poc.get("exit_spot")
            or poc.get("current_spot")
            or poc.get("sell_price")
            or 0
        )
        held = time.time() - oc.opened_at_ts

        _dpm_stats = self._dpm.get_close_stats(cid, realized)
        self._dpm.unregister(cid)

        # Exit reason: prefer DPM reason if the SL floor was hit
        _was_ratchet = (
            oc.trail_sl_locked > _TRAIL_INIT_SL
            and realized <= oc.trail_sl_locked + 0.01
        )
        if (
            oc.max_hold_seconds > 0
            and held >= oc.max_hold_seconds
            and not (self._disable_spike_timeout and is_spike_market(oc.symbol))
        ):
            exit_reason = "spike_timeout"
        elif oc.pending_close_reason:
            exit_reason = oc.pending_close_reason
        elif _was_ratchet:
            exit_reason = f"ratchet_sl_alcanzado(floor={oc.trail_sl_locked:.4f})"
        else:
            exit_reason = self._classify_exit(poc)

        record = {
            "broker": "deriv",
            "contract_id": cid,
            "intent_id": oc.intent_id,
            "symbol": oc.symbol,
            "side": oc.side,
            "stake_usdt": oc.stake_usdt,
            "entry_price": oc.entry_price,
            "exit_price": exit_price,
            "realized_pnl_usdt": realized,
            "exit_reason": exit_reason,
            "closed_by": "ws_event",
            "opened_at_ts": oc.opened_at_ts,
            "closed_at_ts": time.time(),
            "score_breakdown": oc.score_breakdown,
            "max_hold_seconds": oc.max_hold_seconds,
            "_settled_by": "ws_subscription",
            **_dpm_stats,
        }
        _LOGGER.info(
            "[deriv-trader] WS_INSTANT_CLOSE %s symbol=%s pnl=%.4f reason=%s "
            "eficiencia=%.2f max_pnl=%.4f dpm_fase=%s",
            cid, oc.symbol, realized, exit_reason,
            _dpm_stats.get("eficiencia", 0), _dpm_stats.get("max_pnl_alcanzado", 0),
            _dpm_stats.get("dpm_fase", "?"),
        )
        self._append_closed(record, oc)
        await self._post_pamm_webhook(record)
        self._notify("close", record)
        self._update_sym_stats(record)

    # ─────────────────────────────────────────────────────────────────────────
    # WS reconnect hook — re-subscribe open contracts after disconnect
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_ws_reconnect(self) -> None:
        """Re-subscribe all open contracts after a WebSocket reconnect.

        Called automatically by DerivClient.run_forever() after every
        (re)connect + tick-subscription cycle, within 3 seconds of the new
        connection being live.  Ensures the per-contract POC subscription stream
        is restored for every contract that was open when the disconnect happened.
        """
        async with self._lock:
            open_ids = list(self._open.keys())
        if not open_ids:
            return
        _LOGGER.info(
            "[deriv-trader] WS_RECONNECT: re-subscribing %d open contract(s): %s",
            len(open_ids), open_ids,
        )
        for cid in open_ids:
            asyncio.create_task(
                self._client.subscribe_contract(cid, self._on_ws_contract_update),
                name=f"resub-{cid}",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Heartbeat monitor (PROBLEMA 1 — unknown closes)
    # ─────────────────────────────────────────────────────────────────────────    # Runs every 10 seconds as a dedicated safety net:
    #  1. Re-subscribes any open contract whose WS subscription was lost
    #     (e.g. after a WS reconnect that happened while the contract was open).
    #  2. REST-polls every open contract to catch forced closes that the WS
    #     event never delivered — records them as closed_by="heartbeat_poll"
    #     with the real PnL from the broker, eliminating "unknown" close labels.
    # ─────────────────────────────────────────────────────────────────────────
    _HEARTBEAT_INTERVAL_SEC: float = float(os.getenv("DERIV_HEARTBEAT_SEC", "10"))

    async def heartbeat_loop(self) -> None:
        """10-second heartbeat: re-sub lost WS connections + detect forced closes."""
        _TERMINAL_STATUSES = {"won", "lost", "sold", "cancelled", "expired"}
        while True:
            await asyncio.sleep(self._HEARTBEAT_INTERVAL_SEC)
            if self._settings.dry_run or not self._open:
                continue
            try:
                async with self._lock:
                    open_ids = list(self._open.keys())

                for cid in open_ids:
                    # ── 1. Re-subscribe if WS subscription was silently lost ──
                    if cid not in self._client._contract_subs:
                        _LOGGER.debug(
                            "[heartbeat] re-subscribing lost WS sub for contract %s", cid
                        )
                        asyncio.create_task(
                            self._client.subscribe_contract(
                                cid, self._on_ws_contract_update
                            ),
                            name=f"hb-resub-{cid}",
                        )

                    # ── 2. Poll for forced closes (REST fallback) ─────────────
                    try:
                        resp = await asyncio.wait_for(
                            self._client.proposal_open_contract(cid), timeout=8.0
                        )
                    except (DerivClientError, asyncio.TimeoutError) as exc:
                        _LOGGER.debug(
                            "[heartbeat] poll failed for %s: %s", cid, exc
                        )
                        continue

                    poc = resp.get("proposal_open_contract") or {}
                    _poc_status = (poc.get("status") or "").lower()
                    is_sold = (
                        bool(poc.get("is_sold"))
                        and _poc_status in _TERMINAL_STATUSES
                    )
                    if not is_sold:
                        continue

                    # Contract is closed but bot didn't know — forced close
                    async with self._lock:
                        oc = self._open.pop(cid, None)
                        if oc is None:
                            continue  # already settled by WS callback or reaper
                        self._spike_buffer.pop(cid, None)  # Phase 37: clear buffer
                        self._persist_open()

                    await self._notify_mirror_settlement(cid, "heartbeat_settled")

                    realized = float(poc.get("profit") or 0)
                    exit_price = float(
                        poc.get("sell_spot")
                        or poc.get("exit_tick_display_value")
                        or poc.get("exit_spot")
                        or poc.get("current_spot")
                        or poc.get("sell_price")
                        or 0
                    )
                    # ── Heartbeat exit classification (same priority as WS/reaper) ──
                    # CRITICAL FIX: heartbeat must honour pending_close_reason so that
                    # spike_tp/spike_capture closes that lost their WS event are NOT
                    # mislabeled as "manual_close" (broker status=sold is ambiguous).
                    _held_hb = time.time() - oc.opened_at_ts
                    _was_ratchet_hb = (
                        oc.trail_sl_locked > _TRAIL_INIT_SL
                        and realized <= oc.trail_sl_locked + 0.01
                    )
                    if oc.max_hold_seconds > 0 and _held_hb >= oc.max_hold_seconds:
                        exit_reason = "spike_timeout"
                    elif oc.pending_close_reason:
                        exit_reason = oc.pending_close_reason
                    elif _was_ratchet_hb:
                        exit_reason = f"ratchet_sl_alcanzado(floor={oc.trail_sl_locked:.4f})"
                    else:
                        exit_reason = self._classify_exit(poc)
                        if exit_reason in ("", "unknown"):
                            exit_reason = "forced_close"

                    _dpm_stats = self._dpm.get_close_stats(cid, realized)
                    self._dpm.unregister(cid)

                    _LOGGER.warning(
                        "[heartbeat] FORCED_CLOSE detected contract_id=%s symbol=%s "
                        "pnl=%.4f status=%s exit_reason=%s — was not caught by WS event or reaper",
                        cid, oc.symbol, realized, _poc_status, exit_reason,
                    )
                    record = {
                        "broker": "deriv",
                        "contract_id": cid,
                        "intent_id": oc.intent_id,
                        "symbol": oc.symbol,
                        "side": oc.side,
                        "stake_usdt": oc.stake_usdt,
                        "entry_price": oc.entry_price,
                        "exit_price": exit_price,
                        "realized_pnl_usdt": realized,
                        "exit_reason": exit_reason,
                        "closed_by": "heartbeat_poll",
                        "opened_at_ts": oc.opened_at_ts,
                        "closed_at_ts": time.time(),
                        "score_breakdown": oc.score_breakdown,
                        "max_hold_seconds": oc.max_hold_seconds,
                        **_dpm_stats,
                    }
                    self._append_closed(record, oc)
                    await self._post_pamm_webhook(record)
                    self._notify("forced_close", record)
                    self._update_sym_stats(record)

            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[heartbeat] loop error: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Independent timeout clock
    # Runs as a separate asyncio task every 5 s, purely comparing time.time()
    # against max_hold_seconds.  Does NOT depend on the tick stream or the
    # reap_closed() poll cycle — BOOM/CRASH contracts are force-sold even when
    # the event loop is saturated with tick processing.
    # ─────────────────────────────────────────────────────────────────────────
    async def timeout_clock_loop(self) -> None:
        """Independent 5-second timer that enforces spike_timeout on BOOM/CRASH."""
        while True:
            await asyncio.sleep(5)
            if self._settings.dry_run:
                continue
            try:
                async with self._lock:
                    candidates = [
                        (cid, oc) for cid, oc in self._open.items()
                        if oc.max_hold_seconds > 0
                    ]
                for cid, oc in candidates:
                    if self._disable_spike_timeout and is_spike_market(oc.symbol):
                        continue
                    held = time.time() - oc.opened_at_ts
                    if held >= oc.max_hold_seconds:
                        _LOGGER.info(
                            "[deriv-timeout-clock] force-selling %s (%s held=%.1fs limit=%.0fs)",
                            cid, oc.symbol, held, oc.max_hold_seconds,
                        )
                        try:
                            await self._client.sell(cid)
                        except DerivClientError as exc:
                            _LOGGER.debug(
                                "[deriv-timeout-clock] sell %s failed (may be already closed): %s",
                                cid, exc,
                            )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[deriv-timeout-clock] error: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Orphaned-contract grim reaper — institutional safety net (2026-05-18)
    # ─────────────────────────────────────────────────────────────────────────
    # Runs every 10 seconds. For every open contract whose elapsed time exceeds
    # (max_hold_seconds + 30s), force a close and purge from memory. This catches
    # cases where the broker WS dropped the close event or timeout_clock_loop
    # was blocked.  CRITICAL-level log so the orphan is auditable.
    # ─────────────────────────────────────────────────────────────────────────
    async def verify_orphaned_contracts(self) -> None:
        """Independent 10-second grim reaper for orphaned (zombie) contracts."""
        _GRACE_SEC = 30.0
        while True:
            await asyncio.sleep(10)
            if self._settings.dry_run:
                continue
            try:
                now_ts = time.time()
                async with self._lock:
                    candidates = [
                        (cid, oc) for cid, oc in self._open.items()
                        if oc.max_hold_seconds > 0
                        and not (
                            self._disable_spike_timeout and is_spike_market(oc.symbol)
                        )
                        and (now_ts - oc.opened_at_ts) > (oc.max_hold_seconds + _GRACE_SEC)
                    ]
                for cid, oc in candidates:
                    elapsed = now_ts - oc.opened_at_ts
                    _LOGGER.critical(
                        "[GRIM_REAPER] ORPHANED_CONTRACT contract_id=%s symbol=%s "
                        "elapsed=%.1fs > max_hold=%.0fs + grace=%.0fs — force-closing",
                        cid, oc.symbol, elapsed, oc.max_hold_seconds, _GRACE_SEC,
                    )
                    try:
                        await self.close_contract(cid)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "[GRIM_REAPER] sell failed for %s (may be already closed): %s",
                            cid, exc,
                        )
                    # Purge from memory regardless of sell outcome (reaper will
                    # later reconcile via portfolio if the broker still holds it).
                    async with self._lock:
                        self._open.pop(cid, None)
                        self._persist_open()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[GRIM_REAPER] loop error: %s", exc)

    def _notify(self, level: str, payload: dict[str, Any]) -> None:
        if self._telemetry is None:
            return
        with suppress(Exception):
            self._telemetry.send_alert_nowait(f"deriv_{level}", payload)

    # ─────────────────────────────────────────────────────────────────────────
    # Zombie reconciliation daemon
    # ─────────────────────────────────────────────────────────────────────────
    # How long to wait between reconciliation checks.  Default 10 min.
    # Lower values increase the risk of false ghost-purges (Deriv portfolio
    # takes up to 60s to reflect newly-opened contracts).
    _RECON_INTERVAL_SEC: float = float(os.getenv("DERIV_RECON_INTERVAL_SEC", "600"))
    # Minimum contract age before it can be considered a ghost.  Contracts
    # opened within this window are ALWAYS skipped regardless of portfolio state.
    _RECON_MIN_AGE_SEC: float = float(os.getenv("DERIV_RECON_MIN_AGE_SEC", "300"))

    async def reconciliation_loop(self, interval_sec: float | None = None) -> None:
        """Background task that hard-reconciles local state against Deriv's portfolio.

        Every *interval_sec* seconds (default: DERIV_RECON_INTERVAL_SEC = 600s)
        the bot queries Deriv's live ``portfolio`` endpoint.  Any contract that
        is registered as open locally but is NOT present in the broker's response
        is treated as a ghost and purged, subject to the minimum-age guard.

        CRITICAL SAFETY GUARD — minimum age:
          Deriv's portfolio API may take up to 60s to reflect a newly-opened
          contract.  Purging a contract that is simply «not settled yet» would
          create false ghost-close records and desync the UI.  We therefore
          NEVER purge a contract opened within the last DERIV_RECON_MIN_AGE_SEC
          seconds (default 300s = 5 min).  Only contracts that have been locally
          live for more than 5 minutes AND are absent from broker portfolio are
          true ghosts.
        """
        _interval = interval_sec if interval_sec is not None else self._RECON_INTERVAL_SEC
        _LOGGER.info(
            "[deriv-recon] reconciliation_loop started (interval=%.0fs min_age=%.0fs)",
            _interval, self._RECON_MIN_AGE_SEC,
        )
        # ── Boot-sync: immediate pass to flush ghosts from previous session ──
        # Runs BEFORE the first periodic sleep.  A short delay lets the Deriv
        # portfolio API settle after the WS connect.  We use min_age_sec=0 so
        # ALL locally-registered contracts from the previous session are eligible
        # for purge — none of them were opened in this process lifetime.
        if not self._settings.dry_run:
            await asyncio.sleep(5.0)
            try:
                _LOGGER.info("[deriv-recon] BOOT-SYNC: immediate reconciliation starting")
                await self._reconcile_once(boot=True)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[deriv-recon] BOOT-SYNC error (non-fatal): %s", exc)

        while True:
            await asyncio.sleep(_interval)
            if self._settings.dry_run:
                continue
            try:
                await self._reconcile_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("[deriv-recon] reconcile cycle error: %s", exc)

    async def _reconcile_once(self, boot: bool = False) -> None:
        """Single reconciliation pass — compare local open set vs broker portfolio.

        When *boot* is ``True`` (called at daemon startup) three things change:
          1. The minimum-age threshold is set to 0 s — all locally-registered
             contracts are from a previous process lifetime and cannot be fresh.
          2. The empty-portfolio guard is bypassed if every local contract is
             older than 60 s — an empty broker response at boot means they
             genuinely closed before the restart, not a transient WS failure.
          3. Inverse direction is checked: broker contracts NOT present in the
             local DB are recovered and re-subscribed (boot amnesia fix).
        """
        _min_age = 0.0 if boot else self._RECON_MIN_AGE_SEC

        # At boot, fetch the full portfolio payload so we can reconstruct
        # DerivOpenContract entries for broker-side orphans (Phase 2 below).
        # At steady-state, only contract IDs are needed for ghost detection.
        if boot:
            broker_contracts: list[dict[str, Any]] = await self._client.portfolio_full()
            broker_ids: list[int] = [
                int(c["contract_id"]) for c in broker_contracts if "contract_id" in c
            ]
        else:
            broker_contracts = []
            broker_ids = await self._client.portfolio()

        async with self._lock:
            # Snapshot open contracts with their ages
            local_snapshot: dict[int, float] = {
                cid: oc.opened_at_ts for cid, oc in self._open.items()
            }

        # ── Phase 1: Ghost purge (local → broker direction) ───────────────
        if not local_snapshot:
            # No locally-known contracts to purge.  At boot we still need to
            # run Phase 2 to recover any broker-side orphans (e.g. a fresh
            # container that lost its disk state entirely).
            if boot:
                await self._boot_recover_broker_orphans(broker_contracts)
                await self._boot_sync_mirror_followers()
            return

        # Broker must return a non-empty list before we trust any diff.
        # An empty portfolio while we have open contracts is almost always
        # a transient WS failure — never purge on empty broker response.
        # EXCEPTION (boot=True): if every local contract is > 60 s old the
        # process restarted after they were already closed; treat as ghosts.
        if len(broker_ids) == 0:
            _boot_all_old = boot and all(
                (time.time() - ts) > 60.0 for ts in local_snapshot.values()
            )
            if not _boot_all_old:
                _LOGGER.debug(
                    "[deriv-recon] broker portfolio empty while %d local contracts open — "
                    "assuming transient WS failure, skipping purge",
                    len(local_snapshot),
                )
                # Still run boot recovery even though ghost-purge is skipped.
                if boot:
                    await self._boot_recover_broker_orphans(broker_contracts)
                    await self._boot_sync_mirror_followers()
                return
            _LOGGER.info(
                "[deriv-recon] BOOT-SYNC: broker returned empty portfolio and all %d "
                "local contracts are >60 s old — treating as closed ghosts",
                len(local_snapshot),
            )

        broker_set = set(broker_ids)
        now = time.time()

        # Identify ghost candidates: locally open but absent from broker.
        # Apply minimum-age guard: skip contracts opened within the last
        # _min_age seconds to avoid false positives on fresh contracts
        # that haven't propagated to Deriv's portfolio yet.
        ghost_ids: list[int] = []
        young_skipped: int = 0
        for cid, opened_at in local_snapshot.items():
            if cid in broker_set:
                continue  # broker confirms this contract — not a ghost
            age_sec = now - opened_at
            if age_sec < _min_age:
                young_skipped += 1
                _LOGGER.debug(
                    "[deriv-recon] contract_id=%s age=%.0fs < min_age=%.0fs — skipping ghost check",
                    cid, age_sec, _min_age,
                )
                continue
            ghost_ids.append(cid)

        if young_skipped:
            _LOGGER.info(
                "[deriv-recon] %d contract(s) skipped (too young, min_age=%.0fs)",
                young_skipped, _min_age,
            )

        for cid in ghost_ids:
            async with self._lock:
                oc = self._open.pop(cid, None)
                self._persist_open()

            if oc is None:
                continue

            await self._notify_mirror_settlement(cid, "reconcile_ghost")

            age_min = (now - oc.opened_at_ts) / 60
            _LOGGER.warning(
                "[deriv-recon] GHOST purged: contract_id=%s symbol=%s side=%s "
                "stake=%.2f age=%.1fmin — not found in broker portfolio",
                cid, oc.symbol, oc.side, oc.stake_usdt, age_min,
            )

            # Smart classification: query broker history to determine real exit reason.
            _ghost_reason, _ghost_pnl = await self._classify_ghost_exit(cid, oc)
            _LOGGER.info(
                "[deriv-recon] ghost %s exit_reason=%s pnl=%.4f",
                cid, _ghost_reason, _ghost_pnl,
            )
            record = {
                "broker": "deriv",
                "contract_id": cid,
                "intent_id": oc.intent_id,
                "symbol": oc.symbol,
                "side": oc.side,
                "stake_usdt": oc.stake_usdt,
                "entry_price": oc.entry_price,
                "exit_price": 0.0,
                "realized_pnl_usdt": _ghost_pnl,
                "exit_reason": _ghost_reason,
                "opened_at_ts": oc.opened_at_ts,
                "closed_at_ts": now,
                "score_breakdown": oc.score_breakdown,
                "max_hold_seconds": oc.max_hold_seconds,
            }
            self._append_closed(record, oc)
            await self._post_pamm_webhook(record)
            self._notify("ghost_closed", record)

        # ── Phase 2: Broker → local recovery (boot only) ──────────────────
        # Must run AFTER ghost purge so we never re-insert a freshly-purged
        # ghost.  broker_contracts is only populated when boot=True.
        if boot:
            await self._boot_recover_broker_orphans(broker_contracts)
            # Phase 15 fix: re-apply broker BE lock for any contract whose
            # broker_be_locked=True was persisted before the restart.
            # Doing this AFTER orphan recovery ensures ALL open contracts
            # (both disk-restored and broker-recovered) are considered.
            await self._reapply_broker_be_locks()
            await self._boot_sync_mirror_followers()

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 15: Broker BE lock re-application on restart
    # After a deploy/restart, any contract that had broker_be_locked=True had
    # its SL set to $0.01 in the previous process.  The broker retains the SL
    # but we re-issue the call to be safe (idempotent: setting SL=$0.01 twice
    # has no side-effects).  Also proactively locks any contract whose
    # peak_profit already exceeded the BE threshold but whose lock was NOT
    # persisted (e.g. lost due to a sudden kill before the next write).
    # ─────────────────────────────────────────────────────────────────────────
    async def _reapply_broker_be_locks(self) -> None:
        async with self._lock:
            snapshot = list(self._open.items())

        if not snapshot:
            return

        for cid, oc in snapshot:
            # Case 1: lock was persisted — re-apply unconditionally
            if oc.broker_be_locked and not self._settings.dry_run:
                try:
                    await self._client.contract_update(cid, stop_loss=0.01)
                    _LOGGER.info(
                        "[RECOVERY] Re-applied broker BE lock contract_id=%s symbol=%s "
                        "peak=%.4f — SL=$0.01 re-issued after restart",
                        cid, oc.symbol, oc.peak_profit,
                    )
                except Exception as _be_exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "[RECOVERY] Failed to re-apply BE lock for %s: %s",
                        cid, _be_exc,
                    )
            # Case 2: lock was NOT persisted but peak_profit suggests it should
            # have been.  Use the most conservative DPM ratchet_step (25% of
            # stake) as the BE trigger so we never miss a locked contract.
            elif not oc.broker_be_locked and oc.peak_profit >= oc.stake_usdt * 0.25:
                if not self._settings.dry_run:
                    try:
                        await self._client.contract_update(cid, stop_loss=0.01)
                        oc.broker_be_locked = True
                        _LOGGER.info(
                            "[RECOVERY] Applied missed broker BE lock contract_id=%s symbol=%s "
                            "peak=%.4f stake=%.2f (peak/stake=%.1f%%) — proactive lock on restart",
                            cid, oc.symbol, oc.peak_profit, oc.stake_usdt,
                            100 * oc.peak_profit / oc.stake_usdt,
                        )
                    except Exception as _be_exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "[RECOVERY] Failed proactive BE lock for %s: %s",
                            cid, _be_exc,
                        )

    # ─────────────────────────────────────────────────────────────────────────
    # Boot amnesia recovery — broker → local direction
    # Iterates the broker's live portfolio and inserts any contract that is NOT
    # already tracked in self._open.  Fires a WS subscription for each so the
    # trailing-stop reaper and is_sold callbacks resume immediately.
    # ─────────────────────────────────────────────────────────────────────────
    async def _boot_recover_broker_orphans(
        self, broker_contracts: list[dict[str, Any]]
    ) -> None:
        """Recover live broker contracts missing from the local open-contract DB.

        Called only during the boot-sync pass.  For each contract present on
        Deriv's portfolio that is NOT already in ``self._open``:
          1. Calls ``proposal_open_contract`` to get the current spot price
             (best-effort — falls back to ``buy_price`` if unavailable).
          2. Builds a ``DerivOpenContract`` with ``intent_id='recovered_on_boot'``
             and the trailing SL reset to the initial sentinel (–1.0) so the
             trailing tiers start fresh from the current position.
          3. Inserts the record into ``self._open`` and persists to disk.
          4. Subscribes to the broker WS stream so trailing-stop updates and
             ``is_sold`` events resume without waiting for the next reap cycle.
        """
        if not broker_contracts:
            return

        async with self._lock:
            local_ids = set(self._open.keys())

        orphan_contracts = [
            c for c in broker_contracts
            if "contract_id" in c and int(c["contract_id"]) not in local_ids
        ]

        if not orphan_contracts:
            _LOGGER.info("[deriv-recon] BOOT-SYNC: no broker orphans — local DB is in sync")
            return

        _LOGGER.info(
            "[deriv-recon] BOOT-SYNC: found %d broker orphan(s) to recover: %s",
            len(orphan_contracts),
            [int(c["contract_id"]) for c in orphan_contracts],
        )

        recovered = 0
        for c in orphan_contracts:
            cid = int(c["contract_id"])
            symbol: str = (
                c.get("symbol")
                or c.get("underlying_symbol")
                or c.get("underlying")
                or _symbol_from_shortcode(c.get("shortcode", ""))
                or "UNKNOWN"
            )
            # contract_type is MULTUP / MULTDOWN (or RISE/FALL for BOOM/CRASH)
            side: str = str(c.get("contract_type") or "MULTUP").upper()
            # buy_price for multiplier contracts IS the stake paid (in USD)
            stake: float = float(c.get("buy_price") or c.get("stake") or 0.0)
            multiplier: int = int(c.get("multiplier") or 200)
            # date_start is a Unix timestamp (seconds)
            opened_at: float = float(c.get("date_start") or time.time())

            # Best-effort: get the entry spot from the live POC endpoint.
            entry_price = 0.0
            try:
                _poc = (
                    await asyncio.wait_for(
                        self._client.proposal_open_contract(cid), timeout=8.0
                    )
                ).get("proposal_open_contract") or {}
                _spot = float(
                    _poc.get("entry_tick_display_value")
                    or _poc.get("entry_spot")
                    or _poc.get("current_spot")
                    or 0
                )
                if _spot > 0:
                    entry_price = _spot
            except Exception as _ep_exc:  # noqa: BLE001
                _LOGGER.debug(
                    "[deriv-recon] boot-recover POC failed for %s: %s — entry_price=0",
                    cid, _ep_exc,
                )

            # Restore per-symbol max_hold from profile so spike timeouts resume
            # correctly after a restart (boot-recovered contracts inherit the same
            # 450s limit as freshly-opened ones).
            _oc_profile = get_asset_profile(symbol)
            _max_hold_recovered = float(_oc_profile.get("max_hold_seconds", 0.0))
            if is_spike_market(symbol) and _max_hold_recovered > 0:
                if self._disable_spike_timeout:
                    _max_hold_recovered = 0.0
                else:
                    _max_hold_recovered += self._spike_hold_bonus_sec(symbol)

            oc = DerivOpenContract(
                contract_id=cid,
                intent_id="recovered_on_boot",
                symbol=symbol,
                side=side,
                stake_usdt=stake,
                multiplier=multiplier,
                entry_price=entry_price,
                opened_at_ts=opened_at,
                score_breakdown={"recovered": True},
                max_hold_seconds=_max_hold_recovered,
                peak_profit=0.0,
                trail_sl_locked=_TRAIL_INIT_SL,
            )

            async with self._lock:
                # Double-check: another coroutine may have inserted it between
                # our snapshot and now (race guard).
                if cid not in self._open:
                    self._open[cid] = oc
                    self._persist_open()
                    recovered += 1
                    _LOGGER.info(
                        "[deriv-recon] RECOVERED contract_id=%s symbol=%s side=%s "
                        "stake=%.2f entry=%.5f opened_at=%s",
                        cid, symbol, side, stake, entry_price,
                        time.strftime("%H:%M:%S", time.localtime(opened_at)),
                    )

            # Register with DPM so ratchet SL picks up on this recovered contract.
            if not self._dpm.is_registered(cid):
                self._dpm.register(
                    contract_id=cid,
                    symbol=symbol,
                    stake=stake,
                    entry_price=entry_price,
                    entry_ts=opened_at,
                    max_duration_override_sec=(_max_hold_recovered + self._dpm_timeout_buffer_sec()),
                )

            # Re-subscribe to broker WS stream so trailing-stop and is_sold
            # callbacks work immediately — no waiting for next reap_closed cycle.
            asyncio.create_task(
                self._client.subscribe_contract(cid, self._on_ws_contract_update),
                name=f"boot-sub-{cid}",
            )

        if recovered:
            _LOGGER.info(
                "[deriv-recon] BOOT-SYNC complete: Recovered %d live contract(s) from broker",
                recovered,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Smart ghost-exit classifier
    # Queries Deriv's profit_table to determine the real exit reason for a
    # contract that the reconciliation loop found as a ghost (no longer in the
    # broker portfolio).  Returns (exit_reason, realized_pnl) so the closed
    # record gets accurate data instead of blanket 'lost_or_ghost_closed'.
    # ─────────────────────────────────────────────────────────────────────────
    async def _classify_ghost_exit(
        self, cid: int, oc: "DerivOpenContract"
    ) -> tuple[str, float]:
        """Query broker profit_table to classify a ghost contract's exit.

        Returns:
          ("broker_sl", pnl)             — negative PnL, stop-loss fired
          ("broker_tp", pnl)             — positive PnL, take-profit fired
          ("broker_trailing_stop", pnl)  — positive PnL, trail was active
          ("broker_closed_zero", 0.0)    — found in history but zero PnL
          ("ghost_unknown", 0.0)         — not found in broker history
        """
        try:
            txns = await asyncio.wait_for(
                self._client.profit_table(limit=50), timeout=10.0
            )
            for tx in txns:
                if int(tx.get("contract_id", 0)) == cid:
                    profit = float(tx.get("profit") or 0.0)
                    _LOGGER.info(
                        "[deriv-recon] ghost %s classified via profit_table: "
                        "profit=%.4f trail_locked=%.3f",
                        cid, profit, oc.trail_sl_locked,
                    )
                    if profit < 0:
                        return "broker_sl", profit
                    if profit > 0:
                        if oc.trail_sl_locked > _TRAIL_INIT_SL:
                            return "broker_trailing_stop", profit
                        return "broker_tp", profit
                    return "broker_closed_zero", 0.0
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "[deriv-recon] profit_table classify failed for %s: %s — fallback ghost_unknown",
                cid, exc,
            )
        return "ghost_unknown", 0.0


