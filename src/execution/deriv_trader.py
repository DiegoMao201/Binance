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
from src.strategies.deriv_signals import is_spike_market
from src.utils.deriv_config import DerivSettings
from src.utils.telegram_telemetry import TelegramTelemetry


_LOGGER = logging.getLogger(__name__)


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
_T1_PCT:          float = float(os.getenv("DERIV_TRAIL_T1_PCT",        "0.010"))  # 1.0 % → BE
_T2_PCT:          float = float(os.getenv("DERIV_TRAIL_T2_PCT",        "0.025"))  # 2.5 % → lock
_T3_PCT:          float = float(os.getenv("DERIV_TRAIL_T3_PCT",        "0.040"))  # 4.0 % → tight
_T2_LOCK_PCT:     float = float(os.getenv("DERIV_TRAIL_T2_LOCK_PCT",   "0.010"))  # lock = +1 %
_T3_STEP_PCT:     float = float(os.getenv("DERIV_TRAIL_T3_STEP_PCT",   "0.005"))  # trail gap 0.5 %


def _compute_trail_floor(peak: float, stake: float = 0.0) -> float:
    """Tiered trailing stop floor.

    When *stake* > 0 the three tiers are evaluated as a percentage of stake so
    the logic is invariant to position size.  Falls back to the legacy flat
    ratchet when stake is zero or unavailable.
    """
    if stake > 0:
        # Tier 3: strong run — follow closely (0.5 % behind peak)
        if peak >= stake * _T3_PCT:
            return round(peak - stake * _T3_STEP_PCT, 4)
        # Tier 2: decent gain — lock in 1 % of stake
        if peak >= stake * _T2_PCT:
            return round(stake * _T2_LOCK_PCT, 4)
        # Tier 1: first profit — protect break-even
        if peak >= stake * _T1_PCT:
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

            # ── Dynamic trailing SL (non-spike trades, runs while contract is open) ──
            # Tracks peak profit and force-sells when current profit falls below the
            # trailing floor.  Also updates broker SL at each new ratchet milestone
            # so the position is protected even if the bot restarts.
            if not is_sold and oc_check is not None and oc_check.max_hold_seconds == 0:
                _current_profit = float(poc.get("profit") or 0)
                # Ratchet up peak (never down)
                if _current_profit > oc_check.peak_profit:
                    oc_check.peak_profit = _current_profit
                _new_floor = _compute_trail_floor(oc_check.peak_profit, oc_check.stake_usdt)
                # Update broker SL when the floor ratchets up to a new level.
                # Deriv broker SL = maximum loss in absolute USD (always > 0).
                # When the floor is still negative we can express it as a SL
                # amount; when the floor crosses 0 (break-even / profit lock),
                # Deriv has no way to express "close if profit drops below X" via
                # stop_loss — that's handled by the software force-sell below.
                # Also enforces Deriv's minimum SL amount of $0.35.
                if _new_floor > oc_check.trail_sl_locked:
                    oc_check.trail_sl_locked = _new_floor
                    if _new_floor < 0:
                        # Still in loss territory — express as a stop-loss amount.
                        _broker_sl = round(max(0.35, abs(_new_floor)), 2)
                        try:
                            await self._client.contract_update(
                                cid, stop_loss=_broker_sl
                            )
                            _LOGGER.info(
                                "[deriv-trader] trail_ratchet %s: peak=%.3f new_floor=%.3f "
                                "broker_sl=%.2f",
                                oc_check.symbol, oc_check.peak_profit, _new_floor, _broker_sl,
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
                            "[deriv-trader] trail_ratchet %s: peak=%.3f new_floor=%.3f "
                            "(profit-lock — software guard only, no broker SL update)",
                            oc_check.symbol, oc_check.peak_profit, _new_floor,
                        )
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
        # Reconciliation-purged contracts carry realized_pnl_usdt=0.0 and
        # exit_reason="lost_or_ghost_closed". The PAMM allocation engine has
        # nothing to distribute ($0 → 0% of 0 = 0), so calling it just creates
        # noisy HTTP 500s when the frontend DB transaction conflicts on a
        # zero-value allocation. The local closed-contracts JSON record (and
        # frontend file-watcher) already remove the zombie from the UI, so the
        # webhook hop is purely redundant for ghosts.
        if (
            str(record.get("exit_reason")) == "lost_or_ghost_closed"
            and float(record.get("realized_pnl_usdt") or 0.0) == 0.0
        ):
            _LOGGER.info(
                "[deriv-trader] webhook skipped (ghost close, $0 allocation) "
                "contract_id=%s — local JSON purge already syncs UI",
                record.get("contract_id"),
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

        When *boot* is ``True`` (called at daemon startup) two safety guards are
        relaxed:
          1. The minimum-age threshold is set to 0 s — all locally-registered
             contracts are from a previous process lifetime and therefore cannot
             be "fresh" in the current session.
          2. The empty-portfolio guard is bypassed **if every local contract is
             older than 60 s** — at startup an empty broker response means the
             contracts genuinely closed before the restart, not a transient WS
             failure.
        """
        _min_age = 0.0 if boot else self._RECON_MIN_AGE_SEC
        broker_ids: list[int] = await self._client.portfolio()

        async with self._lock:
            # Snapshot open contracts with their ages
            local_snapshot: dict[int, float] = {
                cid: oc.opened_at_ts for cid, oc in self._open.items()
            }

        if not local_snapshot:
            return  # nothing to reconcile

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

        if not ghost_ids:
            return  # no confirmed ghosts after age filter

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

            record = {
                "broker": "deriv",
                "contract_id": cid,
                "intent_id": oc.intent_id,
                "symbol": oc.symbol,
                "side": oc.side,
                "stake_usdt": oc.stake_usdt,
                "entry_price": oc.entry_price,
                "exit_price": 0.0,
                "realized_pnl_usdt": 0.0,
                "exit_reason": "lost_or_ghost_closed",
                "opened_at_ts": oc.opened_at_ts,
                "closed_at_ts": now,
                "score_breakdown": oc.score_breakdown,
                "max_hold_seconds": oc.max_hold_seconds,
            }
            self._append_closed(record)
            await self._post_pamm_webhook(record)
            self._notify("ghost_closed", record)


