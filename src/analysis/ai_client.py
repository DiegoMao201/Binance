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

    def _build_payload(
        self,
        frame: pd.DataFrame,
        symbol: str | None = None,
        technical_signal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

        candidate_context = {
            "candidate_signal": (technical_signal or {}).get("signal", "hold"),
            "scenario": (technical_signal or {}).get("scenario"),
            "scenario_a": bool((technical_signal or {}).get("scenario_a", False)),
            "scenario_b": bool((technical_signal or {}).get("scenario_b", False)),
            "rsi": (technical_signal or {}).get("rsi"),
            "close": (technical_signal or {}).get("close"),
            "ema_slow": (technical_signal or {}).get("ema_slow"),
            "atr_pct": (technical_signal or {}).get("atr_pct"),
            "volume_ratio": (technical_signal or {}).get("volume_ratio"),
            "green_candle": (technical_signal or {}).get("green_candle"),
            "bullish_cross": (technical_signal or {}).get("bullish_cross"),
            "ema_slow_slope": (technical_signal or {}).get("ema_slow_slope"),
        }

        prompt = {
            "symbol": symbol or self.settings.trading_symbol,
            "timeframe": self.settings.timeframe,
            "objetivo": "Validar o rechazar una entrada long ya prefiltrada por el motor técnico para scalping conservador.",
            "instrucciones": [
                "Responde solo JSON válido.",
                "Campos requeridos: signal, confidence, rationale, approved, direction_alignment, setup_quality, risk_flags.",
                "signal debe ser buy, sell o hold.",
                "confidence debe ir de 0 a 1.",
                "approved debe ser true solo si aceptarías la entrada con criterio profesional y conservador.",
                "direction_alignment debe ser aligned o misaligned respecto a la señal técnica candidata.",
                "setup_quality debe ser low, medium o high.",
                "risk_flags debe ser una lista corta de riesgos concretos; usa [] si no ves ninguno.",
                "Estás revisando una candidata ya filtrada por reglas técnicas; no estás generando una idea desde cero.",
                "Si el escenario es A y la tendencia/pullback siguen razonables, setup_quality=medium puede seguir siendo aprobable.",
                "Usa hold y approved=false cuando veas desalineación clara, estructura débil o riesgo concreto; evita rechazar por inercia.",
                "Evita señales agresivas si los datos no son concluyentes.",
            ],
            "candidate_context": candidate_context,
            "ohlcv": sample.to_dict(orient="records"),
        }
        return prompt

    def analyze(
        self,
        frame: pd.DataFrame,
        symbol: str | None = None,
        technical_signal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            self.logger.warning("No hay API key de OpenRouter; se usará señal hold por seguridad.")
            return {
                "signal": "hold",
                "confidence": 0.0,
                "rationale": "OpenRouter no configurado.",
                "model": self.settings.openrouter_model,
                "approved": False,
                "direction_alignment": "misaligned",
                "setup_quality": "low",
                "risk_flags": ["openrouter_not_configured"],
            }

        payload = self._build_payload(frame, symbol=symbol, technical_signal=technical_signal)
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
                "approved": False,
                "direction_alignment": "misaligned",
                "setup_quality": "low",
                "risk_flags": ["openrouter_timeout"],
            }
        parsed.setdefault("signal", "hold")
        parsed.setdefault("confidence", 0.0)
        parsed.setdefault("rationale", "Sin explicación.")
        parsed.setdefault("model", self.settings.openrouter_model)
        parsed.setdefault("timed_out", False)
        parsed.setdefault("approved", False)
        parsed.setdefault("direction_alignment", "misaligned")
        parsed.setdefault("setup_quality", "low")
        parsed.setdefault("risk_flags", [])
        try:
            parsed["confidence"] = float(parsed["confidence"])
        except (TypeError, ValueError):
            parsed["confidence"] = 0.0
        parsed["approved"] = bool(parsed.get("approved", False))
        if parsed.get("direction_alignment") not in {"aligned", "misaligned"}:
            parsed["direction_alignment"] = "misaligned"
        if parsed.get("setup_quality") not in {"low", "medium", "high"}:
            parsed["setup_quality"] = "low"
        if not isinstance(parsed.get("risk_flags"), list):
            parsed["risk_flags"] = [str(parsed.get("risk_flags"))]
        return parsed
