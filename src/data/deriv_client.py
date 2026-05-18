"""
src/data/deriv_client.py
─────────────────────────────────────────────────────────────────────────────
Asynchronous Deriv WebSocket data client.

Architecture
────────────
* Uses the official Deriv public WS endpoint:  wss://ws.derivws.com/websockets/v3?app_id=<id>
* Pure asyncio — never blocks the calling event loop.
* Adapter pattern: every Deriv message is normalised into a broker-agnostic
  dictionary that the rest of the platform (risk, router, PAMM webhook) can
  consume without knowing it came from Deriv.

Robustness
──────────
* Exponential backoff reconnection (1 s → 2 s → 4 s … capped at 60 s).
* Heartbeat ping every 30 s; if no reply within 15 s the socket is recycled.
* All public methods are idempotent and never raise into the daemon loop —
  failures are surfaced via structured logs and a `last_error` attribute.

Public surface
──────────────
* `DerivClient(settings)` — construct
* `await client.connect()` — handshake + authorize + subscribe
* `await client.run_forever(on_tick)` — main consumer loop, calls back per tick
* `await client.buy(symbol, contract_type, stake, multiplier, sl_pct, tp_pct)`
                                     — place a multiplier contract (returns dict)
* `await client.sell(contract_id)`   — close an open contract
* `await client.proposal_open_contract(contract_id)` — current PnL of a contract
* `await client.balance()`           — account balance snapshot
* `await client.close()`             — graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        ConnectionClosedOK,
        WebSocketException,
    )
    # InvalidStatusCode was removed in websockets 14.0; import conditionally.
    try:
        from websockets.exceptions import InvalidStatusCode
    except ImportError:
        InvalidStatusCode = WebSocketException  # type: ignore[misc,assignment]
except ImportError as exc:  # pragma: no cover — import-time guard
    raise ImportError(
        "The 'websockets' package is required for the Deriv pipeline. "
        "Install with `pip install websockets>=12.0`."
    ) from exc

from src.utils.deriv_config import DerivSettings


_LOGGER = logging.getLogger(__name__)


# ─── Adapter Pattern: normalised tick payload ────────────────────────────────
@dataclass(slots=True)
class NormalisedTick:
    """Broker-agnostic tick representation."""

    broker: str
    symbol: str
    timestamp_ms: int
    price: float
    high: float
    low: float
    volume: float
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "symbol": self.symbol,
            "timestamp": self.timestamp_ms,
            "price": self.price,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "metrics": dict(self.metrics),
        }


# ─── Errors ──────────────────────────────────────────────────────────────────
class DerivClientError(RuntimeError):
    """Wraps any Deriv-side failure for upstream classification."""


# ─── Client ──────────────────────────────────────────────────────────────────
class DerivClient:
    """Async Deriv WebSocket client (data + execution gateway)."""

    # OTP REST endpoint: POST here with PAT token → get ready-to-use WS URL.
    # This WS URL supports FULL Deriv API: ticks, proposal (MULTUP/MULTDOWN),
    # buy, sell, balance, etc. The `symbol` field is `underlying_symbol` here.
    _OTP_REST_URL = "https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp"
    # Legacy WS kept for reference only — rejected with 401 for alphanumeric app_ids
    _LEGACY_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id={app_id}"

    # Reconnection backoff bounds
    _BACKOFF_INITIAL = 1.0
    _BACKOFF_MAX = 60.0
    _BACKOFF_FACTOR = 2.0

    # Heartbeat parameters
    _PING_INTERVAL = 30.0
    _PING_TIMEOUT = 15.0

    def __init__(self, settings: DerivSettings) -> None:
        if not settings.api_token:
            raise DerivClientError(
                "DERIV_API_TOKEN is empty. Set it in the .env before starting the daemon."
            )
        if not settings.account_id:
            raise DerivClientError(
                "DERIV_ACCOUNT_ID is empty. Set it to your Deriv loginid (e.g. DOT92114701)."
            )
        self._settings = settings
        self._ws: Optional[Any] = None
        self._ws_ctx: Optional[Any] = None
        self._req_id: int = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()           # serialises send()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stopped = False
        self.last_error: str | None = None
        self.connected_at: float | None = None
        # contract_id → async callback fired on is_sold=1 (live WS subscription)
        self._contract_subs: dict[int, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        # Optional reconnect hook: called after _subscribe_ticks() on every
        # (re)connect so the executor can re-subscribe open contracts whose
        # WS subscriptions were lost during the disconnect.
        self._on_reconnect_cb: Optional[Callable[[], Awaitable[None]]] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ─────────────────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Open Deriv WS via OTP flow (PAT token → WS URL → connect).

        Flow:
          1. POST to /trading/v1/options/accounts/{accountId}/otp with PAT
          2. Extract data.url → pre-authenticated WS URL
          3. Connect to that URL (no separate `authorize` message needed)
          4. Start the background reader task; subsequent calls use req_id
             correlation for proposal / buy / sell / balance / etc.
        """
        if self._ws is not None:
            return

        ws_url = await self._get_otp_ws_url()
        _LOGGER.info("[deriv] Connecting via OTP WS (account=%s)",
                     self._settings.account_id)
        self._ws_ctx = websockets.connect(
            ws_url,
            ping_interval=self._PING_INTERVAL,
            ping_timeout=self._PING_TIMEOUT,
            close_timeout=5.0,
            max_size=2 ** 20,
        )
        self._ws = await self._ws_ctx.__aenter__()
        self.connected_at = time.time()
        self._reader_task = asyncio.create_task(self._reader_loop(), name="deriv-reader")
        _LOGGER.info("[deriv] WS connected (OTP) for account=%s", self._settings.account_id)

    async def close(self) -> None:
        """Graceful shutdown — cancels reader and closes the WS."""
        self._stopped = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with suppress(Exception):
                ctx = getattr(self, "_ws_ctx", None)
                if ctx is not None:
                    await ctx.__aexit__(None, None, None)
                else:
                    await self._ws.close()
            self._ws = None
            self._ws_ctx = None
        # Cancel all pending futures so callers do not hang forever.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DerivClientError("connection closed"))
        self._pending.clear()

    def set_reconnect_callback(
        self, cb: Callable[[], Awaitable[None]]
    ) -> None:
        """Register a coroutine to call after each (re)connect + tick subscription.

        Used by DerivTradeExecutor to re-subscribe open contract WS streams
        after a WebSocket disconnect/reconnect cycle.
        """
        self._on_reconnect_cb = cb

    # ─────────────────────────────────────────────────────────────────────────
    # Public market-data API
    # ─────────────────────────────────────────────────────────────────────────
    async def run_forever(
        self, on_tick: Callable[[NormalisedTick], Awaitable[None]]
    ) -> None:
        """
        Connect, subscribe to all configured symbols and dispatch every
        normalised tick to `on_tick`. Reconnects automatically with
        exponential backoff on any disconnection.
        """
        backoff = self._BACKOFF_INITIAL
        while not self._stopped:
            try:
                if self._ws is None:
                    await self.connect()

                # Subscribe (tick-stream) for every symbol we care about.
                await self._subscribe_ticks()

                # Fire the reconnect hook (if set) so the executor can
                # re-subscribe open contract WS subscriptions that were lost
                # during the previous disconnect.
                if self._on_reconnect_cb is not None:
                    try:
                        asyncio.create_task(
                            self._on_reconnect_cb(), name="deriv-reconnect-hook"
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning("[deriv] reconnect_cb scheduling failed")

                _LOGGER.info(
                    "[deriv] Tick stream up for %d symbols", len(self._settings.symbols)
                )

                # Sit on the queue and dispatch ticks until the reader closes.
                while not self._stopped and self._ws is not None:
                    tick = await self._tick_queue.get()
                    if tick is None:  # sentinel from reader on disconnect
                        break
                    try:
                        await on_tick(tick)
                    except Exception:  # noqa: BLE001 — never let user code kill us
                        _LOGGER.exception("[deriv] on_tick handler raised")

                # If we reach here, the socket was closed externally.
                raise ConnectionClosed(rcvd=None, sent=None)

            except (
                ConnectionClosed,
                ConnectionClosedError,
                ConnectionClosedOK,
                InvalidStatusCode,
                WebSocketException,
                OSError,
                asyncio.TimeoutError,
            ) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning(
                    "[deriv] Disconnected (%s) — reconnecting in %.1f s",
                    self.last_error, backoff,
                )
                await self._teardown_after_disconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * self._BACKOFF_FACTOR, self._BACKOFF_MAX)

            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.exception("[deriv] Unexpected loop error")
                await self._teardown_after_disconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * self._BACKOFF_FACTOR, self._BACKOFF_MAX)

            else:
                # Clean exit (run_forever returned without exception); reset backoff.
                backoff = self._BACKOFF_INITIAL

    # ─────────────────────────────────────────────────────────────────────────
    # Public execution API
    # ─────────────────────────────────────────────────────────────────────────
    async def balance(self) -> dict[str, Any]:
        return await self._request({"balance": 1})

    async def buy_spike(
        self,
        symbol: str,
        contract_type: str,
        stake_usdt: float,
        duration_ticks: int = 10,
    ) -> dict[str, Any]:
        """
        Buy a duration-based RISE/FALL contract for Boom/Crash spike indices.

        Boom/Crash do NOT support MULTUP/MULTDOWN.  The correct contract type is:
          RISE  — bet that the next spike fires UP   (use on BOOM* symbols)
          FALL  — bet that the next spike fires DOWN (use on CRASH* symbols)

        Uses the Deriv v3 direct-buy format ("buy":1, "price":100, "parameters":{…})
        so no prior proposal call is needed — the API handles the quote internally.

        Returns the raw `buy` response dict (contains contract_id, buy_price…).
        """
        ct = contract_type.upper()
        if ct not in {"RISE", "FALL"}:
            raise DerivClientError(f"buy_spike: unsupported contract_type '{ct}' — expected RISE or FALL")

        stake = max(round(float(stake_usdt), 2), 1.0)  # Deriv demo minimum is $1
        dt = max(1, int(duration_ticks))

        # Direct-buy format: buy="1" (string) + parameters block.
        # Schema: buy_request.schema.json requires buy to be a STRING matching
        # ^(?:[\w-]{32,128}|1)$ — the integer 1 is rejected.
        # parameters.underlying_symbol is REQUIRED ("symbol" does not exist).
        # "price" is required but ignored when basis="stake".
        buy_req: dict[str, Any] = {
            "buy": "1",
            "price": 100,
            "parameters": {
                "amount": stake,
                "basis": "stake",
                "contract_type": ct,
                "currency": "USD",
                "underlying_symbol": symbol,
                "duration": dt,
                "duration_unit": "t",
            },
        }
        result = await self._request(buy_req)
        if "error" in result:
            err = result["error"]
            _LOGGER.critical(
                "[RECHAZO BROKER] Símbolo: %s | Código: %s | Mensaje: %s",
                symbol, err.get("code", "?"), err.get("message", str(err)),
            )
            raise DerivClientError(f"buy_spike error ({symbol} {ct}): {err}")
        return result.get("buy") or result

    async def buy(
        self,
        symbol: str,
        contract_type: str,
        stake_usdt: float,
        multiplier: int,
        stop_loss_pct: float,
        take_profit_pct: float,
    ) -> dict[str, Any]:
        """
        Buy a MULTUP/MULTDOWN multiplier contract (synthetic indices, e.g. R_100).

        Uses the Deriv v3 direct-buy format ("buy":1, "price":100, "parameters":{…})
        instead of the legacy two-step proposal→buy flow.  This fixes silent
        rejections caused by the deprecated `underlying_symbol` field and the
        incorrect limit_order scaling of the old implementation.

        limit_order values are absolute USD P&L amounts:
          stop_loss   = stake * stop_loss_pct  (minimum $0.50 for demo accounts)
          take_profit = stake * take_profit_pct (minimum $1.00)

        The "price":100 field is required by the WS protocol but is ignored by
        Deriv when basis="stake" — the actual price is calculated server-side.

        Returns the raw `buy` response dict (contains contract_id, buy_price…).
        """
        ct = contract_type.upper()
        if ct not in {"MULTUP", "MULTDOWN"}:
            raise DerivClientError(f"unsupported contract_type: {ct}")

        stake = max(round(float(stake_usdt), 2), 1.0)  # Deriv demo minimum is $1
        mult = int(multiplier)

        # SL/TP as absolute USD P&L amounts (Deriv API requirement for limit_order).
        sl_usd = round(max(stake * float(stop_loss_pct), 0.50), 2)
        tp_usd = round(max(stake * float(take_profit_pct), 1.00), 2)

        # Direct-buy format: buy="1" (string) + parameters block.
        # Schema: buy_request.schema.json requires:
        #   - buy: STRING "1" (not integer 1)
        #   - parameters.underlying_symbol: REQUIRED (not "symbol")
        #   - parameters.contract_type, currency: REQUIRED
        # limit_order stop_loss/take_profit are absolute USD P&L amounts.
        buy_req: dict[str, Any] = {
            "buy": "1",
            "price": 100,
            "parameters": {
                "amount": stake,
                "basis": "stake",
                "contract_type": ct,
                "currency": "USD",
                "underlying_symbol": symbol,
                "multiplier": mult,
                "limit_order": {
                    "stop_loss": sl_usd,
                    "take_profit": tp_usd,
                },
            },
        }
        _intended_stake = stake  # keep original for logging
        result = await self._request(buy_req)
        # Auto-adjust stake if broker rejects — this happens when the computed
        # risk-sized stake exceeds the broker's allowed range for a given product
        # (e.g. R_* multiplier contracts cap out around $5-6 on demo).
        # code_args[0] is the broker's limit value; we use it directly (no +5%
        # padding — that was pushing us over the cap).
        if "error" in result:
            err = result["error"]
            if err.get("code") == "InvalidtoBuy" and err.get("code_args"):
                try:
                    broker_limit = max(round(float(err["code_args"][0]), 2), 1.00)
                    _LOGGER.info(
                        "[deriv-client] %s stake capped: %.2f → %.2f (broker limit, floor $1.00)",
                        symbol, _intended_stake, broker_limit,
                    )
                    buy_req["parameters"]["amount"] = broker_limit
                    # Recalculate SL/TP for the adjusted stake
                    buy_req["parameters"]["limit_order"]["stop_loss"] = round(
                        max(broker_limit * float(stop_loss_pct), 0.50), 2
                    )
                    buy_req["parameters"]["limit_order"]["take_profit"] = round(
                        max(broker_limit * float(take_profit_pct), 1.00), 2
                    )
                    result = await self._request(buy_req)
                    # Tag the result so the executor can store the actual stake
                    if "buy" in result:
                        result["buy"]["_actual_stake_usdt"] = broker_limit
                        result["buy"]["_intended_stake_usdt"] = _intended_stake
                except (ValueError, IndexError, KeyError):
                    pass  # fall through to original error handling

        # Second retry: LimitOrderAmountTooLow on the SL/TP after stake adjustment.
        # Broker reports the minimum acceptable amount in code_args[0]; we use it
        # as the new floor for both SL and TP (TP gets +50% headroom).
        if "error" in result:
            err = result["error"]
            if err.get("subcode") == "LimitOrderAmountTooLow" and err.get("code_args"):
                try:
                    min_limit = round(float(err["code_args"][0]) + 0.01, 2)
                    cur_stake = float(buy_req["parameters"]["amount"])
                    buy_req["parameters"]["limit_order"]["stop_loss"] = min_limit
                    buy_req["parameters"]["limit_order"]["take_profit"] = round(min_limit * 1.5, 2)
                    _LOGGER.info(
                        "[deriv-client] %s SL/TP raised to broker minimum: SL=%.2f TP=%.2f (stake=%.2f)",
                        symbol, min_limit, round(min_limit * 1.5, 2), cur_stake,
                    )
                    result = await self._request(buy_req)
                    if "buy" in result:
                        result["buy"]["_actual_stake_usdt"] = cur_stake
                        result["buy"]["_intended_stake_usdt"] = _intended_stake
                except (ValueError, IndexError, KeyError):
                    pass

        if "error" in result:
            err = result["error"]
            _LOGGER.critical(
                "[RECHAZO BROKER] Símbolo: %s | Código: %s | Mensaje: %s",
                symbol, err.get("code", "?"), err.get("message", str(err)),
            )
            raise DerivClientError(f"buy error ({symbol} {ct}): {err}")
        return result.get("buy") or result

    async def sell(self, contract_id: int) -> dict[str, Any]:
        """Close an open contract at market."""
        result = await self._request({"sell": contract_id, "price": 0})
        if "error" in result:
            raise DerivClientError(f"sell error: {result['error']}")
        return result

    async def contract_update(
        self,
        contract_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Update stop_loss and/or take_profit on an open multiplier contract.

        Values are ABSOLUTE USD P&L thresholds:
          stop_loss   = max loss in USD (positive number, e.g. 0.50 = "stop if I lose $0.50")
          take_profit = target profit in USD (positive number)

        To implement a broker-side trailing stop milestone:
          - Breakeven:  stop_loss=0.01  (minimum accepted by Deriv)
          - Lock +0.50: stop_loss must be omitted; instead reduce take_profit is not useful.
            Use software trailing in the execution layer for profit-lock levels.
        """
        limit_order: dict[str, Any] = {}
        if stop_loss is not None:
            limit_order["stop_loss"] = max(0.01, round(float(stop_loss), 2))
        if take_profit is not None:
            limit_order["take_profit"] = round(float(take_profit), 2)
        if not limit_order:
            return {}
        result = await self._request({
            "contract_update": 1,
            "contract_id": contract_id,
            "limit_order": limit_order,
        })
        if "error" in result:
            raise DerivClientError(f"contract_update error: {result['error']}")
        return result

    async def proposal_open_contract(self, contract_id: int) -> dict[str, Any]:
        """Get current bid_price / profit / status for an open contract."""
        return await self._request(
            {"proposal_open_contract": 1, "contract_id": contract_id}
        )

    async def subscribe_contract(
        self,
        contract_id: int,
        on_sold_cb: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Subscribe to live broker updates for *contract_id*.

        Deriv pushes a ``proposal_open_contract`` message every time the
        contract state changes (profit moves, SL/TP hit, etc.).  When the
        broker sends ``is_sold: 1`` the callback is scheduled immediately as
        an independent asyncio task — no waiting for the next reap_closed()
        cycle.

        The subscription is silently discarded on reconnect (the reaper and
        reconciliation loop serve as fallback for re-connected sessions).
        """
        self._contract_subs[contract_id] = on_sold_cb
        try:
            await self._send_no_wait({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1,
            })
            _LOGGER.debug("[deriv] subscribe_contract %s registered", contract_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[deriv] subscribe_contract send failed %s: %s — fallback to reaper",
                contract_id, exc,
            )
            self._contract_subs.pop(contract_id, None)

    async def portfolio(self) -> list[int]:
        """Return contract_ids for all currently open contracts on this account.

        Uses Deriv's ``portfolio`` request which returns every open multiplier
        contract.  The reconciliation loop uses this to detect ghost positions
        that are no longer alive on the broker side.

        Returns an empty list on any API error rather than raising, so a
        transient WS glitch never kills the reconciliation daemon.
        """
        contracts = await self.portfolio_full()
        return [int(c["contract_id"]) for c in contracts if "contract_id" in c]

    async def portfolio_full(self) -> list[dict[str, Any]]:
        """Return full contract objects for all currently open contracts.

        Each dict contains (at minimum):
          contract_id, contract_type, buy_price, date_start, multiplier, symbol

        Used by the boot-sync to reconstruct ``DerivOpenContract`` entries for
        positions that were live when the process was last stopped.

        Returns an empty list on any API error — never raises.
        """
        try:
            resp = await self._request({"portfolio": 1}, timeout=15.0)
        except Exception:  # noqa: BLE001
            return []
        if "error" in resp:
            _LOGGER.debug("portfolio_full error: %s", resp["error"])
            return []
        return list((resp.get("portfolio") or {}).get("contracts") or [])

    async def ticks_history(
        self,
        symbol: str,
        count: int = 1000,
    ) -> dict[str, Any]:
        """Fetch the last `count` ticks for `symbol`.

        Returns:
          {"prices": [float, ...], "times": [int, ...]}
          where times are Unix seconds.

        Raises DerivClientError on any API error.
        """
        resp = await self._request(
            {
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count": max(1, min(count, 5000)),
                "end": "latest",
                "start": 1,
                "style": "ticks",
            },
            timeout=30.0,
        )
        if "error" in resp:
            raise DerivClientError(f"ticks_history error ({symbol}): {resp['error']}")
        hist = resp.get("history") or {}
        prices = [float(p) for p in (hist.get("prices") or [])]
        times  = [int(t)   for t in (hist.get("times")  or [])]
        return {"prices": prices, "times": times, "symbol": symbol}

    async def active_symbols(self, product_type: str = "basic") -> list[dict[str, Any]]:
        """Return the full Deriv active-symbols catalogue.

        product_type: "basic" (lightweight) or "full" (all fields).
        Useful for discovering available synthetic indices and their pip sizes.
        """
        resp = await self._request(
            {"active_symbols": product_type},
            timeout=20.0,
        )
        if "error" in resp:
            raise DerivClientError(f"active_symbols error: {resp['error']}")
        return resp.get("active_symbols") or []

    async def profit_table(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return closed contracts with realised P&L from Deriv's own records.

        Useful as a secondary source of truth to reconcile with local JSON logs.
        """
        resp = await self._request(
            {"profit_table": 1, "description": 1, "limit": limit, "offset": offset},
            timeout=20.0,
        )
        if "error" in resp:
            raise DerivClientError(f"profit_table error: {resp['error']}")
        return (resp.get("profit_table") or {}).get("transactions") or []

    async def statement(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Full account statement (deposits, withdrawals, trades).

        Returns a list of transaction dicts ordered newest-first.
        """
        resp = await self._request(
            {"statement": 1, "description": 1, "limit": limit, "offset": offset},
            timeout=20.0,
        )
        if "error" in resp:
            raise DerivClientError(f"statement error: {resp['error']}")
        return (resp.get("statement") or {}).get("transactions") or []

    # ─────────────────────────────────────────────────────────────────────────
    # Internal: request/response correlation
    # ─────────────────────────────────────────────────────────────────────────
    _tick_queue: asyncio.Queue[NormalisedTick | None]

    async def _request(self, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        """Send a request and await its single matching response."""
        if self._ws is None:
            raise DerivClientError("websocket not connected")

        async with self._lock:
            self._req_id += 1
            req_id = self._req_id
            payload = {**payload, "req_id": req_id}
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            # Let websockets raise ConnectionClosed naturally if the socket is gone
            await self._ws.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _subscribe_ticks(self) -> None:
        """Open a streaming subscription for every configured symbol."""
        # Re-create the queue each time (in case of reconnect with leftovers).
        self._tick_queue = asyncio.Queue(maxsize=10_000)
        for sym in self._settings.symbols:
            # subscribe=1 keeps the stream open; new ticks arrive as separate
            # messages with msg_type='tick' and the same subscription id.
            await self._send_no_wait({"ticks": sym, "subscribe": 1})

    async def _send_no_wait(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise DerivClientError("websocket not connected")
        async with self._lock:
            self._req_id += 1
            payload = {**payload, "req_id": self._req_id}
            await self._ws.send(json.dumps(payload))

    async def _get_otp_ws_url(self) -> str:
        """Call the Deriv REST OTP endpoint, return the ready-to-use WS URL."""
        import aiohttp  # optional dep — only needed at runtime

        account_id = self._settings.account_id
        url = self._OTP_REST_URL.format(account_id=account_id)
        headers = {
            "Deriv-App-ID": str(self._settings.app_id or "1089"),
            "Authorization": f"Bearer {self._settings.api_token}",
            "Content-Type": "application/json",
        }
        _LOGGER.info("[deriv] Requesting OTP for account=%s", account_id)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise DerivClientError(
                        f"OTP endpoint returned {resp.status}: {text[:300]}"
                    )
                data = await resp.json()

        ws_url = data.get("data", {}).get("url")
        if not ws_url:
            raise DerivClientError(f"OTP response missing 'data.url': {data}")
        _LOGGER.info("[deriv] OTP obtained, WS URL ready")
        return ws_url

    async def _reader_loop(self) -> None:
        """Background task that demultiplexes WS messages."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    _LOGGER.warning("[deriv] malformed message dropped")
                    continue

                # 1) Replies to req_id correlated requests
                req_id = msg.get("req_id")
                if req_id is not None and req_id in self._pending:
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        fut.set_result(msg)

                # 2) Tick streams (msg_type == 'tick')
                if msg.get("msg_type") == "tick":
                    norm = self._normalise_tick(msg)
                    if norm is not None and hasattr(self, "_tick_queue"):
                        with suppress(asyncio.QueueFull):
                            self._tick_queue.put_nowait(norm)

                # 3) Live contract subscription updates (msg_type == 'proposal_open_contract')
                # Fires immediately when Deriv reports is_sold=1 — no polling delay.
                if msg.get("msg_type") == "proposal_open_contract":
                    poc = msg.get("proposal_open_contract") or {}
                    _cid = int(poc.get("contract_id") or 0)
                    if _cid > 0 and _cid in self._contract_subs:
                        _cb = self._contract_subs[_cid]
                        if poc.get("is_sold"):
                            # Remove before scheduling so duplicate fires are impossible.
                            self._contract_subs.pop(_cid, None)
                        asyncio.create_task(
                            _cb(poc), name=f"poc-sold-{_cid}"
                        )

        except (ConnectionClosed, ConnectionClosedError, ConnectionClosedOK, OSError) as exc:
            _LOGGER.info("[deriv] reader: socket closed (%s)", exc)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[deriv] reader crashed")
        finally:
            # Wake up the consumer with a sentinel so run_forever can reconnect.
            if hasattr(self, "_tick_queue"):
                with suppress(asyncio.QueueFull):
                    self._tick_queue.put_nowait(None)

    @staticmethod
    def _normalise_tick(msg: dict[str, Any]) -> NormalisedTick | None:
        """Translate a Deriv tick message into the platform-wide payload."""
        tick = msg.get("tick") or {}
        symbol = tick.get("symbol")
        quote = tick.get("quote")
        epoch = tick.get("epoch")
        if symbol is None or quote is None or epoch is None:
            return None
        try:
            price = float(quote)
            ts_ms = int(epoch) * 1000
        except (TypeError, ValueError):
            return None

        bid = tick.get("bid")
        ask = tick.get("ask")
        try:
            bid_f = float(bid) if bid is not None else price
            ask_f = float(ask) if ask is not None else price
            spread_pct = (ask_f - bid_f) / price if price else 0.0
        except (TypeError, ValueError):
            spread_pct = 0.0

        return NormalisedTick(
            broker="deriv",
            symbol=str(symbol),
            timestamp_ms=ts_ms,
            price=price,
            high=price,    # Deriv tick stream is single-quote, no OHLC bucket
            low=price,
            volume=0.0,
            metrics={
                "spread": round(spread_pct, 8),
                "is_spike": False,    # populated by deriv_risk during evaluation
                "raw_pip_size": tick.get("pip_size"),
            },
        )

    async def _teardown_after_disconnect(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with suppress(Exception):
                ctx = getattr(self, "_ws_ctx", None)
                if ctx is not None:
                    await ctx.__aexit__(None, None, None)
                else:
                    await self._ws.close()
            self._ws = None
            self._ws_ctx = None
        # Cancel pending RPCs so callers do not deadlock.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(DerivClientError("websocket reconnecting"))
        self._pending.clear()
        # Drop contract subscriptions — they are not portable across reconnects.
        # The reaper + reconciliation loop will catch any closes that happened
        # during the disconnect window.
        self._contract_subs.clear()
