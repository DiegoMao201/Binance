from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import ccxt
from requests import RequestException

from src.analysis.ai_client import OpenRouterAnalyzer
from src.analysis.indicators import build_technical_signal, compute_indicators
from src.analysis.trade_monitor import ActiveTradeMonitor
from src.data.binance_client import BinanceClientError, BinanceDataClient
from src.execution.trader import TradeExecutor
from src.safety.risk_manager import RiskManager, RiskSnapshot
from src.utils.config import Settings, load_settings
from src.utils.logger import setup_logger
from src.utils.market_profiles import (
    apply_market_profile,
    get_market_cadence,
    market_profiles_summary,
    sort_symbols_by_priority,
)
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


_PAMM_WEBHOOK_TIMEOUT_S = 10  # seconds — bot must not be blocked by Next.js latency


def _fire_pamm_webhook(
    closed_trade: dict[str, Any],
    settings: "Settings",
    logger: logging.Logger,
) -> None:
    """Fire-and-forget PAMM allocation webhook to the Next.js portal.

    Sends the closed trade's PnL to /api/webhooks/trade-closed so the portal
    distributes the return across all active client balances, charges the 5%
    performance fee + Binance commission, and writes immutable allocation rows.

    Rules:
      - Silently skipped in dry_run mode (no balance changes on paper trades).
      - Silently skipped when PAMM_WEBHOOK_URL or WEBHOOK_SECRET are not set
        (e.g. during local development without a portal).
      - All exceptions are caught and logged as warnings — a webhook failure
        must never take the bot down.
    """
    if getattr(settings, "dry_run", True):
        logger.debug("[PAMM] dry_run — webhook skipped for %s", closed_trade.get("symbol"))
        return

    webhook_url    = getattr(settings, "pamm_webhook_url", "").strip()
    webhook_secret = getattr(settings, "webhook_secret", "").strip()
    if not webhook_url or not webhook_secret:
        logger.debug("[PAMM] PAMM_WEBHOOK_URL / WEBHOOK_SECRET no configurados — omitiendo webhook.")
        return

    symbol      = closed_trade.get("symbol", "UNKNOWN")
    pnl_pct     = float(closed_trade.get("pnl_pct") or 0.0)
    side        = str(closed_trade.get("side") or "BUY").upper()
    exit_reason = str(closed_trade.get("exit_reason") or "unknown")
    fees_usdt   = float(closed_trade.get("fees_usdt") or 0.0)

    # Approximate Binance commission as a fraction of position notional.
    # notional = entry_price × amount. Falls back to the standard 0.1% fee.
    entry_price = float(
        closed_trade.get("entry_price") or closed_trade.get("fill_price") or 0.0
    )
    amount   = float(closed_trade.get("amount") or 0.0)
    notional = entry_price * amount
    fees_pct = round(fees_usdt / notional, 8) if notional > 0 else 0.001

    payload = {
        "symbol":      symbol,
        "pnl_pct":     pnl_pct,
        "side":        side,
        "exit_reason": exit_reason,
        "fees_pct":    fees_pct,
    }

    try:
        import requests as _req  # noqa: PLC0415 — lazy import to avoid startup cost
        resp = _req.post(
            webhook_url,
            json=payload,
            headers={"Authorization": f"Bearer {webhook_secret}"},
            timeout=_PAMM_WEBHOOK_TIMEOUT_S,
        )
        if resp.status_code == 200:
            _data = resp.json()
            logger.info(
                "[PAMM] Webhook OK — %s pnl_pct=%.4f allocated=%d clientes.",
                symbol, pnl_pct, _data.get("allocated", 0),
            )
        else:
            logger.warning(
                "[PAMM] Webhook rechazado %d para %s: %s",
                resp.status_code, symbol, resp.text[:300],
            )
    except Exception as exc:  # noqa: BLE001
        # A network error or timeout must never crash the bot.
        logger.warning("[PAMM] Webhook error (no crítico): %s", exc)


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
_BALANCE_BACKOFF_ALERT_STATE: dict[str, float] = {"last_sent": 0.0}
# Per-symbol radar-alert cooldown (unix timestamp of last send).
# 1-hour silence per symbol: prevents alert fatigue when a coin lingers near a gate.
_RADAR_ALERT_STATE: dict[str, float] = {}
_RADAR_ALERT_COOLDOWN_SECONDS: int = 3600  # 1 hour per symbol


def _maybe_notify_balance_backoff(settings: Settings, logger: logging.Logger, failures: int) -> None:
    """Avisa por Telegram cuando el bot entra en backoff por saldo insuficiente.

    Rate-limit 30 min para no spammear si el operador esta dormido o el saldo
    sigue bajo durante un rato largo.
    """
    now = time.time()
    if now - _BALANCE_BACKOFF_ALERT_STATE["last_sent"] < 1800:
        return
    _BALANCE_BACKOFF_ALERT_STATE["last_sent"] = now
    notifier = _build_notifier(settings, logger)
    _notify_safe(
        notifier,
        logger,
        "warning",
        {
            "title": "BACKOFF SALDO INSUFICIENTE",
            "detail": (
                f"{failures} ordenes rechazadas en 15 min por insufficient_balance. "
                "Bot en pausa local. Verifica USDT libre o desactiva activos no liquidos."
            ),
            "status": "degraded",
        },
    )


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
        "side": str(decision.get("side", "BUY")).upper(),
        "entry_price": decision.get("fill_price") or decision.get("price") or 0.0,
        "stop_loss": decision.get("stop_loss", 0.0),
        "take_profit": decision.get("take_profit"),
        "notional_usdt": decision.get("notional_usdt"),
        "ai_confidence": decision.get("ai_confidence"),
        "scenario": decision.get("scenario"),
        "regime": decision.get("regime"),
        "entry_logic_tag": decision.get("entry_logic_tag", "standard_ai"),
        "ai_risk_flags": decision.get("ai_risk_flags") or [],
        "mode": decision.get("mode", "live"),
    }


def _format_trade_close_message(closed_trade: dict[str, Any]) -> dict[str, Any]:
    # Compute hold_minutes for closed trade if not already stored
    hold_minutes = closed_trade.get("hold_minutes")
    if hold_minutes is None:
        try:
            from datetime import datetime, timezone  # noqa: PLC0415
            opened_s = closed_trade.get("opened_at") or ""
            closed_s = closed_trade.get("closed_at") or ""
            if opened_s and closed_s:
                def _parse(s: str) -> datetime:
                    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
                        try:
                            return datetime.strptime(s, fmt)
                        except ValueError:
                            continue
                    raise ValueError(f"unparseable: {s}")
                delta = _parse(closed_s) - _parse(opened_s)
                hold_minutes = delta.total_seconds() / 60
        except Exception:  # noqa: BLE001
            hold_minutes = None
    return {
        "symbol": closed_trade.get("symbol", "n/d"),
        "side": "CLOSE",
        "entry_price": closed_trade.get("entry_price") or closed_trade.get("fill_price"),
        "exit_price": closed_trade.get("exit_price"),
        "stop_loss": closed_trade.get("stop_loss"),
        "pnl_usdt": closed_trade.get("pnl_usdt", 0.0),
        "pnl_pct": closed_trade.get("pnl_pct"),
        "exit_reason": closed_trade.get("exit_reason", "desconocido"),
        "hold_minutes": hold_minutes,
        "mode": closed_trade.get("mode", "live"),
    }


def _compute_scan_proximity(scan: dict[str, Any]) -> tuple[float, str, float, float]:
    """Compute the highest gate proximity across all MicroGateRadar gates.

    Returns (proximity_0_to_1, gate_name, current_value, threshold).
    Mirrors the logic in web/components/MicroGateRadar.js.
    """
    ts = scan  # scan is already a technical_signal-enriched dict

    def _pct(val: Any, threshold: float) -> float:
        try:
            return min(float(val) / threshold, 1.0) if threshold else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Per-market vol_accel threshold: WIF 0.35, DOGE 0.40, SOL 0.50, BNB/ETH 0.55, BTC 0.60
    _symbol = ts.get("symbol")
    _vol_accel_thr = float(get_market_cadence(_symbol).get("vol_accel_threshold") or 0.60)

    gates: list[tuple[str, float, float]] = [
        # (name, current_value, threshold)
        ("flow_v3",   float(ts.get("order_flow_imbalance") or 0.0),     0.40),
        ("ob_v3",     float(ts.get("orderbook_pressure") or 0.0),        0.50),
        ("vol_accel", float(ts.get("volume_acceleration") or 0.0),       _vol_accel_thr),
        ("atr_c1",    float(ts.get("atr_pct") or 0.0),                   0.003),
        ("bb_c2",     float(ts.get("bb_width_pct") or 0.0),              0.005),
        ("flow_mga",  float(ts.get("order_flow_imbalance") or 0.0),     0.55),
        ("ob_mga",    float(ts.get("orderbook_pressure") or 0.0),        0.55),
    ]

    best_name, best_val, best_thr, best_prox = "n/d", 0.0, 0.0, 0.0
    for name, val, thr in gates:
        p = _pct(val, thr)
        if p > best_prox:
            best_prox, best_name, best_val, best_thr = p, name, val, thr

    return best_prox, best_name, best_val, best_thr


def _maybe_notify_radar_alert(
    notifier: "TelegramTelemetry",
    logger: logging.Logger,
    scan_summary: dict[str, Any],
    mode: str,
) -> None:
    """Fire a Radar Alert Telegram push when a gate is >= 96% proximity.

    Threshold is intentionally strict (96%) to fire only when entry is imminent.
    Rate-limited to once per _RADAR_ALERT_COOLDOWN_SECONDS (1 h) per symbol to
    prevent alert fatigue when a coin lingers near the gate for extended periods.
    Only fires when no position is open for that symbol (global_lock is checked
    by the caller — if global_lock is True, caller skips this).
    """
    ts = scan_summary.get("technical_signal") or scan_summary
    symbol = str(scan_summary.get("symbol") or ts.get("symbol") or "n/d")
    prox, gate_name, cur_val, threshold = _compute_scan_proximity(ts)

    if prox < 0.96:
        return

    now = time.time()
    last_sent = _RADAR_ALERT_STATE.get(symbol, 0.0)
    if now - last_sent < _RADAR_ALERT_COOLDOWN_SECONDS:
        return

    _RADAR_ALERT_STATE[symbol] = now
    regime = str(ts.get("regime") or "NORMAL")
    delta_raw = threshold - cur_val
    try:
        delta_s = f"{delta_raw:.5f}"
    except Exception:  # noqa: BLE001
        delta_s = "n/d"

    _notify_safe(
        notifier,
        logger,
        "radar",
        {
            "symbol": symbol,
            "regime": regime,
            "gate": gate_name,
            "proximity_pct": round(prox * 100, 1),
            "current_value": round(cur_val, 6),
            "required_value": round(threshold, 6),
            "delta": delta_s,
            "mode": mode,
        },
    )


def write_heartbeat(status: str, detail: str | None = None) -> None:
    settings = load_settings()
    control = load_state(settings.control_file)
    payload = {
        "status": status,
        "detail": detail or "",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "symbol": settings.trading_symbol,
        "target_symbols": list(settings.target_symbols),
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


# Estados de orden que cuentan como "intento real" para efectos de cooldown / backoff.
# Antes solo se contaba `submitted` => el bot reintentaba el mismo simbolo cada minuto
# tras un rejected/error (loop ETH x4 en 6 min visto en audit 2026-05-04).
_ATTEMPT_STATUSES = {"submitted", "simulated", "rejected", "error", "reconcile_failed"}
_FAILURE_STATUSES = {"rejected", "error", "reconcile_failed"}
_INSUFFICIENT_BALANCE_FRAGMENTS = ("insufficient balance", "saldo libre insuficiente")
# Ventana corta per-symbol: tras CUALQUIER intento (exitoso o fallido) en X minutos
# evitamos disparar otro contra el mismo par y quemar fees / generar log noise.
_PER_SYMBOL_COOLDOWN_MINUTES = 3
# Backoff fuerte: si hay >=3 fallos por saldo insuficiente en los ultimos 15 min,
# bloqueamos el ciclo completo hasta que la condicion expire.
_INSUFFICIENT_BALANCE_BACKOFF_MINUTES = 15
_INSUFFICIENT_BALANCE_THRESHOLD = 3


def _parse_order_ts(order: dict[str, Any]) -> datetime | None:
    raw = order.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _is_insufficient_balance_order(order: dict[str, Any]) -> bool:
    if str(order.get("status", "")).lower() != "rejected":
        return False
    reason = str(order.get("reason", "")).lower()
    return any(fragment in reason for fragment in _INSUFFICIENT_BALANCE_FRAGMENTS)


def _is_in_cooldown(order_history: list[dict[str, Any]], cooldown_minutes: int) -> bool:
    """Cooldown global: cualquier intento reciente (exitoso o fallido) cuenta.

    Un rejected/error TAMBIEN consume cuota; no queremos martillar al exchange
    cada minuto cuando hubo un fallo. Esto colapsa el rejection-storm pattern
    visto en produccion.
    """
    if not order_history or cooldown_minutes <= 0:
        return False

    now = datetime.now(timezone.utc)
    window = cooldown_minutes * 60
    for order in reversed(order_history):
        if str(order.get("status", "")).lower() not in _ATTEMPT_STATUSES:
            continue
        ts = _parse_order_ts(order)
        if not ts:
            continue
        if (now - ts).total_seconds() < window:
            return True
        # Si el intento mas reciente ya esta fuera de la ventana, no hace falta seguir.
        return False
    return False


def _is_symbol_in_cooldown(order_history: list[dict[str, Any]], symbol: str, minutes: int = _PER_SYMBOL_COOLDOWN_MINUTES) -> bool:
    """Cooldown per-symbol: evita reintentos consecutivos sobre el mismo par."""
    if not order_history or not symbol or minutes <= 0:
        return False
    now = datetime.now(timezone.utc)
    window = minutes * 60
    for order in reversed(order_history):
        if str(order.get("symbol", "")).upper() != str(symbol).upper():
            continue
        if str(order.get("status", "")).lower() not in _ATTEMPT_STATUSES:
            continue
        ts = _parse_order_ts(order)
        if not ts:
            continue
        return (now - ts).total_seconds() < window
    return False


def _insufficient_balance_backoff_active(order_history: list[dict[str, Any]]) -> tuple[bool, int]:
    """Devuelve (activo, conteo_de_fallos) si hubo demasiados rechazos por saldo."""
    if not order_history:
        return False, 0
    now = datetime.now(timezone.utc)
    window = _INSUFFICIENT_BALANCE_BACKOFF_MINUTES * 60
    failures = 0
    for order in reversed(order_history):
        ts = _parse_order_ts(order)
        if not ts:
            continue
        if (now - ts).total_seconds() > window:
            break
        if _is_insufficient_balance_order(order):
            failures += 1
    return failures >= _INSUFFICIENT_BALANCE_THRESHOLD, failures


def _build_guardrails(
    settings,
    technical_signal: dict,
    ai_signal: dict,
    order_history: list[dict[str, Any]],
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    # ── PER-MARKET PROFILES ──────────────────────────────────────────────
    # Apply per-market entry guardrail overrides (RSI bounds, ATR range,
    # volume ratio, orderbook imbalance, trade flow, AI confidence,
    # max spread, guardrail relaxation). SL/TP/trailing remain global.
    settings = apply_market_profile(settings, symbol)
    # ── /PER-MARKET PROFILES ─────────────────────────────────────────────
    signal = technical_signal["signal"]
    scenario = technical_signal.get("scenario")
    setup_score = float(technical_signal.get("setup_score", 0.0))
    spread_pct = float(technical_signal.get("spread_pct", 0.0) or 0.0)
    orderbook_imbalance = float(technical_signal.get("orderbook_imbalance", 0.5) or 0.5)
    trade_flow_score = float(technical_signal.get("trade_flow_score", 0.5) or 0.5)
    macro_trend = str(technical_signal.get("macro_trend", "neutral")).lower()

    strong_micro = (
        spread_pct > 0
        and spread_pct <= settings.max_spread_pct
        and orderbook_imbalance >= max(settings.min_orderbook_imbalance, 0.52)
        and trade_flow_score >= max(settings.min_trade_flow_score, 0.55)
    )

    same_direction = signal == ai_signal.get("signal") if signal == "buy" else True
    ai_confidence = float(ai_signal.get("confidence", 0.0))
    ai_conf_threshold = settings.ai_confidence_threshold
    # Extraer risk_flags pronto para detectar fallback tecnico antes de calcular umbrales
    risk_flags_raw = ai_signal.get("risk_flags") or []
    risk_flags = risk_flags_raw if isinstance(risk_flags_raw, list) else [str(risk_flags_raw)]
    # Reduccion de umbral cuando la IA externa no esta disponible y el motor tecnico local evaluo
    is_technical_fallback = "technical_fallback_mode" in risk_flags
    if is_technical_fallback:
        # El fallback tecnico tiene un techo de ~0.64; reducimos el umbral para permitir el paso
        ai_conf_threshold = max(0.50, ai_conf_threshold - 0.10)
    if strong_micro:
        ai_conf_threshold = max(0.52, ai_conf_threshold - 0.05)
    ai_confident = ai_confidence >= ai_conf_threshold
    ai_approved = bool(ai_signal.get("approved", False))
    ai_alignment = ai_signal.get("direction_alignment") == "aligned"
    setup_quality = str(ai_signal.get("setup_quality", "low")).lower()

    # ── Python AI Veto Correction Layer ─────────────────────────────────────
    # Detects AI model hallucinations: the model cites correct values in its
    # rationale but still applies hard vetoes whose conditions are provably
    # NOT met by the actual technical data. Override approved/alignment/quality
    # only when we can confirm the veto trigger condition is false.
    _vol_ratio_for_corr = float(technical_signal.get("volume_ratio", 0.0))
    _ob_for_corr        = float(technical_signal.get("orderbook_imbalance", 0.0))
    _rsi_slope_for_corr = float(technical_signal.get("rsi_slope", 0.0))
    _ai_correction_applied = False
    _corrected_flags = list(risk_flags)
    # V1: macro_trend=bearish AND volume_ratio < 1.5 — if actual >= 1.5, veto invalid
    if "counter_trend_no_volume" in _corrected_flags and _vol_ratio_for_corr >= 1.5:
        _corrected_flags = [f for f in _corrected_flags if f != "counter_trend_no_volume"]
        _ai_correction_applied = True
    # V2: macro_trend=bearish AND ob_imbalance < 0.55 — if actual >= 0.55, veto invalid
    if "counter_trend_weak_book" in _corrected_flags and _ob_for_corr >= 0.55:
        _corrected_flags = [f for f in _corrected_flags if f != "counter_trend_weak_book"]
        _ai_correction_applied = True
    # V4: macro_trend=bearish AND rsi_slope <= 0 — if actual > 0, veto invalid
    if "downtrend_rsi_still_falling" in _corrected_flags and _rsi_slope_for_corr > 0:
        _corrected_flags = [f for f in _corrected_flags if f != "downtrend_rsi_still_falling"]
        _ai_correction_applied = True
    if _ai_correction_applied:
        _hard_veto_flags = {
            "counter_trend_no_volume", "counter_trend_weak_book",
            "dual_microstructure_bearish", "downtrend_rsi_still_falling", "volume_exhaustion",
        }
        _remaining_hard = [f for f in _corrected_flags if f in _hard_veto_flags]
        if not _remaining_hard:
            ai_approved   = True
            ai_alignment  = True
            setup_quality = "medium" if setup_quality == "low" else setup_quality
            risk_flags    = _corrected_flags
    # ── /Python AI Veto Correction Layer ────────────────────────────────────

    # risk_flags ya fue extraido arriba para detectar fallback_mode antes del threshold

    critical_risk_flags = {
        "openrouter_timeout",
        "orderbook_vs_signal",
        "macro_bearish_pressure",
        "momentum_breakdown",
        "illiquid_spread",
    }
    critical_flags_present = [f for f in risk_flags if str(f).lower() in critical_risk_flags]
    # Los flags internos del fallback tecnico no cuentan como riesgos externos
    internal_flags = {"openrouter_unavailable", "technical_fallback_mode", "not_consulted", "tape_weak"}
    external_flags = [f for f in risk_flags if str(f).lower() not in internal_flags]
    non_critical_flags_count = max(0, len(external_flags) - len(critical_flags_present))

    # Gate adaptativo:
    # - Escenario A: medium/high permitido; risk flags no bloquean por si solos.
    # - Escenario B: permite medium si la conviccion es alta y no hay flags criticos.
    # - Escenario C: continuacion de tendencia, exige alineacion IA + micro o calidad alta.
    # - Escenario D: cruce EMA, senal tecnica fuerte, similar a A en leniencia.
    if scenario == "A":
        ai_setup_ready = setup_quality in {"medium", "high"}
        ai_risk_clear = len(critical_flags_present) == 0
    elif scenario == "D":
        # EMA cross es senal tecnica precisa, tratamos similar a A
        ai_setup_ready = setup_quality in {"medium", "high"}
        ai_risk_clear = len(critical_flags_present) == 0
    elif scenario == "C":
        # ── 15m alignment veto for Scenario C (continuation/FOMO killer) ──
        # V3 cohort: Scenario C win rate = 16.7% because the bot was buying
        # tops into 15m downtrends. If macro_slope_pct < -0.0015 (15m EMA50
        # falling at >0.15% per 6 candles) we block the entry outright.
        macro_slope_pct = float(technical_signal.get("macro_slope_pct") or 0.0)
        if macro_slope_pct < -0.0015:
            ai_setup_ready = False
            ai_risk_clear = False
        else:
            ai_setup_ready = bool(
                setup_quality == "high"
                or (
                    setup_quality == "medium"
                    and ai_confidence >= max(ai_conf_threshold, 0.58 if is_technical_fallback else 0.60)
                    and len(critical_flags_present) == 0
                    and (strong_micro or orderbook_imbalance >= max(settings.min_orderbook_imbalance, 0.52))
                )
            )
            ai_risk_clear = len(critical_flags_present) == 0 and non_critical_flags_count <= 1
    else:
        high_ready = setup_quality == "high" and len(critical_flags_present) == 0
        fallback_medium_floor = 0.55 if is_technical_fallback else 0.62
        medium_ready = (
            setup_quality == "medium"
            and ai_confidence >= max(ai_conf_threshold, fallback_medium_floor)
            and len(critical_flags_present) == 0
            and non_critical_flags_count <= 1
        )
        if strong_micro and setup_quality == "medium" and len(critical_flags_present) == 0:
            medium_ready = True
        ai_setup_ready = high_ready or medium_ready
        ai_risk_clear = len(critical_flags_present) == 0 and non_critical_flags_count <= 1

    # Scenario A is a structured counter-trend rebound — the AI may return
    # approved=false / direction_alignment=misaligned because Veto V1 fires
    # on bearish macro + low volume_ratio. That veto is correct for random
    # counter-trend bets but WRONG for Scenario A where being counter-trend
    # is the entire point (oversold rebound from support). Bypass approved,
    # alignment AND setup_quality when: confidence >= threshold, no critical
    # risk flags. The Python technical gates (volatility_ready, score_ready,
    # spread, flow) are the real safety net for Scenario A — not the AI's
    # regime classification which can fire chop_regime for DOGE ATR=0.27%
    # even though the Python min_atr_pct for DOGE is only 0.0015.
    # Scenario B also gets this override for deep oversold with strong microstructure
    # (flow >= 0.60 AND ob >= 0.55): the AI's V5 vol_accel veto is irrelevant when
    # the orderbook and tape are already showing accumulation at RSI <= 35.
    _rsi_for_b  = float(technical_signal.get("rsi", 99.0))
    _flow_for_b = float(technical_signal.get("trade_flow_score", 0.0))
    _ob_for_b   = float(technical_signal.get("orderbook_imbalance", 0.0))
    scenario_a_override = (
        scenario == "A"
        and ai_confident
        and len(critical_flags_present) == 0
    )
    scenario_b_micro_override = (
        scenario == "B"
        and ai_confident
        and _rsi_for_b <= 35.0
        and _flow_for_b >= 0.60
        and _ob_for_b >= 0.55
        and len(critical_flags_present) == 0
    )
    # AI correction override: the Python correction layer proved the AI applied
    # one or more hard vetoes incorrectly (actual data contradicts veto condition).
    # In this case trust the technical gates over the AI's flawed reasoning.
    ai_correction_override = (
        _ai_correction_applied
        and ai_confident
        and len(critical_flags_present) == 0
    )
    ai_gate_ready = all([
        ai_confident,
        same_direction,
        ai_approved or scenario_a_override or scenario_b_micro_override or ai_correction_override,
        ai_alignment or scenario_a_override or scenario_b_micro_override or ai_correction_override,
        ai_setup_ready or scenario_a_override or scenario_b_micro_override or ai_correction_override,
        scenario == "A" or ai_risk_clear,
    ])

    # ── [Bypass AI] Technical Override ──────────────────────────────────────
    # Activado cuando el modelo de IA es el fallback (lazy_gate / gratuito)
    # O cuando hay un error de API (openrouter_unavailable en risk_flags).
    # Condición: solo Escenario B con RSI extremo y volumen mínimo aceptable.
    # El override NO rebaja el SL ni cambia el sizing — solo omite el gate de IA.
    _ai_model_used = str(ai_signal.get("model", "")).lower()
    _is_lazy_fallback = (
        _ai_model_used == "lazy_gate"
        or "openrouter_unavailable" in risk_flags
        or _ai_model_used == ""
    )
    # Telemetry: rastrear qué bypass activó la entrada.
    _bypass_ai_active: bool = False
    if not ai_gate_ready and _is_lazy_fallback and scenario == "B":
        _rsi_override = float(technical_signal.get("rsi", 99.0))
        _vol_override  = float(technical_signal.get("volume_ratio", 0.0))
        # Umbral estricto: RSI <= 30 (sobreventa extrema) y volumen mínimo 0.8x
        # para que el knife-catch tenga liquidez real antes de entrar.
        _technical_override = _rsi_override <= 30.0 and _vol_override >= 0.8
        if _technical_override:
            ai_gate_ready = True
            _bypass_ai_active = True  # Telemetry Tag
    # ── [/Bypass AI] ─────────────────────────────────────────────────────────

    # Volatilidad: solo exigimos piso de ATR (regla del usuario), techo opcional.
    atr_pct = float(technical_signal.get("atr_pct", 0.0))
    volatility_ready = atr_pct >= settings.min_atr_pct and atr_pct <= settings.max_atr_pct
    volume_ready = float(technical_signal.get("volume_ratio", 0.0)) >= settings.min_volume_ratio

    # ---- FASE 3: Filtros de regimen + score + microestructura ----
    # Objetivo scalping activo: operar mas cuando hay liquidez/flow real,
    # sin abrir entradas de baja calidad.
    regime = str(technical_signal.get("regime", "range")).lower()
    score_relax = max(0.0, min(0.20, settings.guardrail_relaxation))

    effective_setup_score = setup_score
    if strong_micro:
        effective_setup_score += 0.08
    elif spread_pct > settings.max_spread_pct and spread_pct > 0:
        effective_setup_score -= 0.08
    if macro_trend == "bullish":
        effective_setup_score += 0.04
    elif macro_trend == "bearish":
        effective_setup_score -= 0.04
    effective_setup_score = max(0.0, min(1.0, effective_setup_score))

    micro_ready = spread_pct <= settings.max_spread_pct if spread_pct > 0 else True
    flow_ready = trade_flow_score >= settings.min_trade_flow_score

    if regime == "trending_down":
        # Permitimos contrarian SOLO para escenario B con confirmacion micro fuerte.
        regime_ready = bool(
            scenario == "B"
            and orderbook_imbalance >= max(settings.min_orderbook_imbalance, 0.56)
            and flow_ready
            and macro_trend != "bearish"
        )
        # ── [Bypass Macro] Mean Reversion Valve ──────────────────────────────
        # Si la macro es BEARISH pero el 5m detecta sobreventa extrema + volumen,
        # permitimos capturar el rebote táctico sin esperar confirmación macro.
        # Esto captura los "fallen knife bounces" más explosivos de media reversión.
        # Condición doble: RSI <= 30 (extremo) + volumen acelerado (>= 1.2x media)
        # para que no sea una caída libre sin compradores — tiene que haber acción.
        # Telemetry: rastrear si el bypass macro se activó.
        _bypass_macro_active: bool = False
        if not regime_ready and scenario == "B" and macro_trend == "bearish":
            _rsi_mr = float(technical_signal.get("rsi", 99.0))
            _vol_mr = float(technical_signal.get("volume_ratio", 0.0))
            _ob_mr  = float(orderbook_imbalance)
            _mean_reversion_bypass = (
                _rsi_mr <= 30.0       # Sobreventa extrema confirmada
                and _vol_mr >= 1.2    # Volumen >= 1.2x promedio (compradores llegando)
                and _ob_mr >= 0.30    # Mínimo de presión compradora en orderbook
            )
            if _mean_reversion_bypass:
                regime_ready = True
                _bypass_macro_active = True  # Telemetry Tag
                # ── [Flow Bypass Patch] ──────────────────────────────────────
                # Dentro del bypass macro, el MIN_TRADE_FLOW_SCORE global (0.44)
                # se reduce a 0.30. Los rebotes de media reversión arranca con
                # flow bajo porque los market makers aún no han reaccionado.
                # La restricción aplica SOLO aquí — el resto del código sigue
                # usando settings.min_trade_flow_score sin cambios.
                if trade_flow_score < settings.min_trade_flow_score and trade_flow_score >= 0.30:
                    flow_ready = True
                # ── [/Flow Bypass Patch] ─────────────────────────────────────
        # ── [/Bypass Macro] ───────────────────────────────────────────────────
        regime_min_score = max(0.55, 0.62 - score_relax)
    elif regime == "chop":
        # En chop solo entramos si el spread es sano y el tape acompana.
        regime_ready = bool(micro_ready and flow_ready and orderbook_imbalance >= max(settings.min_orderbook_imbalance, 0.54))
        regime_min_score = max(0.50, 0.58 - score_relax)
    elif regime == "trending_up":
        regime_ready = True
        # En tendencia alcista favorecemos C y D (continuation/cross), A sigue permitido
        if scenario == "C":
            regime_min_score = max(0.34, 0.44 - score_relax)
        elif scenario == "D":
            regime_min_score = max(0.32, 0.42 - score_relax)
        else:
            regime_min_score = max(0.30, 0.40 - score_relax)
    else:  # range
        regime_ready = True
        regime_min_score = max(0.38, 0.50 - score_relax)

    score_ready = effective_setup_score >= regime_min_score

    cooldown_active = _is_in_cooldown(order_history, settings.trade_cooldown_minutes)
    symbol_cooldown_active = _is_symbol_in_cooldown(order_history, symbol) if symbol else False
    insufficient_backoff_active, insufficient_failures = _insufficient_balance_backoff_active(order_history)
    executable_signal = signal == "buy" and scenario in {"A", "B", "C", "D"}

    return {
        "scenario": scenario,
        "scenario_a": bool(technical_signal.get("scenario_a")),
        "scenario_b": bool(technical_signal.get("scenario_b")),
        "scenario_c": bool(technical_signal.get("scenario_c")),
        "scenario_d": bool(technical_signal.get("scenario_d")),
        "same_direction": same_direction,
        "ai_confident": ai_confident,
        "ai_approved": ai_approved,
        "ai_alignment": ai_alignment,
        "ai_setup_ready": ai_setup_ready,
        "ai_risk_clear": ai_risk_clear,
        "ai_gate_ready": ai_gate_ready,
        "ai_confidence": ai_confidence,
        "ai_conf_threshold": round(ai_conf_threshold, 4),
        "volatility_ready": volatility_ready,
        "volume_ready": volume_ready,
        "micro_ready": micro_ready,
        "flow_ready": flow_ready,
        "spread_pct": spread_pct,
        "orderbook_imbalance": orderbook_imbalance,
        "trade_flow_score": trade_flow_score,
        "macro_trend": macro_trend,
        "regime": regime,
        "regime_ready": regime_ready,
        "setup_score": setup_score,
        "effective_setup_score": round(effective_setup_score, 4),
        "score_ready": score_ready,
        "regime_min_score": regime_min_score,
        "cooldown_active": cooldown_active or symbol_cooldown_active or insufficient_backoff_active,
        "global_cooldown_active": cooldown_active,
        "symbol_cooldown_active": symbol_cooldown_active,
        "insufficient_balance_backoff_active": insufficient_backoff_active,
        "insufficient_balance_failures": insufficient_failures,
        "executable_signal": executable_signal,
        # ── Telemetry Tags ───────────────────────────────────────────────────
        # Identifica el camino lógico que aprobó la entrada.
        # standard_ai  = flujo normal, Gemini conf >= umbral
        # bypass_ai    = lazy_gate / API error + RSI<=30 + vol>=0.8
        # bypass_macro = mean reversion: trending_down + bearish + RSI<=30 + vol>=1.2
        "entry_logic_tag": (
            "bypass_macro" if locals().get("_bypass_macro_active", False)
            else "bypass_ai" if _bypass_ai_active
            else "standard_ai"
        ),
        # ── /Telemetry Tags ──────────────────────────────────────────────────
    }


def _is_pre_signal_candidate(
    settings: Settings,
    technical_signal: dict[str, Any],
    *,
    symbol: str | None = None,
) -> tuple[bool, str]:
    # Apply per-market profile so volume/ATR gates use the right thresholds.
    settings = apply_market_profile(settings, symbol)

    scenario = technical_signal.get("scenario")
    if scenario not in {"A", "B", "C", "D"}:
        return False, "sin escenario A/B/C/D"
    volume_ratio = float(technical_signal.get("volume_ratio", 0.0))
    if volume_ratio < settings.min_volume_ratio:
        return False, "volumen insuficiente"
    atr_pct = float(technical_signal.get("atr_pct", 0.0))
    if not (settings.min_atr_pct <= atr_pct <= settings.max_atr_pct):
        return False, "volatilidad fuera de rango"
    # Escenario C (continuacion) exige algo mas de traccion para no comprar ruido en 5m.
    # Escenario A: pullback con tendencia. Permitimos slope ligeramente negativo
    # (mercado oscila); solo rechazamos si la caida es muy agresiva.
    rsi_value = float(technical_signal.get("rsi", 50.0))
    rsi_slope_value = float(technical_signal.get("rsi_slope", 0.0))
    if scenario == "A":
        if rsi_slope_value < -1.8:
            return False, "escenario A RSI cayendo con fuerza (slope < -1.8)"
    # Escenario B: sobreventa extrema.
    # OVERRIDE deep-oversold: si RSI <= 28, capturamos el knife-catch sin exigir
    # vela verde ni freno de slope (los rebotes mas explosivos vienen de aqui).
    if scenario == "B":
        deep_oversold = rsi_value <= 28.0
        if not deep_oversold and rsi_slope_value < -2.2:
            return False, "escenario B RSI cayendo con fuerza (slope < -2.2)"
    if scenario == "C":
        if not bool(technical_signal.get("green_candle")):
            return False, "escenario C sin vela verde"
        # Relajamos: RSI slope no debe estar cayendo agresivamente (era > 0)
        if rsi_slope_value < -1.5:
            return False, "escenario C RSI cayendo con fuerza"
        if float(technical_signal.get("volume_acceleration", 0.0)) < 0.80:
            return False, "escenario C sin aceleracion de volumen"
    # Escenario D (EMA cross): confirmar que el cruce sea real
    if scenario == "D":
        if not bool(technical_signal.get("bullish_cross")):
            return False, "escenario D sin cruce EMA confirmado"
    return True, f"candidato escenario {scenario}"


def _build_scan_summary(scan: dict[str, Any], settings: Settings, *, blocked_by_lock: bool) -> dict[str, Any]:
    """Resumen para el dashboard del estado de cada ticker en el ciclo actual."""
    # Per-market profile so the dashboard shows the EFFECTIVE thresholds
    # for this symbol (not the global defaults).
    sym = scan.get("symbol")
    market_settings = apply_market_profile(settings, sym)
    ts = scan.get("technical_signal", {})
    candidate = bool(scan.get("candidate"))
    if blocked_by_lock:
        status = "locked"
    elif candidate:
        status = "candidate"
    elif ts.get("scenario") in {"A", "B", "C", "D"}:
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
    elif ts.get("scenario") in {"A", "B", "C", "D"}:
        rejection_stage = "guardrail"
    return {
        "symbol": scan.get("symbol"),
        "status": status,
        "scenario": ts.get("scenario"),
        "scenario_a": bool(ts.get("scenario_a")),
        "scenario_b": bool(ts.get("scenario_b")),
        "scenario_c": bool(ts.get("scenario_c")),
        "scenario_d": bool(ts.get("scenario_d")),
        "regime": ts.get("regime"),
        "macro_trend": ts.get("macro_trend"),
        "setup_score": ts.get("setup_score"),
        "spread_pct": ts.get("spread_pct"),
        "trade_flow_score": ts.get("trade_flow_score"),
        "trade_flow_ratio": ts.get("trade_flow_ratio"),
        "tape_momentum_pct": ts.get("tape_momentum_pct"),
        "orderbook_imbalance": ts.get("orderbook_imbalance"),
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
        "ia_cached": bool(scan.get("ia_cached", False)),
        "ia_cached_age_seconds": float(scan.get("ia_cached_age_seconds") or 0.0),
        "ai_signal": scan.get("ia_signal"),
        "guardrails": scan.get("guardrails"),
        "blocked_by_lock": blocked_by_lock,
        "candle_timestamp": str(candle.get("timestamp")) if candle else None,
        "orderbook": scan.get("orderbook") or {},
        "macro_regime": scan.get("macro_regime") or {},
        # ── Micro-Gate proximity fields (V3 AI prompt, regime-aware gates) ──
        # Required by MicroGateRadar UI component.
        "bb_width_pct": ts.get("bb_width_pct"),
        "rsi_slope": ts.get("rsi_slope"),
        "candle_body_pct": ts.get("candle_body_pct"),
        "volume_acceleration": ts.get("volume_acceleration"),
        # ── Per-market thresholds (entry profile) ────────────────────────────
        # Frontend reads these to render "live vs threshold" gauges per market.
        "thresholds": {
            "scenario_a_rsi_max": market_settings.scenario_a_rsi_max,
            "scenario_b_rsi_max": market_settings.scenario_b_rsi_max,
            "min_atr_pct": market_settings.min_atr_pct,
            "max_atr_pct": market_settings.max_atr_pct,
            "min_volume_ratio": market_settings.min_volume_ratio,
            "min_orderbook_imbalance": market_settings.min_orderbook_imbalance,
            "min_trade_flow_score": market_settings.min_trade_flow_score,
            "max_spread_pct": market_settings.max_spread_pct,
            "ai_confidence_threshold": market_settings.ai_confidence_threshold,
        },
        # ── Cadence per-market (TTL IA, prioridad, label, tag) ───────────────
        # Permite al frontend mostrar badges de velocidad ("turbo 30s",
        # "institucional 180s") y al operador entender por qué WIF se
        # re-evalúa antes que BTC.
        "cadence": get_market_cadence(scan["symbol"]),
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


# ─────────────────────────────────────────────────────────────────────────────
# FEE ACCOUNTING HELPER
# Calcula el PnL NETO descontando comisiones de entrada y salida.
# Fee de exchange Binance: 0.1% por lado (0.2% round-trip). Si el cierre live
# reporta fee_quote real, se usa ese valor; si no, se estima al 0.1%.
# En DRY_RUN siempre se estiman las fees para no confundir con cero.
# ─────────────────────────────────────────────────────────────────────────────
_BINANCE_FEE_RATE = 0.001  # 0.1% por lado


def _compute_net_pnl(
    exit_price: float,
    entry_price: float,
    amount: float,
    side: str,
    *,
    exit_fee_quote: float | None = None,
    entry_fee_quote: float | None = None,
    mode: str = "live",
) -> tuple[float, float]:
    """Devuelve (pnl_neto_usdt, total_fees_usdt).

    Para posiciones live, usa las fees reales del exchange cuando están disponibles;
    en caso contrario las estima al 0.1% por lado. En DRY_RUN siempre estima.
    """
    gross = (exit_price - entry_price) * amount if side == "buy" else (entry_price - exit_price) * amount
    notional_entry = entry_price * amount
    notional_exit = exit_price * amount
    is_live = str(mode).lower() in {"live"}
    # Fee de salida: real (live) o estimada
    if is_live and exit_fee_quote is not None:
        fee_exit = max(0.0, float(exit_fee_quote))
    else:
        fee_exit = notional_exit * _BINANCE_FEE_RATE
    # Fee de entrada: real (live) o estimada
    if is_live and entry_fee_quote is not None:
        fee_entry = max(0.0, float(entry_fee_quote))
    else:
        fee_entry = notional_entry * _BINANCE_FEE_RATE
    total_fees = fee_entry + fee_exit
    return round(gross - total_fees, 6), round(total_fees, 6)


async def _settle_open_positions(
    open_positions: list[dict[str, Any]],
    candles_by_symbol: dict[str, dict[str, Any]],
    *,
    settings: Settings,
    live_mode: bool,
    executor: TradeExecutor,
    logger: logging.Logger,
    persist_open_positions: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    microstructure_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # ---- Trailing stop: Strict Percentage-Based Tiers ----
    # Tiers deterministas basados en el MFE% real desde precio de entrada.
    # Sin dependencia de ATR — el sistema reacciona al recorrido concreto de cada
    # posición, no a la volatilidad estimada. Esto garantiza que el usuario NUNCA
    # necesita intervenir manualmente: el bot asegura cada micro-escalon de ganancia.
    #
    # Formato: (tier_id, trigger_pct, sl_offset_pct)
    #   trigger_pct   = MFE mínimo desde entry para activar el tier
    #   sl_offset_pct = nivel del nuevo SL como % positivo desde entry
    #                   (el signo se aplica según side en el call site)
    #
    # TRAILING TIERS 2.0 — Diseño "Hit & Run":
    # Tier 1 activa al +0.35%: SL sube a Atomic Breakeven (entry + 0.22% = fees 0.20% + buffer).
    # Tier 2 activa al +0.70%: SL sube a entry +0.55% (= mark - 0.15% aproximado).
    # Tier 3 activa al +1.10%: SL sube a entry +0.95% (= mark - 0.15% aproximado).
    # Tier 4 activa al +1.50%: SL sube a entry +1.35% (= mark - 0.15% aproximado).
    # El offset es siempre desde entry; la diferencia entre tiers es 0.15% = trailing offset efectivo.
    _TRAILING_TIERS: tuple[tuple[int, float, float], ...] = (
        (1, 0.0035, 0.0022),  # MFE >= +0.35% → SL en entry +0.22% (atomic breakeven: fees + buffer)
        (2, 0.0070, 0.0055),  # MFE >= +0.70% → SL en entry +0.55% (≈ mark - 0.15%)
        (3, 0.0110, 0.0095),  # MFE >= +1.10% → SL en entry +0.95% (≈ mark - 0.15%)
        (4, 0.0150, 0.0135),  # MFE >= +1.50% → SL en entry +1.35% (≈ mark - 0.15%)
    )

    def _resolve_trailing_tier(mfe_pct: float, atr_pct: float | None = None) -> tuple[int, float | None]:
        """Evalúa los tiers en cascada ascendente y devuelve el más alto alcanzado.

        El parámetro ``atr_pct`` se conserva por compatibilidad de firma pero ya
        no afecta el resultado — los tiers son 100% porcentuales.

        Invariante de no-retroceso: el llamador compara ``new_tier > trailing_tier``
        antes de mover el SL, por lo que este método nunca degrada una protección
        ya consolidada.
        """
        target_tier: int = 0
        target_offset: float | None = None
        # Iteración ascendente: el último tier cuyo trigger es superado gana.
        # Usamos comparación con tolerancia flotante mínima para evitar errores
        # de representación binaria en precios de 8 decimales.
        for tier_id, trigger_pct, offset_pct in _TRAILING_TIERS:
            if mfe_pct >= trigger_pct - 1e-9:
                target_tier = tier_id
                target_offset = offset_pct
        return target_tier, target_offset

    def _resolve_dynamic_stagnation_thresholds(hold_minutes: float | None) -> tuple[float, float, int]:
        base_mfe_cap = settings.smart_stagnation_max_mfe_pct
        base_loss_cut = settings.smart_stagnation_loss_cut_pct
        if hold_minutes is None:
            return base_mfe_cap, base_loss_cut, 0

        if (
            settings.smart_degradation_step_minutes <= 0
            or hold_minutes < settings.smart_degradation_start_minutes
        ):
            return base_mfe_cap, base_loss_cut, 0

        steps = int((hold_minutes - settings.smart_degradation_start_minutes) // settings.smart_degradation_step_minutes) + 1
        dynamic_mfe_cap = min(
            settings.smart_degradation_max_mfe_cap_pct,
            base_mfe_cap + (steps * settings.smart_degradation_mfe_cap_step_pct),
        )
        dynamic_loss_cut = max(
            settings.smart_degradation_min_loss_cut_pct,
            base_loss_cut - (steps * settings.smart_degradation_loss_cut_step_pct),
        )
        return dynamic_mfe_cap, dynamic_loss_cut, steps

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
            hard_timeout_enabled = settings.smart_hard_timeout_minutes > 0 and hold_minutes is not None
            stagnation_enabled = (
                settings.smart_stagnation_minutes > 0
                and settings.smart_stagnation_max_mfe_pct > 0
                and settings.smart_stagnation_loss_cut_pct > 0
                and hold_minutes is not None
            )
            dynamic_mfe_cap, dynamic_loss_cut, degradation_steps = _resolve_dynamic_stagnation_thresholds(hold_minutes)

            # 0) Micro-Structure Bailout:
            # Cierre defensivo anticipado cuando la microestructura colapsa mientras
            # la posicion esta en drawdown. Actua ANTES del Hard SL para preservar capital
            # cuando el orderbook imbalance y el trade flow score caen simultaneamente.
            # Guardia: solo si PnL < 0, hold >= BAILOUT_MIN_HOLD_MINUTES, y metricas disponibles.
            _ms = (microstructure_by_symbol or {}).get(symbol, {})
            _ob_bailout = _ms.get("orderbook_imbalance")
            _flow_bailout = _ms.get("trade_flow_score")
            bailout_triggered = (
                settings.bailout_enabled
                and unrealized_pct < -settings.bailout_min_drawdown_pct
                and hold_minutes is not None
                and hold_minutes >= settings.bailout_min_hold_minutes
                and _ob_bailout is not None
                and _flow_bailout is not None
                and _ob_bailout < settings.bailout_max_ob_imbalance
                and _flow_bailout < settings.bailout_max_flow_score
            )
            if bailout_triggered:
                exit_price = mark_price
                exit_reason = "microstructure_bailout"
                position["smart_exit"] = {
                    "rule": "microstructure_bailout",
                    "hold_minutes": round(hold_minutes, 1),
                    "unrealized_pct": round(unrealized_pct, 6),
                    "orderbook_imbalance": round(float(_ob_bailout), 4),
                    "trade_flow_score": round(float(_flow_bailout), 4),
                    "threshold_ob": settings.bailout_max_ob_imbalance,
                    "threshold_flow": settings.bailout_max_flow_score,
                }
                logger.warning(
                    "Bailout Executed: Micro-structure collapsed while in drawdown. Preventing Hard SL. "
                    "%s side=%s hold=%.1fmin pnl=%.3f%% ob_imbalance=%.3f flow_score=%.3f",
                    symbol,
                    side,
                    hold_minutes,
                    unrealized_pct * 100,
                    _ob_bailout,
                    _flow_bailout,
                )
            # 1) Hard timeout inteligente:
            # si una posicion lleva demasiado tiempo sin asegurar tier 1,
            # cerramos por mercado para reciclar capital y evitar "muertes lentas".
            elif (
                hard_timeout_enabled
                and hold_minutes >= settings.smart_hard_timeout_minutes
                and trailing_tier < 1
            ):
                exit_price = mark_price
                exit_reason = "smart_hard_timeout"
                position["smart_exit"] = {
                    "rule": "hard_timeout",
                    "hold_minutes": round(hold_minutes, 1),
                    "threshold_minutes": settings.smart_hard_timeout_minutes,
                    "trailing_tier": trailing_tier,
                    "mfe_pct": round(mfe_pct, 6),
                    "unrealized_pct": round(unrealized_pct, 6),
                }
                logger.info(
                    "Smart exit hard-timeout %s side=%s hold=%.1fmin tier=%s mfe=%.3f%% pnl=%.3f%%",
                    symbol,
                    side,
                    hold_minutes,
                    trailing_tier,
                    mfe_pct * 100,
                    unrealized_pct * 100,
                )
            # 2) Stagnation loss-cut:
            # si tras X minutos el trade nunca mostró tracción suficiente (MFE bajo)
            # y además ya está en pérdida material, salimos antes del SL completo.
            elif (
                stagnation_enabled
                and hold_minutes >= settings.smart_stagnation_minutes
                and mfe_pct <= dynamic_mfe_cap
                and unrealized_pct <= -dynamic_loss_cut
            ):
                exit_price = mark_price
                exit_reason = "smart_stagnation_loss_cut"
                position["smart_exit"] = {
                    "rule": "stagnation_loss_cut",
                    "hold_minutes": round(hold_minutes, 1),
                    "threshold_minutes": settings.smart_stagnation_minutes,
                    "max_mfe_pct": dynamic_mfe_cap,
                    "loss_cut_pct": dynamic_loss_cut,
                    "base_max_mfe_pct": settings.smart_stagnation_max_mfe_pct,
                    "base_loss_cut_pct": settings.smart_stagnation_loss_cut_pct,
                    "degradation_steps": degradation_steps,
                    "mfe_pct": round(mfe_pct, 6),
                    "unrealized_pct": round(unrealized_pct, 6),
                }
                logger.info(
                    "Smart exit stagnation %s side=%s hold=%.1fmin mfe=%.3f%% pnl=%.3f%% (cap=%.3f%% cut=%.3f%% steps=%s)",
                    symbol,
                    side,
                    hold_minutes,
                    mfe_pct * 100,
                    unrealized_pct * 100,
                    dynamic_mfe_cap * 100,
                    dynamic_loss_cut * 100,
                    degradation_steps,
                )
            elif (
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

        # PnL NETO: descuenta fees reales de salida + fees estimadas de entrada.
        # entry_fee_quote se persiste al abrir la posición (ver run_cycle).
        _net, _fees = _compute_net_pnl(
            exit_price, entry_price, amount, side,
            exit_fee_quote=live_payload.get("fee_quote") if live_payload else None,
            entry_fee_quote=position.get("entry_fee_quote"),
            mode=position.get("mode", "live"),
        )
        pnl_usdt = _net
        closed_trades.append(
            {
                **position,
                "closed_at": closed_at,
                "exit_price": round(exit_price, 4),
                "exit_reason": exit_reason,
                "pnl_usdt": round(pnl_usdt, 4),
                "pnl_pct": round((pnl_usdt / max(entry_price * amount, 1e-9)), 4),
                "fees_usdt": round(_fees, 6),
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


def _deduce_exit_reason(position: dict[str, Any], exit_price: float, logger: logging.Logger) -> str:
    """Deduce the true exit reason for a position that disappeared without a bot-issued close.

    Compares the known exit price (last candle close / mark price) against the
    position's stored SL / TP thresholds with a 0.05% relative tolerance to
    absorb normal slippage.  Falls back to ``external_reconcile`` only when no
    threshold matches.

    Deduction rules (evaluated in priority order):
    1. exit_price >= take_profit            → "take_profit"
    2. exit_price ≈ current stop_loss
       AND trailing_tier > 0               → "trailing_stop_tier_<N>"
    3. exit_price ≈ current stop_loss
       AND trailing_tier == 0              → "stop_loss"
    4. exit_price ≈ initial_stop_loss
       (trailing had been set but price fell through to original SL level)
                                           → "stop_loss"
    5. exit_price < entry_price
       (drawdown — assume stop-loss even if price is far below, covers flash-crash)
                                           → "stop_loss"
    6. Otherwise                           → "external_reconcile"
    """
    # ── Extract position state ───────────────────────────────────────────────
    entry_price    = float(position.get("entry_price") or 0.0)
    take_profit    = float(position.get("take_profit") or 0.0)
    stop_loss      = float(position.get("stop_loss") or 0.0)          # current SL (may be trailing)
    initial_sl     = float(position.get("initial_stop_loss") or stop_loss)
    trailing_tier  = int(position.get("trailing_tier") or 0)
    symbol         = str(position.get("symbol") or "")

    if exit_price <= 0 or entry_price <= 0:
        logger.debug(
            "EXIT DEDUCTOR: symbol=%s — exit_price or entry_price is zero, "
            "cannot deduce; falling back to external_reconcile.",
            symbol,
        )
        return "external_reconcile"

    # Relative tolerance: 0.05% of the reference price.
    # math.isclose(a, b, rel_tol=5e-4) is True when |a-b| <= 5e-4 * max(|a|,|b|).
    _TOL = 5e-4  # 0.05%

    def _close_to(price: float, reference: float) -> bool:
        if reference <= 0:
            return False
        return math.isclose(price, reference, rel_tol=_TOL)

    # ── Rule 1: Take-Profit hit ──────────────────────────────────────────────
    # For a long, TP is above entry. A candle close AT or above TP is a hit.
    if take_profit > 0 and (exit_price >= take_profit or _close_to(exit_price, take_profit)):
        logger.info(
            "EXIT DEDUCTOR: symbol=%s exit_price=%.6f — DEDUCED take_profit "
            "(tp=%.6f tol=%.4f%%)",
            symbol, exit_price, take_profit, _TOL * 100,
        )
        return "take_profit"

    # ── Rule 2 / 3: Stop-Loss hit (current SL) ──────────────────────────────
    if stop_loss > 0 and (exit_price <= stop_loss or _close_to(exit_price, stop_loss)):
        if trailing_tier > 0:
            reason = f"trailing_stop_tier_{trailing_tier}"
            logger.info(
                "EXIT DEDUCTOR: symbol=%s exit_price=%.6f — DEDUCED %s "
                "(sl=%.6f trailing_tier=%d tol=%.4f%%)",
                symbol, exit_price, reason, stop_loss, trailing_tier, _TOL * 100,
            )
            return reason
        else:
            logger.info(
                "EXIT DEDUCTOR: symbol=%s exit_price=%.6f — DEDUCED stop_loss "
                "(sl=%.6f no trailing tol=%.4f%%)",
                symbol, exit_price, stop_loss, _TOL * 100,
            )
            return "stop_loss"

    # ── Rule 4: Price touched the initial SL even if current SL was trailed ──
    # (rare: price fell through all tiers back to original protection level)
    if initial_sl > 0 and (exit_price <= initial_sl or _close_to(exit_price, initial_sl)):
        logger.info(
            "EXIT DEDUCTOR: symbol=%s exit_price=%.6f — DEDUCED stop_loss via "
            "initial_stop_loss=%.6f (trailing_tier=%d, price fell through all tiers)",
            symbol, exit_price, initial_sl, trailing_tier,
        )
        return "stop_loss"

    # ── Rule 5: Flash-crash / drawdown — price ended below entry ─────────────
    # Even if not close to any stored SL, a close below entry signals the SL
    # was hit (possibly with extreme slippage or OCO/stop-market gap).
    if exit_price < entry_price:
        logger.info(
            "EXIT DEDUCTOR: symbol=%s exit_price=%.6f < entry=%.6f — DEDUCED "
            "stop_loss (drawdown / flash-crash; sl=%.6f initial_sl=%.6f "
            "trailing_tier=%d)",
            symbol, exit_price, entry_price, stop_loss, initial_sl, trailing_tier,
        )
        if trailing_tier > 0:
            # Trailing was active: the SL that got hit was the trailed one.
            return f"trailing_stop_tier_{trailing_tier}"
        return "stop_loss"

    # ── Rule 6: Cannot deduce — genuine external / manual close ─────────────
    logger.debug(
        "EXIT DEDUCTOR: symbol=%s exit_price=%.6f — no threshold matched "
        "(tp=%.6f sl=%.6f initial_sl=%.6f trailing_tier=%d); "
        "classifying as external_reconcile.",
        symbol, exit_price, take_profit, stop_loss, initial_sl, trailing_tier,
    )
    return "external_reconcile"


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
        # PnL NETO con estimación de fees (no tenemos fee_quote real en cierre externo).
        _net, _fees = _compute_net_pnl(
            exit_price, entry_price, amount, str(position.get("side", "buy")),
            mode=position.get("mode", "live"),
        )
        pnl_usdt = _net

        # ── EXIT-REASON DEDUCTOR ─────────────────────────────────────────────
        # La posición desapareció sin que el bot emitiera la orden de cierre.
        # En lugar de registrar siempre "external_reconcile", deducimos el motivo
        # real comparando el precio de salida con los umbrales almacenados.
        #
        # Tolerancia de slippage: 0.05% del precio de referencia (configurable).
        # Usamos math.isclose con rel_tol=0.0005 como delta relativo.
        #
        # Campos relevantes del estado de la posición:
        #   take_profit       — precio del TP original
        #   stop_loss         — precio del SL *actual* (puede haber sido movido por el trailing)
        #   initial_stop_loss — SL original al abrir (< entry para long)
        #   trailing_tier     — 0 si nunca se activó trailing; 1-4 si fue movido

        deduced_exit_reason = _deduce_exit_reason(position, exit_price, logger)

        mfe_pct = float(position.get("mfe_pct") or 0.0)
        mfe_usdt = float(position.get("mfe_usdt") or 0.0)

        if deduced_exit_reason == "external_reconcile":
            logger.warning(
                "MANUAL CLOSE DETECTED: Se ha saboteado el MFE de la operacion. "
                "symbol=%s mfe_pct=%.4f%% mfe_usdt=%.4f exit_reason=external_reconcile "
                "pnl_neto=%.4f fees=%.4f. "
                "El trailing stop y el TP automatico fueron anulados por intervencion externa.",
                symbol,
                mfe_pct * 100,
                mfe_usdt,
                pnl_usdt,
                _fees,
            )
        else:
            logger.info(
                "EXIT DEDUCED: symbol=%s exit_price=%.6f deduced_reason=%s "
                "trailing_tier=%s pnl_neto=%.4f",
                symbol,
                exit_price,
                deduced_exit_reason,
                position.get("trailing_tier", 0),
                pnl_usdt,
            )
        externally_closed.append(
            {
                **position,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "exit_price": round(exit_price, 4),
                "exit_reason": deduced_exit_reason,
                "pnl_usdt": round(pnl_usdt, 4),
                "pnl_pct": round((pnl_usdt / max(entry_price * amount, 1e-9)), 4),
                "fees_usdt": round(_fees, 6),
                "status": "closed",
                "live_close": {
                    "status": "reconciled_external" if deduced_exit_reason == "external_reconcile" else "reconciled_deduced",
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
            # Sanity guard: if held_total is < 20% of the expected position amount,
            # the holdings are stale dust from a previously closed trade (e.g. 0.000964 SOL
            # left over after the last close).  Using that dust value would make equity
            # appear to crash to near-zero.  Fall back to position `amount` in that case.
            if holdings and held_total >= amount * 0.20:
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


def _rehidrate_recovered_position(
    recovered: dict[str, Any],
    previous_open_positions: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    settings: Settings,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Inject persisted metadata into a freshly-recovered exchange position.

    When the bot restarts and finds an orphaned holding on Binance it builds a
    minimal "recovered_live" position dict.  Without this function that dict
    lacks critical fields:
        - trailing_tier / initial_stop_loss (trailing engine needs these)
        - ai_prompt_version / ai_regime      (cohort analytics)
        - scenario / ai_confidence / entry_rsi (dashboard display)
        - entry_logic_tag                    (telemetry badge)

    Search order:
      1. Previous open_positions.json  — hot reload: bot restarted mid-trade.
      2. closed_trades.json (last entry for symbol) — position closed + reopened
         before the bot had a chance to write open_positions.
      3. Inverse-calculate trailing_tier from SL distance if no persisted state
         is found (cold recovery — no historical record).
    """
    symbol = str(recovered.get("symbol") or "")
    entry_price = float(recovered.get("entry_price") or 0.0)
    current_sl = float(recovered.get("stop_loss") or 0.0)

    # ── Strategy 1: previous open_positions (exact symbol match) ─────────────
    prev_match = next((p for p in previous_open_positions if str(p.get("symbol") or "") == symbol), None)
    if prev_match:
        _FIELDS_TO_RESTORE = (
            "trailing_tier", "initial_stop_loss", "ai_prompt_version", "ai_regime",
            "scenario", "ai_confidence", "entry_rsi", "entry_logic_tag",
            "ai_micro_gate_path", "ai_risk_flags", "ai_setup_quality",
            "conviction_multiplier", "sl_pct_used", "tp_pct_used",
            "entry_atr_pct", "entry_regime", "entry_setup_score",
            "algo_version",
        )
        restored: dict[str, Any] = {}
        for field in _FIELDS_TO_RESTORE:
            if prev_match.get(field) is not None:
                restored[field] = prev_match[field]
        if restored:
            logger.info(
                "Recovery rehidration [open_positions]: %s restored fields: %s",
                symbol, list(restored.keys()),
            )
            return {**recovered, **restored, "rehidrated_from": "previous_open_positions"}

    # ── Strategy 2: last closed_trade for same symbol ─────────────────────────
    closed_for_symbol = [t for t in closed_trades if str(t.get("symbol") or "") == symbol and t.get("mode") == "live"]
    if closed_for_symbol:
        last_closed = closed_for_symbol[-1]
        _CLOSED_FIELDS = (
            "ai_prompt_version", "ai_regime", "scenario", "entry_logic_tag",
            "ai_micro_gate_path", "algo_version",
        )
        restored = {}
        for field in _CLOSED_FIELDS:
            if last_closed.get(field) is not None:
                restored[field] = last_closed[field]
        if restored:
            logger.info(
                "Recovery rehidration [closed_trades]: %s partial metadata restored: %s",
                symbol, list(restored.keys()),
            )
            return {**recovered, **restored, "rehidrated_from": "last_closed_trade"}

    # ── Strategy 3: inverse-calculate trailing tier from SL distance ──────────
    # If SL is above entry (trailing already moved) infer the tier.
    inferred_tier = 0
    inferred_initial_sl: float | None = None
    if entry_price > 0 and current_sl > 0:
        sl_pct_from_entry = (current_sl - entry_price) / entry_price
        from src.safety.risk_manager import RiskManager  # noqa: PLC0415
        _TRAILING_TIERS = [
            {"tier": 4, "trigger": 0.0160, "slAt": 0.0120},
            {"tier": 3, "trigger": 0.0120, "slAt": 0.0080},
            {"tier": 2, "trigger": 0.0080, "slAt": 0.0040},
            {"tier": 1, "trigger": 0.0050, "slAt": 0.0020},
        ]
        for tier_def in _TRAILING_TIERS:
            if abs(sl_pct_from_entry - tier_def["slAt"]) < 0.0015:  # ±0.15% tolerance
                inferred_tier = tier_def["tier"]
                inferred_initial_sl = round(entry_price * (1.0 - tier_def["slAt"]), 4)
                break
        if sl_pct_from_entry < -0.0005:
            # SL is still below entry — tier 0, initial_sl = current_sl
            inferred_initial_sl = current_sl

    if inferred_tier > 0 or inferred_initial_sl is not None:
        logger.info(
            "Recovery rehidration [inverse-calc]: %s inferred trailing_tier=%d initial_stop_loss=%s",
            symbol, inferred_tier, inferred_initial_sl,
        )
        return {
            **recovered,
            "trailing_tier": inferred_tier,
            "initial_stop_loss": inferred_initial_sl or current_sl,
            "rehidrated_from": "inverse_calculation",
        }

    logger.warning(
        "Recovery rehidration [none]: %s — no persisted metadata and inverse-calc inconclusive. "
        "Position will run with default trailing_tier=0.",
        symbol,
    )
    return {**recovered, "rehidrated_from": "none"}


def _recover_unmanaged_exchange_positions(
    open_positions: list[dict[str, Any]],
    settings: Settings,
    client: BinanceDataClient,
    logger: logging.Logger,
    order_history: list[dict[str, Any]] | None = None,
    closed_trades: list[dict[str, Any]] | None = None,
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
    # Ventana de bloqueo post-cierre: si un simbolo fue cerrado recientemente
    # (aparece en order_history con status submitted/simulated dentro de la ventana)
    # no lo recuperamos para evitar el bucle closed → recovered_live → closed.
    recovery_cooldown_minutes = max(settings.trade_cooldown_minutes, 15)  # minimo 15 min

    for symbol in settings.target_symbols:
        if symbol in tracked_symbols:
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": "already_tracked"})
            continue

        # Bloqueo cooldown: si este simbolo tuvo una orden reciente (cierre o entrada
        # rechazada) NO lo recuperamos. Previene el bucle ghost-position.
        if order_history and _is_symbol_in_cooldown(order_history, symbol, minutes=recovery_cooldown_minutes):
            logger.debug(
                "Recovery bloqueada para %s: simbolo en cooldown (%d min).",
                symbol, recovery_cooldown_minutes,
            )
            recovery_report["skipped_symbols"].append({"symbol": symbol, "reason": f"symbol_in_cooldown_{recovery_cooldown_minutes}min"})
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
        # Restore persisted metadata to avoid trailing/exit "amnesia".
        recovered = _rehidrate_recovered_position(
            recovered,
            previous_open_positions=list(open_positions),  # positions loaded before this loop
            closed_trades=closed_trades or [],
            settings=settings,
            logger=logger,
        )
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


def _load_ai_signals_by_symbol(settings) -> dict[str, dict[str, Any]]:
    """Carga el cache de senales IA por simbolo persistido en el state previo.

    Mantiene compatibilidad: si el state legacy solo trae `ai_signal` global, se
    indexa bajo el simbolo de la lectura previa cuando esta disponible para
    evitar perder informacion util tras el upgrade del motor.
    """
    previous_state = load_state(settings.state_file)
    raw = previous_state.get("ai_signals_by_symbol") or {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for sym, sig in raw.items():
            if isinstance(sig, dict) and sym:
                out[str(sym)] = dict(sig)

    # Fallback legacy: aprovechar `ai_signal` global si trae symbol embebido.
    legacy = previous_state.get("ai_signal")
    if isinstance(legacy, dict) and legacy.get("consulted"):
        legacy_symbol = legacy.get("symbol") or (
            previous_state.get("technical_signal", {}) or {}
        ).get("symbol")
        if legacy_symbol and str(legacy_symbol) not in out:
            entry = dict(legacy)
            entry.setdefault("cached_at", previous_state.get("updated_at"))
            entry["symbol"] = str(legacy_symbol)
            out[str(legacy_symbol)] = entry
    return out


# Tactical AI cache: price-delta below this triggers reuse even if TTL expired.
# 0.15% matches the FOMO threshold used for Scenario C limit-sniping.
_AI_PRICE_DELTA_REUSE_PCT = 0.0015

# Scenario C micro-pullback ("Limit Sniping"): the bot will NOT chase a continuation
# unless the candle has already retraced this much from its close. "Compramos barato
# o no compramos". Empirically tuned: V3 cohort showed Scenario C win rate 16.7% with
# market entries; waiting for a 0.15% pullback should restore positive expectancy.
_SCENARIO_C_PULLBACK_PCT = 0.0015


def _can_reuse_ai_for_price(
    cache_entry: dict[str, Any],
    current_price: float,
    *,
    threshold_pct: float | None = None,
) -> bool:
    """Tactical cache: if price has not moved enough, reuse the previous AI verdict.

    Saves OpenRouter quota and avoids the 429 spiral when 4 symbols all churn
    sub-tick movements. Returns True only when the previous decision is still
    statistically valid (price drift < threshold_pct, default 0.15%).
    """
    if not cache_entry or current_price <= 0:
        return False
    price_at_consult = cache_entry.get("price_at_consult")
    try:
        ref_price = float(price_at_consult) if price_at_consult is not None else 0.0
    except (TypeError, ValueError):
        return False
    if ref_price <= 0:
        return False
    delta = abs(current_price - ref_price) / ref_price
    limit = float(threshold_pct) if threshold_pct is not None else _AI_PRICE_DELTA_REUSE_PCT
    return delta < limit


def _get_cached_ai_signal_for_symbol(
    cache: dict[str, dict[str, Any]],
    symbol: str,
    max_age_seconds: int,
) -> dict[str, Any] | None:
    """Devuelve la senal IA cacheada para `symbol` si sigue dentro del TTL.

    El cache es por simbolo: una lectura de BTC NO contamina ETH, SOL, etc.
    """
    entry = cache.get(symbol)
    if not entry:
        return None
    if not entry.get("consulted", True):
        return None
    cached_at = entry.get("cached_at")
    if not cached_at:
        return None
    try:
        cached_ts = datetime.fromisoformat(str(cached_at))
    except ValueError:
        return None
    age_seconds = (datetime.now(timezone.utc) - cached_ts).total_seconds()
    if age_seconds > max_age_seconds:
        return None
    out = dict(entry)
    out["cached"] = True
    out["cached_age_seconds"] = round(age_seconds, 1)
    out["symbol"] = symbol
    return out


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
        "entry_atr_pct": p.get("entry_atr_pct"),
        "stop_loss": p.get("stop_loss"),
        "initial_stop_loss": p.get("initial_stop_loss"),
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
        "initial_stop_loss": p.get("initial_stop_loss"),
        "conviction_multiplier": p.get("conviction_multiplier"),
        "algo_version": p.get("algo_version"),
        # Telemetry tag: required by buildPos() in dashboard-client.js.
        # Without this, the frontend falls back to "standard_ai" — fine but
        # the badge would be wrong for bypass_ai / bypass_macro entries.
        "entry_logic_tag": p.get("entry_logic_tag", "standard_ai"),
        # entry_atr_pct is used by the frontend's trailing tier progress bar.
        "entry_atr_pct": float(p.get("entry_atr_pct") or 0.0),
        # mode used by the frontend live/dry-run badge.
        "mode": p.get("mode"),
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
    # Apply per-market profile for technical signal building (so RSI bounds /
    # ATR ranges / volume ratio in build_technical_signal use the right gates).
    market_settings = apply_market_profile(settings, symbol)
    technical_signal = build_technical_signal(enriched_frame, market_settings)
    candidate, candidate_reason = _is_pre_signal_candidate(
        market_settings, technical_signal, symbol=symbol
    )
    orderbook: dict[str, Any] = {}
    macro_regime: dict[str, Any] = {}
    ticker_snapshot: dict[str, Any] = {}
    trade_flow: dict[str, Any] = {}
    # Solo gastamos llamadas extra a Binance cuando el simbolo es candidato real:
    # asi mantenemos rate-limit bajo control y damos contexto rico a la IA solo
    # cuando vale la pena.
    if candidate:
        orderbook = client.fetch_orderbook_snapshot(symbol=symbol, depth=20)
        macro_regime = client.fetch_macro_regime(symbol=symbol, timeframe="15m", limit=100)
        ticker_snapshot = client.fetch_ticker_snapshot(symbol=symbol)
        trade_flow = client.fetch_trade_flow_snapshot(symbol=symbol, limit=80)
        if orderbook and "imbalance" in orderbook:
            technical_signal["orderbook_imbalance"] = orderbook["imbalance"]
            technical_signal["spread_pct"] = orderbook.get("spread_pct")
        if macro_regime and "trend" in macro_regime:
            technical_signal["macro_trend"] = macro_regime["trend"]
            technical_signal["macro_slope_pct"] = macro_regime.get("slope_pct")
        if ticker_snapshot:
            if technical_signal.get("spread_pct") is None and ticker_snapshot.get("spread_pct") is not None:
                technical_signal["spread_pct"] = ticker_snapshot.get("spread_pct")
            technical_signal["price_change_pct_24h"] = ticker_snapshot.get("price_change_pct_24h")
            technical_signal["quote_volume_24h"] = ticker_snapshot.get("quote_volume_24h")
            technical_signal["vwap_24h"] = ticker_snapshot.get("vwap_24h")
        if trade_flow and "flow_score" in trade_flow:
            technical_signal["trade_flow_ratio"] = trade_flow.get("buy_ratio")
            technical_signal["trade_flow_score"] = trade_flow.get("flow_score")
            technical_signal["tape_momentum_pct"] = trade_flow.get("momentum_pct")
    return {
        "symbol": symbol,
        "frame": enriched_frame,
        "latest_candle": enriched_frame.iloc[-1].to_dict(),
        "technical_signal": technical_signal,
        "candidate": candidate,
        "candidate_reason": candidate_reason,
        "orderbook": orderbook,
        "macro_regime": macro_regime,
        "ticker_snapshot": ticker_snapshot,
        "trade_flow": trade_flow,
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
    # Priorizar mercados volatiles: WIF (1) -> DOGE (2) -> SOL (3) -> ETH/BNB (4) -> BTC (5).
    # Asegura que en cada ciclo los scalpers se escaneen y consulten IA primero,
    # antes de que un 429 backoff o latencia de OHLCV los deje sin turno.
    target_symbols = sort_symbols_by_priority(target_symbols)
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
        open_positions, recovery_report = _recover_unmanaged_exchange_positions(
            open_positions, settings, client, logger, order_history,
            closed_trades=closed_trades,
        )
        persist_history(settings.open_positions_file, open_positions)
        persist_state(settings.logs_dir / "recovery_status.json", recovery_report)

        # 0b) MANUAL CLOSE REQUEST — Rotacion de Capital desde el dashboard.
        # El frontend escribe manual_close_request=True en control.json.
        # El bot lo lee aqui, ANTES del scan, cierra la posicion a mercado
        # a traves del executor (mismo path que cualquier otro cierre),
        # luego limpia el flag. El mutex se libera naturalmente al vaciar
        # open_positions, de modo que el proximo ciclo busca nuevos setups.
        control_for_manual = load_state(settings.control_file)
        if control_for_manual.get("manual_close_request") and open_positions:
            target_symbol = str(control_for_manual.get("manual_close_symbol") or "").strip().upper()
            target_pos = next(
                (p for p in open_positions if not target_symbol or str(p.get("symbol") or "").upper() == target_symbol),
                open_positions[0],
            )
            logger.info(
                "Manual close request recibido para %s (solicitado por: %s, en: %s).",
                target_pos.get("symbol"),
                control_for_manual.get("manual_close_requested_by", "dashboard"),
                control_for_manual.get("manual_close_requested_at", "?"),
            )
            manual_exit_price: float | None = None
            manual_live_payload: dict[str, Any] = {}
            manual_close_ok: bool = False  # guardabarros: solo avanzamos si el exchange confirmó
            if live_mode and target_pos.get("mode") == "live":
                close_result = executor.close_position_market(target_pos)
                if close_result.get("status") not in {"submitted", "simulated_close"}:
                    logger.error(
                        "Manual close FALLO para %s: %s. Posicion NO eliminada del estado. Reintenta con el boton.",
                        target_pos.get("symbol"),
                        close_result,
                    )
                    # Limpiar flag para no bloquear el ciclo, pero NO modificar open_positions.
                    persist_state(
                        settings.control_file,
                        {
                            **control_for_manual,
                            "manual_close_request": False,
                            "manual_close_symbol": None,
                            "manual_close_executed_at": datetime.now(timezone.utc).isoformat(),
                            "manual_close_result": "failed",
                            "manual_close_error": str(close_result.get("reason", "unknown")),
                        },
                    )
                    # Notificar el fallo via Telegram si live
                    if live_mode:
                        _notify_safe(notifier, logger, "error", {
                            "title": "Manual Close FALLO",
                            "symbol": target_pos.get("symbol"),
                            "reason": close_result.get("reason", "unknown"),
                        })
                else:
                    manual_exit_price = float(close_result.get("avg_price") or 0.0) or None
                    manual_live_payload = close_result
                    manual_close_ok = True
            else:
                # Dry run o posicion simulada: proceder sin llamar al exchange.
                manual_close_ok = True
            if manual_close_ok:
                if manual_exit_price is None:
                    # Dry run o posicion sin precio de fill: usar mark_price.
                    manual_exit_price = float(
                        target_pos.get("mark_price")
                        or target_pos.get("entry_price")
                        or 0.0
                    )
                manual_entry = float(target_pos.get("entry_price") or 0.0)
                manual_amount = float(target_pos.get("amount") or 0.0)
                # PnL NETO con fees reales del cierre manual.
                _net_mc, _fees_mc = _compute_net_pnl(
                    manual_exit_price, manual_entry, manual_amount,
                    str(target_pos.get("side", "buy")),
                    exit_fee_quote=(manual_live_payload or {}).get("fee_quote") if manual_live_payload else None,
                    entry_fee_quote=target_pos.get("entry_fee_quote"),
                    mode=target_pos.get("mode", "live"),
                )
                manual_pnl = _net_mc
                # ── ALERTA: CIERRE MANUAL FORZADO ───────────────────────────
                # El operador ordenó cerrar la posición antes de que SL/TP/trailing
                # o el AI monitor tomaran la decisión. Esto destruye el edge estadístico.
                _mfe_manual_pct = float(target_pos.get("mfe_pct") or 0.0)
                _mfe_manual_usdt = float(target_pos.get("mfe_usdt") or 0.0)
                logger.warning(
                    "MANUAL CLOSE DETECTED: Se ha saboteado el MFE de la operacion. "
                    "symbol=%s mfe_pct=%.4f%% mfe_usdt=%.4f exit_reason=manual_capital_rotation "
                    "pnl_neto=%.4f fees=%.4f. "
                    "El trailing stop y el TP automatico fueron anulados por intervencion manual.",
                    target_pos.get("symbol"),
                    _mfe_manual_pct * 100,
                    _mfe_manual_usdt,
                    manual_pnl,
                    _fees_mc,
                )
                manual_closed_trade = {
                    **target_pos,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                    "exit_price": round(manual_exit_price, 4),
                    "exit_reason": "manual_capital_rotation",
                    "pnl_usdt": round(manual_pnl, 4),
                    "pnl_pct": round(manual_pnl / max(manual_entry * manual_amount, 1e-9), 4),
                    "fees_usdt": round(_fees_mc, 6),
                    "status": "closed",
                    "algo_version": target_pos.get("algo_version") or ALGO_VERSION,
                    "live_close": manual_live_payload or None,
                }
                open_positions = [p for p in open_positions if p is not target_pos]
                closed_trades.append(manual_closed_trade)
                persist_history(settings.open_positions_file, open_positions)
                persist_history(settings.closed_trades_file, closed_trades)
                # Insertar en order_history para activar el cooldown global y evitar
                # re-entrada inmediata en el mismo ciclo.
                order_history.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbol": target_pos.get("symbol"),
                    "side": "sell",
                    "status": "submitted",
                    "exit_reason": "manual_capital_rotation",
                    "pnl_usdt": round(manual_pnl, 4),
                })
                persist_history(settings.order_history_file, order_history)
                _notify_safe(notifier, logger, "trade_close", _format_trade_close_message(manual_closed_trade))
                # Distribute PnL to all PAMM investors atomically via the portal.
                _fire_pamm_webhook(manual_closed_trade, settings, logger)
                logger.info(
                    "Manual capital rotation completado: %s exit=%.4f pnl=%.4f USDT.",
                    target_pos.get("symbol"),
                    manual_exit_price,
                    manual_pnl,
                )
                # Limpiar el flag del control file para no repetir en el proximo ciclo.
                persist_state(
                    settings.control_file,
                    {
                        **control_for_manual,
                        "manual_close_request": False,
                        "manual_close_symbol": None,
                        "manual_close_executed_at": datetime.now(timezone.utc).isoformat(),
                        "manual_close_result": "ok",
                    },
                )

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

        # ---- Microstructure fresca para Bailout ----
        # Para open positions que no estuvieran en scan_results como candidatos,
        # hacemos fetch directo de orderbook + trade_flow para tener datos actualizados
        # en cada tick. Posiciones candidatas ya traen los datos de _scan_symbol.
        microstructure_by_symbol: dict[str, dict[str, Any]] = {
            sr["symbol"]: {
                "orderbook_imbalance": sr["technical_signal"].get("orderbook_imbalance"),
                "trade_flow_score": sr["technical_signal"].get("trade_flow_score"),
            }
            for sr in scan_results
            if sr.get("technical_signal", {}).get("orderbook_imbalance") is not None
            or sr.get("technical_signal", {}).get("trade_flow_score") is not None
        }
        if settings.bailout_enabled and open_positions:
            for _pos in open_positions:
                _sym = _pos.get("symbol")
                if _sym and _sym not in microstructure_by_symbol:
                    try:
                        _ob = client.fetch_orderbook_snapshot(symbol=_sym, depth=20)
                        _tf = client.fetch_trade_flow_snapshot(symbol=_sym, limit=80)
                        microstructure_by_symbol[_sym] = {
                            "orderbook_imbalance": _ob.get("imbalance") if _ob else None,
                            "trade_flow_score": _tf.get("flow_score") if _tf else None,
                        }
                    except Exception as _exc:  # noqa: BLE001
                        logger.debug("Bailout: no se pudo obtener microestructura para %s: %s", _sym, _exc)
                        microstructure_by_symbol[_sym] = {"orderbook_imbalance": None, "trade_flow_score": None}

        open_positions, newly_closed_trades = asyncio.run(
            _settle_open_positions(
                open_positions,
                candles_by_symbol,
                settings=settings,
                live_mode=live_mode,
                executor=executor,
                logger=logger,
                persist_open_positions=_persist_open_positions_async,
                microstructure_by_symbol=microstructure_by_symbol,
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
            for closed_trade in newly_closed_trades:
                _notify_safe(notifier, logger, "trade_close", _format_trade_close_message(closed_trade))
                # Distribute PnL to all PAMM investors atomically via the portal.
                _fire_pamm_webhook(closed_trade, settings, logger)
        persist_history(settings.open_positions_file, open_positions)

        # 2b) DYNAMIC AI TRADE MONITOR — evaluacion IA de posiciones abiertas.
        # Se ejecuta cada ciclo (≈60s) cuando hay una posicion activa.
        # Puede mover el SL a breakeven+ o cerrar de emergencia.
        trade_monitor_verdict: dict[str, Any] = {}
        if open_positions and settings.ai_monitor_enabled:
            try:
                monitor = ActiveTradeMonitor(settings, logger)
                active_pos = open_positions[0]
                active_sym = str(active_pos.get("symbol") or "")
                # Buscar el scan de ese simbolo para datos tecnicos fresh
                active_scan = next(
                    (s for s in scan_results if s.get("symbol") == active_sym), None
                )
                trade_monitor_verdict = monitor.evaluate(active_pos, active_scan, client)
                monitor_action = trade_monitor_verdict.get("action", "HOLD")

                if monitor_action == "EMERGENCY_CLOSE":
                    logger.warning(
                        "TradeMonitor EMERGENCY_CLOSE para %s. Rationale: %s",
                        active_sym,
                        trade_monitor_verdict.get("rationale"),
                    )
                    # close_position_market es sincrono
                    close_result = executor.close_position_market(active_pos)
                    if close_result.get("status") in {"submitted", "simulated_close"}:
                        exit_p = float(
                            close_result.get("avg_price")
                            or active_pos.get("mark_price")
                            or active_pos.get("entry_price")
                            or 0.0
                        )
                        entry_p = float(active_pos.get("entry_price") or 0.0)
                        amt = float(active_pos.get("amount") or 0.0)
                        # PnL NETO con fees reales del cierre de emergencia.
                        _net_em, _fees_em = _compute_net_pnl(
                            exit_p, entry_p, amt, str(active_pos.get("side", "buy")),
                            exit_fee_quote=close_result.get("fee_quote"),
                            entry_fee_quote=active_pos.get("entry_fee_quote"),
                            mode=active_pos.get("mode", "live"),
                        )
                        pnl_usdt = _net_em
                        # Usar el exit_reason del veredicto si es un bailout
                        # especializado (p.ej. momentum_exhaustion_bailout);
                        # de lo contrario usar el generico ai_emergency_close.
                        _exit_reason = (
                            trade_monitor_verdict.get("exit_reason")
                            or "ai_emergency_close"
                        )
                        emergency_closed = {
                            **active_pos,
                            "closed_at": datetime.now(timezone.utc).isoformat(),
                            "exit_price": round(exit_p, 4),
                            "exit_reason": _exit_reason,
                            "pnl_usdt": round(pnl_usdt, 4),
                            "pnl_pct": round(
                                pnl_usdt / max(entry_p * amt, 1e-9), 4
                            ),
                            "fees_usdt": round(_fees_em, 6),
                            "status": "closed",
                            "trade_monitor_rationale": trade_monitor_verdict.get("rationale"),
                            "live_close": close_result,
                        }
                        open_positions = [
                            p for p in open_positions
                            if p.get("symbol") != active_sym
                        ]
                        closed_trades.append(emergency_closed)
                        newly_closed_trades.append(emergency_closed)
                        persist_history(settings.open_positions_file, open_positions)
                        persist_history(settings.closed_trades_file, closed_trades)
                        # Insertar en order_history para que el cooldown global lo vea
                        # y bloquee re-entradas durante trade_cooldown_minutes.
                        order_history.append({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "symbol": active_sym,
                            "side": "sell",
                            "status": "submitted",
                            "exit_reason": _exit_reason,
                            "pnl_usdt": round(pnl_usdt, 4),
                        })
                        persist_history(settings.order_history_file, order_history)
                        _notify_safe(
                            notifier, logger, "trade_close",
                            _format_trade_close_message(emergency_closed),
                        )
                        logger.warning(
                            "TradeMonitor cierre de emergencia ejecutado: %s pnl=%.4f USDT. "
                            "Cooldown de %s min activo para evitar re-entrada inmediata.",
                            active_sym,
                            pnl_usdt,
                            settings.trade_cooldown_minutes,
                        )
                    else:
                        logger.error(
                            "TradeMonitor EMERGENCY_CLOSE fallo en ejecucion: %s", close_result
                        )

                elif monitor_action == "UPDATE_SL":
                    new_sl_price = trade_monitor_verdict.get("new_sl_price")
                    current_sl = float(active_pos.get("stop_loss") or 0.0)
                    take_profit = float(active_pos.get("take_profit") or 0.0)
                    is_improvement = (
                        new_sl_price is not None
                        and float(new_sl_price) > current_sl
                    )
                    if is_improvement:
                        logger.info(
                            "TradeMonitor UPDATE_SL para %s: current_sl=%.6f new_sl=%.6f rationale=%s",
                            active_sym,
                            current_sl,
                            float(new_sl_price),
                            trade_monitor_verdict.get("rationale"),
                        )
                        try:
                            # price_to_precision es sincrono
                            new_sl_formatted = executor.client.price_to_precision(
                                float(new_sl_price), symbol=active_sym
                            )
                            # replace_stop_loss_async requiere contexto async
                            sl_result = asyncio.run(
                                executor.replace_stop_loss_async(
                                    position=active_pos,
                                    new_stop_loss=float(new_sl_formatted),
                                    take_profit=take_profit,
                                    trailing_tier=int(active_pos.get("trailing_tier") or 0),
                                )
                            )
                            if sl_result.get("status") in {"submitted", "simulated"}:
                                active_pos["stop_loss"] = float(new_sl_formatted)
                                active_pos["monitor_sl_updated_at"] = datetime.now(timezone.utc).isoformat()
                                active_pos["monitor_sl_rationale"] = trade_monitor_verdict.get("rationale")
                                open_positions[0] = active_pos
                                persist_history(settings.open_positions_file, open_positions)
                                logger.info(
                                    "TradeMonitor SL actualizado: %s new_sl=%.6f",
                                    active_sym,
                                    float(new_sl_formatted),
                                )
                            else:
                                logger.warning(
                                    "TradeMonitor UPDATE_SL no confirmado: %s", sl_result
                                )
                        except (BinanceClientError, ccxt.NetworkError, ccxt.ExchangeError) as exc:
                            logger.warning(
                                "TradeMonitor UPDATE_SL fallo para %s: %s", active_sym, exc
                            )
                    else:
                        logger.info(
                            "TradeMonitor UPDATE_SL descartado: new_sl=%.6f <= current_sl=%.6f",
                            float(new_sl_price or 0),
                            current_sl,
                        )

                else:  # HOLD
                    logger.info(
                        "TradeMonitor HOLD para %s. Rationale: %s",
                        active_sym,
                        trade_monitor_verdict.get("rationale"),
                    )

            except RuntimeError as exc:
                # asyncio.run() no puede ejecutarse dentro de un event loop existente
                logger.error(
                    "TradeMonitor: conflicto de event loop; accion aplazada: %s", exc
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "TradeMonitor: error inesperado; el trade sigue con su gestion normal. %s",
                    exc,
                )

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
                    market_profiles=market_profiles_summary(target_symbols),
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
        # Sanity guard: en trading spot sin apalancamiento es imposible perder >40%
        # del equity en un solo ciclo. Un drawdown > 0.40 indica lectura transitoria
        # mala del balance (e.g. Binance no ha liquidado el USDT tras un cierre reciente).
        # En ese caso, ignorar el kill switch y dejar que el siguiente ciclo corrija.
        _KS_SANITY_CAP = 0.40
        if risk_snapshot.kill_switch_triggered and risk_snapshot.drawdown_pct > _KS_SANITY_CAP:
            logger.error(
                "Kill Switch IGNORADO (falso positivo): drawdown=%.4f > %.0f%% sanity cap. "
                "Posible lectura de balance transitoria. Se revisara en el proximo ciclo.",
                risk_snapshot.drawdown_pct,
                _KS_SANITY_CAP * 100,
            )
            # Forzar recalculo conservador usando el equity anterior para no contaminar el HWM
            risk_snapshot = risk_manager.evaluate(
                balance_usd,
                equity_usd=float(previous_state.get("risk", {}).get("equity_usd") or equity_usd),
                high_water_mark=new_hwm,
            )
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
                    market_profiles=market_profiles_summary(target_symbols),
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

        # 6) Carrera de senales multi-mercado.
        # Radar pre-check: notifica por Telegram si algún scan tiene >= 88% de
        # proximidad a un gate, siempre que no haya posicion abierta (global_lock).
        if not global_lock:
            _mode_tag = "live" if live_mode else "dry_run"
            for _radar_scan in scan_results:
                _maybe_notify_radar_alert(notifier, logger, _radar_scan, _mode_tag)

        # Regla profesional: la IA evalua CADA candidato que paso las guardias
        # tecnicas, con cache por simbolo (una lectura de BTC no se reutiliza
        # para ETH). El primer simbolo que cumpla TODOS los guardarrailes
        # dispara la orden; si ninguno cumple, la telemetria refleja al mejor
        # candidato (mayor conviccion IA) para que el dashboard explique el
        # rechazo del ticker realmente evaluado y no de uno arbitrario.
        ai_signals_by_symbol: dict[str, dict[str, Any]] = _load_ai_signals_by_symbol(settings)
        per_symbol_ai: dict[str, dict[str, Any]] = {}
        per_symbol_guardrails: dict[str, dict[str, Any]] = {}
        ai_signal: dict[str, Any] = dict(AI_NOT_CONSULTED)
        ai_consulted_symbol: str | None = None
        chosen_scan: dict[str, Any] | None = None
        chosen_guardrails: dict[str, Any] | None = None
        best_candidate_scan: dict[str, Any] | None = None
        best_candidate_guards: dict[str, Any] | None = None
        best_candidate_ai: dict[str, Any] | None = None
        decision: dict[str, Any] = {
            "action": "hold",
            "reason": "global_lock" if global_lock else "Sin senal valida en ningun ticker.",
            "global_lock": global_lock,
            "active_symbol": active_symbol,
        }

        if not global_lock:
            # Detecta backoff por insuficiente UNA vez por ciclo (con order_history actual)
            # y notifica si esta activo, para evitar que el operador descubra el problema
            # solo mirando logs.
            backoff_active, backoff_failures = _insufficient_balance_backoff_active(order_history)
            if backoff_active:
                _maybe_notify_balance_backoff(settings, logger, backoff_failures)

            for scan in scan_results:
                technical_signal = scan["technical_signal"]
                symbol = scan["symbol"]

                # ── ONE-TRADE-PER-SYMBOL LOCK ──────────────────────────────────────────
                # Si ya hay una posicion abierta en este simbolo, no abrimos otra.
                # Se comprueba ANTES de pre_guards para ahorrar CPU y creditos IA.
                # Esta regla es independiente del global_lock (MAX_GLOBAL_POSITIONS):
                # el global_lock limita el total del portfolio, este lock evita la
                # "Correlated Risk" donde WIF colapsa con 2 posiciones abiertas en WIF.
                if any(str(p.get("symbol") or "") == symbol for p in open_positions):
                    logger.debug(
                        "ONE-TRADE-PER-SYMBOL: %s ya tiene una posicion abierta — saltando.",
                        symbol,
                    )
                    continue
                # ── /ONE-TRADE-PER-SYMBOL LOCK ────────────────────────────────────────


                # ── ONE-TRADE-PER-SYMBOL LOCK ──────────────────────────────────────────
                # Si ya hay una posicion abierta en este simbolo, no abrimos otra.
                # Se comprueba ANTES de pre_guards para ahorrar CPU y creditos IA.
                # Esta regla es independiente del global_lock (MAX_GLOBAL_POSITIONS):
                # el global_lock limita el total del portfolio, este lock evita la
                # "Correlated Risk" donde WIF colapsa con 2 posiciones abiertas en WIF.
                if any(str(p.get("symbol") or "") == symbol for p in open_positions):
                    logger.debug(
                        "ONE-TRADE-PER-SYMBOL: %s ya tiene una posicion abierta — saltando.",
                        symbol,
                    )
                    continue
                # ── /ONE-TRADE-PER-SYMBOL LOCK ────────────────────────────────────────

                # Pre-gate barato por simbolo (sin gastar tokens IA).
                pre_guards = _build_guardrails(settings, technical_signal, AI_NOT_CONSULTED, order_history, symbol=symbol)
                if not (
                    pre_guards["executable_signal"]
                    and pre_guards["volatility_ready"]
                    and pre_guards["volume_ready"]
                    and not pre_guards["cooldown_active"]
                ):
                    continue

                # Cadencia per-market: WIF/DOGE refrescan IA cada 30-45s,
                # BTC/ETH cada 120-180s. Reduce latencia en mercados volatiles
                # sin disparar la cuota OpenRouter en mercados lentos.
                _cadence = get_market_cadence(symbol)
                _per_market_ttl = _cadence.get("ai_cache_ttl_seconds")
                _ttl_seconds = int(_per_market_ttl) if _per_market_ttl else int(settings.ai_min_interval_seconds)
                _per_market_delta = _cadence.get("price_delta_reuse_pct") or _AI_PRICE_DELTA_REUSE_PCT

                # Consulta IA por simbolo, con cache por simbolo (TTL adaptativo).
                cached_signal = _get_cached_ai_signal_for_symbol(
                    ai_signals_by_symbol, symbol, _ttl_seconds
                )
                # Tactical price-delta cache: if TTL expired but price barely moved,
                # reuse the verdict anyway. Critical to survive 429 storms.
                if cached_signal is None:
                    stale_entry = ai_signals_by_symbol.get(symbol)
                    if stale_entry and _can_reuse_ai_for_price(
                        stale_entry,
                        float(technical_signal.get("close") or 0.0),
                        threshold_pct=_per_market_delta,
                    ):
                        cached_signal = dict(stale_entry)
                        cached_signal["cached"] = True
                        cached_signal["cached_reason"] = "price_delta_below_threshold"
                        cached_signal["symbol"] = symbol
                        logger.info(
                            "IA price-delta cache hit para %s (delta < %.2f%%, ahorro de cuota).",
                            symbol,
                            float(_per_market_delta) * 100,
                        )
                if cached_signal is not None:
                    symbol_ai_signal = cached_signal
                    logger.info(
                        "IA cache hit para %s (%ss de antiguedad).",
                        symbol,
                        symbol_ai_signal.get("cached_age_seconds", 0),
                    )
                else:
                    # Keep the heartbeat fresh while waiting for the AI call
                    # (can take 10-30 s per symbol; 4 markets easily exceed 120 s).
                    write_heartbeat("online", f"IA analizando {symbol}…")
                    symbol_ai_signal = ai_analyzer.analyze(
                        scan["frame"],
                        symbol=symbol,
                        technical_signal=technical_signal,
                    )
                    symbol_ai_signal.setdefault("consulted", True)
                    symbol_ai_signal["symbol"] = symbol
                    symbol_ai_signal["cached"] = False
                    symbol_ai_signal["cached_age_seconds"] = 0.0
                    symbol_ai_signal["cached_at"] = datetime.now(timezone.utc).isoformat()
                    # Anchor the price at the moment of the consult so the next
                    # cycle can decide whether the cached verdict is still valid
                    # (see _can_reuse_ai_for_price).
                    symbol_ai_signal["price_at_consult"] = float(technical_signal.get("close") or 0.0)
                    ai_signals_by_symbol[symbol] = dict(symbol_ai_signal)
                    logger.info(
                        "IA evaluada en vivo para %s. signal=%s confidence=%.3f approved=%s setup=%s flags=%s",
                        symbol,
                        symbol_ai_signal.get("signal"),
                        float(symbol_ai_signal.get("confidence", 0.0)),
                        symbol_ai_signal.get("approved"),
                        symbol_ai_signal.get("setup_quality"),
                        symbol_ai_signal.get("risk_flags"),
                    )

                per_symbol_ai[symbol] = symbol_ai_signal
                symbol_guardrails = _build_guardrails(
                    settings, technical_signal, symbol_ai_signal, order_history, symbol=symbol
                )
                per_symbol_guardrails[symbol] = symbol_guardrails

                # Mejor candidato por conviccion (telemetria honesta del rechazo).
                current_best_conf = float((best_candidate_ai or {}).get("confidence", -1.0))
                if best_candidate_scan is None or float(symbol_ai_signal.get("confidence", 0.0)) > current_best_conf:
                    best_candidate_scan = scan
                    best_candidate_guards = symbol_guardrails
                    best_candidate_ai = symbol_ai_signal

                if all([
                    symbol_guardrails["executable_signal"],
                    symbol_guardrails["ai_gate_ready"],
                    symbol_guardrails["volatility_ready"],
                    symbol_guardrails["volume_ready"],
                    symbol_guardrails.get("regime_ready", True),
                    symbol_guardrails.get("score_ready", True),
                    not symbol_guardrails["cooldown_active"],
                    str(technical_signal["signal"]) == "buy",
                    symbol_ai_signal.get("consulted", False),
                ]):
                    # ── [SNIPER GATE] ─────────────────────────────────────────────────────
                    # Gates reforzados para meme-coins (WIF, DOGE, PEPE):
                    #  1. OB Imbalance >= 0.62 (precision gate: 50% es moneda al aire)
                    #  2. OI Delta no negativamente extremo (institucional liquidando)
                    #  3. Top Trader L/S ratio > 0.80 (ballenas no estan net-short)
                    #  4. Scenario B REQUIERE liquidation_spike >= $300k (V-Shape trigger)
                    # Fallos en Futures API son fail-open (no bloquean) para resiliencia.
                    _sniper_syms = {s.upper() for s in (settings.sniper_symbols or ())}
                    _is_sniper = symbol.upper() in _sniper_syms
                    _sniper_pass = True
                    _sniper_veto = None

                    if _is_sniper:
                        # 1. OB Imbalance Sniper Gate
                        _ob_sniper = float(symbol_guardrails.get("orderbook_imbalance") or 0.0)
                        if _ob_sniper < settings.sniper_ob_imbalance_min:
                            _sniper_pass = False
                            _sniper_veto = (
                                f"OB={_ob_sniper:.3f} < sniper_ob_min={settings.sniper_ob_imbalance_min:.2f} "
                                f"(meme-coin precision gate — compradores insuficientes)"
                            )

                        # 2. OI Delta Veto (fallo de fetch = fail-open)
                        if _sniper_pass and settings.oi_delta_veto_enabled:
                            try:
                                _oi_hist = client.fetch_open_interest_history(symbol, period="5m", limit=3)
                                if len(_oi_hist) >= 2 and _oi_hist[-2] > 0:
                                    _oi_delta_pct = (_oi_hist[-1] - _oi_hist[-2]) / _oi_hist[-2]
                                    if _oi_delta_pct < -0.02:  # OI cayo > 2% en 5 min
                                        _sniper_pass = False
                                        _sniper_veto = (
                                            f"OI_delta={_oi_delta_pct*100:+.2f}% < -2%% "
                                            f"(institucional cerrando posiciones, veto)"
                                        )
                            except Exception:  # noqa: BLE001
                                pass  # fail-open: no bloquear si futures API no responde

                        # 3. Top Trader L/S Ratio Veto
                        if _sniper_pass and settings.top_trader_ls_veto_enabled:
                            try:
                                _ls_ratio = client.fetch_top_trader_long_short_ratio(symbol)
                                if _ls_ratio is not None and _ls_ratio < 0.80:
                                    _sniper_pass = False
                                    _sniper_veto = (
                                        f"TopTrader_LS={_ls_ratio:.3f} < 0.80 "
                                        f"(ballenas net-short — no ir contra institucionales)"
                                    )
                            except Exception:  # noqa: BLE001
                                pass  # fail-open

                        # 4. Scenario B: REQUIERE liquidation spike >= $300k (V-Shape)
                        _scen = str(technical_signal.get("scenario") or "")
                        if _sniper_pass and _scen == "B":
                            try:
                                _liq_usd = client.fetch_liquidation_volume_usd(
                                    symbol, window_seconds=settings.sniper_liq_window_seconds
                                )
                                if _liq_usd < settings.sniper_liq_spike_usd:
                                    _sniper_pass = False
                                    _sniper_veto = (
                                        f"Scenario B requiere liquidation_spike >= "
                                        f"${settings.sniper_liq_spike_usd:,.0f} USD "
                                        f"(actual=${_liq_usd:,.0f}) — esperando capitulacion"
                                    )
                                else:
                                    logger.info(
                                        "SNIPER B V-Shape activado: liquidation_spike=%.0f USD "
                                        ">= %.0f threshold | %s",
                                        _liq_usd, settings.sniper_liq_spike_usd, symbol,
                                    )
                            except Exception:  # noqa: BLE001
                                pass  # fail-open: si no podemos leer liquidaciones, no bloqueamos

                    if not _sniper_pass:
                        logger.info(
                            "SNIPER GATE veto %s: %s",
                            symbol, _sniper_veto,
                        )
                        continue  # proximo simbolo en el scan loop
                    # ── [/SNIPER GATE] ────────────────────────────────────────────────────

                    chosen_scan = scan
                    chosen_guardrails = symbol_guardrails
                    ai_signal = symbol_ai_signal
                    ai_consulted_symbol = symbol
                    break

        # Si no se eligio ninguno, expone telemetria del mejor candidato evaluado.
        if chosen_scan is None and best_candidate_scan is not None:
            ai_signal = best_candidate_ai or ai_signal
            ai_consulted_symbol = best_candidate_scan["symbol"]

        if chosen_scan and chosen_guardrails:
            symbol = chosen_scan["symbol"]
            technical_signal = chosen_scan["technical_signal"]
            # ── Scenario C "Limit Sniping" gate (FOMO killer) ──────────────────
            # V3 cohort: Scenario C market entries had 16.7% win rate (chasing
            # tops). Rule: do NOT execute a market buy on a continuation signal
            # unless the current candle has already retraced >= 0.15% from its
            # close. We approximate the retrace using the candle low vs close:
            # if low <= close*(1 - 0.0015), the pullback already happened and
            # we can buy at a better price. Otherwise we wait. Compramos barato
            # o no compramos.
            if technical_signal.get("scenario") == "C":
                latest_candle = chosen_scan.get("latest_candle") or {}
                _close = float(technical_signal.get("close") or 0.0)
                _low = float(latest_candle.get("low") or _close)
                # Pullback per-market: WIF/DOGE 0.08-0.10% (turbo), BTC 0.20% (estricto).
                _pullback_pct = float(get_market_cadence(symbol).get("scenario_c_pullback_pct") or _SCENARIO_C_PULLBACK_PCT)
                _required_low = _close * (1.0 - _pullback_pct)
                if _close > 0 and _low > _required_low:
                    logger.info(
                        "Scenario C bloqueado por pullback insuficiente | %s low=%.6f need<=%.6f "
                        "(close=%.6f, retroceso requerido=%.2f%%). Esperando precio mejor.",
                        symbol, _low, _required_low, _close,
                        _pullback_pct * 100,
                    )
                    # Persist a synthetic decision so the dashboard knows why we
                    # didn't fire. No order is sent; cooldown is NOT triggered.
                    decision = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "symbol": symbol,
                        "side": "buy",
                        "amount": 0.0,
                        "price": round(_close, 6),
                        "signal_price": round(_close, 6),
                        "notional_usdt": 0.0,
                        "slippage_pct": 0.0,
                        "mode": "dry_run" if settings.dry_run else "live",
                        "status": "deferred_pullback",
                        "reason": (
                            f"Scenario C: esperando pullback >= {_pullback_pct*100:.2f}% "
                            f"(low={_low:.6f}, target<={_required_low:.6f})"
                        ),
                        "scenario": "C",
                        "entry_logic_tag": "scenario_c_pullback_pending",
                    }
                    append_history(settings.order_history_file, decision)
                    order_history = load_history(settings.order_history_file)
                    chosen_scan = None  # block the executor branch below

        if chosen_scan and chosen_guardrails:
            symbol = chosen_scan["symbol"]
            technical_signal = chosen_scan["technical_signal"]
            # ---- Conviction multiplier (sizing por calidad del setup) ----
            # Combina conviccion de la IA y score tecnico multifactor.
            # Mapea [0,1] -> [0.5, 1.0] para no apostar full size en setups borderline.
            conv_inputs = [
                float(ai_signal.get("confidence", 0.0) or 0.0),
                float(technical_signal.get("setup_score", 0.0) or 0.0),
            ]
            avg_conv = sum(conv_inputs) / len(conv_inputs) if conv_inputs else 0.0
            conviction_multiplier = round(0.5 + 0.5 * max(0.0, min(1.0, avg_conv)), 4)
            decision = executor.execute(
                side=str(technical_signal["signal"]),
                market_price=float(technical_signal["close"]),
                risk=risk_snapshot,
                symbol=symbol,
                atr_pct=float(technical_signal.get("atr_pct", 0.0)) or None,
                conviction_multiplier=conviction_multiplier,
            )
            decision["scenario"] = technical_signal.get("scenario")
            decision["entry_rsi"] = technical_signal.get("rsi")
            decision["symbol"] = symbol
            decision["regime"] = technical_signal.get("regime")
            decision["setup_score"] = technical_signal.get("setup_score")
            decision["conviction_multiplier"] = conviction_multiplier
            # Telemetry Patch: propagar el tag de lógica de entrada al decision dict
            # para que se persista en open_positions y order_history.
            decision["entry_logic_tag"] = chosen_guardrails.get("entry_logic_tag", "standard_ai")
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
                # Snapshot del perfil de mercado al momento de entrar (para auditoría histórica).
                market_settings = apply_market_profile(settings, symbol)
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
                        # Metadata de calidad para trailing/exit adaptativos.
                        "entry_atr_pct": float(technical_signal.get("atr_pct", 0.0) or 0.0),
                        "entry_regime": technical_signal.get("regime"),
                        "entry_setup_score": technical_signal.get("setup_score"),
                        "conviction_multiplier": decision.get("conviction_multiplier"),
                        "sl_pct_used": decision.get("sl_pct_used"),
                        "tp_pct_used": decision.get("tp_pct_used"),
                        # Fee de entrada real (disponible en live; None en DRY_RUN).
                        # Se usa al cerrar para calcular PnL neto exacto.
                        "entry_fee_quote": decision.get("fee_quote"),
                        # Telemetry Tag: qué lógica de bypass (si alguna) aprobó la entrada.
                        # standard_ai | bypass_ai | bypass_macro
                        "entry_logic_tag": decision.get("entry_logic_tag", "standard_ai"),
                        # ── Prompt-version cohort tracking ────────────────────────────
                        # Identifica qué versión del AI prompt aprobó este trade.
                        # Permite aislar métricas de V3 (regime-aware) de V1/V2.
                        # v3 = Regime-Aware Dynamic Risk Manager (commit e08e40e, 2026-05-13)
                        "ai_prompt_version": "v3",
                        "ai_risk_flags": list(ai_signal.get("risk_flags") or []),
                        "ai_setup_quality": ai_signal.get("setup_quality", "low"),
                        # REGIME detectado por la IA al evaluar: LOW_VOL | NORMAL | HIGH_VOL
                        # Extraído del rationale si la IA lo reporta, sino derivado de atr_pct.
                        "ai_regime": (
                            "LOW_VOL" if float(technical_signal.get("atr_pct", 0.0) or 0.0) < 0.0025
                            and float(technical_signal.get("bb_width_pct", 0.0) or 0.0) < 0.005
                            else "HIGH_VOL" if float(technical_signal.get("atr_pct", 0.0) or 0.0) >= 0.005
                            else "NORMAL"
                        ),
                        # micro_gate_path: "standard" | "micro_gate" (LOW_VOL con MICRO-GATE-A/B)
                        "ai_micro_gate_path": (
                            "micro_gate"
                            if (
                                float(technical_signal.get("atr_pct", 0.0) or 0.0) < 0.0025
                                and float(technical_signal.get("bb_width_pct", 0.0) or 0.0) < 0.005
                            )
                            else "standard"
                        ),
                        # ── /Prompt-version cohort tracking ──────────────────────────
                        # ── Per-market profile snapshot (audit) ──────────────────────
                        # Captura los umbrales EFECTIVOS del perfil de mercado al
                        # momento de entrar. Si después modificamos los perfiles,
                        # podremos auditar trades históricos contra los umbrales
                        # exactos que regían en ese momento.
                        "entry_profile": {
                            "scenario_a_rsi_max": float(getattr(market_settings, "scenario_a_rsi_max", 0.0)),
                            "scenario_b_rsi_max": float(getattr(market_settings, "scenario_b_rsi_max", 0.0)),
                            "min_atr_pct": float(getattr(market_settings, "min_atr_pct", 0.0)),
                            "max_atr_pct": float(getattr(market_settings, "max_atr_pct", 0.0)),
                            "min_volume_ratio": float(getattr(market_settings, "min_volume_ratio", 0.0)),
                            "min_orderbook_imbalance": float(getattr(market_settings, "min_orderbook_imbalance", 0.0)),
                            "min_trade_flow_score": float(getattr(market_settings, "min_trade_flow_score", 0.0)),
                            "max_spread_pct": float(getattr(market_settings, "max_spread_pct", 0.0)),
                            "ai_confidence_threshold": float(getattr(market_settings, "ai_confidence_threshold", 0.0)),
                        } if market_settings is not None else None,
                    }
                )
                persist_history(settings.open_positions_file, open_positions)
                global_lock = True
                active_symbol = symbol
                if decision.get("status") == "submitted":
                    # Propagate enrichment fields that executor adds to decision
                    decision["ai_confidence"] = ai_signal.get("confidence")
                    decision["ai_risk_flags"] = ai_signal.get("risk_flags") or []
                    decision["scenario"] = technical_signal.get("scenario")
                    decision["regime"] = technical_signal.get("regime")
                    decision["mode"] = "dry_run" if not live_mode else "live"
                    _notify_safe(notifier, logger, "trade_open", _format_trade_open_message(decision))
        else:
            # No hubo entrada: usamos el MEJOR candidato evaluado por IA como
            # referencia honesta del rechazo. Si no hubo ningun candidato, caemos
            # al primer scan para no dejar el dashboard vacio.
            primary_scan = best_candidate_scan or (scan_results[0] if scan_results else None)
            primary_signal = primary_scan["technical_signal"] if primary_scan else {"signal": "hold"}
            if best_candidate_scan is not None and best_candidate_guards is not None:
                primary_guards = best_candidate_guards
            elif primary_scan is not None:
                primary_guards = _build_guardrails(settings, primary_signal, ai_signal, order_history, symbol=primary_scan.get("symbol"))
            else:
                primary_guards = {}
            lock_detail = _describe_global_lock(open_positions) if global_lock else "guardarrailes sin match"
            decision = {
                "action": "hold",
                "reason": "Posicion abierta (mutex global)" if global_lock else "Ningun ticker cumplio los guardarrailes.",
                "detail": lock_detail,
                "global_lock": global_lock,
                "active_symbol": active_symbol,
                "symbol": primary_scan["symbol"] if primary_scan else None,
                "ai_consulted": ai_signal.get("consulted", False),
                "ai_consulted_symbol": ai_consulted_symbol,
                "ai_confidence": ai_signal.get("confidence", 0.0),
                **primary_guards,
            }
            logger.info(
                "Sin operacion en este ciclo. lock=%s active=%s focus=%s detail=%s scans=%s",
                global_lock,
                active_symbol,
                ai_consulted_symbol,
                lock_detail,
                [
                    (
                        s["symbol"],
                        s["technical_signal"].get("scenario"),
                        s["candidate_reason"],
                        round(float(per_symbol_ai.get(s["symbol"], {}).get("confidence", 0.0)), 3),
                    )
                    for s in scan_results
                ],
            )

        scan_summaries = [
            _build_scan_summary(
                {
                    **scan,
                    "ia_consulted": scan["symbol"] in per_symbol_ai,
                    "ia_confidence": float(per_symbol_ai.get(scan["symbol"], {}).get("confidence", 0.0)),
                    "ia_signal": per_symbol_ai.get(scan["symbol"]),
                    "ia_cached": bool(per_symbol_ai.get(scan["symbol"], {}).get("cached", False)),
                    "ia_cached_age_seconds": float(per_symbol_ai.get(scan["symbol"], {}).get("cached_age_seconds", 0.0) or 0.0),
                    "guardrails": per_symbol_guardrails.get(scan["symbol"]),
                },
                settings,
                blocked_by_lock=global_lock and scan["symbol"] != active_symbol,
            )
            for scan in scan_results
        ]

        # 7) Persistencia final con telemetria multi-ticker.
        primary_scan = chosen_scan or best_candidate_scan or (scan_results[0] if scan_results else None)
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
                ai_signals_by_symbol=ai_signals_by_symbol,
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
                market_profiles=market_profiles_summary(target_symbols),
                global_lock=global_lock,
                active_symbol=active_symbol,
                recovery=recovery_report,
                trade_monitor=trade_monitor_verdict or {},
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
    # Garantizar que el directorio de logs exista ANTES de cualquier escritura.
    # Critico en Docker: el volumen /data/logs se monta en runtime sobre el directorio
    # creado en la imagen; si el montaje tarda o falla, las escrituras explotan.
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Si ni siquiera podemos crear el directorio, fallar rapido con mensaje claro.
        raise RuntimeError(
            f"No se pudo crear/acceder al directorio de logs '{settings.logs_dir}': {exc}. "
            "Verifica el volumen persistente en Coolify."
        ) from exc
    logger = setup_logger(settings)
    notifier = _build_notifier(settings, logger)
    ensure_control_file()
    logger.info("OptiFerre-Trader iniciado. DRY_RUN=%s LOGS_DIR=%s", settings.dry_run, settings.logs_dir)

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