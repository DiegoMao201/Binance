from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from requests import RequestException

from src.analysis.ai_client import OpenRouterAnalyzer
from src.analysis.indicators import build_technical_signal, compute_indicators
from src.data.binance_client import BinanceClientError, BinanceDataClient
from src.execution.trader import TradeExecutor
from src.safety.risk_manager import RiskManager, RiskSnapshot
from src.utils.config import Settings, load_settings
from src.utils.logger import setup_logger
from src.utils.state_store import append_history, build_state_snapshot, load_history, load_state, persist_history, persist_state


AI_NOT_CONSULTED = {
    "signal": "hold",
    "confidence": 0.0,
    "rationale": "IA no consultada: sin pre-se\u00f1al t\u00e9cnica candidata.",
    "model": "lazy_gate",
    "consulted": False,
}


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


def _is_pre_signal_candidate(settings: Settings, technical_signal: dict[str, Any]) -> tuple[bool, str]:
    """Lazy-AI gate: only call the model when the technical layer alone is already candidate."""
    if technical_signal.get("signal") != "buy":
        return False, "tecnica no es buy"
    technical_confidence = float(technical_signal.get("confidence", 0.0))
    if technical_confidence < settings.technical_confidence_threshold:
        return False, "confianza tecnica insuficiente"
    volume_ratio = float(technical_signal.get("volume_ratio", 0.0))
    if volume_ratio < settings.min_volume_ratio:
        return False, "volumen insuficiente"
    atr_pct = float(technical_signal.get("atr_pct", 0.0))
    if not (settings.min_atr_pct <= atr_pct <= settings.max_atr_pct):
        return False, "volatilidad fuera de rango"
    if float(technical_signal.get("ema_slow_slope", 0.0)) <= 0:
        return False, "tendencia no acompana"
    if not (technical_signal.get("bullish_cross") or technical_signal.get("oversold")):
        return False, "sin cruce alcista ni sobreventa"
    return True, "candidato tecnico valido"


def _settle_open_positions(
    open_positions: list[dict[str, Any]],
    latest_candle: dict[str, Any],
    *,
    live_mode: bool,
    executor: TradeExecutor,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    remaining_positions: list[dict[str, Any]] = []
    closed_trades: list[dict[str, Any]] = []
    candle_high = float(latest_candle.get("high", 0.0))
    candle_low = float(latest_candle.get("low", 0.0))
    candle_close = float(latest_candle.get("close", 0.0))
    closed_at = str(latest_candle.get("timestamp"))

    for position in open_positions:
        side = position.get("side")
        entry_price = float(position.get("entry_price", 0.0))
        amount = float(position.get("amount", 0.0))
        stop_loss = float(position.get("stop_loss", 0.0))
        take_profit = float(position.get("take_profit", 0.0))
        exit_price: float | None = None
        exit_reason: str | None = None

        if side == "buy":
            if candle_low <= stop_loss:
                exit_price = stop_loss
                exit_reason = "stop_loss"
            elif candle_high >= take_profit:
                exit_price = take_profit
                exit_reason = "take_profit"
        elif side == "sell":
            if candle_high >= stop_loss:
                exit_price = stop_loss
                exit_reason = "stop_loss"
            elif candle_low <= take_profit:
                exit_price = take_profit
                exit_reason = "take_profit"

        if exit_price is None:
            mark_price = candle_close
            position["unrealized_pnl_usdt"] = round(
                (mark_price - entry_price) * amount if side == "buy" else (entry_price - mark_price) * amount,
                4,
            )
            position["mark_price"] = round(mark_price, 4)
            remaining_positions.append(position)
            continue

        live_payload: dict[str, Any] = {}
        if live_mode and position.get("mode") == "live":
            close_result = executor.close_position_market(position)
            live_payload = close_result
            if close_result.get("status") != "submitted":
                logger.error("No se pudo cerrar posicion live: %s", close_result)
                position["last_close_error"] = close_result
                remaining_positions.append(position)
                continue
            exit_price = float(close_result.get("avg_price") or exit_price)

        pnl_usdt = (exit_price - entry_price) * amount if side == "buy" else (entry_price - exit_price) * amount
        closed_trades.append(
            {
                **position,
                "closed_at": closed_at,
                "exit_price": round(exit_price, 4),
                "exit_reason": exit_reason,
                "pnl_usdt": round(pnl_usdt, 4),
                "pnl_pct": round((pnl_usdt / max(entry_price * amount, 1e-9)), 4),
                "status": "closed",
                "live_close": live_payload or None,
            }
        )

    return remaining_positions, closed_trades


def _build_portfolio_summary(
    settings: Settings,
    risk_snapshot: RiskSnapshot,
    open_positions: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    asset_holdings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    realized_pnl = round(sum(float(item.get("pnl_usdt", 0.0)) for item in closed_trades), 4)
    unrealized_pnl = round(sum(float(item.get("unrealized_pnl_usdt", 0.0)) for item in open_positions), 4)
    simulated_equity = round(settings.initial_capital_usd + realized_pnl + unrealized_pnl, 4)
    wins = sum(1 for item in closed_trades if float(item.get("pnl_usdt", 0.0)) > 0)
    losses = sum(1 for item in closed_trades if float(item.get("pnl_usdt", 0.0)) < 0)
    total_closed = len(closed_trades)
    win_rate = round((wins / total_closed) * 100.0, 2) if total_closed else 0.0
    accumulated_pnl_pct = 0.0
    if settings.initial_capital_usd > 0:
        accumulated_pnl_pct = round(realized_pnl / settings.initial_capital_usd, 6)

    return {
        "mode": "dry_run" if settings.dry_run else "live",
        "open_positions": len(open_positions),
        "closed_trades": total_closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "realized_pnl_usdt": realized_pnl,
        "unrealized_pnl_usdt": unrealized_pnl,
        "accumulated_pnl_pct": accumulated_pnl_pct,
        "simulated_equity_usdt": simulated_equity,
        "balance_reference_usdt": risk_snapshot.balance_usd,
        "equity_usdt": risk_snapshot.equity_usd,
        "high_water_mark_usdt": risk_snapshot.high_water_mark,
        "max_drawdown_pct": risk_snapshot.drawdown_pct,
        "asset_holdings": asset_holdings or {},
    }


def _compute_equity(balance_usd: float, open_positions: list[dict[str, Any]], mark_price: float) -> float:
    open_value = 0.0
    for position in open_positions:
        amount = float(position.get("amount") or 0.0)
        entry = float(position.get("entry_price") or 0.0)
        side = position.get("side")
        if side == "buy":
            open_value += amount * mark_price
        elif side == "sell":
            open_value += amount * entry + (entry - mark_price) * amount
        else:
            open_value += amount * mark_price
    if any(p.get("side") == "buy" for p in open_positions):
        # We already deducted USDT to open the buy, so equity = remaining USDT + asset value
        return float(balance_usd) + open_value
    return float(balance_usd) + open_value


def _update_high_water_mark(equity_history: list[dict[str, Any]], equity_usd: float, timestamp: str) -> tuple[float, list[dict[str, Any]]]:
    previous_hwm = 0.0
    for item in equity_history:
        try:
            previous_hwm = max(previous_hwm, float(item.get("high_water_mark", 0.0)))
        except (TypeError, ValueError):
            continue
    new_hwm = max(previous_hwm, equity_usd)
    equity_history.append(
        {
            "timestamp": timestamp,
            "equity_usdt": round(equity_usd, 4),
            "high_water_mark": round(new_hwm, 4),
        }
    )
    return new_hwm, equity_history


def _set_control_state(settings: Settings, desired_state: str, reason: str) -> None:
    payload = {
        "desired_state": desired_state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "kill_switch",
        "reason": reason,
    }
    persist_state(settings.control_file, payload)


def pre_flight_check(settings: Settings, client: BinanceDataClient, logger: logging.Logger) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "dry_run": settings.dry_run,
        "api_keys_present": bool(settings.binance_api_key and settings.binance_api_secret),
        "control_file_ok": False,
        "binance_reachable": False,
        "balance_ok": False,
        "balance_usdt": 0.0,
        "minimum_required": round(settings.minimum_trade_usdt * 1.10, 4),
        "ok": False,
        "detail": "",
    }
    try:
        ensure_control_file()
        checks["control_file_ok"] = True
    except Exception as exc:  # noqa: BLE001
        checks["detail"] = f"control_file: {exc}"
        return checks

    if settings.dry_run:
        checks["binance_reachable"] = client.ping()
        checks["balance_ok"] = True
        checks["ok"] = checks["binance_reachable"]
        checks["detail"] = "DRY_RUN activo: chequeo m\u00ednimo aprobado." if checks["ok"] else "DRY_RUN: sin acceso a Binance."
        return checks

    if not checks["api_keys_present"]:
        checks["detail"] = "Faltan claves API para operar live."
        return checks

    try:
        balance = client.fetch_balance_usd()
        checks["binance_reachable"] = True
        checks["balance_usdt"] = round(balance, 4)
        checks["balance_ok"] = balance >= checks["minimum_required"]
    except BinanceClientError as exc:
        checks["detail"] = f"binance: {exc}"
        return checks

    if not checks["balance_ok"]:
        checks["detail"] = (
            f"Saldo USDT {checks['balance_usdt']} < requerido {checks['minimum_required']}"
        )
        return checks

    checks["ok"] = True
    checks["detail"] = "Pre-flight OK para operar live."
    return checks


def _load_recent_ai_signal(settings) -> dict[str, Any] | None:
    previous_state = load_state(settings.state_file)
    previous_ai_signal = previous_state.get("ai_signal")
    updated_at = previous_state.get("updated_at")
    if not previous_ai_signal or not updated_at:
        return None
    if not previous_ai_signal.get("consulted", True):
        return None

    try:
        previous_updated_at = datetime.fromisoformat(updated_at)
    except ValueError:
        return None

    age_seconds = (datetime.now(timezone.utc) - previous_updated_at).total_seconds()
    if age_seconds > settings.ai_min_interval_seconds:
        return None

    cached_ai_signal = dict(previous_ai_signal)
    cached_ai_signal["cached"] = True
    cached_ai_signal["cached_age_seconds"] = round(age_seconds, 1)
    return cached_ai_signal


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

        order_history = load_history(settings.order_history_file)
        open_positions = load_history(settings.open_positions_file)
        closed_trades = load_history(settings.closed_trades_file)
        equity_history = load_history(settings.equity_history_file)

        latest_candle = enriched_frame.iloc[-1].to_dict()
        live_mode = not settings.dry_run
        open_positions, newly_closed_trades = _settle_open_positions(
            open_positions,
            latest_candle,
            live_mode=live_mode,
            executor=executor,
            logger=logger,
        )
        if newly_closed_trades:
            closed_trades.extend(newly_closed_trades)
            persist_history(settings.closed_trades_file, closed_trades)
        persist_history(settings.open_positions_file, open_positions)
        has_open_position = bool(open_positions)

        candidate, candidate_reason = _is_pre_signal_candidate(settings, technical_signal)
        if candidate and not has_open_position:
            ai_signal = _load_recent_ai_signal(settings)
            if ai_signal is None:
                ai_signal = ai_analyzer.analyze(enriched_frame)
                ai_signal.setdefault("consulted", True)
            else:
                logger.info(
                    "Reutilizando se\u00f1al de IA en cach\u00e9 (%ss de antig\u00fcedad).",
                    ai_signal.get("cached_age_seconds", 0),
                )
        else:
            ai_signal = dict(AI_NOT_CONSULTED)
            ai_signal["rationale"] = f"Lazy AI: {candidate_reason}."
            logger.info("Lazy AI activo (%s); no se consulta OpenRouter este ciclo.", candidate_reason)

        try:
            balance_usd = client.fetch_balance_usd()
        except BinanceClientError as exc:
            logger.error("No se pudo obtener saldo: %s", exc)
            write_heartbeat("offline", f"Saldo no disponible: {exc}")
            return

        asset_holdings: dict[str, Any] = {}
        if live_mode and settings.binance_api_key:
            try:
                asset_holdings = client.fetch_asset_balance()
            except BinanceClientError as exc:
                logger.error("No se pudo leer holdings: %s", exc)
                asset_holdings = {"asset": client.base_asset, "free": 0.0, "used": 0.0, "total": 0.0, "error": str(exc)}

        mark_price = float(latest_candle.get("close") or 0.0)
        equity_usd = _compute_equity(balance_usd, open_positions, mark_price)
        new_hwm, equity_history = _update_high_water_mark(
            equity_history,
            equity_usd,
            datetime.now(timezone.utc).isoformat(),
        )
        persist_history(settings.equity_history_file, equity_history)

        risk_snapshot = risk_manager.evaluate(balance_usd, equity_usd=equity_usd, high_water_mark=new_hwm)

        if risk_snapshot.kill_switch_triggered:
            decision = {
                "action": "halt",
                "reason": "Kill Switch activado por drawdown",
                "drawdown_pct": risk_snapshot.drawdown_pct,
            }
            logger.critical(
                "Kill Switch activado. Drawdown %.2f%% excede el l\u00edmite %.2f%%.",
                risk_snapshot.drawdown_pct * 100,
                settings.kill_switch_drawdown * 100,
            )
            _set_control_state(
                settings,
                "stopped",
                f"Kill Switch: drawdown {risk_snapshot.drawdown_pct:.4f} >= {settings.kill_switch_drawdown}",
            )
            append_history(settings.signal_history_file, _build_signal_event(technical_signal, ai_signal, decision))
            persist_state(
                settings.state_file,
                build_state_snapshot(
                    market=_serialize_market(enriched_frame),
                    technical_signal=technical_signal,
                    ai_signal=ai_signal,
                    risk=asdict(risk_snapshot),
                    portfolio=_build_portfolio_summary(settings, risk_snapshot, open_positions, closed_trades, asset_holdings),
                    decision=decision,
                    order_history=order_history,
                    open_positions=open_positions,
                    closed_trades=closed_trades,
                    signal_history=load_history(settings.signal_history_file),
                ),
            )
            write_heartbeat("offline", "Kill Switch activado")
            return

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
                not has_open_position,
                str(technical_signal["signal"]) == "buy",
                ai_signal.get("consulted", False),
            ]
        ):
            decision = executor.execute(
                side=str(technical_signal["signal"]),
                market_price=float(technical_signal["close"]),
                risk=risk_snapshot,
            )
            append_history(settings.order_history_file, decision)
            order_history = load_history(settings.order_history_file)

            if decision.get("status") in {"error", "reconcile_failed"} and live_mode:
                _set_control_state(
                    settings,
                    "stopped",
                    f"Kill Switch ejecucion: {decision.get('reason', 'fallo de exchange')}",
                )
                logger.critical("Ejecuci\u00f3n fall\u00f3 en live; bot detenido. %s", decision)

            if decision.get("status") in {"simulated", "submitted"}:
                open_positions.append(
                    {
                        "opened_at": decision.get("timestamp"),
                        "symbol": decision.get("symbol"),
                        "side": decision.get("side"),
                        "amount": decision.get("amount"),
                        "entry_price": decision.get("price"),
                        "notional_usdt": decision.get("notional_usdt"),
                        "stop_loss": decision.get("stop_loss"),
                        "take_profit": decision.get("take_profit"),
                        "status": "open",
                        "mode": decision.get("mode"),
                        "reconciled_holdings": decision.get("reconciled_holdings"),
                    }
                )
                persist_history(settings.open_positions_file, open_positions)
        else:
            decision = {
                "action": "hold",
                "reason": "No se cumplen los filtros prudentes de entrada.",
                "position_open": has_open_position,
                "spot_ready": str(technical_signal["signal"]) == "buy",
                "ai_consulted": ai_signal.get("consulted", False),
                "lazy_gate_reason": candidate_reason,
                **guardrails,
            }
            logger.info("Sin operaci\u00f3n prudente. T=%s | IA=%s | G=%s", technical_signal, ai_signal, guardrails)

        append_history(settings.signal_history_file, _build_signal_event(technical_signal, ai_signal, decision))

        persist_state(
            settings.state_file,
            build_state_snapshot(
                market=_serialize_market(enriched_frame),
                technical_signal=technical_signal,
                ai_signal=ai_signal,
                risk=asdict(risk_snapshot),
                portfolio=_build_portfolio_summary(settings, risk_snapshot, open_positions, closed_trades, asset_holdings),
                decision=decision,
                order_history=order_history,
                open_positions=open_positions,
                closed_trades=closed_trades,
                signal_history=load_history(settings.signal_history_file),
            ),
        )
        write_heartbeat("online", "Ciclo completado")

    except BinanceClientError as exc:
        write_heartbeat("offline", f"Binance error: {exc}")
        logger.exception("Error de Binance: %s", exc)
        if not settings.dry_run:
            _set_control_state(settings, "stopped", f"Kill Switch infra: {exc}")
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

    client = BinanceDataClient(settings)
    pre_flight = pre_flight_check(settings, client, logger)
    persist_state(
        settings.logs_dir / "pre_flight.json",
        {"timestamp": datetime.now(timezone.utc).isoformat(), **pre_flight},
    )
    if not pre_flight["ok"]:
        logger.critical("Pre-flight fall\u00f3: %s. Forzando pausa.", pre_flight["detail"])
        _set_control_state(settings, "paused", f"Pre-flight: {pre_flight['detail']}")
        write_heartbeat("paused", f"Pre-flight: {pre_flight['detail']}")
    else:
        logger.info("Pre-flight OK: %s", pre_flight["detail"])

    while True:
        control = load_state(settings.control_file)
        desired_state = control.get("desired_state", "running")

        if desired_state == "paused":
            logger.info("Bot en pausa por control remoto. Esperando reanudaci\u00f3n.")
            write_heartbeat("paused", control.get("reason", "Pausa remota activa"))
            time.sleep(max(5, settings.poll_interval_seconds))
            continue

        if desired_state == "stopped":
            logger.warning("Bot detenido por control remoto. Finalizando proceso.")
            write_heartbeat("offline", control.get("reason", "Detenci\u00f3n remota"))
            break

        cycle_start = time.monotonic()
        run_cycle()
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1, settings.poll_interval_seconds - int(elapsed))
        logger.info("Ciclo completado. Pr\u00f3xima evaluaci\u00f3n en %s segundos.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()