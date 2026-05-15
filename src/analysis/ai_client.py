from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from src.utils.config import Settings


class OpenRouterAnalyzer:
    # 429 backoff config (tactical cache + exponential wait)
    _MIN_BACKOFF_SECONDS = 60
    _MAX_BACKOFF_SECONDS = 600  # 10 min cap

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        # Rate-limit state shared across all symbols (OpenRouter quota is per-key).
        self._rate_limit_until: datetime | None = None
        self._consecutive_429: int = 0

    # ─── 429 helpers ────────────────────────────────────────────────────────
    def _is_in_429_backoff(self) -> tuple[bool, float]:
        if self._rate_limit_until is None:
            return False, 0.0
        remaining = (self._rate_limit_until - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            self._rate_limit_until = None
            self._consecutive_429 = 0
            return False, 0.0
        return True, remaining

    def _trigger_429_backoff(self, retry_after_header: str | None) -> float:
        """Exponential backoff respecting Retry-After header when present."""
        self._consecutive_429 += 1
        # Honour Retry-After if the API gave us one (seconds, integer).
        explicit: float | None = None
        if retry_after_header:
            try:
                explicit = float(retry_after_header)
            except (TypeError, ValueError):
                explicit = None
        backoff = explicit if explicit is not None else min(
            self._MAX_BACKOFF_SECONDS,
            self._MIN_BACKOFF_SECONDS * (2 ** (self._consecutive_429 - 1)),
        )
        backoff = max(backoff, self._MIN_BACKOFF_SECONDS)
        self._rate_limit_until = datetime.now(timezone.utc) + timedelta(seconds=backoff)
        return backoff

    @staticmethod
    def _normalize_payload(parsed: dict[str, Any], model_name: str) -> dict[str, Any]:
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
                # --- Paso 1: clasificar régimen obligatorio ---
                "STEP 1 — Classify volatility regime FIRST: REGIME=HIGH_VOL if atr_pct >= 0.005 or bb_width_pct >= 0.010; REGIME=NORMAL if atr_pct >= 0.0025 or bb_width_pct >= 0.005; REGIME=LOW_VOL otherwise. State regime in rationale.",
                # --- Paso 2: Hard vetoes (todos los regímenes) ---
                "HARD VETO V1: macro_trend=bearish AND volume_ratio < 1.5 → approved=false, confidence < 0.40, risk_flag: counter_trend_no_volume.",
                "HARD VETO V2: macro_trend=bearish AND orderbook_imbalance < 0.55 → approved=false, confidence < 0.40, risk_flag: counter_trend_weak_book.",
                "HARD VETO V3: trade_flow_score < 0.40 AND orderbook_imbalance < 0.50 → approved=false, confidence < 0.40, risk_flag: dual_microstructure_bearish.",
                "HARD VETO V4: macro_trend=bearish AND rsi_slope <= 0 → approved=false, confidence < 0.40, risk_flag: downtrend_rsi_still_falling.",
                "HARD VETO V5: volume_acceleration < 0.60 → approved=false, confidence < 0.40, risk_flag: volume_exhaustion.",
                # --- Paso 3: Vetos condicionales por régimen ---
                "REGIME-VETO C1 (chop): ACTIVE if REGIME=HIGH_VOL or NORMAL and atr_pct < 0.003 → approved=false, risk_flag: chop_regime. SUSPENDED if REGIME=LOW_VOL — apply MICRO-GATE-A instead: trade_flow_score >= 0.62 AND orderbook_imbalance >= 0.57 required; if not met → approved=false, risk_flag: low_vol_no_micro_edge.",
                "REGIME-VETO C2 (BB squeeze): ACTIVE if REGIME=HIGH_VOL or NORMAL and bb_width_pct < 0.005 → approved=false, risk_flag: bb_squeeze_undefined. SUSPENDED if REGIME=LOW_VOL — apply MICRO-GATE-B instead: rsi_slope > 1 AND candle_body_pct >= 0.45 required; if not met → approved=false, risk_flag: low_vol_no_momentum_confirmation.",
                "LOW_VOL PRINCIPLE: In compressed markets, book+flow strength IS the edge — it signals pre-breakout accumulation. If both MICRO-GATE-A and MICRO-GATE-B pass in LOW_VOL regime, treat as valid setup. Do not invent extra reasons to reject.",
                # --- Paso 4: Confluencia ---
                "CONFLUENCE RULE: macro (macro_trend, rsi_slope) vs micro (trade_flow_score, orderbook_imbalance) contradiction → approved=false. Exception: in LOW_VOL regime, passing both micro-gates overrides neutral macro (macro_trend=neutral only).",
                # --- Paso 5: Penalizaciones ---
                "PENALTY P1: macro_trend=bearish (no hard veto) → -0.15.",
                "PENALTY P2: trade_flow_score < 0.50 → -0.10.",
                "PENALTY P3: orderbook_imbalance < 0.52 → -0.08.",
                "PENALTY P4: candle_body_pct < 0.35 → -0.08, risk_flag: indecision_candle.",
                "PENALTY P5: rsi_slope <= 0 AND rsi < 40 → -0.10, risk_flag: rsi_not_turning.",
                "PENALTY P6: bb_position_pct > 0.60 → -0.10, risk_flag: price_extended_from_value.",
                "PENALTY P7: spread_pct > 0.12 → -0.07, risk_flag: high_slippage_risk.",
                "PENALTY P8: REGIME=LOW_VOL → -0.05 (compressed expected reward, not a disqualifier).",
                # --- Paso 6: Boosts ---
                "BOOST B1: macro_trend=bullish AND macro_slope_pct > 0.002 → +0.12.",
                "BOOST B2: volume_ratio > 1.5 AND volume_acceleration > 1.2 → +0.10.",
                "BOOST B3: rsi_slope > 2 AND rsi < 45 → +0.10.",
                "BOOST B4: orderbook_imbalance > 0.60 → +0.08.",
                "BOOST B5: trade_flow_score > 0.60 AND tape_momentum_pct > 0 → +0.08.",
                "BOOST B6: candle_body_pct > 0.65 AND consecutive_green >= 2 → +0.07.",
                "BOOST B7: bb_position_pct < 0.25 → +0.05.",
                "BOOST B8: REGIME=LOW_VOL AND MICRO-GATE-A passed AND MICRO-GATE-B passed → +0.08 (pre-breakout accumulation signal).",
                # --- Umbral y formato ---
                "APPROVAL THRESHOLD: approved=true ONLY if confidence >= 0.62 after all adjustments.",
                "RESPOND ONLY with a valid JSON object. No markdown, no extra text.",
                "REQUIRED fields: signal (buy/sell/hold), confidence (float 0.0-1.0), rationale (string — must state regime, cite >= 3 specific values, list which rules fired), approved (boolean), direction_alignment (aligned/misaligned), setup_quality (low/medium/high), risk_flags (array of strings).",
                "setup_quality=high: macro=bullish, volume_ratio > 1.3, rsi_slope > 0, ob_imbalance > 0.55, flow_score > 0.55, candle_body > 0.50.",
                "setup_quality=medium: 3-4 positive factors, no hard veto, at most 1 penalty.",
                "setup_quality=low: any veto triggered, or >= 2 penalties, or contradictory signals.",
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

        # ─── 429 backoff guard ───────────────────────────────────────────────
        # If we recently got a 429, do NOT hammer the API again. Skip directly to
        # the local technical fallback so the bot keeps trading without burning quota.
        in_backoff, remaining = self._is_in_429_backoff()
        if in_backoff:
            self.logger.warning(
                "OpenRouter en 429-backoff (%.0fs restantes). Usando fallback técnico para %s.",
                remaining,
                symbol or self.settings.trading_symbol,
            )
            fb = self._technical_fallback_analyze(technical_signal)
            flags = list(fb.get("risk_flags") or [])
            if "openrouter_rate_limited" not in flags:
                flags.append("openrouter_rate_limited")
            fb["risk_flags"] = flags
            fb["rate_limited"] = True
            fb["rate_limit_remaining_s"] = round(remaining, 1)
            return self._normalize_payload(fb, fb.get("model", "technical_fallback"))

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
            return self._normalize_payload(parsed, model_name)

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
                                    "You are a Dynamic Risk Manager at a quant hedge fund. "
                                    "Your function is to protect capital AND keep the bot operational. "
                                    "You evaluate long entries for a scalping bot with SL=-1.2% and tiered TP up to +2.4%. "
                                    "A bad approval bleeds the account. But permanent starvation (refusing every trade) also kills the strategy. "
                                    "Your job is to find VIABLE trades within the CURRENT market regime, not to seek excuses to reject. "
                                    "\n"
                                    "━━━ STEP 1 — CLASSIFY CURRENT VOLATILITY REGIME ━━━\n"
                                    "Before applying any rule, determine the regime from atr_pct and bb_width_pct:\n"
                                    "REGIME=HIGH_VOL  if atr_pct >= 0.005 OR bb_width_pct >= 0.010\n"
                                    "REGIME=NORMAL    if atr_pct >= 0.0025 OR bb_width_pct >= 0.005\n"
                                    "REGIME=LOW_VOL   if atr_pct < 0.0025 AND bb_width_pct < 0.005\n"
                                    "Include regime classification in your rationale.\n"
                                    "\n"
                                    "━━━ STEP 2 — HARD VETOES (apply in ALL regimes) ━━━\n"
                                    "These are structural disqualifiers. Any single one → approved=false, confidence < 0.40:\n"
                                    "V1. macro_trend=bearish AND volume_ratio < 1.5 → counter_trend_no_volume. Counter-trend without exceptional volume is a trap.\n"
                                    "V2. macro_trend=bearish AND orderbook_imbalance < 0.55 → counter_trend_weak_book. Must show overwhelming buy pressure against trend.\n"
                                    "V3. trade_flow_score < 0.40 AND orderbook_imbalance < 0.50 → dual_microstructure_bearish. Both micro signals bearish simultaneously = no edge.\n"
                                    "V4. macro_trend=bearish AND rsi_slope <= 0 → downtrend_rsi_still_falling. No reversal evidence.\n"
                                    "V5. volume_acceleration < 0.60 → volume_exhaustion. Move has no institutional backing.\n"
                                    "\n"
                                    "━━━ STEP 3 — REGIME-CONDITIONAL VETOES ━━━\n"
                                    "These vetoes are ACTIVE only in HIGH_VOL or NORMAL regimes. In LOW_VOL regime they are SUSPENDED and replaced by stricter micro-structure requirements:\n"
                                    "\n"
                                    "VETO-C1 (chop/low-ATR): ACTIVE if REGIME=HIGH_VOL or NORMAL AND atr_pct < 0.003 → chop_regime, approved=false.\n"
                                    "VETO-C1 SUSPENDED in LOW_VOL regime. Instead apply MICRO-GATE-A: trade_flow_score >= 0.62 AND orderbook_imbalance >= 0.57 required, else approved=false.\n"
                                    "\n"
                                    "VETO-C2 (BB squeeze): ACTIVE if REGIME=HIGH_VOL or NORMAL AND bb_width_pct < 0.005 → bb_squeeze_undefined, approved=false.\n"
                                    "VETO-C2 SUSPENDED in LOW_VOL regime. Instead apply MICRO-GATE-B: rsi_slope > 1 AND candle_body_pct >= 0.45 required, else approved=false.\n"
                                    "\n"
                                    "LOW_VOL LOGIC: In a compressed market, micro-structure IS the edge. A strong book + strong flow in a squeeze is a pre-breakout accumulation signal. "
                                    "Approve it when the micro-gates pass. Reject it when micro-structure is absent.\n"
                                    "\n"
                                    "━━━ STEP 4 — PENALTY RULES (reduce confidence) ━━━\n"
                                    "P1. macro_trend=bearish (no veto): -0.15.\n"
                                    "P2. trade_flow_score < 0.50: -0.10.\n"
                                    "P3. orderbook_imbalance < 0.52: -0.08.\n"
                                    "P4. candle_body_pct < 0.35: -0.08, risk_flag: indecision_candle.\n"
                                    "P5. rsi_slope <= 0 in oversold (rsi < 40): -0.10, risk_flag: rsi_not_turning.\n"
                                    "P6. bb_position_pct > 0.60: -0.10, risk_flag: price_extended_from_value.\n"
                                    "P7. spread_pct > 0.12: -0.07, risk_flag: high_slippage_risk.\n"
                                    "P8. REGIME=LOW_VOL (informational): -0.05. Low volatility compresses expected reward. Not a disqualifier, just a reality check.\n"
                                    "\n"
                                    "━━━ STEP 5 — BOOST RULES (increase confidence) ━━━\n"
                                    "B1. macro_trend=bullish AND macro_slope_pct > 0.002: +0.12.\n"
                                    "B2. volume_ratio > 1.5 AND volume_acceleration > 1.2: +0.10.\n"
                                    "B3. rsi_slope > 2 AND rsi < 45: +0.10.\n"
                                    "B4. orderbook_imbalance > 0.60: +0.08.\n"
                                    "B5. trade_flow_score > 0.60 AND tape_momentum_pct > 0: +0.08.\n"
                                    "B6. candle_body_pct > 0.65 AND consecutive_green >= 2: +0.07.\n"
                                    "B7. bb_position_pct < 0.25: +0.05.\n"
                                    "B8. REGIME=LOW_VOL AND MICRO-GATE-A passed AND MICRO-GATE-B passed: +0.08. Both micro-gates active = strong pre-breakout signal.\n"
                                    "\n"
                                    "━━━ STEP 6 — CONFLUENCE CHECK ━━━\n"
                                    "If macro indicators (macro_trend, rsi_slope) and micro indicators (trade_flow_score, orderbook_imbalance) contradict each other, "
                                    "ALWAYS resolve in favor of rejection. Contradictory evidence = no statistical edge. "
                                    "Exception: In LOW_VOL regime, micro-gates passing overrides neutral macro. Micro IS the signal in compressed markets.\n"
                                    "\n"
                                    "━━━ APPROVAL THRESHOLD ━━━\n"
                                    "approved=true ONLY if final confidence >= 0.62 AFTER all penalties and boosts. Below 0.62 → approved=false, no exceptions.\n"
                                    "\n"
                                    "━━━ OUTPUT FORMAT ━━━\n"
                                    "Respond ONLY with a valid JSON object. "
                                    "Required fields: signal (buy/sell/hold), confidence (float 0-1), rationale (string — state regime, cite >= 3 specific numeric values, list which vetoes/penalties/boosts applied), "
                                    "approved (boolean), direction_alignment (aligned/misaligned), setup_quality (low/medium/high), risk_flags (array of strings). "
                                    "Do NOT add extra fields. Do NOT include markdown. Do NOT explain outside the rationale field."
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
                # Detect 429 BEFORE raise_for_status so we can backoff gracefully.
                if response.status_code == 429:
                    backoff_s = self._trigger_429_backoff(response.headers.get("Retry-After"))
                    self.logger.error(
                        "OpenRouter 429 Too Many Requests para %s (modelo=%s). Backoff %.0fs (consecutivos=%d).",
                        symbol or self.settings.trading_symbol,
                        model_name,
                        backoff_s,
                        self._consecutive_429,
                    )
                    last_error = f"rate_limited:{backoff_s:.0f}s"
                    # Do NOT try further models — quota is per-key, all will 429.
                    break
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                # Reset 429 counter on first successful response.
                self._consecutive_429 = 0
                self._rate_limit_until = None
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
