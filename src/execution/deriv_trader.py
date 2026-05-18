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
from typing import Any

import aiohttp

from src.data.deriv_client import DerivClient, DerivClientError
from src.strategies.deriv_signals import get_asset_profile, is_spike_market
from src.utils.deriv_config import DerivSettings
from src.utils.telegram_telemetry import TelegramTelemetry


_LOGGER = logging.getLogger(__name__)


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
_T2_LOCK_PCT:     float = float(os.getenv("DERIV_TRAIL_T2_LOCK_PCT",   "0.15"))   # lock = +15%
_T3_STEP_PCT:     float = float(os.getenv("DERIV_TRAIL_T3_STEP_PCT",   "0.05"))   # trail gap 5%


def _compute_trail_floor(peak: float, stake: float = 0.0, spike_market: bool = False) -> float:
    """Tiered trailing stop floor.

    When *stake* > 0 the three tiers are evaluated as a percentage of stake so
    the logic is invariant to position size.  Falls back to the legacy flat
    ratchet when stake is zero or unavailable.

    spike_market=True skips Tier 1 (break-even at 1%) for BOOM/CRASH contracts:
    pre-spike inter-tick drift is violent enough to trigger BE exits prematurely.
    Trailing only activates at Tier 2 (2.5% gain = genuine profit territory).
    """
    if stake > 0:
        # Tier 3: strong run — follow closely (0.5 % behind peak)
        if peak >= stake * _T3_PCT:
            return round(peak - stake * _T3_STEP_PCT, 4)
        # Tier 2: decent gain — lock in 1 % of stake
        if peak >= stake * _T2_PCT:
            return round(stake * _T2_LOCK_PCT, 4)
        # Tier 1: first profit — protect break-even
        # BOOM/CRASH: skip — 1% gain is normal inter-spike noise; BE causes premature exit.
        if not spike_market and peak >= stake * _T1_PCT:
            return 0.0
        return _TRAIL_INIT_SL
    # Legacy flat ratchet (stake unknown)
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


class DerivTradeExecutor:
    """Sequencer that owns the lifecycle of every Deriv contract."""

    _POLL_INTERVAL_SECONDS = 2.0  # how often we re-check open contracts

    def __init__(
        self,
        settings: DerivSettings,
        client: DerivClient,
        telemetry: TelegramTelemetry | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._telemetry = telemetry
        self._open: dict[int, DerivOpenContract] = {}
        self._lock = asyncio.Lock()
        # Running per-symbol stats (updated on each contract close)
        self._sym_stats: dict[str, dict[str, Any]] = {}
        self._session_pnl: float = 0.0
        self._session_trades: int = 0

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

        # Also block a second contract on the same symbol — synthetic indices are
        # independent but doubling up on the same underlying adds no edge.
        # Age-based bypass: if the "open" contract on this symbol is stale (past
        # its expected max_hold or older than 300 s), the reaper likely hasn't
        # caught up yet after a restart. Allow the new trade through so the bot
        # doesn't stay permanently frozen on that symbol.
        open_on_symbol = sum(1 for oc in self._open.values() if oc.symbol == order.symbol)
        if open_on_symbol >= 1:
            _stale_threshold = 300.0
            _all_stale = all(
                (time.time() - oc.opened_at_ts) > (
                    oc.max_hold_seconds * 1.5 if oc.max_hold_seconds > 0 else _stale_threshold
                )
                for oc in self._open.values()
                if oc.symbol == order.symbol
            )
            if _all_stale:
                _LOGGER.warning(
                    "[deriv-trader] skipped_duplicate bypass: stale open contract on %s "
                    "(threshold=%.0fs) — allowing new trade through",
                    order.symbol, _stale_threshold,
                )
            else:
                _LOGGER.debug(
                    "[deriv-trader] %s already open — skipping duplicate order",
                    order.symbol,
                )
                return {"status": "skipped_duplicate", "symbol": order.symbol}

        # All synthetic indices (R_*, BOOM*, CRASH*) use MULTUP/MULTDOWN
        # multiplier contracts.  BOOM/CRASH directional restriction is enforced
        # upstream by direction_veto in the risk engine (BOOM→MULTUP only,
        # CRASH→MULTDOWN only).  The old RISE/FALL path was rejected by the broker.
        # ABSOLUTE HARD CAP — final guard before any order touches the broker.
        # DERIV_MAX_STAKE_USDT (default $3.00) is unbreakable: no regime/score
        # multiplier or Hurst boost can send a larger stake. Logged as a warning
        # so calibration samples are always within the configured ceiling.
        _final_hard_cap = float(os.getenv("DERIV_MAX_STAKE_USDT", "3.00"))
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
            if order.max_hold_seconds != _profile_mh:
                _LOGGER.info(
                    "[deriv-trader] max_hold_seconds enforced from profile: %.0fs → %.0fs (%s)",
                    order.max_hold_seconds, _profile_mh, order.symbol,
                )
            order.max_hold_seconds = _profile_mh
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
        )
        async with self._lock:
            self._open[contract_id] = oc
            self._persist_open()

        # Subscribe to live broker stream for this contract so we settle
        # immediately on is_sold=1 without waiting for the next reap_closed cycle.
        asyncio.create_task(
            self._client.subscribe_contract(contract_id, self._on_ws_contract_sold),
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
                held = time.time() - oc_check.opened_at_ts
                if held >= oc_check.max_hold_seconds:
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
                        # Contract may already be closed; continue to poll below

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

            # ── Dynamic trailing SL (all non-settled trades) ──────────────────────
            # Spike markets (BOOM/CRASH) now also run trailing alongside spike_timeout:
            # whichever fires first exits the contract.
            # Tier 1 (BE at 1%) is suppressed for spike markets via spike_market flag
            # to prevent premature exits on inter-spike drift; trailing activates at
            # Tier 2 (2.5% gain = confirmed profitable territory).
            if not is_sold and oc_check is not None:
                _is_spike_oc = is_spike_market(oc_check.symbol)
                _current_profit = float(poc.get("profit") or 0)
                # Ratchet up peak (never down)
                if _current_profit > oc_check.peak_profit:
                    oc_check.peak_profit = _current_profit
                _new_floor = _compute_trail_floor(
                    oc_check.peak_profit, oc_check.stake_usdt,
                    spike_market=_is_spike_oc,
                )
                # Update broker SL when the floor ratchets up to a new level.
                # Deriv broker SL = maximum loss in absolute USD (always > 0).
                # When the floor is still negative we can express it as a SL
                # amount; when the floor crosses 0 (break-even / profit lock),
                # Deriv has no way to express "close if profit drops below X" via
                # stop_loss — that's handled by the software force-sell below.
                # Also enforces Deriv's minimum SL amount of $0.35.
                if _new_floor > oc_check.trail_sl_locked:
                    oc_check.trail_sl_locked = _new_floor
                    # Determine which tier fired for logging clarity.
                    if _new_floor < 0:
                        _tier_label = "T1_tighten" if oc_check.stake_usdt > 0 else "legacy_ratchet"
                    elif _new_floor == 0.0:
                        _tier_label = "T1_breakeven"
                    elif _new_floor > 0:
                        _peak_ratio = oc_check.peak_profit / max(oc_check.stake_usdt, 0.01)
                        _tier_label = "T3_tight_trail" if _peak_ratio >= _T3_PCT else "T2_profit_lock"
                    else:
                        _tier_label = "unknown"
                    if _new_floor < 0:
                        # Still in loss territory — express as a stop-loss amount.
                        _broker_sl = round(max(0.35, abs(_new_floor)), 2)
                        try:
                            await self._client.contract_update(
                                cid, stop_loss=_broker_sl
                            )
                            _LOGGER.info(
                                "[deriv-trader] trail_ratchet %s [%s]: peak=%.3f new_floor=%.3f "
                                "broker_sl=%.2f",
                                oc_check.symbol, _tier_label,
                                oc_check.peak_profit, _new_floor, _broker_sl,
                            )
                        except DerivClientError as _trl_exc:
                            _LOGGER.warning(
                                "[deriv-trader] trail broker_sl update failed %s: %s",
                                cid, _trl_exc,
                            )
                    else:
                        # Floor >= 0 (break-even / profit lock): software force-sell
                        # below handles this; broker SL can't express profit floors.
                        _LOGGER.info(
                            "[deriv-trader] trail_ratchet %s [%s]: peak=%.3f new_floor=%.3f "
                            "(profit-lock — software guard only, no broker SL update)",
                            oc_check.symbol, _tier_label,
                            oc_check.peak_profit, _new_floor,
                        )
                    # Persist trail state to JSON so the dashboard reflects the live tier.
                    async with self._lock:
                        self._persist_open()
                # Software force-sell when profit drops below the locked floor
                if _current_profit < _new_floor:
                    _LOGGER.info(
                        "[deriv-trader] trail_stop %s: profit=%.3f < floor=%.3f "
                        "(peak=%.3f) — force-selling",
                        oc_check.symbol, _current_profit, _new_floor, oc_check.peak_profit,
                    )
                    try:
                        await self._client.sell(cid)
                    except DerivClientError as exc:
                        _LOGGER.warning(
                            "[deriv-trader] trail_stop sell failed %s: %s", cid, exc
                        )
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
            # Label spike-timeout exits distinctly for analytics
            _was_trail_stop = (
                oc_check is not None
                and oc_check.max_hold_seconds == 0
                and realized < oc_check.trail_sl_locked + 0.01
            )
            if oc_check is not None and oc_check.max_hold_seconds > 0:
                held = time.time() - oc_check.opened_at_ts
                if held >= oc_check.max_hold_seconds:
                    exit_reason = "spike_timeout"
                else:
                    exit_reason = self._classify_exit(poc)
            elif _was_trail_stop:
                exit_reason = f"trail_stop(floor={oc_check.trail_sl_locked:.2f})"
            else:
                exit_reason = self._classify_exit(poc)

            async with self._lock:
                oc = self._open.pop(cid, None)
                self._persist_open()
            if oc is None:
                continue

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
                "opened_at_ts": oc.opened_at_ts,
                "closed_at_ts": time.time(),
                "score_breakdown": oc.score_breakdown,
                "max_hold_seconds": oc.max_hold_seconds,
            }
            self._append_closed(record)
            await self._post_pamm_webhook(record)
            self._notify("close", record)
            self._update_sym_stats(record)
            closed_now.append(record)

        return closed_now

    def _update_sym_stats(self, record: dict[str, Any]) -> None:
        sym = record.get("symbol", "unknown")
        pnl = float(record.get("realized_pnl_usdt") or 0)
        if sym not in self._sym_stats:
            self._sym_stats[sym] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "best": None, "worst": None}
        s = self._sym_stats[sym]
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
        self._session_pnl = round(self._session_pnl + pnl, 6)
        self._session_trades += 1

    def get_per_symbol_stats(self) -> dict[str, Any]:
        """Return a snapshot of per-symbol performance and session totals."""
        result: dict[str, Any] = {}
        for sym, s in self._sym_stats.items():
            wr = round(s["wins"] / s["trades"], 4) if s["trades"] > 0 else 0.0
            result[sym] = {**s, "win_rate": wr}
        result["_session"] = {
            "pnl": self._session_pnl,
            "trades": self._session_trades,
        }
        return result

    def get_open_contracts_for_status(self) -> list[dict[str, Any]]:
        """Return a safe serialisable snapshot of all open contracts."""
        return [
            {
                "contract_id": oc.contract_id,
                "symbol": oc.symbol,
                "side": oc.side,
                "stake_usdt": oc.stake_usdt,
                "multiplier": oc.multiplier,
                "entry_price": oc.entry_price,
                "opened_at_ts": oc.opened_at_ts,
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
            }
            for oc in self._open.values()
        ]
        tmp.write_text(json.dumps(body, indent=2))
        tmp.replace(path)

    def _append_closed(self, record: dict[str, Any]) -> None:
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

    # ─────────────────────────────────────────────────────────────────────────
    # PAMM webhook (broker='deriv')
    # ─────────────────────────────────────────────────────────────────────────
    async def _post_pamm_webhook(self, record: dict[str, Any]) -> None:
        url = self._settings.pamm_webhook_url
        secret = self._settings.webhook_secret
        if not url or not secret or not self._settings.user_id:
            _LOGGER.warning(
                "[deriv-trader] webhook skipped — missing PAMM_WEBHOOK_URL/WEBHOOK_SECRET/DERIV_USER_ID",
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

        payload = {
            "tradeId": f"deriv:{record['contract_id']}",
            "userId": self._settings.user_id,
            "rawPnl": float(record["realized_pnl_usdt"]),
            "binanceFee": 0.0,    # Deriv fees already netted into realized PnL
            "symbol": record["symbol"],
            "side": "BUY" if record["side"] == "MULTUP" else "SELL",
            "exitReason": record["exit_reason"],
            "broker": "deriv",
            # Entry context — backend stores in deriv_contracts.score_breakdown JSONB
            "score_breakdown": record.get("score_breakdown"),
        }
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {secret}"},
                ) as resp:
                    text = await resp.text()
                    if resp.status == 500:
                        _LOGGER.warning(
                            "[deriv-trader] webhook %s -> HTTP 500 (frontend DB issue) "
                            "contract_id=%s saved locally — trade audit intact",
                            url, record["contract_id"],
                        )
                    elif resp.status >= 300:
                        _LOGGER.warning(
                            "[deriv-trader] webhook %s -> HTTP %s: %s",
                            url, resp.status, text[:200],
                        )
                    else:
                        _LOGGER.info(
                            "[deriv-trader] webhook OK — contract_id=%s pnl=%.4f",
                            record["contract_id"], payload["rawPnl"],
                        )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "[deriv-trader] webhook timeout (>8s) contract_id=%s — continuing",
                record["contract_id"],
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("[deriv-trader] webhook POST failed: %s — trade saved locally", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _classify_exit(poc: dict[str, Any]) -> str:
        """Best-effort mapping of Deriv close codes to our internal vocabulary."""
        status = (poc.get("status") or "").lower()
        if status == "won":
            return "take_profit"
        if status == "lost":
            return "stop_loss"
        if status == "sold":
            return "manual_close"
        return status or "unknown"

    # ─────────────────────────────────────────────────────────────────────────
    # Live WS settlement callback
    # Called immediately when Deriv pushes is_sold=1 via the POC subscription.
    # Settles the contract without waiting for the next reap_closed() poll.
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_ws_contract_sold(self, poc: dict[str, Any]) -> None:
        """Immediate settlement triggered by the live broker WS stream."""
        # Guard: Deriv WS fires this callback on EVERY tick update, not just
        # on close. Only proceed when the broker explicitly marks is_sold=True.
        if not poc.get("is_sold"):
            return
        cid = int(poc.get("contract_id") or 0)
        if cid <= 0:
            return

        async with self._lock:
            oc = self._open.pop(cid, None)
            if oc is None:
                return  # already settled by reaper or reconciliation
            self._persist_open()

        realized = float(poc.get("profit") or 0)
        exit_price = float(
            poc.get("sell_spot")
            or poc.get("exit_tick_display_value")
            or poc.get("exit_spot")
            or poc.get("current_spot")
            or poc.get("sell_price")
            or 0
        )
        held = time.time() - oc.opened_at_ts
        _was_trail = (
            oc.trail_sl_locked > _TRAIL_INIT_SL
            and realized <= oc.trail_sl_locked + 0.01
        )
        if oc.max_hold_seconds > 0 and held >= oc.max_hold_seconds:
            exit_reason = "spike_timeout"
        elif _was_trail:
            exit_reason = f"trail_stop(floor={oc.trail_sl_locked:.2f})"
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
            "opened_at_ts": oc.opened_at_ts,
            "closed_at_ts": time.time(),
            "score_breakdown": oc.score_breakdown,
            "max_hold_seconds": oc.max_hold_seconds,
            "_settled_by": "ws_subscription",
        }
        _LOGGER.info(
            "[deriv-trader] WS_INSTANT_CLOSE %s symbol=%s pnl=%.4f reason=%s",
            cid, oc.symbol, realized, exit_reason,
        )
        self._append_closed(record)
        await self._post_pamm_webhook(record)
        self._notify("close", record)
        self._update_sym_stats(record)

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
            self._append_closed(record)
            await self._post_pamm_webhook(record)
            self._notify("ghost_closed", record)

        # ── Phase 2: Broker → local recovery (boot only) ──────────────────
        # Must run AFTER ghost purge so we never re-insert a freshly-purged
        # ghost.  broker_contracts is only populated when boot=True.
        if boot:
            await self._boot_recover_broker_orphans(broker_contracts)

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

            # Re-subscribe to broker WS stream so trailing-stop and is_sold
            # callbacks work immediately — no waiting for next reap_closed cycle.
            asyncio.create_task(
                self._client.subscribe_contract(cid, self._on_ws_contract_sold),
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


