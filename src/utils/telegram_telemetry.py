from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any

import aiohttp


MARKDOWN_V2_RESERVED_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    escaped = []
    for char in text:
        if char in MARKDOWN_V2_RESERVED_CHARS:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


class TelegramTelemetry:
    def __init__(
        self,
        *,
        enabled: bool,
        logger: logging.Logger,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float = 4.0,
    ) -> None:
        self.enabled = enabled
        self.logger = logger
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.timeout_seconds = timeout_seconds
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="telegram-telemetry", daemon=True)
        self._thread.start()

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def send_alert_nowait(self, level: str, data: dict[str, Any]) -> Future[Any] | None:
        if not self.configured:
            return None

        try:
            return asyncio.run_coroutine_threadsafe(self.send_alert(level, data), self._loop)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("No se pudo programar alerta Telegram: %s", exc)
            return None

    async def send_alert(self, level: str, data: dict[str, Any]) -> None:
        if not self.configured:
            return

        payload = {
            "chat_id": self.chat_id,
            "text": self._render_message(level, data)[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._api_url(), data=payload) as response:
                    if response.status == 429:
                        # Rate-limited — log silently and continue, never block trading.
                        self.logger.warning("Telegram rate-limit (429) — mensaje descartado")
                    elif response.status >= 400:
                        body = await response.text()
                        self.logger.warning(
                            "Telegram devolvio %s: %s",
                            response.status,
                            body[:500],
                        )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("No se pudo enviar notificacion Telegram: %s", exc)

    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def _render_message(self, level: str, data: dict[str, Any]) -> str:
        normalized = level.strip().lower()
        if normalized == "trade_open":
            return self._render_trade_open(data)
        if normalized == "trade_close":
            return self._render_trade_close(data)
        if normalized == "radar":
            return self._render_radar(data)
        # Legacy level names kept for backward compatibility.
        if normalized == "trade":
            return self._render_trade_open(data) if data.get("side", "").upper() != "CLOSE" else self._render_trade_close(data)
        if normalized == "critical":
            return self._render_critical(data)
        return self._render_sys(data)

    # ── Rich renderers (HTML) ────────────────────────────────────────────────

    def _render_trade_open(self, data: dict[str, Any]) -> str:
        """🟢 Trade Opened — HTML."""
        symbol  = _h(str(data.get("symbol") or "N/D"))
        side    = _h(str(data.get("side") or "BUY").upper())
        entry   = _h(self._fmt_price(data.get("entry_price") or data.get("fill_price") or data.get("price") or data.get("entry")))
        sl      = _h(self._fmt_price(data.get("stop_loss") or data.get("sl")))
        tp      = _h(self._fmt_price(data.get("take_profit") or data.get("tp")))
        conf    = data.get("ai_confidence")
        conf_s  = f"{round(float(conf) * 100)}%" if conf is not None else "n/d"
        scenario = _h(str(data.get("scenario") or "–"))
        regime   = _h(str(data.get("regime") or "–"))
        logic    = _h(str(data.get("entry_logic_tag") or "standard_ai"))
        mode_raw = str(data.get("mode") or "live").upper()
        mode_icon = "🧪" if "DRY" in mode_raw else "💶"
        flags_raw = data.get("ai_risk_flags") or []
        flags_s = _h(", ".join(flags_raw)) if flags_raw else "ninguno"
        notional = data.get("notional_usdt")
        notional_s = f"{float(notional):.2f} USDT" if notional else "n/d"
        lines = [
            f"🟢 <b>TRADE ABIERTO</b> {mode_icon}",
            f"<b>{symbol}</b>  ·  {side}  ·  Escenario <b>{scenario}</b>",
            "",
            f"📍 Entrada   <code>{entry}</code>",
            f"🛑 Stop Loss <code>{sl}</code>",
            f"🎯 Take Profit <code>{tp}</code>",
            f"💰 Nocional  <code>{notional_s}</code>",
            "",
            f"🤖 IA Convicción  <b>{_h(conf_s)}</b>",
            f"🌡 Régimen        <b>{regime}</b>",
            f"🔖 Lógica entrada <code>{logic}</code>",
            f"⚠️ Risk flags     {flags_s}",
        ]
        return "\n".join(lines)

    def _render_trade_close(self, data: dict[str, Any]) -> str:
        """🔴/🔵 Trade Closed — HTML."""
        symbol  = _h(str(data.get("symbol") or "N/D"))
        pnl_raw = data.get("pnl_usdt", 0.0)
        try:
            pnl = float(pnl_raw)
        except (TypeError, ValueError):
            pnl = 0.0
        pnl_pct_raw = data.get("pnl_pct")
        try:
            pnl_pct = float(pnl_pct_raw)
            pnl_pct_s = f"{pnl_pct:+.2f}%"
        except (TypeError, ValueError):
            pnl_pct_s = "n/d"
        pnl_icon = "🔵" if pnl >= 0 else "🔴"
        pnl_sign  = "+" if pnl >= 0 else ""
        exit_raw  = str(data.get("exit_reason") or data.get("reason") or "desconocido")
        exit_icon = {
            "take_profit": "✅", "tp": "✅",
            "stop_loss": "🛑", "sl": "🛑",
            "microstructure_bailout": "🧬",
            "hard_timeout": "⏱",
            "ai_emergency_exit": "🤖",
            "manual": "🖐",
            "trailing_tier": "📈",
            "break_even": "⚖️",
            "stagnation_timeout": "⏳",
        }.get(exit_raw.lower(), "📤")
        entry   = _h(self._fmt_price(data.get("entry_price") or data.get("entry")))
        exit_p  = _h(self._fmt_price(data.get("exit_price") or data.get("exit")))
        hold_m  = data.get("hold_minutes")
        hold_s  = f"{float(hold_m):.0f} min" if hold_m else "n/d"
        mode_raw = str(data.get("mode") or "live").upper()
        mode_icon = "🧪" if "DRY" in mode_raw else "💶"
        lines = [
            f"{pnl_icon} <b>TRADE CERRADO</b> {mode_icon}",
            f"<b>{symbol}</b>  ·  {exit_icon} <code>{_h(exit_raw)}</code>",
            "",
            f"📍 Entrada  <code>{entry}</code>",
            f"🏁 Salida   <code>{exit_p}</code>",
            f"⏱ Duración <code>{_h(hold_s)}</code>",
            "",
            f"💰 PnL neto <b>{_h(pnl_sign)}{_h(f'{pnl:.4f}')} USDT</b>  ({_h(pnl_pct_s)})",
        ]
        return "\n".join(lines)

    def _render_radar(self, data: dict[str, Any]) -> str:
        """🟡 Radar Alert — ultra-compact single-line format (low-noise policy)."""
        symbol = _h(str(data.get("symbol") or "N/D"))
        gate   = _h(str(data.get("gate") or "gate"))
        prox   = data.get("proximity_pct")
        prox_s = _h(f"{float(prox):.0f}") if prox else "??"
        return f"🟡 [RADAR] <b>{symbol}</b> al <b>{prox_s}%</b> de cruzar <code>{gate}</code>"

    # ── Legacy renderers (kept for backward-compat, now emit HTML too) ───────

    def _render_trade(self, data: dict[str, Any]) -> str:
        """Alias para compatibilidad: delega a trade_open o trade_close."""
        return self._render_trade_close(data) if str(data.get("side", "")).upper() == "CLOSE" else self._render_trade_open(data)

    def _render_critical(self, data: dict[str, Any]) -> str:
        title  = _h(str(data.get("title") or data.get("event") or "CRÍTICO"))
        detail = _h(str(data.get("detail") or data.get("message") or "Sin detalle"))
        status = _h(str(data.get("status") or "APAGADO").upper())
        return "\n".join([
            f"🔴 <b>CRÍTICO: {title}</b>",
            detail,
            f"Estado: <b>{status}</b>",
        ])

    def _render_sys(self, data: dict[str, Any]) -> str:
        title  = _h(str(data.get("title") or data.get("event") or "ALERTA RED"))
        detail = _h(str(data.get("detail") or data.get("message") or ""))
        cycle_text = _h(self._fmt_cycle(data.get("cycle_seconds") or data.get("cycle")))
        summary = f"Ciclo: {cycle_text}"
        if detail:
            summary = f"{summary}  ({detail})"
        return "\n".join([
            f"🟡 <b>{title}</b>",
            summary,
        ])

    def _fmt_price(self, value: Any) -> str:
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return "n/d"

    def _fmt_signed(self, value: Any, *, suffix: str = "") -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"0.00{suffix}"
        sign = "+" if number >= 0 else ""
        return f"{sign}{number:.4f}{suffix}"

    def _fmt_cycle(self, value: Any) -> str:
        try:
            return f"{float(value):.1f}s"
        except (TypeError, ValueError):
            return "n/d"


def _h(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse_mode."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
