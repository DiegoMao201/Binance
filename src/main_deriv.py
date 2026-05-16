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
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.data.deriv_client import DerivClient, NormalisedTick
from src.execution.deriv_trader import DerivTradeExecutor
from src.execution.order_router import OrderRouter, OrderRouterError
from src.safety.deriv_risk import DerivRiskManager
from src.utils.deriv_config import DerivSettings, load_deriv_settings
from src.utils.telegram_telemetry import TelegramTelemetry


_LOGGER = logging.getLogger("deriv.daemon")


# ─── Per-symbol cooldown to prevent burst entries ────────────────────────────
class _CooldownGate:
    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self._last: dict[str, float] = {}

    def can_fire(self, symbol: str) -> bool:
        now = time.time()
        return (now - self._last.get(symbol, 0)) >= self._seconds

    def mark(self, symbol: str) -> None:
        self._last[symbol] = time.time()


# ─── Daemon orchestrator ─────────────────────────────────────────────────────
class DerivDaemon:
    def __init__(self, settings: DerivSettings) -> None:
        self._settings = settings
        self._client = DerivClient(settings)
        self._telemetry = self._build_telemetry()
        self._executor = DerivTradeExecutor(settings, self._client, self._telemetry)
        self._risk = DerivRiskManager(settings)
        self._router = OrderRouter(binance_executor=None, deriv_executor=self._executor)
        self._cooldown = _CooldownGate(seconds=max(60, int(settings.contract_duration_sec)))
        self._stop_event = asyncio.Event()
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

    def _record_decision(self, *, symbol: str, allowed: bool, side: str | None,
                         score: float, reason: str) -> None:
        rec = {
            "symbol": symbol,
            "allowed": bool(allowed),
            "side": side,
            "score": round(float(score or 0.0), 3),
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._last_decisions.append(rec)
        if len(self._last_decisions) > 30:
            self._last_decisions = self._last_decisions[-30:]
        self._counters["decisions_total"] += 1

    # ─────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        _LOGGER.info(
            "[deriv-daemon] starting | symbols=%s dry_run=%s bankroll=%.2f",
            self._settings.symbols, self._settings.dry_run, self._settings.bankroll_usdt,
        )

        # Spawn the reaper as a background task; cancel on shutdown.
        reaper_task = asyncio.create_task(self._reaper_loop(), name="deriv-reaper")
        status_task = asyncio.create_task(self._status_writer_loop(), name="deriv-status")
        ws_task = asyncio.create_task(
            self._client.run_forever(self._handle_tick), name="deriv-ws"
        )
        stop_task = asyncio.create_task(self._stop_event.wait(), name="deriv-stop")

        try:
            done, _pending = await asyncio.wait(
                {ws_task, reaper_task, stop_task, status_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                if t.exception() is not None:
                    _LOGGER.exception("[deriv-daemon] task crashed: %s", t.get_name(),
                                      exc_info=t.exception())
        finally:
            for t in (ws_task, reaper_task, stop_task, status_task):
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
    # Tick handler — single hot path
    # ─────────────────────────────────────────────────────────────────────────
    async def _handle_tick(self, tick: NormalisedTick) -> None:
        # Telemetría: registrar último tick por símbolo (para que el frontend
        # demuestre que el stream WS está vivo aunque no haya entradas).
        self._counters["ticks_total"] += 1
        self._last_ticks[tick.symbol] = {
            "price": float(tick.price),
            "ts": datetime.now(timezone.utc).isoformat(),
            "spread": float(tick.metrics.get("spread") or 0.0),
        }

        # 1. Feed risk engine.
        self._risk.ingest_tick(tick.symbol, tick.price)

        # 2. Cool-down between attempts on the same symbol.
        if not self._cooldown.can_fire(tick.symbol):
            return

        # 3. Score.
        spread_pct = float(tick.metrics.get("spread") or 0.0)
        snap = self._risk.evaluate(tick.symbol, spread_pct)
        if not snap.allowed or snap.side is None:
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=getattr(snap, "score", 0.0),
                reason=getattr(snap, "reason", "risk_rejected") or "risk_rejected",
            )
            return

        # 4. Build broker-agnostic payload and route.
        payload: dict[str, Any] = {
            "broker": "deriv",
            "symbol": tick.symbol,
            "side": snap.side,                          # MULTUP / MULTDOWN
            "stake_usdt": snap.suggested_stake_usdt,
            "multiplier": snap.suggested_multiplier,
            "stop_loss_pct": self._settings.stop_loss_pct,
            "take_profit_pct": self._settings.take_profit_pct,
        }
        self._cooldown.mark(tick.symbol)
        self._record_decision(
            symbol=tick.symbol, allowed=True, side=snap.side,
            score=snap.score, reason="firing_order",
        )
        self._counters["orders_sent"] += 1
        try:
            result = await self._router.route_order(payload)
            self._counters["orders_ok"] += 1
            _LOGGER.info(
                "[deriv-daemon] ORDER %s | score=%.2f | %s",
                tick.symbol, snap.score, result,
            )
        except OrderRouterError as exc:
            self._counters["orders_failed"] += 1
            _LOGGER.warning("[deriv-daemon] router rejected: %s", exc)
        except Exception:  # noqa: BLE001
            self._counters["orders_failed"] += 1
            _LOGGER.exception("[deriv-daemon] order pipeline crashed (suppressed)")

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
            data = {
                "status": "running" if connected else "stopped",
                "connected": connected,
                "account_id": self._settings.account_id,
                "dry_run": self._settings.dry_run,
                "symbols": list(self._settings.symbols),
                "bankroll_usdt": self._settings.bankroll_usdt,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                # ── Telemetría rica para auditoría visual ─────────────────
                "counters": dict(self._counters),
                "last_ticks": dict(self._last_ticks),
                "last_decisions": list(self._last_decisions[-15:]),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[deriv-daemon] failed to write status file")

    # ─────────────────────────────────────────────────────────────────────────
    # Reaper — periodically settle closed contracts
    # ─────────────────────────────────────────────────────────────────────────
    async def _reaper_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                closed = await self._executor.reap_closed()
                for rec in closed:
                    self._risk.register_close(float(rec.get("realized_pnl_usdt") or 0))
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
