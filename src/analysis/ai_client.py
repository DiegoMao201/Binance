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
            "scenario_c": bool(ts.get("scenario_c", False)),
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
            "trade_flow_score": ts.get("trade_flow_score"),
            "trade_flow_ratio": ts.get("trade_flow_ratio"),
            "tape_momentum_pct": ts.get("tape_momentum_pct"),
            "quote_volume_24h": ts.get("quote_volume_24h"),
            "price_change_pct_24h": ts.get("price_change_pct_24h"),
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
                "trade_flow_score > 0.55 y tape_momentum_pct > 0 fortalecen setups de scalping (continuidad de micro-momentum).",
                "trade_flow_score < 0.45 sugiere tape débil o vendedor; evita aprobar buy salvo reversión excepcional.",
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
                    timeout=30.0,
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

        self.logger.warning(
            "OpenRouter sin respuesta valida para %s tras modelos=%s (%s). Activando evaluacion tecnica de fallback.",
            symbol or self.settings.trading_symbol,
            models_to_try,
            last_error,
        )
        fallback_result = self._technical_fallback_analyze(technical_signal)
        return _normalize(fallback_result, fallback_result.get("model", "technical_fallback"))

    def _technical_fallback_analyze(self, technical_signal: dict[str, Any] | None) -> dict[str, Any]:
        """Evaluacion tecnica pura cuando OpenRouter no responde tras intentar todos los modelos.

        Proporciona una aprobacion conservadora basada unicamente en indicadores tecnicos.
        Siempre incluye 'openrouter_unavailable' en risk_flags para que el operador sepa
        que la IA externa no estuvo disponible y que la decision fue local.
        """
        ts = technical_signal or {}
        scenario = str(ts.get("scenario") or "")
        signal = str(ts.get("signal", "hold"))
        setup_score = float(ts.get("setup_score") or 0.0)
        rsi = float(ts.get("rsi") or 50.0)
        rsi_slope = float(ts.get("rsi_slope") or 0.0)
        volume_acceleration = float(ts.get("volume_acceleration") or 0.0)
        orderbook_imbalance = float(ts.get("orderbook_imbalance") or 0.5)
        trade_flow_score = float(ts.get("trade_flow_score") or 0.5)
        macro_trend = str(ts.get("macro_trend") or "neutral").lower()
        green_candle = bool(ts.get("green_candle"))
        bullish_cross = bool(ts.get("bullish_cross"))
        candle_body_pct = float(ts.get("candle_body_pct") or 0.0)

        base_flags: list[str] = ["openrouter_unavailable", "technical_fallback_mode"]

        # Solo aplica para senales buy con escenario valido
        if signal != "buy" or scenario not in {"A", "B", "C", "D"}:
            return {
                "signal": "hold",
                "confidence": 0.0,
                "rationale": "Fallback tecnico: sin senal buy/escenario valido.",
                "model": "technical_fallback",
                "consulted": True,
                "timed_out": False,
                "approved": False,
                "direction_alignment": "misaligned",
                "setup_quality": "low",
                "risk_flags": base_flags,
            }

        risk_flags = list(base_flags)

        # Macro bearish bloquea salvo escenario B con agotamiento vendedor claro
        if macro_trend == "bearish":
            risk_flags.append("macro_bearish_pressure")
            if not (scenario == "B" and rsi < 30):
                return {
                    "signal": "hold",
                    "confidence": 0.0,
                    "rationale": f"Fallback tecnico: macro bearish bloquea entrada. scenario={scenario} rsi={rsi:.1f}",
                    "model": "technical_fallback",
                    "consulted": True,
                    "timed_out": False,
                    "approved": False,
                    "direction_alignment": "misaligned",
                    "setup_quality": "low",
                    "risk_flags": risk_flags,
                }

        if orderbook_imbalance < 0.40:
            risk_flags.append("orderbook_vs_signal")

        approved = False
        confidence = 0.0

        if scenario == "A":
            # Pullback con tendencia: RSI rebotando desde zona de valor
            cond_rsi = rsi <= 45 and rsi_slope >= -0.5
            cond_score = setup_score >= 0.48
            cond_ob = orderbook_imbalance >= 0.45
            approved = cond_rsi and cond_score and cond_ob
            if approved:
                confidence = 0.63 if setup_score >= 0.58 else 0.58
        elif scenario == "B":
            # Sobreventa extrema: RSI en zona de agotamiento
            cond_rsi = rsi <= 33 and rsi_slope >= -2.0
            cond_score = setup_score >= 0.48
            approved = cond_rsi and cond_score
            if approved:
                confidence = 0.64 if rsi <= 28 else 0.60
        elif scenario == "C":
            # Continuacion: vela verde solida + volumen acelerando + flow positivo
            cond_candle = green_candle and candle_body_pct >= 0.30
            cond_volume = volume_acceleration >= 0.85
            cond_score = setup_score >= 0.52
            cond_flow = trade_flow_score >= 0.50
            approved = cond_candle and cond_volume and cond_score and cond_flow
            if approved:
                confidence = 0.61
        elif scenario == "D":
            # EMA cross: senal tecnica objetiva
            cond_cross = bullish_cross
            cond_score = setup_score >= 0.48
            approved = cond_cross and cond_score
            if approved:
                confidence = 0.61

        # Penalizacion por orderbook en contra
        if "orderbook_vs_signal" in risk_flags and approved:
            confidence = max(0.0, confidence - 0.05)
            if confidence < 0.52:
                approved = False

        # Penalizacion por tape debil
        if trade_flow_score < 0.42 and approved:
            confidence = max(0.0, confidence - 0.04)
            risk_flags.append("tape_weak")

        setup_quality = "medium" if (approved and setup_score >= 0.52) else "low"

        self.logger.info(
            "Fallback tecnico para %s: approved=%s confidence=%.3f scenario=%s rsi=%.1f setup=%.3f ob=%.2f macro=%s",
            ts.get("symbol", "?"),
            approved,
            confidence,
            scenario,
            rsi,
            setup_score,
            orderbook_imbalance,
            macro_trend,
        )

        return {
            "signal": "buy" if approved else "hold",
            "confidence": round(confidence, 4),
            "rationale": (
                f"Fallback tecnico (OpenRouter no disponible): setup={setup_score:.2f} "
                f"scenario={scenario} rsi={rsi:.1f} ob={orderbook_imbalance:.2f} "
                f"approved={approved}"
            ),
            "model": "technical_fallback",
            "consulted": True,
            "timed_out": False,
            "approved": approved,
            "direction_alignment": "aligned" if approved else "misaligned",
            "setup_quality": setup_quality,
            "risk_flags": risk_flags,
        }
