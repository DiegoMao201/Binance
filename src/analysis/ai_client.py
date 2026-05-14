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
                # --- Formato obligatorio ---
                "RESPOND ONLY with a valid JSON object. No markdown, no extra text outside JSON.",
                "REQUIRED fields: signal (buy/sell/hold), confidence (float 0.0-1.0), rationale (string), approved (boolean), direction_alignment (aligned/misaligned), setup_quality (low/medium/high), risk_flags (array of strings).",
                "approved=true REQUIRES confidence >= 0.62. If confidence < 0.62, approved MUST be false. This is a hard rule.",
                # --- Vetoes absolutos (cualquiera = approved=false, confidence < 0.40) ---
                "VETO V1: macro_trend=bearish AND volume_ratio < 1.5 → approved=false, confidence < 0.40, risk_flag: counter_trend_no_volume.",
                "VETO V2: macro_trend=bearish AND orderbook_imbalance < 0.55 → approved=false, confidence < 0.40, risk_flag: counter_trend_weak_book.",
                "VETO V3: trade_flow_score < 0.40 AND orderbook_imbalance < 0.50 → approved=false, confidence < 0.40, risk_flag: dual_microstructure_bearish.",
                "VETO V4: atr_pct < 0.003 → approved=false, confidence < 0.40, risk_flag: chop_regime. Market is in dead range, SL fires before any move.",
                "VETO V5: bb_width_pct < 0.005 → approved=false, confidence < 0.40, risk_flag: bb_squeeze_undefined. Breakout direction unknown.",
                "VETO V6: macro_trend=bearish AND rsi_slope <= 0 → approved=false, confidence < 0.40, risk_flag: downtrend_rsi_still_falling.",
                "VETO V7: volume_acceleration < 0.70 → approved=false, confidence < 0.40, risk_flag: volume_exhaustion. No institutional backing.",
                # --- Regla de confluencia macro/micro ---
                "CONFLUENCE RULE: If macro signals (macro_trend, rsi_slope) and micro signals (trade_flow_score, orderbook_imbalance) contradict each other, set approved=false. Contradictory evidence = no statistical edge.",
                # --- Penalizaciones (reducen confidence) ---
                "PENALTY P1: macro_trend=bearish without veto → subtract 0.15 from base confidence.",
                "PENALTY P2: trade_flow_score < 0.50 → subtract 0.10.",
                "PENALTY P3: orderbook_imbalance < 0.52 → subtract 0.08.",
                "PENALTY P4: candle_body_pct < 0.35 → subtract 0.08, risk_flag: indecision_candle.",
                "PENALTY P5: rsi_slope <= 0 in oversold zone (rsi < 40) → subtract 0.10, risk_flag: rsi_not_turning.",
                "PENALTY P6: bb_position_pct > 0.60 → subtract 0.10, risk_flag: price_extended_from_value.",
                "PENALTY P7: spread_pct > 0.12 → subtract 0.07, risk_flag: high_slippage_risk.",
                # --- Boosts (aumentan confidence) ---
                "BOOST B1: macro_trend=bullish AND macro_slope_pct > 0.002 → add 0.12. Trend is confirmed ally.",
                "BOOST B2: volume_ratio > 1.5 AND volume_acceleration > 1.2 → add 0.10. Institutional volume surge.",
                "BOOST B3: rsi_slope > 2 AND rsi < 45 → add 0.10. RSI recovering from oversold with conviction.",
                "BOOST B4: orderbook_imbalance > 0.60 → add 0.08. Strong buy wall in book.",
                "BOOST B5: trade_flow_score > 0.60 AND tape_momentum_pct > 0 → add 0.08. Tape confirms buyers.",
                "BOOST B6: candle_body_pct > 0.65 AND consecutive_green >= 2 → add 0.07. Clean impulse with buildup.",
                "BOOST B7: bb_position_pct < 0.25 → add 0.05. Price at structural value zone.",
                # --- Síntesis de calidad ---
                "setup_quality=high: ALL of these true: macro_trend=bullish, volume_ratio > 1.3, rsi_slope > 0, orderbook_imbalance > 0.55, trade_flow_score > 0.55, candle_body_pct > 0.50.",
                "setup_quality=medium: 3-4 positive factors with no veto triggered and at most 1 penalty.",
                "setup_quality=low: veto triggered, or >= 2 penalties, or contradictory signals.",
                # --- Rationale exigido ---
                "rationale must cite AT LEAST 3 specific numeric values from candidate_context (e.g. 'volume_ratio=1.8, ob_imbalance=0.61, rsi_slope=+3.2'). Generic rationales are not acceptable.",
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

        # Cadena de modelos para ENTRADAS:
        #   1. openrouter_entry_model          → modelo pagado preciso (Gemini 2.5 Flash)
        #   2. openrouter_entry_fallback_models → fallbacks pagados (GPT-4.1 mini, etc.)
        #   3. openrouter_model + fallback_models → último recurso (puede ser free)
        models_to_try: list[str] = [self.settings.openrouter_entry_model]
        for candidate in self.settings.openrouter_entry_fallback_models:
            if candidate and candidate not in models_to_try:
                models_to_try.append(candidate)
        # Agregar modelo genérico y sus fallbacks como último recurso
        if self.settings.openrouter_model and self.settings.openrouter_model not in models_to_try:
            models_to_try.append(self.settings.openrouter_model)
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
                                    "You are the Chief Risk Officer of a quant hedge fund. "
                                    "Your SOLE function is to protect capital. You are DEEPLY skeptical by default. "
                                    "A setup must EARN your approval — it does not have it by default. "
                                    "You evaluate long entries on crypto spot/perpetual markets for a scalping bot with SL=-1.2% and tiered TP up to +2.4%. "
                                    "A bad approval bleeds the account. A missed trade costs nothing. "
                                    "\n"
                                    "ABSOLUTE VETO RULES — if ANY of these apply, set approved=false and confidence < 0.40, no exceptions:\n"
                                    "V1. macro_trend=bearish AND volume_ratio < 1.5 → VETO. A counter-trend bounce without exceptional volume is a trap.\n"
                                    "V2. macro_trend=bearish AND orderbook_imbalance < 0.55 → VETO. Orderbook must show overwhelming buy pressure to fight the trend.\n"
                                    "V3. trade_flow_score < 0.40 AND orderbook_imbalance < 0.50 → VETO. Both micro-structure signals are bearish. Do not fight tape AND book simultaneously.\n"
                                    "V4. atr_pct < 0.003 → VETO. Market is in consolidation/chop. SL will be hit before any real move. Label risk_flag: chop_regime.\n"
                                    "V5. bb_width_pct < 0.005 → VETO. Bollinger squeeze is extreme — breakout direction is a coin flip. Label risk_flag: bb_squeeze_undefined.\n"
                                    "V6. macro_trend=bearish AND rsi_slope < 0 → VETO. RSI is still falling in a downtrend. No reversal evidence.\n"
                                    "V7. volume_acceleration < 0.7 → VETO. Volume is drying up. The move has no institutional backing.\n"
                                    "\n"
                                    "PENALTY RULES — each applies a -0.08 to -0.15 confidence penalty:\n"
                                    "P1. macro_trend=bearish (without veto): -0.15. Counter-trend scalps have lower expected value.\n"
                                    "P2. trade_flow_score < 0.50: -0.10. Tape is not supporting the buy thesis.\n"
                                    "P3. orderbook_imbalance < 0.52: -0.08. Book is not confirming buy pressure.\n"
                                    "P4. candle_body_pct < 0.35: -0.08. Indecision candle. No directional conviction.\n"
                                    "P5. rsi_slope <= 0 in oversold zone: -0.10. RSI not yet turning. Catching a falling knife.\n"
                                    "P6. bb_position_pct > 0.60: -0.10. Price is extended, not at value.\n"
                                    "\n"
                                    "BOOST RULES — each adds +0.05 to +0.12 confidence:\n"
                                    "B1. macro_trend=bullish AND macro_slope_pct > 0.002: +0.12. Wind at the back.\n"
                                    "B2. volume_ratio > 1.5 AND volume_acceleration > 1.2: +0.10. Institutional-grade volume surge.\n"
                                    "B3. rsi_slope > 2 AND rsi < 45: +0.10. RSI recovering from oversold with momentum.\n"
                                    "B4. orderbook_imbalance > 0.60: +0.08. Strong buy wall confirmed.\n"
                                    "B5. trade_flow_score > 0.60 AND tape_momentum_pct > 0: +0.08. Tape confirms buyers in control.\n"
                                    "B6. candle_body_pct > 0.65 AND consecutive_green >= 2: +0.07. Clean impulse candle with buildup.\n"
                                    "B7. bb_position_pct < 0.25: +0.05. Price at structural support (BB lower).\n"
                                    "\n"
                                    "APPROVAL THRESHOLD: approved=true ONLY if final confidence >= 0.62 AFTER applying all penalties and boosts. "
                                    "Below 0.62, always set approved=false regardless of how the setup looks qualitatively. "
                                    "\n"
                                    "CONFLUENCE REQUIREMENT: If macro indicators (RSI trend, macro_trend) and micro indicators (trade_flow_score, orderbook_imbalance) "
                                    "contradict each other, ALWAYS resolve in favor of rejection. Contradictory signals = no edge. "
                                    "\n"
                                    "OUTPUT: Respond ONLY with a valid JSON object. "
                                    "Required fields: signal (buy/sell/hold), confidence (float 0-1), rationale (string, max 3 sentences citing SPECIFIC numbers), "
                                    "approved (boolean), direction_alignment (aligned/misaligned), setup_quality (low/medium/high), risk_flags (array of strings). "
                                    "Do NOT add extra fields. Do NOT include markdown. Do NOT explain your reasoning outside the rationale field."
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
                if model_name != self.settings.openrouter_entry_model:
                    parsed["fallback_used"] = True
                    parsed["fallback_from"] = self.settings.openrouter_entry_model
                self.logger.info(
                    "OpenRouter ENTRADA OK | symbol=%s model=%s fallback=%s",
                    symbol or self.settings.trading_symbol,
                    model_name,
                    model_name != self.settings.openrouter_entry_model,
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
