from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd
import requests

from src.utils.config import Settings


class OpenRouterAnalyzer:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def _build_payload(self, frame: pd.DataFrame, symbol: str | None = None) -> dict[str, Any]:
        sample = frame.tail(50)[
            [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "ema_fast",
                "ema_slow",
                "rsi",
                "bb_upper",
                "bb_lower",
            ]
        ].copy()
        sample["timestamp"] = sample["timestamp"].astype(str)

        prompt = {
            "symbol": symbol or self.settings.trading_symbol,
            "timeframe": self.settings.timeframe,
            "objetivo": "Emitir una opinión técnica prudente para scalping conservador.",
            "instrucciones": [
                "Responde solo JSON válido.",
                "Campos requeridos: signal, confidence, rationale.",
                "signal debe ser buy, sell o hold.",
                "confidence debe ir de 0 a 1.",
                "Evita señales agresivas si los datos no son concluyentes.",
            ],
            "ohlcv": sample.to_dict(orient="records"),
        }
        return prompt

    def analyze(self, frame: pd.DataFrame, symbol: str | None = None) -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            self.logger.warning("No hay API key de OpenRouter; se usará señal hold por seguridad.")
            return {
                "signal": "hold",
                "confidence": 0.0,
                "rationale": "OpenRouter no configurado.",
                "model": self.settings.openrouter_model,
            }

        payload = self._build_payload(frame, symbol=symbol)
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.openrouter_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Eres un analista cuant conservador. Prioriza preservación de capital.",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        },
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
                timeout=4.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (requests.Timeout, TimeoutError) as exc:
            self.logger.error("Timeout OpenRouter para %s: %s", symbol or self.settings.trading_symbol, exc)
            return {
                "signal": "hold",
                "confidence": 0.0,
                "rationale": "OpenRouter timeout; trade abortado por seguridad.",
                "model": self.settings.openrouter_model,
                "consulted": True,
                "timed_out": True,
                "timeout_seconds": 4.0,
            }
        parsed.setdefault("signal", "hold")
        parsed.setdefault("confidence", 0.0)
        parsed.setdefault("rationale", "Sin explicación.")
        parsed.setdefault("model", self.settings.openrouter_model)
        parsed.setdefault("timed_out", False)
        try:
            parsed["confidence"] = float(parsed["confidence"])
        except (TypeError, ValueError):
            parsed["confidence"] = 0.0
        return parsed