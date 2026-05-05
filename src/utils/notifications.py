from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(slots=True)
class TelegramNotifier:
    enabled: bool
    bot_token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token and self.chat_id)

    def send(self, message: str) -> None:
        if not self.configured:
            return

        response = requests.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": message[:4000],
                "disable_web_page_preview": "true",
            },
            timeout=10,
        )
        response.raise_for_status()