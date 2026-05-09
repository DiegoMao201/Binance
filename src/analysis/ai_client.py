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

        ts = technical_signal or {}
        candidate_context = {
            # --- Señal base ---
            "candidate_signal": ts.get("signal", "hold"),
            "scenario": ts.get("scenario"),
            "scenario_a": bool(ts.get("scenario_a", False)),
            "scenario_b": bool(ts.get("scenario_b", False)),
            # --- Momentum técnico ---
            "rsi": ts.get("rsi"),
            "rsi_slope": ts.get("rsi_slope"),          # >0 = RSI recuperándose, señal real
            "close": ts.get("close"),
            "ema_slow": ts.get("ema_slow"),
            "ema_slow_slope": ts.get("ema_slow_slope"),
            "bullish_cross": ts.get("bullish_cross"),
            # --- Fuerza de vela ---
            "green_candle": ts.get("green_candle"),
            "candle_body_pct": ts.get("candle_body_pct"),  # >0.6 = vela de convicción
            "consecutive_green": ts.get("consecutive_green"),  # buildup de momentum
            # --- Volatilidad y posición ---
            "atr_pct": ts.get("atr_pct"),
            "bb_width_pct": ts.get("bb_width_pct"),
            "bb_position_pct": ts.get("bb_position_pct"),  # 0=BB lower, 1=BB upper; <0.3 es zona de valor
            # --- Volumen ---
            "volume_ratio": ts.get("volume_ratio"),
            "volume_acceleration": ts.get("volume_acceleration"),  # >1.2 = aceleración real vs últimas 3
            # --- Orderbook (spot público) ---
            "orderbook_imbalance": ts.get("orderbook_imbalance"),
            "spread_pct": ts.get("spread_pct"),
            # --- Régimen macro 15m ---
            "macro_trend": ts.get("macro_trend"),
            "macro_slope_pct": ts.get("macro_slope_pct"),
        }

        prompt = {
            "symbol": symbol or self.settings.trading_symbol,
            "timeframe": self.settings.timeframe,
            "objetivo": "Validar o rechazar una entrada long ya prefiltrada por el motor técnico para scalping conservador.",
            "instrucciones": [
                "Responde solo JSON válido.",
                "Campos requeridos: signal, confidence, rationale, approved, direction_alignment, setup_quality, risk_flags.",
                "signal debe ser buy, sell o hold.",
                "confidence debe ir de 0 a 1 (usa el rango completo con convicción: no escales artificialmente hacia 0.5).",
                "approved debe ser true cuando el setup es técnicamente sólido; aprueba con convicción cuando corresponde.",
                "direction_alignment debe ser aligned o misaligned respecto a la señal técnica candidata.",
                "setup_quality debe ser low, medium o high. Sé preciso: high solo cuando múltiples factores convergen.",
                "risk_flags debe ser una lista concreta de riesgos reales; usa [] si no ves ninguno. No inventes flags genéricos.",
                "Esta candidata ya pasó los filtros técnicos; tu trabajo es confirmar timing y calidad, no filtrar por defecto.",
                # Fuerza de vela
                "candle_body_pct > 0.6 indica vela de convicción real (poco shadow = intención limpia). < 0.3 indica doji/incertidumbre.",
                "consecutive_green >= 2 + volume_acceleration > 1.2 = momentum construyéndose; es factor positivo para buy.",
                # RSI y momentum
                "rsi_slope > 0 después de zona de sobreventa es el patrón clave de rebote; refuerza fuertemente la señal.",
                "rsi_slope < -2 en zona media indica momentum que se debilita; añade risk_flag 'rsi_momentum_fading'.",
                # Posición en BB
                "bb_position_pct < 0.25 = precio cerca del soporte BB inferior = zona de valor óptima para long.",
                "bb_position_pct > 0.75 = precio extendido; para escenario A es aceptable si tendencia es fuerte; para B es señal de extensión peligrosa.",
                # Volumen
                "volume_ratio > 1.5 con vela verde = compra institucional, no retail; es confirmación fuerte.",
                "volume_acceleration < 0.7 = volumen secándose; el movimiento puede no tener seguimiento.",
                # Orderbook
                "orderbook_imbalance > 0.55 con señal buy = confirmación de presión compradora real en el libro.",
                "orderbook_imbalance < 0.45 contra señal buy = desalineación estructural; añade risk_flag 'orderbook_vs_signal'.",
                "spread_pct > 0.15 = mercado ilíquido; eleva el riesgo de slippage; menciona si es relevante.",
                # Macro 15m
                "macro_trend=bullish + slope positivo: entorno favorable, el setup tiene viento a favor. Sube confidence.",
                "macro_trend=bearish + slope_pct < -0.002: tendencia bajista confirmada en 15m. Solo aprueba si escenario B tiene señales de agotamiento vendedor muy claras.",
                "macro_trend=neutral: no bloquea ni refuerza; deja que los factores locales decidan.",
                # Síntesis
                "Setup perfecto (confidence >= 0.80): escenario A + macro bullish + candle_body_pct > 0.55 + volume_acceleration > 1.1 + rsi_slope > 0 + orderbook_imbalance > 0.52.",
                "Setup sólido (confidence 0.65-0.79): 3-4 de esos factores convergentes sin contradicciones fuertes.",
                "Setup débil (confidence < 0.55): múltiples factores contradictorios o datos inconclusos; approved=false.",
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
        models_to_try: list[str] = [self.settings.openrouter_model]
        for candidate in self.settings.openrouter_fallback_models:
            if candidate and candidate not in models_to_try:
                models_to_try.append(candidate)

        def _normalize(parsed: dict[str, Any], model_name: str) -> dict[str, Any]:
            parsed.setdefault("signal", "hold")
            parsed.setdefault("confidence", 0.0)
            parsed.setdefault("rationale", "Sin explicación.")
            parsed.setdefault("model", model_name)
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

        last_error = "unknown_error"
        for model_name in models_to_try:
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Eres un trader cuantitativo experto con 30 años de experiencia en mercados financieros: "
                                    "hedge fund macro, scalping institucional y gestión de riesgo avanzada. "
                                    "Tu criterio es preciso y basado en convergencia de señales, no en sesgo conservador genérico. "
                                    "Cuando el setup es sólido, apruebas con convicción alta y confidence real. "
                                    "Cuando hay debilidades estructurales, las identificas con precisión quirúrgica. "
                                    "Nunca rechazas por inercia ni apruebas por cortesía. "
                                    "Maximizas la tasa de éxito identificando el timing de máxima probabilidad dentro de setups ya filtrados técnicamente."
                                ),
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
                if model_name != self.settings.openrouter_model:
                    parsed["fallback_used"] = True
                    parsed["fallback_from"] = self.settings.openrouter_model
                self.logger.info(
                    "OpenRouter OK para %s con modelo=%s fallback=%s",
                    symbol or self.settings.trading_symbol,
                    model_name,
                    model_name != self.settings.openrouter_model,
                )
                return _normalize(parsed, model_name)
            except (requests.Timeout, TimeoutError) as exc:
                last_error = f"timeout:{exc}"
                self.logger.warning(
                    "Timeout OpenRouter para %s con modelo=%s. Probando fallback si existe.",
                    symbol or self.settings.trading_symbol,
                    model_name,
                )
            except (requests.RequestException, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = f"model_error:{exc}"
                self.logger.warning(
                    "OpenRouter fallo para %s con modelo=%s (%s). Probando fallback si existe.",
                    symbol or self.settings.trading_symbol,
                    model_name,
                    exc,
                )

        self.logger.error(
            "OpenRouter sin respuesta valida para %s tras modelos=%s (%s)",
            symbol or self.settings.trading_symbol,
            models_to_try,
            last_error,
        )
        return {
            "signal": "hold",
            "confidence": 0.0,
            "rationale": "OpenRouter fallo en todos los modelos configurados; trade abortado por seguridad.",
            "model": self.settings.openrouter_model,
            "consulted": True,
            "timed_out": True,
            "timeout_seconds": 4.0,
            "approved": False,
            "direction_alignment": "misaligned",
            "setup_quality": "low",
            "risk_flags": ["openrouter_unavailable"],
        }
