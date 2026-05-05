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
            "text": self._render_message(level, data)[:4000],
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._api_url(), data=payload) as response:
                    if response.status >= 400:
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
        if normalized == "trade":
            return self._render_trade(data)
        if normalized == "critical":
            return self._render_critical(data)
        return self._render_sys(data)

    def _render_trade(self, data: dict[str, Any]) -> str:
        side = escape_markdown_v2(str(data.get("side") or data.get("direction") or "LONG").upper())
        symbol = escape_markdown_v2(str(data.get("symbol") or "N/D"))
        entry = escape_markdown_v2(self._fmt_price(data.get("entry") or data.get("entry_price") or data.get("fill_price")))
        stop_loss = escape_markdown_v2(self._fmt_price(data.get("stop_loss") or data.get("sl")))
        pnl = escape_markdown_v2(self._fmt_signed(data.get("pnl_usdt"), suffix=" USDT"))
        return "\n".join(
            [
                f"🟢 {side} {symbol}",
                f"In: {entry} | SL: {stop_loss}",
                f"PNL: {pnl}",
            ]
        )

    def _render_critical(self, data: dict[str, Any]) -> str:
        title = escape_markdown_v2(str(data.get("title") or data.get("event") or "CRÍTICO"))
        detail = escape_markdown_v2(str(data.get("detail") or data.get("message") or "Sin detalle"))
        status = escape_markdown_v2(str(data.get("status") or "APAGADO").upper())
        return "\n".join(
            [
                f"🔴 CRÍTICO: {title}",
                detail,
                f"Estado: {status}",
            ]
        )

    def _render_sys(self, data: dict[str, Any]) -> str:
        title = escape_markdown_v2(str(data.get("title") or data.get("event") or "ALERTA RED"))
        cycle_seconds = data.get("cycle_seconds") or data.get("cycle")
        detail = escape_markdown_v2(str(data.get("detail") or data.get("message") or ""))
        cycle_text = escape_markdown_v2(self._fmt_cycle(cycle_seconds))
        summary = f"Ciclo: {cycle_text}"
        if detail:
            summary = f"{summary} ({detail})"
        return "\n".join(
            [
                f"🟡 {title}",
                summary,
            ]
        )

    def _fmt_price(self, value: Any) -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "n/d"

    def _fmt_signed(self, value: Any, *, suffix: str = "") -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"0.00{suffix}"
        sign = "+" if number >= 0 else ""
        return f"{sign}{number:.2f}{suffix}"

    def _fmt_cycle(self, value: Any) -> str:
        try:
            return f"{float(value):.1f}s"
        except (TypeError, ValueError):
            return "n/d"
