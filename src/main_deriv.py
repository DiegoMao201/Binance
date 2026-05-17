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

from src.analysis.deriv_analyst import DerivAnalyst
from src.data.deriv_client import DerivClient, NormalisedTick
from src.execution.deriv_trader import DerivTradeExecutor
from src.execution.order_router import OrderRouter, OrderRouterError
from src.safety.deriv_risk import DerivRiskManager, HurstCalibrator
from src.strategies.deriv_signals import is_spike_market, spike_timeout_sec
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
        self._analyst = DerivAnalyst(settings, self._client)
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
        # Balance cache — refreshed by _balance_refresh_loop every 30 s.
        self._balance_usd: float | None = None
        self._balance_currency: str = "USD"
        # Rolling equity snapshots (last 200) for the analytics page.
        self._equity_history: list[dict[str, Any]] = []
        # Per-symbol tick counter for periodic diagnostic logs
        self._diag_tick_count: dict[str, int] = {}

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

    # ─────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        _LOGGER.info(
            "[deriv-daemon] starting | symbols=%s dry_run=%s bankroll=%.2f",
            self._settings.symbols, self._settings.dry_run, self._settings.bankroll_usdt,
        )

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

        # Spawn the reaper as a background task; cancel on shutdown.
        reaper_task      = asyncio.create_task(self._reaper_loop(), name="deriv-reaper")
        status_task      = asyncio.create_task(self._status_writer_loop(), name="deriv-status")
        balance_task     = asyncio.create_task(self._balance_refresh_loop(), name="deriv-balance")
        history_task     = asyncio.create_task(self._analyst.history_refresh_loop(), name="deriv-history")
        calibrator_task  = asyncio.create_task(
            HurstCalibrator().calibration_loop(), name="deriv-hurst-calib"
        )
        ttl_task         = asyncio.create_task(self._snapshot_ttl_loop(), name="deriv-ttl")
        ws_task          = asyncio.create_task(
            self._client.run_forever(self._handle_tick), name="deriv-ws"
        )
        stop_task        = asyncio.create_task(self._stop_event.wait(), name="deriv-stop")

        all_tasks = {ws_task, reaper_task, stop_task, status_task, balance_task, history_task, calibrator_task, ttl_task}
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
        try:
            await self._pipeline(tick)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[deriv-daemon] pipeline error for %s (suppressed)", tick.symbol)

    async def _pipeline(self, tick: NormalisedTick) -> None:
        # ── Cool-down between attempts on the same symbol ──────────────────────
        if not self._cooldown.can_fire(tick.symbol):
            return

        # ── Score: informed by Hurst calibration ───────────────────────────────
        spread_pct = float(tick.metrics.get("spread") or 0.0)
        pre_analysis = self._analyst.get_history_summary().get(tick.symbol) or {}
        pre_hurst    = float(pre_analysis.get("hurst") or 0.5)
        pre_autocorr = float(pre_analysis.get("autocorr_lag1") or 0.0)

        # ── Score ────────────────────────────────────────────────────────────────
        snap = self._risk.evaluate(
            tick.symbol, spread_pct,
            hurst=pre_hurst,
            autocorr_lag1=pre_autocorr,
        )

        # Record decision with full breakdown for telemetry
        decision_extra = {
            "score_breakdown": snap.score_breakdown,
            "regime": snap.regime,
            "hurst_delta": snap.hurst_score_delta,
            "effective_min_score": snap.effective_min_score,
        }
        if not snap.allowed or snap.side is None:
            self._record_decision(
                symbol=tick.symbol, allowed=False, side=snap.side,
                score=getattr(snap, "score", 0.0),
                reason="; ".join(snap.reasons) if snap.reasons else "risk_rejected",
                extra=decision_extra,
            )
            return

        # ── AI gate ──────────────────────────────────────────────────────────────────
        # 5. Full AI gate — runs analysis (cached up to 15 min) + optional LLM call.
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

        # 6. AI veto check — but respect the math override flag from risk engine.
        #    If snap.hurst_ai_override is True, we skip the AI veto for this signal.
        if analysis is not None and not analysis.ai_approved and not analysis.ai_skipped:
            # Re-evaluate with the now-known AI confidence so the override is applied
            snap2 = self._risk.evaluate(
                tick.symbol, spread_pct,
                hurst=pre_hurst,
                autocorr_lag1=pre_autocorr,
                ai_confidence=analysis.ai_confidence,
            )
            if not snap2.hurst_ai_override:
                reason = f"AI_VETO: {analysis.ai_reason} (conf={analysis.ai_confidence:.2f})"
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
                return
            # Override applied — log and continue
            _LOGGER.info(
                "[deriv-daemon] Hurst math override: AI vetoed but H=%.3f autocorr=%.3f — proceeding",
                pre_hurst, pre_autocorr,
            )

        # ── Order payload ──────────────────────────────────────────────────────────────
        # 7. Build broker-agnostic payload and route.
        payload: dict[str, Any] = {
            "broker": "deriv",
            "symbol": tick.symbol,
            "side": snap.side,
            "stake_usdt": snap.suggested_stake_usdt,
            "multiplier": snap.suggested_multiplier,
            "stop_loss_pct": self._settings.stop_loss_pct,
            "take_profit_pct": self._settings.take_profit_pct,
            # score_breakdown flows to DerivOrder → PAMM webhook → deriv_contracts.score_breakdown
            "score_breakdown": snap.score_breakdown,
            # Spike timeout: force-close BOOM/CRASH contracts after N seconds if
            # the spike hasn't fired.  0 = disabled for non-spike markets.
            "max_hold_seconds": float(spike_timeout_sec(tick.symbol)),
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
                "hurst_ai_override": snap.hurst_ai_override,
            } if analysis else {},
        }
        self._cooldown.mark(tick.symbol)

        ai_note = "" if analysis is None or analysis.ai_skipped else f" ai={analysis.ai_confidence:.2f}"
        hurst_note = f" H={analysis.hurst:.3f}" if analysis and analysis.hurst != 0.5 else ""
        override_note = " [MATH_OVERRIDE]" if snap.hurst_ai_override else ""
        self._record_decision(
            symbol=tick.symbol, allowed=True, side=snap.side,
            score=snap.score,
            reason=f"GO{ai_note}{hurst_note}{override_note} score={snap.score:.2f} regime={snap.regime}",
            extra={
                **decision_extra,
                "hurst": analysis.hurst if analysis else None,
                "autocorr": analysis.autocorr_lag1 if analysis else None,
                "vol_regime": analysis.vol_regime if analysis else None,
                "ai_confidence": analysis.ai_confidence if analysis else None,
                "hurst_ai_override": snap.hurst_ai_override,
            },
        )
        self._counters["orders_sent"] += 1
        try:
            result = await self._router.route_order(payload)
            self._counters["orders_ok"] += 1
            _LOGGER.info(
                "[deriv-daemon] ORDER %s | score=%.2f%s%s%s | %s",
                tick.symbol, snap.score, ai_note, hurst_note, override_note, result,
            )
        except OrderRouterError as exc:
            self._counters["orders_failed"] += 1
            _LOGGER.warning("[deriv-daemon] router rejected: %s", exc)
        except Exception:  # noqa: BLE001
            self._counters["orders_failed"] += 1
            _LOGGER.exception("[deriv-daemon] order pipeline crashed (suppressed)")

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
                # ── Telemetría rica para auditoría visual ─────────────────
                "counters": dict(self._counters),
                "last_ticks": dict(self._last_ticks),
                "last_decisions": list(self._last_decisions[-15:]),
                "per_symbol_stats": per_sym,
                "open_contracts_live": open_contracts,
                "equity_history": list(self._equity_history[-50:]),  # last 50 snapshots
                # ── Analyst statistics (Hurst, vol regime, AI gate) ───────
                "analyst_summary": self._analyst.get_history_summary(),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=None, separators=(",", ":")))
            tmp.replace(path)
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
