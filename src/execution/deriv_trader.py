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
        open_on_symbol = sum(1 for oc in self._open.values() if oc.symbol == order.symbol)
        if open_on_symbol >= 1:
            raise DerivClientError(
                f"already have an open contract on {order.symbol} — skipping duplicate"
            )

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
        entry_price = float(result.get("buy_price") or 0)
        if contract_id <= 0:
            raise DerivClientError(f"buy returned no contract_id: {result}")

        oc = DerivOpenContract(
            contract_id=contract_id,
            intent_id=order.intent_id,
            symbol=order.symbol,
            side=order.side,
            stake_usdt=order.stake_usdt,
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
            "[deriv-trader] LIVE buy ok | contract_id=%s symbol=%s side=%s stake=%.2f",
            contract_id, order.symbol, order.side, order.stake_usdt,
        )
        self._notify("live_buy", {
            "contract_id": contract_id,
            "symbol": order.symbol,
            "side": order.side,
            "stake_usdt": order.stake_usdt,
            "intent_id": order.intent_id,
        })

        return {
            "broker": "deriv",
            "status": "live",
            "contract_id": contract_id,
            "intent_id": order.intent_id,
            "symbol": order.symbol,
            "side": order.side,
            "stake_usdt": order.stake_usdt,
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
            is_sold = bool(poc.get("is_sold") or poc.get("is_settleable"))
            if not is_sold:
                continue

            realized = float(poc.get("profit") or 0)
            exit_price = float(poc.get("sell_price") or poc.get("current_spot") or 0)
            # Label spike-timeout exits distinctly for analytics
            if oc_check is not None and oc_check.max_hold_seconds > 0:
                held = time.time() - oc_check.opened_at_ts
                if held >= oc_check.max_hold_seconds:
                    exit_reason = "spike_timeout"
                else:
                    exit_reason = self._classify_exit(poc)
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
