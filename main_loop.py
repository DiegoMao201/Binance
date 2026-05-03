from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from requests import RequestException

from src.analysis.ai_client import OpenRouterAnalyzer
from src.analysis.indicators import build_technical_signal, compute_indicators
from src.data.binance_client import BinanceDataClient
from src.execution.trader import TradeExecutor
from src.safety.risk_manager import RiskManager
from src.utils.config import load_settings
from src.utils.logger import setup_logger
from src.utils.state_store import append_history, build_state_snapshot, load_history, load_state, persist_state


def write_heartbeat(status: str, detail: str | None = None) -> None:
    settings = load_settings()
    control = load_state(settings.control_file)
    payload = {
        "status": status,
        "detail": detail or "",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "symbol": settings.trading_symbol,
        "timeframe": settings.timeframe,
        "dry_run": settings.dry_run,
        "desired_state": control.get("desired_state", "running"),
    }
    persist_state(settings.status_file, payload)


def ensure_control_file() -> dict[str, Any]:
    settings = load_settings()
    current = load_state(settings.control_file)
    if current:
        return current

    default_control = {
        "desired_state": "running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system",
        "reason": "Inicialización automática del control del bot.",
    }
    persist_state(settings.control_file, default_control)
    return default_control


def _serialize_market(frame):
    return frame.tail(50).assign(timestamp=lambda data: data["timestamp"].astype(str)).to_dict(orient="records")


def _build_signal_event(technical_signal: dict, ai_signal: dict, decision: dict) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "technical_signal": technical_signal.get("signal", "hold"),
        "technical_rsi": technical_signal.get("rsi", 0),
        "technical_price": technical_signal.get("close", 0),
        "ai_signal": ai_signal.get("signal", "hold"),
        "ai_confidence": ai_signal.get("confidence", 0),
        "decision_action": decision.get("action", decision.get("side", "hold")),
        "decision_status": decision.get("status", decision.get("reason", "n/a")),
    }


def _is_in_cooldown(order_history: list[dict[str, Any]], cooldown_minutes: int) -> bool:
    if not order_history:
        return False

    latest = order_history[-1]
    timestamp = latest.get("timestamp")
    if not timestamp:
        return False

    try:
        latest_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False

    age_seconds = (datetime.now(timezone.utc) - latest_at).total_seconds()
    return age_seconds < cooldown_minutes * 60


def _build_guardrails(settings, technical_signal: dict, ai_signal: dict, order_history: list[dict[str, Any]]) -> dict[str, Any]:
    signal = technical_signal["signal"]
    same_direction = signal == ai_signal.get("signal")
    ai_confidence = float(ai_signal.get("confidence", 0.0))
    ai_confident = ai_confidence >= settings.ai_confidence_threshold
    technical_confident = float(technical_signal.get("confidence", 0.0)) >= settings.technical_confidence_threshold
    volatility_ready = (
        settings.min_atr_pct <= float(technical_signal.get("atr_pct", 0.0)) <= settings.max_atr_pct
        and float(technical_signal.get("bb_width_pct", 0.0)) >= settings.min_bb_width_pct
    )
    volume_ready = float(technical_signal.get("volume_ratio", 0.0)) >= settings.min_volume_ratio
    trend_ready = (
        (signal == "buy" and float(technical_signal.get("ema_slow_slope", 0.0)) > 0)
        or (signal == "sell" and float(technical_signal.get("ema_slow_slope", 0.0)) < 0)
    )
    cooldown_active = _is_in_cooldown(order_history, settings.trade_cooldown_minutes)
    executable_signal = signal in {"buy", "sell"}

    return {
        "same_direction": same_direction,
        "ai_confident": ai_confident,
        "ai_confidence": ai_confidence,
        "technical_confident": technical_confident,
        "volatility_ready": volatility_ready,
        "volume_ready": volume_ready,
        "trend_ready": trend_ready,
        "cooldown_active": cooldown_active,
        "executable_signal": executable_signal,
    }


def run_cycle() -> None:
    settings = load_settings()
    logger = setup_logger(settings)
    client = BinanceDataClient(settings)
    risk_manager = RiskManager(settings)
    executor = TradeExecutor(settings, client, risk_manager, logger)
    ai_analyzer = OpenRouterAnalyzer(settings, logger)

    logger.info("Iniciando ciclo para %s en %s", settings.trading_symbol, settings.timeframe)
    write_heartbeat("online", "Ciclo iniciado")

    try:
        raw_frame = client.fetch_ohlcv(limit=200)
        enriched_frame = compute_indicators(raw_frame)
        technical_signal = build_technical_signal(enriched_frame)
        ai_signal = ai_analyzer.analyze(enriched_frame)
        balance_usd = client.fetch_balance_usd()
        risk_snapshot = risk_manager.evaluate(balance_usd)
        order_history = load_history(settings.order_history_file)

        if risk_snapshot.kill_switch_triggered:
            decision = {
                "action": "halt",
                "reason": "Kill Switch activado",
            }
            logger.critical(
                "Kill Switch activado. Drawdown %.2f%% excede el límite %.2f%%.",
                risk_snapshot.drawdown_pct * 100,
                settings.kill_switch_drawdown * 100,
            )
            append_history(settings.signal_history_file, _build_signal_event(technical_signal, ai_signal, decision))
            persist_state(
                settings.state_file,
                build_state_snapshot(
                    market=_serialize_market(enriched_frame),
                    technical_signal=technical_signal,
                    ai_signal=ai_signal,
                    risk=asdict(risk_snapshot),
                    decision=decision,
                    order_history=order_history,
                    signal_history=load_history(settings.signal_history_file),
                ),
            )
            write_heartbeat("offline", "Kill Switch activado")
            raise SystemExit(1)

        guardrails = _build_guardrails(settings, technical_signal, ai_signal, order_history)

        if all(
            [
                guardrails["same_direction"],
                guardrails["ai_confident"],
                guardrails["technical_confident"],
                guardrails["volatility_ready"],
                guardrails["volume_ready"],
                guardrails["trend_ready"],
                not guardrails["cooldown_active"],
                guardrails["executable_signal"],
            ]
        ):
            decision = executor.execute(
                side=str(technical_signal["signal"]),
                market_price=float(technical_signal["close"]),
                risk=risk_snapshot,
            )
            append_history(settings.order_history_file, decision)
            order_history = load_history(settings.order_history_file)
        else:
            decision = {
                "action": "hold",
                "reason": "No se cumplen los filtros prudentes de entrada.",
                **guardrails,
            }
            logger.info("Sin operación prudente. Técnica=%s | IA=%s | Guardrails=%s", technical_signal, ai_signal, guardrails)

        append_history(settings.signal_history_file, _build_signal_event(technical_signal, ai_signal, decision))

        persist_state(
            settings.state_file,
            build_state_snapshot(
                market=_serialize_market(enriched_frame),
                technical_signal=technical_signal,
                ai_signal=ai_signal,
                risk=asdict(risk_snapshot),
                decision=decision,
                order_history=order_history,
                signal_history=load_history(settings.signal_history_file),
            ),
        )
        write_heartbeat("online", "Ciclo completado")

    except RequestException as exc:
        write_heartbeat("offline", f"Error de red: {exc}")
        logger.exception("Error de red en OpenRouter o Binance: %s", exc)
    except Exception as exc:  # noqa: BLE001
        write_heartbeat("offline", f"Fallo inesperado: {exc}")
        logger.exception("Fallo inesperado en el ciclo principal: %s", exc)


def main() -> None:
    settings = load_settings()
    logger = setup_logger(settings)
    ensure_control_file()
    logger.info("OptiFerre-Trader iniciado. DRY_RUN=%s", settings.dry_run)

    while True:
        control = load_state(settings.control_file)
        desired_state = control.get("desired_state", "running")

        if desired_state == "paused":
            logger.info("Bot en pausa por control remoto. Esperando reanudación.")
            write_heartbeat("paused", control.get("reason", "Pausa remota activa"))
            time.sleep(max(5, settings.poll_interval_seconds))
            continue

        if desired_state == "stopped":
            logger.warning("Bot detenido por control remoto. Finalizando proceso.")
            write_heartbeat("offline", control.get("reason", "Detención remota"))
            break

        cycle_start = time.monotonic()
        run_cycle()
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1, settings.poll_interval_seconds - int(elapsed))
        logger.info("Ciclo completado. Próxima evaluación en %s segundos.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()