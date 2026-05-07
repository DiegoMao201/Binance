from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import ccxt
from requests import RequestException

from src.analysis.ai_client import OpenRouterAnalyzer
from src.analysis.indicators import build_technical_signal, compute_indicators
from src.data.binance_client import BinanceClientError, BinanceDataClient
from src.execution.trader import TradeExecutor
from src.safety.risk_manager import RiskManager, RiskSnapshot
from src.utils.config import Settings, load_settings
from src.utils.logger import setup_logger
from src.utils.telegram_telemetry import TelegramTelemetry
from src.utils.state_store import append_history, build_state_snapshot, load_history, load_state, persist_history, persist_state


AI_NOT_CONSULTED = {
    "signal": "hold",
    "confidence": 0.0,
    "rationale": "IA no consultada: sin pre-se\u00f1al t\u00e9cnica candidata.",
    "model": "lazy_gate",
    "consulted": False,
    "approved": False,
    "direction_alignment": "misaligned",
    "setup_quality": "low",
    "risk_flags": ["not_consulted"],
}

ALGO_VERSION = "v2.0_tiered"
LEGACY_HOT_SWAP_VERSION = "legacy_hot_swap"


def _build_notifier(settings: Settings, logger: logging.Logger) -> TelegramTelemetry:
    return TelegramTelemetry(
        enabled=settings.telegram_enabled,
        logger=logger,
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )


def _notify_safe(notifier: TelegramTelemetry, logger: logging.Logger, level: str, data: dict[str, Any]) -> None:
    if not notifier.configured:
        return

    try:
        loop = asyncio.get_running_loop()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, RuntimeError):
            notifier.send_alert_nowait(level, data)
            return
        logger.warning("No se pudo programar notificacion Telegram: %s", exc)
        return

    task = loop.create_task(notifier.send_alert(level, data))
    task.add_done_callback(lambda scheduled: _log_async_notification_failure(scheduled, logger))


def _log_async_notification_failure(task: asyncio.Task[None], logger: logging.Logger) -> None:
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo enviar notificacion Telegram: %s", exc)


_AUTH_ALERT_STATE: dict[str, float] = {"last_sent": 0.0}


def _maybe_notify_auth_invalid(settings: Settings, logger: logging.Logger, detail: str) -> None:
    """Send a critical Telegram alert about Binance auth failure, rate-limited
    to once every 30 minutes to avoid spam while the operator fixes whitelist."""
    now = time.time()
    if now - _AUTH_ALERT_STATE["last_sent"] < 1800:
        return
    _AUTH_ALERT_STATE["last_sent"] = now
    notifier = _build_notifier(settings, logger)
    _notify_safe(
        notifier,
        logger,
        "critical",
        {
            "title": "BINANCE AUTH INVALIDA",
            "detail": (
                f"{detail} | Revisa: 1) IP egress en whitelist Binance, "
                "2) permisos Spot Trading, 3) BINANCE_PROXY_URL en Coolify."
            ),
            "status": "degraded",
        },
    )


def _format_trade_open_message(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": decision.get("symbol", "n/d"),
        "side": str(decision.get("side", "LONG")).upper(),
        "entry_price": decision.get("fill_price") or decision.get("price") or 0.0,
        "stop_loss": decision.get("stop_loss", 0.0),
        "pnl_usdt": decision.get("pnl_usdt", 0.0),
    }


def _format_trade_close_message(closed_trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": closed_trade.get("symbol", "n/d"),
        "side": "CLOSE",
        "entry": closed_trade.get("entry_price") or closed_trade.get("fill_price") or closed_trade.get("exit_price"),
        "stop_loss": closed_trade.get("stop_loss"),
        "pnl_usdt": closed_trade.get("pnl_usdt", 0.0),
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
        "symbol": decision.get("symbol") or technical_signal.get("symbol"),
        "technical_signal": technical_signal.get("signal", "hold"),
        "technical_rsi": technical_signal.get("rsi", 0),
        "technical_price": technical_signal.get("close", 0),
        "scenario": technical_signal.get("scenario"),
        "scenario_a": technical_signal.get("scenario_a", False),
        "scenario_b": technical_signal.get("scenario_b", False),
        "ai_signal": ai_signal.get("signal", "hold"),
        "ai_confidence": ai_signal.get("confidence", 0),
        "decision_action": decision.get("action", decision.get("side", "hold")),
        "decision_status": decision.get("status", decision.get("reason", "n/a")),
    }


def _build_scan_history_event(
    *,
    timestamp: str,
    scans: list[dict[str, Any]],
    ai_signal: dict[str, Any],
    ai_consulted_symbol: str | None,
    global_lock: bool,
    active_symbol: str | None,
    decision: dict[str, Any],
    balance_ok: bool,
    balance_error: str | None = None,
    balance_error_class: str | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "global_lock": global_lock,
        "active_symbol": active_symbol,
        "decision_action": decision.get("action", decision.get("side", "hold")),
        "decision_reason": decision.get("reason", decision.get("status", "n/a")),
        "balance_ok": balance_ok,
        "balance_error": balance_error,
        "balance_error_class": balance_error_class,
        "ai_consulted": bool(ai_signal.get("consulted")),
        "ai_consulted_symbol": ai_consulted_symbol,
        "scans": scans,
    }


def _is_in_cooldown(order_history: list[dict[str, Any]], cooldown_minutes: int) -> bool:
    if not order_history:
        return False

    latest = next(
        (order for order in reversed(order_history) if str(order.get("status", "")).lower() == "submitted"),
        None,
    )
    if not latest:
        return False

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
    scenario = technical_signal.get("scenario")
    same_direction = signal == ai_signal.get("signal") if signal == "buy" else True
    ai_confidence = float(ai_signal.get("confidence", 0.0))
    ai_confident = ai_confidence >= settings.ai_confidence_threshold
    ai_approved = bool(ai_signal.get("approved", False))
    ai_alignment = ai_signal.get("direction_alignment") == "aligned"
    ai_setup_ready = str(ai_signal.get("setup_quality", "low")).lower() == "high"
    risk_flags = ai_signal.get("risk_flags") or []
    ai_risk_clear = isinstance(risk_flags, list) and len(risk_flags) == 0
    # Volatilidad: solo exigimos piso de ATR (regla del usuario), techo opcional.
    atr_pct = float(technical_signal.get("atr_pct", 0.0))
    volatility_ready = atr_pct >= settings.min_atr_pct and atr_pct <= settings.max_atr_pct
    volume_ready = float(technical_signal.get("volume_ratio", 0.0)) >= settings.min_volume_ratio
    cooldown_active = _is_in_cooldown(order_history, settings.trade_cooldown_minutes)
    executable_signal = signal == "buy" and scenario in {"A", "B"}

    return {
        "scenario": scenario,
        "scenario_a": bool(technical_signal.get("scenario_a")),
        "scenario_b": bool(technical_signal.get("scenario_b")),
        "same_direction": same_direction,
        "ai_confident": ai_confident,
        "ai_approved": ai_approved,
        "ai_alignment": ai_alignment,
        "ai_setup_ready": ai_setup_ready,
        "ai_risk_clear": ai_risk_clear,
        "ai_confidence": ai_confidence,
        "volatility_ready": volatility_ready,
        "volume_ready": volume_ready,
        "cooldown_active": cooldown_active,
        "executable_signal": executable_signal,
    }


def _is_pre_signal_candidate(settings: Settings, technical_signal: dict[str, Any]) -> tuple[bool, str]:

    scenario = technical_signal.get("scenario")
    if scenario not in {"A", "B"}:
        return False, "sin escenario A ni B"
    volume_ratio = float(technical_signal.get("volume_ratio", 0.0))
    if volume_ratio < settings.min_volume_ratio:
        return False, "volumen insuficiente"
    atr_pct = float(technical_signal.get("atr_pct", 0.0))
    if not (settings.min_atr_pct <= atr_pct <= settings.max_atr_pct):
        return False, "volatilidad fuera de rango"
    return True, f"candidato escenario {scenario}"


def _build_scan_summary(scan: dict[str, Any], settings: Settings, *, blocked_by_lock: bool) -> dict[str, Any]:
    """Resumen para el dashboard del estado de cada ticker en el ciclo actual."""
    ts = scan.get("technical_signal", {})
    candidate = bool(scan.get("candidate"))
    if blocked_by_lock:
        status = "locked"
    elif candidate:
        status = "candidate"
    elif ts.get("scenario") in {"A", "B"}:
        status = "scenario_only"
    else:
        status = "waiting"
    candle = scan.get("latest_candle") or {}
    rejection_stage = "pre_signal"
    rejection_reason = scan.get("candidate_reason")
    if blocked_by_lock:
        rejection_stage = "lock"
        rejection_reason = "mutex global activo"
    elif candidate:
        rejection_stage = "candidate"
        rejection_reason = "esperando validacion IA/ejecucion"
    elif ts.get("scenario") in {"A", "B"}:
        rejection_stage = "guardrail"
    return {
        "symbol": scan.get("symbol"),
        "status": status,
        "scenario": ts.get("scenario"),
        "scenario_a": bool(ts.get("scenario_a")),
        "scenario_b": bool(ts.get("scenario_b")),
        "rsi": ts.get("rsi"),
        "close": ts.get("close"),
        "atr_pct": ts.get("atr_pct"),
        "volume_ratio": ts.get("volume_ratio"),
        "ema_slow": ts.get("ema_slow"),
        "green_candle": ts.get("green_candle"),
        "candidate_reason": scan.get("candidate_reason"),
        "rejection_stage": rejection_stage,
        "rejection_reason": rejection_reason,
        "ia_consulted": bool(scan.get("ia_consulted")),
        "ia_confidence": float(scan.get("ia_confidence") or 0.0),
        "blocked_by_lock": blocked_by_lock,
        "candle_timestamp": str(candle.get("timestamp")) if candle else None,
    }


def _degraded_risk_snapshot(
    previous_state: dict[str, Any],
    *,
    balance_error: str,
    balance_usd: float | None = None,
    equity_usd: float | None = None,
) -> RiskSnapshot:
    previous_risk = previous_state.get("risk") or {}
    _ = balance_error
    return RiskSnapshot(
        balance_usd=float(balance_usd if balance_usd is not None else previous_risk.get("balance_usd", 0.0) or 0.0),
        equity_usd=float(equity_usd if equity_usd is not None else previous_risk.get("equity_usd", 0.0) or 0.0),
        high_water_mark=float(previous_risk.get("high_water_mark", 0.0) or 0.0),
        max_trade_usd=float(previous_risk.get("max_trade_usd", 0.0) or 0.0),
        recommended_trade_usd=float(previous_risk.get("recommended_trade_usd", 0.0) or 0.0),
        drawdown_pct=float(previous_risk.get("drawdown_pct", 0.0) or 0.0),
        daily_pnl_pct=float(previous_risk.get("daily_pnl_pct", 0.0) or 0.0),
        kill_switch_triggered=bool(previous_risk.get("kill_switch_triggered", False)),
    )


async def _settle_open_positions(
    open_positions: list[dict[str, Any]],
    candles_by_symbol: dict[str, dict[str, Any]],
    *,
    settings: Settings,
    live_mode: bool,
    executor: TradeExecutor,
    logger: logging.Logger,
    persist_open_positions: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trailing_tiers: tuple[tuple[int, float, float], ...] = (
        (1, 0.008, 0.004),
        (2, 0.010, 0.006),
        (3, 0.014, 0.008),
        (4, 0.018, 0.010),
    )

    def _resolve_trailing_tier(mfe_pct: float) -> tuple[int, float | None]:
        target_tier = 0
        target_offset = None
        for tier_id, trigger_pct, offset_pct in trailing_tiers:
            if mfe_pct >= trigger_pct:
                target_tier = tier_id
                target_offset = offset_pct
        return target_tier, target_offset

    remaining_positions: list[dict[str, Any]] = []
    closed_trades: list[dict[str, Any]] = []

    for position_index, position in enumerate(open_positions):
        symbol = position.get("symbol")
        latest_candle = candles_by_symbol.get(symbol) if symbol else None
        if not latest_candle:
            # Sin vela disponible para este simbolo; preservamos la posicion sin tocar.
            remaining_positions.append(position)
            continue

        candle_high = float(latest_candle.get("high", 0.0))
        candle_low = float(latest_candle.get("low", 0.0))
        candle_close = float(latest_candle.get("close", 0.0))
        closed_at = str(latest_candle.get("timestamp"))

        side = position.get("side")
        entry_price = float(position.get("entry_price", 0.0))
        amount = float(position.get("amount", 0.0))
        stop_loss = float(position.get("stop_loss", 0.0))
        take_profit = float(position.get("take_profit", 0.0))
        trailing_tier = int(
            position.get(
                "trailing_tier",
                1 if bool(position.get("trailing_activated", False)) else 0,
            ) or 0
        )

        # ---- Hold time desde el fill (necesario para trailing/time-stop antes del exit check) ----
        hold_minutes: float | None = None
        opened_at_raw = position.get("opened_at")
        candle_at_raw = latest_candle.get("timestamp")
        if opened_at_raw and candle_at_raw:
            try:
                opened_at = datetime.fromisoformat(str(opened_at_raw).replace("Z", "+00:00"))
                candle_at = candle_at_raw if isinstance(candle_at_raw, datetime) else datetime.fromisoformat(str(candle_at_raw).replace("Z", "+00:00"))
                hold_minutes = max(0.0, (candle_at - opened_at).total_seconds() / 60.0)
            except ValueError:
                hold_minutes = None

        # ---- MAE / MFE telemetry (porcentajes y absolutos en USDT) ----
        if entry_price > 0 and amount > 0:
            if side == "buy":
                adverse_price = candle_low
                favorable_price = candle_high
                adverse_pct = (entry_price - adverse_price) / entry_price
                favorable_pct = (favorable_price - entry_price) / entry_price
            else:
                adverse_price = candle_high
                favorable_price = candle_low
                adverse_pct = (adverse_price - entry_price) / entry_price
                favorable_pct = (entry_price - favorable_price) / entry_price

            adverse_pct = max(0.0, adverse_pct)
            favorable_pct = max(0.0, favorable_pct)
            adverse_usdt = adverse_pct * entry_price * amount
            favorable_usdt = favorable_pct * entry_price * amount

            position["mae_pct"] = round(max(float(position.get("mae_pct", 0.0)), adverse_pct), 6)
            position["mfe_pct"] = round(max(float(position.get("mfe_pct", 0.0)), favorable_pct), 6)
            position["mae_usdt"] = round(max(float(position.get("mae_usdt", 0.0)), adverse_usdt), 4)
            position["mfe_usdt"] = round(max(float(position.get("mfe_usdt", 0.0)), favorable_usdt), 4)
        # ---------------------------------------------------------------

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
            unrealized_pnl_usdt = round(
                (mark_price - entry_price) * amount if side == "buy" else (entry_price - mark_price) * amount,
                4,
            )
            position["unrealized_pnl_usdt"] = unrealized_pnl_usdt
            position["mark_price"] = round(mark_price, 4)

            unrealized_pct = 0.0
            if entry_price > 0:
                unrealized_pct = (mark_price - entry_price) / entry_price if side == "buy" else (entry_price - mark_price) / entry_price

            position["hold_minutes"] = round(hold_minutes, 1) if hold_minutes is not None else None

            # ---- Tiered Trailing Stop (persistido por niveles) ----
            # El bot solo toca Binance al cruzar un tier superior de MFE. Esto evita churn
            # intra-tier y mantiene el presupuesto de rate limit bajo control.
            mfe_pct = float(position.get("mfe_pct", 0.0) or 0.0)
            new_tier, new_sl_offset_pct = _resolve_trailing_tier(mfe_pct)
            if new_tier > trailing_tier and new_sl_offset_pct is not None and entry_price > 0:
                raw_new_sl = entry_price * (1 + new_sl_offset_pct) if side == "buy" else entry_price * (1 - new_sl_offset_pct)
                try:
                    new_sl = await asyncio.to_thread(
                        executor.client.price_to_precision,
                        float(raw_new_sl),
                        symbol=symbol,
                    )
                except BinanceClientError as exc:
                    logger.warning(
                        "Tiered trailing %s: no se pudo formatear SL tier=%s (%s). Reintento próximo tick.",
                        symbol,
                        new_tier,
                        exc,
                    )
                    new_sl = None

                is_improvement = bool(
                    new_sl is not None
                    and (
                        (side == "buy" and (stop_loss <= 0 or new_sl > stop_loss))
                        or (side == "sell" and (stop_loss <= 0 or new_sl < stop_loss))
                    )
                )
                if is_improvement:
                    try:
                        replace_result = await executor.replace_stop_loss_async(
                            position=position,
                            new_stop_loss=float(new_sl),
                            take_profit=float(take_profit),
                            trailing_tier=new_tier,
                        )
                    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                        logger.warning(
                            "Tiered trailing %s: Binance rechazo tier=%s new_SL=%.6f (%s). Estado no actualizado.",
                            symbol,
                            new_tier,
                            new_sl,
                            exc,
                        )
                    except BinanceClientError as exc:
                        logger.warning(
                            "Tiered trailing %s: error controlado tier=%s (%s). Estado no actualizado.",
                            symbol,
                            new_tier,
                            exc,
                        )
                    else:
                        if replace_result.get("status") in {"submitted", "simulated"}:
                            position["initial_stop_loss"] = position.get("initial_stop_loss", round(stop_loss, 4))
                            position["stop_loss"] = round(float(new_sl), 4)
                            position["trailing_tier"] = new_tier
                            position["trailing_tier_updated_at"] = datetime.now(timezone.utc).isoformat()
                            position["trailing_sl"] = round(float(new_sl), 4)
                            position["trailing_mfe_pct"] = round(mfe_pct, 6)
                            position["protection_order"] = replace_result.get("protection_order")
                            position.pop("trailing_activated", None)
                            position.pop("trailing_activated_at", None)
                            position.pop("trailing_activation_pct", None)
                            position.pop("trailing_sl_offset_pct", None)
                            if persist_open_positions is not None:
                                await persist_open_positions(
                                    open_positions[:position_index] + [position] + open_positions[position_index + 1 :]
                                )
                            logger.info(
                                "Tiered trailing confirmado %s side=%s tier=%s mfe=%.4f%% new_SL=%.6f",
                                symbol,
                                side,
                                new_tier,
                                mfe_pct * 100,
                                new_sl,
                            )

            # ---- Invalidador de Inercia (Time-Stop Bidireccional) ----
            # Si tras TIME_STOP_MINUTES el setup no produjo movimiento neto suficiente
            # (|PnL| <= TIME_STOP_DEAD_ZONE_PCT) consideramos invalidada la inercia técnica
            # y liberamos el Mutex. Si el PnL salió de la zona muerta dejamos correr al SL/TP.
            time_stop_enabled = (
                settings.time_stop_minutes > 0
                and settings.time_stop_dead_zone_pct > 0
                and hold_minutes is not None
            )
            if (
                time_stop_enabled
                and hold_minutes >= settings.time_stop_minutes
                and abs(unrealized_pct) <= settings.time_stop_dead_zone_pct
            ):
                exit_price = mark_price
                exit_reason = "time_stop_invalidator"
                position["time_stop"] = {
                    "hold_minutes": round(hold_minutes, 1),
                    "unrealized_pct": round(unrealized_pct, 6),
                    "threshold_minutes": settings.time_stop_minutes,
                    "dead_zone_pct": settings.time_stop_dead_zone_pct,
                }
            else:
                remaining_positions.append(position)
                continue

        live_payload: dict[str, Any] = {}
        if live_mode and position.get("mode") == "live":
            close_result = await asyncio.to_thread(executor.close_position_market, position)
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
                "algo_version": position.get("algo_version") or LEGACY_HOT_SWAP_VERSION,
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
    equity_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    filtered_closed_trades = [
        item for item in closed_trades if str(item.get("algo_version") or "") == ALGO_VERSION
    ]

    realized_pnl = round(sum(float(item.get("pnl_usdt", 0.0)) for item in filtered_closed_trades), 4)
    unrealized_pnl = round(sum(float(item.get("unrealized_pnl_usdt", 0.0)) for item in open_positions), 4)
    simulated_equity = round(settings.initial_capital_usd + realized_pnl + unrealized_pnl, 4)
    wins = sum(1 for item in filtered_closed_trades if float(item.get("pnl_usdt", 0.0)) > 0)
    losses = sum(1 for item in filtered_closed_trades if float(item.get("pnl_usdt", 0.0)) < 0)
    total_closed = len(filtered_closed_trades)
    win_rate = round((wins / total_closed) * 100.0, 2) if total_closed else 0.0
    accumulated_pnl_pct = 0.0
    if settings.initial_capital_usd > 0:
        accumulated_pnl_pct = round(realized_pnl / settings.initial_capital_usd, 6)

    # --- Telemetria por escenario (global) ---
    def _bucket_stats(bucket: list[dict[str, Any]]) -> dict[str, Any]:
        if not bucket:
            return {"trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "pnl_usdt": 0.0, "avg_mae_pct": 0.0, "avg_mfe_pct": 0.0}
        s_wins = sum(1 for t in bucket if float(t.get("pnl_usdt", 0.0)) > 0)
        s_losses = sum(1 for t in bucket if float(t.get("pnl_usdt", 0.0)) < 0)
        s_pnl = round(sum(float(t.get("pnl_usdt", 0.0)) for t in bucket), 4)
        avg_mae = round(sum(float(t.get("mae_pct", 0.0)) for t in bucket) / len(bucket), 6)
        avg_mfe = round(sum(float(t.get("mfe_pct", 0.0)) for t in bucket) / len(bucket), 6)
        return {
            "trades": len(bucket),
            "wins": s_wins,
            "losses": s_losses,
            "win_rate_pct": round((s_wins / len(bucket)) * 100.0, 2),
            "pnl_usdt": s_pnl,
            "avg_mae_pct": avg_mae,
            "avg_mfe_pct": avg_mfe,
        }

    scenario_stats: dict[str, dict[str, Any]] = {
        label: _bucket_stats([t for t in filtered_closed_trades if t.get("scenario") == label])
        for label in ("A", "B")
    }

    # --- Telemetria por simbolo ---
    symbols_in_history = sorted({t.get("symbol") for t in filtered_closed_trades if t.get("symbol")})
    per_symbol_stats: dict[str, dict[str, Any]] = {}
    for sym in symbols_in_history:
        sym_trades = [t for t in filtered_closed_trades if t.get("symbol") == sym]
        per_symbol_stats[sym] = {
            "global": _bucket_stats(sym_trades),
            "A": _bucket_stats([t for t in sym_trades if t.get("scenario") == "A"]),
            "B": _bucket_stats([t for t in sym_trades if t.get("scenario") == "B"]),
        }

    # --- Velocidad de drawdown: tiempo desde el ultimo HWM nuevo ---
    drawdown_velocity_seconds = 0.0
    last_hwm_at: str | None = None
    if equity_history:
        peak = 0.0
        peak_ts: str | None = None
        for item in equity_history:
            try:
                hwm = float(item.get("high_water_mark", 0.0))
            except (TypeError, ValueError):
                continue
            if hwm > peak:
                peak = hwm
                peak_ts = item.get("timestamp")
        if peak_ts:
            last_hwm_at = peak_ts
            try:
                drawdown_velocity_seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - datetime.fromisoformat(peak_ts)).total_seconds(),
                )
            except ValueError:
                drawdown_velocity_seconds = 0.0

    return {
        "mode": "dry_run" if settings.dry_run else "live",
        "algo_version": ALGO_VERSION,
        "open_positions": len(open_positions),
        "closed_trades_all": len(closed_trades),
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
        "scenario_stats": scenario_stats,
        "per_symbol_stats": per_symbol_stats,
        "drawdown_velocity_seconds": round(drawdown_velocity_seconds, 1),
        "last_hwm_at": last_hwm_at,
    }


def _reconcile_live_open_positions(
    open_positions: list[dict[str, Any]],
    candles_by_symbol: dict[str, dict[str, Any]],
    *,
    client: BinanceDataClient,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    remaining_positions: list[dict[str, Any]] = []
    externally_closed: list[dict[str, Any]] = []

    for position in open_positions:
        if position.get("mode") != "live" or position.get("side") != "buy":
            remaining_positions.append(position)
            continue

        symbol = str(position.get("symbol") or "")
        amount = float(position.get("amount") or 0.0)
        if not symbol or amount <= 0:
            remaining_positions.append(position)
            continue

        try:
            holdings = client.fetch_asset_balance(symbol=symbol)
        except BinanceClientError as exc:
            logger.warning("No se pudo reconciliar holdings para %s: %s", symbol, exc)
            remaining_positions.append(position)
            continue

        held_total = float(holdings.get("total") or 0.0)
        held_ratio = held_total / max(amount, 1e-12)
        if held_ratio > 0.2:
            position["reconciled_holdings"] = holdings
            remaining_positions.append(position)
            continue

        candle = candles_by_symbol.get(symbol) or {}
        exit_price = float(candle.get("close") or position.get("fill_price") or position.get("entry_price") or 0.0)
        entry_price = float(position.get("entry_price") or 0.0)
        pnl_usdt = (exit_price - entry_price) * amount
        logger.warning(
            "Posicion %s reconciliada como cerrada fuera del bot. holdings_total=%.8f amount=%.8f",
            symbol,
            held_total,
            amount,
        )
        externally_closed.append(
            {
                **position,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "exit_price": round(exit_price, 4),
                "exit_reason": "external_reconcile",
                "pnl_usdt": round(pnl_usdt, 4),
                "pnl_pct": round((pnl_usdt / max(entry_price * amount, 1e-9)), 4),
                "status": "closed",
                "live_close": {
                    "status": "reconciled_external",
                    "post_close_holdings": holdings,
                },
            }
        )

    return remaining_positions, externally_closed


def _compute_equity(balance_usd: float, open_positions: list[dict[str, Any]], mark_prices: dict[str, float]) -> float:
    open_value = 0.0
    for position in open_positions:
        amount = float(position.get("amount") or 0.0)
        entry = float(position.get("entry_price") or 0.0)
        side = position.get("side")
        symbol = position.get("symbol")
        mark_price = float(mark_prices.get(symbol, entry) or entry)
        effective_amount = amount
        if side == "buy" and position.get("mode") == "live":
            holdings = position.get("reconciled_holdings") or {}
            held_total = float(holdings.get("total") or 0.0)
            if holdings:
                effective_amount = max(0.0, min(amount, held_total))
        if side == "buy":
            open_value += effective_amount * mark_price
        elif side == "sell":
            open_value += amount * entry + (entry - mark_price) * amount
        else:
            open_value += effective_amount * mark_price
    if any(p.get("side") == "buy" for p in open_positions):
        # We already deducted USDT to open the buy, so equity = remaining USDT + asset value
        return float(balance_usd) + open_value
    return float(balance_usd) + open_value


def _should_reset_high_water_mark(
    open_positions: list[dict[str, Any]],
    newly_closed_trades: list[dict[str, Any]],
) -> bool:
    if open_positions:
        return False
    return any(str(trade.get("exit_reason") or "") == "external_reconcile" for trade in newly_closed_trades)


def _update_high_water_mark(
    equity_history: list[dict[str, Any]],
    equity_usd: float,
    timestamp: str,
    *,
    reset_hwm: bool = False,
) -> tuple[float, list[dict[str, Any]]]:
    previous_hwm = 0.0
    if not reset_hwm:
        for item in equity_history:
            try:
                previous_hwm = max(previous_hwm, float(item.get("high_water_mark", 0.0)))
            except (TypeError, ValueError):
                continue
    new_hwm = equity_usd if reset_hwm else max(previous_hwm, equity_usd)
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


def _has_recoverable_live_holdings(settings: Settings, client: BinanceDataClient) -> bool:
    if settings.dry_run or not settings.binance_api_key:
        return False

    for symbol in settings.target_symbols:
        try:
            holdings = client.fetch_asset_balance(symbol=symbol)
            mark_price = client.fetch_ticker_price(symbol)
        except BinanceClientError:
            continue

        total = float(holdings.get("total") or 0.0)
        if total * mark_price >= settings.minimum_trade_usdt * 0.5:
            return True

    return False


def _recover_unmanaged_exchange_positions(
    open_positions: list[dict[str, Any]],
    settings: Settings,
    client: BinanceDataClient,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recovery_report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if settings.dry_run else "live",
        "tracked_symbols": sorted({str(position.get("symbol") or "") for position in open_positions if position.get("symbol")}),
        "recovered_positions": [],
        "skipped_symbols": [],
    }
    if settings.dry_run or not settings.binance_api_key:
        recovery_report["status"] = "skipped"
        recovery_report["reason"] = "dry_run_or_missing_api_keys"
        recovery_report["recovered_count"] = 0
        return open_positions, recovery_report

    tracked_symbols = {str(position.get("symbol") or "") for position in open_positions}
    recovered_positions = list(open_positions)

    for symbol in settings.target_symbols:
        if symbol in tracked_symbols:
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": "already_tracked"})
            continue

        try:
            holdings = client.fetch_asset_balance(symbol=symbol)
            mark_price = client.fetch_ticker_price(symbol)
        except BinanceClientError as exc:
            logger.warning("No se pudo leer holdings live para %s: %s", symbol, exc)
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": f"holdings_unavailable: {exc}"})
            continue

        total = float(holdings.get("total") or 0.0)
        if total * mark_price < settings.minimum_trade_usdt * 0.5:
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": "holdings_below_threshold"})
            continue

        try:
            trades = client.fetch_recent_trades(symbol, limit=20)
        except BinanceClientError as exc:
            logger.warning("No se pudo leer trades recientes para %s: %s", symbol, exc)
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": f"recent_trades_unavailable: {exc}"})
            continue

        latest_buy = next((trade for trade in reversed(trades) if str(trade.get("side") or "").lower() == "buy"), None)
        if latest_buy is None:
            logger.warning("Holdings detectados en %s pero sin buy reciente para reconstruir posicion.", symbol)
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": "no_recent_buy"})
            continue

        entry_price = float(latest_buy.get("price") or 0.0)
        if entry_price <= 0:
            logger.warning("Trade reciente en %s sin precio valido; no se reconstruye posicion.", symbol)
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": "invalid_recent_buy_price"})
            continue

        opened_at = latest_buy.get("datetime") or latest_buy.get("timestamp") or datetime.now(timezone.utc).isoformat()
        amount = round(total, 8)
        protections = RiskManager(settings).build_protection_levels(entry_price, "buy")
        recovered = {
            "opened_at": str(opened_at),
            "symbol": symbol,
            "side": "buy",
            "amount": amount,
            "entry_price": round(entry_price, 4),
            "notional_usdt": round(amount * entry_price, 4),
            "stop_loss": round(protections["stop_loss"], 4),
            "take_profit": round(protections["take_profit"], 4),
            "status": "open",
            "mode": "live",
            "reconciled_holdings": holdings,
            "scenario": "recovered_live",
            "entry_rsi": None,
            "ai_confidence": None,
            "signal_price": round(entry_price, 4),
            "fill_price": round(entry_price, 4),
            "slippage_pct": 0.0,
            "trailing_tier": 0,
            "mae_pct": 0.0,
            "mfe_pct": 0.0,
            "mae_usdt": 0.0,
            "mfe_usdt": 0.0,
        }
        logger.warning("Posicion live recuperada desde exchange para %s: %s", symbol, recovered)
        recovered_positions.append(recovered)
        recovery_report["recovered_positions"].append(
            {
                "symbol": symbol,
                "amount": amount,
                "entry_price": round(entry_price, 4),
                "opened_at": str(opened_at),
                "source": "exchange_recent_buy",
            }
        )

    recovery_report["status"] = "ok"
    recovery_report["recovered_count"] = len(recovery_report["recovered_positions"])
    return recovered_positions, recovery_report


def _clear_stale_preflight_pause_reason(settings: Settings) -> None:
    control = load_state(settings.control_file)
    if control.get("desired_state") != "paused":
        return
    if control.get("updated_by") != "kill_switch":
        return

    reason = str(control.get("reason") or "")
    if not reason.startswith("Pre-flight:"):
        return

    persist_state(
        settings.control_file,
        {
            **control,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "Pausa remota activa",
        },
    )


def _clear_stale_kill_switch_stop(settings: Settings) -> None:
    control = load_state(settings.control_file)
    if control.get("desired_state") != "stopped":
        return
    if control.get("updated_by") != "kill_switch":
        return

    reason = str(control.get("reason") or "")
    if not reason.startswith("Kill Switch:"):
        return

    previous_state = load_state(settings.state_file)
    previous_risk = previous_state.get("risk") or {}
    persisted_open_positions = load_history(settings.open_positions_file)
    persisted_drawdown = float(previous_risk.get("drawdown_pct") or 0.0)

    if persisted_drawdown >= settings.kill_switch_drawdown:
        return
    if previous_state.get("open_positions") or persisted_open_positions:
        return

    if previous_state:
        sanitized_risk = {
            **previous_risk,
            "kill_switch_triggered": False,
        }
        persist_state(
            settings.state_file,
            {
                **previous_state,
                "risk": sanitized_risk,
            },
        )

    persist_state(
        settings.control_file,
        {
            **control,
            "desired_state": "running",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "Recuperado automaticamente tras kill switch persistido sin riesgo activo.",
        },
    )


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

    egress = client.detect_egress_ip()
    checks["egress"] = egress
    egress_ip = str(egress.get("egress_ip") or "").strip()
    whitelist_match = (not settings.binance_whitelist_ips) or (egress_ip in settings.binance_whitelist_ips)
    checks["whitelist_match"] = whitelist_match
    checks["whitelist_expected"] = list(settings.binance_whitelist_ips)
    logger.info(
        "Egress check: ip=%s proxy_configured=%s whitelist_match=%s expected=%s source=%s error=%s",
        egress_ip,
        egress.get("proxy_url_configured"),
        whitelist_match,
        ",".join(settings.binance_whitelist_ips) if settings.binance_whitelist_ips else "n/a",
        egress.get("source"),
        egress.get("error"),
    )

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
        category = getattr(exc, "category", "exchange_other")
        checks["error_class"] = category
        if category == "auth_invalid":
            ip_seen = egress_ip or "desconocido"
            if settings.binance_whitelist_ips and egress_ip and not whitelist_match:
                checks["detail"] = (
                    f"AUTH INVALIDA Binance (-2015). IP egress vista: {ip_seen}, "
                    f"pero whitelist esperada: {', '.join(settings.binance_whitelist_ips)}. "
                    "Diagnostico: el proxy esta configurado pero la salida efectiva no coincide con la whitelist. "
                    "Accion: corrige el routing del proxy o agrega la IP egress real a Binance."
                )
            else:
                checks["detail"] = (
                    f"AUTH INVALIDA Binance (-2015). IP egress vista: {ip_seen}. "
                    f"Proxy configurado: {egress.get('proxy_url_configured')}. "
                    "Accion: a\u00f1ade esa IP al whitelist de la API key en Binance, "
                    "verifica permisos de Spot Trading y que BINANCE_PROXY_URL en Coolify "
                    "siga apuntando al proxy correcto."
                )
        else:
            checks["detail"] = f"binance: {exc}"
        return checks

    if not checks["balance_ok"]:
        if _has_recoverable_live_holdings(settings, client):
            checks["ok"] = True
            checks["detail"] = "Pre-flight OK: holdings live detectados; se permite gestion de posicion abierta."
            return checks
        checks["detail"] = (
            f"Saldo USDT {checks['balance_usdt']} < requerido {checks['minimum_required']}"
        )
        return checks

    checks["ok"] = True
    checks["detail"] = "Pre-flight OK para operar live."
    return checks


def _is_degradable_preflight_failure(pre_flight: dict[str, Any]) -> bool:
    detail = str(pre_flight.get("detail") or "")
    if pre_flight.get("ok"):
        return False
    if pre_flight.get("error_class") == "auth_invalid":
        return True
    return detail.startswith("binance:")


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


def _summarize_open_position(open_positions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Estructura compacta para exponer la posicion activa al dashboard."""
    if not open_positions:
        return None
    p = open_positions[0]
    return {
        "symbol": p.get("symbol"),
        "side": p.get("side"),
        "scenario": p.get("scenario"),
        "entry_price": p.get("entry_price"),
        "stop_loss": p.get("stop_loss"),
        "take_profit": p.get("take_profit"),
        "amount": p.get("amount"),
        "notional_usdt": p.get("notional_usdt"),
        "opened_at": p.get("opened_at"),
        "mae_pct": p.get("mae_pct"),
        "mfe_pct": p.get("mfe_pct"),
        "mae_usdt": p.get("mae_usdt"),
        "mfe_usdt": p.get("mfe_usdt"),
        "unrealized_pnl_usdt": p.get("unrealized_pnl_usdt"),
        "mark_price": p.get("mark_price"),
        "ai_confidence": p.get("ai_confidence"),
        "signal_price": p.get("signal_price"),
        "fill_price": p.get("fill_price"),
        "slippage_pct": p.get("slippage_pct"),
        "trailing_tier": p.get("trailing_tier", 0),
        "algo_version": p.get("algo_version"),
    }


def _describe_global_lock(open_positions: list[dict[str, Any]]) -> str:
    if not open_positions:
        return "mutex global inactivo"

    position = open_positions[0]
    symbol = position.get("symbol") or "n/d"
    side = str(position.get("side") or "n/d").upper()
    entry_price = float(position.get("entry_price") or 0.0)
    mark_price = float(position.get("mark_price") or 0.0)
    unrealized_pnl = float(position.get("unrealized_pnl_usdt") or 0.0)
    return (
        f"mutex global activo por {symbol} {side} | "
        f"entry={entry_price:.4f} mark={mark_price:.4f} pnl={unrealized_pnl:.4f} USDT"
    )


def _scan_symbol(
    symbol: str,
    settings: Settings,
    client: BinanceDataClient,
    order_history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Lee OHLCV y construye se\u00f1al tecnica para un simbolo concreto."""
    raw_frame = client.fetch_ohlcv(limit=200, symbol=symbol)
    enriched_frame = compute_indicators(raw_frame)
    technical_signal = build_technical_signal(enriched_frame, settings)
    candidate, candidate_reason = _is_pre_signal_candidate(settings, technical_signal)
    return {
        "symbol": symbol,
        "frame": enriched_frame,
        "latest_candle": enriched_frame.iloc[-1].to_dict(),
        "technical_signal": technical_signal,
        "candidate": candidate,
        "candidate_reason": candidate_reason,
    }


def run_cycle() -> None:
    settings = load_settings()
    logger = setup_logger(settings)
    notifier = _build_notifier(settings, logger)
    client = BinanceDataClient(settings)
    risk_manager = RiskManager(settings)
    executor = TradeExecutor(settings, client, risk_manager, logger)
    ai_analyzer = OpenRouterAnalyzer(settings, logger)

    target_symbols = list(settings.target_symbols) or [settings.trading_symbol]
    logger.info(
        "Iniciando ciclo multi-ticker (%s) en %s. MAX_OPEN=%s",
        ", ".join(target_symbols),
        settings.timeframe,
        settings.max_global_open_positions,
    )
    write_heartbeat("online", "Ciclo iniciado")
    recovery_report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "not_run",
        "recovered_count": 0,
        "recovered_positions": [],
        "tracked_symbols": [],
        "skipped_symbols": [],
    }
    open_positions: list[dict[str, Any]] = []

    try:
        previous_state = load_state(settings.state_file)
        order_history = load_history(settings.order_history_file)
        open_positions = load_history(settings.open_positions_file)
        closed_trades = load_history(settings.closed_trades_file)
        equity_history = load_history(settings.equity_history_file)
        live_mode = not settings.dry_run
        open_positions, recovery_report = _recover_unmanaged_exchange_positions(open_positions, settings, client, logger)
        persist_history(settings.open_positions_file, open_positions)
        persist_state(settings.logs_dir / "recovery_status.json", recovery_report)

        # 1) Escaneo SECUENCIAL multi-ticker (rate-limit safe gracias a enableRateLimit de ccxt).
        scan_results: list[dict[str, Any]] = []
        for index, symbol in enumerate(target_symbols):
            try:
                scan_results.append(_scan_symbol(symbol, settings, client, order_history))
            except BinanceClientError as exc:
                logger.error("Fallo OHLCV para %s: %s", symbol, exc)
                scan_results.append({
                    "symbol": symbol,
                    "frame": None,
                    "latest_candle": {},
                    "technical_signal": {"signal": "hold", "scenario": None, "rsi": 0.0, "close": 0.0, "atr_pct": 0.0, "volume_ratio": 0.0},
                    "candidate": False,
                    "candidate_reason": f"OHLCV error: {exc}",
                })
            if index < len(target_symbols) - 1 and settings.symbol_scan_pause_seconds > 0:
                time.sleep(settings.symbol_scan_pause_seconds)

        candles_by_symbol = {s["symbol"]: s["latest_candle"] for s in scan_results if s.get("latest_candle")}

        # 2) Liquidacion de posiciones abiertas con la vela mas reciente de su simbolo.
        async def _persist_open_positions_async(snapshot: list[dict[str, Any]]) -> None:
            await asyncio.to_thread(persist_history, settings.open_positions_file, snapshot)

        open_positions, newly_closed_trades = asyncio.run(
            _settle_open_positions(
                open_positions,
                candles_by_symbol,
                settings=settings,
                live_mode=live_mode,
                executor=executor,
                logger=logger,
                persist_open_positions=_persist_open_positions_async,
            )
        )
        if live_mode and open_positions:
            open_positions, externally_closed_trades = _reconcile_live_open_positions(
                open_positions,
                candles_by_symbol,
                client=client,
                logger=logger,
            )
            if externally_closed_trades:
                newly_closed_trades.extend(externally_closed_trades)
        if newly_closed_trades:
            closed_trades.extend(newly_closed_trades)
            persist_history(settings.closed_trades_file, closed_trades)
            if live_mode:
                for closed_trade in newly_closed_trades:
                    _notify_safe(notifier, logger, "trade", _format_trade_close_message(closed_trade))
        persist_history(settings.open_positions_file, open_positions)

        # 3) MUTEX GLOBAL: si hay posicion activa en cualquier ticker, no abrimos otra.
        global_lock = len(open_positions) >= settings.max_global_open_positions
        active_symbol = open_positions[0].get("symbol") if open_positions else None

        # 4) Saldo + holdings.
        try:
            balance_usd = client.fetch_balance_usd()
        except BinanceClientError as exc:
            logger.error("No se pudo obtener saldo: %s", exc)
            balance_error_class = getattr(exc, "category", "exchange_other")
            if balance_error_class == "auth_invalid":
                heartbeat_detail = (
                    f"AUTH INVALIDA Binance (-2015): {exc}. "
                    "Acci\u00f3n: revisa whitelist IP, permisos Spot Trading "
                    "y BINANCE_PROXY_URL en Coolify."
                )
                _maybe_notify_auth_invalid(settings, logger, str(exc))
            else:
                heartbeat_detail = f"Saldo no disponible: {exc}"
            degraded_scans = [
                _build_scan_summary(s, settings, blocked_by_lock=global_lock and s["symbol"] != active_symbol)
                for s in scan_results
            ]
            degraded_risk = _degraded_risk_snapshot(previous_state, balance_error=str(exc))
            degraded_portfolio = _build_portfolio_summary(
                settings,
                degraded_risk,
                open_positions,
                closed_trades,
                previous_state.get("portfolio", {}).get("asset_holdings") or {},
                equity_history,
            )
            degraded_decision = {
                "action": "hold",
                "reason": "Infra degradada: balance Binance no disponible.",
                "status": "degraded_balance",
                "global_lock": global_lock,
                "active_symbol": active_symbol,
                "infra_error": str(exc),
                "infra_error_class": balance_error_class,
            }
            append_history(
                settings.scan_history_file,
                _build_scan_history_event(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    scans=degraded_scans,
                    ai_signal=dict(AI_NOT_CONSULTED),
                    ai_consulted_symbol=None,
                    global_lock=global_lock,
                    active_symbol=active_symbol,
                    decision=degraded_decision,
                    balance_ok=False,
                    balance_error=str(exc),
                    balance_error_class=balance_error_class,
                ),
                limit=1440,
            )
            persist_state(
                settings.state_file,
                build_state_snapshot(
                    market=_serialize_market(scan_results[0]["frame"]) if scan_results and scan_results[0].get("frame") is not None else [],
                    technical_signal=(scan_results[0]["technical_signal"] if scan_results else {"signal": "hold"}),
                    ai_signal=dict(AI_NOT_CONSULTED),
                    risk=asdict(degraded_risk),
                    portfolio=degraded_portfolio,
                    decision=degraded_decision,
                    order_history=order_history,
                    open_positions=open_positions,
                    closed_trades=closed_trades,
                    signal_history=load_history(settings.signal_history_file),
                    open_position=_summarize_open_position(open_positions),
                    last_scans=degraded_scans,
                    target_symbols=target_symbols,
                    global_lock=global_lock,
                    active_symbol=active_symbol,
                    recovery=recovery_report,
                ),
            )
            write_heartbeat("degraded", heartbeat_detail)
            return

        asset_holdings: dict[str, Any] = {}
        if live_mode and settings.binance_api_key and active_symbol:
            try:
                asset_holdings = client.fetch_asset_balance(symbol=active_symbol)
                for position in open_positions:
                    if position.get("symbol") == active_symbol and position.get("mode") == "live":
                        position["reconciled_holdings"] = asset_holdings
            except BinanceClientError as exc:
                logger.error("No se pudo leer holdings: %s", exc)
                asset_holdings = {"asset": client.base_asset_for(active_symbol), "free": 0.0, "used": 0.0, "total": 0.0, "error": str(exc)}

        mark_prices = {sym: float(c.get("close") or 0.0) for sym, c in candles_by_symbol.items()}
        equity_usd = _compute_equity(balance_usd, open_positions, mark_prices)
        reset_hwm = _should_reset_high_water_mark(open_positions, newly_closed_trades)
        new_hwm, equity_history = _update_high_water_mark(
            equity_history,
            equity_usd,
            datetime.now(timezone.utc).isoformat(),
            reset_hwm=reset_hwm,
        )
        persist_history(settings.equity_history_file, equity_history)

        risk_snapshot = risk_manager.evaluate(balance_usd, equity_usd=equity_usd, high_water_mark=new_hwm)

        # 5) Kill switch global.
        if risk_snapshot.kill_switch_triggered:
            decision = {
                "action": "halt",
                "reason": "Kill Switch activado por drawdown",
                "drawdown_pct": risk_snapshot.drawdown_pct,
            }
            logger.critical(
                "Kill Switch activado. Drawdown %.2f%% excede el limite %.2f%%.",
                risk_snapshot.drawdown_pct * 100,
                settings.kill_switch_drawdown * 100,
            )
            _set_control_state(
                settings,
                "stopped",
                f"Kill Switch: drawdown {risk_snapshot.drawdown_pct:.4f} >= {settings.kill_switch_drawdown}",
            )
            primary = scan_results[0] if scan_results else {"technical_signal": {"signal": "hold"}, "frame": None}
            ai_signal_dummy = dict(AI_NOT_CONSULTED)
            append_history(
                settings.signal_history_file,
                _build_signal_event(primary["technical_signal"], ai_signal_dummy, decision),
            )
            persist_state(
                settings.state_file,
                build_state_snapshot(
                    market=_serialize_market(primary["frame"]) if primary.get("frame") is not None else [],
                    technical_signal=primary["technical_signal"],
                    ai_signal=ai_signal_dummy,
                    risk=asdict(risk_snapshot),
                    portfolio=_build_portfolio_summary(settings, risk_snapshot, open_positions, closed_trades, asset_holdings, equity_history),
                    decision=decision,
                    order_history=order_history,
                    open_positions=open_positions,
                    closed_trades=closed_trades,
                    signal_history=load_history(settings.signal_history_file),
                    open_position=_summarize_open_position(open_positions),
                    last_scans=[_build_scan_summary(s, settings, blocked_by_lock=False) for s in scan_results],
                    target_symbols=target_symbols,
                    global_lock=global_lock,
                    active_symbol=active_symbol,
                    recovery=recovery_report,
                ),
            )
            write_heartbeat("offline", "Kill Switch activado")
            _notify_safe(
                notifier,
                logger,
                "critical",
                {
                    "title": "Drawdown",
                    "detail": f"Max DD cruzo {risk_snapshot.drawdown_pct * 100:.2f}%",
                    "status": "apagado",
                },
            )
            return

        # 6) Carrera de senales: el primer simbolo que cumpla TODOS los guardarrailes dispara orden.
        ai_signal: dict[str, Any] = dict(AI_NOT_CONSULTED)
        ai_consulted_symbol: str | None = None
        chosen_scan: dict[str, Any] | None = None
        chosen_guardrails: dict[str, Any] | None = None
        decision: dict[str, Any] = {
            "action": "hold",
            "reason": "global_lock" if global_lock else "Sin senal valida en ningun ticker.",
            "global_lock": global_lock,
            "active_symbol": active_symbol,
        }

        if not global_lock:
            for scan in scan_results:
                technical_signal = scan["technical_signal"]
                # Pre-gate barato (sin tocar IA).
                pre_guards = _build_guardrails(settings, technical_signal, AI_NOT_CONSULTED, order_history)
                if not (pre_guards["executable_signal"] and pre_guards["volatility_ready"] and pre_guards["volume_ready"] and not pre_guards["cooldown_active"]):
                    continue

                # Consulta IA solo para este candidato; cache valida para todo el ciclo.
                if not ai_signal.get("consulted"):
                    cached = _load_recent_ai_signal(settings)
                    if cached is not None:
                        ai_signal = cached
                        logger.info(
                            "Reutilizando senal de IA en cache (%ss de antiguedad).",
                            ai_signal.get("cached_age_seconds", 0),
                        )
                    else:
                        ai_signal = ai_analyzer.analyze(scan["frame"], symbol=scan["symbol"])
                        ai_signal.setdefault("consulted", True)
                    ai_consulted_symbol = scan["symbol"]

                guardrails = _build_guardrails(settings, technical_signal, ai_signal, order_history)
                if all([
                    guardrails["executable_signal"],
                    guardrails["ai_confident"],
                    guardrails["same_direction"],
                    guardrails["ai_approved"],
                    guardrails["ai_alignment"],
                    guardrails["ai_setup_ready"],
                    guardrails["ai_risk_clear"],
                    guardrails["volatility_ready"],
                    guardrails["volume_ready"],
                    not guardrails["cooldown_active"],
                    str(technical_signal["signal"]) == "buy",
                    ai_signal.get("consulted", False),
                ]):
                    chosen_scan = scan
                    chosen_guardrails = guardrails
                    break

        if chosen_scan and chosen_guardrails:
            symbol = chosen_scan["symbol"]
            technical_signal = chosen_scan["technical_signal"]
            decision = executor.execute(
                side=str(technical_signal["signal"]),
                market_price=float(technical_signal["close"]),
                risk=risk_snapshot,
                symbol=symbol,
            )
            decision["scenario"] = technical_signal.get("scenario")
            decision["entry_rsi"] = technical_signal.get("rsi")
            decision["symbol"] = symbol
            append_history(settings.order_history_file, decision)
            order_history = load_history(settings.order_history_file)

            if decision.get("status") in {"error", "reconcile_failed"} and live_mode:
                _set_control_state(
                    settings,
                    "stopped",
                    f"Kill Switch ejecucion: {decision.get('reason', 'fallo de exchange')}",
                )
                logger.critical("Ejecucion fallo en live; bot detenido. %s", decision)
                _notify_safe(
                    notifier,
                    logger,
                    "critical",
                    {
                        "title": "API Reject",
                        "detail": f"{decision.get('reason', 'fallo de exchange')} | {decision.get('symbol', 'n/d')}",
                        "status": "apagado",
                    },
                )

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
                        "scenario": technical_signal.get("scenario"),
                        "entry_rsi": technical_signal.get("rsi"),
                        "ai_confidence": ai_signal.get("confidence"),
                        "signal_price": decision.get("signal_price"),
                        "fill_price": decision.get("fill_price") or decision.get("price"),
                        "slippage_pct": decision.get("slippage_pct", 0.0),
                        "trailing_tier": 0,
                        "algo_version": ALGO_VERSION,
                        "mae_pct": 0.0,
                        "mfe_pct": 0.0,
                        "mae_usdt": 0.0,
                        "mfe_usdt": 0.0,
                    }
                )
                persist_history(settings.open_positions_file, open_positions)
                global_lock = True
                active_symbol = symbol
                if live_mode and decision.get("status") == "submitted":
                    _notify_safe(notifier, logger, "trade", _format_trade_open_message(decision))
        else:
            # No hubo entrada: usamos el primer scan como referencia de la decision.
            primary_scan = scan_results[0] if scan_results else None
            primary_signal = primary_scan["technical_signal"] if primary_scan else {"signal": "hold"}
            primary_guards = _build_guardrails(settings, primary_signal, ai_signal, order_history) if primary_scan else {}
            lock_detail = _describe_global_lock(open_positions) if global_lock else "guardarrailes sin match"
            decision = {
                "action": "hold",
                "reason": "Posicion abierta (mutex global)" if global_lock else "Ningun ticker cumplio los guardarrailes.",
                "detail": lock_detail,
                "global_lock": global_lock,
                "active_symbol": active_symbol,
                "ai_consulted": ai_signal.get("consulted", False),
                "ai_confidence": ai_signal.get("confidence", 0.0),
                **primary_guards,
            }
            logger.info(
                "Sin operacion en este ciclo. lock=%s active=%s detail=%s scans=%s",
                global_lock,
                active_symbol,
                lock_detail,
                [(s["symbol"], s["technical_signal"].get("scenario"), s["candidate_reason"]) for s in scan_results],
            )

        scan_summaries = [
            _build_scan_summary(
                {
                    **scan,
                    "ia_consulted": ai_consulted_symbol == scan["symbol"] and ai_signal.get("consulted", False),
                    "ia_confidence": ai_signal.get("confidence", 0.0) if ai_consulted_symbol == scan["symbol"] else 0.0,
                },
                settings,
                blocked_by_lock=global_lock and scan["symbol"] != active_symbol,
            )
            for scan in scan_results
        ]

        # 7) Persistencia final con telemetria multi-ticker.
        primary_scan = chosen_scan or (scan_results[0] if scan_results else None)
        primary_signal = primary_scan["technical_signal"] if primary_scan else {"signal": "hold"}
        primary_frame = primary_scan["frame"] if primary_scan else None
        if primary_scan is not None and primary_scan.get("symbol"):
            primary_signal = {**primary_signal, "symbol": primary_scan["symbol"]}

        append_history(settings.signal_history_file, _build_signal_event(primary_signal, ai_signal, decision))
        append_history(
            settings.scan_history_file,
            _build_scan_history_event(
                timestamp=datetime.now(timezone.utc).isoformat(),
                scans=scan_summaries,
                ai_signal=ai_signal,
                ai_consulted_symbol=ai_consulted_symbol,
                global_lock=global_lock,
                active_symbol=active_symbol,
                decision=decision,
                balance_ok=True,
            ),
            limit=1440,
        )

        persist_state(
            settings.state_file,
            build_state_snapshot(
                market=_serialize_market(primary_frame) if primary_frame is not None else [],
                technical_signal=primary_signal,
                ai_signal=ai_signal,
                risk=asdict(risk_snapshot),
                portfolio=_build_portfolio_summary(settings, risk_snapshot, open_positions, closed_trades, asset_holdings, equity_history),
                decision=decision,
                order_history=order_history,
                open_positions=open_positions,
                closed_trades=closed_trades,
                signal_history=load_history(settings.signal_history_file),
                open_position=_summarize_open_position(open_positions),
                last_scans=scan_summaries,
                target_symbols=target_symbols,
                global_lock=global_lock,
                active_symbol=active_symbol,
                recovery=recovery_report,
            ),
        )
        heartbeat_detail = decision.get("detail") or decision.get("reason") or "Ciclo completado"
        write_heartbeat("online", str(heartbeat_detail))

    except BinanceClientError as exc:
        error_category = getattr(exc, "category", "exchange_other")
        transient_categories = {"network_local", "timeout_binance", "rate_limit"}
        if error_category in transient_categories:
            write_heartbeat("degraded", f"Binance transitorio ({error_category}): {exc}")
            logger.exception("Error de Binance recuperable (%s): %s", error_category, exc)
            _notify_safe(
                _build_notifier(settings, logger),
                logger,
                "sys",
                {"title": "BINANCE DEGRADADO", "detail": f"{error_category}: {exc}"},
            )
            return

        write_heartbeat("offline", f"Binance error: {exc}")
        logger.exception("Error de Binance: %s", exc)
        if not settings.dry_run:
            _set_control_state(settings, "stopped", f"Kill Switch infra: {exc}")
            _notify_safe(_build_notifier(settings, logger), logger, "critical", {"title": "API Reject", "detail": str(exc), "status": "apagado"})
    except RequestException as exc:
        write_heartbeat("offline", f"Error de red: {exc}")
        logger.exception("Error de red en OpenRouter o Binance: %s", exc)
        _notify_safe(_build_notifier(settings, logger), logger, "sys", {"title": "ALERTA RED", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001
        write_heartbeat("offline", f"Fallo inesperado: {exc}")
        logger.exception("Fallo inesperado en el ciclo principal: %s", exc)
        _notify_safe(_build_notifier(settings, logger), logger, "critical", {"title": "Loss of Containment", "detail": str(exc), "status": "apagado"})


def main() -> None:
    settings = load_settings()
    logger = setup_logger(settings)
    notifier = _build_notifier(settings, logger)
    ensure_control_file()
    logger.info("OptiFerre-Trader iniciado. DRY_RUN=%s", settings.dry_run)

    client = BinanceDataClient(settings)
    pre_flight = pre_flight_check(settings, client, logger)
    persist_state(
        settings.logs_dir / "pre_flight.json",
        {"timestamp": datetime.now(timezone.utc).isoformat(), **pre_flight},
    )
    if not pre_flight["ok"]:
        if _is_degradable_preflight_failure(pre_flight):
            logger.error("Pre-flight degradado: %s. El bot seguira en modo observacional.", pre_flight["detail"])
            write_heartbeat("degraded", f"Pre-flight degradado: {pre_flight['detail']}")
            _notify_safe(notifier, logger, "sys", {"title": "ALERTA RED", "detail": pre_flight["detail"]})
        else:
            logger.critical("Pre-flight fall\u00f3: %s. Forzando pausa.", pre_flight["detail"])
            _set_control_state(settings, "paused", f"Pre-flight: {pre_flight['detail']}")
            write_heartbeat("paused", f"Pre-flight: {pre_flight['detail']}")
            _notify_safe(notifier, logger, "critical", {"title": "Pre-flight", "detail": pre_flight["detail"], "status": "apagado"})
    else:
        logger.info("Pre-flight OK: %s", pre_flight["detail"])
        _clear_stale_preflight_pause_reason(settings)
        _clear_stale_kill_switch_stop(settings)
        _notify_safe(
            notifier,
            logger,
            "sys",
            {
                "title": "BOT READY",
                "cycle_seconds": 0.0,
                "detail": f"Balance {float(pre_flight.get('balance_usdt', 0.0)):.2f} USDT",
            },
        )

    while True:
        control = load_state(settings.control_file)
        desired_state = control.get("desired_state", "running")

        if desired_state == "paused":
            logger.info("Bot en pausa por control remoto. Esperando reanudaci\u00f3n.")
            write_heartbeat("paused", control.get("reason", "Pausa remota activa"))
            time.sleep(max(5, settings.poll_interval_seconds))
            continue

        if desired_state == "stopped":
            logger.warning("Bot detenido por control remoto. Queda en espera hasta nueva orden.")
            write_heartbeat("offline", control.get("reason", "Detenci\u00f3n remota"))
            time.sleep(max(5, settings.poll_interval_seconds))
            continue

        cycle_start = time.monotonic()
        run_cycle()
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1, settings.poll_interval_seconds - int(elapsed))
        logger.info("Ciclo completado. Pr\u00f3xima evaluaci\u00f3n en %s segundos.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()